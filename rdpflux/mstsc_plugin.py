from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import time
from ctypes import byref, c_int, c_void_p
from ctypes.wintypes import DWORD

from .config import ClientConfig
from .forwarding import ClientForwarder
from .mux import MuxPeer
from .registry import PLUGIN_CLSID
from .transport import CallbackTransport

LOG = logging.getLogger(__name__)
CHANNEL_NAME = b"com.rdpflux.v1"
DVC_WRITE_CHUNK_LENGTH = 1590


class _ConfigReloader:
    """Re-read the client config per channel so an RDP reconnect picks up edits.

    The COM server outlives individual RDP sessions, so a config loaded once at
    process start could only be refreshed by killing the plugin.
    """

    def __init__(self, config: ClientConfig, factory=None) -> None:
        self._config = config
        self._factory = factory

    def current(self) -> ClientConfig:
        if self._factory is None:
            return self._config
        try:
            self._config = self._factory()
        except Exception:
            LOG.exception("reloading the client config failed; keeping the previous one")
        return self._config


class _ChannelRuntime:
    def __init__(self, channel, config: ClientConfig) -> None:
        self.channel = channel
        self.config = config
        self.transport = CallbackTransport(self._write)
        self.thread = threading.Thread(target=self._thread_main, name="rdpflux-dvc", daemon=True)
        self.started = threading.Event()
        self.closed = threading.Event()
        self.stopping = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.startup_error: Exception | None = None

    def start(self) -> None:
        self.thread.start()
        if not self.started.wait(5):
            self.close()
            raise TimeoutError("mstsc channel runtime did not start within 5 seconds")
        # Only failures from before the runtime signalled readiness can be
        # returned synchronously to OnNewChannelConnection. Later failures close
        # the accepted DVC so the agent can attach a fresh channel.
        if self.startup_error:
            raise self.startup_error

    def _write(self, data: bytes) -> None:
        if not data:
            return
        # The RDP stack limits IWTSVirtualChannel.Write to 1590 bytes per call.
        # FrameDecoder reconstructs protocol frames across these DVC messages.
        for offset in range(0, len(data), DVC_WRITE_CHUNK_LENGTH):
            chunk = data[offset:offset + DVC_WRITE_CHUNK_LENGTH]
            buffer = (ctypes.c_ubyte * len(chunk)).from_buffer_copy(chunk)
            hr = self.channel.Write(len(chunk), buffer, None)
            code = int(getattr(hr, "value", hr))
            if code != 0:
                raise OSError(f"IWTSVirtualChannel.Write failed: 0x{code & 0xFFFFFFFF:08x}")

    def feed(self, data: bytes) -> None:
        if self.closed.is_set():
            return
        self.transport.feed_from_thread(data)

    def close(self, timeout: float = 5.0) -> None:
        self.stopping.set()
        self.transport.eof_from_thread()
        self._cancel_task()
        if self.thread is not threading.current_thread() and self.thread.is_alive():
            self.thread.join(timeout)
            if self.thread.is_alive():
                LOG.warning("mstsc channel runtime did not stop within %.1f seconds", timeout)

    def _cancel_task(self) -> None:
        loop, task = self.loop, self.task
        if loop is None or task is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # The loop finished between the check above and the call.
            pass

    def _thread_main(self) -> None:
        try:
            try:
                asyncio.run(self._run())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.error = exc
                LOG.exception("mstsc channel runtime failed")
                if not self.started.is_set():
                    self.startup_error = exc
        finally:
            self.started.set()
            self.closed.set()
            if not self.stopping.is_set():
                # A mux failure cannot be recovered by starting a second protocol
                # session over the same DVC. Close it so the agent opens a fresh one.
                try:
                    self.channel.Close()
                except Exception:
                    LOG.debug("closing failed mstsc DVC raised", exc_info=True)

    async def _run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        peer = MuxPeer(self.transport, role="client", max_streams=self.config.max_streams)
        forwarder = ClientForwarder(peer, self.config)
        self.started.set()
        try:
            await forwarder.start()
            await peer.wait_closed()
        finally:
            await forwarder.close()


def run_com_server(config: ClientConfig, *, smoke_test: bool = False, config_factory=None) -> int:
    try:
        from win32more._comclass import ComClass
        from win32more._win32api import E_FAIL, Guid, HRESULT, S_OK
        from win32more.Windows.Win32.Foundation import CLASS_E_NOAGGREGATION, RPC_E_CHANGED_MODE, RPC_E_TOO_LATE
        from win32more.Windows.Win32.System.Com import (
            CLSCTX_LOCAL_SERVER,
            COINIT_MULTITHREADED,
            REGCLS_MULTIPLEUSE,
            REGCLS_SUSPENDED,
            RPC_C_AUTHN_LEVEL_DEFAULT,
            RPC_C_IMP_LEVEL_IMPERSONATE,
            CoInitializeEx,
            CoInitializeSecurity,
            CoRegisterClassObject,
            CoResumeClassObjects,
            CoRevokeClassObject,
            CoUninitialize,
            IClassFactory,
        )
        from win32more.Windows.Win32.System.RemoteDesktop import (
            IWTSListener,
            IWTSListenerCallback,
            IWTSPlugin,
            IWTSVirtualChannelCallback,
            IWTSVirtualChannelManager,
        )
        from win32more.Windows.Win32.UI.WindowsAndMessaging import (
            DispatchMessageW,
            MSG,
            PeekMessageW,
            PM_REMOVE,
            TranslateMessage,
            WM_QUIT,
        )
    except ImportError as exc:
        raise RuntimeError("mstsc support requires: pip install 'rdpflux[mstsc]'") from exc

    shutdown = threading.Event()
    reloader = _ConfigReloader(config, config_factory)

    def hr_value(value) -> int:
        return int(getattr(value, "value", value if value is not None else E_FAIL))

    def add_ref(obj) -> None:
        try:
            obj.AddRef()
        except Exception:
            pass

    def release(obj) -> None:
        try:
            obj.Release()
        except Exception:
            pass

    class TunnelPlugin(ComClass, IWTSPlugin, IWTSListenerCallback, IWTSVirtualChannelCallback):
        _clsid_ = PLUGIN_CLSID

        def __init__(self):
            ComClass.__init__(self)
            self.listener = None
            self.channel = None
            self.channel_ref = False
            self.runtime: _ChannelRuntime | None = None

        def Initialize(self, manager: IWTSVirtualChannelManager) -> int:
            try:
                listener = IWTSListener()
                result = manager.CreateListener(CHANNEL_NAME, 0, self, byref(listener))
                if hr_value(result) == 0:
                    self.listener = listener
                return hr_value(result)
            except Exception:
                LOG.exception("IWTSPlugin.Initialize failed")
                return hr_value(E_FAIL)

        def Connected(self) -> int:
            return hr_value(S_OK)

        def Disconnected(self, _code: int) -> int:
            self._close_channel()
            return hr_value(S_OK)

        def Terminated(self) -> int:
            self._close_channel()
            shutdown.set()
            return hr_value(S_OK)

        def OnNewChannelConnection(self, channel, _data, accept, callback) -> int:
            accept[0] = False
            callback[0] = None
            if not channel:
                return hr_value(E_FAIL)
            try:
                self._close_channel()
                self.channel = channel
                add_ref(channel)
                self.channel_ref = True
                channel_config = reloader.current()
                LOG.info("channel config: %d local, %d socks, %d reverse",
                         len(channel_config.local_forwards), len(channel_config.socks),
                         len(channel_config.reverse_forwards))
                self.runtime = _ChannelRuntime(channel, channel_config)
                # The channel is not writable until mstsc has accepted this
                # callback.  Publish acceptance before starting the runtime;
                # otherwise its initial HELLO can be dropped, while a later
                # HELLO_ACK succeeds and leaves the peer half-handshaken.
                accept[0] = True
                callback[0] = self
                self.runtime.start()
                return hr_value(S_OK)
            except Exception:
                accept[0] = False
                callback[0] = None
                LOG.exception("opening the mstsc channel failed")
                self._close_channel()
                return hr_value(E_FAIL)

        def OnDataReceived(self, size: int, data_ptr) -> int:
            if self.runtime and self.runtime.closed.is_set():
                # The runtime thread exited (see logged traceback); tear the channel down
                # instead of silently dropping data.
                self._close_channel()
                return hr_value(E_FAIL)
            if size > 0 and data_ptr and self.runtime:
                try:
                    self.runtime.feed(ctypes.string_at(data_ptr, size))
                except Exception:
                    LOG.exception("feeding channel data failed")
                    self._close_channel()
                    return hr_value(E_FAIL)
            return hr_value(S_OK)

        def OnClose(self) -> int:
            self._close_channel()
            return hr_value(S_OK)

        def _close_channel(self) -> None:
            if self.runtime:
                self.runtime.close()
                self.runtime = None
            if self.channel and self.channel_ref:
                release(self.channel)
            self.channel = None
            self.channel_ref = False

    class PluginFactory(ComClass, IClassFactory):
        def CreateInstance(self, outer: c_void_p, iid: Guid, output: c_void_p) -> HRESULT:
            if outer:
                return HRESULT(CLASS_E_NOAGGREGATION)
            try:
                return TunnelPlugin().QueryInterface(iid, output)
            except Exception:
                LOG.exception("CreateInstance failed")
                return HRESULT(E_FAIL)

        def LockServer(self, _lock: c_int) -> HRESULT:
            return HRESULT(S_OK)

    result = CoInitializeEx(None, COINIT_MULTITHREADED)
    if result < 0 and result != RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitializeEx failed: 0x{result & 0xFFFFFFFF:08x}")
    result = CoInitializeSecurity(None, -1, None, None, RPC_C_AUTHN_LEVEL_DEFAULT, RPC_C_IMP_LEVEL_IMPERSONATE, None, 0, None)
    if result < 0 and result != RPC_E_TOO_LATE:
        LOG.warning("CoInitializeSecurity returned 0x%08x", result & 0xFFFFFFFF)
    cookie = DWORD()
    result = CoRegisterClassObject(Guid(PLUGIN_CLSID), PluginFactory(), CLSCTX_LOCAL_SERVER,
                                   REGCLS_MULTIPLEUSE | REGCLS_SUSPENDED, byref(cookie))
    if result != 0:
        CoUninitialize()
        raise OSError(f"CoRegisterClassObject failed: 0x{result & 0xFFFFFFFF:08x}")
    try:
        result = CoResumeClassObjects()
        if result != 0:
            raise OSError(f"CoResumeClassObjects failed: 0x{result & 0xFFFFFFFF:08x}")
        LOG.info("mstsc DVC COM server ready for %s", CHANNEL_NAME.decode())
        if smoke_test:
            shutdown.set()
        message = MSG()
        while not shutdown.is_set():
            if PeekMessageW(byref(message), None, 0, 0, PM_REMOVE):
                if message.message == WM_QUIT:
                    break
                TranslateMessage(byref(message))
                DispatchMessageW(byref(message))
            else:
                time.sleep(0.01)
    finally:
        CoRevokeClassObject(cookie.value)
        CoUninitialize()
    return 0

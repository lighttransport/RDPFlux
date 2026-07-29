import ctypes
import os
import threading

import pytest

from rdpflux.mux import describe_exception
from rdpflux.config import ClientConfig
from rdpflux.mstsc_plugin import _ChannelRuntime


@pytest.mark.skipif(os.name != "nt", reason="mstsc COM is Windows-only")
def test_mstsc_com_server_registration_smoke():
    pytest.importorskip("win32more")
    from rdpflux.mstsc_plugin import run_com_server

    assert run_com_server(ClientConfig(), smoke_test=True) == 0


def test_channel_runtime_close_joins_worker():
    class Channel:
        @staticmethod
        def Write(_size, _buffer, _reserved):
            return 0

    runtime = _ChannelRuntime(Channel(), ClientConfig())
    runtime.start()
    runtime.close()
    assert runtime.closed.is_set()
    assert not runtime.thread.is_alive()


def test_channel_write_uses_ubyte_buffer():
    """IWTSVirtualChannel.Write declares pBuffer as POINTER(Byte); c_char buffers are rejected."""
    captured = []

    class Channel:
        @staticmethod
        def Write(size, buffer, _reserved):
            captured.append((size, buffer))
            return 0

    runtime = _ChannelRuntime(Channel(), ClientConfig())
    runtime._write(b"hello")
    size, buffer = captured[0]
    assert size == 5
    assert ctypes.POINTER(ctypes.c_ubyte).from_param(buffer) is not None
    assert bytes(buffer) == b"hello"

    runtime._write(b"")
    assert len(captured) == 1, "empty writes must not reach the channel"


def test_channel_write_splits_at_the_dvc_limit():
    from rdpflux.mstsc_plugin import DVC_WRITE_CHUNK_LENGTH

    captured = []

    class Channel:
        @staticmethod
        def Write(size, buffer, _reserved):
            captured.append(bytes(buffer[:size]))
            return 0

    payload = bytes(range(256)) * 20
    _ChannelRuntime(Channel(), ClientConfig())._write(payload)
    assert all(len(chunk) <= DVC_WRITE_CHUNK_LENGTH for chunk in captured)
    assert b"".join(captured) == payload


def test_channel_runtime_failure_closes_the_dvc():
    attempts = []
    closed = threading.Event()

    class Channel:
        @staticmethod
        def Write(_size, _buffer, _reserved):
            attempts.append(1)
            raise OSError("channel write failed")

        @staticmethod
        def Close():
            closed.set()

    runtime = _ChannelRuntime(Channel(), ClientConfig())
    runtime.start()
    runtime.thread.join(15)
    assert not runtime.thread.is_alive()
    assert runtime.closed.is_set()
    assert len(attempts) == 1
    assert closed.is_set(), "a failed mux must reconnect on a fresh DVC"


def test_channel_runtime_startup_failure_is_raised():
    class Channel:
        @staticmethod
        def Write(_size, _buffer, _reserved):
            return 0

    runtime = _ChannelRuntime(Channel(), ClientConfig())
    runtime.config = None  # forces MuxPeer construction to fail before started is set
    with pytest.raises(Exception):
        runtime.start()
    runtime.close()


def test_config_reloader_picks_up_edits_per_channel():
    from rdpflux.mstsc_plugin import _ConfigReloader

    first = ClientConfig(max_streams=1)
    second = ClientConfig(max_streams=2)
    versions = [first, second]
    reloader = _ConfigReloader(first, lambda: versions.pop(0))
    assert reloader.current() is first
    assert reloader.current() is second, "a reconnect must see config edits"


def test_config_reloader_keeps_the_last_good_config():
    from rdpflux.mstsc_plugin import _ConfigReloader

    good = ClientConfig(max_streams=7)

    def broken():
        raise ValueError("bad json")

    reloader = _ConfigReloader(good, broken)
    # A malformed edit must not take the channel down with it.
    assert reloader.current() is good
    assert reloader.current() is good


def test_config_reloader_without_a_factory_is_static():
    from rdpflux.mstsc_plugin import _ConfigReloader

    config = ClientConfig()
    reloader = _ConfigReloader(config)
    assert reloader.current() is config


def test_log_path_defaults_to_plugin_log_only_under_com():
    from rdpflux.client import _log_path, _parser
    from rdpflux.paths import default_client_log

    args = _parser().parse_args(["run"])
    assert _log_path(args, embedding=True) == default_client_log()
    assert _log_path(args, embedding=False) is None, "interactive runs stay on stderr"


def test_log_path_honours_explicit_and_disabled():
    from rdpflux.client import _log_path, _parser

    explicit = _parser().parse_args(["run", "--log-file", r"C:\tmp\rdpflux.log"])
    assert str(_log_path(explicit, embedding=False)) == r"C:\tmp\rdpflux.log"
    assert str(_log_path(explicit, embedding=True)) == r"C:\tmp\rdpflux.log"

    disabled = _parser().parse_args(["run", "--no-log-file"])
    assert _log_path(disabled, embedding=True) is None
    assert _log_path(disabled, embedding=False) is None


def test_configure_logging_writes_to_the_file(tmp_path):
    import logging

    from rdpflux.client import _configure_logging, _parser

    target = tmp_path / "nested" / "client.log"
    args = _parser().parse_args(["run", "--log-file", str(target), "--verbose"])
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        _configure_logging(args, embedding=True)
        logging.getLogger("rdpflux.test").warning("hello from the plugin")
        for handler in root.handlers:
            handler.flush()
        assert target.exists(), "the parent directory must be created"
        assert "hello from the plugin" in target.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved


def test_describe_names_empty_exceptions():
    assert describe_exception(TimeoutError()) == "TimeoutError"
    assert describe_exception(ConnectionError("refused")) == "refused"

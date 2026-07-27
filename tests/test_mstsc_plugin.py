import os

import pytest

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

import os

import pytest

from rdp2tcp.config import ClientConfig


@pytest.mark.skipif(os.name != "nt", reason="mstsc COM is Windows-only")
def test_mstsc_com_server_registration_smoke():
    pytest.importorskip("win32more")
    from rdp2tcp.mstsc_plugin import run_com_server

    assert run_com_server(ClientConfig(), smoke_test=True) == 0

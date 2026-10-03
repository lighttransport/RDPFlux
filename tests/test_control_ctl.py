import sys

import pytest

from rdpflux.control import ctl
from rdpflux.control.client import ControlError
from rdpflux.control.files import FileStore
from rdpflux.control.http_client import HTTPControlClient

from tests.test_control_http import serve


async def run(port, argv, token="secret"):
    args = ctl.build_parser().parse_args(["--url", f"http://127.0.0.1:{port}", "--token", token,
                                          "--retry-delay", "0", *argv])
    client = HTTPControlClient(args.url, args.token, timeout=30)
    return await args.handler(client, args)


@pytest.mark.asyncio
async def test_put_get_and_ls_round_trip(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    port, _, close = await serve(files=FileStore(root))
    try:
        local = tmp_path / "payload.bin"
        local.write_bytes(bytes(range(256)) * 1024)  # larger than one 64 KiB header
        assert await run(port, ["put", str(local), "sub/payload.bin", "--create-parents"]) == 0
        fetched = tmp_path / "fetched.bin"
        assert await run(port, ["get", "sub/payload.bin", str(fetched)]) == 0
        assert fetched.read_bytes() == local.read_bytes()
        assert await run(port, ["ls", "sub"]) == 0
    finally:
        await close()


@pytest.mark.asyncio
async def test_exec_passes_exit_code_and_output(capsys):
    port, _, close = await serve(allow_exec=True)
    try:
        code = await run(port, ["exec", "--", sys.executable, "-c",
                                "import sys; print('hello'); sys.exit(3)"])
        assert code == 3
        assert "hello" in capsys.readouterr().out
    finally:
        await close()


@pytest.mark.asyncio
async def test_exec_reports_truncated_output(capsys):
    port, _, close = await serve(allow_exec=True)
    try:
        code = await run(port, ["exec", "--", sys.executable, "-c", "print('x' * 200000)"])
        assert code == 0
        assert "output truncated" in capsys.readouterr().err
    finally:
        await close()


@pytest.mark.asyncio
async def test_errors_are_not_retried_unless_transient():
    port, _, close = await serve(token="secret")
    try:
        with pytest.raises(ControlError, match="HTTP 401"):
            await run(port, ["diag"], token="wrong")
    finally:
        await close()


@pytest.mark.asyncio
async def test_transient_failures_are_retried(monkeypatch):
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ControlError("HTTP 502: agent closed the control stream without replying")
        return "done"

    assert await ctl._with_retries(flaky, retries=3, delay=0) == "done"
    assert len(calls) == 3
    calls.clear()

    async def denied():
        calls.append(1)
        raise ControlError("HTTP 403: forbidden")

    with pytest.raises(ControlError):
        await ctl._with_retries(denied, retries=3, delay=0)
    assert len(calls) == 1

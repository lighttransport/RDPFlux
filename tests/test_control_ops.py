import asyncio
import sys

import pytest

from rdpflux.control import execute
from rdpflux.control.actions import ActionError
from rdpflux.control.client import ControlClient, ControlError
from rdpflux.control.files import FileRoot, FileRule, FileStore
from rdpflux.control.service import ControlService
from rdpflux.forwarding import AgentForwarder
from rdpflux.mux import MuxPeer
from rdpflux.transport import MemoryTransport

from tests.test_control import FakeBackend


# --- file store ----------------------------------------------------------

def test_file_store_round_trip(tmp_path):
    store = FileStore(tmp_path)
    store.write("notes/todo.txt", b"hello", create_parents=True)
    meta, data = store.read("notes/todo.txt")
    assert data == b"hello"
    assert meta["size"] == 5


def test_file_store_lists_a_directory(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    listing = store_list(tmp_path)
    names = {entry["name"]: entry for entry in listing["entries"]}
    assert names["a.txt"]["dir"] is False and names["a.txt"]["size"] == 1
    assert names["sub"]["dir"] is True and names["sub"]["size"] is None


def store_list(root):
    return FileStore(root).list()


@pytest.mark.parametrize("path", ["../escape.txt", "sub/../../escape.txt", "/etc/passwd",
                                  "C:/Windows/system32/x", "notes/../../outside"])
def test_file_store_rejects_traversal(tmp_path, path):
    (tmp_path / "notes").mkdir()
    store = FileStore(tmp_path)
    with pytest.raises(ActionError, match="escapes the file root"):
        store.resolve(path)


def test_file_store_rejects_a_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.write_bytes(b"secret")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")
    with pytest.raises(ActionError, match="escapes the file root"):
        FileStore(root).read("link")


def test_file_store_requires_an_existing_root(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        FileStore(tmp_path / "missing")


def test_file_store_write_needs_an_existing_parent_without_create(tmp_path):
    with pytest.raises(ActionError, match="parent directory does not exist"):
        FileStore(tmp_path).write("deep/nested/file.txt", b"x")


def test_file_store_allowlist_modes_and_denylist(tmp_path):
    (tmp_path / "read-only.txt").write_bytes(b"read")
    (tmp_path / "write-only.txt").write_bytes(b"old")
    (tmp_path / "private.txt").write_bytes(b"secret")
    store = FileStore(
        tmp_path,
        allowlist=[
            FileRule("read-only.txt", "read"),
            FileRule("write-only.txt", "write"),
            FileRule("private.txt", "read_write"),
        ],
        denylist=["private.txt"],
    )
    assert store.read("read-only.txt")[1] == b"read"
    with pytest.raises(ActionError, match="read access is denied"):
        store.read("write-only.txt")
    with pytest.raises(ActionError, match="write access is denied"):
        store.write("read-only.txt", b"no")
    with pytest.raises(ActionError, match="read access is denied"):
        store.read("private.txt")
    with pytest.raises(ActionError, match="write access is denied"):
        store.write("private.txt", b"no")
    assert store.write("write-only.txt", b"new")["size"] == 3


def test_file_store_glob_rules_cover_subdirectories(tmp_path):
    (tmp_path / "safe").mkdir()
    (tmp_path / "safe" / "nested.txt").write_bytes(b"ok")
    (tmp_path / "blocked").mkdir()
    (tmp_path / "blocked" / "nested.txt").write_bytes(b"no")
    store = FileStore(tmp_path, allowlist=[FileRule("safe/**", "read_write")],
                      denylist=["safe/private/**"])
    assert store.read("safe/nested.txt")[1] == b"ok"
    with pytest.raises(ActionError, match="access is denied"):
        store.read("blocked/nested.txt")


def test_file_store_names_multiple_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_bytes(b"one")
    (second / "two.txt").write_bytes(b"two")
    store = FileStore(roots=[FileRoot("one", first), FileRoot("two", second)])
    assert store.read("one:/one.txt")[1] == b"one"
    assert store.read("two:/two.txt")[1] == b"two"
    with pytest.raises(ActionError, match="root prefix"):
        store.read("one.txt")
    with pytest.raises(ActionError, match="unknown file root"):
        store.read("other:/file.txt")


# --- exec ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_exec_captures_output_and_exit_code():
    result = await execute.run({"command": [sys.executable, "-c",
                                            "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"]})
    assert result["exit_code"] == 3
    assert result["stdout"].strip() == "out"
    assert result["stderr"].strip() == "err"


@pytest.mark.asyncio
async def test_exec_enforces_a_timeout():
    with pytest.raises(ActionError, match="timed out"):
        await execute.run({"command": [sys.executable, "-c", "import time; time.sleep(5)"],
                          "timeout": 0.2})


@pytest.mark.asyncio
async def test_exec_reports_a_missing_binary():
    with pytest.raises(ActionError, match="cannot run"):
        await execute.run({"command": ["this-binary-does-not-exist-9f3c"]})


@pytest.mark.parametrize("params, message", [
    ({"command": []}, "non-empty array"),
    ({"command": "ls"}, "non-empty array"),
    ({"command": ["ls", 5]}, "must be a string"),
    ({"command": ["ls"], "timeout": 0}, "between 0 and"),
    ({"command": ["ls"], "timeout": "soon"}, "must be a number"),
])
@pytest.mark.asyncio
async def test_exec_validates_params(params, message):
    with pytest.raises(ActionError, match=message):
        await execute.run(params)


# --- end to end over the mux --------------------------------------------

async def connect(*, allow_exec=False, file_root=None):
    from rdpflux.config import AgentConfig

    left, right = MemoryTransport.pair()
    client_peer = MuxPeer(left, role="client")
    agent_peer = MuxPeer(right, role="agent")

    async def reject(stream, metadata):
        raise ValueError("client accepts no streams")

    client_peer.set_handlers(on_open=reject)
    files = FileStore(file_root) if file_root else None
    service = ControlService(FakeBackend(), allow_exec=allow_exec, files=files)
    config = AgentConfig(enable_control=True)
    agent = AgentForwarder(agent_peer, config, service)
    await asyncio.gather(agent.start(), client_peer.start())
    await client_peer.wait_ready()
    return ControlClient(client_peer), agent


@pytest.mark.asyncio
async def test_write_and_read_a_file_through_the_tunnel(tmp_path):
    client, agent = await connect(file_root=tmp_path)
    try:
        await client.write_file("data.bin", b"\x00\x01\x02payload", create_parents=False)
        meta, data = await client.read_file("data.bin")
        assert data == b"\x00\x01\x02payload"
        assert meta["size"] == 10
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_file_transfer_is_refused_when_disabled(tmp_path):
    client, agent = await connect()  # no file_root
    try:
        with pytest.raises(ControlError, match="file transfer is disabled"):
            await client.read_file("anything")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_traversal_is_rejected_over_the_tunnel(tmp_path):
    client, agent = await connect(file_root=tmp_path)
    try:
        with pytest.raises(ControlError, match="escapes the file root"):
            await client.read_file("../../etc/passwd")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_exec_is_refused_when_disabled():
    client, agent = await connect()
    try:
        with pytest.raises(ControlError, match="command execution is disabled"):
            await client.exec(["echo", "hi"])
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_exec_runs_over_the_tunnel():
    client, agent = await connect(allow_exec=True)
    try:
        result = await client.exec([sys.executable, "-c", "print('tunnelled')"])
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "tunnelled"
    finally:
        await agent.close()

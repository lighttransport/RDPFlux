from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest


MODULE_PATH = Path(__file__).parents[1] / "examples" / "bash-over-http" / "bash_http_client.py"
SPEC = importlib.util.spec_from_file_location("bash_http_example_client", MODULE_PATH)
assert SPEC and SPEC.loader
bash_http = importlib.util.module_from_spec(SPEC)
sys.modules["bash_http_example_client"] = bash_http
SPEC.loader.exec_module(bash_http)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def response(value) -> FakeResponse:
    return FakeResponse(json.dumps(value).encode("utf-8"))


def test_shell_adapters_send_expected_argv():
    seen = []

    def fake_open(request, timeout):
        seen.append((request, timeout))
        return response({"ok": True, "result": {
            "exit_code": 0, "stdout": "ok", "stderr": "", "truncated": False,
        }})

    with patch.object(bash_http, "urlopen", side_effect=fake_open):
        powershell = bash_http.Client(program="powershell")
        assert powershell.run("Get-Location", cwd="C:/work").stdout == "ok"
        bash = bash_http.Client(program="bash")
        assert bash.run("pwd").ok

    powershell_payload = json.loads(seen[0][0].data.decode("utf-8"))
    bash_payload = json.loads(seen[1][0].data.decode("utf-8"))
    assert powershell_payload["command"] == [
        "powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Location",
    ]
    assert powershell_payload["cwd"] == "C:/work"
    assert bash_payload["command"] == ["bash", "-lc", "pwd"]


def test_direct_argv_preserves_arguments():
    seen = []

    def fake_open(request, timeout):
        seen.append(json.loads(request.data.decode("utf-8")))
        return response({"ok": True, "result": {
            "exit_code": 0, "stdout": "", "stderr": "", "truncated": False,
        }})

    with patch.object(bash_http, "urlopen", side_effect=fake_open):
        bash_http.Client().exec_argv(["git", "commit", "message with spaces"])
    assert seen[0]["command"] == ["git", "commit", "message with spaces"]


def test_file_operations_use_encoded_paths_and_control_envelopes():
    requests = []

    def fake_open(request, timeout):
        requests.append(request)
        if request.method == "GET" and "/v1/file" in request.full_url:
            return FakeResponse(b"hello")
        if request.method == "PUT":
            return response({"ok": True, "result": {"size": len(request.data)}})
        return response({"ok": True, "result": {
            "path": ".", "entries": [], "truncated": False,
        }})

    with patch.object(bash_http, "urlopen", side_effect=fake_open):
        client = bash_http.Client()
        assert client.list_dir("notes/today one") == {"path": ".", "entries": [], "truncated": False}
        assert client.read_file("notes/today one") == b"hello"
        assert client.write_file("empty.bin", b"") == {"size": 0}

    assert "path=notes%2Ftoday%20one" in requests[0].full_url
    assert "path=notes%2Ftoday%20one" in requests[1].full_url
    assert requests[2].data == b""


def test_large_files_are_rejected_before_upload():
    client = bash_http.Client()
    with pytest.raises(ValueError, match="64"):
        client.write_file("large.bin", b"x" * (bash_http.MAX_FILE + 1))


def test_result_and_error_status_are_preserved():
    with patch.object(bash_http, "urlopen", return_value=response({
        "ok": True, "result": {
            "exit_code": 7, "stdout": "out", "stderr": "err", "truncated": False,
        },
    })):
        result = bash_http.Client().run("false")
    assert result.exit_code == 7
    assert not result.ok


def test_authenticated_requests_include_bearer_token_and_timeout():
    seen = []

    def fake_open(request, timeout):
        seen.append((request, timeout))
        return response({"ok": True})

    with patch.object(bash_http, "urlopen", side_effect=fake_open):
        assert bash_http.Client("http://localhost:18080/", "secret", 4.5).health() == {"ok": True}

    request, timeout = seen[0]
    assert request.full_url == "http://localhost:18080/"
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 4.5


def test_http_and_network_errors_are_wrapped_without_losing_context():
    http_error = HTTPError("http://localhost", 401, "Unauthorized", {}, io.BytesIO(b"bad token"))
    with patch.object(bash_http, "urlopen", side_effect=http_error):
        with pytest.raises(bash_http.BashHTTPError, match="HTTP 401: bad token"):
            bash_http.Client().health()

    with patch.object(bash_http, "urlopen", side_effect=URLError("offline")):
        with pytest.raises(bash_http.BashHTTPError, match="cannot reach.*offline"):
            bash_http.Client().health()


@pytest.mark.parametrize("value", [
    {"ok": False, "error": "exec disabled"},
    {"ok": True, "result": []},
    {"ok": True, "result": "bad"},
])
def test_invalid_control_envelopes_raise_clear_errors(value):
    with patch.object(bash_http, "urlopen", return_value=response(value)):
        with pytest.raises(bash_http.BashHTTPError):
            bash_http.Client().list_dir(".")


def test_client_rejects_invalid_urls_timeouts_and_shells():
    with pytest.raises(ValueError, match="url"):
        bash_http.Client("localhost:18080")
    with pytest.raises(ValueError, match="timeout"):
        bash_http.Client(timeout=0)
    with pytest.raises(ValueError, match="unsupported shell"):
        bash_http.Client(program="cmd").run("echo hi")
    with pytest.raises(ValueError, match="must not be empty"):
        bash_http.Client().run("  ")
    with pytest.raises(ValueError, match="non-empty"):
        bash_http.Client().exec_argv(["", "arg"])


def test_result_formatting_reports_output_exit_code_and_truncation(capsys):
    result = bash_http.Result(3, "out", "err", True)
    assert bash_http._print_result(result, False) == 3
    output = capsys.readouterr()
    assert "out" in output.out
    assert "err" in output.err
    assert "truncated" in output.err

    assert bash_http._print_result(result, True) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"exit_code": 3, "stdout": "out", "stderr": "err", "truncated": True}


def test_listing_formats_human_and_json_output(capsys):
    value = {"path": ".", "entries": [
        {"name": "src", "dir": True, "size": None},
        {"name": "a.txt", "dir": False, "size": 12},
    ], "truncated": False}
    assert bash_http._print_listing(value, False) == 0
    assert "d" in capsys.readouterr().out
    assert bash_http._print_listing(value, True) == 0
    assert json.loads(capsys.readouterr().out) == value


def test_download_refuses_existing_file_unless_forced():
    class FakeClient:
        def read_file(self, path):
            assert path == "remote.txt"
            return b"data"

    with patch.object(Path, "exists", return_value=True):
        with pytest.raises(ValueError, match="--force"):
            bash_http._download(FakeClient(), "remote.txt", "local.txt", False)

    with patch.object(Path, "exists", return_value=True), \
         patch.object(Path, "write_bytes") as write_bytes:
        assert bash_http._download(FakeClient(), "remote.txt", "local.txt", True) == 0
        write_bytes.assert_called_once_with(b"data")


def test_interactive_colon_commands_update_cwd_and_run_remote_commands(capsys):
    class FakeClient:
        def health(self):
            return {"ok": True}

        def list_dir(self, path):
            assert path == "."
            return {"entries": [], "truncated": False}

        def read_file(self, path):
            assert path == "notes.txt"
            return b"hello\n"

        def run(self, command, *, cwd=None):
            assert command == "Get-Location"
            assert cwd == "C:/work"
            return bash_http.Result(0, "C:/work\n", "")

    shell = bash_http.Shell(FakeClient())
    with patch("builtins.input", side_effect=[":cwd C:/work", ":health", ":ls", ":cat notes.txt", "Get-Location", ":quit"]):
        assert bash_http._interactive(shell, shell.client, False) == 0
    output = capsys.readouterr().out
    assert "cwd: C:/work" in output
    assert "hello" in output
    assert "C:/work" in output


def test_parser_supports_legacy_command_and_subcommands():
    args = bash_http._parser().parse_args(["--program", "bash", "--command", "pwd"])
    assert args.legacy_command == "pwd"
    args = bash_http._parser().parse_args(["--json", "exec", "--argv", "git", "status"])
    assert args.subcommand == "exec"
    assert args.argv == ["git", "status"]

# Bash over HTTP through RDPFlux

This example is a small command-line client for RDPFlux's desktop-control HTTP
endpoint. It runs commands and performs bounded file operations on the remote
agent through the existing RDP channel.

The agent must be started with command execution enabled:

```powershell
rdpflux-agent --enable-control --enable-exec
```

The client configuration must expose the control API, preferably with a long
random bearer token:

```json
{
  "control": {
    "listen": "127.0.0.1:18080",
    "token": "change-me"
  }
}
```

For file commands, also enable the confined file service on the agent:

```powershell
rdpflux-agent --enable-control --enable-exec --enable-file-transfer --file-root C:\work
```

Global options must appear before the subcommand. The default shell is
PowerShell; use `--program bash` for Git Bash, MSYS2, or WSL.

Run commands:

```powershell
python examples/bash-over-http/bash_http_client.py --token change-me exec --command "Get-Location"
python examples/bash-over-http/bash_http_client.py --token change-me exec --argv git status --short
python examples/bash-over-http/bash_http_client.py --program bash exec --command "pwd"
```

Inspect and transfer files under the configured `file_root`:

```powershell
python examples/bash-over-http/bash_http_client.py --token change-me ls .
python examples/bash-over-http/bash_http_client.py --token change-me cat README.txt
python examples/bash-over-http/bash_http_client.py --token change-me upload .\README.md README.md
python examples/bash-over-http/bash_http_client.py --token change-me download README.md .\README-copy.md
```

Use `download --force` to overwrite an existing local file. Uploads can create
remote parent directories with `--create-parents`.

Start the interactive prompt:

```text
python examples/bash-over-http/bash_http_client.py --token change-me shell
bash-http> pwd
bash-http> :ls .
bash-http> :cwd C:\work\project
bash-http> :upload local.txt remote.txt
bash-http> :quit
```

The `exec` command accepts either shell text or direct argv. Direct argv is
preferred for automation because it avoids shell quoting. The RDPFlux exec API
captures command output rather than streaming a live PTY and limits output to
1 MiB, so this example is intended for short commands and automation. It does
not provide a persistent remote shell session; `--cwd` and `:cwd` simply send a
working directory with each command.

The HTTP listener is loopback-only by default but still requires authentication:
any local process can reach a loopback port, so do not omit the token for a
shared workstation.

# RDPFlux

> Securely forward SSH, HTTPS, SOCKS5, and arbitrary TCP traffic through an
> existing RDP session using RDP virtual channels.

RDPFlux carries SSH, HTTPS, SOCKS5, and other TCP streams inside an already
authenticated RDP connection. It does not open an SSH, HTTPS, controller, or
proxy port on the remote machine's external interfaces. Only the existing RDP
connection needs to cross the firewall.

The implementation is pure Python. On Windows, it uses the system RDP virtual
channel APIs through `ctypes` and `win32more`. It supports:

- Microsoft Remote Desktop Connection (`mstsc.exe`) on Windows 10/11.
- FreeRDP 3.x clients on Linux using FreeRDP's built-in `rdp2tcp` adapter.
- Local fixed TCP forwards, TCP `CONNECT` SOCKS5, and opt-in reverse forwards.
- Multiple simultaneous connections with bounded flow control.

## Project purpose and security-scanner notices

This repository documents and implements an administration utility for systems
you own or are explicitly authorized to manage. It uses documented Windows RDP
virtual-channel APIs and FreeRDP's supported external `rdp2tcp` interface. It
does not exploit RDP, bypass RDP authentication, install persistence, collect
credentials, or open a hidden Internet-facing listener.

Security products may still flag the source or packaged executables because
generic tunneling, SOCKS, reverse-forwarding, PyInstaller, and COM-registration
patterns are also seen in dual-use software. Such a heuristic result can be a
false positive for an authorized deployment, but should not be dismissed
solely on this statement. Review the source, verify the artifact's provenance,
compare its hash with your trusted build, and confirm that its configuration
contains only approved listeners and destinations before creating an allowlist
or exception. Please report reproducible detections with the product name,
signature, file hash, and build method so they can be investigated.

## Security model

RDP provides transport authentication and encryption. The tunnel adds no new
network-facing management service. Client and reverse listeners bind to
loopback unless you explicitly configure another address.

The Windows agent permits only `127.0.0.0/8` and `::1/128` destinations by
default. This is enough to reach an SSH or web service on the RDP host itself.
Access to other addresses must be added with an agent allowlist. Domain names
used through SOCKS are resolved on the agent, and every resulting address is
checked against that allowlist.

Reverse forwarding is disabled on the agent by default. Non-loopback reverse
listeners require a second explicit opt-in.

Each stream has a 256 KiB flow-control window. The mux additionally caps
aggregate buffered data at 32 MiB, and the mstsc callback adapter closes an
overloaded channel instead of growing an unbounded queue. `max_streams` limits
both mux streams and accepted forwarding work; its default is 128.

## Install from source

Python 3.10 or newer is required.

On the Windows mstsc client:

```powershell
python -m pip install -e ".[mstsc]"
```

On the remote Windows machine and on a Linux FreeRDP client, the base package
has no third-party runtime dependencies:

```powershell
python -m pip install -e .
```

Copy `examples/client.json` to the default client location if desired:

- Windows: `%LOCALAPPDATA%\rdpflux\client.json`
- Linux: `$XDG_CONFIG_HOME/rdpflux/client.json`, normally
  `~/.config/rdpflux/client.json`

The agent's optional default configuration is
`%LOCALAPPDATA%\rdpflux\agent.json`.

## mstsc setup

Register the client plugin for the current Windows user, then fully close any
existing `mstsc.exe` processes:

```powershell
python -m rdpflux.client register
```

Connect to the Windows machine normally with mstsc. Inside that RDP desktop,
copy or install this package and run:

```powershell
python -m rdpflux.agent
```

The client plugin is activated by mstsc and loads the default client JSON. With
the example configuration, SSH through it using:

```text
ssh -p 2222 user@127.0.0.1
```

To debug the client plugin in a foreground console, start it before mstsc:

```powershell
python -m rdpflux.client run --transport mstsc --config examples/client.json --verbose
```

Remove its per-user registry entries with:

```powershell
python -m rdpflux.client unregister
```

`--machine` is available for machine-wide registration and requires an
elevated console.

## FreeRDP setup

Install the package or use the standalone Linux client. Put configuration at
the default Linux path, then launch FreeRDP with its external adapter:

```text
xfreerdp /v:windows-host /u:user /rdp2tcp:rdpflux-client
```

Some distributions name the executable `xfreerdp3` or `sdl-freerdp`. Check
that the client's help includes `/rdp2tcp:<executable path[:arg...]>`.
The `rdp2tcp` spelling in that option and its static channel is FreeRDP's
fixed compatibility interface; the RDPFlux package, commands, and files use
the `rdpflux` name.

Inside the remote Windows RDP session, run the same agent command. Its `auto`
transport tries the mstsc dynamic channel and then FreeRDP's `rdp2tcp` static
channel. Use `--transport svc` to select FreeRDP explicitly.

FreeRDP reserves the child process's stdout for tunnel bytes. All program logs
therefore go to stderr.

## Configuration and one-off rules

Client JSON fields are shown in `examples/client.json`. Endpoint strings use
`host:port`; IPv6 addresses use `[address]:port`.

Rules can also be appended on the command line when running in the foreground:

```text
rdpflux-client run --transport mstsc \
  --local 127.0.0.1:2222=127.0.0.1:22 \
  --socks 127.0.0.1:1080
```

### Direct private-network proxy

If this Windows client can reach a Linux machine directly on the same private
network, RDPFlux can also host a small TCP proxy on Windows. This path does not
cross the RDP mux; it uses Python's standard asyncio sockets and is useful when
you want a local Windows port for a Linux service:

```json
{
  "proxy_forwards": [
    {
      "name": "linux-web",
      "listen": "127.0.0.1:8081",
      "target": "192.168.1.20:8080"
    }
  ]
}
```

Or add one in a foreground launch:

```text
rdpflux-client run --transport mstsc --proxy 127.0.0.1:8081=192.168.1.20:8080
```

Connect to `127.0.0.1:8081` on Windows. The Windows host must have a route and
firewall permission to the Linux target. Keep the listener on loopback unless
you intentionally want to expose the Linux service to other hosts. A proxy
rule is independent from `local_forwards`: the latter opens its target through
the RDP agent, while `proxy_forwards` connects directly from Windows.

### Bash-over-HTTP service on the remote host

To expose an HTTP service running on the remote host at port `8000` to another
machine on the private LAN, add a normal `local_forwards` rule to the Windows
client configuration:

```json
{
  "local_forwards": [
    {
      "name": "bash-over-http",
      "listen": "192.168.100.6:18000",
      "target": "127.0.0.1:8000"
    }
  ]
}
```

The resulting path is:

```text
Linux -> Windows 192.168.100.6:18000 -> RDPFlux -> remote 127.0.0.1:8000
```

Configure the Linux bash-over-HTTP client to use
`http://192.168.100.6:18000`. The Windows listener must bind to the LAN
address (or `0.0.0.0`), not only `127.0.0.1`, and Windows Firewall must allow
TCP `18000` from the Linux host on the trusted Private profile. If the service
is only needed on Windows, bind the listener to `127.0.0.1:18000` instead.

This differs from the RDPFlux control example, which uses the client-side
control API on port `18080`, and from `proxy_forwards`, which connects directly
from Windows to a Linux target. `local_forwards` is the correct rule for a
remote service reached through the RDP agent.

### Mutagen file synchronization

RDPFlux can carry Mutagen's SSH transport through the RDP channel. The remote
Windows host must have an SSH server (and Mutagen's normal SSH prerequisites),
and the agent policy must allow that SSH target. Add a dedicated forward to the
client configuration:

```json
{
  "sync_forwards": [
    {
      "name": "mutagen-ssh",
      "listen": "127.0.0.1:2223",
      "target": "127.0.0.1:22"
    }
  ]
}
```

After connecting with RDPFlux, create a Mutagen session from the local machine
using Mutagen's SSH endpoint syntax:

```text
mutagen sync create ./project user@127.0.0.1:2223:C:/work/project
```

Use the path syntax accepted by the SSH server on the RDP host. The equivalent
one-off foreground option is `--sync-ssh 127.0.0.1:2223=127.0.0.1:22`.
`sync_forwards` is an intent-revealing alias for `local_forwards`; it does not
change the tunnel protocol or grant the agent any additional network access.

The optional client `limits` object accepts `max_streams` (`1..4096`),
`connect_timeout` (a positive value up to 300 seconds), and `idle_timeout`
(zero disables it). The same bounds apply to the agent's top-level
`max_streams` and `connect_timeout`. Configuration values are type-checked;
strings such as `"false"` or `"15"` are not treated as booleans or numbers.

SOCKS5 supports unauthenticated TCP `CONNECT` with IPv4, IPv6, and domain
targets. SOCKS `BIND`, UDP, and username/password authentication are not
implemented. Keep the SOCKS listener on loopback unless you intentionally want
other local users to access it.

To reach an internal address from the remote RDP machine, opt it into the
agent policy. For example:

```powershell
rdpflux-agent --allow-target 10.20.0.0/16:22-443
```

To enable a loopback-only reverse rule in client JSON:

```json
{
  "reverse_forwards": [
    {
      "name": "remote-web",
      "listen": "127.0.0.1:8080",
      "target": "127.0.0.1:3000"
    }
  ]
}
```

Start the agent with `--enable-reverse`. A non-loopback remote listener also
requires `--allow-nonloopback-reverse` and should only be used on a trusted
network with appropriate firewall rules.

## Desktop control for LLM/VLM agents

RDPFlux can expose the remote desktop to a vision-language model: screenshots
in, mouse and keyboard out, over the same RDP channel. **No new port is opened on
the remote machine.** The agent captures and injects locally; the client machine
runs a loopback REST API and, optionally, an MCP server that a model drives.

Enable it on the agent (opt-in, like reverse forwarding):

```powershell
python -m rdpflux.agent --enable-control
```

To run shell commands through the HTTP API, also enable execution and see the
[`examples/bash-over-http`](examples/bash-over-http) client example.

The client exposes a loopback REST API and OpenAPI spec. Add a `control` block to
`client.json`:

```json
{
  "control": { "listen": "127.0.0.1:18080", "token": "a-long-random-string" }
}
```

Then, from the client machine:

```text
curl -H "Authorization: Bearer a-long-random-string" \
     -X POST http://127.0.0.1:18080/v1/screenshot \
     -d '{"width":1280,"format":"jpeg"}' -o shot.jpg

curl -H "Authorization: Bearer a-long-random-string" \
     -X POST http://127.0.0.1:18080/v1/action \
     -d '{"action":"left_click","coordinate":[640,360]}'
```

`GET /openapi.json` serves the full schema for OpenAI-style function calling. The
action vocabulary mirrors Anthropic's computer-use tool (`screenshot`,
`left_click`, `type`, `key`, `scroll`, `left_click_drag`, …). Coordinates are in
the delivered screenshot's pixel space; the agent scales them to the native
display, so the model never handles two coordinate systems.

### Persistent shell and system control

Enable the persistent shell with `--enable-exec`. The control mux supports a
long-lived PowerShell session (or Bash/WSL when explicitly selected) with
incremental output, interruption, and cleanup. The REST API exposes session
creation and command execution under `/v1/sessions`.

Typed process, service, task, and diagnostic operations require a separate
agent opt-in:

```powershell
rdpflux-agent --enable-control --enable-system-ops
```

Process termination, service mutations, and scheduled-task execution are more
restrictive and require explicit permissions, for example:

```powershell
rdpflux-agent --enable-system-ops --allow-process-terminate `
  --allow-service Spooler --allow-task MyApprovedTask
```

Text clipboard access is separately gated:

```powershell
rdpflux-agent --enable-control --enable-clipboard
```

The client control block can advertise these optional APIs in OpenAPI:

```json
{
  "control": {
    "listen": "127.0.0.1:18080",
    "token": "a-long-random-string",
    "system_ops": true,
    "clipboard": true
  }
}
```

For general PowerShell automation, prefer PowerShell Remoting over SSH when an
OpenSSH server is installed on the remote Windows host. Reuse a normal RDPFlux
local forward:

```json
{
  "local_forwards": [
    {
      "name": "remote-ssh",
      "listen": "127.0.0.1:2222",
      "target": "127.0.0.1:22"
    }
  ]
}
```

Then connect from the Windows client with either OpenSSH or PowerShell:

```text
ssh -p 2222 user@127.0.0.1
Enter-PSSession -HostName 127.0.0.1 -Port 2222 -UserName user
```

WinRM over HTTPS can be carried similarly by forwarding remote `127.0.0.1:5986`
to a local loopback port. Prefer HTTPS and existing Windows authentication; do
not expose WinRM or SSH listeners to an untrusted network.

For Claude Desktop or Claude Code, run an MCP server that bridges to the REST API:

```text
python -m rdpflux.client mcp --url http://127.0.0.1:18080 --token a-long-random-string
```

Install `rdpflux[control]` on the agent for smaller JPEG frames; without Pillow it
falls back to PNG.

### Control security model

- Screen capture and input are off unless `--enable-control` is set.
- **Shell execution** (`--enable-exec`) and **file transfer** (`--enable-file-transfer`)
  are separate flags, because they turn desktop control into arbitrary remote
  code execution. File transfer is confined to `file_root` (the agent launch
  directory by default), with optional glob allow/deny rules; paths that escape
  the root (including `..` and symlinks) are rejected.
- Typed system operations require `--enable-system-ops`; process termination,
  service control, and scheduled-task execution additionally require their
  explicit allowlists.
- Clipboard access requires `--enable-clipboard` and is limited to text.
- The client REST listener binds loopback and requires a bearer token — any local
  process can otherwise reach a loopback port.
- Windows limits apply: input from a non-elevated agent cannot drive elevated
  windows or the UAC secure desktop, and capture returns black if the RDP session
  is disconnected, so mstsc must stay connected.

### File access policy

File transfer is disabled unless `enable_file_transfer` is set. When enabled,
the file root defaults to the agent's launch directory; set `file_root` to use
another directory such as `D:\\temp`. API paths remain relative to that root.
An optional allowlist uses Python-style glob patterns and explicit permissions;
deny patterns always take precedence:

```json
{
  "enable_file_transfer": true,
  "file_root": "D:\\temp",
  "max_file_upload": 134217728,
  "file_allowlist": [
    {"pattern": "incoming/**", "mode": "read_write"},
    {"pattern": "reports/**", "mode": "read"},
    {"pattern": "outgoing/**", "mode": "write"}
  ],
  "file_denylist": [
    "incoming/private/**",
    "reports/secrets/**"
  ]
}
```

Valid modes are `read`, `write`, and `read_write`. If no allowlist is given,
the whole configured root is allowed for read/write, subject to the denylist.
Keep the root narrow and use an allowlist for production deployments.
`max_file_upload` is in bytes and defaults to 128 MiB. It limits uploads only;
downloads have no file-store size limit, although the control message transport
still has a 128 MiB per-message ceiling.

Multiple roots can be exposed with names. Requests then use the explicit
`name:/relative/path` form, so paths cannot cross from one root into another:

```json
{
  "enable_file_transfer": true,
  "max_file_upload": 134217728,
  "file_roots": [
    {
      "name": "temp",
      "path": "D:\\temp",
      "allowlist": [{"pattern": "tests/**", "mode": "read_write"}]
    },
    {
      "name": "reports",
      "path": "D:\\reports",
      "allowlist": [{"pattern": "**", "mode": "read"}],
      "denylist": ["private/**"]
    }
  ]
}
```

Use `temp:/tests/sample.txt` or `reports:/2026/summary.txt` as the API path.
`file_roots` takes precedence over the legacy singular `file_root` setting.

## Standalone builds

On Windows:

```powershell
.\build.ps1 -Install
```

This creates `dist\rdpflux-client.exe` and `dist\rdpflux-agent.exe`. Register
the packaged client using `rdpflux-client.exe register`.

On Linux:

```sh
./build-linux.sh
```

This creates `dist/rdpflux-client` for FreeRDP.

## License and third-party software

This project is distributed under the [MIT License](LICENSE). Copyright (c)
2026 RDPFlux contributors.

Runtime and packaged-build components:

| Component | License | How it is used |
| --- | --- | --- |
| [Python](https://docs.python.org/3/license.html) | Python Software Foundation License | Runtime; a Python runtime is included in PyInstaller executables. |
| [win32more](https://github.com/ynkdir/py-win32more/blob/main/LICENSE) | MIT; Copyright (c) 2022 Yukihiro Nakadaira | Windows COM and RDP API bindings used by the mstsc client plugin. |
| [PyInstaller](https://pyinstaller.org/en/stable/license.html) | GPL-2.0-or-later with the PyInstaller bootloader exception | Build tool; its bootloader is included in standalone executables. The exception permits distribution under this project's MIT license. |

External and reference projects not copied or bundled into this repository:

| Project | License | Relationship |
| --- | --- | --- |
| [FreeRDP](https://github.com/FreeRDP/FreeRDP/blob/master/LICENSE) | Apache-2.0 | Separately installed RDP client. This tool interoperates with FreeRDP's supported external `rdp2tcp` adapter. |
| [Microsoft RDP DVC plugin samples](https://github.com/microsoft/rdp-dvc-plugin-samples/blob/main/LICENSE) | MIT; Copyright (c) Microsoft Corporation | Reference for the documented Python/`win32more` COM LocalServer activation pattern. |
| [NotMedic/rdp-tunnel](https://github.com/NotMedic/rdp-tunnel) and its [included original rdp2tcp source](https://github.com/NotMedic/rdp-tunnel/blob/master/sources/rdp2tcp/COPYING) | GPL-3.0-or-later for the included rdp2tcp source | Behavioral and architectural reference only. No GPL source, binary, or wire protocol is incorporated into this clean implementation. |

Development-only dependencies are not part of the normal runtime: [pytest](https://github.com/pytest-dev/pytest/blob/main/LICENSE) is MIT,
[pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio/blob/main/LICENSE) is Apache-2.0,
[setuptools](https://github.com/pypa/setuptools/blob/main/LICENSE) is MIT, and
[wheel](https://github.com/pypa/wheel/blob/main/LICENSE.txt) is MIT. Each
third-party project remains governed by its own license. Consult the linked
upstream license texts when redistributing dependencies separately.

## Behavior and troubleshooting

- Run the agent inside the interactive RDP session, not at the physical console
  and not as a Windows service.
- The mstsc plugin reloads client configuration for each newly opened RDP
  channel. Existing listeners keep their current configuration until the
  channel reconnects. The agent loads its configuration once at startup.
- Existing TCP streams close when RDP disconnects. The agent retries channel
  attachment; newly accepted connections work after RDP reconnects.
- A fatal mux or callback error closes the current virtual channel and reconnects
  on a fresh channel; protocol state is never restarted inside a damaged DVC.
- If the mstsc channel does not open, confirm the plugin is registered under
  the same Windows user and restart all mstsc processes.
- If an agent connection is denied, add only the required CIDR and port range
  to `allow_targets` or `--allow-target`.
- No SSH server is bundled. Install/configure one on the remote Windows host,
  keep its external firewall port closed, and forward to its loopback port.

## Development

```powershell
python -m pip install -e ".[test]"
python -m pytest -q
```

The tests exercise incremental framing, policy validation, multiplexing,
backpressure with a megabyte-scale stream, request timeouts, bounded callback
buffering, and SOCKS5 over an in-memory RDP transport. Interactive capture and
input tests are opt-in so headless Windows CI remains reliable:

```powershell
$env:RDPFLUX_RUN_DESKTOP_TESTS = "1"
python -m pytest -q tests/test_control_windows.py
```

Before release, exercise both mstsc and FreeRDP with parallel checksum-verified
transfers, TCP half-close, more than 60 seconds idle, and repeated RDP
disconnect/reconnect cycles.

The design was informed by the original
[`NotMedic/rdp-tunnel`](https://github.com/NotMedic/rdp-tunnel), current
FreeRDP's external `rdp2tcp` adapter, and Microsoft's Python DVC sample. This
repository is a clean Python 3 implementation with its own protocol.

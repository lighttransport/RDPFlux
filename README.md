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

For Claude Desktop or Claude Code, run an MCP server that bridges to the REST API:

```text
python -m rdpflux.client mcp --url http://127.0.0.1:18080 --token a-long-random-string
```

Install `rdpflux[control]` on the agent for smaller JPEG frames; without Pillow it
falls back to PNG.

### Control security model

- Screen capture and input are off unless `--enable-control` is set.
- **Shell execution** (`--enable-exec`) and **file transfer** (`--enable-file-transfer
  --file-root DIR`) are separate flags, because they turn desktop control into
  arbitrary remote code execution. File transfer is confined to `--file-root`;
  paths that escape it (including `..` and symlinks) are rejected.
- The client REST listener binds loopback and requires a bearer token — any local
  process can otherwise reach a loopback port.
- Windows limits apply: input from a non-elevated agent cannot drive elevated
  windows or the UAC secure desktop, and capture returns black if the RDP session
  is disconnected, so mstsc must stay connected.

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

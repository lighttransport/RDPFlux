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
- Configuration is loaded when the client/plugin starts. Restart mstsc to pick
  up changes to an automatically activated plugin.
- Existing TCP streams close when RDP disconnects. The agent retries channel
  attachment; newly accepted connections work after RDP reconnects.
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
backpressure with a megabyte-scale stream, and SOCKS5 over an in-memory RDP
transport.

The design was informed by the original
[`NotMedic/rdp-tunnel`](https://github.com/NotMedic/rdp-tunnel), current
FreeRDP's external `rdp2tcp` adapter, and Microsoft's Python DVC sample. This
repository is a clean Python 3 implementation with its own protocol.

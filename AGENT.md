# Coding-agent guide

RDPFlux is a Python 3.10+ project that multiplexes TCP streams and desktop
control traffic over an already-authenticated RDP session. Preserve that
security boundary: this is an administration tool for authorized systems.

## Scope and safety

- Inspect `git status` before editing. Preserve existing user changes; do not
  reset, clean, delete, or overwrite unrelated files.
- Keep control, SOCKS, proxy, and reverse-forward listeners on loopback unless
  a trusted LAN binding is explicitly required.
- Keep command execution, desktop input, system operations, clipboard access,
  and file transfer independently gated by configuration flags.
- File-transfer changes must preserve root confinement, allowlist/denylist
  checks, read/write modes, traversal protection, and upload-size limits.
- Never put real tokens, credentials, private hostnames, usernames, workstation
  paths, customer/project names, or copied private repository content in
  source, tests, examples, documentation, commit messages, or logs. Use
  placeholders such as `C:\\work`, `user@host`, and `change-me`.
- Do not resolve an unrelated merge or rebase. If one is in progress, inspect
  and report its state rather than choosing a side without explicit direction.

## Repository map

- `rdpflux/`: package code for configuration, transports, multiplexing,
  forwarding, agent control, and the HTTP control API.
- `tests/`: unit and integration tests; platform-specific tests skip when
  their platform or optional dependency is unavailable.
- `examples/`: sample JSON configurations and the bash-over-HTTP client.
- `README.md`: user-facing setup, security model, configuration, and examples.
- `.githooks/`: local pre-push secret audit hook.

## Normal workflow

1. Read the relevant implementation and tests before changing behavior.
2. Make the smallest focused patch with `apply_patch`.
3. Add tests for parsing, authorization boundaries, error handling, and
   platform-independent behavior. Never test against real user data.
4. Run focused tests, then the full suite:

   ```text
   python -m pytest -q tests/test_config_policy.py tests/test_control_ops.py
   python -m pytest -q
   ```

5. Review the diff and status, then run the secret audit before pushing. Do
   not commit generated screenshots, temporary test trees, tokens, or private
   paths.

Useful checks:

```text
git diff --check
git diff --stat
git status --short
```

The Windows mstsc tests require Windows and `win32more`; optional control image
support requires Pillow. Do not weaken tests because an optional dependency is
absent.

This repository uses a shared local `pre-push` audit hook to prevent pushing
secrets.

## Security pre-push hook

Enable the hook once per clone:

```bash
git config core.hooksPath .githooks
```

The hook executes:

- `gitleaks detect --source . --no-git --redact --no-banner`
- `${HOME}/local/bin/trufflehog filesystem --fail --no-update .`

CI also runs the same audit via `.github/workflows/secret-scan.yml`.
The CI workflow currently pins:

- `gitleaks` at `v8.16.0`
- `trufflehog` at `v3.95.6`

Environment overrides:

- `GITLEAKS_BIN`: path or binary name for `gitleaks` (default: `gitleaks`)
- `TRUFFLEHOG_BIN`: path to `trufflehog` (default:
  `${HOME}/local/bin/trufflehog`)
- `SKIP_SECRET_AUDIT=1`: temporarily skip the hook during a push

## Project notes

- Python 3.10+ is required.
- See `README.md` for installation and run instructions.

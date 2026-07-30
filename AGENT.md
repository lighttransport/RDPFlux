# AGENT

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

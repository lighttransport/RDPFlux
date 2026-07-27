#!/usr/bin/env sh
set -eu
python3 -m pip install -e '.[build]'
python3 -m PyInstaller --noconfirm --onefile --name rdpflux-client \
  --workpath build/client --distpath dist --specpath build/spec scripts/client_entry.py
echo 'Built dist/rdpflux-client'

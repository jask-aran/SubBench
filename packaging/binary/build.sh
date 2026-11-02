#!/usr/bin/env sh
# Build the single-file `subbench` executable for this machine's OS and architecture.
#
#   sh packaging/binary/build.sh        -> dist/bin/subbench-<os>-<arch>
#
# Needs Python 3.11+ and network for the first `pip install`. The result runs with no
# Python, Node or anything else installed.
set -eu
cd "$(dirname "$0")/../.."
# A private environment, so the build neither needs nor touches the system Python's packages.
python3 -m venv build/venv
build/venv/bin/python -m pip install --quiet . "pyinstaller>=6.10,<7"
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)
case "$arch" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; esac
name="subbench-$os-$arch"
build/venv/bin/python -m PyInstaller --onefile --clean --noconfirm --name "$name" \
  --distpath dist/bin --workpath build/pyinstaller --specpath build/pyinstaller \
  --collect-data subbench --hidden-import plotext \
  packaging/binary/entry.py
"dist/bin/$name" version
( cd dist/bin && shasum -a 256 "$name" > "$name.sha256" 2>/dev/null || sha256sum "$name" > "$name.sha256" )
echo "built dist/bin/$name"

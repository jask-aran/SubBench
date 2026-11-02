#!/usr/bin/env sh
# Install the single-file SubBench collector, then start it in the background.
#
#   curl -fsSL https://github.com/jask-aran/SubBench/releases/latest/download/install.sh | sh
#
# It downloads one file for this OS and architecture, checks its SHA-256 against the
# release's SHA256SUMS, puts it in ~/.local/bin, and runs `subbench service install`.
# Set SUBBENCH_NO_SERVICE=1 to skip the last step. Nothing else is installed or changed.
set -eu
repo="jask-aran/SubBench"
version="${SUBBENCH_VERSION:-latest}"
bin_dir="${SUBBENCH_BIN_DIR:-$HOME/.local/bin}"
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)
case "$arch" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac
case "$os" in linux|darwin) ;; *) echo "unsupported OS: $os" >&2; exit 1 ;; esac
name="subbench-$os-$arch"
if [ "$version" = latest ]; then base="https://github.com/$repo/releases/latest/download"; else base="https://github.com/$repo/releases/download/$version"; fi

base="${SUBBENCH_DOWNLOAD_BASE:-$base}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Downloading $name"
curl -fsSL -o "$tmp/$name" "$base/$name"
curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS"
expected=$(grep " $name\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)
if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$tmp/$name" | cut -d' ' -f1); else actual=$(shasum -a 256 "$tmp/$name" | cut -d' ' -f1); fi
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then echo "checksum mismatch for $name; not installing" >&2; exit 1; fi

mkdir -p "$bin_dir"
install -m 0755 "$tmp/$name" "$bin_dir/subbench"
echo "Installed $bin_dir/subbench ($("$bin_dir/subbench" version))"
case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "Add $bin_dir to your PATH to run \`subbench\` directly." ;; esac
if [ "${SUBBENCH_NO_SERVICE:-}" != 1 ]; then "$bin_dir/subbench" service install; fi
echo "\`subbench privacy\` shows everything it reads, keeps and sends, and every request it has made."

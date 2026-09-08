#!/usr/bin/env bash
# Fetch the sv2v binary into docker/vendor/ for the worker image build.
#
# sv2v is vendored rather than downloaded inside the Dockerfile because docker
# build on the WSL2 host cannot complete a TLS transfer to github (curl exits
# 28, SSL connection timeout), while the identical fetch succeeds from the host
# shell and from running containers. Fetching here keeps the image build
# hermetic and offline.
#
# Usage (from the repo root, on the Linux build host):
#   ./docker/fetch-sv2v.sh
#   docker build -f docker/CocotbVerilatorDockerfile -t spec2inc-cocotb-verilator:latest .

set -euo pipefail

SV2V_VERSION="${SV2V_VERSION:-v0.0.13}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$here/vendor"
mkdir -p "$dest"

if [ -x "$dest/sv2v" ]; then
    echo "sv2v already vendored at $dest/sv2v"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

url="https://github.com/zachjs/sv2v/releases/download/${SV2V_VERSION}/sv2v-Linux.zip"
echo "fetching ${url}"
curl -fsSL -o "$tmp/sv2v.zip" "$url"
# python rather than unzip: unzip is not installed on the WSL2 build host, and
# python is (miniconda). One less thing to require.
python3 -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "$tmp/sv2v.zip" "$tmp/x"
install -m 0755 "$(find "$tmp/x" -type f -name sv2v | head -1)" "$dest/sv2v"

if "$dest/sv2v" --version >/dev/null 2>&1; then
    echo "vendored $("$dest/sv2v" --version) at $dest/sv2v"
else
    # Expected when run from macOS: the binary targets the Linux image.
    echo "vendored $dest/sv2v (Linux binary, not executable on this host)"
fi

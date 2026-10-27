#!/bin/bash
# Isolated deployment Python; leave Ubuntu's /usr/bin/python3 unchanged.
# Source/checksum: https://www.python.org/downloads/release/python-31318/
set -euo pipefail
version=3.13.18
checksum=abb0102e6d4ae027509bcb82327d9dd85515b44a3ba8ad538cfc5e9847029f68
prefix=${1:-/usr/lib/genestack-python}
destination=${2-/out}
jobs=${3:-2}
build_work=$(mktemp -d)
trap 'rm -rf "$build_work"' EXIT
curl --proto '=https' --tlsv1.2 -fsSL \
  "https://www.python.org/ftp/python/$version/Python-$version.tar.xz" \
  -o "$build_work/source.tar.xz"
printf '%s  %s\n' "$checksum" "$build_work/source.tar.xz" | sha256sum -c -
tar --no-same-owner -xJf "$build_work/source.tar.xz" -C "$build_work"
cd "$build_work/Python-$version"
./configure --prefix="$prefix" --with-ensurepip=install --disable-test-modules
make -j"$jobs"
make install DESTDIR="$destination"
"$destination$prefix/bin/python3" -c 'import ssl, sqlite3, bz2, lzma, ctypes, readline, venv, ensurepip; import sys; assert sys.version_info[:3] == (3, 13, 18)'

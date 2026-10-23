#!/bin/sh
# Builds an offline install kit on a machine with internet access, for a server without it.
#
# Usage: sh scripts/make-wheelhouse.sh [--python-version 3.10] [--arch x86_64|aarch64] [--out dist]
#
# The kit is dist/ballast-<version>-offline-py<version>-<arch>.tar.gz. It holds the Ballast source and a
# wheelhouse/ directory with prebuilt wheels for the target Python and architecture (glibc 2.17 or later).
# On the server:
#   tar -xzf ballast-1.0.0-offline-py310-x86_64.tar.gz
#   sh ballast-1.0.0-offline/scripts/install.sh --wheelhouse ballast-1.0.0-offline/wheelhouse --prefix ~/ballast
set -eu
src=$(cd "$(dirname "$0")/.." && pwd -P)
pyver=3.10
arch=$(uname -m)
out="$src/dist"
python=${PYTHON:-}
while [ $# -gt 0 ]; do
  case $1 in
    --python-version) pyver=$2; shift 2 ;;
    --arch) arch=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "make-wheelhouse.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
case $arch in
  x86_64|amd64) arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  *) echo "make-wheelhouse.sh: unsupported architecture $arch" >&2; exit 2 ;;
esac
if [ -z "$python" ]; then
  python=python3
  command -v "python$pyver" >/dev/null 2>&1 && python="python$pyver"
fi
host=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
extra=""
if [ "$host" != "$pyver" ]; then
  echo "note: $python is Python $host, not $pyver. pip decides version-specific dependencies from the Python running it, so"
  echo "      the ones aiohttp and pydantic need on older Pythons are added explicitly. Set PYTHON=python$pyver to avoid this."
  extra="async-timeout>=4.0,<6.0 typing-extensions>=4.6 exceptiongroup>=1.0"
fi
version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$src/ballast/product.py")
tag=$(echo "$pyver" | tr -d .)
name="ballast-$version-offline"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kit="$work/$name"
mkdir -p "$kit/wheelhouse"

platforms=""
for p in manylinux2014 manylinux_2_17 manylinux_2_28 manylinux_2_34; do
  platforms="$platforms --platform ${p}_$arch"
done
# shellcheck disable=SC2086
"$python" -m pip download --disable-pip-version-check -q --dest "$kit/wheelhouse" --only-binary=:all: \
  --implementation cp --python-version "$pyver" $platforms \
  "aiohttp>=3.9" "pydantic>=2.5" "pymongo>=4.13" "pyyaml>=6.0" "faker>=20" $extra

(cd "$src" && git ls-files -z 2>/dev/null || find . -type f ! -path './.git/*' -print0) | (cd "$src" && xargs -0 tar -cf - --exclude='docs/design' --exclude='docs/evidence' --exclude='dist' --exclude='runtime') | tar -xf - -C "$kit"
mkdir -p "$out"
archive="$out/$name-py$tag-$arch.tar.gz"
tar -czf "$archive" -C "$work" "$name"
echo "$archive ($(ls "$kit/wheelhouse" | wc -l | tr -d ' ') wheels)"

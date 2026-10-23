#!/bin/sh
# Installs a private Python runtime into runtime/python for a git checkout.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd -P)
. "$root/scripts/python-runtime.env"

os=$(uname -s)
arch=$(uname -m)
case $os in
  Linux) os_key=linux; triple_os=unknown-linux-gnu ;;
  Darwin) os_key=darwin; triple_os=apple-darwin ;;
  *) echo "setup-runtime: unsupported operating system $os" >&2; exit 1 ;;
esac
case $arch in
  x86_64|amd64) arch_key=x86_64 ;;
  aarch64|arm64) arch_key=aarch64 ;;
  *) echo "setup-runtime: unsupported CPU $arch" >&2; exit 1 ;;
esac
eval "expected=\$PY_SHA256_${os_key}_${arch_key}"
name="cpython-${PY_VERSION}+${PY_RELEASE}-${arch_key}-${triple_os}-install_only.tar.gz"
url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${name}"
cache="$root/.cache"
mkdir -p "$cache"
if [ ! -f "$cache/$name" ]; then
  echo "downloading $name"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$cache/$name.part" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$cache/$name.part" "$url"
  else
    echo "setup-runtime: need curl or wget" >&2; exit 1
  fi
  mv "$cache/$name.part" "$cache/$name"
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$cache/$name" | cut -d' ' -f1)
else
  actual=$(shasum -a 256 "$cache/$name" | cut -d' ' -f1)
fi
if [ "$actual" != "$expected" ]; then
  echo "setup-runtime: checksum mismatch for $name" >&2
  rm -f "$cache/$name"
  exit 1
fi
rm -rf "$root/runtime/python.new"
mkdir -p "$root/runtime/python.new"
tar -xzf "$cache/$name" -C "$root/runtime/python.new" --strip-components=1
PIP_ROOT_USER_ACTION=ignore "$root/runtime/python.new/bin/python3" -m pip install -q --disable-pip-version-check --no-warn-script-location -r "$root/requirements/runtime.lock"
site=$("$root/runtime/python.new/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$root" > "$site/ballast-source.pth"
rm -rf "$root/runtime/python"
mv "$root/runtime/python.new" "$root/runtime/python"
echo "runtime ready: $("$root/runtime/python/bin/python3" --version) at runtime/python"

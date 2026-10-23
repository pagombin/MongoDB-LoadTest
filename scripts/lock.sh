#!/bin/sh
set -eu
root=$(cd "$(dirname "$0")/.." && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$root/runtime/python/bin/python3" -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install -q --upgrade pip
"$tmp/venv/bin/pip" install -q aiohttp faker pydantic pymongo pyyaml dash plotly reportlab typer waitress
{
  echo "# Exact runtime dependencies shipped in release bundles and installed by scripts/setup-runtime.sh."
  echo "# Regenerate with: scripts/lock.sh"
  "$tmp/venv/bin/pip" freeze | grep -v '^pip='
} > "$root/requirements/runtime.lock"
echo "wrote requirements/runtime.lock"

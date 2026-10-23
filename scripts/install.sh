#!/bin/sh
# Installs Ballast with the host's Python and pip, as a normal user, into one directory.
#
# Usage: sh scripts/install.sh [--prefix DIR] [--python PYTHON] [--wheelhouse DIR] [--minimal] [--no-run-doctor]
#
# It tries, in order: a virtual environment in DIR/runtime/venv, then pip --target DIR/runtime/site
# (for hosts where python3-venv or ensurepip is missing). Required packages are aiohttp, pydantic and
# pymongo. Ballast itself is pure Python and is copied, not built, so no setuptools or compiler is needed. If a compiled package has no wheel for this host and cannot be built, the aiohttp family is
# retried in pure-Python mode. Optional packages (PyYAML, Faker) are installed one at a time and a failure
# only disables the feature that needs them. --wheelhouse installs from a directory of wheels with no
# internet access (see scripts/make-wheelhouse.sh).
set -u
src=$(cd "$(dirname "$0")/.." && pwd -P)
prefix="$HOME/ballast"
python=""
wheelhouse=""
minimal=0
doctor=1
while [ $# -gt 0 ]; do
  case $1 in
    --prefix) prefix=$2; shift 2 ;;
    --python) python=$2; shift 2 ;;
    --wheelhouse) wheelhouse=$(cd "$2" && pwd -P); shift 2 ;;
    --minimal) minimal=1; shift ;;
    --no-run-doctor) doctor=0; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

version_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null
}

if [ -n "$python" ]; then
  version_ok "$python" || fail "$python is not Python 3.10 or later"
else
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && version_ok "$candidate"; then
      python=$(command -v "$candidate")
      break
    fi
  done
  [ -n "$python" ] || fail "no Python 3.10 or later found. Install one, pass --python, or use a release bundle (it brings its own Python)."
fi
say "Python: $python ($("$python" -c 'import platform; print(platform.python_version())'))"

for module in sqlite3 ssl; do
  "$python" -c "import $module" 2>/dev/null || fail "this Python has no $module module. Use another Python or a release bundle."
done

mkdir -p "$prefix" || fail "cannot create $prefix"
prefix=$(cd "$prefix" && pwd -P)
runtime="$prefix/runtime"
mkdir -p "$runtime"

mode=""
pip=""
if "$python" -m venv "$runtime/venv" >/dev/null 2>&1 && "$runtime/venv/bin/python" -m pip --version >/dev/null 2>&1; then
  mode=venv
  py="$runtime/venv/bin/python"
  pip="$py -m pip"
  say "Environment: virtual environment in $runtime/venv"
else
  rm -rf "$runtime/venv"
  if "$python" -m pip --version >/dev/null 2>&1; then
    mode=target
    py="$python"
    mkdir -p "$runtime/site"
    pip="$python -m pip"
    say "Environment: no working venv here, installing into $runtime/site with pip --target"
  else
    fail "neither venv nor pip works for $python. Try '$python -m ensurepip --user', or use a release bundle."
  fi
fi

pip_install() {
  if [ "$mode" = target ]; then
    set -- --target "$runtime/site" --upgrade "$@"
  fi
  if [ -n "$wheelhouse" ]; then
    set -- --no-index --find-links "$wheelhouse" "$@"
  fi
  $pip install --disable-pip-version-check --no-warn-script-location -q "$@"
}

required="aiohttp>=3.9 pydantic>=2.5 pymongo>=4.13"
say "Installing the required packages (aiohttp, pydantic, pymongo)"
if ! pip_install $required; then
  say "That failed. Retrying with the aiohttp family in pure-Python mode (no compiler needed)."
  AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
  export AIOHTTP_NO_EXTENSIONS MULTIDICT_NO_EXTENSIONS YARL_NO_EXTENSIONS FROZENLIST_NO_EXTENSIONS PROPCACHE_NO_EXTENSIONS
  pip_install $required || fail "the required packages could not be installed. Run '$pip install $required' to see why, or use a release bundle."
fi

say "Copying Ballast into $runtime/site"
mkdir -p "$runtime/site"
for package in ballast mongo_load_tester; do
  rm -rf "$runtime/site/$package"
  (cd "$src" && find "$package" -name __pycache__ -prune -o -type f -print) | while IFS= read -r file; do
    mkdir -p "$runtime/site/$(dirname "$file")"
    cp "$src/$file" "$runtime/site/$file"
  done
done
missing=""
if [ "$minimal" -eq 0 ]; then
  for optional in pyyaml faker; do
    if pip_install "$optional" >/dev/null 2>&1; then
      say "  optional $optional: installed"
    else
      say "  optional $optional: not installed (Ballast works without it; 'ballast doctor' says what it adds)"
      missing="$missing $optional"
    fi
  done
fi

for dir in bin config data logs run certs; do
  mkdir -p "$prefix/$dir"
done
chmod 700 "$prefix/data" "$prefix/certs" "$prefix/run"
if [ "$prefix" != "$src" ]; then
  for f in bin/ballast bin/ballastctl bin/ballast-launch.py; do
    cp "$src/$f" "$prefix/$f"
  done
  cp "$src/scripts/verify-host.sh" "$prefix/bin/verify-host.sh"
  cp "$src/config/ballast.yaml.example" "$prefix/config/"
  [ -f "$src/LICENSE" ] && cp "$src/LICENSE" "$prefix/"
fi
chmod 755 "$prefix/bin/ballast" "$prefix/bin/ballastctl"
[ -f "$prefix/bin/verify-host.sh" ] && chmod 755 "$prefix/bin/verify-host.sh"

{
  echo "# Written by scripts/install.sh. bin/ballast reads this when there is no bundled runtime."
  echo "BALLAST_PYTHON='$py'"
  echo "BALLAST_SITE='$runtime/site'"
} > "$runtime/python.env"

say ""
say "Installed in $prefix"
if [ "$doctor" -eq 1 ]; then
  "$prefix/bin/ballast" doctor --quick || true
fi
say ""
say "Start it with:  $prefix/bin/ballastctl start"
[ -n "$missing" ] && say "Optional packages not installed:$missing"
exit 0

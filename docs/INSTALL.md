# Installing Ballast

Every method below runs as a normal user. None needs root, `sudo`, or system packages, and the installed app needs no internet access except to your MongoDB cluster.

Supported hosts: Linux on x86-64 or ARM64 with glibc 2.17 or later (any current distribution), and macOS 11 or later on Apple Silicon or Intel for local use.

There are three ways in. Pick the first one your server allows:

| Your server | Use |
|---|---|
| Can receive a file you copy to it | **Release bundle** below. Brings its own Python; nothing is installed on the host. |
| Has Python 3.10 or later and `pip` reaches PyPI | **With the host's Python and pip** below. Works without `python3-venv`. |
| Has Python 3.10 or later but no internet | **Offline kit** below. Built on another machine, installed with no network. |

Whichever you use, `bin/ballast doctor` checks the host afterwards and prints the exact command for anything missing.

## From a release bundle

1. Download the archive for your platform and the `SHA256SUMS` file, and check the archive on the machine you downloaded it to:

   ```
   sha256sum -c SHA256SUMS --ignore-missing
   ```

   On macOS use `shasum -a 256 -c SHA256SUMS --ignore-missing`.

2. Copy it to the server and extract it in your home directory:

   ```
   scp ballast-1.0.0-linux-amd64.tar.gz you@server:
   ssh you@server
   tar -xzf ballast-1.0.0-linux-amd64.tar.gz
   cd ballast-1.0.0
   ```

3. Optionally create a configuration file (see `docs/CONFIGURATION.md`):

   ```
   cp config/ballast.yaml.example config/ballast.yaml
   ```

4. Start it and check it:

   ```
   bin/ballastctl start
   bin/ballastctl status
   ```

5. Optionally run the host check, which starts, probes, crash-tests and stops Ballast and prints PASS or FAIL for each step:

   ```
   bin/verify-host.sh
   bin/verify-host.sh --uri 'mongodb://user:password@host:27017/?replicaSet=rs0'
   ```

   With `--uri` it also runs a 20-second workload against that cluster in database `ballast_verify`.

The archive unpacks to:

```
ballast-1.0.0/
  bin/ballast
  bin/ballastctl
  bin/verify-host.sh
  config/ballast.yaml.example
  certs/
  data/
  logs/
  run/
  docs/
  runtime/python/
  THIRD_PARTY_NOTICES
  VERSION
  LICENSE
  sbom.cdx.json
```

`runtime/python` is a private Python 3.12 with every dependency already installed. Ballast does not use any Python on the host.

## With the host's Python and pip

Ballast needs only three packages: `aiohttp`, `pydantic` and `pymongo`. Everything else is optional:

| Package | Adds | Without it |
|---|---|---|
| `pyyaml` | Every YAML feature in the config file | A built-in reader handles plain mappings, lists and scalars, which is all the shipped example uses |
| `faker` | The `schema` document shape | Only the `simple` document shape |
| `dnspython` | `mongodb+srv://` connection strings | List the hosts in a `mongodb://` string (it comes with `pymongo` in normal installs) |
| `zstandard`, `python-snappy` | zstd and snappy wire compression | zlib compression |

Neither `cryptography` nor `openssl` is needed: the self-signed certificate is generated in pure Python.

From a checkout or an unpacked source archive:

```
sh scripts/install.sh --prefix ~/ballast
~/ballast/bin/ballastctl start
```

The script:

1. Finds Python 3.10 or later (`--python /path/to/python3` to choose one) and checks it has `sqlite3` and `ssl`.
2. Makes a virtual environment in `~/ballast/runtime/venv`. If `venv` or `ensurepip` is missing (common on Debian and Ubuntu without `python3-venv`), it installs with `pip --target ~/ballast/runtime/site` instead.
3. Installs the three required packages. If one has no wheel for this host and there is no compiler, it retries the aiohttp family in pure-Python mode.
4. Installs each optional package on its own. A failure only disables what that package adds.
5. Copies Ballast itself in. It is pure Python, so nothing is built and setuptools is not needed.
6. Runs `bin/ballast doctor`.

`--minimal` skips the optional packages. Everything lives under the prefix; remove the directory to uninstall.

On a recent pip (23 or later) `pip install .` and `pip install ".[all]"` from a checkout also work. The installer does not build a package on purpose: Ubuntu 22.04's pip 22 with `--target` builds it with the system setuptools 59, which cannot read `pyproject.toml` and produces an empty package named `UNKNOWN`.

## Offline kit

On a machine with internet access and the same architecture family, build a kit for the server's Python version:

```
sh scripts/make-wheelhouse.sh --python-version 3.10 --arch x86_64
```

It writes `dist/ballast-1.0.0-offline-py310-x86_64.tar.gz` with the source and prebuilt wheels. Use a Python of the target version to build it if you can (`PYTHON=python3.10`); otherwise the script adds the version-specific dependencies itself. Copy the kit to the server and run:

```
tar -xzf ballast-1.0.0-offline-py310-x86_64.tar.gz
sh ballast-1.0.0-offline/scripts/install.sh --wheelhouse ballast-1.0.0-offline/wheelhouse --prefix ~/ballast
```

`docs/evidence/install-offline-kit.log` shows this on Ubuntu 22.04 with no network, and `docs/evidence/install-ubuntu22-no-venv.log` shows the online route on the same system without `python3-venv`.

## From git

You need `git`, `make`, `tar`, and `curl` or `wget`. `make build` downloads a private Python runtime (checked against a pinned SHA-256) into `runtime/` and installs the pinned dependencies there.

```
git clone https://github.com/pagombin/MongoDB-LoadTest.git ballast
cd ballast
make build
bin/ballastctl start
```

To update:

```
bin/ballastctl stop
git pull
make build
bin/ballastctl start
```

`make build` is only needed again when `requirements/runtime.lock` or `scripts/python-runtime.env` changed, but running it is always safe. The runtime points at the checkout through a `.pth` file, so code changes from `git pull` take effect at the next start.

## Upgrading a bundle

Extract the new bundle next to the old one, copy your configuration and data across, and start the new one:

```
tar -xzf ballast-1.1.0-linux-amd64.tar.gz
ballast-1.0.0/bin/ballastctl stop
cp -p ballast-1.0.0/config/ballast.yaml ballast-1.1.0/config/
cp -pR ballast-1.0.0/data/. ballast-1.1.0/data/
cp -pR ballast-1.0.0/certs/. ballast-1.1.0/certs/
ballast-1.1.0/bin/ballastctl start
```

Copy any token or password files your configuration names, and keep their mode at 0600 (`cp -p` does). If autostart was enabled for the old directory, run `ballast-1.0.0/bin/ballastctl autostart disable` and `ballast-1.1.0/bin/ballastctl autostart enable`.

On first start the new version upgrades the data store if its schema changed. Before it changes anything it copies the database to `data/backups/ballast-pre-migration-v<N>-<time>.db`. To go back, stop the new version and start the old directory, which still has its own untouched `data/`.

## Uninstalling

```
bin/ballastctl autostart disable
bin/ballastctl stop
cd ..
rm -rf ballast-1.0.0
```

To remove benchmark data from the cluster first, use **Targets > Clean up data**, which drops only the collections Ballast created.

## Running as a different port or address

Edit `config/ballast.yaml` or pass a flag:

```
bin/ballastctl start --listen 127.0.0.1:9443
```

Flags given to `start` apply to that start only; put permanent settings in the file.

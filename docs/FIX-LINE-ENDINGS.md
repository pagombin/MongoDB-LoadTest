# Fixing `make build` on a copy with Windows line endings

This applies to a copy of Ballast taken from `main` (up to and including the merge of pull request 9) whose files ended up with Windows line endings (CRLF). That happens when the repository is cloned on Windows and copied to the server, unpacked from a ZIP made on Windows, or cloned with `git config core.autocrlf true`.

Copies that include the `.gitattributes` file added after that merge get Unix line endings on checkout, so they need none of this.

## Symptoms

Either of these from `make build`:

```
make: scripts/setup-runtime.sh: No such file or directory
make: *** [Makefile:7: build] Error 127
```

The script exists, but its first line is `#!/bin/sh` followed by a hidden carriage return, so Linux looks for an interpreter named `/bin/sh\r`.

```
curl: (3) URL using bad/illegal format or missing URL
make: *** [Makefile:7: build] Error 3
```

This one means only some files were converted. Converting just `scripts/*.sh` is not enough: `scripts/setup-runtime.sh` reads the Python version and release from `scripts/python-runtime.env`. If that file still has carriage returns, they end up inside the download URL. Newer curl versions report the same error as `URL rejected: Malformed input to a URL function`.

To confirm, look at the end of the first line of a file. `\r \n` means Windows line endings; `\n` alone means Unix:

```
head -1 scripts/python-runtime.env | od -c | head -2
```

## Fix

Run these from the Ballast directory, for example `/apps/mongod/MongoDB-LoadTest`. They work in ksh, bash and sh, and need no root access.

```
cd /apps/mongod/MongoDB-LoadTest

# 1. List the text files that still have carriage returns. Binary files such as images are skipped.
grep -rlI "$(printf '\r')" --exclude-dir=.git .

# 2. Convert all of them to Unix line endings.
grep -rlIZ "$(printf '\r')" --exclude-dir=.git . | xargs -0 -r sed -i 's/\r$//'

# 3. Check: this must print 0.
grep -rlI "$(printf '\r')" --exclude-dir=.git . | wc -l

# 4. Restore the executable bits, in case the copy lost them.
chmod +x bin/ballast bin/ballastctl scripts/*.sh

# 5. Remove anything the failed attempts left behind.
rm -rf .cache runtime/python.new

# 6. Build again.
make build
```

`make build` ends with:

```
runtime ready: Python 3.12.12 at runtime/python
```

Then check the host and start Ballast:

```
bin/ballast doctor
bin/ballastctl start
```

`make build` installs Ballast's own Python 3.12 under `runtime/`, so the server's system Python (3.9, for example) is not used and does not need changing.

The fix was checked on a copy of `main` with every text file converted to CRLF and then only `scripts/*.sh` converted back, which matches the state in the second symptom. 156 files had carriage returns. After the steps above, 0 remained, `make build` finished, and `bin/ballast doctor` reported no failures.

## If `make build` then fails to download

`make build` downloads Python from `github.com` and packages from `pypi.org`. If the server can reach neither, or needs a proxy:

- With a proxy, set it first: `export https_proxy=http://proxy.example.com:8080` (curl and pip both read it).
- With no internet at all, build an offline kit on a machine that has internet access, copy it over, and install from it. See "C. No internet on the server" in `README.md`.

## Keeping it from coming back

- When cloning on a machine whose git converts line endings, turn the conversion off for this clone: `git clone -c core.autocrlf=false https://github.com/pagombin/MongoDB-LoadTest.git`.
- Prefer copying the repository to the server with `git clone` on the server, or with `scp`/`rsync` from a Linux or macOS machine, rather than through a Windows folder or ZIP.
- Once the `.gitattributes` file is on `main`, a fresh clone or a `git pull` gives Unix line endings regardless of the git setting.

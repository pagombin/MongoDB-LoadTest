# Enhancements after the 1.0 rebuild

Thirty-one changes made after the first rebuild. About a third make Ballast installable on servers where pip works only for some packages. Each row names the commit and how it was checked. Commit links are relative to https://github.com/pagombin/MongoDB-LoadTest/commit/.

## Installing on restricted servers

| # | Change | Commit | Checked by |
|---|---|---|---|
| 1 | Runs on Python 3.10 (Ubuntu 22.04's default), not only 3.11+ | 36a2184 | Unit tests on 3.10 with only the core packages (93 passed); server start on 3.10 |
| 2 | `cryptography` is no longer needed: self-signed certificates are generated and read in pure Python (D-027) | 36a2184 | `tests/unit/test_tls.py` (handshake, signature verified by `cryptography`, foreign certificate parsed); `openssl verify`; browser test over the new certificate |
| 3 | PyYAML is optional: a built-in reader handles the config file (D-028) | 36a2184 | `tests/unit/test_miniyaml.py`, compared with PyYAML |
| 4 | Faker and PyYAML load only for the legacy `schema` document shape | 36a2184 | Unit tests with neither installed |
| 5 | Core is three packages (`aiohttp`, `pydantic`, `pymongo`); extras `yaml`, `schema`, `all`, `legacy` | 36a2184 | `pip install .` on Python 3.10 with pip 23, then `ballast doctor` |
| 6 | `ballast doctor`: Python, `sqlite3`, `ssl`, packages, C speedups, process start, directories, port, optional cluster; exact fix command per problem | 36a2184 | `tests/unit/test_doctor.py`; output in both install logs |
| 7 | `scripts/install.sh`: venv if possible, else `pip --target`, no setuptools or compiler needed for Ballast itself (D-030) | ca98107 | Stock Ubuntu 22.04 without `python3-venv`: install and 21 of 21 host checks including a load run (`docs/evidence/install-ubuntu22-no-venv.log`) |
| 8 | Installer retries the aiohttp family in pure-Python mode when a wheel is missing and nothing can compile | ca98107 | Script path reviewed; not exercised, because every test host had wheels |
| 9 | Offline kit: `scripts/make-wheelhouse.sh` builds source plus wheels for a target Python and architecture | ca98107, 039bea5 | Install, doctor and start on Ubuntu 22.04 with no network (`docs/evidence/install-offline-kit.log`); this test found that version-gated dependencies were missing, fixed in 039bea5 |
| 10 | Intel Mac bundle (`darwin-amd64`) now builds, since `cryptography` is gone (D-018) | this commit | Built; interpreter and extensions checked to be x86-64 Mach-O; not run on a Mac |

## Engine, API and command line

| # | Change | Commit | Checked by |
|---|---|---|---|
| 11 | Guardrails: stop a run when the failed share or p99 stays over a limit for N measured seconds | cad7be8 | Unit test of the rule; integration test (exit code 4); live webhook received |
| 12 | Pass/fail criteria (SLO) per profile, stored with each run | cad7be8 | Unit tests; integration test of both outcomes; browser test checks the report card |
| 13 | `ballast run`: one run without the web server, with progress lines, Markdown summary, JSON and Markdown files, and exit codes for CI | cad7be8 | `tests/integration/test_headless.py` against MongoDB 8.0 |
| 14 | Webhook notifications (Slack-compatible) on start, finish, guardrail, and new failures | cad7be8 | Unit test with a local HTTP server; three real deliveries from a guardrail run |
| 15 | `GET /metrics` in Prometheus text format | cad7be8 | Unit tests of the text and the endpoint |
| 16 | Run name, notes and tags editable after the run (`PATCH /api/runs/{id}`), with validation | cad7be8 | Unit test; browser test |
| 17 | Run again (`POST /api/runs/{id}/rerun`), with "(again)" names | cad7be8, 661f4dc | Browser test starts and stops a rerun |
| 18 | Markdown summary export (`export.md`) | cad7be8 | Unit test of the exact table lines; browser test reads it from the clipboard |
| 19 | `ballastctl backup` and `restore` | cad7be8 | Unit test: backup, delete, restore, refusal of a non-store file |
| 20 | Live phase progress in the run state; store schema v2 with backed-up migration | cad7be8 | Store tests; browser test sees the progress bar |

## Interface

| # | Change | Commit | Checked by |
|---|---|---|---|
| 21 | Guardrail and pass/fail fields in the profile form and the new-run wizard, described on the review step | 661f4dc | Browser test fills them and checks the review text; accessibility scan of the step |
| 22 | Sizing notes that follow the form: how long a stall the in-flight slots absorb, pool limit, write volume per hour, data load size | 661f4dc | Visible in `docs/design/after/02b-new-run-workload.png` |
| 23 | Report pass/fail card, also in the offline HTML export; guardrail notice | 661f4dc | Browser test; `docs/design/after/16-report-slo-tags.png` |
| 24 | Report notes and tags with an edit dialog; tags link to a filtered runs list | 661f4dc | Browser test |
| 25 | Copy summary (Markdown to the clipboard, with a fallback dialog) and Markdown download | 661f4dc | Browser test reads the clipboard |
| 26 | Runs list: tag filter, SLO and guardrail columns, and filters kept in the address for bookmarks | 661f4dc | Browser test opens `#/runs?tag=e2e` and checks the address after typing |
| 27 | Live view: progress bar with time left in the phase and the run | 661f4dc | Browser test; `docs/design/after/03-live.png` |
| 28 | Live numbers in the tab title, and opt-in desktop notifications while the tab is in the background | 661f4dc | Browser test checks the title; notifications tested by hand only |
| 29 | Compare: change against the first run for each metric, green or red; duplicate names show their start time | 661f4dc | Browser test; `docs/design/after/05-compare.png` |
| 30 | Ctrl+K command palette for pages and runs | 661f4dc | Browser test; accessibility scan with the palette open; `docs/design/after/17-command-palette.png` |
| 31 | System page lists installed components with the command to add each | 661f4dc | Browser test; `docs/design/after/10-system.png` |

The list I first proposed had two items that already existed (runs search and filters, and multi-select compare). I replaced them with 26 (tag filter and filters in the address) and 22 (sizing notes).

Bugs found while doing this, and fixed: the supervisor started the server without the installer's package path (found on Ubuntu 22.04); `BALLAST_SITE` and `BALLAST_LAUNCHER` were rejected by the strict environment check; Ubuntu 22.04's pip built an empty `UNKNOWN` package under `--target`; the offline kit lacked `async-timeout` for Python 3.10; the live "Target rate" box was empty when the view opened during data load; doctor suggested `pip install --user`, which a bundle or `--target` install would never import.

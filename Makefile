PY ?= runtime/python/bin/python3
TARGETS ?= linux-amd64,linux-arm64,darwin-arm64

.PHONY: build dev test integration e2e lint format typecheck audit release clean

build:
	scripts/setup-runtime.sh

dev: build
	$(PY) -m pip install -q -r requirements/dev.txt

test:
	$(PY) -m pytest -q tests

integration:
	$(PY) -m pytest -q -m integration tests/integration

e2e:
	node tests/e2e/run.mjs

lint:
	$(PY) -m ruff check ballast tests/unit tests/integration tests/soak scripts
	$(PY) -m ruff format --check ballast tests/unit tests/integration tests/soak scripts
	sh -n bin/ballast && sh -n bin/ballastctl && sh -n scripts/setup-runtime.sh && sh -n scripts/verify-host.sh

format:
	$(PY) -m ruff format ballast tests/unit tests/integration scripts

typecheck:
	$(PY) -m mypy ballast
	npx --yes -p typescript@5.6.3 tsc -p ballast/web/jsconfig.json

audit:
	$(PY) -m pip_audit -r requirements/runtime.lock --strict

release:
	$(PY) scripts/build_release.py --targets $(TARGETS)

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache

from __future__ import annotations

import pytest

from mongo_load_tester.compat import ensure_supported_python


def test_ensure_supported_python_accepts_supported_versions() -> None:
    ensure_supported_python((3, 10, 0))
    ensure_supported_python((3, 12, 1))


def test_ensure_supported_python_rejects_old_versions() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        ensure_supported_python((3, 9, 18))
    assert "requires Python 3.10+" in str(exc_info.value)

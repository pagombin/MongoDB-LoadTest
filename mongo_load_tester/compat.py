"""Runtime compatibility checks."""

from __future__ import annotations

import sys
from typing import Optional, Tuple

MIN_SUPPORTED_PYTHON: Tuple[int, int] = (3, 10)


def ensure_supported_python(version_info: Optional[Tuple[int, int, int]] = None) -> None:
    """Raise a clear error when Python runtime is too old."""

    detected = version_info or (
        int(sys.version_info.major),
        int(sys.version_info.minor),
        int(sys.version_info.micro),
    )
    min_major, min_minor = MIN_SUPPORTED_PYTHON
    if (detected[0], detected[1]) < MIN_SUPPORTED_PYTHON:
        raise RuntimeError(
            "mongo-load-tester requires Python %d.%d+ (detected %d.%d.%d). "
            "Please create a new virtual environment with python3.10, python3.11, or python3.12."
            % (min_major, min_minor, detected[0], detected[1], detected[2])
        )

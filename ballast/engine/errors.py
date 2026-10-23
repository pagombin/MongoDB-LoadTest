"""Classify failed operations by error code, code name, and error labels."""

from __future__ import annotations

from dataclasses import dataclass

from pymongo import errors as pe

NOT_PRIMARY_CODES = {10107: "NotWritablePrimary", 13435: "NotPrimaryNoSecondaryOk", 13436: "NotPrimaryOrSecondary"}
STATE_CHANGE_CODES = {
    189: "PrimarySteppedDown",
    11602: "InterruptedDueToReplStateChange",
    91: "ShutdownInProgress",
    11600: "InterruptedAtShutdown",
    262: "ExceededTimeLimit",
    50: "MaxTimeMSExpired",
    9001: "SocketException",
    6: "HostUnreachable",
    7: "HostNotFound",
    89: "NetworkTimeout",
    11000: "DuplicateKey",
    13: "Unauthorized",
    18: "AuthenticationFailed",
    64: "WriteConcernFailed",
    100: "UnsatisfiableWriteConcern",
    112: "WriteConflict",
    251: "NoSuchTransaction",
    133: "FailedToSatisfyReadPreference",
}
KNOWN_CODES = {**NOT_PRIMARY_CODES, **STATE_CHANGE_CODES}


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    error_class: str
    code: int | None
    code_name: str | None
    labels: tuple[str, ...]
    message: str

    def as_dict(self) -> dict:
        return {
            "class": self.error_class,
            "code": self.code,
            "code_name": self.code_name,
            "labels": list(self.labels),
            "message": self.message,
        }


def _labels(exc: BaseException) -> tuple[str, ...]:
    details = getattr(exc, "details", None)
    labels: list[str] = []
    if isinstance(details, dict):
        labels.extend(details.get("errorLabels") or [])
    raw = getattr(exc, "_error_labels", None)
    if raw:
        labels.extend(raw)
    return tuple(sorted(set(labels)))


def _first_write_error(exc: pe.BulkWriteError) -> tuple[int | None, str | None]:
    details = exc.details or {}
    for key in ("writeErrors", "writeConcernErrors"):
        items = details.get(key) or []
        if items:
            first = items[0]
            return first.get("code"), first.get("codeName")
    return None, None


def classify(exc: BaseException, redact=None) -> ErrorInfo:
    """Return a stable class name plus the raw code, code name and labels."""
    message = str(exc).split("\n")[0][:300]
    if redact is not None:
        message = redact(message)
    labels = _labels(exc)
    code: int | None = getattr(exc, "code", None)
    code_name: str | None = None
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        code_name = details.get("codeName")
        if code is None and isinstance(details.get("code"), int):
            code = details["code"]

    if isinstance(exc, pe.BulkWriteError):
        code, code_name = _first_write_error(exc)

    if isinstance(exc, pe.ServerSelectionTimeoutError):
        cls = "ServerSelectionTimeout"
    elif isinstance(exc, pe.NetworkTimeout):
        cls = "NetworkTimeout"
    elif isinstance(exc, pe.NotPrimaryError):
        cls = code_name or NOT_PRIMARY_CODES.get(code or 0, "NotWritablePrimary")
    elif isinstance(exc, (pe.AutoReconnect, pe.ConnectionFailure)):
        cls = "NetworkError"
    elif isinstance(exc, pe.WTimeoutError):
        cls = "WriteConcernTimeout"
    elif isinstance(exc, pe.ExecutionTimeout):
        cls = "OperationTimeout"
    elif isinstance(exc, pe.PyMongoError) and getattr(exc, "timeout", False):
        cls = "OperationTimeout"
    elif isinstance(exc, (pe.OperationFailure, pe.BulkWriteError, pe.WriteConcernError)):
        cls = code_name or KNOWN_CODES.get(code or 0) or (f"Code{code}" if code is not None else type(exc).__name__)
    elif isinstance(exc, TimeoutError):
        cls = "OperationTimeout"
    elif isinstance(exc, pe.PyMongoError):
        cls = type(exc).__name__
    else:
        cls = "ClientError"
    if code is not None and code_name is None:
        code_name = KNOWN_CODES.get(code)
    return ErrorInfo(error_class=cls, code=code, code_name=code_name, labels=labels, message=message)

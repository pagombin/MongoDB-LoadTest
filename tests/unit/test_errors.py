from pymongo import errors as pe

from ballast.engine.errors import classify


def test_not_writable_primary_by_code_and_labels():
    exc = pe.NotPrimaryError("not primary", {"code": 10107, "codeName": "NotWritablePrimary", "errorLabels": ["RetryableWriteError"]})
    info = classify(exc)
    assert info.error_class == "NotWritablePrimary"
    assert info.code == 10107
    assert info.labels == ("RetryableWriteError",)


def test_operation_failure_uses_code_name():
    exc = pe.OperationFailure("stepped down", code=189, details={"code": 189, "codeName": "PrimarySteppedDown", "errorLabels": ["RetryableWriteError"]})
    info = classify(exc)
    assert info.error_class == "PrimarySteppedDown"
    assert "RetryableWriteError" in info.labels


def test_operation_failure_without_code_name_uses_known_table():
    info = classify(pe.OperationFailure("dup", code=11000))
    assert info.error_class == "DuplicateKey"
    assert info.code_name == "DuplicateKey"


def test_network_classes():
    assert classify(pe.NetworkTimeout("timed out")).error_class == "NetworkTimeout"
    assert classify(pe.AutoReconnect("connection closed")).error_class == "NetworkError"
    assert classify(pe.ServerSelectionTimeoutError("no primary")).error_class == "ServerSelectionTimeout"
    assert classify(pe.ExecutionTimeout("exceeded", code=50)).error_class == "OperationTimeout"
    assert classify(pe.WTimeoutError("wtimeout", code=64)).error_class == "WriteConcernTimeout"


def test_bulk_write_error_uses_first_write_error():
    exc = pe.BulkWriteError({"writeErrors": [{"code": 11000, "codeName": "DuplicateKey", "errmsg": "dup"}]})
    assert classify(exc).error_class == "DuplicateKey"


def test_messages_are_redacted_when_redactor_given():
    info = classify(pe.ConfigurationError("bad uri mongodb://u:secret@h"), redact=lambda s: s.replace("secret", "****"))
    assert "secret" not in info.message


def test_non_driver_errors():
    assert classify(TimeoutError()).error_class == "OperationTimeout"
    assert classify(ValueError("x")).error_class == "ClientError"

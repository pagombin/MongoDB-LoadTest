import os
from pathlib import Path

import pytest

from ballast.config import ConfigError, load, split_listen


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "ballast.yaml"
    p.write_text(text)
    return p


def test_defaults_without_file(tmp_path):
    loaded = load(None, [], environ={}, home=tmp_path)
    assert loaded.config.server.listen == "127.0.0.1:8443"
    assert loaded.config.tls.mode == "self-signed"
    assert loaded.path is None


def test_precedence_flag_over_env_over_file_over_default(tmp_path):
    cfg = write(tmp_path, "server:\n  listen: 127.0.0.1:9001\nlogging:\n  level: warning\n")
    env = {"BALLAST_SERVER_LISTEN": "127.0.0.1:9002", "BALLAST_LOGGING_FORMAT": "text"}
    loaded = load(cfg, ["server.listen=127.0.0.1:9003"], environ=env, home=tmp_path)
    assert loaded.config.server.listen == "127.0.0.1:9003"
    assert loaded.sources["server.listen"] == "flag"
    assert loaded.config.logging.format == "text"
    assert loaded.sources["logging.format"] == "env"
    assert loaded.config.logging.level == "warning"
    assert loaded.sources["logging.level"] == "file"
    assert loaded.config.retention.runs_max_count == 0
    assert "retention.runs_max_count" not in loaded.sources


def test_env_values_are_typed(tmp_path):
    loaded = load(
        None,
        [],
        environ={
            "BALLAST_SERVER_TRUST_PROXY_HEADERS": "true",
            "BALLAST_TLS_SELF_SIGNED_HOSTS": "[a.example, 10.0.0.5]",
            "BALLAST_RETENTION_RUNS_MAX_COUNT": "7",
        },
        home=tmp_path,
    )
    assert loaded.config.server.trust_proxy_headers is True
    assert loaded.config.tls.self_signed_hosts == ["a.example", "10.0.0.5"]
    assert loaded.config.retention.runs_max_count == 7


@pytest.mark.parametrize(
    "text, needle",
    [
        ("server:\n  listn: 127.0.0.1:9000\n", "server.listn: unknown key"),
        ("serverx:\n  listen: 1\n", "serverx: unknown key"),
        ("tls:\n  mode: maybe\n", "tls.mode"),
        ("server:\n  listen: 127.0.0.1:443\n", "below 1024"),
        ("branding:\n  accent_color: teal\n", "branding.accent_color"),
    ],
)
def test_unknown_keys_and_bad_values_name_the_key(tmp_path, text, needle):
    with pytest.raises(ConfigError) as exc:
        load(write(tmp_path, text), [], environ={}, home=tmp_path)
    assert needle in str(exc.value)


def test_unknown_env_variable_stops_startup(tmp_path):
    with pytest.raises(ConfigError, match="BALLAST_SERVER_LISTN"):
        load(None, [], environ={"BALLAST_SERVER_LISTN": "x"}, home=tmp_path)
    with pytest.raises(ConfigError, match="BALLAST_NOPE_X"):
        load(None, [], environ={"BALLAST_NOPE_X": "x"}, home=tmp_path)


def test_bad_flag_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(None, ["server.listen"], environ={}, home=tmp_path)
    with pytest.raises(ConfigError):
        load(None, ["server.nope=1"], environ={}, home=tmp_path)


def test_relative_paths_resolve_against_install_dir_not_cwd(tmp_path, monkeypatch):
    home = tmp_path / "install"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = load(None, ["paths.data_dir=mydata", "paths.log_dir=/abs/logs"], environ={}, home=home)
    assert loaded.data_dir == home / "mydata"
    assert loaded.log_dir == Path("/abs/logs")
    assert loaded.resolve("certs/server.crt") == home / "certs" / "server.crt"


def test_default_config_file_in_install_dir(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ballast.yaml").write_text("server:\n  listen: 127.0.0.1:9100\n")
    loaded = load(None, [], environ={}, home=tmp_path)
    assert loaded.config.server.listen == "127.0.0.1:9100"


def test_secret_sources_and_permission_warning(tmp_path):
    from ballast import config as cfgmod

    cfgmod.PERMISSION_WARNINGS.clear()
    token = tmp_path / "token"
    token.write_text("s3cret-token\n")
    os.chmod(token, 0o644)
    loaded = load(None, [f"server.access_token_file={token}"], environ={}, home=tmp_path)
    assert loaded.access_token() == "s3cret-token"
    assert any("readable by group or others" in w for w in cfgmod.PERMISSION_WARNINGS)
    cfgmod.PERMISSION_WARNINGS.clear()
    os.chmod(token, 0o600)
    loaded.access_token()
    assert cfgmod.PERMISSION_WARNINGS == []
    env_loaded = load(None, ["server.access_token_env=MY_TOKEN"], environ={"MY_TOKEN": "from-env"}, home=tmp_path)
    os.environ["MY_TOKEN"] = "from-env"
    try:
        assert env_loaded.access_token() == "from-env"
    finally:
        del os.environ["MY_TOKEN"]
    with pytest.raises(ConfigError, match="only one"):
        load(None, ["server.access_token=a", "server.access_token_env=B"], environ={}, home=tmp_path)


def test_redacted_hides_secrets(tmp_path):
    cfg = write(
        tmp_path,
        "server:\n  access_token: topsecret\ntargets:\n  - name: prod\n    uri: mongodb://bench:pw123456@h1:27017/\n    password: other-secret\n",
    )
    data = load(cfg, [], environ={}, home=tmp_path).redacted()
    text = str(data)
    assert "topsecret" not in text and "pw123456" not in text and "other-secret" not in text


def test_target_preset_password_from_env(tmp_path):
    cfg = write(tmp_path, "targets:\n  - name: prod\n    uri: mongodb+srv://bench@cluster.example.net/\n    password_env: PROD_PW\n")
    os.environ["PROD_PW"] = "p@ss word"
    try:
        presets = load(cfg, [], environ={}, home=tmp_path).target_presets()
    finally:
        del os.environ["PROD_PW"]
    assert presets[0]["uri"] == "mongodb+srv://bench:p%40ss%20word@cluster.example.net/"


def test_split_listen():
    assert split_listen("0.0.0.0:8443") == ("0.0.0.0", 8443)
    assert split_listen(":9000") == ("0.0.0.0", 9000)
    assert split_listen("[::1]:9000") == ("::1", 9000)
    with pytest.raises(ValueError):
        split_listen("nope")


def test_tls_off_survives_yaml_boolean_parsing(tmp_path):
    assert load(write(tmp_path, "tls:\n  mode: off\n"), [], environ={}, home=tmp_path).config.tls.mode == "off"
    assert load(None, ["tls.mode=off"], environ={}, home=tmp_path).config.tls.mode == "off"
    assert load(None, [], environ={"BALLAST_TLS_MODE": "off"}, home=tmp_path).config.tls.mode == "off"


def test_reserved_environment_variables_are_ignored(tmp_path):
    env = {
        "BALLAST_HOME": "/x",
        "BALLAST_CONFIG": "",
        "BALLAST_SUPERVISED": "1",
        "BALLAST_FOREGROUND": "1",
        "BALLAST_TEST_URI": "mongodb://h/",
    }
    assert load(None, [], environ=env, home=tmp_path).config.server.listen == "127.0.0.1:8443"

import builtins
import sys

from ballast import doctor
from ballast.config import load


def test_reports_required_and_optional_packages(tmp_path):
    checks = {c.name: c for c in doctor.run(load(None, ["server.listen=127.0.0.1:59999"], environ={}, home=tmp_path), skip_process=True)}
    assert checks["python"].status == doctor.OK
    assert checks["pymongo"].status == doctor.OK and checks["aiohttp"].status == doctor.OK
    assert checks["data directory"].status == doctor.OK
    assert "cryptography" not in checks


def test_missing_optional_package_is_a_warning_with_the_pip_command(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faker" or name.startswith("faker."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "faker", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    faker = next(c for c in doctor.package_checks() if c.name == "faker")
    assert faker.status == doctor.WARN
    assert faker.fix.endswith("-m pip install faker") or faker.fix.endswith("-m pip install --user faker")
    assert "simple" in faker.detail


def test_render_counts_failures():
    text = doctor.render([doctor.Check("a", doctor.FAIL, "broken", "do x"), doctor.Check("b", doctor.OK, "fine")], color=False)
    assert "FAIL  a: broken" in text and "fix: do x" in text and text.endswith("1 failed, 0 warnings")


def test_pip_command_targets_the_install_site(monkeypatch, tmp_path):
    monkeypatch.setenv("BALLAST_SITE", str(tmp_path))
    assert doctor.pip_command("faker").endswith(f"-m pip install --target {tmp_path} faker")

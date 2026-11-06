"""run.py: config.env parsing."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run  # noqa: E402


@pytest.fixture
def clean_env():
    saved = dict(os.environ)          # load_config exports its settings; undo that afterwards
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_config_parsing(tmp_path, monkeypatch, clean_env):
    (tmp_path / "config.env").write_text(
        'CENSUS_API_KEY= #your key\nexport PORT=9000 # comment\nBASE_URL_PATH="/econ"\n'
        "A=x#y\nnot a setting\n")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    for k in ("PORT", "CENSUS_API_KEY", "BASE_URL_PATH", "HOST", "A"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOST", "0.0.0.0")                  # the environment wins (Docker)
    cfg = run.load_config()
    assert (cfg["CENSUS_API_KEY"], cfg["PORT"], cfg["BASE_URL_PATH"], cfg["A"]) == \
        ("", "9000", "/econ", "x#y")
    assert cfg["HOST"] == "0.0.0.0" and cfg["RUN_TESTS"] == "first-run"


# ------------------------------------------------------------------ data build / version stamp
def test_data_version_matches_pipeline():
    import build_econ_dataset
    assert run.data_version_wanted() == build_econ_dataset.DATA_VERSION != ""


@pytest.fixture
def data_dir(tmp_path, monkeypatch, clean_env):
    d = tmp_path / "econ_data"
    d.mkdir()
    monkeypatch.setattr(run, "DATA_DIR", d)
    return d


def fill(d, stamp):
    for t in run.REQUIRED_TABLES:
        (d / f"{t}.csv").write_text("x\n")
    if stamp is not None:
        (d / ".data-version").write_text(stamp + "\n")


def start(monkeypatch, tmp_path, rc_build=0, rc_tests=0, argv=("run",)):
    """Run run.main() with every subprocess faked; returns the scripts it ran and the exit."""
    calls = []

    def fake_run(py, *args, replace=False):
        calls.append(args[0] if args[0] != "-m" else args[1])
        if args[0] == "build_econ_dataset.py":
            return rc_build
        return rc_tests if args[:2] == ("-m", "pytest") else 0

    monkeypatch.setattr(run, "load_config", lambda: dict(run.DEFAULTS))
    monkeypatch.setattr(run, "python_env", lambda cfg: Path(sys.executable))
    monkeypatch.setattr(run, "prefix_of", lambda py: tmp_path)
    monkeypatch.setattr(run, "ensure_packages", lambda py, prefix: True)
    monkeypatch.setattr(run, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    with pytest.raises(SystemExit) as e:
        run.main()
    return calls, e.value.code


def test_current_data_is_not_rebuilt(data_dir, tmp_path, monkeypatch):
    fill(data_dir, run.data_version_wanted())
    calls, _ = start(monkeypatch, tmp_path)
    assert calls == ["pytest", "streamlit"]


@pytest.mark.parametrize("stamp", [None, "1"])   # built before the stamp existed / older version
def test_stale_data_is_rebuilt(data_dir, tmp_path, monkeypatch, stamp):
    fill(data_dir, stamp)
    calls, _ = start(monkeypatch, tmp_path)
    assert calls == ["build_econ_dataset.py", "pytest", "streamlit"]


def test_stale_data_start_only_warns(data_dir, tmp_path, monkeypatch):
    fill(data_dir, None)
    calls, _ = start(monkeypatch, tmp_path, argv=("start",))
    assert calls == ["streamlit"]


def test_partial_source_failure_is_not_fatal(data_dir, tmp_path, monkeypatch):
    calls, code = start(monkeypatch, tmp_path, rc_build=2)
    assert calls == ["build_econ_dataset.py", "pytest", "streamlit"]


def test_failed_build_without_usable_data_exits(data_dir, tmp_path, monkeypatch):
    fill(data_dir, None)                                # e.g. tables that lack the US
    calls, code = start(monkeypatch, tmp_path, rc_build=1)
    assert calls == ["build_econ_dataset.py"] and code == 1


def test_failed_tests_exit(data_dir, tmp_path, monkeypatch):
    fill(data_dir, run.data_version_wanted())
    calls, code = start(monkeypatch, tmp_path, rc_tests=1)
    assert calls == ["pytest"] and code == 1

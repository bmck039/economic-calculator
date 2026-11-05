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

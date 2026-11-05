"""run.py: config parsing and the auto-updater (against a fake GitHub API on localhost)."""
import io
import json
import os
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run  # noqa: E402

# A fake release: its run.py "setup" writes the required tables, or fails.
GOOD = ("import pathlib, sys\n"
        "d = pathlib.Path(__file__).parent / 'econ_data'; d.mkdir()\n"
        f"[(d / (t + '.csv')).write_text('x\\n1\\n') for t in {run.REQUIRED_TABLES!r}]\n")
BAD = "import sys; sys.exit(1)\n"


def tarball(run_py: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = run_py.encode()
        info = tarfile.TarInfo("owner-repo-abc123/run.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def github(monkeypatch):
    """Serves /repos/o/r/releases/latest and tarballs from a dict the test fills in."""
    routes: dict[str, bytes] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("UPDATE_API_URL", base)

    def publish(tag, run_py):
        routes[f"/tar/{tag}"] = tarball(run_py)
        routes["/repos/o/r/releases/latest"] = json.dumps(
            {"tag_name": tag, "tarball_url": f"{base}/tar/{tag}"}).encode()

    yield publish
    srv.shutdown()


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
    for k in ("ECON_CONFIG_LOADED", "PORT", "CENSUS_API_KEY", "BASE_URL_PATH", "HOST", "A"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOST", "0.0.0.0")                  # the environment wins (Docker)
    cfg = run.load_config()
    assert (cfg["CENSUS_API_KEY"], cfg["PORT"], cfg["BASE_URL_PATH"], cfg["A"]) == \
        ("", "9000", "/econ", "x#y")
    assert cfg["HOST"] == "0.0.0.0" and cfg["RUN_TESTS"] == "first-run"
    assert Path(cfg["VENV_DIR"]).is_absolute()


def test_no_release_yet(github, tmp_path):
    assert run.latest_release("o/r") is None
    assert not run.wants_update(None, tmp_path)


def test_good_release_becomes_current(github, tmp_path, monkeypatch):
    monkeypatch.delenv("ECON_CACHE_DIR", raising=False)
    github("v1.0", GOOD)
    rel = run.latest_release("o/r")
    assert run.wants_update(rel, tmp_path)
    assert run.stage_release(rel, tmp_path, Path(sys.executable))
    assert run.current_release(tmp_path) == "v1.0"
    assert run.release_script(tmp_path, "v1.0").exists()
    assert not run.wants_update(rel, tmp_path)              # already current

    github("v1.1", BAD)                                     # a broken release is rejected...
    rel = run.latest_release("o/r")
    assert not run.stage_release(rel, tmp_path, Path(sys.executable))
    assert run.current_release(tmp_path) == "v1.0"          # ...the old one stays current
    assert not (tmp_path / "releases" / "v1.1").exists()
    for _ in range(run.MAX_ATTEMPTS - 1):                   # ...retried a few times (could be
        assert run.wants_update(rel, tmp_path)              # a network blip), then skipped
        assert not run.stage_release(rel, tmp_path, Path(sys.executable))
    assert not run.wants_update(rel, tmp_path)

    github("v1.2", GOOD)                                    # the next good one is taken
    assert run.stage_release(run.latest_release("o/r"), tmp_path, Path(sys.executable))
    run.cleanup_releases(tmp_path)
    assert [d.name for d in (tmp_path / "releases").iterdir()] == ["v1.2"]

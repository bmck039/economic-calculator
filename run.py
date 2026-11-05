#!/usr/bin/env python3
"""One command for first-time setup and for starting the Economic Calculator.

Works on macOS, Linux and Windows with only the Python standard library.

    python run.py                  set up whatever is missing, then start the app
    python run.py setup            set up only (venv, packages, data, tests); don't start
    python run.py start            start the app now (skips the data build and tests)
    python run.py test             run the test suite
    python run.py data             rebuild the data tables (uses the 30-day download cache)
    python run.py data --refresh   re-download everything, ignoring the cache

Add --no-test to skip the first-run tests. Settings live in config.env.
With AUTO_UPDATE=true (set by compose.yaml), `run` also follows the latest stable GitHub
release of UPDATE_REPO; see "auto-update" below.
(On Windows use `py run.py` if `python` isn't on PATH; on macOS/Linux `python3 run.py`.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "econ_data"
REQUIRED_TABLES = ["earnings_profile", "tax_curve", "couple_budget", "life_events",
                   "fx_ppp", "reference_values", "country_snapshot"]
DEFAULTS = {"HOST": "localhost", "PORT": "8501", "BASE_URL_PATH": "", "OPEN_BROWSER": "true",
            "VENV_DIR": ".venv", "RUN_TESTS": "first-run", "AUTO_UPDATE": "false",
            "UPDATE_REPO": "bmck039/economic-calculator", "UPDATE_CHECK_HOURS": "6"}
WINDOWS = os.name == "nt"

if WINDOWS:
    os.system("")  # enables ANSI colours in the Windows console
_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def say(msg: str) -> None:
    print(f"{_c('1;34', '==>')} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{_c('1;33', 'warning:')} {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"{_c('1;31', 'error:')} {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ------------------------------------------------------------------ config
def load_config() -> dict[str, str]:
    """Read config.env (KEY=VALUE lines, # comments, optional quotes / `export`).
    Its values are exported to the environment of everything run.py starts. Variables
    already set in the environment win (that is how Docker passes the settings in)."""
    path = ROOT / "config.env"
    example = ROOT / "example.config.env"
    if os.environ.get("ECON_CONFIG_LOADED"):      # started by another run.py: settings are in env
        path = example = ROOT / ".no-config"
    if not path.exists() and example.exists():
        shutil.copy(example, path)
        warn("created config.env from example.config.env; edit it to add your CENSUS_API_KEY")
    cfg = dict(DEFAULTS)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            warn(f"config.env line {n} ignored: {raw!r}")
            continue
        key, val = m.groups()
        q = re.match(r"""(['"])(.*?)\1""", val)
        val = q.group(2) if q else re.split(r"(?:^|\s+)#", val, maxsplit=1)[0].strip()
        cfg[key] = val
    cfg.update({k: os.environ[k] for k in cfg.keys() | DEFAULTS.keys() if k in os.environ})
    venv = Path(cfg["VENV_DIR"]).expanduser()
    cfg["VENV_DIR"] = str(venv if venv.is_absolute() else ROOT / venv)
    os.environ.update(cfg, ECON_CONFIG_LOADED="1")
    return cfg


# ------------------------------------------------------------------ python environment
def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if WINDOWS else "bin/python")


def python_env(cfg: dict[str, str]) -> Path:
    """Return the interpreter to use: the active venv/conda env, else $VENV_DIR (created if needed)."""
    if sys.prefix != sys.base_prefix:
        say(f"using the active virtualenv: {sys.prefix}")
        return Path(sys.executable)
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if os.environ.get("CONDA_PREFIX") and conda and conda != "base":
        say(f"using the active conda env: {conda}")
        return Path(sys.executable)
    venv = Path(cfg["VENV_DIR"])
    py = venv_python(venv)
    if not py.exists():
        say(f"creating a virtual environment in {venv}")
        r = subprocess.run([sys.executable, "-m", "venv", str(venv)])
        if r.returncode != 0 or not py.exists():
            shutil.rmtree(venv, ignore_errors=True)
            die("could not create the venv. On Debian/Ubuntu: sudo apt install python3-venv")
    return py


def run(py: Path, *args: str, replace: bool = False) -> int:
    env = dict(os.environ, PYTHONUTF8="1")  # consistent UTF-8 output on Windows consoles
    if replace and not WINDOWS:
        # Become the server process so Ctrl+C / kill / service managers reach it directly.
        sys.stdout.flush()
        os.execve(str(py), [str(py), *args], env)
    try:
        return subprocess.run([str(py), *args], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 130


def prefix_of(py: Path) -> Path:
    out = subprocess.run([str(py), "-c", "import sys; print(sys.prefix)"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


# ------------------------------------------------------------------ steps
def ensure_packages(py: Path, prefix: Path) -> bool:
    """pip install only when requirements.txt or the interpreter changed. Returns True if it ran."""
    stamp = prefix / ".econcalc-requirements"
    version = subprocess.run([str(py), "--version"], capture_output=True, text=True).stdout
    want = hashlib.sha256((ROOT / "requirements.txt").read_bytes() + version.encode()).hexdigest()
    if stamp.exists() and stamp.read_text().strip() == want:
        return False
    say("installing Python packages (requirements.txt)")
    if run(py, "-m", "pip", "install", "--disable-pip-version-check", "-q",
           "-r", "requirements.txt") != 0:
        die("pip install failed (see above)")
    stamp.write_text(want)
    return True


def missing_tables(data_dir: Path = DATA_DIR) -> list[str]:
    return [t for t in REQUIRED_TABLES
            if not (data_dir / f"{t}.csv").exists() or (data_dir / f"{t}.csv").stat().st_size == 0]


def build_data(py: Path, refresh: bool) -> None:
    say(f"building the data tables in {DATA_DIR.name}/ "
        "(first run downloads ~10-20 min; later runs use the cache)")
    args = ["build_econ_dataset.py", "--out", str(DATA_DIR)] + (["--refresh"] if refresh else [])
    if os.environ.get("ECON_CACHE_DIR"):       # download cache shared between releases (Docker)
        args += ["--cache", os.environ["ECON_CACHE_DIR"]]
    if run(py, *args) != 0:
        die("the data build failed (see above)")


def run_tests(py: Path) -> bool:
    say("running tests")
    return run(py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests") == 0


def streamlit_args(cfg: dict[str, str]) -> list[str]:
    host, port, base = cfg["HOST"], cfg["PORT"], cfg["BASE_URL_PATH"].strip("/")
    headless = "false" if cfg["OPEN_BROWSER"].lower() in ("1", "true", "yes") else "true"
    args = ["-m", "streamlit", "run", "app.py", "--server.address", host, "--server.port", port,
            "--server.headless", headless, "--browser.gatherUsageStats", "false"]
    if base:
        args += ["--server.baseUrlPath", base]
    shown = "<this machine's address>" if host == "0.0.0.0" else host
    say(f"starting the app at http://{shown}:{port}/{base}  (Ctrl+C to stop)")
    return args


def start_app(py: Path, cfg: dict[str, str]) -> int:
    args = streamlit_args(cfg)
    if not truthy(cfg["AUTO_UPDATE"]):
        return run(py, *args, replace=True)
    return serve_with_updates(py, cfg, args)


def truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def reexec(script: Path, args: list[str]) -> None:
    """Replace this process with `python script args` (a new release's run.py)."""
    cmd = [sys.executable, str(script), *args]
    sys.stdout.flush()
    sys.stderr.flush()
    if WINDOWS:
        sys.exit(subprocess.call(cmd, cwd=script.parent))
    os.chdir(script.parent)
    os.execv(sys.executable, cmd)


# ------------------------------------------------------------------ auto-update
# Each stable release of UPDATE_REPO is unpacked into $ECON_STATE_DIR/releases/<tag>/ and
# fully set up there (packages, its own econ_data, tests) by that release's own run.py,
# while the current version keeps serving. Only if that succeeds does the app switch;
# failures are counted in <state>/failed (network trouble can be temporary) and a release
# is skipped after MAX_ATTEMPTS. <state>/current
# names the release to run; on start, run.py hands over to it.
UA = {"User-Agent": "econ-calculator-updater", "Accept": "application/vnd.github+json"}


def state_dir() -> Path:
    return Path(os.environ.get("ECON_STATE_DIR") or ROOT / ".state").resolve()


def _safe(tag: str) -> str:
    return re.sub(r"[^\w.-]", "_", tag)


def current_release(st: Path) -> str:
    p = st / "current"
    return p.read_text().strip() if p.exists() else ""


def latest_release(repo: str) -> dict | None:
    """The latest non-draft, non-prerelease release, or None (none yet / network error)."""
    api = os.environ.get("UPDATE_API_URL", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(f"{api}/repos/{repo}/releases/latest", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 404:                       # 404 = no stable release published yet
            warn(f"update check failed: HTTP {e.code}")
    except (OSError, ValueError) as e:
        warn(f"update check failed: {e}")
    return None


MAX_ATTEMPTS = 3   # a release that fails this often is skipped until a newer one appears


def wants_update(rel: dict | None, st: Path) -> bool:
    if not rel or not rel.get("tag_name"):
        return False
    tag = rel["tag_name"]
    failed = (st / "failed").read_text().split() if (st / "failed").exists() else []
    return tag != current_release(st) and failed.count(tag) < MAX_ATTEMPTS


def stage_release(rel: dict, st: Path, py: Path) -> bool:
    """Download, set up and test a release. True = ready and now current."""
    tag = rel["tag_name"]
    releases = st / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    tmp, dest = releases / f".staging-{_safe(tag)}", releases / _safe(tag)
    for d in (tmp, dest):
        shutil.rmtree(d, ignore_errors=True)
    say(f"new release {tag}: downloading")
    try:
        req = urllib.request.Request(rel["tarball_url"], headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, tempfile.TemporaryFile() as f:
            shutil.copyfileobj(r, f)
            f.seek(0)
            with tarfile.open(fileobj=f, mode="r:gz") as tar:
                safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tar.extractall(tmp, **safe)
        (top,) = list(tmp.iterdir())            # GitHub tarballs have one top-level folder
        top.rename(dest)
        tmp.rmdir()
    except Exception as e:                      # network etc.: try again at the next check
        warn(f"could not download release {tag}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    say(f"release {tag}: setting up and testing it (anything already running keeps serving)")
    env = dict(os.environ, AUTO_UPDATE="false", PYTHONUTF8="1")
    ok = (dest / "run.py").exists() and subprocess.run(
        [str(py), str(dest / "run.py"), "setup"], cwd=dest, env=env).returncode == 0
    if ok and missing_tables(dest / "econ_data"):
        ok = False
    if not ok:
        warn(f"release {tag} failed setup or tests; staying on the current version")
        with open(st / "failed", "a") as fh:
            fh.write(tag + "\n")
        shutil.rmtree(dest, ignore_errors=True)
        return False
    (st / "current.tmp").write_text(tag)
    os.replace(st / "current.tmp", st / "current")
    say(f"release {tag} is ready")
    return True


def release_script(st: Path, tag: str) -> Path:
    return st / "releases" / _safe(tag) / "run.py"


def cleanup_releases(st: Path) -> None:
    """Delete releases other than the current one (and leftovers of interrupted staging)."""
    keep = _safe(current_release(st))
    for d in (st / "releases").glob("*") if (st / "releases").exists() else []:
        if d.is_dir() and d.name != keep and d.resolve() != ROOT:
            shutil.rmtree(d, ignore_errors=True)


def serve_with_updates(py: Path, cfg: dict[str, str], args: list[str]) -> int:
    """Run the server as a child; periodically stage new releases and switch to them."""
    st, repo = state_dir(), cfg["UPDATE_REPO"]
    every = max(float(cfg["UPDATE_CHECK_HOURS"]), 0.005) * 3600
    proc = subprocess.Popen([str(py), *args], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"))

    def stop_server() -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()

    def on_term(*_):
        stop_server()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    say(f"auto-update on: checking {repo} for new stable releases every {every / 3600:g} h")
    next_check = time.time() + min(every, 60)
    try:
        while True:
            try:
                return proc.wait(timeout=max(1.0, next_check - time.time()))  # server exited
            except subprocess.TimeoutExpired:
                pass
            next_check = time.time() + every
            rel = latest_release(repo)
            if not wants_update(rel, st):
                continue
            if stage_release(rel, st, py):
                say(f"switching to release {rel['tag_name']}")
                stop_server()
                reexec(release_script(st, rel["tag_name"]), ["run"])
            else:
                ensure_packages(py, prefix_of(py))  # undo any packages the failed release changed
    except KeyboardInterrupt:
        stop_server()
        return 130


# ------------------------------------------------------------------ main
def main() -> None:
    if sys.version_info < (3, 10):
        die(f"Python 3.10+ is required (this is {sys.version.split()[0]})")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 2)[2])
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "setup", "start", "test", "data"])
    ap.add_argument("--refresh", action="store_true", help="data: ignore the download cache")
    ap.add_argument("--no-test", action="store_true", help="skip the first-run tests")
    a = ap.parse_args()

    os.chdir(ROOT)
    cfg = load_config()
    if not (cfg["PORT"].isdigit() and 1 <= int(cfg["PORT"]) <= 65535):
        die(f"PORT must be 1-65535, got {cfg['PORT']!r}")
    if not os.environ.get("CENSUS_API_KEY"):
        warn("CENSUS_API_KEY is not set; US rents will use the built-in ACS 2024 reference value")

    auto = a.command == "run" and truthy(cfg["AUTO_UPDATE"])
    if auto:
        st = state_dir()
        cur = current_release(st)
        if cur and release_script(st, cur).exists() and release_script(st, cur).parent != ROOT:
            say(f"handing over to release {cur}")
            reexec(release_script(st, cur), sys.argv[1:])
        cleanup_releases(st)

    py = python_env(cfg)
    prefix = prefix_of(py)
    changed = ensure_packages(py, prefix)

    if auto and not current_release(st):        # first deploy: start on the latest release
        rel = latest_release(cfg["UPDATE_REPO"])
        if wants_update(rel, st) and stage_release(rel, st, py):
            reexec(release_script(st, rel["tag_name"]), sys.argv[1:])

    if a.command == "data":
        build_data(py, a.refresh)
        say("data rebuilt; restart the app to load it")
        return
    if a.command == "test":
        sys.exit(0 if run_tests(py) else 1)

    missing = missing_tables()
    retry = DATA_DIR / ".rebuild"   # tests failed right after a build: a source probably timed out
    if retry.exists() and not missing and a.command in ("run", "setup"):
        say("the last data build failed its tests; rebuilding (downloads already cached are reused)")
        build_data(py, a.refresh)
        changed = True
    elif missing:
        if a.command == "start":
            die(f"no data yet (missing: {', '.join(missing)}). Run `python run.py setup` first")
        build_data(py, a.refresh)
        changed = True
    else:
        age = (time.time() - (DATA_DIR / "earnings_profile.csv").stat().st_mtime) / 86400
        if age > 90:
            warn(f"data is {age:.0f} days old; refresh with `python run.py data`")

    marker = prefix / ".econcalc-tested"
    mode = cfg["RUN_TESTS"]
    want_tests = a.command == "setup" or (
        a.command == "run" and (mode == "always" or (mode == "first-run"
                                                     and (changed or not marker.exists()))))
    if want_tests:
        if a.no_test:
            say("skipping tests (--no-test)")
        elif run_tests(py):
            marker.touch()
            retry.unlink(missing_ok=True)
        else:
            if changed and DATA_DIR.exists():
                retry.touch()               # the next start rebuilds the data before testing
            die("tests failed (see above). Fix them, or start anyway with `python run.py start`")

    if a.command == "setup":
        say("setup complete. Start the app with `python run.py`")
        return
    sys.exit(start_app(py, cfg))


if __name__ == "__main__":
    main()

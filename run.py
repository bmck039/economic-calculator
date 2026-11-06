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
(On Windows use `py run.py` if `python` isn't on PATH; on macOS/Linux `python3 run.py`.)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "econ_data"
REQUIRED_TABLES = ["earnings_profile", "tax_curve", "couple_budget", "life_events",
                   "fx_ppp", "reference_values", "country_snapshot"]
DEFAULTS = {"HOST": "localhost", "PORT": "8501", "BASE_URL_PATH": "", "OPEN_BROWSER": "true",
            "VENV_DIR": ".venv", "RUN_TESTS": "first-run"}
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
    os.environ.update(cfg)
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
    venv = Path(cfg["VENV_DIR"]).expanduser()
    if not venv.is_absolute():
        venv = ROOT / venv
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


def missing_tables() -> list[str]:
    return [t for t in REQUIRED_TABLES
            if not (DATA_DIR / f"{t}.csv").exists() or (DATA_DIR / f"{t}.csv").stat().st_size == 0]


def data_version_wanted() -> str:
    """DATA_VERSION from build_econ_dataset.py (read as text: this script needs no pandas)."""
    m = re.search(r'^DATA_VERSION = "([^"]+)"', (ROOT / "build_econ_dataset.py")
                  .read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def data_version_built() -> str:
    """The stamp a complete, successful build leaves; '' if none (failed, interrupted,
    partial or built by a version before the stamp existed)."""
    try:
        return (DATA_DIR / ".data-version").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_data(py: Path, refresh: bool) -> bool:
    """Build the tables. True if new tables were written; False if the build failed but the
    previous, complete data was kept; exits if there is no usable data."""
    say(f"building the data tables in {DATA_DIR.name}/ "
        "(first run downloads ~10-20 min; later runs use the cache)")
    args = ["build_econ_dataset.py", "--out", str(DATA_DIR)] + (["--refresh"] if refresh else [])
    rc = run(py, *args)
    if rc == 2:   # some sources failed, but every country has what the app needs
        warn("some data sources failed and fell back to cached or reference values "
             "(see the summary above); rebuild later with `python run.py data`")
    elif rc != 0:
        if not missing_tables() and data_version_built() == data_version_wanted():
            warn("the data build failed (see above); keeping the previous data")
            return False
        die("the data build failed (see above). Run it again later (downloads already "
            "cached are reused)")
    return True


def run_tests(py: Path) -> bool:
    say("running tests")
    return run(py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests") == 0


def start_app(py: Path, cfg: dict[str, str]) -> int:
    host, port, base = cfg["HOST"], cfg["PORT"], cfg["BASE_URL_PATH"].strip("/")
    headless = "false" if cfg["OPEN_BROWSER"].lower() in ("1", "true", "yes") else "true"
    args = ["-m", "streamlit", "run", "app.py", "--server.address", host, "--server.port", port,
            "--server.headless", headless, "--browser.gatherUsageStats", "false"]
    if base:
        args += ["--server.baseUrlPath", base]
    shown = "<this machine's address>" if host == "0.0.0.0" else host
    say(f"starting the app at http://{shown}:{port}/{base}  (Ctrl+C to stop)")
    return run(py, *args, replace=True)


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

    py = python_env(cfg)
    prefix = prefix_of(py)
    changed = ensure_packages(py, prefix)

    if a.command == "data":
        if build_data(py, a.refresh):
            say("data rebuilt; restart the app to load it")
        return
    if a.command == "test":
        sys.exit(0 if run_tests(py) else 1)

    missing = missing_tables()
    stale = not missing and data_version_built() != data_version_wanted()
    if missing:
        if a.command == "start":
            die(f"no data yet (missing: {', '.join(missing)}). Run `python run.py setup` first")
        build_data(py, a.refresh)
        changed = True
    elif stale and a.command == "start":
        warn("the data tables are from an incomplete build or an older version; "
             "rebuild with `python run.py data`")
    elif stale:
        say("the data tables are from an incomplete build or an older version; rebuilding "
            "(downloads already cached are reused)")
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
        else:
            die("tests failed (see above). Fix them, or start anyway with `python run.py start`")

    if a.command == "setup":
        say("setup complete. Start the app with `python run.py`")
        return
    sys.exit(start_app(py, cfg))


if __name__ == "__main__":
    main()

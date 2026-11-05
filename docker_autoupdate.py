#!/usr/bin/env python3
"""docker-autoupdate: keep a Docker Compose project on its latest GitHub release.

On a schedule, for every registered project (a git clone of a GitHub repo with a compose
file), this checks the repo's latest *stable* release (drafts and pre-releases are
ignored). When a new release appears, the clone is checked out at that tag and, if the
release changed the Docker setup (Dockerfile, compose file, .dockerignore, or whatever
the project's `.docker-autoupdate.json` lists), the stack is rebuilt with
`docker compose up -d --build`. A failed rebuild is rolled back, retried at the next two
checks, then skipped until a newer release. The tool also keeps itself up to date from
its own GitHub releases.

Only the Python standard library is used; works on macOS (launchd), Linux (systemd user
timer, or cron) and Windows (Task Scheduler).

    python docker_autoupdate.py install [--project DIR] [--every HOURS] [--repo OWNER/NAME]
    python docker_autoupdate.py uninstall [--project DIR | --all]
    python docker_autoupdate.py run [--project DIR]     (what the scheduler runs)
    python docker_autoupdate.py status
    python docker_autoupdate.py --version
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"
TOOL_REPO = "bmck039/docker-auto-update"
APP = "docker-autoupdate"
LABEL = "io.github.bmck039.docker-autoupdate"   # launchd label / Windows task name base
SCRIPT_NAME = "docker_autoupdate.py"
CONFIG_NAME = ".docker-autoupdate.json"
MAX_ATTEMPTS = 3
DEFAULT_WATCH = ["Dockerfile", "Dockerfile.*", "*.Dockerfile", "*.dockerfile", ".dockerignore",
                 "compose.yaml", "compose.yml", "compose.*.yaml", "compose.*.yml",
                 "docker-compose.yaml", "docker-compose.yml",
                 "docker-compose.*.yaml", "docker-compose.*.yml"]
COMPOSE_NAMES = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
# Where Docker / git usually live when a scheduler starts us with a minimal PATH.
EXTRA_PATH = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
              "/Applications/Docker.app/Contents/Resources/bin",
              r"C:\Program Files\Docker\Docker\resources\bin", r"C:\Program Files\Git\cmd"]
SYSTEM = sys.platform           # "darwin", "linux", "win32"
WINDOWS = SYSTEM == "win32"


# ------------------------------------------------------------------ paths, state, logging
def app_dir() -> Path:
    if os.environ.get("DOCKER_AUTOUPDATE_HOME"):
        return Path(os.environ["DOCKER_AUTOUPDATE_HOME"])
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP
    if SYSTEM == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP


def installed_script() -> Path:
    return app_dir() / SCRIPT_NAME


def state_path() -> Path:
    return app_dir() / "state.json"


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("interval_hours", 6)
    st.setdefault("projects", {})
    return st


def save_state(st: dict) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path())


def log(msg: str, level: str = "info") -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level.upper():5} {msg}"
    print(line, file=sys.stderr if level != "info" else sys.stdout, flush=True)
    try:
        d = app_dir()
        if d.exists():
            f = d / f"{APP}.log"
            if f.exists() and f.stat().st_size > 1_000_000:
                os.replace(f, d / f"{APP}.log.1")
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ helpers
def sh(cmd: list[str], cwd: Path | None = None, timeout: float | None = None,
       capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True,
                              capture_output=capture, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return sh(["git", "-C", str(project), *args], timeout=600)


def extend_path() -> None:
    """Schedulers start us with a minimal PATH; add the usual Docker / git locations."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in EXTRA_PATH + [str(Path(x).parent) for x in load_state().get("tool_paths", [])]:
        if p not in parts and Path(p).is_dir():
            parts.append(p)
    os.environ["PATH"] = os.pathsep.join(parts)


def parse_version(tag: str) -> tuple[int, ...]:
    nums = [int(n) for n in re.findall(r"\d+", tag)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def base_python() -> str:
    """The real interpreter (not a project venv that might be deleted later)."""
    exe = Path(getattr(sys, "_base_executable", "") or sys.executable)
    if not exe.exists():
        exe = Path(sys.executable)
    if WINDOWS and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"      # no console window for the scheduled task
    return str(exe)


def github_repo_from_remote(url: str) -> str | None:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ------------------------------------------------------------------ GitHub
def _api(path: str, accept: str = "application/vnd.github+json") -> bytes:
    base = os.environ.get("DOCKER_AUTOUPDATE_API", "https://api.github.com").rstrip("/")
    headers = {"User-Agent": f"{APP}/{__version__}", "Accept": accept}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def latest_release(repo: str) -> dict | None:
    """Latest non-draft, non-prerelease release; None if there is none. Raises on network errors."""
    try:
        return json.loads(_api(f"/repos/{repo}/releases/latest"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ------------------------------------------------------------------ self-update
def self_update() -> bool:
    """Replace the installed copy with the tool's latest release if it is newer."""
    target = installed_script()
    if not target.exists():
        return False
    current = parse_version(_version_of(target.read_text(encoding="utf-8")) or "0")
    try:
        rel = latest_release(TOOL_REPO)
    except (OSError, ValueError) as e:
        log(f"self-update check failed: {e}", "warn")
        return False
    if not rel or parse_version(rel["tag_name"]) <= current:
        return False
    tag = rel["tag_name"]
    try:
        src = _api(f"/repos/{TOOL_REPO}/contents/{SCRIPT_NAME}?ref={urllib.parse.quote(tag)}",
                   accept="application/vnd.github.raw").decode("utf-8")
        compile(src, SCRIPT_NAME, "exec")
        if parse_version(_version_of(src) or "0") != parse_version(tag):
            raise ValueError(f"file version {_version_of(src)!r} does not match tag {tag}")
    except Exception as e:   # noqa: BLE001 - any problem: keep the working copy
        log(f"self-update to {tag} skipped: {e}", "warn")
        return False
    tmp = target.with_suffix(".new")
    tmp.write_text(src, encoding="utf-8")
    os.replace(tmp, target)
    log(f"updated {APP} to {tag}")
    return True


def _version_of(src: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
    return m.group(1) if m else None


# ------------------------------------------------------------------ docker
def docker_cmd(st: dict) -> list[str]:
    return st.get("docker") or [shutil.which("docker") or "docker"]


def compose_cmd(st: dict) -> list[str] | None:
    d = docker_cmd(st)
    if sh(d + ["compose", "version"], timeout=60).returncode == 0:
        return d + ["compose"]
    legacy = shutil.which("docker-compose")
    return [legacy] if legacy else None


def docker_running(st: dict) -> bool:
    return sh(docker_cmd(st) + ["info", "--format", "{{.ServerVersion}}"], timeout=60).returncode == 0


def project_config(project: Path, ref: str) -> dict:
    """`.docker-autoupdate.json` as of `ref` (so a release can change its own rules)."""
    r = git(project, "show", f"{ref}:{CONFIG_NAME}")
    cfg = {}
    if r.returncode == 0:
        try:
            cfg = json.loads(r.stdout)
        except ValueError:
            log(f"{project}: ignoring invalid {CONFIG_NAME} in {ref}", "warn")
    return {"watch": cfg.get("watch", DEFAULT_WATCH), "compose_files": cfg.get("compose_files", [])}


def matches(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or ("/" not in p and fnmatch.fnmatch(name, p))
               for p in patterns)


def compose_files_args(project: Path, cfg: dict) -> list[str]:
    files = cfg["compose_files"] or [n for n in COMPOSE_NAMES if (project / n).exists()][:1]
    return [a for f in files for a in ("-f", f)]


# ------------------------------------------------------------------ the update check
def check_project(path: str, st: dict) -> str:
    """Bring one project to its latest release. Returns a one-line result."""
    p = st["projects"][path]
    project = Path(path)
    if git(project, "rev-parse", "--git-dir").returncode != 0:
        return "error: not a git repository (moved or deleted?)"
    try:
        rel = latest_release(p["repo"])
    except (OSError, ValueError) as e:
        return f"error: could not reach GitHub ({e}); will retry"
    if not rel:
        return "no stable release published yet"
    tag = rel["tag_name"]
    if tag == p.get("deployed"):
        return f"up to date ({tag})"
    failed = p.setdefault("failed", {})
    if failed.get(tag, 0) >= MAX_ATTEMPTS:
        return f"skipping {tag}: it failed {failed[tag]} times (waiting for a newer release)"
    if not docker_running(st):
        return "Docker is not running; will retry"
    compose = compose_cmd(st)
    if not compose:
        return "error: docker compose not found"
    if git(project, "fetch", "--quiet", "--tags", "--force", "origin").returncode != 0:
        return "error: git fetch failed; will retry"
    if git(project, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode:
        return f"error: tag {tag} not found in origin"
    if git(project, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "skipped: the clone has uncommitted changes to tracked files"
    branch = git(project, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch:
        ahead = git(project, "rev-list", "--count", "@{u}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
            return f"skipped: branch {branch} has commits that are not on origin"
    prev = git(project, "rev-parse", "HEAD").stdout.strip()
    base = p.get("deployed") or prev
    if git(project, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode:
        base = prev
    diff = git(project, "diff", "--name-only", base, f"refs/tags/{tag}")
    changed = [x for x in diff.stdout.splitlines() if x]
    cfg = project_config(project, f"refs/tags/{tag}")
    docker_changes = [x for x in changed if matches(x, cfg["watch"])]

    def checkout(ref: str) -> bool:
        args = [ref] if branch and ref == branch else ["--detach", ref]
        ok = git(project, "checkout", "--quiet", *args).returncode == 0
        if ok and (project / ".gitmodules").exists():
            git(project, "submodule", "update", "--init", "--recursive", "--quiet")
        return ok

    if not checkout(f"refs/tags/{tag}"):
        return f"error: could not check out {tag}"
    if not docker_changes:
        p.update(deployed=tag, deployed_at=now_iso())
        failed.pop(tag, None)
        return f"checked out {tag} (no Docker changes, nothing rebuilt)"

    files = compose_files_args(project, cfg)
    running = bool(sh(compose + files + ["ps", "-q"], cwd=project, timeout=120).stdout.strip())
    action = ["up", "-d", "--build", "--remove-orphans"] if running else ["build"]
    log(f"{project}: {tag} changed {', '.join(docker_changes)}; running "
        f"`docker compose {' '.join(action)}`")
    r = sh(compose + files + action, cwd=project, capture=False)
    if r.returncode == 0:
        p.update(deployed=tag, deployed_at=now_iso())
        failed.pop(tag, None)
        return f"deployed {tag} ({'rebuilt and restarted' if running else 'image rebuilt; stack was not running'})"

    failed[tag] = failed.get(tag, 0) + 1
    checkout(branch or prev)                    # roll the clone back
    if running:                                 # and make sure the old version is what runs
        sh(compose + compose_files_args(project, project_config(project, "HEAD"))
           + ["up", "-d", "--build", "--remove-orphans"], cwd=project, capture=False)
    return f"error: rebuilding {tag} failed (attempt {failed[tag]}/{MAX_ATTEMPTS}); rolled back"


class Lock:
    """Keeps two runs from overlapping. A lock older than 3 hours is considered stale."""

    def __init__(self) -> None:
        self.path = app_dir() / ".lock"

    def acquire(self, wait: float) -> bool:
        app_dir().mkdir(parents=True, exist_ok=True)
        deadline = time.time() + wait
        while True:
            try:
                if time.time() - self.path.stat().st_mtime > 3 * 3600:
                    self.path.unlink()
            except OSError:
                pass
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                if time.time() >= deadline:
                    return False
                time.sleep(2)

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def cmd_run(project: str | None, allow_self_update: bool = True) -> int:
    extend_path()
    if allow_self_update and Path(__file__).resolve() == installed_script().resolve() and self_update():
        # hand the rest of this run to the new version
        return subprocess.call([sys.executable, str(installed_script()), "run", "--no-self-update"]
                               + (["--project", project] if project else []))
    lock = Lock()
    # a scheduled run (all projects) just skips if another run is busy; a manual one waits
    if not lock.acquire(wait=900 if project else 0):
        log("another check is still running; skipping this one" if not project
            else "another check is still running; try again later", "warn")
        return 0 if not project else 1
    try:
        st = load_state()
        paths = [str(Path(project).resolve())] if project else list(st["projects"])
        bad = False
        for path in paths:
            if path not in st["projects"]:
                log(f"{path} is not registered (run `install` in it first)", "error")
                bad = True
                continue
            result = check_project(path, st)
            st["projects"][path].update(last_check=now_iso(), last_result=result)
            save_state(st)
            log(f"{path}: {result}", "error" if result.startswith("error") else "info")
            bad |= result.startswith("error")
    finally:
        lock.release()
    return 1 if bad else 0


# ------------------------------------------------------------------ schedulers
def scheduler_command() -> list[str]:
    return [load_state().get("python") or base_python(), str(installed_script()), "run"]


def systemd_units(cmd: list[str], hours: float) -> tuple[str, str]:
    exec_start = " ".join(f'"{c}"' for c in cmd)
    service = ("[Unit]\nDescription=docker-autoupdate: update Docker projects to new GitHub releases\n"
               "After=network-online.target\n\n[Service]\nType=oneshot\n"
               f"ExecStart={exec_start}\n")
    timer = ("[Unit]\nDescription=Run docker-autoupdate periodically\n\n[Timer]\n"
             f"OnBootSec=10min\nOnUnitActiveSec={int(hours * 60)}min\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def launchd_plist(cmd: list[str], hours: float, path_env: str) -> bytes:
    logf = str(app_dir() / f"{APP}.launchd.log")
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": cmd, "StartInterval": int(hours * 3600),
        "RunAtLoad": True, "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": logf, "StandardErrorPath": logf, "ProcessType": "Background",
    })


def schtasks_create_args(cmd: list[str], hours: float) -> list[str]:
    tr = subprocess.list2cmdline(cmd)
    if len(tr) > 261:
        raise SystemExit(f"Task Scheduler command too long ({len(tr)} > 261 chars): {tr}")
    return ["schtasks", "/Create", "/F", "/TN", APP, "/SC", "HOURLY",
            "/MO", str(max(1, round(hours))), "/TR", tr]


def _unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _has_systemd_user() -> bool:
    r = sh(["systemctl", "--user", "is-system-running"], timeout=30)
    return r.stdout.strip() in ("running", "degraded", "starting")


CRON_MARK = f"# {APP}"


def _crontab_without_us() -> str:
    cur = sh(["crontab", "-l"], timeout=30)
    lines = cur.stdout.splitlines() if cur.returncode == 0 else []
    return "".join(f"{x}\n" for x in lines if not x.endswith(CRON_MARK))


def install_scheduler(hours: float) -> str:
    if os.environ.get("DOCKER_AUTOUPDATE_NO_SCHEDULER"):
        return "scheduler skipped (DOCKER_AUTOUPDATE_NO_SCHEDULER is set)"
    cmd = scheduler_command()
    if SYSTEM == "darwin":
        path_env = os.pathsep.join(dict.fromkeys(
            [str(Path(x).parent) for x in load_state().get("tool_paths", [])] + EXTRA_PATH[:7]))
        plist = _launchd_plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(launchd_plist(cmd, hours, path_env))
        domain = f"gui/{os.getuid()}"
        sh(["launchctl", "bootout", f"{domain}/{LABEL}"], timeout=30)
        r = sh(["launchctl", "bootstrap", domain, str(plist)], timeout=30)
        if r.returncode != 0:
            raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")
        return f"launchd agent {LABEL} (every {hours:g} h)"
    if WINDOWS:
        r = sh(schtasks_create_args(cmd, hours), timeout=60)
        if r.returncode != 0:
            raise SystemExit(f"schtasks failed: {(r.stderr or r.stdout).strip()}")
        return f"Task Scheduler task '{APP}' (every {max(1, round(hours))} h, while you are signed in)"
    if shutil.which("systemctl") and _has_systemd_user():
        service, timer = systemd_units(cmd, hours)
        d = _unit_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{APP}.service").write_text(service)
        (d / f"{APP}.timer").write_text(timer)
        sh(["systemctl", "--user", "daemon-reload"], timeout=60)
        r = sh(["systemctl", "--user", "enable", "--now", f"{APP}.timer"], timeout=60)
        if r.returncode != 0:
            raise SystemExit(f"systemctl failed: {r.stderr.strip()}")
        linger = sh(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], timeout=30)
        note = ""
        if "Linger=yes" not in linger.stdout:
            note = ("; note: it only runs while you are logged in. To run it at boot: "
                    "sudo loginctl enable-linger $USER")
        return f"systemd user timer {APP}.timer (every {hours:g} h){note}"
    if shutil.which("crontab"):
        step = max(1, min(23, round(hours)))
        line = (f"17 */{step} * * * {' '.join(shlex.quote(c) for c in cmd)} "
                f">/dev/null 2>&1 {CRON_MARK}\n")
        r = subprocess.run(["crontab", "-"], input=_crontab_without_us() + line, text=True)
        if r.returncode != 0:
            raise SystemExit("crontab install failed")
        return f"cron job (every {step} h)"
    raise SystemExit("no supported scheduler found (launchd, systemd, cron or Task Scheduler)")


def remove_scheduler() -> None:
    if os.environ.get("DOCKER_AUTOUPDATE_NO_SCHEDULER"):
        return
    if SYSTEM == "darwin":
        sh(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=30)
        _launchd_plist_path().unlink(missing_ok=True)
    elif WINDOWS:
        sh(["schtasks", "/Delete", "/F", "/TN", APP], timeout=60)
    else:
        if shutil.which("systemctl"):
            sh(["systemctl", "--user", "disable", "--now", f"{APP}.timer"], timeout=60)
            for n in (f"{APP}.service", f"{APP}.timer"):
                (_unit_dir() / n).unlink(missing_ok=True)
            sh(["systemctl", "--user", "daemon-reload"], timeout=60)
        if shutil.which("crontab"):
            cur = sh(["crontab", "-l"], timeout=30)
            if cur.returncode == 0 and CRON_MARK in cur.stdout:
                subprocess.run(["crontab", "-"], input=_crontab_without_us(), text=True)


# ------------------------------------------------------------------ install / uninstall / status
def cmd_install(project_arg: str | None, hours: float, run_now: bool,
                repo: str | None = None) -> int:
    project = Path(project_arg or os.getcwd()).resolve()
    top = git(project, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise SystemExit(f"{project} is not inside a git repository")
    project = Path(top.stdout.strip()).resolve()
    remote = git(project, "remote", "get-url", "origin").stdout.strip()
    repo = repo or github_repo_from_remote(remote)
    if not repo:
        raise SystemExit(f"origin ({remote or 'none'}) is not a GitHub repository; "
                         "pass --repo OWNER/NAME")
    if not any((project / n).exists() for n in COMPOSE_NAMES):
        log(f"{project} has no compose file at its root; add `compose_files` to {CONFIG_NAME}",
            "warn")
    if SYSTEM == "darwin" and any(project.is_relative_to(Path.home() / d)
                                  for d in ("Documents", "Desktop", "Downloads")):
        log("macOS blocks background jobs from ~/Documents, ~/Desktop and ~/Downloads unless "
            "Python has Full Disk Access; a clone elsewhere (e.g. ~/docker/<name>) avoids that",
            "warn")

    # install (or refresh) the tool itself, then let it fetch a newer release if there is one
    app_dir().mkdir(parents=True, exist_ok=True)
    target, me = installed_script(), Path(__file__).resolve()
    have = parse_version(_version_of(target.read_text(encoding="utf-8")) or "0") \
        if target.exists() else (0, 0, 0)
    if me != target.resolve() and parse_version(__version__) >= have:
        shutil.copyfile(me, target)
    self_update()

    st = load_state()
    st["interval_hours"] = hours
    st["python"] = base_python()
    st["tool_paths"] = [p for p in (shutil.which("docker"), shutil.which("git")) if p]
    entry = st["projects"].setdefault(str(project), {})
    entry["repo"] = repo
    save_state(st)
    log(f"registered {project} (follows releases of {repo})")
    log(f"scheduled: {install_scheduler(hours)}")
    if run_now:
        return subprocess.call([sys.executable, str(target), "run", "--project", str(project)])
    return 0


def cmd_uninstall(project_arg: str | None, everything: bool) -> int:
    st = load_state()
    if not everything:
        project = Path(project_arg or os.getcwd()).resolve()
        top = git(project, "rev-parse", "--show-toplevel")
        key = str(Path(top.stdout.strip()).resolve()) if top.returncode == 0 else str(project)
        if st["projects"].pop(key, None) is None:
            log(f"{key} was not registered", "warn")
        else:
            log(f"unregistered {key}")
    if everything or not st["projects"]:
        remove_scheduler()
        shutil.rmtree(app_dir(), ignore_errors=True)
        log(f"removed the scheduler and {app_dir()}")
    else:
        save_state(st)
    return 0


def cmd_status() -> int:
    st = load_state()
    have = installed_script()
    v = _version_of(have.read_text(encoding="utf-8")) if have.exists() else None
    print(f"{APP} {v or '(not installed)'} in {app_dir()}; checks every {st['interval_hours']:g} h")
    if not st["projects"]:
        print("no projects registered")
    for path, p in st["projects"].items():
        print(f"- {path}\n    repo: {p.get('repo')}\n    deployed: {p.get('deployed') or '-'}"
              f"\n    last check: {p.get('last_check') or '-'}: {p.get('last_result') or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP, description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"{APP} {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="register a project and schedule checks")
    i.add_argument("--project", help="project folder (default: current directory)")
    i.add_argument("--every", type=float, default=6.0, metavar="HOURS",
                   help="how often to check (default 6)")
    i.add_argument("--no-run", action="store_true", help="don't check right away")
    i.add_argument("--repo", metavar="OWNER/NAME",
                   help="GitHub repo to follow (default: from the origin remote)")
    u = sub.add_parser("uninstall", help="unregister a project (removes everything when none are left)")
    g = u.add_mutually_exclusive_group()
    g.add_argument("--project")
    g.add_argument("--all", action="store_true", help="remove every project and the tool")
    r = sub.add_parser("run", help="check now (this is what the scheduler runs)")
    r.add_argument("--project")
    r.add_argument("--no-self-update", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="show registered projects and their last result")
    a = ap.parse_args(argv)
    if a.cmd == "install":
        return cmd_install(a.project, a.every, not a.no_run, a.repo)
    if a.cmd == "uninstall":
        return cmd_uninstall(a.project, a.all)
    if a.cmd == "run":
        return cmd_run(a.project, not a.no_self_update)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())

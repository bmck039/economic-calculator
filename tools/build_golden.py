"""Exact-results check for the dataset builder: rebuild offline from the download cache and
compare every table with a previous rebuild.

    python -m tools.build_golden save    build into econ_data/golden_build
    python -m tools.build_golden check   build again and compare

(--cache: the download cache to build from, default econ_data/.cache.)

The network is switched off (a request that is not cached gets HTTP 404, as for data not yet
published) and the cache never expires, so two builds from the same cache
must give identical tables; a refactor of the builder that is meant to change nothing must
pass `check`. Run both on the same day (some sources stop at the current year).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"fetch_log.csv"}  # request timings, not data


def build(out: Path, cache: Path) -> int:
    def offline(*_a, **_k):  # a cache miss is "not published" (HTTP 404), like a month
        r = requests.Response()  # the Census has not released: sources skip it
        r.status_code, r._content, r.url = (
            404,
            b"offline rebuild",
            str(_a[2] if len(_a) > 2 else ""),
        )
        return r

    requests.Session.request = offline
    sys.path.insert(0, str(ROOT))
    import build_econ_dataset

    if out.exists():
        shutil.rmtree(out)
    return build_econ_dataset.main(["--out", str(out), "--cache", str(cache), "--ttl-days", "1e9"])


def compare(a: Path, b: Path) -> list[str]:
    diffs = []
    names = {p.name for p in a.glob("*.csv")} | {p.name for p in b.glob("*.csv")}
    for name in sorted(names - SKIP):
        pa, pb = a / name, b / name
        if not (pa.exists() and pb.exists()):
            diffs.append(f"{name}: only in {'saved' if pa.exists() else 'new'} build")
            continue
        if pa.read_bytes() == pb.read_bytes():
            continue
        x = pd.read_csv(pa, low_memory=False)
        y = pd.read_csv(pb, low_memory=False)
        if list(x.columns) != list(y.columns) or len(x) != len(y):
            diffs.append(
                f"{name}: shape/columns {x.shape} {list(x.columns)[:8]} vs "
                f"{y.shape} {list(y.columns)[:8]}"
            )
            continue
        try:
            pd.testing.assert_frame_equal(x, y, check_exact=False, rtol=1e-12, atol=1e-12)
        except AssertionError as e:
            diffs.append(f"{name}: {str(e).splitlines()[0:4]}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["save", "check"])
    ap.add_argument("--cache", type=Path, default=ROOT / "econ_data" / ".cache")
    ap.add_argument("--dir", type=Path, default=ROOT / "econ_data" / "golden_build")
    a = ap.parse_args(argv)
    saved = a.dir
    out = saved if a.cmd == "save" else saved.with_name(saved.name + "_new")
    rc = build(out, a.cache)
    print(f"builder exit status {rc}")
    if a.cmd == "save":
        return 0
    diffs = compare(saved, out)
    for d in diffs:
        print("DIFF", d)
    n = len({p.name for p in saved.glob("*.csv")} - SKIP)
    print(f"{n - len(diffs)}/{n} tables identical")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())

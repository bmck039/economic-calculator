"""Exact-results check for refactoring: run a fixed set of plans and compare every number.

    python -m tools.golden save     record today's results (econ_data/golden/)
    python -m tools.golden check    re-run and compare with what was recorded

A refactor that is meant to change nothing must pass `check` with no differences. The
plans cover every country, US states, the career model, job loss, owners, US citizens
abroad, the backtest's history path and the calibration. Results depend on the built data,
so they are kept next to it (not in git); `save` again after a deliberate model change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from econsim.engine import simulate, to_display
from econsim.model import Earner, JobLoss, Plan

E2 = (Earner(30), Earner(30))


def plans(ds) -> dict[str, tuple[Plan, str | None]]:
    """name -> (plan, US state)."""
    out: dict[str, tuple[Plan, str | None]] = {}
    for iso, _ in ds.countries():  # every country, default couple
        out[f"{iso}:default"] = (Plan(iso, E2), None)
        out[f"{iso}:young"] = (Plan(iso, (Earner(23), Earner(23))), None)
    for st in ("CA", "MS", "TX", "NY", "MA"):
        out[f"USA-{st}:default"] = (Plan("USA", E2, state=st), st)
    v = {
        "p75_degrees": dict(
            earners=(
                Earner(28, "p75", education="tertiary"),
                Earner(28, "p75", education="tertiary"),
            )
        ),
        "p25_no_kids": dict(
            earners=(Earner(35, "p25"), Earner(35, "p25")), child_ages=(None, None)
        ),
        "one_earner": dict(earners=(Earner(30), Earner(30, works=False))),
        "job_loss": dict(job_loss=JobLoss(earner=0, age=36, months=12)),
        "protect": dict(job_loss=JobLoss(earner=1, age=33, months=9), protect_savings=True),
        "own_home": dict(earners=(Earner(45), Earner(45)), own_home=True),
        "early_retire": dict(retirement_age=60, spend_down=False),
        "no_career": dict(career_model=False, job_market="now"),
        "outlooks": dict(
            wage_outlook="trend",
            home_outlook="trend",
            rate_outlook="history",
            price_outlook="flat",
        ),
        "gift_inherit": dict(family_gift=40_000.0, inheritance=150_000.0),
        "rent_own": dict(rent=1500.0, buy_house_age=None),
        "glide_off": dict(glide_path=False, equity_share=0.8),
    }
    for iso in ("USA", "DEU", "GBR", "NLD", "FRA", "CHE", "IRL", "DNK", "CAN", "HUN"):
        for name, kw in v.items():
            kw = dict(kw)
            ear = kw.pop("earners", E2)
            out[f"{iso}:{name}"] = (Plan(iso, ear, **kw), None)
    for iso in ("DEU", "FRA", "GBR", "ESP"):  # US citizens abroad
        out[f"{iso}:us_citizens"] = (Plan(iso, E2, us_citizens=True, us_employer=True), None)
    return out


def run(ds) -> dict[str, pd.DataFrame]:
    us = ds.us_params()
    res = {}
    for name, (p, st) in plans(ds).items():
        cd = ds.country(p.country, st)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = simulate(p, cd, us)
        d = r.years.copy()
        # what the app shows: real, intl$ - covers to_display
        disp = to_display(r, cd, True, "intl$")
        for c in ("net_worth", "saving", "net_income"):
            d[f"display:{c}"] = disp[c]
        d.attrs = {}
        meta = {
            "events": r.events,
            "warnings": r.warnings,
            "summary": {
                k: v
                for k, v in r.summary.items()
                if isinstance(v, (int, float, str, bool, type(None)))
            },
        }
        res[name] = (d, json.loads(json.dumps(meta, default=float)))
    # the backtest's history path (start in a past year, real prices since)
    from econsim.backtest import Backtest

    bt = Backtest(ds)
    sims = bt.run(60, workers=4)
    res["backtest:sims"] = (sims.sort_values(list(sims.columns)).reset_index(drop=True), {})
    # the calibration population
    from econsim.calibrate import population_saving

    for iso in ("USA", "DEU", "ITA"):
        cd = ds.country(iso)
        r = population_saving(cd, us if iso == "USA" else None, spending_fit=False)
        res[f"calibrate:{iso}"] = (
            pd.DataFrame(r["cells"], columns=["age", "pct", "kids", "rate"]).astype(str),
            {"rate": r["rate"]},
        )
    return res


def _fingerprint(ds) -> str:
    h = hashlib.sha256()
    for f in sorted(Path(ds.dir).glob("*.csv")):
        if f.name.startswith("fetch_log"):
            continue
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def compare(a: pd.DataFrame, b: pd.DataFrame, rtol: float = 1e-9) -> list[str]:
    diffs = []
    if list(a.columns) != list(b.columns):
        diffs.append(f"columns differ: {sorted(set(a.columns) ^ set(b.columns))}")
    if len(a) != len(b):
        return diffs + [f"rows {len(a)} vs {len(b)}"]
    for c in [c for c in a.columns if c in b.columns]:
        x, y = a[c], b[c]
        if pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y):
            xa, ya = x.to_numpy(float), y.to_numpy(float)
            bad = ~(np.isclose(xa, ya, rtol=rtol, atol=1e-6) | (np.isnan(xa) & np.isnan(ya)))
            if bad.any():
                i = int(np.argmax(bad))
                diffs.append(f"{c}: {int(bad.sum())} values, e.g. row {i}: {xa[i]} vs {ya[i]}")
        elif not x.astype(str).equals(y.astype(str)):
            diffs.append(f"{c}: text differs")
    return diffs


def main() -> int:
    from econsim.data import DataStore

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["save", "check"])
    a = ap.parse_args()
    ds = DataStore()
    out = Path(ds.dir) / "golden"
    res = run(ds)
    if a.action == "save":
        out.mkdir(exist_ok=True)
        for name, (d, meta) in res.items():
            d.to_pickle(out / f"{name.replace(':', '__')}.pkl")
            (out / f"{name.replace(':', '__')}.json").write_text(json.dumps(meta, indent=1))
        (out / "FINGERPRINT").write_text(_fingerprint(ds))
        print(f"saved {len(res)} results to {out}")
        return 0
    if (out / "FINGERPRINT").read_text() != _fingerprint(ds):
        print("the data changed since the results were saved - save again first")
        return 2
    n_bad = 0
    for name, (d, meta) in res.items():
        base = out / name.replace(":", "__")
        old = pd.read_pickle(f"{base}.pkl")
        diffs = compare(old, d)
        old_meta = json.loads(Path(f"{base}.json").read_text())
        if old_meta != meta:
            diffs.append("events / warnings / summary differ")
        if diffs:
            n_bad += 1
            print(f"{name}:\n  " + "\n  ".join(diffs[:8]))
    print(f"{len(res) - n_bad} of {len(res)} identical")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())

"""How much a country's households save, in the model and in the national accounts.

A synthetic population of couples - heads aged 25-64, pay percentiles 10-90, with and
without children - is simulated for its first two years under today's conditions; its
aggregate saving rate (saving incl. mortgage principal and employer pension
contributions, over disposable income incl. those contributions) is compared with the
national accounts' household net saving rate. The same model and behavioural
parameters run everywhere (fitted in econsim.backtest on what US couples actually
accumulated); each country's spending is its own survey basket raised to its own national
accounts totals. This checks that the countries are comparable: a model that saved far
more in one country than its households do would make that country look better.

    python -m econsim.calibrate [--countries USA DEU ...]
"""
from __future__ import annotations

import argparse
import warnings

import numpy as np
import pandas as pd

from .engine import simulate
from .model import Earner, Plan

AGES = (28, 38, 48, 58)
PCTS = (("p25", 0.3), ("median", 0.4), ("p75", 0.3))
KIDS = ((None, None), "two")      # no children / two (born at 30 and 33)


def population_saving(cd, us=None, **kw) -> dict:
    num = den = 0.0
    rows = []
    for a in AGES:
        for pct, w in PCTS:
            if pct not in cd.earnings and pct != "median":
                continue
            for kids in KIDS:
                ca = (None, None) if kids != "two" else (30, 33)
                p = Plan(cd.iso3, (Earner(a, pct), Earner(a, pct)), horizon_age=a + 2,
                         child_ages=ca, marriage_age=a, buy_house_age=None, **kw)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    y = simulate(p, cd, us).years
                r = y.iloc[1]
                s = r["saving"] + r.get("employer_pension", 0.0)
                inc = r["net_income"] + r.get("employer_pension", 0.0)
                num += w * s
                den += w * inc
                rows.append((a, pct, kids, s / inc if inc else np.nan))
    return {"rate": 100 * num / den if den else float("nan"), "cells": rows}


def fit_level(cd, national: float, us=None, lo: float = 0.7, hi: float = 2.0,
              tol: float = 0.25, **kw) -> tuple[float, float]:
    """Spending level (x the survey basket raised to national accounts) at which the
    population saves the national rate: secant on [lo, hi]."""
    def f(k):
        return population_saving(cd, us, spending_level=k, **kw)["rate"] - national
    a, b = 1.0, 1.4
    fa, fb = f(a), f(b)
    for _ in range(6):
        if abs(fb) < tol or fb == fa:
            break
        c = min(max(b - fb * (b - a) / (fb - fa), lo), hi)
        a, fa, b, fb = b, fb, c, f(c)
    return round(b, 3), round(fb + national, 2)


def _fit_one(args):
    data_dir, iso, kw = args
    from .data import DataStore
    global _GROSS_DIR
    _GROSS_DIR = data_dir
    warnings.simplefilter("ignore")
    ds = DataStore(data_dir)
    nat = target(ds, iso)
    if nat is None:
        return None
    cd = ds.country(iso)
    k, got = fit_level(cd, nat, ds.us_params() if iso == "USA" else None,
                       spending_fit=False, **kw)
    return {"iso3": iso, "spending_level": k, "saving_model": got, "saving_national": nat,
            "source": "fitted: a population of couples (25-64, pay percentiles 25-75, with "
            "and without children) saves the national accounts' household net saving rate "
            "(OECD; else Eurostat gross less the usual depreciation; at least 0%); shared "
            "behaviour from the US backtest"}


def write_levels(data_dir, workers: int = 16, **kw) -> pd.DataFrame:
    """spending_level.csv for every country (run after the data build). kw: plan settings
    (for trying other behaviour)."""
    from concurrent.futures import ProcessPoolExecutor
    from pathlib import Path
    from .data import DataStore
    Path(f"{data_dir}/spending_level.csv").unlink(missing_ok=True)   # fit from scratch
    isos = [i for i, _ in DataStore(data_dir).countries()]
    with ProcessPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(_fit_one, [(data_dir, i, kw) for i in isos]) if r]
    t = pd.DataFrame(rows)
    # each country as fitted (the 0% floor already tames national accounts that miss
    # informal income); countries without any saving rate take the median
    med = float(np.exp(np.log(t["spending_level"]).median()))
    t["level_fit"] = t["spending_level"]
    have = set(t["iso3"])
    extra = [{"iso3": i, "spending_level": round(med, 3), "level_fit": None,
              "saving_model": None, "saving_national": None,
              "source": "median of the countries with a national household saving rate"}
             for i in isos if i not in have]
    t = pd.concat([t, pd.DataFrame(extra)], ignore_index=True)
    t.to_csv(f"{data_dir}/spending_level.csv", index=False)
    return t


SAVING_FLOOR = 0.0   # national accounts where households "dissave" (Greece, Bulgaria,
#                      Romania, Latvia) mostly miss informal and cross-border income: a
#                      working-age couple cannot run down savings for decades


def target(ds, iso3: str):
    """Household net saving rate (%), latest year: OECD national accounts; else Eurostat's
    gross rate less the median gross-net gap of countries with both. Floored at 0."""
    snap = ds.snap[ds.snap["iso3"] == iso3] if not ds.snap.empty else ds.snap
    v = None
    if "household_net_saving_rate" in snap and snap["household_net_saving_rate"].notna().any():
        v = float(snap["household_net_saving_rate"].iloc[0])
    else:
        g = _gross().get("CHE" if iso3 == "LIE" else iso3)
        gap = _gross().get("_gap")
        if g is not None and gap is not None:
            v = g - gap
    return None if v is None else max(v, SAVING_FLOOR)


_GROSS: dict = {}


def _gross() -> dict:
    """Eurostat gross household saving rates (econ_long) and the median gross - net gap."""
    if not _GROSS:
        from pathlib import Path
        from .data import DATA_DIR
        p = Path(_GROSS_DIR or DATA_DIR) / "econ_long.parquet"
        if p.exists():
            L = pd.read_parquet(p, columns=["iso3", "measure", "year", "value"])
            g = L[L["measure"] == "saving_rate_gross"].dropna().sort_values("year") \
                .groupby("iso3").tail(1).set_index("iso3")["value"]
            n = L[L["measure"] == "saving_rate_net"].dropna().sort_values("year") \
                .groupby("iso3").tail(1).set_index("iso3")["value"]
            both = [i for i in g.index if i in n.index]
            _GROSS.update({k: float(v) for k, v in g.items()})
            if both:
                _GROSS["_gap"] = float(np.median([g[i] - n[i] for i in both]))
        _GROSS.setdefault("_none", 0)
    return _GROSS


_GROSS_DIR = None


def main():
    from .data import DataStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="*")
    a = ap.parse_args()
    ds = DataStore()
    us = ds.us_params()
    out = []
    for iso, name in ds.countries():
        if a.countries and iso not in a.countries:
            continue
        cd = ds.country(iso)
        r = population_saving(cd, us if iso == "USA" else None)
        out.append({"iso3": iso, "model": round(r["rate"], 1), "national": target(ds, iso)})
        print(out[-1], flush=True)
    pd.DataFrame(out).to_csv("saving_check.csv", index=False)


if __name__ == "__main__":
    main()

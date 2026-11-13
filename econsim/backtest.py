"""Backtest the simulation against what actually happened.

Start real households in a past survey (every couple in the Fed's SCF with the head aged
25-60, waves 2004-2022): their own ages, degree, children, wages, savings, retirement
accounts, home, mortgage and student debt. Run them through what actually happened
(econsim.history: prices of each cost, rents, home prices, mortgage rates, market returns,
deposit rates, unemployment) with their own generation's pay path (CPS ASEC: full-time pay
of their birth years and degree, year by year). Then compare with the SAME birth years in
the later waves: median wealth (financial assets + home equity - student debt) and the
share who own, by start wave x birth cohort (5 years) x degree.

Nothing here is compared with people of other generations: each cell is one cohort, as
the model predicted it from its starting point, against the same cohort surveyed later.

    python -m econsim.backtest [--sample 400] [--out report.md] [--calibrate]
"""
from __future__ import annotations

import argparse
import warnings
from concurrent.futures import ProcessPoolExecutor
from typing import Optional

import numpy as np
import pandas as pd

from .career import RANK_PERSISTENCE
from .engine import simulate
from .history import from_table
from .model import Earner, Plan

WAVES = (2004, 2007, 2010, 2013, 2016, 2019, 2022)
AGES = (25, 60)
EDU = {1: "below_upper_secondary", 2: "upper_secondary", 3: "upper_secondary",
       4: "bachelor"}
GROUP = {1: "no_degree", 2: "no_degree", 3: "no_degree", 4: "degree"}
SPLIT = 0.6                 # head's share of the couple's wages
MIN_CELL = 40               # households in a cell (model and survey) to compare it


def wealth(fin, ret, home, mort, sl):
    """Financial assets (incl. retirement accounts) + home equity - student debt."""
    return fin + ret + home - mort - sl


def survey_wealth(d: pd.DataFrame) -> pd.Series:
    return wealth(d["fin"], 0.0, d["houses"], d["mrthel"], d["edn_inst"])


def _wq(x, w, q: float) -> float:
    x, w = np.asarray(x, float), np.asarray(w, float)
    if len(x) == 0:
        return float("nan")
    o = np.argsort(x)
    c = np.cumsum(w[o])
    return float(x[o][min(np.searchsorted(c, c[-1] * q), len(x) - 1)])


def _wmed(x, w) -> float:
    return _wq(x, w, 0.5)


def _by_earnings(wealth, wage, w) -> tuple[float, float]:
    """Median wealth of the bottom and top third by wages (weighted ranks)."""
    wealth, wage, w = (np.asarray(v, float) for v in (wealth, wage, w))
    o = np.argsort(wage, kind="stable")
    c = np.cumsum(w[o]) / w.sum()
    lo, hi = o[c <= 1 / 3], o[c > 2 / 3]
    return _wmed(wealth[lo], w[lo]), _wmed(wealth[hi], w[hi])


def _kids_share(d: pd.DataFrame, k: int) -> dict:
    """age -> share of couples with at least k children (that wave, 7-year window)."""
    out = {}
    for a in range(20, 70):
        m = d["age"].between(a - 3, a + 3)
        out[a] = float(np.average(d.loc[m, "kids"] >= k, weights=d.loc[m, "wgt"])) \
            if m.any() else 0.0
    return out


class Backtest:
    def __init__(self, ds, iso3: str = "USA"):
        self.ds = ds
        self.cd = ds.country(iso3)
        self.us = ds.us_params()
        self.hist = pd.read_csv(ds.dir / "us_history.csv")
        self.scf = pd.read_csv(ds.dir / "scf_households.csv")
        self.coh = self.cd.cohort
        self.last = int(self.scf["year"].max())
        self._pg: dict = {}

    # ------------------------------------------------------------ cohort pay
    def pay_growth(self, group: str, age: int, y0: int, years: int) -> np.ndarray:
        """Nominal wage income of this cohort's couples (same birth years +-2, same degree
        group), wave by wave in the SCF (incl. those who stopped working), year t vs 0;
        log-linear between waves (every 3 years)."""
        key = (group, age, y0)
        if key in self._pg:
            g = self._pg[key]
            return g[:years]
        s = self.scf
        b = y0 - age
        pts = {}
        for w in sorted(s["year"].unique()):
            if w < y0:
                continue
            m = s[(s["year"] == w) & (s["edcl"].map(GROUP) == group) &
                  (s["year"] - s["age"]).between(b - 2, b + 2)]
            if len(m) >= 30:
                pts[w] = _wmed(m["wageinc"], m["wgt"])
        ys = sorted(pts)
        out = []
        for t in range(years):
            y = y0 + t
            lo = max([w for w in ys if w <= y], default=None)
            hi = min([w for w in ys if w >= y], default=None)
            if lo is None or pts.get(lo, 0) <= 0:
                out.append(out[-1] if out else 1.0)
                continue
            if hi is None or hi == lo or pts[hi] <= 0:
                v = pts[lo]
            else:
                v = np.exp(np.log(pts[lo]) + (np.log(pts[hi]) - np.log(pts[lo])) *
                           (y - lo) / (hi - lo))
            out.append(v)
        g = np.array(out) / out[0] if out and out[0] > 0 else np.ones(years)
        # SCF incomes are for the year before the survey: shift by one year
        g = np.concatenate([[1.0], g[:-1] * (g[1] if len(g) > 1 else 1.0)]) \
            if len(g) > 1 else g
        self._pg[key] = g
        return g[:years]

    def households(self, wave: int, sample: Optional[int], seed: int = 0) -> pd.DataFrame:
        d = self.scf[(self.scf["year"] == wave) & self.scf["age"].between(*AGES)].copy()
        if sample and len(d) > sample:     # a random subset, keeping the survey weights
            d = d.sample(sample, random_state=seed)
        return d

    def plan_for(self, r, wave: int, ks1: dict, ks2: dict, cell_med: float,
                 rng: np.random.Generator, **kw) -> tuple[Plan, int]:
        a = int(r.age)
        edu = EDU[int(r.edcl)]
        years = self.last - wave + 1
        g = self.pay_growth(GROUP[int(r.edcl)], a, wave, years)
        # this couple's wages (last calendar year, moved to the wave year by the cohort's
        # growth) - their distance from the typical couple fades as ranks do
        w0 = float(r.wageinc) * (g[1] if len(g) > 1 else 1.0) ** 0.5
        dev = np.log(max(w0, 1.0) / cell_med) if w0 > 0 and cell_med > 0 else 0.0
        path = [cell_med * g[t] * np.exp(dev * RANK_PERSISTENCE ** t) if w0 > 0 else 0.0
                for t in range(years)]
        # children: those they have (ages spread), and births at the rate couples of
        # their age have them
        kids = int(min(r.kids, 2))
        births = [a - min(max(a - 28 - 3 * i, 0), 17) for i in range(kids)]
        for k in (1, 2):
            if kids < k and a < 40:
                s_now = ks1 if k == 1 else ks2
                p = max(s_now.get(a + 5, 0) - s_now.get(a, 0), 0) / \
                    max(1 - s_now.get(a, 0), 1e-6)
                if rng.random() < p:
                    births.append(a + 2 + 3 * (k - 1 - kids))
                    kids += 1
        births = sorted(births)[:2] + [None] * (2 - min(len(births), 2))
        own = float(r.houses) > 0
        e = [Earner(a, education=edu, pay_path=tuple(x * s for x in path))
             for s in (SPLIT, 1 - SPLIT)]
        p = Plan(self.cd.iso3, (e[0], e[1]), start_year=wave, horizon_age=a + years - 1,
                 marriage_age=a, child_ages=tuple(births), buy_house_age=a,
                 own_home=own, home_value_now=float(r.houses) if own else None,
                 mortgage_now=float(r.mrthel) if own else None,
                 starting_savings=max(float(r.fin) - float(r.retqliq), 0.0),
                 starting_retirement=float(r.retqliq), student_loan=float(r.edn_inst),
                 student_loan_rate=6.8, career_model=False, cohort_effects="off",
                 typical_student_debt=False, **kw)
        return p, years

    def run(self, sample: Optional[int] = 400, waves=WAVES, workers: int = 16,
            **kw) -> pd.DataFrame:
        """One row per household x later wave: model wealth and ownership."""
        jobs = []
        for wave in waves:
            if wave >= self.last:
                continue
            d = self.households(wave, sample, seed=wave)
            all_w = self.scf[self.scf["year"] == wave]
            ks1, ks2 = _kids_share(all_w, 1), _kids_share(all_w, 2)
            rng = np.random.default_rng(wave)
            for r in d.itertuples(index=False):
                m = all_w[(all_w["age"].between(r.age - 3, r.age + 3)) &
                          (all_w["edcl"].map(GROUP) == GROUP[int(r.edcl)]) &
                          (all_w["wageinc"] > 0)]
                med = _wmed(m["wageinc"], m["wgt"]) if len(m) else float(r.wageinc)
                p, years = self.plan_for(r, wave, ks1, ks2, med, rng, **kw)
                jobs.append((p, wave, int(r.age), int(r.edcl), float(r.wgt)))
        if workers and workers > 1:
            with ProcessPoolExecutor(workers, initializer=_init,
                                     initargs=(self.ds.dir,)) as ex:
                res = list(ex.map(_one, jobs, chunksize=8))
        else:
            _init(self.ds.dir)
            res = [_one(j) for j in jobs]
        return pd.DataFrame([x for rr in res for x in rr])

    # ------------------------------------------------------------ compare
    def compare(self, sims: pd.DataFrame, min_model: int = MIN_CELL) -> pd.DataFrame:
        """Cells: start wave x 5-year birth cohort x degree group, at each later wave."""
        s = self.scf.copy()
        s["birth"] = s["year"] - s["age"]
        s["group"] = s["edcl"].map(GROUP)
        s["w"] = survey_wealth(s)
        sims = sims.copy()
        sims["birth"] = sims["wave"] - sims["age0"]
        out = []
        for (w0, w1, grp), g in sims.groupby(["wave", "year", "group"]):
            for b0 in range(int(g["birth"].min()) // 5 * 5, int(g["birth"].max()) + 1, 5):
                gm = g[g["birth"].between(b0, b0 + 4)]
                sv = s[(s["year"] == w1) & (s["group"] == grp) & s["birth"].between(b0, b0 + 4)]
                if len(gm) < min_model or len(sv) < MIN_CELL:
                    continue
                inc = _wmed(sv.loc[sv["wageinc"] > 0, "wageinc"],
                            sv.loc[sv["wageinc"] > 0, "wgt"])
                out.append({"wave": w0, "year": w1, "h": w1 - w0, "group": grp, "income": inc,
                            "birth": b0, "age": w1 - b0 - 2, "n_model": len(gm),
                            "n_survey": len(sv),
                            "wealth_model": _wmed(gm["wealth"], gm["wgt"]),
                            "wealth_survey": _wmed(sv["w"], sv["wgt"]),
                            "fin_model": _wmed(gm["fin"], gm["wgt"]),
                            "fin_survey": _wmed(sv["fin"], sv["wgt"]),
                            "heq_model": _wmed(gm["home_eq"], gm["wgt"]),
                            "heq_survey": _wmed(sv["houses"] - sv["mrthel"], sv["wgt"]),
                            "own_model": float(np.average(gm["owned"], weights=gm["wgt"])),
                            "own_survey": float(np.average(sv["houses"] > 0,
                                                           weights=sv["wgt"])),
                            "p25_model": _wq(gm["wealth"], gm["wgt"], 0.25),
                            "p25_survey": _wq(sv["w"], sv["wgt"], 0.25),
                            "p75_model": _wq(gm["wealth"], gm["wgt"], 0.75),
                            "p75_survey": _wq(sv["w"], sv["wgt"], 0.75),
                            **dict(zip(("low_model", "high_model"),
                                       _by_earnings(gm["wealth"], gm["wage"], gm["wgt"]))),
                            **dict(zip(("low_survey", "high_survey"),
                                       _by_earnings(sv["w"], sv["wageinc"], sv["wgt"])))})
        c = pd.DataFrame(out)
        if not c.empty:
            # in years of the cohort's median wages (log errors explode near zero)
            c["wealth_err"] = (c["wealth_model"] - c["wealth_survey"]) / c["income"]
            c["own_err"] = c["own_model"] - c["own_survey"]
            for k in ("p25", "p75", "low", "high"):
                c[f"{k}_err"] = (c[f"{k}_model"] - c[f"{k}_survey"]) / c["income"]
        return c


_DS = None


def _init(data_dir):
    global _DS
    from .data import DataStore
    warnings.simplefilter("ignore")
    _DS = DataStore(data_dir)
    _DS._bt_cd = _DS.country("USA")
    _DS._bt_hist = pd.read_csv(_DS.dir / "us_history.csv")
    _DS._bt_H = {}


def _one(job):
    p, wave, age0, edcl, wgt = job
    cd = _DS._bt_cd
    if wave not in _DS._bt_H:
        _DS._bt_H[wave] = from_table(_DS._bt_hist, wave, cd.data_year)
    H = _DS._bt_H[wave]
    try:
        r = simulate(p, cd, _DS.us_params(), history=H)
    except Exception:   # noqa: BLE001 - a failed household is reported as missing
        return []
    y = r.years
    out = []
    for w in range(wave + 3, int(y["year"].max()) + 1, 3):
        row = y[y["year"] == w]
        if row.empty:
            continue
        x = row.iloc[0]
        out.append({"wave": wave, "year": w, "age0": age0, "group": GROUP[edcl],
                    "wgt": wgt, "owned": bool(x["owned"]), "wage": x["gross"],
                    "fin": x["liquid"] + x["retirement_account"],
                    "home_eq": x["home_value"] - x["mortgage_balance"],
                    "wealth": wealth(x["liquid"], x["retirement_account"], x["home_value"],
                                     x["mortgage_balance"], x["student_loan_balance"])})
    return out


def score(c: pd.DataFrame) -> dict:
    """Errors over all cells: wealth in years of the cohort's median wages, ownership
    in percentage points; weighted by the smaller cell size."""
    w = np.minimum(c["n_model"], c["n_survey"])
    return {"cells": len(c),
            "wealth_bias": float(np.average(c["wealth_err"], weights=w)),
            "wealth_mae": float(np.average(c["wealth_err"].abs(), weights=w)),
            "own_bias_pp": float(100 * np.average(c["own_err"], weights=w)),
            "own_mae_pp": float(100 * np.average(c["own_err"].abs(), weights=w)),
            **{f"{k}_bias": float(np.average(c[f"{k}_err"], weights=w))
               for k in ("p25", "p75", "low", "high") if f"{k}_err" in c},
            # the spread within cells: 25th and 75th percentiles, bottom and top third
            # by wages
            "spread_mae": float(np.mean([np.average(c[f"{k}_err"].abs(), weights=w)
                                         for k in ("p25", "p75", "low", "high")
                                         if f"{k}_err" in c] or [np.nan]))}


def main():
    from .data import DataStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--out")
    a = ap.parse_args()
    bt = Backtest(DataStore())
    c = bt.compare(bt.run(a.sample))
    print(score(c))
    print(c.groupby(["group", "h"])[["wealth_err", "own_err"]].mean().round(3))
    if a.out:
        c.to_csv(a.out, index=False)


if __name__ == "__main__":
    main()

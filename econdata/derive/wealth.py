"""Derived wealth tables: starting wealth, cohorts, portfolios."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from econdata.config import COUNTRIES
from econdata.derive.context import Ctx
from econdata.derive.history import hfcs_tables
from econdata.sources.cohorts import COHORT_HALF_WINDOW, SCF_HALF_WINDOW
from econdata.sources.saving import LIFE_INSURANCE_IS_PENSION
from econsim.backtest_eu import Tables

SES_BAND_AGE = {"Y_LT30": 25.0, "Y30-39": 34.5, "Y40-49": 44.5, "Y50-59": 54.5, "Y_GE60": 62.0}


START_STATS = (
    "renter_fin",
    "renter_retirement",
    "owner_fin",
    "owner_retirement",
    "owner_home",
    "owner_mortgage",
    "student_debt_share",
    "student_debt_holders",
    "networth",
    "fin_assets",
    "scf_own",
)


def start_wealth_hfcs(long: pd.DataFrame) -> pd.DataFrame:
    """What households of each age hold in each euro-area country (latest HFCS wave), in
    the layout of the SCF rows: medians by age of the reference person, interpolated
    between the 10-year age bands; owners' and renters' financial assets from the median
    of holders and the owner / renter ratio; owners' debt = median debt of debtors x the
    share of owners in debt. Non-euro currencies at that year's exchange rates."""
    t = hfcs_tables(long)
    if t.empty:
        return pd.DataFrame()
    rows = []
    for iso in sorted(set(t["iso3"]) - {"EA"}):
        if iso not in COUNTRIES:
            continue
        T = Tables(t, iso)
        if not T.waves:
            continue
        w = T.waves[-1]
        yr = int(np.floor(T.field[w]))
        k = 1000.0  # own currency (hfcs_tables)
        r_own, r_rent = T.tenure_ratio(w)
        src = f"ECB HFCS {w} (fieldwork {yr})"
        for a in range(20, 86):
            own = T.at("B3", w, a, log=False) / 100
            fin = T.at("C4", w, a) * k
            debt = T.at("E6", w, a) * k
            dsh = T.at("E5", w, a, log=False) / 100
            vals = {
                "networth": T.at("A3", w, a) * k,
                "scf_own": own,
                "owner_home": T.at("B5", w, a) * k,
                "owner_mortgage": (
                    debt * min(1.0, dsh / own) if own > 0 and debt == debt and dsh == dsh else 0.0
                ),
                "owner_fin": fin * r_own,
                "renter_fin": fin * r_rent,
                "owner_retirement": 0.0,
                "renter_retirement": 0.0,
            }
            rows += [
                {
                    "iso3": iso,
                    "statistic": st,
                    "education": "all",
                    "household_type": "all",
                    "age": a,
                    "year": yr,
                    "value": v,
                    "n": None,
                    "source": src,
                }
                for st, v in vals.items()
                if v == v
            ]
    return pd.DataFrame(rows)


def derive_start_wealth(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """start_wealth.csv: what families of each age hold now - US: latest SCF, by
    education and couple / all families; euro area: latest HFCS wave, all households -
    the default starting point of a simulation at that age."""
    c = long[
        (long["measure"] == "cohort")
        & long["statistic"].isin(START_STATS)
        & (long["source"].astype(str).str.contains("Consumer Finances"))
    ]
    c = c.dropna(subset=["value", "year"])
    c = c[(c["year"] == c["year"].max()) & c["age_band"].astype(str).str.fullmatch(r"\d+")]
    us = pd.DataFrame(
        {
            "iso3": c["iso3"],
            "statistic": c["statistic"],
            "education": c["category"],
            "household_type": c["household_type"].fillna("all"),
            "age": c["age_band"].astype(int),
            "year": c["year"].astype(int),
            "value": c["value"],
            "n": pd.to_numeric(
                c["method"].astype(str).str.extract(r"n=(\d+)")[0], errors="coerce"
            ),
            "source": c["source"],
        }
    )
    return pd.concat([us, start_wealth_hfcs(long)], ignore_index=True)


def derive_cohort(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """cohort.csv: each statistic by age, education and survey year with that year's
    all-ages reference (25-64; SES: all ages), for following cohorts over time.
    US: CPS ASEC (every year since 2003) and the SCF (every 3 years since 1989).
    Europe: LFS unemployment by 5-year band (since 2000) and SES median earnings by age
    band (2006-2022, every 4 years)."""
    c = long[long["measure"] == "cohort"].dropna(subset=["value", "year"]).copy()
    c["household_type"] = c["household_type"].fillna("all").astype(str)
    c = c[
        (c["household_type"] == "all")
        & ~c["statistic"].isin(
            [s for s in START_STATS if s not in ("networth", "fin_assets", "scf_own")]
        )
    ]
    out = []
    for (iso, src), g in c.groupby(["iso3", "source"]):
        ref = g[g["age_band"] == "ref"].set_index(["statistic", "year"])["value"]
        d = g[g["age_band"] != "ref"].copy()
        ab = d["age_band"].astype(str)
        single = ab.str.fullmatch(r"\d+")
        d["age"] = np.where(
            single,
            pd.to_numeric(ab.where(single), errors="coerce"),
            ab.str.extract(r"(\d+)-(\d+)").astype(float).mean(axis=1),
        )
        d["ref"] = [ref.get((s, y), np.nan) for s, y in zip(d["statistic"], d["year"])]
        d["n"] = pd.to_numeric(d["method"].astype(str).str.extract(r"n=(\d+)")[0], errors="coerce")
        out.append(
            pd.DataFrame(
                {
                    "iso3": iso,
                    "measure": d["statistic"],
                    "education": d["category"],
                    "household_type": d["household_type"],
                    "age": d["age"],
                    "year": d["year"].astype(int),
                    "value": d["value"],
                    "ref": d["ref"],
                    "n": d["n"],
                    "source": src,
                    "span": np.where(
                        single,
                        2 * (SCF_HALF_WINDOW if "Consumer" in src else COHORT_HALF_WINDOW) + 1,
                        10,
                    ),
                }
            )
        )
    s = long[
        (long["measure"] == "earnings_by_age")
        & (long["source"] == "Eurostat SES")
        & (long["statistic"] == "median")
        & (long["unit"] == "EUR/month")
    ]
    s = s.dropna(subset=["value", "year"])
    if not s.empty:
        tot = s[s["age_band"] == "TOTAL"].set_index(["iso3", "year"])["value"]
        b = s[s["age_band"].isin(SES_BAND_AGE)]
        out.append(
            pd.DataFrame(
                {
                    "iso3": b["iso3"],
                    "measure": "earn_ses",
                    "education": "all",
                    "household_type": "all",
                    "age": b["age_band"].map(SES_BAND_AGE),
                    "year": b["year"].astype(int),
                    "value": b["value"],
                    "ref": [tot.get((i, y), np.nan) for i, y in zip(b["iso3"], b["year"])],
                    "n": np.nan,
                    "source": "Eurostat SES (median, full and part time)",
                    "span": 20,
                }
            )
        )
    if not out:
        return pd.DataFrame()
    t = pd.concat(out, ignore_index=True).dropna(subset=["value", "ref", "age"])
    return t.sort_values(
        ["iso3", "measure", "education", "household_type", "year", "age"]
    ).reset_index(drop=True)


def _logit(p):
    p = min(max(float(p), 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def _expit(z):
    return 1 / (1 + math.exp(-z))


def derive_portfolio(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """How households hold their free financial savings (deposits, bonds, listed shares,
    funds, life insurance; not pensions or unlisted business equity), latest year with
    deposits and funds reported. Funds count as shares, life insurance as bonds."""
    a = long[long["measure"] == "household_assets"].dropna(subset=["year", "value"])
    rows = []
    for iso in COUNTRIES:
        src = "CHE" if iso == "LIE" else iso
        g = a[a["iso3"] == src]
        best = None
        for yr in sorted(g["year"].unique(), reverse=True):
            p = g[g["year"] == yr].groupby("statistic")["value"].sum(min_count=1)
            if pd.notna(p.get("deposits")) and pd.notna(p.get("fund_shares")):
                best = (int(yr), p)
                break
        if best is None:
            rows.append({"iso3": iso})
            continue
        yr, p = best
        v = lambda k: float(p.get(k)) if pd.notna(p.get(k)) else 0.0  # noqa: E731
        life = 0.0 if iso in LIFE_INSURANCE_IS_PENSION else v("life_insurance")
        dep, shares = v("deposits"), v("listed_shares") + v("fund_shares")
        fin = dep + v("debt_securities") + shares + life
        tr, oth = v("deposits_transferable"), v("deposits_other")
        rows.append(
            {
                "iso3": iso,
                "year": yr,
                "deposit_share": round(dep / fin, 3),
                "equity_share": round(shares / (fin - dep), 3) if fin > dep else None,
                "overnight_share": round(tr / (tr + oth), 3) if tr + oth > 0 else None,
                "source": "OECD financial balance sheets, households and NPISH"
                + (" (Swiss data)" if iso == "LIE" else ""),
                "estimated": False,
            }
        )
    df = pd.DataFrame(rows)
    have = df.dropna(subset=["deposit_share"])
    eu = have[have["iso3"].map(lambda c: COUNTRIES[c][3] == "EU")]
    for c in ("deposit_share", "equity_share", "overnight_share"):
        miss = df[c].isna() if c in df else pd.Series(True, index=df.index)
        df.loc[miss, c] = round(float(eu[c].median()), 3)
    gap = df["source"].isna()
    df.loc[gap, "source"] = "EU median (no national balance sheet with deposits and funds)"
    df.loc[gap, "estimated"] = True
    df.loc[gap, "year"] = int(eu["year"].max()) if not eu.empty else None
    # typical households (bottom 70% by wealth): ECB DWA, US SCF; elsewhere the sector's
    # shares moved by the median gap (log-odds) between typical and sector where both exist
    t = long[long["measure"] == "household_assets_typical"].dropna(subset=["value"])
    typ = {(r.iso3, r.statistic): (float(r.value), str(r.source)) for r in t.itertuples()}
    for c in ("deposit_share", "equity_share"):
        gaps = [
            _logit(typ[(i, c + "_typical")][0]) - _logit(x)
            for i, x in zip(df["iso3"], df[c])
            if (i, c + "_typical") in typ and x == x
        ]
        shift = float(np.median(gaps)) if gaps else 0.0
        vals, srcs = [], []
        for i, x in zip(df["iso3"], df[c]):
            k = ("CHE" if i == "LIE" else i, c + "_typical")
            if k in typ:
                vals.append(round(typ[k][0], 3))
                srcs.append(typ[k][1])
            else:
                vals.append(round(_expit(_logit(x) + shift), 3) if x == x else None)
                srcs.append(
                    f"household sector (OECD) moved by the median gap of typical "
                    f"households in {len(gaps)} countries ({shift:+.2f} log-odds)"
                )
        df[c + "_typical"] = vals
        df[c + "_typical_source"] = srcs
    return df

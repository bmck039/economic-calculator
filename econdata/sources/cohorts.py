"""Cohorts: the same birth years followed across many years of surveys."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime

import numpy as np
import pandas as pd

from econdata.clients import EU_GEOS, _annual, eurostat, fred, statcan
from econdata.config import NOTES, ROOT
from econdata.fetch import CachedFetcher, FetchError, is_json, is_zip
from econdata.sources.history import US_HISTORY
from econdata.sources.labour import EDU_EUROSTAT, _age, _wq
from econdata.sources.spending import cpi_series
from econdata.tidy import frame

log = logging.getLogger("econ")


# Each statistic is computed by single year of age (a 5-year window centred on it, so
# small cells are not noise) and education, for every survey year, plus the all-ages
# reference (25-64) of that year. econsim/cohort.py turns these into how each cohort
# compares with the cohorts before it at the same age, and how much of that gap has
# lasted historically.
ASEC = "https://api.census.gov/data/{y}/cps/asec/mar"


ASEC_VARS = (
    "A_AGE,A_HGA,MARSUPWT,PEARNVAL,WKSWORK,HRSWK,A_LFSR,A_ENRLW,A_FTPT,H_TENURE,HTOTVAL,A_EXPRRP"
)


ASEC_FIRST = 2003  # earliest March supplement on the Census API


COHORT_AGES = (18, 79)


# A_HGA: 31-38 below high school, 39 high school, 40 some college, 41-42 associate,
# 43 bachelor's, 44-46 master's / professional / doctorate
ASEC_EDU = {
    "all": None,
    "degree": (43, 46),
    "no_degree": (31, 40),
    "below_upper_secondary": (31, 38),
    "upper_secondary": (39, 40),
    "short_tertiary": (41, 42),
    "bachelor": (43, 43),
    "master_plus": (44, 46),
}


COHORT_HALF_WINDOW = 2


def _by_age(age: np.ndarray, *cols) -> dict:
    """age -> tuple of column arrays for that age (for fast moving windows)."""
    o = np.argsort(age, kind="stable")
    a = age[o]
    out = {}
    for v in np.unique(a):
        lo, hi = np.searchsorted(a, v), np.searchsorted(a, v, side="right")
        out[int(v)] = tuple(c[o][lo:hi] for c in cols)
    return out


def _window(groups: dict, a: int, h: int) -> tuple:
    parts = [groups[x] for x in range(a - h, a + h + 1) if x in groups]
    if not parts:
        return None
    return tuple(np.concatenate([p[i] for p in parts]) for i in range(len(parts[0])))


def _wmed(x: np.ndarray, w: np.ndarray) -> float:
    if len(x) == 0 or w.sum() <= 0:
        return float("nan")
    o = np.argsort(x)
    c = np.cumsum(w[o])
    return float(x[o][min(np.searchsorted(c, c[-1] / 2), len(x) - 1)])


def _wtlog(x: np.ndarray, w: np.ndarray, lo: float = 0.1, hi: float = 0.9) -> float:
    """Geometric mean of the middle 80% (weighted): a typical level that, unlike the
    median, does not jump between the round numbers people report."""
    m = x > 0
    x, w = x[m], w[m]
    if len(x) < 10 or w.sum() <= 0:
        return float("nan")
    o = np.argsort(x)
    x, w = x[o], w[o]
    c = np.cumsum(w) / w.sum()
    k = (c >= lo) & (c <= hi)
    if k.sum() < 5:
        return float("nan")
    return float(np.exp(np.average(np.log(x[k]), weights=w[k])))


def _wshare(m: np.ndarray, w: np.ndarray) -> float:
    return float((w * m).sum() / w.sum()) if w.sum() > 0 else float("nan")


def _asec_year(f: CachedFetcher, y: int) -> pd.DataFrame | None:
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("CPS ASEC needs CENSUS_API_KEY")
    try:
        d = census_table(
            f.get(
                ASEC.format(y=y),
                {"get": ASEC_VARS, "A_AGE": "18:79", "key": key},
                label=f"census:asec:{y}",
                validate=is_json,
                timeout=300,
            )
        )
    except FetchError as e:
        if any(s in str(e) for s in ("HTTP 404", "HTTP 204", "unexpected response")):
            return None
        raise
    return d


def census_table(body: bytes) -> pd.DataFrame:
    """Census API JSON (an array of arrays of strings) -> numeric DataFrame, through the
    C CSV parser (json.loads + to_numeric takes ~10x longer on 100k-row files)."""
    t = body.replace(b"[", b"").replace(b"]", b"").replace(b'"', b"")
    d = pd.read_csv(
        io.BytesIO(t), na_values=["null"], skipinitialspace=True, skip_blank_lines=True
    )
    d = d.loc[:, [c for c in d.columns if not str(c).startswith("Unnamed")]]
    d.columns = [re.sub(r"\.\d+$", "", str(c)) for c in d.columns]
    return d.loc[:, ~d.columns.duplicated()].apply(pd.to_numeric, errors="coerce")


def cohort_us_asec(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """CPS Annual Social and Economic Supplement (March), every year since 2003, by single
    year of age and education: typical earnings of earners (last calendar year; geometric
    mean of the middle 80%), the same for full-time year-round workers, share working full
    time year-round, the unemployment rate in March, householders' home-ownership rate and
    household income."""
    rows = []
    for y in range(ASEC_FIRST, datetime.now().year + 1):
        d = _asec_year(f, y)
        if d is None or d.empty:
            continue
        age = d["A_AGE"].to_numpy(int)
        w = d["MARSUPWT"].fillna(0).to_numpy(float)
        earn = d["PEARNVAL"].fillna(0).to_numpy(float)
        ft_student = ((d["A_ENRLW"] == 1) & (d["A_FTPT"] == 1)).to_numpy()
        ftyr = ((d["WKSWORK"] >= 50) & (d["HRSWK"] >= 35)).to_numpy()
        lf = d["A_LFSR"].isin([1, 2, 3, 4]).to_numpy()
        une = d["A_LFSR"].isin([3, 4]).to_numpy()
        head = d["A_EXPRRP"].isin([1, 2]).to_numpy()
        own = (d["H_TENURE"] == 1).to_numpy()
        hinc = d["HTOTVAL"].fillna(0).to_numpy(float)
        hga = d["A_HGA"].to_numpy(int)

        def stats(ix: np.ndarray) -> dict:
            ww = w[ix]
            ok = ~ft_student[ix]
            e, ft = earn[ix], ftyr[ix]
            m_e, m_ft = ok & (e > 0), ft & (e > 0)
            m_lf, m_h = lf[ix] & ok, head[ix]
            return {
                "earn": (_wtlog(e[m_e], ww[m_e]), int(m_e.sum())),
                "earn_ftyr": (_wtlog(e[m_ft], ww[m_ft]), int(m_ft.sum())),
                "ftyr_share": (_wshare(ft[ok], ww[ok]), int(ok.sum())),
                "unemp": (_wshare(une[ix][m_lf], ww[m_lf]), int(m_lf.sum())),
                "own": (_wshare(own[ix][m_h], ww[m_h]), int(m_h.sum())),
                "hh_income": (_wtlog(hinc[ix][m_h], ww[m_h]), int(m_h.sum())),
            }

        ref = stats(np.flatnonzero((age >= 25) & (age <= 64)))
        for k, (v, n) in ref.items():
            rows.append(
                {
                    "period": str(y),
                    "statistic": k,
                    "age_band": "ref",
                    "category": "all",
                    "value": v,
                    "method": f"n={n}",
                }
            )
        idx = np.arange(len(d))
        for edu, rng in ASEC_EDU.items():
            m = np.ones(len(d), bool) if rng is None else (hga >= rng[0]) & (hga <= rng[1])
            g = _by_age(age[m], idx[m])
            for a in range(COHORT_AGES[0], COHORT_AGES[1] + 1):
                win = _window(g, a, COHORT_HALF_WINDOW)
                if win is None:
                    continue
                for k, (v, n) in stats(win[0]).items():
                    if n >= 30 and v == v:
                        rows.append(
                            {
                                "period": str(y),
                                "statistic": k,
                                "age_band": str(a),
                                "category": edu,
                                "value": v,
                                "method": f"n={n}",
                            }
                        )
    if not rows:
        raise FetchError("CPS ASEC: no years fetched")
    out = pd.DataFrame(rows).assign(iso3="USA", series="CPS ASEC")
    return frame(
        out,
        measure="cohort",
        frequency="A",
        unit="USD / share",
        source="Census CPS ASEC microdata (Census API)",
        comparability_note="Earnings refer to the previous calendar year; status "
        "(unemployment, home ownership) to March. 5-year age windows.",
    )


SCF_ZIP = "https://www.federalreserve.gov/econres/files/scfp{y}excel.zip"


SCF_COLS = [
    "YY1",
    "XX1",
    "Y1",
    "WGT",
    "AGE",
    "EDCL",
    "MARRIED",
    "KIDS",
    "LF",
    "NETWORTH",
    "FIN",
    "LIQ",
    "RETQLIQ",
    "HOUSES",
    "MRTHEL",
    "EDN_INST",
    "INCOME",
    "WAGEINC",
    "EQUITY",
    "DEBT",
    "HDEBT",
]


SCF_EDU = {"all": None, "degree": (4, 4), "no_degree": (1, 3)}


SCF_HALF_WINDOW = 3


def cohort_us_scf(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Survey of Consumer Finances summary extract (every 3 years since 1989), by age of
    the family head and education: median net worth, financial assets, home ownership;
    and for the latest survey, what families of each age hold now (renters' financial
    assets and retirement accounts, owners' home value and mortgage, student debt)."""
    rows = []
    first, last = 1989, datetime.now().year
    years = [y for y in range(first, last + 1, 3)]
    got = []
    for y in years:
        try:
            b = f.get(SCF_ZIP.format(y=y), label=f"fed:scf:{y}", validate=is_zip, timeout=300)
        except FetchError as e:
            if "HTTP 404" in str(e):
                continue
            raise
        z = zipfile.ZipFile(io.BytesIO(b))
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        d = pd.read_csv(z.open(name), usecols=lambda c: c.upper() in SCF_COLS)
        d.columns = [c.upper() for c in d.columns]
        got.append((y, d))
    if not got:
        raise FetchError("SCF: no surveys fetched")
    latest_y = got[-1][0]
    for y, d in got:
        age = d["AGE"].to_numpy(int)
        w = d["WGT"].to_numpy(float)
        edcl = d["EDCL"].to_numpy(int)
        couple = (d["MARRIED"] == 1).to_numpy()
        nw, fin = d["NETWORTH"].to_numpy(float), d["FIN"].to_numpy(float)
        houses = d["HOUSES"].to_numpy(float)
        owner = houses > 0
        ret = d["RETQLIQ"].to_numpy(float) if "RETQLIQ" in d else np.zeros(len(d))
        mort = d["MRTHEL"].to_numpy(float) if "MRTHEL" in d else np.zeros(len(d))
        edn = d["EDN_INST"].to_numpy(float) if "EDN_INST" in d else np.zeros(len(d))

        def stats(ix: np.ndarray, detail: bool) -> dict:
            ww = w[ix]
            n = len(ix) // 5
            o = {
                "networth": (_wmed(nw[ix], ww), n),
                "fin_assets": (_wmed(fin[ix], ww), n),
                "scf_own": (_wshare(owner[ix], ww), n),
            }
            if detail:
                r, ow = ix[~owner[ix]], ix[owner[ix]]
                dh = ix[edn[ix] > 0]
                o.update(
                    {
                        "renter_fin": (_wmed(fin[r], w[r]), len(r) // 5),
                        "renter_retirement": (_wmed(ret[r], w[r]), len(r) // 5),
                        "owner_fin": (_wmed(fin[ow], w[ow]), len(ow) // 5),
                        "owner_retirement": (_wmed(ret[ow], w[ow]), len(ow) // 5),
                        "owner_home": (_wmed(houses[ow], w[ow]), len(ow) // 5),
                        "owner_mortgage": (_wmed(mort[ow], w[ow]), len(ow) // 5),
                        "student_debt_share": (_wshare(edn[ix] > 0, ww), n),
                        "student_debt_holders": (_wmed(edn[dh], w[dh]), len(dh) // 5),
                    }
                )
            return o

        for k, (v, n) in stats(np.flatnonzero((age >= 25) & (age <= 64)), False).items():
            rows.append(
                {
                    "period": str(y),
                    "statistic": k,
                    "age_band": "ref",
                    "category": "all",
                    "household_type": "all",
                    "value": v,
                    "method": f"n={n}",
                }
            )
        idx = np.arange(len(d))
        for edu, rng in SCF_EDU.items():
            em = np.ones(len(d), bool) if rng is None else (edcl >= rng[0]) & (edcl <= rng[1])
            for ht, hm in (("all", np.ones(len(d), bool)), ("couple", couple)):
                if ht == "couple" and y != latest_y:
                    continue
                m = em & hm
                g = _by_age(age[m], idx[m])
                for a in range(20, 86):
                    win = _window(g, a, SCF_HALF_WINDOW)
                    if win is None:
                        continue
                    for k, (v, n) in stats(win[0], y == latest_y).items():
                        if n >= 30 and v == v:
                            rows.append(
                                {
                                    "period": str(y),
                                    "statistic": k,
                                    "age_band": str(a),
                                    "category": edu,
                                    "household_type": ht,
                                    "value": v,
                                    "method": f"n={n}",
                                }
                            )
    # how typical families (bottom 70% by net worth) hold their savings, latest survey
    y, d = got[-1]
    if {"LIQ", "EQUITY", "FIN", "NETWORTH"} <= set(d.columns):
        m = d[d["NETWORTH"] <= _wq(d["NETWORTH"], d["WGT"], [70])[0]]
        w = m["WGT"]
        dep = float((m["LIQ"] * w).sum() / (m["FIN"] * w).sum())
        eq = float((m["EQUITY"] * w).sum() / ((m["FIN"] - m["LIQ"]) * w).sum())
        typ = pd.DataFrame(
            [
                {"period": str(y), "statistic": s_, "value": v_, "method": f"SCF {y}"}
                for s_, v_ in (("deposit_share_typical", dep), ("equity_share_typical", eq))
            ]
        )
        typ = frame(
            typ.assign(iso3="USA", series="SCF bottom 70%"),
            measure="household_assets_typical",
            frequency="3-yearly",
            unit="share",
            source="Federal Reserve SCF (families in the bottom 70% by net worth)",
        )
    else:
        typ = None
    out = pd.DataFrame(rows).assign(iso3="USA", series="SCF summary extract")
    res = frame(
        out,
        measure="cohort",
        frequency="3-yearly",
        unit="USD (survey year)",
        source="Federal Reserve Survey of Consumer Finances",
        comparability_note="Families by age of the head; 7-year age windows; 5 "
        "implicates, weighted. Net worth incl. homes and retirement accounts.",
    )
    return pd.concat([res, typ], ignore_index=True) if typ is not None else res


def _scf_waves(f: CachedFetcher):
    for y in range(1989, datetime.now().year + 1, 3):
        try:
            b = f.get(SCF_ZIP.format(y=y), label=f"fed:scf:{y}", validate=is_zip, timeout=300)
        except FetchError as e:
            if "HTTP 404" in str(e):
                continue
            raise
        z = zipfile.ZipFile(io.BytesIO(b))
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        d = pd.read_csv(z.open(name), usecols=lambda c: c.upper() in SCF_COLS)
        d.columns = [c.upper() for c in d.columns]
        yield y, d


SCF_DOLLARS = [
    "NETWORTH",
    "FIN",
    "LIQ",
    "RETQLIQ",
    "HOUSES",
    "MRTHEL",
    "EDN_INST",
    "INCOME",
    "WAGEINC",
    "EQUITY",
    "DEBT",
    "HDEBT",
]


def scf_nominal(f: CachedFetcher):
    """The SCF summary extracts state every wave in dollars of the latest survey (the Fed
    adjusts them with the CPI-U-RS): yield each wave back in its own year's dollars."""
    waves = list(_scf_waves(f))
    if not waves:
        return
    base = max(y for y, _ in waves)
    cpi = _annual(cpi_series(f, US_HISTORY["cpi"]))
    for y, d in waves:
        if y in cpi.index and base in cpi.index:
            k = float(cpi[y] / cpi[base])
            d = d.copy()
            for c in SCF_DOLLARS:
                if c in d:
                    d[c] = d[c] * k
        else:
            log.warning("SCF %s: no CPI to state it in its own dollars", y)
        yield y, d


def scf_households(f: CachedFetcher) -> pd.DataFrame:
    """scf_households.csv: every SCF couple (married or living together) with the head
    aged 22-64, first implicate, every wave since 1989, in that year's dollars - the
    starting points for backtesting the simulation against what the same birth years held
    in later waves."""
    out = []
    for y, d in scf_nominal(f):
        hid = "YY1" if "YY1" in d else "XX1" if "XX1" in d else None
        d = d.drop_duplicates(hid) if hid else d  # first implicate
        d = d[(d["MARRIED"] == 1) & d["AGE"].between(22, 64)]
        keep = {
            c.lower(): d[c]
            for c in SCF_COLS
            if c in d and c not in ("YY1", "XX1", "Y1", "MARRIED")
        }
        out.append(pd.DataFrame(keep).assign(year=y))
    if not out:
        raise FetchError("SCF: no waves")
    t = pd.concat(out, ignore_index=True)
    t["wgt"] = t["wgt"] * 5  # the first implicate alone carries a fifth
    return t


HFCS_BANDS = {
    "16-34": (16, 34),
    "35-44": (35, 44),
    "45-54": (45, 54),
    "55-64": (55, 64),
    "65-74": (65, 74),
    "75+": (75, 200),
}


def scf_hfcs_tables(f: CachedFetcher) -> pd.DataFrame:
    """scf_tables.csv: the SCF (all families, all implicates) in the layout of the ECB's
    HFCS statistical tables - the same statistics by the same age bands of the reference
    person, USD thousands - so US and euro-area households are measured alike, and the
    backtest built on those tables (econsim.backtest_eu) can be checked on the US, where
    the households themselves are known too."""

    def wq(x, w, q=0.5):
        o = np.argsort(x)
        c = np.cumsum(w[o])
        return float(x[o][np.searchsorted(c, q * c[-1])]) if len(x) else np.nan

    rows = []
    for y, d in scf_nominal(f):
        if y < 1995:
            continue
        w = d["WGT"].to_numpy(float)
        nw, inc = d["NETWORTH"].to_numpy(float), d["INCOME"].to_numpy(float)
        houses, fin, debt = (d[c].to_numpy(float) for c in ("HOUSES", "FIN", "DEBT"))
        for band, (lo, hi) in HFCS_BANDS.items():
            m = d["AGE"].between(lo, hi).to_numpy()
            if m.sum() < 50:
                continue
            ww = w[m]
            own, has_fin, has_debt = houses[m] > 0, fin[m] > 0, debt[m] > 0
            for t, ind, v in (
                ("A3", "networth_median", wq(nw[m], ww) / 1000),
                ("B3", "own_hmr_pct", 100 * np.average(own, weights=ww)),
                ("B5", "realestate_median", wq(houses[m][own], ww[own]) / 1000),
                ("C4", "financial_median", wq(fin[m][has_fin], ww[has_fin]) / 1000),
                ("E5", "has_debt_pct", 100 * np.average(has_debt, weights=ww)),
                ("E6", "debt_median", wq(debt[m][has_debt], ww[has_debt]) / 1000),
                ("I1", "income_median", wq(inc[m], ww) / 1000),
            ):
                rows.append(
                    {
                        "wave": y,
                        "iso3": "USA",
                        "table": t,
                        "indicator": ind,
                        "group": "Age of RP",
                        "row": band,
                        "value": round(v, 2),
                        "se": None,
                    }
                )
        ten = {
            "Owners - outright": (houses > 0) & (d["MRTHEL"].to_numpy(float) <= 0),
            "Owners - with mortgage": (houses > 0) & (d["MRTHEL"].to_numpy(float) > 0),
            "Renters / other": houses <= 0,
        }
        allf = fin > 0
        for row, m in list(ten.items()) + [("All", np.ones(len(d), bool))]:
            mm = m & allf
            rows.append(
                {
                    "wave": y,
                    "iso3": "USA",
                    "table": "C4",
                    "indicator": "financial_median",
                    "group": "Housing status" if row != "All" else "Total population",
                    "row": row,
                    "value": round(wq(fin[mm], w[mm]) / 1000, 2),
                    "se": None,
                }
            )
        # fieldwork May-December of the survey year; income: the calendar year before
        rows += [
            {
                "wave": y,
                "iso3": "USA",
                "table": "REF",
                "indicator": ind,
                "group": "",
                "row": "",
                "value": v,
                "se": None,
            }
            for ind, v in (("fieldwork_mid", y + 0.7), ("income_year_mid", y - 0.5))
        ]
    if not rows:
        raise FetchError("SCF: no waves")
    return pd.DataFrame(rows)


HFCS_TABLES = ROOT / "reference" / "hfcs_tables.csv"


HFCS_MONEY = ("A3", "B5", "C4", "E6", "I1")  # tables in EUR thousands


def part_time_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Part-time share of employment by age and education (Eurostat LFS lfsa_epgaed: all
    part-timers, as the US CPS < 35 usual hours, not only those who want more hours)."""
    df = eurostat(
        f,
        "lfsa_epgaed",
        required={"unit": ["THS_PER"], "sex": ["T"]},
        optional={
            "age": ["Y15-24", "Y25-49", "Y50-64"],
            "worktime": ["TOTAL", "PT"],
            "isced11": list(EDU_EUROSTAT),
        },
        since=max(since or 2015, 2015),
        geo=EU_GEOS,
    )
    df = df[df["isced11"].isin(EDU_EUROSTAT)]
    w = df.pivot_table(
        index=["iso3", "time", "age", "isced11"],
        columns="worktime",
        values="value",
        aggfunc="first",
    ).reset_index()
    w = w.dropna(subset=["TOTAL", "PT"])
    w = w[w["TOTAL"] > 0]
    out = pd.DataFrame(
        {
            "iso3": w["iso3"],
            "period": w["time"],
            "value": 100 * w["PT"] / w["TOTAL"],
            "age_band": w["age"].map(_age),
            "category": w["isced11"].map(EDU_EUROSTAT),
            "series": "lfsa_epgaed",
        }
    )
    return frame(
        out,
        measure="part_time_share",
        statistic="share",
        frequency="A",
        unit="% of employment",
        source="Eurostat LFS (lfsa_epgaed)",
    )


def cohort_eu_lfs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Eurostat LFS unemployment by 5-year age band and education, every year since
    2000 (for following cohorts; the latest year feeds the job market too)."""
    bands = [f"Y{a}-{a + 4}" for a in range(20, 65, 5)]
    df = eurostat(
        f,
        "lfsa_urgaed",
        required={"unit": ["PC"], "sex": ["T"]},
        optional={"age": bands + ["Y25-64"], "isced11": list(EDU_EUROSTAT)},
        since=2000,
        geo=EU_GEOS,
    )
    df = df[df["isced11"].isin(EDU_EUROSTAT)]
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "age_band": df["age"].map(_age).replace({"25-64": "ref"}),
            "category": df["isced11"].map(EDU_EUROSTAT),
            "statistic": "unemp",
            "series": "lfsa_urgaed",
        }
    )
    out = out[(out["age_band"] != "ref") | (out["category"] == "all")]
    return frame(
        out,
        measure="cohort",
        frequency="A",
        unit="% of labour force",
        source="Eurostat LFS (lfsa_urgaed)",
    )


def vacancies_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # The rate code was renamed JOBRATE -> JVR. The query used to "succeed" with 0 rows.
    df = eurostat(
        f,
        "jvs_q_nace2",
        optional={
            "nace_r2": ["B-S"],
            "indic_em": ["JVR"],
            "s_adj": ["NSA"],
            "sizeclas": ["TOTAL", "GE10"],
        },
        since=since,
        geo=EU_GEOS,
    )
    if "indic_em" in df:
        df = df[df["indic_em"].isin(["JVR", "JOBRATE"])]
    if df.empty:  # rate-only headline table as a fallback
        df = eurostat(
            f,
            "ei_lmjv_q_r2",
            optional={"nace_r2": ["B-S"], "s_adj": ["NSA"], "sizeclas": ["TOTAL", "GE10"]},
            since=since,
            geo=EU_GEOS,
        )
    if df.empty:
        raise FetchError("jvs_q_nace2 / ei_lmjv_q_r2: no job-vacancy-rate rows")
    if "sizeclas" in df:
        df["_r"] = df["sizeclas"].map({"TOTAL": 0, "GE10": 1}).fillna(2)
        df = df.sort_values("_r").drop_duplicates(["geo", "time"])
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "series": "jvs_q_nace2:JVR",
        }
    )
    return frame(
        out,
        measure="job_vacancy_rate",
        statistic="rate",
        frequency="Q",
        unit="%",
        source="Eurostat JVS",
        comparability_note=NOTES["jvr_eu"],
    )


def vacancies_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = fred(f, "JTSJOR")
    out = pd.DataFrame(
        {"iso3": "USA", "period": d["date"].str[:7], "value": d["value"], "series": "JTSJOR"}
    )
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    return frame(
        out,
        measure="job_vacancy_rate",
        statistic="rate",
        frequency="M",
        unit="%",
        source="BLS JOLTS (via FRED)",
        comparability_note=NOTES["jvr_us"],
    )


def vacancies_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = statcan(f, "14100325", "Canada", {"statistic": "job vacancy rate"})
    out = pd.DataFrame(
        {"iso3": "CAN", "period": d["REF_DATE"], "value": d["VALUE"], "series": "14-10-0325-01"}
    )
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    return frame(
        out,
        measure="job_vacancy_rate",
        statistic="rate",
        frequency="Q",
        unit="%",
        source="StatCan JVWS",
        comparability_note=NOTES["jvr_ca"],
    )


ONS_AP2Z = (
    "https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/"
    "employmentandemployeetypes/timeseries/ap2z/lms/data"
)


def vacancies_uk(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    js = json.loads(f.get(ONS_AP2Z, label="ons:AP2Z"))
    pts = js.get("quarters") or js.get("months") or []
    rows = []
    for p in pts:
        try:
            v = float(p["value"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            {
                "iso3": "GBR",
                "period": p.get("date", "").replace(" ", "-"),
                "value": v / (100 + v) * 100,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        raise FetchError("ONS AP2Z: no observations")
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    out["series"] = "ONS AP2Z (converted)"
    return frame(
        out,
        measure="job_vacancy_rate",
        statistic="rate",
        frequency="Q" if js.get("quarters") else "M",
        unit="%",
        source="ONS Vacancy Survey",
        comparability_note=NOTES["jvr_uk"],
    )

"""Job market: unemployment, slack, education, CPS microdata (US), careers."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd

from econdata.clients import EU_GEOS, col_like, eurostat, fred, oecd, oecd_rows, statcan
from econdata.config import COUNTRIES, NOTES
from econdata.fetch import CachedFetcher, FetchError, is_json
from econdata.tidy import frame


def unemployment_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "une_rt_a",
        required={"unit": ["PC_ACT"], "sex": ["T"]},
        optional={"age": ["Y15-24", "Y25-54", "Y25-74", "Y55-74", "Y15-74"]},
        since=since,
        geo=EU_GEOS,
    )
    df = df[df["iso3"] != "GBR"]  # UK from OECD (Eurostat stops 2019)
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "age_band": df["age"],
            "series": "une_rt_a",
        }
    )
    return frame(
        out,
        measure="unemployment_rate",
        statistic="rate",
        frequency="A",
        unit="% of labour force",
        source="Eurostat LFS",
        comparability_note=NOTES["une_eu"],
    )


UNE_DIMS = [
    "REF_AREA",
    "MEASURE",
    "UNIT_MEASURE",
    "TRANSFORMATION",
    "ADJUSTMENT",
    "SEX",
    "AGE",
    "ACTIVITY",
    "FREQ",
]


def annual_from_monthly(m: pd.DataFrame, have: set, keys: list[str]) -> pd.DataFrame:
    """Calendar-year means of monthly rows (period 'YYYY-MM') for (keys, year) groups
    that have no published annual value: the current year so far, and years whose annual
    average was never published (the US skipped October 2025: federal shutdown)."""
    if m.empty:
        return m
    m = m.assign(year=m["period"].str[:4])
    g = m.groupby(keys + ["year"], as_index=False).agg(
        value=("value", "mean"),
        n=("value", "size"),
        first=("period", "min"),
        last=("period", "max"),
    )
    g = g[[tuple(r) not in have for r in g[keys + ["year"]].itertuples(index=False)]]
    return g.assign(
        period=g["year"],
        estimated=g["n"] < 12,
        method=[
            f"mean of {n} monthly values ({a} to {b}), seasonally adjusted"
            for n, a, b in zip(g["n"], g["first"], g["last"])
        ],
    ).drop(columns=["year", "n", "first", "last"])


def unemployment_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(
        f,
        "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M",
        UNE_DIMS,
        {
            "REF_AREA": ["USA", "CAN", "GBR"],
            "MEASURE": ["UNE_LF_M"],
            "UNIT_MEASURE": ["PT_LF_SUB"],
            "TRANSFORMATION": ["_Z"],
            "SEX": ["_T"],
            "AGE": ["Y15T24", "Y_GE25", "Y_GE15"],
            "ACTIVITY": ["_Z"],
            "FREQ": ["A", "M"],
        },
        since,
        version="1.0",
    )
    df["_rank"] = df["ADJUSTMENT"].map({"Y": 0, "N": 1}).fillna(2)
    df = df.sort_values("_rank").drop_duplicates(["REF_AREA", "AGE", "FREQ", "TIME_PERIOD"])
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    a = df[df["FREQ"] == "A"]
    out = pd.DataFrame(
        {
            "iso3": a["REF_AREA"],
            "period": a["TIME_PERIOD"],
            "value": a["OBS_VALUE"],
            "age_band": a["AGE"],
        }
    )
    # monthly -> annual where the annual is missing (US 2025: no October survey) or the
    # year is not over yet; SA months only, so a part year has no seasonal bias
    m = df[(df["FREQ"] == "M") & (df["ADJUSTMENT"] == "Y")]
    m = pd.DataFrame(
        {
            "iso3": m["REF_AREA"],
            "period": m["TIME_PERIOD"].astype(str),
            "value": m["OBS_VALUE"],
            "age_band": m["AGE"],
        }
    ).dropna()
    have = {(r.iso3, r.age_band, str(r.period)[:4]) for r in out.itertuples()}
    out = pd.concat([out, annual_from_monthly(m, have, ["iso3", "age_band"])], ignore_index=True)
    return frame(
        out.assign(series="DF_IALFS_UNE_M"),
        measure="unemployment_rate",
        statistic="rate",
        frequency="A",
        unit="% of labour force",
        source="OECD IALFS",
        comparability_note=NOTES["une_oecd"],
    )


def unemployment_europe_monthly(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Current-year (year-to-date) unemployment for Europe from the monthly series; the
    annual table (une_rt_a) only has complete years."""
    yr = datetime.now().year
    df = eurostat(
        f,
        "une_rt_m",
        required={"unit": ["PC_ACT"], "sex": ["T"], "s_adj": ["SA"]},
        optional={"age": ["TOTAL", "Y_LT25"]},
        since=yr - 1,
        geo=EU_GEOS,
    )
    df = df[df["iso3"] != "GBR"]
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"].astype(str),
            "value": pd.to_numeric(df["value"], errors="coerce"),
            "age_band": df["age"].map({"TOTAL": "Y15-74", "Y_LT25": "Y15-24"}),
        }
    )
    out = out.dropna()
    out = annual_from_monthly(out[out["period"].str[:4] == str(yr)], set(), ["iso3", "age_band"])
    return frame(
        out.assign(series="une_rt_m"),
        measure="unemployment_rate",
        statistic="rate",
        frequency="A",
        unit="% of labour force",
        source="Eurostat LFS (monthly)",
        comparability_note=NOTES["une_eu"],
    )


# ---- unemployment, slack and underemployment by age and education ---------------------
# Education keys used everywhere downstream (ISCED 2011 attainment):
EDU_EUROSTAT = {
    "TOTAL": "all",
    "ED0-2": "below_upper_secondary",
    "ED3_4": "upper_secondary",
    "ED5-8": "tertiary",
}


EDU_OECD = {
    "_T": "all",
    "ISCED11A_0T2": "below_upper_secondary",
    "ISCED11A_3_4": "upper_secondary",
    "ISCED11A_5": "short_tertiary",
    "ISCED11A_6": "bachelor",
    "ISCED11A_7_8": "master_plus",
    "ISCED11A_5T8": "tertiary",
}


def _age(code: str) -> str:
    """'Y15-24' / 'Y25T34' / 'Y_GE15' -> '15-24' / '25-34' / '15+'."""
    s = str(code)
    if m := re.fullmatch(r"Y(\d+)[-T](\d+)", s):
        return f"{m.group(1)}-{m.group(2)}"
    if m := re.fullmatch(r"Y_GE(\d+)", s):
        return f"{m.group(1)}+"
    return s


def unemp_by_education_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "lfsa_urgaed",
        required={"unit": ["PC"], "sex": ["T"]},
        optional={
            "age": ["Y15-24", "Y20-24", "Y25-29", "Y30-34", "Y25-64"],
            "isced11": list(EDU_EUROSTAT),
        },
        since=max(since or 2010, 2010),
        geo=EU_GEOS,
    )
    df = df[df["isced11"].isin(EDU_EUROSTAT)]
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "age_band": df["age"].map(_age),
            "category": df["isced11"].map(EDU_EUROSTAT),
            "series": "lfsa_urgaed",
        }
    )
    return frame(
        out,
        measure="unemployment_by_education",
        statistic="rate",
        frequency="A",
        unit="% of labour force",
        source="Eurostat LFS",
        comparability_note="ILO unemployment rate by highest level of education "
        "completed (ISCED 2011).",
    )


SLACK_STATUS = ["UNE", "UEMP_PT", "SEEK_NAVL", "NSEEK_AVL", "SLACK"]


def slack_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Eurostat labour-market slack (the EU's U-6): unemployed + underemployed part-timers
    + people available but not seeking + seeking but not available, % of the extended
    labour force (labour force + the last two groups)."""
    parts = []
    a = eurostat(
        f,
        "lfsi_sla_a",
        required={"sex": ["T"], "unit": ["PC_ELF", "THS_PER"]},
        optional={"age": ["Y15-24", "Y15-29", "Y25-54", "Y15-74"], "wstatus": SLACK_STATUS},
        since=max(since or 2010, 2010),
        geo=EU_GEOS,
    )
    parts.append(
        pd.DataFrame(
            {
                "iso3": a["iso3"],
                "period": a["time"],
                "value": a["value"],
                "age_band": a["age"].map(_age),
                "category": "all",
                "statistic": a["wstatus"].str.lower(),
                "unit": a["unit"].map(
                    {"PC_ELF": "% of extended labour force", "THS_PER": "thousand persons"}
                ),
                "series": "lfsi_sla_a",
            }
        )
    )
    e = eurostat(
        f,
        "lfsa_sup_edu",
        required={"sex": ["T"], "unit": ["PC_ELF", "THS_PER"]},
        optional={"age": ["Y25-74"], "isced11": list(EDU_EUROSTAT), "wstatus": SLACK_STATUS},
        since=max(since or 2010, 2010),
        geo=EU_GEOS,
    )
    e = e[e["isced11"].isin(EDU_EUROSTAT)]
    parts.append(
        pd.DataFrame(
            {
                "iso3": e["iso3"],
                "period": e["time"],
                "value": e["value"],
                "age_band": e["age"].map(_age),
                "category": e["isced11"].map(EDU_EUROSTAT),
                "statistic": e["wstatus"].str.lower(),
                "unit": e["unit"].map(
                    {"PC_ELF": "% of extended labour force", "THS_PER": "thousand persons"}
                ),
                "series": "lfsa_sup_edu",
            }
        )
    )
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="labour_slack",
        frequency="A",
        source="Eurostat LFS",
        comparability_note="Eurostat labour-market slack: UNE unemployed, UEMP_PT "
        "part-timers who want and are available for more hours, NSEEK_AVL available "
        "but not seeking, SEEK_NAVL seeking but not available.",
    )


def overqualification_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "lfsa_eoqgan",
        required={"sex": ["T"], "unit": ["PC"]},
        optional={"citizen": ["TOTAL"], "age": ["Y20-34", "Y25-34", "Y20-64"]},
        since=max(since or 2010, 2010),
        geo=EU_GEOS,
    )
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "age_band": df["age"].map(_age),
            "category": "tertiary",
            "series": "lfsa_eoqgan",
        }
    )
    return frame(
        out,
        measure="overqualification",
        statistic="rate",
        frequency="A",
        unit="% of employed tertiary graduates",
        source="Eurostat LFS",
        comparability_note="Over-qualification: tertiary graduates employed in "
        "ISCO 4-9 jobs (clerical, service, sales, trades, operators, elementary), "
        "which do not usually require a degree.",
    )


EAG_DIMS = [
    "REF_AREA",
    "SEX",
    "AGE",
    "ATTAINMENT_LEV",
    "EDUCATION_FIELD",
    "MEASURE",
    "INCOME",
    "BIRTH_PLACE",
    "MIGRATION_AGE",
    "EDU_STATUS",
    "LABOUR_FORCE_STATUS",
    "DURATION_UNEMP",
    "UNIT_MEASURE",
    "STATISTICAL_OPERATION",
    "WORK_TIME_ARNGMNT",
    "QUESTIONNAIRE",
    "FREQ",
]


EAG_FLOWS = {  # flow -> (measure, statistic, unit)
    "DF_LSO_NEAC_UNEMP": ("unemployment_by_education", "rate", "% of labour force"),
    "DF_LSO_NEAC_EMP": ("employment_by_education", "rate", "% of population"),
    "DF_LSO_NEAC_DISTR_EA": ("attainment_share", "share", "% of population"),
    "DF_LSO_EARN_REL_UPPER": (
        "relative_earnings",
        "index_upper_secondary_100",
        "upper secondary = 100",
    ),
}


def education_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Education at a Glance: unemployment, employment, attainment shares and relative
    full-time earnings by education level and age (all OECD countries + BG/HR/RO)."""
    parts = []
    for flow, (measure, stat, unit) in EAG_FLOWS.items():
        df = oecd(
            f,
            f"OECD.EDU.IMEP,DSD_EAG_LSO_EA@{flow}",
            EAG_DIMS,
            {
                "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
                "SEX": ["_T"],
                "AGE": ["Y25T34", "Y35T44", "Y45T54", "Y55T64", "Y25T64"],
                "ATTAINMENT_LEV": list(EDU_OECD),
            },
            max(since or 2010, 2010),
        )
        df = df[
            (df.get("STATISTICAL_OPERATION", "OBS") == "OBS")
            & df["ATTAINMENT_LEV"].isin(EDU_OECD)
            & (df["SEX"] == "_T")
        ]
        if "WORK_TIME_ARNGMNT" in df and flow == "DF_LSO_EARN_REL_UPPER":
            df = df[df["WORK_TIME_ARNGMNT"] == "FT"]
        parts.append(
            frame(
                oecd_rows(
                    df,
                    {
                        "age_band": df["AGE"].map(_age),
                        "category": df["ATTAINMENT_LEV"].map(EDU_OECD),
                        "series": flow,
                    },
                ),
                measure=measure,
                statistic=stat,
                unit=unit,
                frequency="A",
                source="OECD Education at a Glance",
                comparability_note="Labour-force survey data by highest attainment (ISCED 2011)."
                + (
                    " Earnings: full-time full-year earners, upper secondary = 100."
                    if measure == "relative_earnings"
                    else ""
                ),
            )
        )
    return pd.concat(parts, ignore_index=True)


INVPT_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "WORKER_STATUS", "FREQ"]


def involuntary_pt_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(
        f,
        "OECD.ELS.SAE,DSD_INVPT_EMP@DF_INVPT_I",
        INVPT_DIMS,
        {
            "REF_AREA": ["USA", "CAN", "GBR"],
            "UNIT_MEASURE": ["PT_LF"],
            "SEX": ["_T"],
            "AGE": ["Y15T24", "Y25T54"],
            "WORKER_STATUS": ["_T"],
        },
        max(since or 2010, 2010),
    )
    out = oecd_rows(
        df,
        {
            "age_band": df["AGE"].map(_age),
            "category": "all",
            "series": "DF_INVPT_I",
        },
    )
    return frame(
        out,
        measure="involuntary_part_time",
        statistic="rate",
        frequency="A",
        unit="% of labour force",
        source="OECD Employment Database",
        comparability_note="Part-timers (<30 usual hours) who could not find a "
        "full-time job (OECD-harmonised definition).",
    )


def slack_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """StatCan supplementary unemployment rates (monthly, unadjusted): R4 = official,
    R6 = + discouraged searchers + waiting group (people who want work and would take it
    but are not counted as unemployed)."""
    df = statcan(
        f,
        "14100077",
        match={"supplementary": ["R4 -", "R6 -"], "gender": "Total", "age": None},
        since=max(since or 2010, 2010),
    )
    agec = col_like(df, "age")
    rc = col_like(df, "supplementary")
    bands = {
        "15 to 24 years": "15-24",
        "25 to 44 years": "25-44",
        "25 to 54 years": "25-54",
        "15 years and over": "15+",
    }
    df = df[df[agec].isin(bands)]
    m = pd.DataFrame(
        {
            "iso3": "CAN",
            "period": df["REF_DATE"].astype(str).str[:7],
            "value": df["VALUE"],
            "age_band": df[agec].map(bands),
            "statistic": df[rc].str[:2].str.lower(),
        }
    ).dropna()
    m = m.assign(year=m["period"].str[:4])
    g = m.groupby(["iso3", "age_band", "statistic", "year"], as_index=False).agg(
        value=("value", "mean"), n=("value", "size"), last=("period", "max")
    )
    g = g.assign(
        period=g["year"],
        estimated=g["n"] < 12,
        category="all",
        method=[f"mean of {n} months (to {b})" for n, b in zip(g["n"], g["last"])],
    )
    return frame(
        g.drop(columns=["year", "n", "last"]).assign(series="14-10-0077-01"),
        measure="labour_slack",
        frequency="A",
        unit="% of labour force",
        source="StatCan LFS",
        comparability_note="StatCan supplementary unemployment rates R4 / R6.",
    )


ONS_LMS = (
    "https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/"
    "employmentandemployeetypes/timeseries/{cdid}/lms/data"
)


def slack_uk(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """UK discouraged workers (ONS LF8B, 16-64) as % of the extended labour force (LF2K
    economically active 16-64 + discouraged). Eurostat stopped UK slack data in 2019."""

    def series(cdid):
        js = json.loads(f.get(ONS_LMS.format(cdid=cdid), label=f"ons:{cdid}", validate=is_json))
        out = {}
        for p in js.get("months") or []:
            try:
                out.setdefault(int(p["year"]), []).append(float(p["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    disc, lf = series("lf8b"), series("lf2k")
    rows = [
        {
            "iso3": "GBR",
            "period": str(y),
            "value": 100 * (sum(d) / len(d)) / (sum(lf[y]) / len(lf[y]) + sum(d) / len(d)),
            "estimated": len(d) < 12,
            "method": f"mean of {len(d)} months",
        }
        for y, d in disc.items()
        if y in lf and y >= max(since or 2010, 2010)
    ]
    if not rows:
        raise FetchError("ONS LF8B/LF2K: no observations")
    return frame(
        pd.DataFrame(rows).assign(series="ONS LF8B"),
        measure="labour_slack",
        statistic="discouraged",
        age_band="16-64",
        category="all",
        frequency="A",
        unit=PC_ELF,
        source="ONS LFS",
        comparability_note="ONS discouraged workers (inactive, want a job, believe "
        "no jobs are available): narrower than US 'marginally attached'.",
    )


CPS_BASIC = "https://api.census.gov/data/{y}/cps/basic/{m}"


CPS_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


CPS_VARS = (
    "PRTAGE,PEEDUCA,PEMLR,PRWKSTAT,PRDISC,PEHRWANT,PEHRAVL,PRMJOCC1,PTIO1OCD,"
    "PWCMPWGT,PESEX,PTERNWA,PWORWGT,PRERELG,HRMIS,HRHHID,HRHHID2,PULINENO,PESCHENR,"
    "PEHRUSL1"
)


_CPS_MEMO: dict = {}


def cps_month(f: CachedFetcher, y: int, i: int) -> pd.DataFrame | None:
    """One month of CPS basic microdata (ages 16-64), or None if not released / not
    collected (October 2025: federal shutdown). i = month index 0-11."""
    if (y, i) in _CPS_MEMO:
        return _CPS_MEMO[(y, i)]
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError(
            "CPS microdata needs CENSUS_API_KEY (free: "
            "https://api.census.gov/data/key_signup.html)"
        )
    try:
        js = json.loads(
            f.get(
                CPS_BASIC.format(y=y, m=CPS_MONTHS[i]),
                {"get": CPS_VARS, "PRTAGE": "16:64", "key": key},
                label=f"census:cps:{y}-{CPS_MONTHS[i]}",
                validate=is_json,
                timeout=180,
            )
        )
    except FetchError as e:
        if "HTTP 404" in str(e) or "unexpected response" in str(e) or "HTTP 204" in str(e):
            _CPS_MEMO[(y, i)] = None
            return None
        raise
    d = pd.DataFrame(js[1:], columns=js[0]).loc[:, lambda x: ~x.columns.duplicated()]
    ids = d[["HRHHID", "HRHHID2"]].astype(str)
    d = d.drop(columns=["HRHHID", "HRHHID2"]).apply(pd.to_numeric, errors="coerce")
    d = pd.concat([d, ids], axis=1).assign(month=i + 1, year=y)
    _CPS_MEMO[(y, i)] = d
    return d


# Sales / office occupations that ISCO-08 puts in major groups 2-3 (professionals,
# associate professionals): graduate-level jobs for the over-qualification measure.
CPS_GRAD_OCC = {4710, 4800, 4810, 4820, 4840, 4850, 4920, 4930, 5000, 5250, 5500, 5710, 5720, 5730}


CPS_BANDS = {
    "15-24": (16, 24),
    "20-24": (20, 24),
    "25-29": (25, 29),
    "30-34": (30, 34),
    "25-34": (25, 34),
    "25-54": (25, 54),
    "25-64": (25, 64),
    "15-64": (16, 64),
}


def cps_education(peeduca: pd.Series) -> pd.Series:
    """CPS PEEDUCA -> education key. 'Some college, no degree' is upper secondary."""
    return pd.cut(
        peeduca,
        [0, 38, 40, 42, 43, 46],
        labels=[
            "below_upper_secondary",
            "upper_secondary",
            "short_tertiary",
            "bachelor",
            "master_plus",
        ],
    ).astype(str)


def cps_rates(d: pd.DataFrame) -> dict:
    """Weighted labour-market rates for a CPS microdata frame (columns as CPS_VARS)."""
    w = d["PWCMPWGT"]
    lf = d["PEMLR"].between(1, 4)
    un = d["PEMLR"].isin([3, 4])
    emp = d["PEMLR"].isin([1, 2])
    # marginally attached (BLS): want a job, available, searched in the past 12 months
    ma = d["PEMLR"].between(5, 7) & d["PRDISC"].isin([1, 2])
    # part-timers who want full-time hours and are available (Eurostat UEMP_PT concept)
    ptw = emp & (d["PEHRWANT"] == 1) & (d["PEHRAVL"] != 2)
    pte = emp & d["PRWKSTAT"].isin([3, 6])  # BLS 'part time for economic reasons'
    occ = emp & d["PRMJOCC1"].between(1, 10)
    # non-graduate job ~ ISCO 4-9: outside management/business/financial/professional,
    # except the sales and office jobs ISCO classes as associate professional
    ocd = d["PTIO1OCD"] if "PTIO1OCD" in d else pd.Series(-1, index=d.index)
    nongrad_job = occ & d["PRMJOCC1"].between(3, 10) & ~ocd.isin(CPS_GRAD_OCC)
    W = lambda m: float(w[m].sum())  # noqa: E731
    lfw, elf = W(lf), W(lf) + W(ma)
    if lfw <= 0:
        return {}
    return {
        "unemployment": 100 * W(un) / lfw,
        "functional": 100 * (W(un) + W(ptw) + W(ma)) / elf,
        "u6": 100 * (W(un) + W(pte) + W(ma)) / elf,
        "hours_underemployment": 100 * W(ptw) / elf,
        "slack_extra": 100 * (W(ptw) + W(ma)) / elf,
        "marginally_attached": 100 * W(ma) / elf,
        "overqualification": 100 * W(nongrad_job) / W(occ) if W(occ) else float("nan"),
        "n_lf": float(lf.sum()),
    }


def labour_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US rates by age and education straight from the CPS basic monthly microdata (Census
    API), pooled by calendar year: the current year so far and last year. BLS publishes
    no age x education U-6, and OECD/BLS annual tables lag (or, for 2025, are missing)."""
    now = datetime.now()
    rows = []
    for y in (now.year - 1, now.year):
        months = []
        for i in range(12):
            if y == now.year and i + 1 >= now.month:
                break
            d = cps_month(f, y, i)
            if d is not None:
                months.append(d.copy())
        if not months:
            continue
        d = pd.concat(months, ignore_index=True)
        d["PWCMPWGT"] = d["PWCMPWGT"] / d["month"].nunique()  # pooled = average month
        d["edu"] = cps_education(d["PEEDUCA"])
        tert = d["edu"].isin(["short_tertiary", "bachelor", "master_plus"])
        got = sorted(d["month"].unique())
        span = f"{CPS_MONTHS[got[0] - 1].title()}-{CPS_MONTHS[got[-1] - 1].title()} {y}"
        for band, (lo, hi) in CPS_BANDS.items():
            b = d[d["PRTAGE"].between(lo, hi)]
            for edu, sub in [("all", b), ("tertiary", b[tert.loc[b.index]])] + [
                (e, g) for e, g in b.groupby("edu")
            ]:
                if edu == "nan":
                    continue
                for stat, v in cps_rates(sub).items():
                    rows.append(
                        {
                            "iso3": "USA",
                            "period": str(y),
                            "age_band": band,
                            "category": edu,
                            "statistic": stat,
                            "value": v,
                            "estimated": len(got) < 12,
                            "method": f"{len(got)} months pooled ({span})",
                        }
                    )
    if not rows:
        raise FetchError("CPS microdata: no months fetched")
    return frame(
        pd.DataFrame(rows).assign(series="CPS basic monthly"),
        measure="us_cps_labour",
        frequency="A",
        unit="% (see statistic)",
        source="Census/BLS CPS microdata",
        comparability_note="Computed from CPS public-use microdata with composite "
        "weights. functional = (unemployed + part-timers wanting full-time + "
        "marginally attached) / (labour force + marginally attached); "
        "overqualification = employed outside management, business, financial and "
        "professional occupations (approximates ISCO 4-9).",
    )


CAREER_STATES = ["FT_grad", "FT_nongrad", "PT", "U"]


CAREER_BANDS = {
    "20-24": (20, 24),
    "25-29": (25, 29),
    "30-34": (30, 34),
    "35-39": (35, 39),
    "40-44": (40, 44),
    "45-49": (45, 49),
    "50-54": (50, 54),
    "55-59": (55, 59),
    "60-64": (60, 64),
}


CAREER_GROUPS = {
    "20-24": (20, 24),
    "25-29": (25, 29),
    "30-34": (30, 34),
    "35-44": (35, 44),
    "45-54": (45, 54),
    "55-64": (55, 64),
}


CAREER_EDU = {
    "below_upper_secondary": ["below_upper_secondary"],
    "upper_secondary": ["upper_secondary"],
    "short_tertiary": ["short_tertiary"],
    "bachelor": ["bachelor"],
    "master_plus": ["master_plus"],
    "no_degree": ["below_upper_secondary", "upper_secondary"],
    "degree": ["bachelor", "master_plus"],
    "all": None,
}


EARN_Q = (10, 25, 50, 75, 90)


def cps_career_state(d: pd.DataFrame) -> pd.Series:
    """Each person's situation: full-time in a graduate-level job (management, business,
    financial, professional, or the sales/office jobs ISCO puts in groups 2-3), full-time
    in another job, part-time (< 35 usual hours), or unemployed (incl. discouraged and
    other marginally attached workers). Students (enrolled, 16-24) and people not looking
    for work are left out."""
    emp = d["PEMLR"].isin([1, 2])
    un = d["PEMLR"].isin([3, 4]) | (d["PEMLR"].between(5, 7) & d["PRDISC"].isin([1, 2]))
    hrs = d["PEHRUSL1"]
    pt = emp & (hrs.between(1, 34) | ((hrs < 0) & d["PRWKSTAT"].isin([3, 4, 5, 6])))
    grad = d["PRMJOCC1"].isin([1, 2]) | d["PTIO1OCD"].isin(CPS_GRAD_OCC)
    st = np.select([un, pt, emp & grad, emp], ["U", "PT", "FT_grad", "FT_nongrad"], "NILF")
    st = pd.Series(st, index=d.index)
    st[d["PESCHENR"] == 1] = "student"
    return st


def _wq(x: pd.Series, w: pd.Series, qs) -> list[float]:
    o = np.argsort(x.to_numpy())
    xv, wv = x.to_numpy()[o], w.to_numpy()[o]
    c = np.cumsum(wv) / wv.sum()
    return [float(xv[min(np.searchsorted(c, q / 100), len(xv) - 1)]) for q in qs]


def _edu_mask(edu: pd.Series, key: str) -> pd.Series:
    v = CAREER_EDU[key]
    return pd.Series(True, index=edu.index) if v is None else edu.isin(v)


def career_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """The current US career picture from CPS microdata, last 12 months vs the 12 before:
    shares of each situation by age and degree; pay distributions in each (outgoing
    rotation groups); year-on-year moves between situations and individual pay growth
    (the same people matched one year apart, months-in-sample 1-4 -> 5-8 and 4 -> 8)."""
    got = _cps_recent_months(f)
    if not got:
        raise FetchError("CPS microdata: no months fetched")
    last = max(got)
    cpi = fred(f, "CPIAUCSL").assign(per=lambda x: pd.PeriodIndex(x["date"].str[:7], freq="M"))
    cpi = cpi.set_index("per")["value"]
    rows = []

    def add(window, stat, band, edu, value, n, method):
        rows.append(
            {
                "iso3": "USA",
                "period": str(window[1]),
                "age_band": band,
                "category": edu,
                "statistic": stat,
                "value": value,
                "method": f"{method}; n={n}; {window[0]}..{window[1]}",
                "estimated": False,
            }
        )

    for wname, (w0, w1) in {"current": (last - 11, last), "prior": (last - 23, last - 12)}.items():
        months = [m for m in got if w0 <= m <= w1]
        if not months:
            continue
        win = (w0, w1)
        tag = lambda s: s if wname == "current" else f"prior:{s}"  # noqa: E731
        d = pd.concat([got[m] for m in months], ignore_index=True)
        d["wc"] = d["PWCMPWGT"] / len(months)
        d["wo"] = d["PWORWGT"] / len(months)
        _career_shares_and_pay(add, win, tag, wname == "current", d)
        pairs, grow = _matched_pairs(months, got, cpi)
        if pairs:
            _career_moves(add, win, tag, pairs)
        if grow and wname == "current":
            _pay_growth(f, add, win, grow, months)
    if not rows:
        raise FetchError("CPS career: nothing computed")
    return frame(
        pd.DataFrame(rows).assign(series="CPS basic monthly"),
        measure="us_career",
        frequency="A",
        unit="share / USD per month / %",
        source="Census/BLS CPS microdata (Census API)",
    )


def _cps_recent_months(f: CachedFetcher) -> dict:
    """The CPS basic monthly files of about the last 3 years, with each person's degree
    and situation."""
    now = pd.Period(datetime.now(), "M")
    got = {}
    for k in range(1, 40):  # newest release back ~3 years
        per = now - k
        d = cps_month(f, per.year, per.month - 1)
        if d is not None:
            d = d.copy()
            d["edu"] = cps_education(d["PEEDUCA"]).astype(str)
            d["state"] = cps_career_state(d)
            got[per] = d
    return got


def _career_shares_and_pay(add, win, tag, current: bool, d: pd.DataFrame) -> None:
    """Shares of each situation by age and degree; pay distributions in each (outgoing
    rotation groups; current window only)."""
    inlf = d["state"].isin(CAREER_STATES)
    org = (
        d["HRMIS"].isin([4, 8])
        & d["state"].isin(CAREER_STATES[:3])
        & (d["PRERELG"] == 1)
        & (d["PTERNWA"] > 0)
    )
    for band, (lo, hi) in CAREER_BANDS.items():
        b = d["PRTAGE"].between(lo, hi)
        for ek in CAREER_EDU:
            m = b & _edu_mask(d["edu"], ek)
            s = d[m & inlf]
            if len(s) < 50:
                continue
            tot = s["wc"].sum()
            for stn in CAREER_STATES:
                add(
                    win,
                    tag(f"share|{stn}"),
                    band,
                    ek,
                    float(s.loc[s["state"] == stn, "wc"].sum() / tot),
                    len(s),
                    "share of the labour force (incl. discouraged), students excluded",
                )
            if not current:
                continue
            for stn in CAREER_STATES[:3]:
                e = d[m & org & (d["state"] == stn)]
                if len(e) < 30:
                    continue
                for q, v in zip(EARN_Q, _wq(e["PTERNWA"], e["wo"], EARN_Q)):
                    add(
                        win,
                        f"earn_q{q}|{stn}",
                        band,
                        ek,
                        v * 52 / 12,
                        len(e),
                        "usual weekly earnings x 52 / 12, outgoing rotation groups",
                    )


def _matched_pairs(months: list, got: dict, cpi: pd.Series) -> tuple[list, list]:
    """The same people one year apart: all matched pairs, and those in the same situation
    with pay both years (with the CPI change over that year)."""
    pairs, grow = [], []
    for m in months:
        prev = m - 12
        if prev not in got:
            continue
        a, b = got[prev], got[m]
        k = ["HRHHID", "HRHHID2", "PULINENO"]
        x = a[a["HRMIS"].between(1, 4)].merge(
            b[b["HRMIS"].between(5, 8)], on=k, suffixes=("0", "1")
        )
        ok = (x["PESEX0"] == x["PESEX1"]) & (x["PRTAGE1"] - x["PRTAGE0"]).between(0, 2)
        x = x[ok]
        pairs.append(x)
        g = x[
            (x["HRMIS0"] == 4)
            & (x["HRMIS1"] == 8)
            & (x["PRERELG0"] == 1)
            & (x["PRERELG1"] == 1)
            & (x["PTERNWA0"] > 0)
            & (x["PTERNWA1"] > 0)
            & x["state0"].isin(CAREER_STATES[:3])
            & (x["state1"] == x["state0"])
        ]
        if prev in cpi.index and m in cpi.index:
            g = g.assign(cpi_g=float(cpi[m] / cpi[prev] - 1))
            grow.append(g)
    return pairs, grow


def _career_moves(add, win, tag, pairs: list) -> None:
    """Year-on-year moves between situations, by age group and degree."""
    x = pd.concat(pairs, ignore_index=True)
    x = x[x["state0"].isin(CAREER_STATES) & x["state1"].isin(CAREER_STATES)]
    for grp, (lo, hi) in CAREER_GROUPS.items():
        for ek in CAREER_EDU:
            sub = x[x["PRTAGE0"].between(lo, hi) & _edu_mask(x["edu0"], ek)]
            for s0 in CAREER_STATES:
                o = sub[sub["state0"] == s0]
                if len(o) < 40:
                    continue
                tot = o["PWCMPWGT0"].sum()
                for s1 in CAREER_STATES:
                    add(
                        win,
                        tag(f"trans|{s0}>{s1}"),
                        grp,
                        ek,
                        float(o.loc[o["state1"] == s1, "PWCMPWGT0"].sum() / tot),
                        len(o),
                        "share of those in the first situation who are in "
                        "the second a year later (matched CPS households)",
                    )


def _pay_growth(f: CachedFetcher, add, win, grow: list, months: list) -> None:
    """Inflation, economy-wide wage growth and the same workers' pay growth over a year."""
    g = pd.concat(grow, ignore_index=True)
    g["lg"] = np.log(g["PTERNWA1"] / g["PTERNWA0"])
    g = g[g["lg"].abs() < 1.5]  # coding errors (e.g. weekly vs annual)
    infl = float(np.log(1 + g["cpi_g"]).mean())
    add(
        win,
        "cpi_yoy",
        "total",
        "all",
        100 * (np.exp(infl) - 1),
        len(g),
        "CPI-U change over the matched years",
    )
    ahe = fred(f, "CES0500000003")
    ahe = ahe.assign(per=pd.PeriodIndex(ahe["date"].str[:7], freq="M")).set_index("per")["value"]
    yo = [ahe[m] / ahe[m - 12] - 1 for m in months if m in ahe.index and (m - 12) in ahe.index]
    if yo:
        add(
            win,
            "agg_wage_yoy",
            "total",
            "all",
            100 * float(np.mean(yo)),
            len(yo),
            "average hourly earnings, all private employees (BLS CES), change over a year",
        )
    for grp, (lo, hi) in list(CAREER_GROUPS.items()) + [
        ("25-54", (25, 54)),
        ("16-64", (16, 64)),
    ]:
        for ek in (
            "all",
            "degree",
            "no_degree",
            "short_tertiary",
            "bachelor",
            "master_plus",
        ):
            sub = g[g["PRTAGE0"].between(lo, hi) & _edu_mask(g["edu0"], ek)]
            if len(sub) < 60:
                continue
            med = _wq(sub["lg"], sub["PWORWGT0"], (50,))[0]
            add(
                win,
                "pay_growth_median",
                grp,
                ek,
                100 * (np.exp(med) - 1),
                len(sub),
                "median change in the same worker's usual weekly pay over "
                "a year, nominal, workers in the same situation both years (moves "
                "between situations are modelled separately)",
            )


PC_ELF = "% of extended labour force"

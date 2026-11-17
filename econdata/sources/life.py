"""Life events and emergencies: children, families, pensions, retirement,
university, interest rates, household assets, job loss."""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime

import pandas as pd

from econdata.clients import (
    EU_GEOS,
    EUROSTAT_API,
    col_like,
    dimcol,
    eurostat,
    fred,
    oecd,
    oecd_rows,
    parse_jsonstat,
    statcan,
)
from econdata.config import COUNTRIES, EURO_AREA, ISO2_TO_ISO3, NOTES
from econdata.fetch import CachedFetcher, FetchError, is_json
from econdata.sources.housing import ECB_API
from econdata.sources.labour import CPS_BASIC, CPS_MONTHS, EDU_EUROSTAT, cps_education
from econdata.tidy import frame

log = logging.getLogger("econ")


NCC_DIMS = [
    "REF_AREA",
    "MEASURE",
    "UNIT_MEASURE",
    "INCOME_CURR",
    "HOUSEHOLD_TYPE",
    "AGE_CHILDREN",
    "INCOME_PART",
    "CHILDCARE_USE",
    "SOC_ASS_BENEFIT",
    "HOUSE_BENEFIT",
    "CHILDCARE_COST",
    "FREQ",
]


# ---- families: mothers' work by children, leaving home, survey under-recording ---------
FAM_CHILD = {"0": "0", "1": "1", "2": "2", "GE3": "3"}


FAM_YOUNGEST = {"NAP": "none", "Y_LT6": "lt6", "Y6-11": "6-11", "Y_GE12": "ge12"}


def family_work_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Women 25-49: employment rate (by education) and part-time share by number of
    children and age of the youngest child, plus usual weekly hours of full-time and
    part-time women: the 'child penalty' in employment and hours."""
    yr = max(since or 2015, 2015)
    parts = []
    e = eurostat(
        f,
        "lfst_hheredch",
        required={"sex": ["F"], "unit": ["RT"], "age": ["Y25-49"]},
        optional={
            "n_child": list(FAM_CHILD),
            "agechild": list(FAM_YOUNGEST),
            "isced11": list(EDU_EUROSTAT),
        },
        since=yr,
        geo=EU_GEOS,
    )
    e = e[
        e["n_child"].isin(FAM_CHILD)
        & e["agechild"].isin(FAM_YOUNGEST)
        & e["isced11"].isin(EDU_EUROSTAT)
    ]
    parts.append(
        pd.DataFrame(
            {
                "iso3": e["iso3"],
                "period": e["time"],
                "value": e["value"],
                "statistic": "employment_rate",
                "unit": "%",
                "category": e["isced11"].map(EDU_EUROSTAT),
                "household_type": e["n_child"].map(FAM_CHILD)
                + "|"
                + e["agechild"].map(FAM_YOUNGEST),
                "series": "lfst_hheredch",
            }
        )
    )
    p = eurostat(
        f,
        "lfst_hhptechi",
        required={"sex": ["F"], "unit": ["PC"], "age": ["Y25-49"]},
        optional={"n_child": list(FAM_CHILD), "agechild": list(FAM_YOUNGEST)},
        since=yr,
        geo=EU_GEOS,
    )
    p = p[p["n_child"].isin(FAM_CHILD) & p["agechild"].isin(FAM_YOUNGEST)]
    parts.append(
        pd.DataFrame(
            {
                "iso3": p["iso3"],
                "period": p["time"],
                "value": p["value"],
                "statistic": "part_time_share",
                "unit": "%",
                "category": "all",
                "household_type": p["n_child"].map(FAM_CHILD)
                + "|"
                + p["agechild"].map(FAM_YOUNGEST),
                "series": "lfst_hhptechi",
            }
        )
    )
    h = eurostat(
        f,
        "lfsa_ewhun2",
        required={"sex": ["F"], "unit": ["HR"], "age": ["Y25-54"]},
        optional={"nace_r2": ["TOTAL"], "wstatus": ["EMP"], "worktime": ["FT", "PT"]},
        since=yr,
        geo=EU_GEOS,
    )
    h = h[h["worktime"].isin(["FT", "PT"])]
    parts.append(
        pd.DataFrame(
            {
                "iso3": h["iso3"],
                "period": h["time"],
                "value": h["value"],
                "statistic": "hours_" + h["worktime"].str.lower(),
                "unit": "hours",
                "category": "all",
                "household_type": "any",
                "series": "lfsa_ewhun2",
            }
        )
    )
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="family_work",
        frequency="A",
        source="Eurostat LFS",
        age_band="25-49",
        comparability_note="Women aged 25-49 by number of children and age of the "
        "youngest child; hours = usual weekly hours in the main job.",
    )


def leave_home_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "yth_demo_030",
        required={"sex": ["T"], "unit": ["AVG"]},
        since=max(since or 2010, 2010),
        geo=EU_GEOS,
    )
    out = pd.DataFrame(
        {"iso3": df["iso3"], "period": df["time"], "value": df["value"], "series": "yth_demo_030"}
    )
    return frame(
        out,
        measure="age_leave_home",
        statistic="mean",
        unit="years",
        frequency="A",
        source="Eurostat LFS",
        comparability_note="Estimated average age at which young people leave the "
        "parental household.",
    )


CPS_FAM_VARS = "PRTAGE,PESEX,PERRP,PRCHLD,PRNMCHLD,PEMLR,PEHRUSL1,PEEDUCA,HRHHID,HRHHID2,PWCMPWGT"


CPS_YOUNGEST = {
    1: "lt6",
    2: "lt6",
    5: "lt6",
    6: "lt6",
    7: "lt6",
    8: "lt6",
    9: "lt6",
    11: "lt6",
    12: "lt6",
    13: "lt6",
    14: "lt6",
    15: "lt6",
    3: "6-11",
    10: "6-11",
    4: "ge12",
}


def family_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """From the CPS basic monthly microdata (last 3 published months): the age at which
    half of young adults no longer live with a parent, and women 25-49's employment rate
    and usual hours by number of own children and the youngest child's age."""
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("CPS microdata needs CENSUS_API_KEY")
    now = datetime.now()
    got, frames = [], []
    for back in range(1, 16):
        y, m = now.year, now.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        try:
            js = json.loads(
                f.get(
                    CPS_BASIC.format(y=y, m=CPS_MONTHS[m - 1]),
                    {"get": CPS_FAM_VARS, "PRTAGE": "16:49", "key": key},
                    label=f"census:cps-fam:{y}-{m:02d}",
                    validate=is_json,
                    timeout=120,
                )
            )
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue
            raise
        d = pd.DataFrame(js[1:], columns=js[0]).loc[:, lambda x: ~x.columns.duplicated()]
        frames.append(d.assign(month=f"{y}-{m:02d}"))
        got.append(f"{y}-{m:02d}")
        if len(got) == 3:
            break
    if not frames:
        raise FetchError("CPS family microdata: no months fetched")
    d = pd.concat(frames, ignore_index=True)
    for c in d.columns:
        if c not in ("HRHHID", "HRHHID2", "month"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
    w = d["PWCMPWGT"]
    hh = d["HRHHID"].astype(str) + d["HRHHID2"].astype(str) + d["month"]
    has_parent = hh.isin(set(hh[d["PERRP"] == 50]))  # a parent of the reference person
    with_par = (d["PERRP"] == 48) | (d["PERRP"].isin([40, 41]) & has_parent)
    period, method = got[0][:4], f"CPS basic monthly {got[-1]} to {got[0]}"
    rows = []
    share = {
        a: float(w[(d.PRTAGE == a) & with_par].sum() / w[d.PRTAGE == a].sum())
        for a in range(18, 41)
    }
    below = [a for a in share if share[a] < 0.5]
    if below:
        a = below[0]
        prev = share.get(a - 1, 1.0)
        age = a - 1 + (prev - 0.5) / (prev - share[a]) if prev > share[a] else a
        rows.append(
            {
                "measure": "age_leave_home",
                "statistic": "mean",
                "value": age + 0.5,
                "unit": "years",
                "category": "all",
                "household_type": "any",
                "series": "CPS age_leave_home",
                "method": method + "; age at which half no longer live with a parent",
            }
        )
    women = d[(d.PESEX == 2) & d.PRTAGE.between(25, 49)].copy()
    women["edu"] = cps_education(women["PEEDUCA"]).replace(
        {"short_tertiary": "tertiary", "bachelor": "tertiary", "master_plus": "tertiary"}
    )
    women["young"] = women["PRCHLD"].map(CPS_YOUNGEST).fillna("none")
    women["n"] = women["PRNMCHLD"].clip(lower=0, upper=3).astype(int).astype(str)
    women.loc[women["young"] == "none", "n"] = "0"
    emp = women["PEMLR"].isin([1, 2])
    women["hrs"] = women["PEHRUSL1"].where(emp & (women["PEHRUSL1"] > 0))
    for (n, yg), g in women.groupby(["n", "young"]):
        if n != "0" and yg == "none":
            continue
        for edu, s in [("all", g)] + list(g.groupby("edu")):
            if edu == "nan" or len(s) < 30:
                continue
            ww = s["PWCMPWGT"]
            e = s["PEMLR"].isin([1, 2])
            hrs = s["hrs"].dropna()
            ht = f"{n}|{yg}"
            rows.append(
                {
                    "measure": "family_work",
                    "statistic": "employment_rate",
                    "value": 100 * ww[e].sum() / ww.sum(),
                    "unit": "%",
                    "category": edu,
                    "household_type": ht,
                    "series": "CPS family",
                    "method": method,
                }
            )
            if edu == "all" and len(hrs):
                rows.append(
                    {
                        "measure": "family_work",
                        "statistic": "mean_hours",
                        "value": float((hrs * ww[hrs.index]).sum() / ww[hrs.index].sum()),
                        "unit": "hours",
                        "category": "all",
                        "household_type": ht,
                        "series": "CPS family",
                        "method": method,
                    }
                )
    out = pd.DataFrame(rows).assign(iso3="USA", period=period)
    return frame(
        out,
        frequency="A",
        source="Census/BLS CPS microdata",
        age_band="25-49",
        estimated=True,
        comparability_note="Computed from CPS public-use microdata.",
    )


def family_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """StatCan 14-10-0120 (discontinued after 2020): women in couple families, employment
    and full-/part-time employment by the youngest child's age."""
    df = statcan(
        f,
        "14100120",
        match={
            "labour force": [
                "Population",
                "Employment",
                "Full-time employment",
                "Part-time employment",
            ],
            "family type": "Female reference person/spouse with employed husband",
            "age composition": None,
        },
        since=2015,
    )
    lc, ac = col_like(df, "labour force"), col_like(df, "age composition")
    yb = {
        "Youngest child less than 6 years": "lt6",
        "Youngest child 6 to 15 years": "6-11",
        "Without children less than 16 years": "none",
    }
    df = df[df[ac].isin(yb)]
    p = df.pivot_table(index=["REF_DATE", ac], columns=lc, values="VALUE", aggfunc="first")
    rows = []
    for (yr, a), r in p.iterrows():
        if not r.get("Population"):
            continue
        n = "0" if yb[a] == "none" else "2"
        ht = f"{n}|{yb[a]}"
        rows.append(
            {
                "period": str(yr)[:4],
                "statistic": "employment_rate",
                "value": 100 * r["Employment"] / r["Population"],
                "unit": "%",
                "household_type": ht,
            }
        )
        if r.get("Employment"):
            rows.append(
                {
                    "period": str(yr)[:4],
                    "statistic": "part_time_share",
                    "value": 100 * r["Part-time employment"] / r["Employment"],
                    "unit": "%",
                    "household_type": ht,
                }
            )
    out = pd.DataFrame(rows).assign(iso3="CAN", category="all", series="14-10-0120-01")
    return frame(
        out,
        measure="family_work",
        frequency="A",
        source="StatCan LFS",
        age_band="all",
        estimated=True,
        comparability_note="Women in couple families with an employed husband; "
        "table discontinued after 2020; youngest child 6-15 used for 6-11 and 12+.",
    )


HBS_DIV = [f"CP{i:02d}" for i in range(1, 13)]


def hbs_coverage_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Inputs for the HBS under-recording correction: HBS mean spending per household by
    COICOP division (EUR = PPS shares x EUR total), national-accounts household consumption
    by division, and the number of private households."""
    parts = []
    hb = eurostat(
        f, "hbs_exp_t121", required={"unit": ["PPS_HH"]}, optional={"coicop": HBS_DIV}, geo=EU_GEOS
    )
    tot = eurostat(f, "hbs_exp_t111", required={"unit": ["EUR_HH"]}, geo=EU_GEOS)
    hb = hb[hb["coicop"].isin(HBS_DIV)].copy()
    hb["share"] = hb["value"] / hb.groupby(["iso3", "time"])["value"].transform("sum")
    t = tot.set_index(["iso3", "time"])["value"]
    hb["eur"] = hb["share"] * [t.get((i, y)) for i, y in zip(hb["iso3"], hb["time"])]
    parts.append(
        pd.DataFrame(
            {
                "iso3": hb["iso3"],
                "period": hb["time"],
                "value": hb["eur"],
                "statistic": "hbs_eur_per_household",
                "category": hb["coicop"],
                "unit": "EUR/year",
                "series": "hbs_exp_t121",
            }
        )
    )
    co = eurostat(
        f,
        "nama_10_co3_p3",
        required={"unit": ["CP_MEUR"]},
        optional={"coicop": HBS_DIV},
        since=2010,
        geo=EU_GEOS,
    )
    co = co[co["coicop"].isin(HBS_DIV)]
    parts.append(
        pd.DataFrame(
            {
                "iso3": co["iso3"],
                "period": co["time"],
                "value": co["value"],
                "statistic": "na_meur",
                "category": co["coicop"],
                "unit": "million EUR",
                "series": "nama_10_co3_p3",
            }
        )
    )
    hh = eurostat(
        f,
        "lfst_hhnhtych",
        required={"unit": ["THS_HH"]},
        optional={"phhcomp": ["TOTAL"], "n_child": ["TOTAL"], "agechild": ["TOTAL"]},
        since=2010,
        geo=EU_GEOS,
    )
    parts.append(
        pd.DataFrame(
            {
                "iso3": hh["iso3"],
                "period": hh["time"],
                "value": hh["value"],
                "statistic": "households_ths",
                "category": "all",
                "unit": "thousand",
                "series": "lfst_hhnhtych",
            }
        )
    )
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="hbs_coverage",
        frequency="A",
        source="Eurostat HBS / national accounts / LFS",
        comparability_note="Household budget survey vs national-accounts household "
        "consumption by COICOP division.",
    )


# US: BEA personal consumption (NIPA table 2.4.5, $bn) vs the BLS Consumer Expenditure
# Survey (all consumer units, $ per unit), matched to the same COICOP divisions as Europe
US_COVERAGE = {
    "CP01": (["DFXARC1A027NBEA", "-DAOPRC1A027NBEA"], ["CXUFOODHOMELB0101M"]),
    "CP02": (["DAOPRC1A027NBEA", "DTOBRC1A027NBEA"], ["CXUALCBEVGLB0101M", "CXUTOBACCOLB0101M"]),
    "CP03": (["DCLORC1A027NBEA"], ["CXUAPPARELLB0101M"]),
    "CP05": (["DFDHRC1A027NBEA"], ["CXUHHFURNSHLB0101M"]),
    "CP09": (["DRCARC1A027NBEA", "DREQRC1A027NBEA"], ["CXUENTRTAINLB0101M"]),
    "CP10": (["DEDURC1A027NBEA"], ["CXUEDUCATNLB0101M"]),
    "CP11": (["DFSERC1A027NBEA"], ["CXUFOODAWAYLB0101M"]),
}


def coverage_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US version of the survey under-recording inputs: BEA PCE by product vs BLS CE means
    for all consumer units, and the number of households (Census CPS/HVS, FRED)."""

    def annual(sid):
        d = fred(f, sid.lstrip("-"))
        a = d.assign(y=d["date"].str[:4]).groupby("y")["value"].mean()
        return -a if sid.startswith("-") else a

    rows = []
    for cp, (pce, ce) in US_COVERAGE.items():
        na = sum(annual(s) for s in pce) * 1000.0  # $bn -> $m
        sv = sum(annual(s) for s in ce)
        for y, v in na.dropna().items():
            rows.append(
                {
                    "period": y,
                    "value": v,
                    "statistic": "na_meur",
                    "category": cp,
                    "unit": "million USD",
                    "series": "+".join(pce),
                }
            )
        for y, v in sv.dropna().items():
            rows.append(
                {
                    "period": y,
                    "value": v,
                    "statistic": "hbs_eur_per_household",
                    "category": cp,
                    "unit": "USD/year",
                    "series": "+".join(ce),
                }
            )
    for y, v in annual("TTLHHM156N").dropna().items():
        rows.append(
            {
                "period": y,
                "value": v,
                "statistic": "households_ths",
                "category": "all",
                "unit": "thousand",
                "series": "TTLHHM156N",
            }
        )
    df = pd.DataFrame(rows).assign(iso3="USA")
    df = df[df["period"].astype(int) >= 2015]
    return frame(
        df,
        measure="hbs_coverage",
        frequency="A",
        source="BEA PCE / BLS CE / Census households (FRED)",
        comparability_note="Consumer Expenditure Survey vs national-accounts "
        "personal consumption by category.",
    )


PRR_DIMS = ["REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "OPTIONALITY"]


NPRR_STAT = {
    "NPRR100": "net_rate_average_earner",
    "NPRR50": "net_rate_half_average",
    "NPRR200": "net_rate_double_average",
}


def pensions_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Pensions at a Glance: net pension replacement rates at 50%, 100% and 200% of
    the average wage (mandatory schemes; mandatory + voluntary where voluntary plans are
    widespread). These are PROSPECTIVE: a person entering the labour market at 22 in the
    reference year, under the rules already legislated for their whole career."""
    df = oecd(
        f,
        "OECD.ELS.SPD,DSD_PAG@DF_PRR",
        PRR_DIMS,
        {"REF_AREA": [c for c in COUNTRIES if c != "LIE"], "MEASURE": list(NPRR_STAT)},
        None,
    )
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    g = df.groupby(["REF_AREA", "TIME_PERIOD", "OPTIONALITY", "MEASURE"], as_index=False)[
        "OBS_VALUE"
    ].mean()  # average of men and women
    out = pd.DataFrame(
        {
            "iso3": g["REF_AREA"],
            "period": g["TIME_PERIOD"],
            "value": g["OBS_VALUE"],
            "statistic": g["MEASURE"].map(NPRR_STAT),
            "category": g["OPTIONALITY"].map({"M": "mandatory", "MV": "mandatory_voluntary"}),
            "series": "DF_PRR " + g["MEASURE"],
        }
    )
    return frame(
        out,
        measure="pension_replacement",
        unit="% of net pre-retirement earnings",
        frequency="A",
        source="OECD Pensions at a Glance",
        comparability_note="Net replacement rate, full career from age 22 under "
        "legislated rules; men and women averaged.",
    )


def retirement_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Pensions at a Glance: the normal retirement age for someone who entered the
    labour market at 22 in the reference year (legislated increases, including links to
    life expectancy, applied), today's normal age, and life expectancy at 65 now and as
    projected for 2065 (UN World Population Prospects)."""
    df = oecd(
        f,
        "OECD.ELS.SPD,DSD_PAG@DF_PAG",
        PRR_DIMS,
        {
            "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
            "MEASURE": ["FRPLF22", "CRPLF22", "LE"],
        },
        None,
    )
    df = df[(df["MEASURE"] != "LE") | (df["AGE"] == "Y65")]
    stat = {
        "FRPLF22": "normal_age_future_entrant",
        "CRPLF22": "normal_age_current",
        "LE": "life_expectancy_at_65",
    }
    out = oecd_rows(
        df,
        {
            "statistic": df["MEASURE"].map(stat),
            "category": df["SEX"].map({"M": "male", "F": "female"}),
            "series": "DF_PAG " + df["MEASURE"],
        },
    )
    return frame(
        out,
        measure="retirement",
        unit="age (years)",
        frequency="A",
        source="OECD Pensions at a Glance",
        comparability_note="Retirement ages for a labour-market entrant at 22; "
        "life expectancy at 65 expressed as the age reached (65 + years left).",
    )


EAG_FIN_DIMS = [
    "REF_AREA",
    "MEASURE",
    "EDUCATION_LEV",
    "EXP_SOURCE",
    "EXP_DESTINATION",
    "EXPENDITURE_TYPE",
    "PRICE_BASE",
    "UNIT_MEASURE",
    "Q_SHEET",
]


def university_cost_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Education at a Glance (UOE finance): what households pay to tertiary
    institutions per full-time-equivalent student (tuition and fees net of grants paid to
    the institution, incl. fees financed by student loans), national currency. Where the
    household share is not reported, all private non-educational sources (households and
    firms) are used and flagged."""
    df = oecd(
        f,
        "OECD.EDU.IMEP,DSD_EAG_UOE_FIN@DF_UOE_FIN_INDIC_SOURCE_NATURE",
        EAG_FIN_DIMS,
        {
            "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
            "MEASURE": ["FIN_PERSTUD"],
            "EDUCATION_LEV": ["ISCED11_5T8"],
            "EXP_SOURCE": ["S14", "S1D_NON_EDU"],
            "EXP_DESTINATION": ["INST_EDU"],
            "EXPENDITURE_TYPE": ["DIR_EXP"],
            "PRICE_BASE": ["V"],
            "UNIT_MEASURE": ["XDC_ST"],
        },
        max(since or 2015, 2015),
    )
    out = oecd_rows(
        df,
        {
            "statistic": df["EXP_SOURCE"].map({"S14": "households", "S1D_NON_EDU": "all_private"}),
            "series": "EAG UOE_FIN FIN_PERSTUD",
        },
    )
    return frame(
        out,
        measure="tertiary_private_cost",
        unit="national currency per FTE student",
        frequency="A",
        source="OECD Education at a Glance",
        comparability_note="Payments to tertiary institutions per FTE student; an "
        "average over all students, including those paying nothing.",
    )


STES_DIMS = [
    "REF_AREA",
    "FREQ",
    "MEASURE",
    "UNIT_MEASURE",
    "ACTIVITY",
    "ADJUSTMENT",
    "TRANSFORMATION",
    "TIME_HORIZ",
    "METHODOLOGY",
]


def interest_rates_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD financial-market statistics: 10-year government bond yields (IRLT) and
    3-month interbank rates (IR3TIB), monthly, for the non-EU countries."""
    areas = [c for c in COUNTRIES if COUNTRIES[c][3] != "EU" and c != "LIE"]
    df = oecd(
        f,
        "OECD.SDD.STES,DSD_STES@DF_FINMARK",
        STES_DIMS,
        {"REF_AREA": areas, "FREQ": ["M"], "MEASURE": ["IRLT", "IR3TIB"]},
        max(since or 2000, 2000),
    )
    stat = {"IRLT": "govt_bond_10y", "IR3TIB": "interbank_3m"}
    out = oecd_rows(
        df,
        {
            "statistic": df["MEASURE"].map(stat),
            "series": "STES " + df["MEASURE"],
        },
    )
    return frame(
        out,
        measure="interest_rate",
        unit="% p.a.",
        frequency="M",
        source="OECD financial market statistics",
    )


def interest_rates_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Eurostat: EMU convergence-criterion 10-year bond yields (all EU members) and 3-month
    money-market rates (euro area as one, plus the non-euro members)."""
    s0 = max(since or 2000, 2000)
    lt = eurostat(
        f,
        "irt_lt_mcby_m",
        required={"int_rt": ["MCBY"]},
        since=s0,
        geo=[COUNTRIES[c][1] for c in COUNTRIES if COUNTRIES[c][3] == "EU"],
    )
    lt = lt.assign(statistic="govt_bond_10y", series="irt_lt_mcby_m")
    geos = [COUNTRIES[c][1] for c in COUNTRIES if COUNTRIES[c][3] == "EU" and c not in EURO_AREA]
    own = eurostat(f, "irt_st_m", required={"int_rt": ["IRT_M3"]}, since=s0, geo=geos)
    # the euro-area aggregate has no ISO3 code, so eurostat() would drop it: fetch it raw
    ea = parse_jsonstat(
        f.get(
            EUROSTAT_API + "irt_st_m",
            [
                ("format", "JSON"),
                ("lang", "EN"),
                ("int_rt", "IRT_M3"),
                ("geo", "EA"),
                ("sinceTimePeriod", str(s0)),
            ],
            label="eurostat:irt_st_m:EA",
            validate=is_json,
        )
    )
    st_ = pd.concat(
        [own] + [ea.assign(iso3=c) for c in sorted(EURO_AREA)], ignore_index=True
    ).assign(statistic="interbank_3m", series="irt_st_m")
    d = pd.concat([lt, st_], ignore_index=True)
    out = pd.DataFrame(
        {
            "iso3": d["iso3"],
            "period": d["time"].astype(str),
            "value": d["value"],
            "statistic": d["statistic"],
            "series": d["series"],
        }
    )
    return frame(
        out,
        measure="interest_rate",
        unit="% p.a.",
        frequency="M",
        source="Eurostat (ECB) interest rates",
    )


def deposit_rates_ecb(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What banks actually pay households: ECB MFI interest rates on overnight deposits
    (new business = the current rate) and on deposits with an agreed maturity
    (outstanding amounts), euro-area members."""
    cc = "+".join(COUNTRIES[c][0] for c in sorted(EURO_AREA))
    params = {"format": "csvdata", "startPeriod": f"{max(since or 2015, 2015)}-01"}
    parts = []
    for item, cov, stat in (("L21", "N", "deposit_overnight"), ("L22", "O", "deposit_term")):
        key = f"M.{cc}.B.{item}.A.R.A.2250.EUR.{cov}"
        d = pd.read_csv(
            io.BytesIO(f.get(ECB_API + key, params, label=f"ecb:MIR:{stat}")), dtype=str
        )
        parts.append(
            pd.DataFrame(
                {
                    "iso3": d["REF_AREA"].map(ISO2_TO_ISO3),
                    "period": d["TIME_PERIOD"],
                    "value": d["OBS_VALUE"],
                    "statistic": stat,
                    "series": "MIR:" + d["KEY"],
                }
            )
        )
    out = pd.concat(parts, ignore_index=True).dropna(subset=["iso3"])
    return frame(
        out,
        measure="interest_rate",
        unit="% p.a.",
        frequency="M",
        source="ECB MFI interest rates (households)",
    )


def deposit_rates_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """FDIC national rates (via FRED): savings accounts and 12-month CDs."""
    parts = []
    for sid, stat in (("SNDR", "deposit_overnight"), ("NDR12MCD", "deposit_term")):
        d = fred(f, sid)
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "USA",
                    "period": d["date"].str[:7],
                    "value": d["value"],
                    "statistic": stat,
                    "series": f"FRED {sid}",
                }
            )
        )
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="interest_rate",
        unit="% p.a.",
        frequency="M",
        source="FDIC national deposit rates (FRED)",
    )


T720_DIMS = [
    "FREQ",
    "ADJUSTMENT",
    "REF_AREA",
    "COUNTERPART_AREA",
    "SECTOR",
    "COUNTERPART_SECTOR",
    "CONSOLIDATION",
    "ACCOUNTING_ENTRY",
    "TRANSACTION",
    "INSTR_ASSET",
    "MATURITY",
    "UNIT_MEASURE",
    "CURRENCY_DENOM",
    "VALUATION",
    "PRICE_BASE",
    "TRANSFORMATION",
    "TABLE_IDENTIFIER",
    "DEBT_BREAKDOWN",
]


PORTFOLIO_ITEMS = {
    "F2": "deposits",
    "F22": "deposits_transferable",
    "F29": "deposits_other",
    "F3": "debt_securities",
    "F511": "listed_shares",
    "F52": "fund_shares",
    "F62": "life_insurance",
}


def household_assets_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD financial balance sheets (SNA Table 720): what households (and NPISH) hold
    their financial savings in - deposits, bonds, listed shares, funds, life insurance."""
    df = oecd(
        f,
        "OECD.SDD.NAD,DSD_NASEC20@DF_T720R_A",
        T720_DIMS,
        {
            "FREQ": ["A"],
            "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
            "SECTOR": ["S1M"],
            "ACCOUNTING_ENTRY": ["A"],
            "TRANSACTION": ["LE"],
            "INSTR_ASSET": list(PORTFOLIO_ITEMS),
            "MATURITY": ["T", "_Z"],
            "UNIT_MEASURE": ["XDC"],
        },
        max(since or 2015, 2015),
    )
    out = oecd_rows(
        df,
        {
            "statistic": df["INSTR_ASSET"].map(PORTFOLIO_ITEMS),
            "series": "T720 " + df["INSTR_ASSET"],
        },
    )
    return frame(
        out.dropna(subset=["statistic"]),
        measure="household_assets",
        unit="national currency, millions",
        frequency="A",
        source="OECD financial balance sheets (households and NPISH)",
    )


def childcare_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c != "LIE"]
    df = oecd(
        f,
        "OECD.ELS.JAI,DSD_TAXBEN_NCC@DF_NCC",
        NCC_DIMS,
        {
            "REF_AREA": areas,
            "INCOME_CURR": ["AW100"],
            "HOUSEHOLD_TYPE": ["C_C2"],
            "INCOME_PART": ["AW67", "AW100"],
            "HOUSE_BENEFIT": ["NO"],
            "FREQ": ["A"],
        },
        since,
    )
    if "CHILDCARE_COST" in df and (df["CHILDCARE_COST"] == "_T").any():
        df = df[df["CHILDCARE_COST"] == "_T"]
    if "SOC_ASS_BENEFIT" in df and (df["SOC_ASS_BENEFIT"] == "YES").any():
        df = df[df["SOC_ASS_BENEFIT"] == "YES"]
    hh = "couple 100%+" + df["INCOME_PART"].fillna("") + " AW, 2 children"
    out = oecd_rows(
        df,
        {
            "statistic": df["MEASURE"],
            "household_type": hh,
            "unit": df["UNIT_MEASURE"],
            "category": df.get("CHILDCARE_USE", ""),
            "series": "TAXBEN_NCC",
        },
    )
    return frame(
        out,
        measure="childcare_net_cost",
        frequency="A",
        source="OECD TaxBEN",
        comparability_note=NOTES["ncc"],
    )


NRR_DIMS = [
    "REF_AREA",
    "MEASURE",
    "UNIT_MEASURE",
    "HOUSEHOLD_TYPE",
    "AGE_CHILDREN",
    "INCOME_PREV",
    "INCOME_PART",
    "UNEMP_DURATION",
    "SOC_ASS_BENEFIT",
    "HOUSE_BENEFIT",
    "FREQ",
]


def job_loss_replacement_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c != "LIE"]
    df = oecd(
        f,
        "OECD.ELS.JAI,DSD_TAXBEN_NRR@DF_NRR",
        NRR_DIMS,
        {
            "REF_AREA": areas,
            "HOUSEHOLD_TYPE": ["C_C0", "C_C2"],
            "INCOME_PREV": ["AW100"],
            "UNEMP_DURATION": ["M2", "M6", "M12", "M24"],
            "SOC_ASS_BENEFIT": ["YES"],
            "HOUSE_BENEFIT": ["NO"],
            "FREQ": ["A"],
        },
        since,
    )
    hh = df["HOUSEHOLD_TYPE"] + " | spouse " + df["INCOME_PART"].fillna("")
    out = oecd_rows(
        df,
        {
            "statistic": "net_replacement_rate_" + df["UNEMP_DURATION"].str.lower(),
            "household_type": hh,
            "unit": "% of pre-job-loss net income",
            "series": "TAXBEN_NRR",
        },
    )
    return frame(
        out,
        measure="emergency",
        frequency="A",
        source="OECD TaxBEN",
        comparability_note=NOTES["nrr"],
    )


def emergency_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    # incgrp -> rskpovth, hhtyp -> hhcomp (Eurostat restructuring)
    df = eurostat(f, "ilc_mdes04", optional={"rskpovth": ["TOTAL"]}, since=since, geo=EU_GEOS)
    hh = dimcol(df, "hhcomp", "hhtyp")
    parts.append(
        pd.DataFrame(
            {
                "iso3": df["iso3"],
                "period": df["time"],
                "value": df["value"],
                "statistic": "share_cannot_cover_unexpected_expense",
                "household_type": df[hh + "_label"],
                "unit": "% of persons",
                "series": "ilc_mdes04:" + df[hh],
            }
        )
    )
    try:
        # poverty threshold: was indic_il=LI_C_M60; now statinfo=MED_EI x rskpovth=B_60
        th = eurostat(
            f,
            "ilc_li01",
            optional={
                "hhcomp": ["A1"],
                "statinfo": ["MED_EI"],
                "rskpovth": ["B_60"],
                "unit": ["NAC", "EUR"],
            },
            since=since,
            geo=EU_GEOS,
        )
        th = th[th[dimcol(th, "hhcomp", "hhtyp")] == "A1"]
        unit_col = dimcol(th, "unit", "currency")
        th = th.sort_values(unit_col, key=lambda s: s.map({"NAC": 0, "EUR": 1}))
        th = th.drop_duplicates(["iso3", "time"])
        parts.append(
            pd.DataFrame(
                {
                    "iso3": th["iso3"],
                    "period": th["time"],
                    "value": th["value"] / 12,
                    "statistic": "unexpected_expense_size",
                    "household_type": "reference amount",
                    "unit": th[unit_col] + " (monthly 60% poverty threshold, one person)",
                    "series": "ilc_li01:A1:MED_EI:B_60/12",
                }
            )
        )
    except FetchError as e:
        log.warning("ilc_li01 skipped: %s", e)
    return frame(
        pd.concat(parts),
        measure="emergency",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["mdes"],
    )

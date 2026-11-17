"""Income: earnings by age, household income, tax models, wage history,
pay mobility and raises."""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime

import pandas as pd

from econdata.clients import (
    EU_GEOS,
    col_like,
    dimcol,
    eurostat,
    fred,
    fred_annual_chained,
    oecd,
    oecd_rows,
    statcan,
    worldbank,
)
from econdata.config import COUNTRIES, NOTES, OECD_MEMBERS
from econdata.fetch import CachedFetcher, FetchError, is_json, is_zip
from econdata.tidy import frame

log = logging.getLogger("econ")


SES_STATS = {
    "MED_E_EUR": ("median", "EUR/month"),
    "D1_E_EUR": ("p10", "EUR/month"),
    "D9_E_EUR": ("p90", "EUR/month"),
    "MED_E_PPS": ("median", "PPS/month"),
    "D1_E_PPS": ("p10", "PPS/month"),
    "D9_E_PPS": ("p90", "PPS/month"),
}


def earnings_europe_ses(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "earn_ses_monthly",
        required={"nace_r2": ["B-S_X_O"], "isco08": ["TOTAL"], "worktime": ["FT"], "sex": ["T"]},
        optional={"indic_se": list(SES_STATS)},
        since=since,
    )
    df = df[df["indic_se"].isin(SES_STATS)]
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "age_band": df["age"],
            "statistic": df["indic_se"].map(lambda s: SES_STATS[s][0]),
            "unit": df["indic_se"].map(lambda s: SES_STATS[s][1]),
            "series": "earn_ses_monthly:" + df["indic_se"],
        }
    )
    ses = frame(
        out,
        measure="earnings_by_age",
        frequency="4-yearly",
        source="Eurostat SES",
        comparability_note=NOTES["ses"],
    )
    # Annual earnings INCLUDE bonuses and 13th/14th-month pay, which the October monthly
    # figures miss (annual / (monthly x 12) is 1.12 DE, 1.22 AT, 1.27 PT, 1.34 EL).
    # Kept as a separate measure and used as a per-country scaling factor.
    try:
        a = eurostat(
            f,
            "earn_ses_annual",
            required={
                "nace_r2": ["B-S_X_O"],
                "isco08": ["TOTAL"],
                "worktime": ["FT"],
                "sex": ["T"],
            },
            optional={"indic_se": ["MED_E_EUR", "D1_E_EUR", "D9_E_EUR"]},
            since=since,
        )
        a = a[a["indic_se"].isin(["MED_E_EUR", "D1_E_EUR", "D9_E_EUR"])]
        ann = frame(
            pd.DataFrame(
                {
                    "iso3": a["iso3"],
                    "period": a["time"],
                    "value": a["value"],
                    "age_band": a["age"],
                    "statistic": a["indic_se"].map(lambda x: SES_STATS[x][0]),
                    "unit": "EUR/year",
                    "series": "earn_ses_annual:" + a["indic_se"],
                }
            ),
            measure="earnings_annual_ses",
            frequency="4-yearly",
            source="Eurostat SES annual",
            comparability_note="Eurostat SES gross ANNUAL earnings incl. bonuses, full-time "
            "employees in employment the whole year.",
        )
        ses = pd.concat([ses, ann], ignore_index=True)
    except FetchError as e:
        log.warning("earn_ses_annual skipped: %s", e)
    return ses


# ASHE Table 6 = UK by age group (Table 3.4 is region x age and has NO UK-level age
# rows, which is why the old parser found nothing). Release URLs are discovered from
# the dataset's /data JSON; the list below is the fallback if discovery fails.
ONS = "https://www.ons.gov.uk"


ASHE6_DATASET = (
    "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/datasets/agegroupashetable6"
)


ASHE6_FALLBACK = ["2026provisional", "2025revised"]


ASHE_PCTS = {
    "10": "p10",
    "20": "p20",
    "25": "p25",
    "30": "p30",
    "40": "p40",
    "60": "p60",
    "70": "p70",
    "75": "p75",
    "80": "p80",
    "90": "p90",
}


def ons_editions(f: CachedFetcher, dataset_uri: str) -> list[str]:
    """Edition URIs of an ONS dataset, newest first (e.g. .../2026provisional)."""
    try:
        js = json.loads(
            f.get(
                ONS + dataset_uri + "/data",
                label=f"ons:list:{dataset_uri[-30:]}",
                validate=is_json,
            )
        )
        return [d["uri"] for d in js.get("datasets", [])]
    except (FetchError, ValueError, KeyError) as e:
        log.warning("ONS edition discovery failed for %s: %s", dataset_uri, e)
        return []


def ons_edition_files(f: CachedFetcher, edition_uri: str) -> list[str]:
    try:
        js = json.loads(
            f.get(
                ONS + edition_uri + "/data",
                label=f"ons:edition:{edition_uri[-30:]}",
                validate=is_json,
            )
        )
        return [edition_uri + "/" + d["file"] for d in js.get("downloads", [])]
    except (FetchError, ValueError, KeyError):
        return []


def parse_ashe_xlsx(content: bytes, year: str) -> pd.DataFrame:
    """Full-Time sheet of ASHE Table 6.1a: rows 'All employees', '16-17b', '18-21',
    '22-29', ..., '60+'; header row has Median and percentile columns 10..90."""
    xl = pd.ExcelFile(io.BytesIO(content))
    sheet = next((s for s in xl.sheet_names if s.strip().lower() == "full-time"), None)
    if sheet is None:
        raise FetchError(f"ASHE: no Full-Time sheet in {xl.sheet_names}")
    grid = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str)
    hdr_row = next(
        i for i, r in grid.iterrows() if any(str(v).strip().lower() == "median" for v in r.values)
    )
    header = [re.sub(r"\.0$", "", str(v).strip()) for v in grid.iloc[hdr_row].values]
    rows = []
    for i in range(hdr_row + 1, len(grid)):
        desc = str(grid.iat[i, 0]).strip()
        if desc.lower().startswith("all employees"):
            band = "total"
        elif m := re.fullmatch(r"(\d{2})\s*-\s*(\d{2})[a-z]?", desc):
            band = f"{m.group(1)}-{m.group(2)}"
        elif m := re.fullmatch(r"(\d{2})\s*\+[a-z]?", desc):
            band = f"{m.group(1)}+"
        else:
            continue
        for j, col in enumerate(header):
            stat = "median" if col.lower() == "median" else ASHE_PCTS.get(col)
            if stat:
                rows.append(
                    {
                        "iso3": "GBR",
                        "period": year,
                        "age_band": band,
                        "statistic": stat,
                        "value": pd.to_numeric(grid.iat[i, j], errors="coerce"),
                    }
                )
    if not rows:
        raise FetchError(f"ASHE: no age rows in sheet {sheet}")
    return pd.DataFrame(rows)


def earnings_uk_ashe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    editions = ons_editions(f, ASHE6_DATASET) or [ASHE6_DATASET + "/" + e for e in ASHE6_FALLBACK]
    parts, seen_years = [], set()
    # newest first; one edition per year; only the 4 newest (older zips use other file
    # names and crawling them all trips ONS rate limits: HTTP 429)
    for ed in editions[:4]:
        year = re.search(r"/(\d{4})[a-z]*$", ed)
        if not year or year.group(1) in seen_years or (since and int(year.group(1)) < since):
            continue
        year = year.group(1)
        files = [u for u in ons_edition_files(f, ed) if u.endswith(".zip")] or [
            f"{ed}/ashetable6{ed.rsplit('/', 1)[1]}.zip"
        ]
        try:
            raw = f.get(ONS + "/file?uri=" + files[0], label=f"ons:ashe6:{year}", validate=is_zip)
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                name = next(
                    n
                    for n in z.namelist()
                    if re.search(r"weekly pay\s*-\s*gross", n, re.I)
                    and n.lower().endswith(".xlsx")
                    and " cv" not in n.lower()
                )
                parts.append(parse_ashe_xlsx(z.read(name), year))
                seen_years.add(year)
        except (FetchError, StopIteration, zipfile.BadZipFile, ValueError) as e:
            log.warning("ASHE %s skipped: %s", ed, e)
    if not parts:
        raise FetchError("ASHE: nothing parsed")
    df = pd.concat(parts).assign(unit="GBP/week", series="ASHE Table 6.1a Full-Time")
    return frame(
        df,
        measure="earnings_by_age",
        frequency="A",
        source="ONS ASHE",
        comparability_note=NOTES["ashe"],
    )


CPS_SERIES = {  # titles verified on FRED
    "LEU0252881500A": ("16 years and over", "median"),
    "LEU0252911300A": ("16 years and over", "p25"),
    "LEU0252911400A": ("16 years and over", "p75"),
    "LEU0252887100A": ("20 to 24 years", "median"),
    "LEU0252888500A": ("25 to 34 years", "median"),
    "LEU0252889100A": ("35 to 44 years", "median"),
    "LEU0252889700A": ("45 to 54 years", "median"),
    "LEU0252890900A": ("55 to 64 years", "median"),
    "LEU0252891500A": ("65 years and over", "median"),
    "LEU0252887900A": ("25 to 54 years", "median"),
}


def earnings_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, (band, stat) in CPS_SERIES.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        parts.append(
            d.assign(
                iso3="USA", period=d["date"].str[:4], age_band=band, statistic=stat, series=sid
            )
        )
    if not parts:
        raise FetchError("No CPS series fetched")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].astype(int) >= since]
    return frame(
        df,
        measure="earnings_by_age",
        frequency="A",
        unit="USD/week",
        source="BLS CPS (via FRED)",
        comparability_note=NOTES["cps"],
    )


def earnings_canada_lfs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = statcan(
        f,
        "14100417",
        "Canada",
        {
            "wage": "median weekly",
            "type of work": "full-time",
            "occupation": "total",
            "gender": "total",
            "age group": None,
        },
    )
    age_col = next(c for c in d.columns if c.lower().startswith("age"))
    out = pd.DataFrame(
        {
            "iso3": "CAN",
            "period": d["REF_DATE"].str[:4],
            "value": d["VALUE"],
            "age_band": d[age_col],
            "statistic": "median",
            "series": "14-10-0417-01",
        }
    )
    if since:
        out = out[out["period"].astype(int) >= since]
    return frame(
        out,
        measure="earnings_by_age",
        frequency="A",
        unit="CAD/week",
        source="StatCan LFS",
        comparability_note=NOTES["lfs_ca"],
    )


SILC_UNITS = ["EUR", "PPS", "NAC"]


# Eurostat restructured EU-SILC (2025): indic_il -> statinfo, MED_E -> MED_EI,
# MEI_E -> MEAN_EI, hhtyp -> hhcomp, quantile -> quant_inc, currency -> unit.
SILC_MEDIAN = {"MED_EI", "MED_E"}


SILC_STAT = {"MED_EI": "median", "MED_E": "median", "MEAN_EI": "mean", "MEI_E": "mean"}


def _silc_rows(df: pd.DataFrame, **extra):
    st = dimcol(df, "statinfo", "indic_il")
    unit_col = dimcol(df, "unit", "currency")
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": df[st].map(SILC_STAT).fillna(df[st]),
            "unit": df[unit_col] + "/year (equivalised)",
            **extra,
        }
    )
    return out


def _silc_median(df: pd.DataFrame) -> pd.DataFrame:
    return df[df[dimcol(df, "statinfo", "indic_il")].isin(SILC_MEDIAN)]


def income_silc_by_age(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "ilc_di03",
        optional={"statinfo": ["MED_EI"], "sex": ["T"], "unit": SILC_UNITS},
        since=since,
        geo=EU_GEOS,
    )
    df = _silc_median(df)  # mean kept out on purpose (outliers)
    st = dimcol(df, "statinfo", "indic_il")
    out = _silc_rows(df, age_band=df["age"], series="ilc_di03:" + df[st])
    return frame(
        out,
        measure="household_income",
        household_type="all persons",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["silc"],
    )


def income_silc_by_household(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "ilc_di04",
        optional={"statinfo": ["MED_EI"], "unit": SILC_UNITS},
        since=since,
        geo=EU_GEOS,
    )
    df = _silc_median(df)
    hh = dimcol(df, "hhcomp", "hhtyp")
    out = _silc_rows(df, household_type=df[hh + "_label"], series="ilc_di04:" + df[hh])
    return frame(
        out,
        measure="household_income",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["silc"],
    )


def income_silc_by_urbanisation(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "ilc_di17",
        optional={"statinfo": ["MED_EI"], "sex": ["T"], "unit": SILC_UNITS},
        since=since,
        geo=EU_GEOS,
    )
    df = _silc_median(df)
    out = _silc_rows(
        df,
        area_type=df["deg_urb_label"],
        age_band=df["age"] if "age" in df else "total",
        series="ilc_di17:" + df["deg_urb"],
    )
    return frame(
        out,
        measure="household_income",
        household_type="all persons",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["silc"],
    )


def income_silc_distribution(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Percentile cut-offs (P1..P99, deciles, quartiles) of equivalised income:
    the closest thing to a raw distribution available for free."""
    df = eurostat(
        f, "ilc_di01", optional={"statinfo": ["TC"], "unit": SILC_UNITS}, since=since, geo=EU_GEOS
    )
    df = df[df[dimcol(df, "statinfo", "indic_il")] == "TC"]
    unit_col = dimcol(df, "unit", "currency")
    qcol = dimcol(df, "quant_inc", "quantile")

    def q2stat(q: str) -> str:
        q = str(q).upper()
        if m := re.fullmatch(r"P(\d{1,2})", q):
            return f"p{int(m.group(1))}"
        if m := re.fullmatch(r"D(\d)", q):
            return f"p{int(m.group(1)) * 10}"
        if m := re.fullmatch(r"Q(\d)", q):
            return f"p{int(m.group(1)) * 25}"
        if m := re.fullmatch(r"QU(\d)", q):
            return f"p{int(m.group(1)) * 20}"
        return q.lower()

    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": df[qcol].map(q2stat),
            "unit": df[unit_col] + "/year (equivalised)",
            "series": "ilc_di01:TC:" + df[qcol],
        }
    )
    out = out.drop_duplicates(["iso3", "period", "statistic", "unit"])
    return frame(
        out,
        measure="household_income_distribution",
        household_type="all persons",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["silc"],
    )


IDD_DIMS = [
    "REF_AREA",
    "FREQ",
    "MEASURE",
    "STATISTICAL_OPERATION",
    "UNIT_MEASURE",
    "AGE",
    "METHODOLOGY",
    "DEFINITION",
    "POVERTY_LINE",
]


def income_oecd_idd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(
        f,
        "OECD.WISE.INE,DSD_WISE_IDD@DF_IDD",
        IDD_DIMS,
        {
            "REF_AREA": areas,
            "FREQ": ["A"],
            "MEASURE": ["INC_DISP", "D9_1_INC_DISP", "D5_1_INC_DISP"],
        },
        since,
    )
    if "METHODOLOGY" in df and (df["METHODOLOGY"] == "METH2012").any():
        df = df[df["METHODOLOGY"] == "METH2012"]
    if "DEFINITION" in df and (df["DEFINITION"] == "D_CUR").any():
        df = df[df["DEFINITION"] == "D_CUR"]
    stat = df.get("STATISTICAL_OPERATION", pd.Series("", index=df.index)).fillna("")
    meas = df["MEASURE"]
    statistic = meas.where(meas != "INC_DISP", "INC_DISP_" + stat).str.lower()
    statistic = statistic.replace(
        {
            "inc_disp_median": "median",
            "inc_disp_mean": "mean",
            "inc_disp__z": "mean",
            "d9_1_inc_disp": "p90_p10_ratio",
            "d5_1_inc_disp": "p50_p10_ratio",
        }
    )
    out = oecd_rows(
        df,
        {
            "statistic": statistic,
            "age_band": df.get("AGE", "total"),
            "unit": df.get("UNIT_MEASURE", "") + "/year (equivalised)",
            "series": "IDD:" + meas,
        },
    )
    out = out[~out["statistic"].isin(["mean"])]  # medians & ratios only
    return frame(
        out,
        measure="household_income",
        household_type="all persons",
        frequency="A",
        source="OECD IDD",
        comparability_note=NOTES["idd"],
    )


TW_DIMS = [
    "REF_AREA",
    "MEASURE",
    "UNIT_MEASURE",
    "HOUSEHOLD_TYPE",
    "INCOME_PRINCIPAL",
    "INCOME_SPOUSE",
    "FREQ",
]


def net_income_taxing_wages(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(
        f,
        "OECD.CTP.TPS,DSD_TAX_WAGES_COMP@DF_TW_COMP",
        TW_DIMS,
        {
            "REF_AREA": areas,
            "MEASURE": [
                "AV_ITR",
                "AV_R_EMPEE_SSC",
                "AV_RITEESSC",
                "NPMTR_PE",
                "GEBT",
                "NIAT",
                "AV_TW",
                "NPATR",
            ],
            "UNIT_MEASURE": ["XDC", "PT_WG_EARN_G", "PT_COS_LB"],
            "HOUSEHOLD_TYPE": ["S_C0", "C_C0", "C_C2"],
            "INCOME_PRINCIPAL": ["AW67", "AW100", "AW167"],
            "FREQ": ["A"],
        },
        since,
    )
    hh = (
        df["HOUSEHOLD_TYPE"].fillna("")
        + "|"
        + df["INCOME_PRINCIPAL"].fillna("")
        + "+"
        + df["INCOME_SPOUSE"].fillna("")
    )
    out = oecd_rows(
        df,
        {
            "statistic": df["MEASURE"],
            "household_type": hh,
            "unit": df["UNIT_MEASURE"],
            "series": "TW_COMP:" + df["MEASURE"],
        },
    )
    return frame(
        out,
        measure="net_income_tax_model",
        frequency="A",
        source="OECD Taxing Wages",
        comparability_note=NOTES["taxing_wages"],
    )


def net_income_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "earn_nt_net",
        optional={"currency": ["EUR", "NAC"], "estruct": ["GRS", "TAX", "SOC", "FAM", "NET"]},
        since=since,
        geo=EU_GEOS,
    )
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": df["estruct"],
            "household_type": df["ecase_label"],
            "unit": df["currency"] + "/year",
            "series": "earn_nt_net:" + df["ecase"],
        }
    )
    return frame(
        out,
        measure="net_income_tax_model",
        frequency="A",
        source="Eurostat net earnings",
        comparability_note=NOTES["earn_nt"],
    )


AVW_DIMS = [
    "REF_AREA",
    "MEASURE",
    "UNIT_MEASURE",
    "PAY_PERIOD",
    "PRICE_BASE",
    "AGGREGATION_OPERATION",
    "SEX",
]


def avg_wage_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(f, "OECD.ELS.SAE,DSD_EARNINGS@AV_AN_WAGE", AVW_DIMS, {"REF_AREA": areas}, since)
    if "SEX" in df and (df["SEX"] == "_T").any():
        df = df[df["SEX"].isin(["_T", "_Z"])]
    unit = df["UNIT_MEASURE"].fillna("") + "|" + df.get("PRICE_BASE", "").fillna("")
    out = oecd_rows(
        df,
        {
            "statistic": "mean",
            "unit": unit,
            "series": "AV_AN_WAGE",
        },
    )
    return frame(
        out,
        measure="avg_wage",
        frequency="A",
        source="OECD average wages",
        comparability_note=NOTES["avg_wage"],
    )


def avg_salary_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "nama_10_fte", since=since, geo=EU_GEOS)
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": "mean",
            "unit": df["unit"] + "/year",
            "series": "nama_10_fte",
        }
    )
    return frame(
        out,
        measure="avg_wage",
        frequency="A",
        source="Eurostat national accounts",
        comparability_note=NOTES["nama_fte"],
    )


def cpi_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = worldbank(f, "FP.CPI.TOTL", list(COUNTRIES), since)
    d = d.rename(columns={"date": "period"}).assign(
        statistic="cpi_index", unit="2010=100", series="FP.CPI.TOTL"
    )
    out = frame(
        d,
        measure="price_level",
        frequency="A",
        source="World Bank WDI",
        comparability_note="Consumer price index, national definitions.",
    )
    try:
        out = pd.concat([out, cpi_us_gap_fill(f, out)], ignore_index=True)
    except FetchError as e:
        log.warning("US CPI gap fill skipped: %s", e)
    return out


def cpi_us_gap_fill(f: CachedFetcher, wb: pd.DataFrame) -> pd.DataFrame:
    """The World Bank (IMF) has no 2025 CPI for the US: BLS never collected October 2025
    prices (federal shutdown), so there is no official 12-month average. Fill complete
    past years from the monthly CPI-U (FRED CPIAUCNS, average of the months published),
    chained to the World Bank index at the last year both have."""
    u = wb[(wb["iso3"] == "USA")].set_index("year")["value"]
    if u.empty:
        return wb.iloc[0:0]
    a, base, k = fred_annual_chained(f, "CPIAUCNS", u)
    rows = [
        {
            "iso3": "USA",
            "period": str(y),
            "value": a.loc[y, "mean"] * k,
            "statistic": "cpi_index",
            "unit": "2010=100",
            "series": "CPIAUCNS",
            "estimated": a.loc[y, "size"] < 12,
            "method": f"BLS CPI-U, mean of {a.loc[y, 'size']} months, chained to WDI {base}",
        }
        for y in a.index
        if y > base and y < datetime.now().year and y not in u.index
    ]
    return (
        frame(
            pd.DataFrame(rows),
            measure="price_level",
            frequency="A",
            source="World Bank WDI (gap filled from BLS CPI-U)",
            comparability_note="Consumer price index, national definitions.",
        )
        if rows
        else wb.iloc[0:0]
    )


WGT_SERIES = {  # verified on FRED (Atlanta Fed Wage Growth Tracker)
    "FRBATLWGT12MMUMHGO": "overall",
    "FRBATLWGT12MMUMHWGJST": "job stayer",
    "FRBATLWGT12MMUMHWGJSW": "job switcher",
    "FRBATLWGT12MMUMHWGA1644Y": "age 16-24",
    "FRBATLWGT12MMUMHWGA2554Y": "age 25-54",
    "FRBATLWGT12MMUMHWG55O": "age 55+",
    "FRBATLWGT12MMUMHWGMSA": "metro (MSA)",
    "FRBATLWGT12MMUMHWGNMSA": "non-metro",
}


def wage_growth_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, group in WGT_SERIES.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("WGT %s skipped: %s", sid, e)
            continue
        band = group.replace("age ", "") if group.startswith("age") else "total"
        parts.append(
            d.assign(
                iso3="USA",
                period=d["date"].str[:7],
                statistic=group,
                age_band=band,
                series=sid,
                area_type=group if "metro" in group or "MSA" in group else pd.NA,
            )
        )
    if not parts:
        raise FetchError("No Wage Growth Tracker series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(
        df,
        measure="wage_growth_individual",
        frequency="M",
        unit="% y/y (median, same person)",
        source="Atlanta Fed WGT (via FRED)",
        comparability_note=NOTES["wgt"],
    )


def income_transitions_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "ilc_lvhl34", optional={"sex": ["T"]}, since=since, geo=EU_GEOS)
    trans_col = next(
        (
            c
            for c in df.columns
            if not c.endswith("_label")
            and c not in ("freq", "sex", "unit", "geo", "time", "value", "flag", "iso3")
            and df[c].astype(str).str.contains(r"UP|DW|CHG|NONE", regex=True).any()
        ),
        None,
    )
    q_col = next(
        (
            c
            for c in df.columns
            if not c.endswith("_label")
            and c not in ("freq", "sex", "unit", "geo", "time", "value", "flag", "iso3", trans_col)
        ),
        None,
    )
    if trans_col is None:
        raise FetchError(f"ilc_lvhl34: transition dimension not found in {list(df.columns)}")
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": df[trans_col + "_label"],
            "category": df[q_col + "_label"] if q_col else "total",
            "unit": "% of employees",
            "series": "ilc_lvhl34:" + df[trans_col],
        }
    )
    return frame(
        out,
        measure="income_transitions",
        frequency="A",
        source="Eurostat EU-SILC longitudinal",
        comparability_note=NOTES["lvhl"],
    )


def income_mobility_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # 6.4M raw rows (42 regions x 4 income concepts x 35 groups x 28 statistics);
    # keep Canada, employment income, decile/age/couple groups, mobility statistics.
    d = statcan(
        f,
        "11100061",
        "Canada",
        {
            "income concept": "employment income",
            "groups": ["total, all deciles", "decile", "years", "couple families"],
            "statistic": [
                "median income",
                "rising income",
                "falling income",
                "same decile",
                "higher deciles",
                "lower deciles",
                "moved up by",
                "moved down by",
            ],
        },
        since=since,
    )
    gcol, scol = col_like(d, "groups"), col_like(d, "statistic")
    out = pd.DataFrame(
        {
            "iso3": "CAN",
            "period": d["REF_DATE"].str[-4:],
            "value": d["VALUE"],
            "statistic": d[scol],
            "category": d[gcol],
            "age_band": d[gcol].where(d[gcol].str.contains("years"), "total"),
            "unit": d.get("UOM", ""),
            "series": "11-10-0061-01",
        }
    )
    return frame(
        out,
        measure="income_transitions",
        frequency="A",
        source="StatCan LAD",
        comparability_note="StatCan one-year income mobility (tax-file based).",
    )

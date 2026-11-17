"""Cost of living: household spending surveys."""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime

import pandas as pd

from econdata.clients import EU_GEOS, _annual, col_like, eurostat, fred, statcan, statcan_dims
from econdata.config import NOTES
from econdata.fetch import CachedFetcher, FetchError, is_json, is_zip
from econdata.sources.income import ONS, ons_edition_files, ons_editions
from econdata.tidy import frame

log = logging.getLogger("econ")


# Map COICOP (1999) codes to budget categories. Level-3/4 codes refine level-2.
COICOP_CATS = {
    "CP011": "groceries",
    "CP012": "groceries",
    "CP02": "alcohol_tobacco",
    "CP03": "clothing",
    "CP041": "rent_actual",
    "CP042": "rent_imputed_owners",
    "CP043": "home_maintenance",
    "CP044": "water_refuse",
    "CP045": "energy_home",
    "CP05": "furnishings_household",
    "CP06": "health_out_of_pocket",
    "CP071": "vehicle_purchase",
    "CP0722": "fuel_vehicle",
    "CP0721": "vehicle_maintenance",
    "CP0723": "vehicle_maintenance",
    "CP0724": "vehicle_other",
    "CP073": "transport_services",
    "CP08": "phone_internet",
    "CP09": "recreation",
    "CP10": "education",
    "CP11": "restaurants_hotels",
    "CP1252": "insurance_home",
    "CP1253": "insurance_health",
    "CP1254": "insurance_vehicle",
    "CP125": "insurance_other",
    "CP12": "personal_misc",
}


LEVEL2 = [
    "CP01",
    "CP02",
    "CP03",
    "CP04",
    "CP05",
    "CP06",
    "CP07",
    "CP08",
    "CP09",
    "CP10",
    "CP11",
    "CP12",
]


HH_COUPLE = [
    r"two adults younger than 65",
    r"^two adults$",
    r"two adults(?!.*child)(?!.*65 years or over)",
]


HH_1CHILD = [r"two adults (?:with|and) one dependent child"]


HH_ANYCHILD = [r"^two adults with dependent children$"]


HBS_AVG_CHILDREN = 1.7  # approx. EU mean children in 2-adult households with children


SHS_AVG_CHILDREN = 1.8  # approx. Canada, couples with children


CE_AVG_CHILDREN = 1.9  # BLS CE: married couple with children (~1.9 children)


HH_EXCLUDE = r"at least one aged 65|65 years or over"


def _hbs(f: CachedFetcher, code: str, since: int | None) -> pd.DataFrame:
    return eurostat(f, code, since=since, geo=EU_GEOS)


def spending_eu_hbs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Raw HBS tables; the couple budget is assembled in derive_couple_budget()."""
    parts = []
    for code, kind in [
        ("hbs_exp_t134", "total_by_hhtype"),
        ("hbs_exp_t136", "total_by_urb"),
        ("hbs_str_t224", "share_by_hhtype"),
        ("hbs_str_t226", "share_by_urb"),
        ("hbs_exp_t121", "level_by_coicop"),
    ]:
        try:
            df = _hbs(f, code, since)
        except FetchError as e:
            log.warning("HBS %s skipped: %s", code, e)
            continue
        hh_col = col_like(df, "hhtyp", "hhcomp")
        hh_col = hh_col if hh_col and not hh_col.endswith("_label") else None
        out = pd.DataFrame(
            {
                "iso3": df["iso3"],
                "period": df["time"],
                "value": df["value"],
                "statistic": kind,
                "category": df["coicop"] if "coicop" in df else "TOTAL",
                "household_type": df[hh_col + "_label"] if hh_col else "all households",
                "area_type": df["deg_urb_label"] if "deg_urb" in df else "all areas",
                "unit": df["unit"] if "unit" in df else "",
                "series": code + (":" + df[hh_col] if hh_col else ""),
            }
        )
        parts.append(out)
    if not parts:
        raise FetchError("No HBS tables fetched")
    return frame(
        pd.concat(parts),
        measure="household_spending_raw",
        frequency="5-yearly",
        source="Eurostat HBS",
        comparability_note=NOTES["hbs"],
    )


HICP_CODES = [
    "CP00",
    "CP01",
    "CP011",
    "CP012",
    "CP02",
    "CP03",
    "CP04",
    "CP041",
    "CP043",
    "CP044",
    "CP045",
    "CP05",
    "CP06",
    "CP07",
    "CP071",
    "CP072",
    "CP0721",
    "CP0722",
    "CP0723",
    "CP0724",
    "CP073",
    "CP08",
    "CP09",
    "CP10",
    "CP11",
    "CP12",
    "CP124",
    "CP125",
    "CP1252",
    "CP1253",
    "CP1254",
]


# US CPI components (FRED, seasonally adjusted where available) for each budget line
# FRED ids; "BLS:" = only on the BLS public API (car insurance, day care: not on FRED)
US_CPI_PARTS = {
    "all": "CPIAUCSL",
    "groceries": "CUSR0000SAF11",
    "restaurants_hotels": "CUSR0000SEFV",
    "alcohol_tobacco": "CUSR0000SAF116",
    "clothing": "CPIAPPSL",
    "furnishings_household": "CUSR0000SAH3",
    "household_operations": "CUSR0000SAH3",
    "utilities_phone": "CUSR0000SAH2",
    "health_out_of_pocket": "CPIMEDSL",
    "insurance_health": "CUSR0000SAM2",
    "recreation": "CPIRECSL",
    "education": "CUSR0000SAE1",
    "transport_services": "CUSR0000SAS4",
    "personal_misc": "CPIOGSSL",
    "vehicle_purchase": "CUSR0000SETA02",
    "vehicle_running": "CUSR0000SETD",
    "insurance_vehicle": "BLS:CUUR0000SETE",
    "childcare": "BLS:CUUR0000SEEB03",
    "rent": "CUSR0000SEHA",
}


BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"


def bls_series(f: CachedFetcher, sid: str, first: int = 1995) -> pd.DataFrame:
    """A BLS series (public API v1, no key: 10 years per request) as date/value."""
    rows = []
    last = datetime.now().year
    for y0 in range(first, last + 1, 10):
        js = json.loads(
            f.get(
                BLS_V1,
                label=f"bls:{sid}:{y0}",
                validate=is_json,
                timeout=60,
                method="POST",
                json_body={
                    "seriesid": [sid],
                    "startyear": str(y0),
                    "endyear": str(min(y0 + 9, last)),
                },
            )
        )
        if js.get("status") != "REQUEST_SUCCEEDED":
            raise FetchError(f"BLS {sid}: {js.get('message')}")
        for ser in js["Results"]["series"]:
            for d in ser["data"]:
                if str(d["period"]).startswith("M") and d["period"] != "M13":
                    rows.append({"date": f"{d['year']}-{d['period'][1:]}-01", "value": d["value"]})
    if not rows:
        raise FetchError(f"BLS {sid}: no data")
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date")


def cpi_series(f: CachedFetcher, sid: str) -> pd.DataFrame:
    return bls_series(f, sid[4:]) if sid.startswith("BLS:") else fred(f, sid)


def hicp_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "prc_hicp_aind",
        optional={"unit": ["INX_A_AVG"], "coicop": HICP_CODES},
        since=since,
        geo=EU_GEOS,
    )
    df = df[df["unit"] == "INX_A_AVG"] if "unit" in df else df
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": "hicp_index",
            "category": df["coicop"],
            "unit": "index",
            "series": "prc_hicp_aind:" + df["coicop"],
        }
    )
    return frame(
        out,
        measure="price_level",
        frequency="A",
        source="Eurostat HICP",
        comparability_note="Harmonised index of consumer prices, annual average.",
    )


def prices_us_cpi_parts(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US CPI by component (annual averages of complete years), to see which costs have
    been rising faster than prices in general."""
    parts = []
    for key, sid in US_CPI_PARTS.items():
        try:
            d = cpi_series(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        a = _annual(d)  # complete years (one missing month filled)
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "USA",
                    "period": a.index.astype(str),
                    "value": a.values,
                    "category": key,
                    "series": f"FRED {sid}",
                }
            )
        )
    if not parts:
        raise FetchError("no US CPI components")
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="price_level",
        statistic="cpi_component",
        unit="index",
        frequency="A",
        source="BLS CPI by component (FRED)",
    )


# BLS relative importance of CPI-U components, December 2024 (shares of all items):
# shelter 35.4 = owners' equivalent rent 26.2 + rent 7.6 + lodging away 1.4 + insurance
HCI_W = {"ex_shelter": 0.646, "rent": 0.090, "owners": 0.264}


HCI_RATE_YEARS = 7  # existing loans: the 30-year rate averaged over this many years


def household_cost_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What US households pay, not what the CPI's housing concept says: the CPI less
    shelter, renters' rents, and for owners what owning costs in interest - home prices x
    mortgage rates (instead of the CPI's imputed 'owners' equivalent rent'; as the UK's
    Household Costs Indices, and Bolhuis, Cramer, Schulz & Summers 2024, 'The Cost of
    Money is Part of the Cost of Living'). Existing loans carry the 30-year rate averaged
    over 7 years (most owners locked in or refinanced earlier). Weights: BLS relative
    importance, Dec 2024. Rows: 12-month, 5- and 10-year rates of this index and the CPI."""
    rd = {}
    for k, sid in (
        ("cpi", "CPIAUCSL"),
        ("ex_shelter", "CUSR0000SA0L2"),
        ("rent", "CUSR0000SEHA"),
        ("hpi", "CSUSHPISA"),
        ("m", "MORTGAGE30US"),
    ):
        d = fred(f, sid)
        x = pd.Series(
            pd.to_numeric(d["value"], errors="coerce").to_numpy(), index=pd.to_datetime(d["date"])
        ).dropna()
        rd[k] = x.resample("MS").mean()
    d = pd.concat(rd, axis=1, sort=True)
    d[["cpi", "ex_shelter", "rent"]] = d[["cpi", "ex_shelter", "rent"]].interpolate(
        limit=2, limit_area="inside"
    )  # October 2025: not collected (shutdown)
    d["m_stock"] = d["m"].rolling(12 * HCI_RATE_YEARS, min_periods=12 * 5).mean()
    d["owners"] = d["hpi"] * d["m_stock"]
    d = d.dropna(subset=["cpi", "ex_shelter", "rent", "owners"])
    if len(d) < 12 * 11:
        raise FetchError("household cost index: too little overlapping history")
    last = d.index[-1]

    def growth(k: str, months: int) -> float:
        return float(d[k].iloc[-1] / d[k].iloc[-1 - months])

    rows = []
    for n, lab in ((1, "12m"), (5, "5y"), (10, "10y")):
        # chained yearly (same month), the weights reset each year
        hci = cpi = 1.0
        for j in range(n):
            a, b = -1 - 12 * (j + 1), -1 - 12 * j
            hci *= sum(w * d[k].iloc[b] / d[k].iloc[a] for k, w in HCI_W.items())
            cpi *= d["cpi"].iloc[b] / d["cpi"].iloc[a]
        for item, v in (("hci", hci), ("cpi", cpi)):
            rows.append(
                {
                    "iso3": "USA",
                    "period": str(last.date())[:7],
                    "category": f"{item}_{lab}",
                    "value": 100 * (v ** (1 / n) - 1),
                    "method": "household cost index: CPI less shelter (FRED "
                    "CUSR0000SA0L2), rent (CUSR0000SEHA), owners' interest = Case-Shiller "
                    f"(CSUSHPISA) x 30-year rate, {HCI_RATE_YEARS}-year average "
                    "(MORTGAGE30US); BLS weights Dec 2024"
                    if item == "hci"
                    else "CPI-U (FRED CPIAUCSL)",
                }
            )
    return frame(
        pd.DataFrame(rows),
        measure="price_level",
        statistic="household_cost",
        unit="%/yr",
        frequency="M",
        source="FRED (BLS, S&P Case-Shiller, Freddie Mac)",
    )


def inflation_outlook_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What bond markets expect US inflation to be: 5-year breakeven (TIPS vs nominal
    Treasuries) and the 5-year, 5-year-forward rate (FRED, daily -> monthly means)."""
    parts = []
    for sid, stat in (("T5YIE", "breakeven_5y"), ("T5YIFR", "forward_5y5y")):
        d = fred(f, sid)
        m = d.assign(period=d["date"].str[:7]).groupby("period")["value"].mean()
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "USA",
                    "period": m.index,
                    "value": m.values,
                    "statistic": stat,
                    "series": f"FRED {sid}",
                }
            )
        )
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="inflation_outlook",
        unit="% p.a.",
        frequency="M",
        source="FRED (Federal Reserve Board)",
    )


CE_ITEMS = [
    "TOTALEXP",
    "FOODHOME",
    "FOODAWAY",
    "ALCBEVG",
    "HOUSING",
    "SHELTER",
    "OWNDWELL",
    "RNTDWELL",
    "UTILS",
    "HHOPER",
    "HKPGSUPP",
    "HHFURNSH",
    "APPAREL",
    "TRANS",
    "VEHPURCH",
    "GASOIL",
    "VEHOTHER",
    "VEHINSUR",
    "PUBTRANS",
    "HEALTH",
    "HLTHINSR",
    "ENTRTAIN",
    "PERSCARE",
    "EDUCATN",
    "MISC",
    "CASHCONT",
    "PERINSPN",
]


CE_GROUPS = {
    "LB0603M": "married couple only",
    "LB0605M": "married couple, oldest child under 6",
    "LB0606M": "married couple, oldest child 6-17",
}


CE_AGE = {"LB0403M": "age 25-34", "LB0404M": "age 35-44"}


def spending_us_ce(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    combos = [(i, g, lab) for i in CE_ITEMS for g, lab in CE_GROUPS.items()]
    combos += [("TOTALEXP", g, lab) for g, lab in CE_AGE.items()]
    for item, grp, lab in combos:
        sid = f"CXU{item}{grp}"
        try:
            d = fred(f, sid)
        except FetchError:
            continue
        parts.append(
            d.assign(
                iso3="USA", period=d["date"].str[:4], category=item, household_type=lab, series=sid
            )
        )
    if not parts:
        raise FetchError("No CE series on FRED")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].astype(int) >= since]
    return frame(
        df,
        measure="household_spending",
        statistic="mean",
        frequency="A",
        unit="USD/year",
        area_type="all areas",
        source="BLS CE (via FRED)",
        comparability_note=NOTES["ce"],
    )


def spending_canada_shs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for pid, dim_kw, kind in [
        ("11100224", "household type", "household_type"),
        ("11100226", "size of area", "area_type"),
    ]:
        try:
            d = statcan(f, pid, "Canada", since=since)
        except FetchError as e:
            log.warning("SHS %s skipped: %s", pid, e)
            continue
        cat_col = col_like(d, "expenditure", "spending")
        grp_col = col_like(d, dim_kw)
        stat_col = col_like(d, "statistic")
        if stat_col:
            avg = d[stat_col].str.contains("average", case=False, na=False)
            if avg.any():
                d = d[avg]
        if cat_col is None or grp_col is None:
            log.warning("SHS %s: unexpected columns %s", pid, statcan_dims(d))
            continue
        row = {
            "iso3": "CAN",
            "period": d["REF_DATE"].str[:4],
            "value": d["VALUE"],
            "category": d[cat_col],
            "series": pid,
        }
        row[kind] = d[grp_col]
        parts.append(pd.DataFrame(row))
    if not parts:
        raise FetchError("No SHS tables parsed")
    return frame(
        pd.concat(parts),
        measure="household_spending",
        statistic="mean",
        frequency="A",
        unit="CAD/year",
        source="StatCan SHS",
        comparability_note=NOTES["shs"],
    )


# Household composition is Table A23, in WORKBOOK 2 ("expenditure by income"), not
# workbook 4. Editions (fye2025, fye2026, ...) are discovered from the /data JSON.
FS_WB2 = (
    "/peoplepopulationandcommunity/personalandhouseholdfinances/expenditure/datasets/"
    "familyspendingworkbook2expenditurebyincome"
)


FS_WB1 = (
    "/peoplepopulationandcommunity/personalandhouseholdfinances/expenditure/datasets/"
    "familyspendingworkbook1detailedexpenditureandtrends"
)


def _ons_latest_xlsx(f: CachedFetcher, dataset: str, default_file: str) -> tuple[bytes, str]:
    eds = ons_editions(f, dataset) or [dataset + "/fye2025"]
    for ed in eds[:2]:
        files = [u for u in ons_edition_files(f, ed) if u.endswith(".xlsx")] or [
            f"{ed}/{default_file}"
        ]
        try:
            return f.get(
                ONS + "/file?uri=" + files[0], label=f"ons:{ed[-40:]}", validate=is_zip
            ), ed.rsplit("/", 1)[1]
        except FetchError as e:
            log.warning("ONS %s: %s", ed, e)
    raise FetchError(f"ONS {dataset}: no downloadable edition")


def _num(s) -> float:
    return pd.to_numeric(str(s).replace(",", "").strip(), errors="coerce")


def parse_fs_a23(content: bytes) -> list[dict]:
    """Table A23: the column header is spread over ~7 rows ('Two adults' / 'with one
    child' / 'with two children'); rows are COICOP 1..13 and 'Total expenditure'."""
    grid = pd.read_excel(io.BytesIO(content), sheet_name="A23", header=None, dtype=str).fillna("")
    first = next(
        i for i in range(len(grid)) if str(grid.iat[i, 0]).lower().startswith("weighted number")
    )
    top = next(i for i in range(first) if "retired" in " ".join(grid.iloc[i]).lower())
    wanted, group, sup = {}, "", ""
    for j in range(2, grid.shape[1]):
        s = str(grid.iat[top, j]).strip().lower()
        sup = s or sup  # 'Retired households' / 'Non-retired' / 'Retired and non-retired'
        h = " ".join(" ".join(grid.iloc[top + 1 : first, j].astype(str)).split()).lower()
        if not h:
            continue
        if m := re.match(r"^(.*?(?:one|two|three or more) (?:adults?|person))", h):
            group = m.group(1)
        full = f"{sup} | " + (h if not h.startswith("with") else f"{group} {h}")
        if sup.startswith("non-retired") and full.endswith("two adults"):
            wanted[j] = "two adults"
        elif re.search(r"two adults with one child", full):
            wanted[j] = "two adults, one child"
        elif re.search(r"two adults.*with two children", full):
            wanted[j] = "two adults, two children"
    rows = []
    for i in range(first, len(grid)):
        code = str(grid.iat[i, 0]).strip()
        if re.fullmatch(r"\d{1,2}", code) and 1 <= int(code) <= 13:
            cat = f"CP{int(code):02d}"
        elif code.lower().startswith("total expenditure"):
            cat = "TOTAL"
        else:
            continue
        for j, hh in wanted.items():
            v = _num(grid.iat[i, j])
            if pd.notna(v):
                rows.append({"category": cat, "household_type": hh, "value": v})
        if cat == "TOTAL":
            break
    return rows


def parse_fs_a1_housing_split(content: bytes) -> dict[str, float]:
    """All-household shares of COICOP 4 sub-groups (4.1 net rent, 4.2 maintenance,
    4.3 water & dwelling services, 4.4 energy) from Table A1."""
    grid = pd.read_excel(io.BytesIO(content), sheet_name="A1", header=None, dtype=str).fillna("")
    vals = {}
    for i in range(len(grid)):
        cells = [str(x).strip() for x in grid.iloc[i] if str(x).strip()]
        if len(cells) >= 3 and cells[0] in ("4", "4.1.3", "4.2", "4.3", "4.4"):
            vals.setdefault(cells[0], _num(cells[2]))
    if not vals.get("4"):
        return {}
    return {k: v / vals["4"] for k, v in vals.items() if k != "4" and pd.notna(v)}


def spending_uk_family(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """ONS Family Spending A23 (GBP/week): 2 non-retired adults, 2 adults + 1 child,
    2 adults + 2 children. COICOP 4 ('housing (net), fuel & power') is split into
    rent / maintenance / water / energy using the all-household structure of A1."""
    raw, edition = _ons_latest_xlsx(f, FS_WB2, "workbook2expenditurebyincome.xlsx")
    rows = parse_fs_a23(raw)
    if not rows:
        raise FetchError("Family Spending: Table A23 two-adult columns not found")
    fy_end = int(re.search(r"(\d{4})", edition).group(1))
    split = {}
    try:
        raw1, _ = _ons_latest_xlsx(f, FS_WB1, "workbook1detailedexpenditureandtrends.xlsx")
        split = parse_fs_a1_housing_split(raw1)
    except (FetchError, ValueError, StopIteration) as e:
        log.warning("Family Spending A1 split skipped: %s", e)
    out = []
    for r in rows:
        if r["category"] == "CP04" and split:
            for code, sub in [
                ("4.1.3", "CP041"),
                ("4.2", "CP043"),
                ("4.3", "CP044"),
                ("4.4", "CP045"),
            ]:
                if code in split:
                    out.append(
                        {
                            **r,
                            "category": sub,
                            "value": r["value"] * split[code],
                            "estimated": True,
                            "method": f"CP04 x all-household share of {code} (A1)",
                        }
                    )
        out.append(r)
    df = pd.DataFrame(out).assign(
        iso3="GBR", period=str(fy_end - 1), series=f"Family Spending {edition} A23"
    )
    df["estimated"] = df.get("estimated", False)
    df["estimated"] = df["estimated"].fillna(False).astype(bool)
    return frame(
        df.drop_duplicates(["category", "household_type"]),
        measure="household_spending",
        statistic="mean",
        frequency="A",
        unit="GBP/week",
        area_type="all areas",
        source=f"ONS Family Spending {edition.upper()}",
        comparability_note=NOTES["ons_fs"],
    )


def spending_canada_mbm(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Market Basket Measure: cost of a *modest* basket for 2 adults + 2 children,
    by region incl. rural and small-population areas. A floor, not a typical spend."""
    d = statcan(f, "11100066", None, since=since)
    comp = col_like(d, "component")
    base = col_like(d, "base")
    if base:
        newest = sorted(d[base].dropna().unique())[-1]
        d = d[d[base] == newest]
    out = pd.DataFrame(
        {
            "iso3": "CAN",
            "period": d["REF_DATE"],
            "value": d["VALUE"],
            "category": d[comp] if comp else "total",
            "area_type": d["GEO"],
            "statistic": "mbm_threshold",
            "household_type": "2 adults + 2 children (reference family)",
            "series": "11-10-0066-01",
        }
    )
    return frame(
        out,
        measure="cost_floor",
        frequency="A",
        unit="CAD/year",
        source="StatCan MBM",
        comparability_note=(
            "Poverty-line basket (modest living standard) for 2 adults + 2 kids; "
            "divide by sqrt(4)/sqrt(2)=1.414 for a couple."
        ),
    )

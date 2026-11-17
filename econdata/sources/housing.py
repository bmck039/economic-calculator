"""Cost of living: housing (rents, prices, mortgage rates)."""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime

import pandas as pd

from econdata.clients import EU_GEOS, col_like, eurostat, fred, statcan
from econdata.config import COUNTRIES, ISO2_TO_ISO3, NOTES, ROOT
from econdata.fetch import CachedFetcher, FetchError, is_json
from econdata.sources.labour import annual_from_monthly
from econdata.tidy import frame

log = logging.getLogger("econ")


def housing_costs_silc(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # tenure is now TOTAL/OWN/RENT (labels 'Owner'/'Tenant'); poverty status rskpovth
    df = eurostat(
        f,
        "ilc_mded03",
        optional={
            "rskpovth": ["TOTAL"],
            "hhcomp": ["TOTAL", "A2", "A2_DCH1", "A2_DCH2", "A2_DCH_GE3"],
        },
        since=since,
        geo=EU_GEOS,
    )
    ten = col_like(df, "tenure")
    ten = ten if ten and not ten.endswith("_label") else None
    hh = col_like(df, "hhtyp", "hhcomp")
    hh = hh if hh and not hh.endswith("_label") else None
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": "mean_total_housing_cost",
            "category": df[ten + "_label"] if ten else "all tenures",
            "household_type": df[hh + "_label"] if hh else "all",
            "unit": (df["unit"] if "unit" in df else "PPS") + "/month",
            "series": "ilc_mded03",
        }
    )
    if "incgrp" in df and (df["incgrp"] == "TOTAL").any():
        out = out[df["incgrp"].values == "TOTAL"]
    return frame(
        out,
        measure="housing",
        frequency="A",
        source="Eurostat EU-SILC",
        comparability_note=NOTES["silc_housing"],
    )


def house_price_index_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(
        f,
        "prc_hpi_a",
        optional={"purchase": ["TOTAL"], "unit": ["I15_A_AVG"]},
        since=since,
        geo=EU_GEOS,
    )
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": "house_price_index",
            "unit": "2015=100",
            "series": "prc_hpi_a",
        }
    )
    parts = [out]
    try:  # current year so far from the quarterly index (the annual one lags a year)
        q = eurostat(
            f,
            "prc_hpi_q",
            optional={"purchase": ["TOTAL"], "unit": ["I15_Q"]},
            since=datetime.now().year - 1,
            geo=EU_GEOS,
        )
        q = pd.DataFrame(
            {
                "iso3": q["iso3"],
                "period": q["time"].astype(str),
                "value": pd.to_numeric(q["value"], errors="coerce"),
            }
        ).dropna()
        have = {(r.iso3, str(r.period)) for r in out.itertuples()}
        q = q.assign(y=q["period"].str[:4])
        g = q.groupby(["iso3", "y"], as_index=False).agg(
            value=("value", "mean"), n=("value", "size")
        )
        g = g[[(i, y) not in have for i, y in zip(g["iso3"], g["y"])]]
        parts.append(
            pd.DataFrame(
                {
                    "iso3": g["iso3"],
                    "period": g["y"],
                    "value": g["value"],
                    "statistic": "house_price_index",
                    "unit": "2015=100",
                    "series": "prc_hpi_q",
                    "estimated": True,
                    "method": [f"mean of {n} quarters" for n in g["n"]],
                }
            )
        )
    except FetchError as e:
        log.warning("Eurostat quarterly HPI skipped: %s", e)
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="housing",
        frequency="A",
        source="Eurostat HPI",
        comparability_note="House price index, all dwellings, purchase prices.",
    )


def rent_index_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """HICP actual rentals for housing (CP041), monthly -> calendar-year means (the
    current year so far): moves curated new-lease rents to the latest month."""
    df = eurostat(
        f,
        "prc_hicp_midx",
        required={"unit": ["I15"], "coicop": ["CP041"]},
        since=max(since or 2015, 2015),
        geo=EU_GEOS,
    )
    m = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"].astype(str),
            "value": pd.to_numeric(df["value"], errors="coerce"),
        }
    ).dropna()
    out = annual_from_monthly(m, set(), ["iso3"])
    return frame(
        out.assign(series="prc_hicp_midx:CP041"),
        measure="housing",
        statistic="rent_index",
        unit="2015=100",
        frequency="A",
        source="Eurostat HICP (actual rentals)",
        comparability_note="Rent index of sitting and new tenants (HICP CP041).",
    )


BIS_HPI = {
    "GBR": "QGBN628BIS",
    "CAN": "QCAN628BIS",
    "CHE": "QCHN628BIS",
    "USA": "QUSN628BIS",
    "GRC": "QGRN628BIS",
}


def house_prices_bis(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """BIS nominal residential property prices (via FRED) where Eurostat has none."""
    parts = []
    for iso, sid in BIS_HPI.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        d = d.assign(year=d["date"].str[:4])
        g = d.groupby("year", as_index=False).agg(value=("value", "mean"), n=("value", "size"))
        parts.append(
            pd.DataFrame(
                {
                    "iso3": iso,
                    "period": g["year"],
                    "value": g["value"],
                    "estimated": g["n"] < 4,
                    "series": sid,
                }
            )
        )
    if not parts:
        raise FetchError("BIS house prices: nothing fetched")
    return frame(
        pd.concat(parts, ignore_index=True),
        measure="housing",
        statistic="house_price_index",
        unit="index",
        frequency="A",
        source="BIS residential property prices (via FRED)",
        comparability_note="Nominal residential property prices.",
    )


CENSUS = "https://api.census.gov/data/{y}/acs/acs1"


def housing_us_acs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # The Census API now answers key-less requests with an HTML "Missing Key" page
    # (HTTP 200), which used to be cached as data and then silently skipped.
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError(
            "Census API now requires a key: get one free at "
            "https://api.census.gov/data/key_signup.html and set CENSUS_API_KEY. "
            "US rent falls back to reference_values.csv (ACS 2025 median)."
        )
    rows = []
    for y in range(max(since or 2005, 2005), datetime.now().year):
        if y == 2020:  # no standard 1-year release for 2020
            continue
        params = {"get": "NAME,B25064_001E,B25077_001E", "for": "us:1", "key": key}
        try:
            js = json.loads(
                f.get(
                    CENSUS.format(y=y),
                    params,
                    label=f"census:acs1:{y}",
                    validate=is_json,
                    timeout=30,
                )
            )
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue  # year not released yet
            raise
        hdr, vals = js[0], js[1]
        rec = dict(zip(hdr, vals))
        rows.append(
            {
                "period": str(y),
                "statistic": "median_gross_rent",
                "value": rec.get("B25064_001E"),
                "unit": "USD/month",
                "series": "B25064",
            }
        )
        rows.append(
            {
                "period": str(y),
                "statistic": "median_home_value",
                "value": rec.get("B25077_001E"),
                "unit": "USD",
                "series": "B25077",
            }
        )
    if not rows:
        raise FetchError("ACS: nothing fetched")
    return frame(
        pd.DataFrame(rows).assign(iso3="USA"),
        measure="housing",
        frequency="A",
        source="Census ACS",
        comparability_note=NOTES["acs"],
    )


ACS_STATE_VARS = {
    "B25113_002E": "rent_recent_gross",
    "B25064_001E": "rent_median_gross",
    "B25077_001E": "home_value",
    "B25103_001E": "re_taxes",
    "B20017_003E": "earn_ft_male",
    "B20017_006E": "earn_ft_female",
    "B19013_001E": "hh_income",
}


def us_states_api(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Per state (+ US): ACS 1-year recent-mover rent, median rent, home value, real-estate
    taxes, full-time earnings and household income (latest year released), and the BEA
    regional price parity (FRED {ST}RPPALL)."""
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("ACS by state needs CENSUS_API_KEY")
    rows, yr = [], None
    for y in range(datetime.now().year - 1, datetime.now().year - 4, -1):
        try:
            got = []
            for geo in ("state:*", "us:1"):
                js = json.loads(
                    f.get(
                        CENSUS.format(y=y),
                        {"get": "NAME," + ",".join(ACS_STATE_VARS), "for": geo, "key": key},
                        label=f"census:acs1-states:{y}",
                        validate=is_json,
                        timeout=60,
                    )
                )
                got += [dict(zip(js[0], r)) for r in js[1:]]
            yr = y
            break
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue
            raise
    if yr is None:
        raise FetchError("ACS by state: no year available")
    for r in got:
        nm = "United States" if r["NAME"] == "United States" else r["NAME"]
        for v, col in ACS_STATE_VARS.items():
            rows.append(
                {
                    "category": nm,
                    "statistic": col,
                    "value": r.get(v),
                    "period": str(yr),
                    "series": "ACS1 " + v,
                }
            )
    names = P_STATE_CODES()
    for code, nm in names.items():
        sid = "RPPALL" if code == "US" else f"{code}RPPALL"
        try:
            d = fred(f, sid)
        except FetchError:
            continue
        last = d.iloc[-1]
        rows.append(
            {
                "category": nm,
                "statistic": "rpp",
                "value": last["value"],
                "period": str(last["date"])[:4],
                "series": "FRED " + sid,
            }
        )
    return frame(
        pd.DataFrame(rows).assign(iso3="USA"),
        measure="us_state",
        frequency="A",
        unit="mixed",
        source="Census ACS 1-year / BEA RPP (FRED)",
        comparability_note="US states: ACS medians and BEA regional price parities.",
    )


def P_STATE_CODES() -> dict[str, str]:
    """State code -> name, from the curated table shipped with the code."""
    p = ROOT / "econsim" / "params" / "us_states.csv"
    t = pd.read_csv(p, comment="#")
    return dict(zip(t["state"], t["name"].replace({"US": "United States"})))


FRED_HOUSING = {
    "HSFMEDUSM052N": ("median_existing_single_family_price", "USD"),
    "MORTGAGE30US": ("mortgage_rate_30y_fixed", "%"),
    "APU000072610": ("electricity_price", "USD/kWh"),
    "APU000072620": ("utility_gas_price", "USD/therm"),
    "GASREGW": ("gasoline_price", "USD/gallon"),
    "CUUR0000SEHA": ("rent_index", "index"),  # CPI rent of primary residence
}


def housing_energy_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, (stat, unit) in FRED_HOUSING.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        meas = "housing" if stat.startswith(("median", "mortgage", "rent")) else "energy_prices"
        parts.append(
            d.assign(
                iso3="USA",
                period=d["date"].str[:7],
                statistic=stat,
                unit=unit,
                series=sid,
                measure=meas,
            )
        )
    if not parts:
        raise FetchError("No FRED housing/energy series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(
        df,
        frequency="M",
        source="FRED (NAR, Freddie Mac, BLS, EIA)",
        comparability_note="US national series.",
    )


def housing_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    try:  # CMHC average rents, all centres 10k+; we keep two-bedroom
        d = statcan(
            f, "34100133", None, {"type of unit": ["two bedroom", "two-bedroom"]}, since=since
        )
        st = col_like(d, "type of structure")
        if st:
            d = d[d[st].str.contains("row and apartment|apartment", case=False, na=False)]
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "CAN",
                    "period": d["REF_DATE"].str[:4],
                    "value": d["VALUE"],
                    "statistic": "average_rent_2br",
                    "area_type": d["GEO"],
                    "unit": "CAD/month",
                    "series": "34-10-0133-01",
                    "measure": "housing",
                }
            )
        )
    except FetchError as e:
        log.warning("CMHC rents skipped: %s", e)
    try:  # posted 5-year conventional mortgage rate (Bank of Canada Valet)
        js = json.loads(
            f.get(
                "https://www.bankofcanada.ca/valet/observations/V80691335/json",
                label="boc:V80691335",
            )
        )
        obs = [
            (o["d"], o["V80691335"]["v"])
            for o in js.get("observations", [])
            if o.get("V80691335", {}).get("v") not in (None, "")
        ]
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "CAN",
                    "period": [d[:7] for d, _ in obs],
                    "value": [v for _, v in obs],
                    "statistic": "mortgage_rate_5y_posted",
                    "unit": "%",
                    "series": "BoC V80691335",
                    "measure": "housing",
                }
            )
        )
    except (FetchError, ValueError, KeyError) as e:
        log.warning("BoC mortgage rate skipped: %s", e)
    try:  # rate actually paid on new insured 5y+ fixed mortgages (posted rates are ~2 pts
        # higher than what borrowers get); insured = under 20% down, like the default plan
        js = json.loads(
            f.get(
                "https://www.bankofcanada.ca/valet/observations/V122667780/json",
                label="boc:V122667780",
            )
        )
        obs = [
            (o["d"], o["V122667780"]["v"])
            for o in js.get("observations", [])
            if o.get("V122667780", {}).get("v") not in (None, "")
        ]
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "CAN",
                    "period": [d[:7] for d, _ in obs],
                    "value": [v for _, v in obs],
                    "statistic": "mortgage_rate_new_loans",
                    "unit": "%",
                    "series": "BoC V122667780 (funds advanced, insured, fixed 5y+)",
                    "measure": "housing",
                }
            )
        )
    except (FetchError, ValueError, KeyError) as e:
        log.warning("BoC new-mortgage rate skipped: %s", e)
    try:  # regular gasoline, self-serve, by city
        d = statcan(
            f, "18100001", None, {"type of fuel": "regular unleaded gasoline"}, since=since
        )
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "CAN",
                    "period": d["REF_DATE"].str[:7],
                    "value": d["VALUE"],
                    "statistic": "gasoline_price",
                    "area_type": d["GEO"],
                    "unit": "CAD cents/litre",
                    "series": "18-10-0001-01",
                    "measure": "energy_prices",
                }
            )
        )
    except FetchError as e:
        log.warning("StatCan gasoline skipped: %s", e)
    if not parts:
        raise FetchError("No Canadian housing series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(
        df,
        frequency="M",
        source="StatCan / CMHC / Bank of Canada",
        comparability_note="CMHC rents: purpose-built rentals in centres 10k+; "
        "mortgage rate = rate paid on new insured 5y+ fixed loans "
        "(BoC), else the posted 5y rate (higher).",
    )


BOE_CSV = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
    "?csv.x=yes&Datefrom=01/Jan/2005&Dateto=now&SeriesCodes={s}&CSVF=TN&UsingCodes=Y"
)


def housing_uk_boe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    raw = f.get(
        BOE_CSV.format(s="IUMBV34"),
        label="boe:IUMBV34",
        headers={"User-Agent": "Mozilla/5.0 (research script)"},
    )
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = ["date", "value"][: len(df.columns)]
    df["period"] = pd.to_datetime(df["date"], format="%d %b %Y", errors="coerce").dt.strftime(
        "%Y-%m"
    )
    df = df.dropna(subset=["period"])
    out = df.assign(
        iso3="GBR", statistic="mortgage_rate_2y_fixed_75ltv", unit="%", series="BoE IUMBV34"
    )
    return frame(
        out,
        measure="housing",
        frequency="M",
        source="Bank of England",
        comparability_note="Quoted 2-year fixed rate, 75% LTV.",
    )


ECB_API = "https://data-api.ecb.europa.eu/service/data/MIR/"


def mortgage_rates_ecb(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    eu = [COUNTRIES[c][0] for c in COUNTRIES if COUNTRIES[c][3] == "EU"]  # ISO2; GR=Greece
    key = f"M.{'+'.join(eu)}.B.A2C.A.R.A.2250..N"  # currency wildcard (non-euro NAC)
    params = {"format": "csvdata"}
    if since:
        params["startPeriod"] = f"{since}-01"
    df = pd.read_csv(io.BytesIO(f.get(ECB_API + key, params, label="ecb:MIR")), dtype=str)
    df["iso3"] = df["REF_AREA"].map(ISO2_TO_ISO3)
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["TIME_PERIOD"],
            "value": df["OBS_VALUE"],
            "statistic": "mortgage_rate_new_loans",
            "unit": "% p.a.",
            "series": "MIR:" + df.get("KEY", ""),
        }
    )
    return frame(
        out.dropna(subset=["iso3"]),
        measure="housing",
        frequency="M",
        source="ECB MFI interest rates",
        comparability_note="Agreed rate on NEW lending for house purchase, all "
        "fixation periods (actual contracts, not quotes).",
    )


def mortgage_rates_no_ch(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Norway: SSB table 10748, interest on NEW loans secured on dwellings, households,
    all fixation periods (actual contracts). Switzerland: SNB 'zikrepro' MEDIAN offered
    rate on new mortgages (10-year fixed and variable). Iceland has no free API here:
    see reference_values.csv."""
    parts = []
    try:
        q = {
            "query": [
                {"code": "Utlanstype", "selection": {"filter": "item", "values": ["04"]}},
                {"code": "Sektor", "selection": {"filter": "item", "values": ["04b"]}},
                {"code": "Rentebinding", "selection": {"filter": "item", "values": ["99"]}},
            ],
            "response": {"format": "json-stat2"},
        }
        js = json.loads(
            f.get(
                "https://data.ssb.no/api/v0/en/table/10748",
                label="ssb:10748",
                method="POST",
                json_body=q,
                validate=is_json,
            )
        )
        idx = js["dimension"]["Tid"]["category"]["index"]
        per = sorted(idx, key=idx.get)
        vals = js["value"]
        parts.append(
            pd.DataFrame(
                {
                    "iso3": "NOR",
                    "period": [p.replace("M", "-") for p in per],
                    "value": vals,
                    "statistic": "mortgage_rate_new_loans",
                    "unit": "% p.a.",
                    "series": "SSB 10748",
                    "source": "Statistics Norway",
                    "comparability_note": "Agreed rate on NEW household loans "
                    "secured on dwellings, all fixation periods.",
                }
            )
        )
    except (FetchError, ValueError, KeyError) as e:
        log.warning("SSB mortgage rate skipped: %s", e)
    try:
        raw = f.get(
            "https://data.snb.ch/api/cube/zikrepro/data/csv/en",
            label="snb:zikrepro",
            validate=lambda b: b"zikrepro" in b[:200],
        )
        rows = []
        for line in raw.decode("utf-8-sig").splitlines():
            c = [x.strip('"') for x in line.split(";")]
            if len(c) == 4 and c[1] == "05Q" and c[2] in ("10", "MV") and c[3]:
                rows.append(
                    {
                        "period": c[0],
                        "value": float(c[3]),
                        "statistic": "mortgage_rate_10y_fixed_median"
                        if c[2] == "10"
                        else "mortgage_rate_variable_median",
                    }
                )
        d = pd.DataFrame(rows)
        parts.append(
            d.assign(
                iso3="CHE",
                unit="% p.a.",
                series="SNB zikrepro 05Q",
                source="Swiss National Bank",
                comparability_note="Median rate OFFERED on new mortgages "
                "(bank survey); 10-year fixed is the most common choice.",
            )
        )
    except (FetchError, ValueError) as e:
        log.warning("SNB mortgage rate skipped: %s", e)
    if not parts:
        raise FetchError("No NO/CH mortgage rates")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, measure="housing", frequency="M")

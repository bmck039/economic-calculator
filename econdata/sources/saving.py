"""Saving: household saving rates and typical household portfolios."""

from __future__ import annotations

import io
import logging

import pandas as pd

from econdata.clients import EU_GEOS, eurostat, fred_annual_chained, oecd, oecd_rows
from econdata.config import COUNTRIES, ISO2_TO_ISO3, NOTES
from econdata.fetch import CachedFetcher, FetchError
from econdata.tidy import frame

log = logging.getLogger("econ")


NAAG_DIMS = ["FREQ", "REF_AREA", "MEASURE", "UNIT_MEASURE", "CHAPTER"]


def saving_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(
        f,
        "OECD.SDD.NAD,DSD_NAAG@DF_NAAG_V",
        NAAG_DIMS,
        {
            "FREQ": ["A"],
            "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
            "MEASURE": ["B8NS1M"],
            "UNIT_MEASURE": ["PT_B6N_S1M"],
            "CHAPTER": ["NAAG_V"],
        },
        since,
    )
    out = oecd_rows(
        df,
        {
            "series": "NAAG_V:B8NS1M",
        },
    )
    out = frame(
        out,
        measure="saving_rate_net",
        statistic="rate",
        frequency="A",
        unit="% of net disposable income",
        source="OECD NAAG",
        comparability_note=NOTES["sav_net"],
    )
    try:
        out = pd.concat([out, saving_us_gap_fill(f, out)], ignore_index=True)
    except FetchError as e:
        log.warning("US saving-rate gap fill skipped: %s", e)
    return out


def saving_us_gap_fill(f: CachedFetcher, oecd_rows: pd.DataFrame) -> pd.DataFrame:
    """OECD has no US household saving rate after 2024 (BEA releases were delayed by the
    2025 shutdown). Fill later years, including the current year so far, from BEA's monthly
    personal saving rate (FRED PSAVERT), scaled to the OECD level at the last common year."""
    u = oecd_rows[oecd_rows["iso3"] == "USA"].set_index("year")["value"]
    if u.empty:
        return oecd_rows.iloc[0:0]
    a, base, k = fred_annual_chained(f, "PSAVERT", u)
    rows = [
        {
            "iso3": "USA",
            "period": str(y),
            "value": a.loc[y, "mean"] * k,
            "series": "PSAVERT",
            "estimated": True,
            "method": f"BEA personal saving rate, mean of {a.loc[y, 'size']} months, x{k:.2f} "
            f"(OECD/BEA in {base})",
        }
        for y in a.index
        if y > base and y not in u.index
    ]
    return (
        frame(
            pd.DataFrame(rows),
            measure="saving_rate_net",
            statistic="rate",
            frequency="A",
            unit="% of net disposable income",
            source="OECD NAAG (gap filled from BEA via FRED)",
            comparability_note=NOTES["sav_net"],
        )
        if rows
        else oecd_rows.iloc[0:0]
    )


def saving_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "tec00131", since=since, geo=EU_GEOS)
    if "na_item" in df:
        df = df[df["na_item"] == "SRG_S14_S15"]
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "series": "tec00131:SRG_S14_S15",
        }
    )
    return frame(
        out,
        measure="saving_rate_gross",
        statistic="rate",
        frequency="A",
        unit="% of gross disposable income",
        source="Eurostat",
        comparability_note=NOTES["sav_gross"],
    )


# Danish labour-market pensions are run by life insurers (F62): they are quasi-mandatory
# pension saving (modelled with the pension), not free savings
LIFE_INSURANCE_IS_PENSION = {"DNK"}


DWA_TYPICAL = ("B50", "D6", "D7")  # the bottom 70% of households by net wealth


ECB_DWA = "https://data-api.ecb.europa.eu/service/data/DWA"


def household_assets_typical_ecb(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """How TYPICAL households hold their savings: ECB Distributional Wealth Accounts, the
    bottom 70% of households by net wealth (the whole sector leans on the wealthiest,
    who hold far more in shares). Deposits as a share of deposits + bonds + listed shares
    + funds + life insurance; shares + funds as a share of the rest. Latest quarter."""
    b = f.get(
        ECB_DWA,
        {"format": "csvdata", "detail": "dataonly", "lastNObservations": 1},
        label="ecb:dwa",
        timeout=180,
        validate=lambda x: x[:3] == b"KEY",
    )
    d = pd.read_csv(io.BytesIO(b))
    d = d[(d["UNIT_MEASURE"] == "EUR") & (d["STO"] == "LE") & d["DWA_GRP"].isin(DWA_TYPICAL)]
    rows = []
    for geo, g in d.groupby("REF_AREA"):
        iso = ISO2_TO_ISO3.get(geo)
        if iso is None:
            continue
        v = g.groupby("INSTR_ASSET")["OBS_VALUE"].sum()
        inv = sum(float(v.get(k, 0.0)) for k in ("F3", "F511", "F52", "F62"))
        dep = float(v.get("F2M", 0.0))
        if dep + inv <= 0 or inv <= 0:
            continue
        per = str(g["TIME_PERIOD"].max())
        rows += [
            {
                "iso3": iso,
                "period": per[:4],
                "statistic": "deposit_share_typical",
                "value": dep / (dep + inv),
                "method": per,
            },
            {
                "iso3": iso,
                "period": per[:4],
                "statistic": "equity_share_typical",
                "value": (float(v.get("F511", 0)) + float(v.get("F52", 0))) / inv,
                "method": per,
            },
        ]
    if not rows:
        raise FetchError("ECB DWA: nothing parsed")
    return frame(
        pd.DataFrame(rows).assign(series="ECB DWA bottom 70%"),
        measure="household_assets_typical",
        frequency="Q",
        unit="share",
        source="ECB Distributional Wealth Accounts (bottom 70% by net wealth)",
    )

"""Price levels and PPP."""

from __future__ import annotations

import logging

import pandas as pd

from econdata.clients import EU_GEOS, eurostat, worldbank_indicators
from econdata.config import COUNTRIES, NOTES
from econdata.fetch import CachedFetcher, FetchError
from econdata.tidy import frame

log = logging.getLogger("econ")


WB_PPP = {
    "PA.NUS.PRVT.PP": ("ppp_private_consumption", "LCU per international $"),
    "PA.NUS.PPP": ("ppp_gdp", "LCU per international $"),
    "PA.NUS.FCRF": ("exchange_rate", "LCU per USD"),
}


# PA.NUS.PPPC.RF (price level ratio) was archived by the World Bank; it is now derived
# as PPP (private consumption) / market exchange rate.


def price_levels_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # one failed indicator must not take the whole module down
    parts = worldbank_indicators(f, WB_PPP, list(COUNTRIES) + ["EMU"], since)
    if not parts:
        raise FetchError("No World Bank PPP/FX indicators")
    df = pd.concat(parts)
    piv = df.pivot_table(index=["iso3", "period"], columns="statistic", values="value")
    if {"ppp_private_consumption", "exchange_rate"} <= set(piv.columns):
        plr = (piv["ppp_private_consumption"] / piv["exchange_rate"]).dropna().reset_index()
        plr.columns = ["iso3", "period", "value"]
        parts.append(
            plr.assign(
                statistic="price_level_ratio",
                unit="PPP / market exchange rate",
                estimated=False,
                method="PA.NUS.PRVT.PP / PA.NUS.FCRF",
                series="derived:PRVT.PP/FCRF",
            )
        )
    df = pd.concat(parts)
    return frame(
        df,
        measure="price_level",
        frequency="A",
        source="World Bank WDI",
        comparability_note=NOTES["ppp"],
    )


def price_levels_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """PPPs (NAC per PPS) and price level indices (EU27=100) by consumption category."""
    df = eurostat(
        f,
        "prc_ppp_ind",
        optional={"na_item": ["PPP_EU27_2020", "PLI_EU27_2020"]},
        since=since,
        geo=EU_GEOS,
    )
    out = pd.DataFrame(
        {
            "iso3": df["iso3"],
            "period": df["time"],
            "value": df["value"],
            "statistic": df["na_item"].str.lower(),
            "category": df["ppp_cat_label"],
            "unit": df["na_item"].map(
                {"PPP_EU27_2020": "NAC per PPS", "PLI_EU27_2020": "EU27=100"}
            ),
            "series": "prc_ppp_ind:" + df["ppp_cat"],
        }
    )
    return frame(
        out,
        measure="price_level",
        frequency="A",
        source="Eurostat PPP",
        comparability_note="Eurostat-OECD PPP programme, by ESA/COICOP category.",
    )

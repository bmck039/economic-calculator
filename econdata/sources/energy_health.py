"""Cost of living: energy and health."""

from __future__ import annotations

import logging

import pandas as pd

from econdata.clients import EU_GEOS, eurostat, worldbank_indicators
from econdata.config import COUNTRIES, NOTES
from econdata.fetch import CachedFetcher, FetchError
from econdata.tidy import frame

log = logging.getLogger("econ")


def energy_prices_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for code, band, stat in [
        ("nrg_pc_204", "KWH2500-4999", "electricity_price"),
        ("nrg_pc_202", "GJ20-199", "natural_gas_price"),
    ]:
        try:
            df = eurostat(
                f,
                code,
                optional={"nrg_cons": [band], "tax": ["I_TAX"], "currency": ["EUR", "NAC"]},
                since=since,
                geo=EU_GEOS,
            )
        except FetchError as e:
            log.warning("%s skipped: %s", code, e)
            continue
        unit = df["currency"] + "/" + (df["unit"] if "unit" in df else "unit")
        parts.append(
            pd.DataFrame(
                {
                    "iso3": df["iso3"],
                    "period": df["time"],
                    "value": df["value"],
                    "statistic": stat,
                    "unit": unit,
                    "series": code,
                }
            )
        )
    if not parts:
        raise FetchError("No Eurostat energy prices")
    return frame(
        pd.concat(parts),
        measure="energy_prices",
        frequency="S",
        source="Eurostat energy prices",
        comparability_note=NOTES["energy"],
    )


WB_HEALTH = {
    "SH.XPD.OOPC.PP.CD": ("out_of_pocket_per_capita", "intl $ PPP/person/year"),
    "SH.XPD.CHEX.PP.CD": ("health_spending_per_capita", "intl $ PPP/person/year"),
}


# SDG 3.8.2 (catastrophic out-of-pocket health spending). The SH.UHC.OOPC.* codes were
# archived; the series now lives in the Health Equity and Financial Protection
# Indicators database (source 65) as HF.UHC.OOPC.*. Survey-based: latest year is often
# 2010-2018 for European countries.
WB_HEALTH_HEFPI = {
    "HF.UHC.OOPC.10.ZS": ("share_oop_over_10pct_income", "% of population"),
    "HF.UHC.OOPC.25.ZS": ("share_oop_over_25pct_income", "% of population"),
}


def health_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = worldbank_indicators(f, WB_HEALTH, list(COUNTRIES), since) + worldbank_indicators(
        f, WB_HEALTH_HEFPI, list(COUNTRIES), None, sources=("65",)
    )
    if not parts:
        raise FetchError("No World Bank health indicators")
    return frame(
        pd.concat(parts),
        measure="health",
        frequency="A",
        source="World Bank / WHO",
        comparability_note="WHO Global Health Expenditure Database via World Bank.",
    )

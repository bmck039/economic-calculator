"""History: what prices, rates and returns did year by year (US, Europe)."""

from __future__ import annotations

import io
import logging
import zipfile

import pandas as pd

from econdata.clients import _annual
from econdata.fetch import CachedFetcher, FetchError, is_zip
from econdata.sources.spending import US_CPI_PARTS, cpi_series
from econdata.tidy import frame

log = logging.getLogger("econ")


FF_FACTORS = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip"
)


# Annual US history for backtests (what actually happened): CPI and its parts, home prices,
# rents, mortgage and market rates, deposit rates, returns, wages, unemployment.
US_HISTORY = {
    "cpi": "CPIAUCSL",
    "hpi": "CSUSHPISA",
    "rent": "CUSR0000SEHA",
    "mortgage30": "MORTGAGE30US",
    "tbill3m": "TB3MS",
    "y10": "GS10",
    "deposit_savings": "SNDR",
    "wage": "AHETPI",
    "unemployment": "UNRATE",
}


FF_EUROPE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_3_Factors_CSV.zip"


def french_annual(f: CachedFetcher, url: str, label: str) -> dict[int, tuple[float, float]]:
    """Kenneth French data library: year -> (market minus risk-free, risk-free), % (USD)."""
    b = f.get(url, label=label, validate=is_zip, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(b))
    txt = z.open(z.namelist()[0]).read().decode("latin-1")
    ann = txt[txt.index("Annual Factors") :]
    out = {}
    for line in ann.splitlines()[2:]:
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 5 and p[0].isdigit():
            out[int(p[0])] = (float(p[1]), float(p[4]))
    return out


def history_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """European stock market total returns in USD (Kenneth French data library: developed
    Europe, value-weighted); converted to each currency in derive_eu_history."""
    rows = [
        {
            "iso3": "EA",
            "statistic": "equity_return_usd",
            "period": str(y),
            "value": mkt + rf,
            "series": "Kenneth French data library (Europe market)",
        }
        for y, (mkt, rf) in french_annual(f, FF_EUROPE, "french:europe3").items()
    ]
    return frame(
        pd.DataFrame(rows),
        measure="history_eu",
        frequency="A",
        unit="% per year",
        source="Kenneth French data library",
    )


def history_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What US prices, rates, returns and wages did, year by year (FRED; stock market
    returns from the Kenneth French data library: CRSP value-weighted market)."""
    rows = []
    for key, sid in list(US_HISTORY.items()) + [(f"cpi:{k}", v) for k, v in US_CPI_PARTS.items()]:
        try:
            a = _annual(cpi_series(f, sid))
        except FetchError as e:
            log.warning("history %s skipped: %s", sid, str(e)[:120])
            continue
        rows += [
            {
                "iso3": "USA",
                "statistic": key,
                "period": str(y),
                "value": v,
                "series": f"FRED {sid}",
            }
            for y, v in a.items()
        ]
    for y, (mkt, rf) in french_annual(f, FF_FACTORS, "french:ff3").items():
        rows.append(
            {
                "iso3": "USA",
                "statistic": "equity_return",
                "period": str(y),
                "value": mkt + rf,
                "series": "Kenneth French data library (CRSP market)",
            }
        )
        rows.append(
            {
                "iso3": "USA",
                "statistic": "tbill_return",
                "period": str(y),
                "value": rf,
                "series": "Kenneth French data library",
            }
        )
    return frame(
        pd.DataFrame(rows),
        measure="history",
        frequency="A",
        unit="index / % / % per year",
        source="FRED; Kenneth French data library",
    )

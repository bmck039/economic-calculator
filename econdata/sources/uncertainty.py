"""Uncertainty indices."""

from __future__ import annotations

import pandas as pd

from econdata.clients import fred, worldbank
from econdata.config import COUNTRIES, NOTES
from econdata.fetch import CachedFetcher, FetchError
from econdata.tidy import frame


def uncertainty_wui(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for iso3 in COUNTRIES:
        for sid, stat in ((f"WUIMA{iso3}", "wui_smoothed"), (f"WUI{iso3}", "wui")):
            try:
                d = fred(f, sid)
            except FetchError:
                continue
            per = pd.to_datetime(d["date"])
            parts.append(
                d.assign(
                    iso3=iso3,
                    statistic=stat,
                    series=sid,
                    period=per.dt.year.astype(str) + "-Q" + per.dt.quarter.astype(str),
                )
            )
            break
    if not parts:
        raise FetchError("No WUI series fetched")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(
        df,
        measure="uncertainty",
        frequency="Q",
        unit="index",
        source="World Uncertainty Index (via FRED)",
        comparability_note=NOTES["wui"],
    )


def uncertainty_wgi(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    try:
        d = worldbank(f, "GOV_WGI_PV_EST", list(COUNTRIES), since, sources=(None, "3", "2"))
        sid = "GOV_WGI_PV_EST"
    except FetchError:
        d = worldbank(f, "PV.EST", list(COUNTRIES), since, sources=("3",))
        sid = "PV.EST"
    d = d.rename(columns={"date": "period"}).assign(statistic="political_stability", series=sid)
    return frame(
        d,
        measure="uncertainty",
        frequency="A",
        unit="estimate (-2.5..2.5)",
        source="World Bank WGI",
        comparability_note=NOTES["wgi"],
    )

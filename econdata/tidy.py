"""The tidy row layout every source returns (econ_long.csv)."""

from __future__ import annotations

import re

import pandas as pd

from econdata.config import COUNTRIES

TIDY_COLS = [
    "iso3",
    "country",
    "region",
    "measure",
    "statistic",
    "age_band",
    "career_stage",
    "household_type",
    "area_type",
    "category",
    "period",
    "year",
    "frequency",
    "value",
    "unit",
    "estimated",
    "method",
    "source",
    "series",
    "comparability_note",
]


AGE_RX = re.compile(r"(\d{2})\s*(?:-|to|T)\s*(\d{2})")


AGE_PLUS = re.compile(r"(\d{2})\s*(?:\+|years and over|years or over|and over)|_GE(\d{2})")


AGE_LT = re.compile(r"(?:less than|under|_LT)\s*(\d{2})", re.I)


def career_stage(age_band) -> str:
    """Map an age band to early (<30) / mid (30-49) / late (50+) by its midpoint.
    Broad bands map to 'all'. Age is only a proxy for career stage."""
    a = str(age_band or "").strip()
    if a.upper() in ("TOTAL", "ALL", "", "_T", "NAN"):
        return "all"
    if m := AGE_LT.search(a):
        return "early" if int(m.group(1)) <= 30 else "all"
    if m := AGE_RX.search(a):
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi - lo > 35:
            return "all"
        mid = (lo + hi) / 2
    elif m := AGE_PLUS.search(a):
        lo = int(next(g for g in m.groups() if g))
        if lo <= 25:
            return "all"
        mid = lo + 5
    else:
        return "unknown"
    return "early" if mid < 30 else ("mid" if mid < 50 else "late")


def frame(rows: pd.DataFrame, **const) -> pd.DataFrame:
    df = rows.copy()
    for k, v in const.items():
        df[k] = v
    df["country"] = df["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", c, ""))[2])
    df["region"] = df["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", "", "?"))[3])
    if "age_band" not in df:
        df["age_band"] = "total"
    if "career_stage" not in df:
        df["career_stage"] = df["age_band"].map(career_stage)
    if "estimated" not in df:
        df["estimated"] = False
    df["period"] = df["period"].astype(str)
    df["year"] = pd.to_numeric(df["period"].str[:4], errors="coerce").astype("Int64")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    for c in TIDY_COLS:
        if c not in df:
            df[c] = pd.NA
    return df[TIDY_COLS].dropna(subset=["value"])


def latest(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Keep the latest year per group."""
    if df.empty:
        return df
    return df.sort_values("year").groupby(by, dropna=False).tail(1)

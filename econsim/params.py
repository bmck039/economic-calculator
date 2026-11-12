"""Curated, sourced country parameters (econsim/params/*.csv).

These are policy rules and market levels that no statistics API publishes in a
comparable form: new-lease rents, existing-home prices, lending rules, transfer
taxes, health premiums, car costs, parental leave, pensions, US state taxes...
Every value carries a source and a quality grade (A official/statutory, B official
but approximated, C estimate). They ship with the code, so changing one does not
need a data rebuild.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .model import Sourced

DIR = Path(__file__).resolve().parent / "params"


def _num(x, default=float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(v) else v


@lru_cache(maxsize=1)
def _housing() -> pd.DataFrame:
    return pd.read_csv(DIR / "housing.csv", comment="#")


@lru_cache(maxsize=8)
def _wide(name: str) -> pd.DataFrame:
    return pd.read_csv(DIR / f"{name}.csv").set_index("iso3")


@lru_cache(maxsize=1)
def _hh_sources() -> dict[str, tuple[str, str]]:
    s = pd.read_csv(DIR / "household_sources.csv")
    return {r.param: (r.source, r.quality) for r in s.itertuples()}


@lru_cache(maxsize=1)
def us_states() -> pd.DataFrame:
    return pd.read_csv(DIR / "us_states.csv").set_index("state")


@lru_cache(maxsize=1)
def us_state_tax() -> pd.DataFrame:
    return pd.read_csv(DIR / "us_state_tax.csv", keep_default_na=False).set_index("state")


def us_state_names() -> dict[str, str]:
    s = us_states()
    return {k: v for k, v in s["name"].items() if k != "US"}


def housing_item(iso3: str, key: str) -> Sourced | None:
    h = _housing()
    r = h[(h["key"] == key) & (h["iso3"] == iso3)]
    if r.empty:
        r = h[(h["key"] == key) & (h["iso3"] == "*")]
    if r.empty:
        return None
    r = r.iloc[0]
    q = str(r["quality"])
    return Sourced(float(r["value"]), str(r["unit"]), str(r["source"]),
                   None if pd.isna(r["year"]) else str(int(r["year"])),
                   q == "C" or str(r["source"]).startswith("ESTIMATE"), q,
                   "" if pd.isna(r["note"]) else str(r["note"]))


def lending(iso3: str) -> dict:
    t = _wide("lending")
    if iso3 not in t.index:
        return {}
    r = t.loc[iso3]
    out = {k: (_num(v) if k not in ("dsti_basis", "source", "quality") else v)
           for k, v in r.items()}
    return out


def household(iso3: str) -> dict[str, float]:
    t = _wide("household")
    if iso3 not in t.index:
        return {}
    return {k: _num(v) for k, v in t.loc[iso3].items()}


def household_sourced(iso3: str, key: str, unit: str = "") -> Sourced | None:
    v = household(iso3).get(key, float("nan"))
    if v != v:
        return None
    src, q = _hh_sources().get(key, ("", "C"))
    return Sourced(v, unit, src, "2025", q == "C", q)


def parse_rate_brackets(s: str) -> list[tuple[float, float]]:
    """'2:0;4:500' -> [(0.02, 0), (0.04, 500)] (rate %, lower threshold)."""
    out = []
    for part in str(s).split(";"):
        if part.strip():
            r, t = part.split(":")
            out.append((float(r) / 100, float(t)))
    return sorted(out, key=lambda x: x[1])


@lru_cache(maxsize=1)
def _student_debt() -> pd.DataFrame:
    return pd.read_csv(DIR / "student_debt.csv")


def student_debt(iso3: str) -> dict[str, dict]:
    """Typical student debt at graduation by degree, for those who borrow (local money,
    in the stated year's prices). Only where borrowing is the norm and a national figure
    exists; elsewhere tuition is low and debt mostly small or absent."""
    d = _student_debt()
    return {r["education"]: r for r in d[d["iso3"] == iso3].to_dict("records")}

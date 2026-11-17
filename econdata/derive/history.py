"""Derived history tables for the backtests (US, Europe, HFCS, part-time)."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from econdata.config import EUROPE, LOCAL_CURRENCY
from econdata.derive.budget import hicp_budget_codes
from econdata.derive.context import Ctx
from econdata.sources.cohorts import HFCS_MONEY, HFCS_TABLES

log = logging.getLogger("econ")


def derive_part_time(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """part_time.csv: part-time share of employment by age band and education, latest
    year (Eurostat LFS)."""
    d = long[long["measure"] == "part_time_share"].dropna(subset=["value", "year"])
    d = d.sort_values("year").groupby(["iso3", "age_band", "category"]).tail(1)
    return pd.DataFrame(
        {
            "iso3": d["iso3"],
            "age_band": d["age_band"],
            "education": d["category"],
            "year": d["year"].astype(int),
            "value": d["value"],
            "source": d["source"],
        }
    ).reset_index(drop=True)


def derive_us_history(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    h = long[long["measure"] == "history"].dropna(subset=["value", "year"])
    return (
        pd.DataFrame(
            {
                "series": h["statistic"],
                "year": h["year"].astype(int),
                "value": h["value"],
                "source": h["series"],
            }
        )
        .sort_values(["series", "year"])
        .reset_index(drop=True)
    )


def _yearly(d: pd.DataFrame, min_months: int = 10) -> pd.Series:
    """Rows with a period -> calendar-year means (monthly rows: years with enough months)."""
    d = d.dropna(subset=["value", "year"])
    if d.empty:
        return pd.Series(dtype=float)
    if (d["period"].str.len() > 4).any():
        n = d.groupby("year")["value"].size()
        a = d.groupby("year")["value"].mean()
        return a[n >= min_months].astype(float)
    return d.groupby("year")["value"].mean().astype(float)


def derive_eu_history(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """eu_history.csv: what each European country's prices, wages, home prices, rents,
    rates, returns and unemployment did, year by year (for backtests against the HFCS):
    HICP (all items and each budget line's index), OECD average wages (current prices;
    else Eurostat full-time-adjusted salaries), Eurostat house prices (else BIS), HICP
    actual rentals, ECB rates on new mortgages, 10-year government bond yields, 3-month
    interbank rates, ECB household term-deposit rates, Eurostat unemployment, and the
    European stock market (Kenneth French) in the country's currency (World Bank rates)."""
    L = long.dropna(subset=["value", "year"])
    eq = L[(L["measure"] == "history_eu") & (L["statistic"] == "equity_return_usd")]
    eq = eq.set_index("year")["value"].astype(float)
    codes = hicp_budget_codes()
    rows = []
    for iso in EUROPE:
        g = L[L["iso3"] == iso]
        if g.empty:
            continue
        out: dict[str, tuple[pd.Series, str]] = {}
        hicp = g[(g["measure"] == "price_level") & (g["statistic"] == "hicp_index")]
        hp = hicp.pivot_table(index="year", columns="category", values="value")
        if "CP00" in hp:
            out["cpi"] = (hp["CP00"].dropna(), "Eurostat HICP CP00")
            for k, c in codes.items():
                if c in hp:
                    out[f"cpi:{k}"] = (hp[c].dropna(), f"Eurostat HICP {c}")
            if "CP041" in hp:
                out["rent"] = (hp["CP041"].dropna(), "Eurostat HICP CP041")
        w = g[(g["measure"] == "avg_wage") & (g["statistic"] == "mean")]
        wo = w[
            w["unit"].astype(str).str.endswith("|V") & ~w["unit"].astype(str).str.startswith("USD")
        ]
        we = w[w["series"] == "nama_10_fte"]
        we = we[we["unit"] == "NAC/year"] if (we["unit"] == "NAC/year").any() else we
        for d, src in ((wo, "OECD average wages (current prices)"), (we, "Eurostat nama_10_fte")):
            if len(d) >= 5:
                out["wage"] = (_yearly(d), src)
                break
        h = g[(g["measure"] == "housing") & (g["statistic"] == "house_price_index")]
        for ser, src in (("prc_hpi_a", "Eurostat HPI"), (None, "BIS (via FRED)")):
            d = (
                h[h["series"] == ser]
                if ser
                else h[h["series"].astype(str).str.contains("628BIS", na=False)]
            )
            if len(d) >= 5:
                out["hpi"] = (_yearly(d), src)
                break
        for key, meas, stat, src in (
            ("mortgage30", "housing", "mortgage_rate_new_loans", "ECB MIR new mortgages"),
            ("y10", "interest_rate", "govt_bond_10y", "Eurostat 10-year yields"),
            ("tbill3m", "interest_rate", "interbank_3m", "Eurostat 3-month rates"),
            ("deposit_savings", "interest_rate", "deposit_term", "ECB MIR deposits"),
            ("unemployment", "unemployment_rate", "rate", "Eurostat une_rt"),
        ):
            d = g[(g["measure"] == meas) & (g["statistic"] == stat)]
            if meas == "unemployment_rate" and (d["series"] == "une_rt_a").any():
                d = d[d["series"] == "une_rt_a"]
            a = _yearly(d)
            if len(a):
                out[key] = (a, src)
        if "tbill3m" in out:
            out["tbill_return"] = out["tbill3m"]
        # mortgage rates before the series starts (outside the euro: 2018-): the 10-year
        # yield plus the country's own median spread where both exist
        if "mortgage30" in out and "y10" in out:
            m, y = out["mortgage30"][0], out["y10"][0]
            both = m.index.intersection(y.index)
            if len(both) >= 3:
                spread = float((m[both] - y[both]).median())
                early = y[y.index < m.index.min()] + spread
                out["mortgage30"] = (
                    pd.concat([early, m]).sort_index(),
                    out["mortgage30"][1] + f"; earlier: 10-year yield + "
                    f"{spread:.2f} pp (median spread)",
                )
        fx = g[(g["measure"] == "price_level") & (g["statistic"] == "exchange_rate")]
        fx = fx.set_index("year")["value"].astype(float).sort_index()
        if len(eq) and len(fx) >= 2:
            r = {}
            for y in eq.index:
                if y in fx.index and y - 1 in fx.index and fx[y - 1] > 0:
                    m = fx[y] / fx[y - 1]
                    if 0.5 < m < 1.5:  # not a currency changeover
                        r[y] = 100 * ((1 + eq[y] / 100) * m - 1)
            if r:
                out["equity_return"] = (
                    pd.Series(r),
                    "Kenneth French Europe market, in local currency (World Bank exchange rates)",
                )
        for key, (ser, src) in out.items():
            rows += [
                {"iso3": iso, "series": key, "year": int(y), "value": float(v), "source": src}
                for y, v in ser.items()
                if v == v
            ]
    return (
        pd.DataFrame(rows).sort_values(["iso3", "series", "year"]).reset_index(drop=True)
        if rows
        else pd.DataFrame()
    )


def hfcs_tables(long: pd.DataFrame | None = None) -> pd.DataFrame:
    """hfcs_tables.csv: the ECB HFCS statistical tables (waves 2010-2021) by age of the
    reference person and other breakdowns, as compiled from the ECB's PDFs by
    tools/hfcs_tables.py (committed in reference/; there is no API for them). Amounts in
    thousands of the country's own currency (the tables state all in euro: non-euro
    countries at the fieldwork year's exchange rate, World Bank)."""
    if not HFCS_TABLES.exists():
        return pd.DataFrame()
    t = pd.read_csv(HFCS_TABLES)
    t["currency"] = "EUR"
    if long is None:
        return t
    fx = long[(long["measure"] == "price_level") & (long["statistic"] == "exchange_rate")]
    fx = fx.dropna(subset=["year"]).groupby(["iso3", "year"])["value"].mean()
    ref = t[(t["table"] == "REF") & (t["indicator"] == "fieldwork_mid")].set_index(
        ["iso3", "wave"]
    )["value"]
    for iso in set(t["iso3"]):
        cur = LOCAL_CURRENCY.get(iso, "EUR")
        if cur == "EUR":
            continue
        for w in set(t.loc[t["iso3"] == iso, "wave"]):
            yr = int(np.floor(ref.get((iso, w), w)))
            try:
                k = float(fx[(iso, yr)] / fx[("DEU", yr)])
            except KeyError:
                log.warning("HFCS %s %s: no exchange rate; left out", iso, w)
                t = t[~((t["iso3"] == iso) & (t["wave"] == w))]
                continue
            m = (t["iso3"] == iso) & (t["wave"] == w) & t["table"].isin(HFCS_MONEY)
            t.loc[m, "value"] *= k
            t.loc[m, "se"] *= k
            t.loc[(t["iso3"] == iso) & (t["wave"] == w), "currency"] = cur
    return t.reset_index(drop=True)

"""Derived macro tables: FX/PPP, context series, rates, markets, US states."""

from __future__ import annotations

import statistics
from datetime import datetime

import pandas as pd

from econdata.config import COUNTRIES, EURO_AREA
from econdata.derive.context import Ctx


def derive_fx_ppp(ctx: Ctx) -> pd.DataFrame:
    rows = []
    keys = sorted({k for t in ctx._wb.values() for k in t})
    for iso3, y in keys:
        if iso3 not in COUNTRIES and iso3 != "EMU":
            continue
        rows.append(
            {
                "iso3": iso3,
                "year": y,
                "lcu_per_usd": ctx._wb["exchange_rate"].get((iso3, y)),
                "lcu_per_intl_dollar": ctx._wb["ppp_private_consumption"].get((iso3, y)),
                "price_level_ratio": ctx._wb["price_level_ratio"].get((iso3, y)),
                "cpi_index": ctx._wb["cpi_index"].get((iso3, y)),
                "source": "World Bank WDI (PA.NUS.FCRF, PA.NUS.PRVT.PP, FP.CPI.TOTL)",
            }
        )
    return pd.DataFrame(rows)


CONTEXT = [  # (indicator, measure, statistic regex, age_band regex or None)
    ("unemployment_rate", "unemployment_rate", "rate", r"^(Y15-74|Y_GE15)$"),
    ("youth_unemployment_rate", "unemployment_rate", "rate", r"^(Y15-24|Y15T24)$"),
    ("job_vacancy_rate", "job_vacancy_rate", "rate", None),
    ("saving_rate_net", "saving_rate_net", "rate", None),
    ("saving_rate_gross", "saving_rate_gross", "rate", None),
    ("world_uncertainty_index", "uncertainty", "^wui", None),
    ("political_stability", "uncertainty", "political_stability", None),
    ("house_price_index", "housing", "house_price_index", None),
]


def derive_context(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Small annual panel for the UI context charts."""
    parts = []
    for ind, m, stat, age in CONTEXT:
        d = ctx.measure(m)
        d = d[d["statistic"].astype(str).str.contains(stat, regex=True)]
        if age:
            d = d[d["age_band"].astype(str).str.contains(age, regex=True)]
        if d.empty:
            continue
        g = d.groupby(["iso3", "year"], as_index=False).agg(
            value=("value", "mean"),
            unit=("unit", "first"),
            source=("source", "first"),
            estimated=("estimated", "any"),
            n=("value", "size"),
        )
        parts.append(g.assign(indicator=ind))
    cpi = pd.DataFrame(
        [{"iso3": c, "year": y, "value": v} for (c, y), v in ctx._wb["cpi_index"].items()],
        columns=["iso3", "year", "value"],  # empty when the World Bank was unreachable
    ).sort_values(["iso3", "year"])
    if not cpi.empty:
        cpi["value"] = cpi.groupby("iso3")["value"].pct_change() * 100
        parts.append(
            cpi.dropna().assign(
                indicator="inflation_cpi",
                unit="% y/y",
                source="World Bank WDI",
                estimated=False,
                n=1,
            )
        )
    df = pd.concat(parts, ignore_index=True)
    return df[df["iso3"].isin(COUNTRIES)]


# central-bank inflation targets (statutory; the mid-point where a range is set). Denmark
# pegs to the euro; Switzerland's "price stability" is 0-2%.
INFLATION_TARGET = {
    "USA": 2.0,
    "GBR": 2.0,
    "CAN": 2.0,
    "CHE": 1.0,
    "LIE": 1.0,
    "NOR": 2.0,
    "SWE": 2.0,
    "DNK": 2.0,
    "ISL": 2.5,
    "CZE": 2.0,
    "POL": 2.5,
    "HUN": 3.0,
    "ROU": 2.5,
    **{c: 2.0 for c in EURO_AREA},
}


TERM_PREMIUM = 0.5  # 10-year yield over the expected average short rate (pp); recent


#                      ACM / Kim-Wright estimates for US Treasuries are ~0.3-0.8


def derive_rates(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Forward-looking interest rates per country. The 10-year government yield, less a
    term premium, is the average short rate markets expect over the next decade; a fixed
    mortgage period resets to today's mortgage rate moved by that expected change in
    short rates, instead of to the historical average. (The usual mortgage spread over
    the 10-year yield is kept for reference.)"""
    ir = long[(long["measure"] == "interest_rate")].dropna(subset=["year", "value"])
    pref = {
        "mortgage_rate_new_loans": 0,
        "mortgage_rate_10y_fixed_median": 1,
        "mortgage_rate_2y_fixed_75ltv": 1,
        "mortgage_rate_30y_fixed": 1,
    }
    m = long[long["statistic"].isin(pref)].dropna(subset=["year", "value"])
    last_full = datetime.now().year - 1
    rows = []
    for iso in COUNTRIES:
        src_iso = "CHE" if iso == "LIE" else iso
        r = {"iso3": iso, "inflation_target": INFLATION_TARGET.get(iso, 2.0)}
        g = ir[ir["iso3"] == src_iso]
        for stat, col in (
            ("govt_bond_10y", "y10"),
            ("interbank_3m", "short_rate"),
            ("deposit_overnight", "deposit_overnight"),
            ("deposit_term", "deposit_term"),
        ):
            s = g[g["statistic"] == stat].sort_values("period")
            if s.empty:
                continue
            last = pd.Period(str(s["period"].iloc[-1])[:7], "M")
            if (pd.Period(datetime.now(), "M") - last).n > 18:
                continue  # discontinued (e.g. Iceland in the OECD table): not current
            tail = s.tail(3)  # last 3 months: smooths one-off moves
            r[col] = round(float(tail["value"].mean()), 3)
            r[f"{col}_period"] = str(tail["period"].iloc[-1])
            r[f"{col}_source"] = str(s["source"].iloc[-1])
        y10 = g[g["statistic"] == "govt_bond_10y"].groupby("year")["value"].mean()
        mm = m[m["iso3"] == src_iso]
        if not mm.empty and not y10.empty:
            mm = mm[mm["statistic"].map(pref) == mm["statistic"].map(pref).min()]
            my = mm.groupby("year")["value"].mean()
            both = pd.concat([my.rename("m"), y10.rename("y")], axis=1).dropna()
            # last 10 years: spreads moved a lot as e.g. Bulgaria and the Baltics converged
            both = both[(both.index >= last_full - 9) & (both.index <= last_full)]
            if len(both) >= 5:
                r["mortgage_spread"] = round(float((both["m"] - both["y"]).median()), 3)
                r["spread_years"] = f"{int(both.index.min())}-{int(both.index.max())}"
                r["mortgage_series"] = str(mm["statistic"].iloc[0])
        if not mm.empty:
            mm2 = mm[mm["statistic"].map(pref) == mm["statistic"].map(pref).min()]
            per = mm2.sort_values("period")
            r["mortgage_rate_now"] = round(float(per.tail(3)["value"].mean()), 3)
            r["mortgage_rate_now_period"] = str(per["period"].iloc[-1])
        if "y10" in r and "short_rate" in r and "mortgage_rate_now" in r:
            # expectations hypothesis: 10-year yield - term premium = the average short
            # rate markets expect over the next decade; mortgage rates move with it
            exp_short = r["y10"] - TERM_PREMIUM
            r["expected_short_rate"] = round(exp_short, 3)
            r["mortgage_rate_future"] = round(
                max(r["mortgage_rate_now"] + exp_short - r["short_rate"], 0.5), 2
            )
        rows.append(r)
    # deposit rates where no central source publishes them: the 3-month rate times the
    # median pass-through (deposit rate / 3-month rate) of the euro area and the US
    for col in ("deposit_overnight", "deposit_term"):
        ratios = [
            r[col] / r["short_rate"] for r in rows if col in r and r.get("short_rate", 0) > 0.5
        ]
        if not ratios:
            continue
        k = float(statistics.median(ratios))
        for r in rows:
            if col not in r and "short_rate" in r:
                r[col] = round(max(r["short_rate"] * k, 0.0), 3)
                r[f"{col}_period"] = r["short_rate_period"]
                r[f"{col}_source"] = (
                    f"estimate: 3-month rate x {k:.2f}, the median pass-"
                    f"through in the {len(ratios)} countries with ECB/FDIC "
                    "household deposit rates"
                )
                r[f"{col}_estimated"] = True
    return pd.DataFrame(rows)


def derive_market_index(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Yearly home-price and rent indices per country (current year = months so far), used
    to move curated price/rent levels from their survey year to the latest data."""
    h = long[long["measure"] == "housing"].dropna(subset=["year", "value"])
    rows = []
    for stat, idx in (
        ("house_price_index", "home_price"),
        ("rent_index", "rent"),
        ("median_existing_single_family_price", "home_price"),
    ):
        d = h[h["statistic"] == stat]
        for (iso, yr), g in d.groupby(["iso3", "year"]):
            sub = (
                g[g["frequency"].isin(["M", "Q"])] if g["frequency"].isin(["M", "Q"]).any() else g
            )
            rows.append(
                {
                    "iso3": iso,
                    "index": idx,
                    "year": int(yr),
                    "value": float(sub["value"].mean()),
                    "n": int(len(sub)),
                    "source": str(g["source"].iloc[0]),
                    "estimated": bool(g["estimated"].any())
                    or (g["frequency"].iloc[0] == "M" and len(sub) < 12),
                }
            )
    # fuel: HICP fuels and lubricants (CP0722) in Europe, EIA gasoline price (US)
    pl = long[
        (long["measure"] == "price_level")
        & (long["statistic"] == "hicp_index")
        & (long["category"].astype(str) == "CP0722")
    ].dropna(subset=["year", "value"])
    for (iso, yr), g in pl.groupby(["iso3", "year"]):
        rows.append(
            {
                "iso3": iso,
                "index": "fuel",
                "year": int(yr),
                "value": float(g["value"].mean()),
                "n": int(len(g)),
                "source": "Eurostat HICP (fuels, CP0722)",
                "estimated": False,
            }
        )
    gas = long[(long["statistic"] == "gasoline_price")].dropna(subset=["year", "value"])
    for (iso, yr), g in gas.groupby(["iso3", "year"]):
        rows.append(
            {
                "iso3": iso,
                "index": "fuel",
                "year": int(yr),
                "value": float(g["value"].mean()),
                "n": int(len(g)),
                "source": str(g["source"].iloc[0]) + " (gasoline)",
                "estimated": len(g) < 50,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # one source per country and index: the most recent one (Eurostat stops for the UK in
    # 2019), Eurostat on a tie
    last = df.groupby(["iso3", "index", "source"])["year"].transform("max")
    cnt = df.groupby(["iso3", "index", "source"])["year"].transform("count")
    # newest first, then Eurostat, then the longest series (US: BIS index over NAR medians)
    df["rank"] = -last * 10_000 + df["source"].map(lambda x: 0 if "Eurostat" in x else 5_000) - cnt
    best = df.groupby(["iso3", "index"])["rank"].transform("min")
    return df[df["rank"] == best].drop(columns="rank").sort_values(["iso3", "index", "year"])


def derive_us_states(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    s = long[long["measure"] == "us_state"].dropna(subset=["value"])
    if s.empty:
        return pd.DataFrame()
    t = s.pivot_table(index="category", columns="statistic", values="value", aggfunc="last")
    yrs = s.groupby("statistic")["year"].max()
    t["acs_year"] = int(yrs.drop("rpp", errors="ignore").max())
    if "rpp" in yrs:
        t["rpp_year"] = int(yrs["rpp"])
    return t.reset_index().rename(columns={"category": "name"})


def derive_mortgage_longrun(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Average new-loan mortgage rate over the last 20 complete years (the rate a fixed
    period resets to), from the same series as the current-rate defaults."""
    pref = {
        "mortgage_rate_new_loans": 0,
        "mortgage_rate_10y_fixed_median": 1,
        "mortgage_rate_2y_fixed_75ltv": 1,
        "mortgage_rate_30y_fixed": 1,
    }
    m = long[long["statistic"].isin(pref)].dropna(subset=["year", "value"])
    last = datetime.now().year - 1
    m = m[m["year"].between(last - 19, last)]
    rows = []
    for iso, g in m.groupby("iso3"):
        g = g[g["statistic"].map(pref) == g["statistic"].map(pref).min()]
        y = g.groupby("year")["value"].mean()
        rows.append(
            {
                "iso3": iso,
                "rate_longrun": round(float(y.mean()), 2),
                "years": int(len(y)),
                "from": int(y.index.min()),
                "to": int(y.index.max()),
                "source": str(g["source"].iloc[0]),
                "series": str(g["statistic"].iloc[0]),
            }
        )
    return pd.DataFrame(rows)

"""Derived life events: home purchase, children, family, pensions, university."""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from econdata.config import COUNTRIES, EURO_DATA, EUROPE, LOCAL_CURRENCY
from econdata.derive.budget import SHS_COUPLE, SHS_KIDS, _match_hh, _per_household, _shs_by_cat
from econdata.derive.context import Ctx
from econdata.sources.spending import (
    CE_AVG_CHILDREN,
    HBS_AVG_CHILDREN,
    HH_1CHILD,
    HH_ANYCHILD,
    HH_COUPLE,
    SHS_AVG_CHILDREN,
)
from econdata.tidy import latest


# ---------- Life events ----------------------------------------------------
def mortgage_payment(principal: float, rate_pct: float, years: int) -> float:
    r = rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def derive_life_events(long: pd.DataFrame, ctx: Ctx, home_m2: float) -> pd.DataFrame:
    """life_events.csv: wedding, children, house purchase, rents, emergencies and the
    survey households' income, one row per item."""
    rows = _Rows()
    _wedding_costs(rows, ctx)
    _child_costs(rows, long, ctx)
    _house_purchase(rows, ctx, home_m2)
    _rents(rows, ctx)
    _emergencies(rows, long, ctx)
    _survey_households(rows, long, ctx)
    return pd.DataFrame(rows)


class _Rows(list):
    """The rows of life_events.csv; `add` skips missing values."""

    def add(self, iso3, event, item, value, unit, year=None, source="", method="", quality=""):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        self.append(
            {
                "iso3": iso3,
                "country": COUNTRIES.get(iso3, ("", "", iso3))[2],
                "event": event,
                "item": item,
                "value": value,
                "unit": unit,
                "year": year,
                "source": source,
                "method": method,
                "quality": quality,
            }
        )


def _wedding_costs(rows: _Rows, ctx: Ctx) -> None:
    """Average wedding cost: the reference value, else the US mean scaled by price levels."""
    ly = ctx.latest_year
    us_w = ctx.ref("USA", "average_wedding_cost")
    for iso3 in COUNTRIES:
        r = ctx.ref(iso3, "average_wedding_cost")
        if r is not None and r["iso3"] == iso3:
            rows.add(
                iso3,
                "wedding",
                "average_cost",
                r["value"],
                r["unit"],
                r["as_of"],
                r["source"],
                "reference (mean)",
                r["quality"],
            )
        elif us_w is not None:
            # scale the US mean by relative price levels (PPP/FX), in local currency
            plr_us, _ = ctx.wb("price_level_ratio", "USA", ly)
            plr, _ = ctx.wb("price_level_ratio", iso3, ly)
            fx, _ = ctx.wb("exchange_rate", iso3, ly)
            if plr and plr_us and fx:
                rows.add(
                    iso3,
                    "wedding",
                    "average_cost",
                    us_w["value"] * (plr / plr_us) * fx,
                    LOCAL_CURRENCY.get(iso3, "EUR"),
                    ly,
                    "derived from The Knot (US)",
                    "US mean x relative price level x FX: VERY rough",
                    "C",
                )


def _child_costs(rows: _Rows, long: pd.DataFrame, ctx: Ctx) -> None:
    """Net childcare costs and the extra spending of couples with children."""
    ncc = long[(long["measure"] == "childcare_net_cost")]
    for iso3, g in ncc.groupby("iso3"):
        g = latest(g, ["household_type", "statistic", "unit", "category"])
        for r in g.itertuples():
            rows.add(
                iso3,
                "children",
                f"childcare_net_cost [{r.statistic}|{r.category}]",
                r.value,
                r.unit,
                r.year,
                "OECD TaxBEN",
                r.household_type,
                "A",
            )
    ce = long[(long["source"] == "BLS CE (via FRED)") & (long["category"] == "TOTALEXP")]
    if not ce.empty:
        y = int(ce["year"].max())
        c = ce[ce["year"] == y].set_index("household_type")["value"]
        base = c.get("married couple only")
        for grp in ("married couple, oldest child under 6", "married couple, oldest child 6-17"):
            if base is not None and grp in c:
                rows.add(
                    "USA",
                    "children",
                    f"extra_spending_vs_couple ({grp})",
                    (c[grp] - base) / 12,
                    "USD/month",
                    y,
                    "BLS CE",
                    "household with children minus couple-only (all children, ~1.9 avg)",
                    "B",
                )
    # HBS publishes only "two adults with dependent children" (no 1/2-child split): the
    # per-child figure divides by the EU-average number of children in such households.
    hb = ctx.measure("household_spending_raw")
    hb = hb[hb["statistic"] == "total_by_hhtype"]
    for iso3, g in hb.groupby("iso3"):
        y = int(g["year"].max())
        g, _ = _per_household(g[g["year"] == y])
        if g["unit"].astype(str).str.contains("AE", case=False).all():
            continue  # per-adult-equivalent units cannot give a child's extra cost

        def first(pats):
            lab = _match_hh(g["household_type"], pats)
            return None if lab is None else g[g["household_type"] == lab]["value"].iloc[0]

        base, one, anyk = first(HH_COUPLE), first(HH_1CHILD), first(HH_ANYCHILD)
        nac = ctx.nac_per_pps(iso3, y)
        if not (base and nac):
            continue
        f, py, _ = ctx.hicp_factor(iso3, "CP00", y)
        cur = LOCAL_CURRENCY.get(iso3, "EUR") + "/month"
        if one:
            rows.add(
                iso3,
                "children",
                "extra_spending_per_child",
                (one - base) * nac * f / 12,
                cur,
                py,
                "Eurostat HBS",
                f"HBS {y}: 2 adults+1 child minus 2 adults, HICP",
                "B",
            )
        elif anyk:
            rows.add(
                iso3,
                "children",
                "extra_spending_per_child",
                (anyk - base) / HBS_AVG_CHILDREN * nac * f / 12,
                cur,
                py,
                "Eurostat HBS",
                f"ESTIMATE: HBS {y} (2 adults with dependent children - 2 adults) / "
                f"{HBS_AVG_CHILDREN} avg children, rolled by HICP",
                "C",
            )
    fs = long[
        long["source"].astype(str).str.startswith("ONS Family Spending")
        & (long["category"] == "TOTAL")
    ]
    if not fs.empty:
        t = fs.set_index("household_type")["value"]
        if "two adults" in t and "two adults, one child" in t:
            rows.add(
                "GBR",
                "children",
                "extra_spending_per_child",
                (t["two adults, one child"] - t["two adults"]) * 52 / 12,
                "GBP/month",
                int(fs["year"].iloc[0]),
                "ONS Family Spending",
                "2 adults+1 child minus 2 non-retired adults (total incl. 'other items')",
                "B",
            )
    yk, kids = _shs_by_cat(long, SHS_KIDS)
    _, cpl = _shs_by_cat(long, SHS_COUPLE)
    if kids.get("TOTAL") and cpl.get("TOTAL"):
        rows.add(
            "CAN",
            "children",
            "extra_spending_per_child",
            (kids["TOTAL"] - cpl["TOTAL"]) / SHS_AVG_CHILDREN / 12,
            "CAD/month",
            yk,
            "StatCan SHS",
            f"ESTIMATE: (couples with children - without) / {SHS_AVG_CHILDREN} "
            "avg children; current consumption",
            "C",
        )
    if "USA" in COUNTRIES:
        for r in [x for x in rows if x["iso3"] == "USA" and "oldest child under 6" in x["item"]]:
            rows.add(
                "USA",
                "children",
                "extra_spending_per_child",
                r["value"] / CE_AVG_CHILDREN,
                "USD/month",
                r["year"],
                "BLS CE",
                f"ESTIMATE: (married couple, oldest child <6 - couple only) / {CE_AVG_CHILDREN}",
                "C",
            )


def _house_purchase(rows: _Rows, ctx: Ctx, home_m2: float) -> None:
    """Home price, down payment, transfer tax, mortgage rate and payments."""
    hpi = ctx.measure("housing")
    hpi = hpi[hpi["statistic"] == "house_price_index"]
    rates = ctx.measure("housing")
    rates = rates[rates["statistic"].astype(str).str.startswith("mortgage_rate")]
    rate_pref = [
        "mortgage_rate_new_loans",
        "mortgage_rate_10y_fixed_median",
        "mortgage_rate_30y_fixed",
        "mortgage_rate_2y_fixed_75ltv",
        "mortgage_rate_5y_posted",
    ]
    prices: dict[str, tuple] = {}
    for iso3 in COUNTRIES:
        price, unit, src, yr, q = None, LOCAL_CURRENCY.get(iso3, "EUR"), "", None, "B"
        m2 = ctx.ref(iso3, "new_dwelling_price_per_m2")
        if iso3 == "USA":
            s_ = ctx.mi("housing", "USA")
            s_ = s_[s_["statistic"] == "median_existing_single_family_price"]
            if not s_.empty:
                s_ = s_.sort_values("period").iloc[-1]
                price, src, yr = (
                    s_["value"],
                    "NAR via FRED (median existing single-family)",
                    s_["period"],
                )
        elif iso3 in ("CAN", "GBR"):
            r = ctx.ref(iso3, "average_home_price")
            if r is not None:
                price, src, yr = r["value"], r["source"] + " (mean)", r["as_of"]
        elif m2 is not None and m2["iso3"] == iso3:
            p = m2["value"] * home_m2
            h = hpi[hpi["iso3"] == iso3].sort_values("year")
            if not h.empty and (h["year"] == 2025).any():
                p *= h["value"].iloc[-1] / h[h["year"] == 2025]["value"].iloc[0]
                yr = int(h["year"].iloc[-1])
            else:
                yr = 2025
            price = ctx.to_local(p, "EUR", iso3, yr) if iso3 not in EURO_DATA else p
            src = f"Deloitte new-dwelling EUR/m2 x {home_m2:g} m2 x Eurostat HPI"
        if price is not None:
            prices[iso3] = (price, unit, src, yr, q)
    # Countries without a price: median price-to-income ratio of the others (Europe)
    # x local median couple disposable income. ESTIMATE, quality C.
    inc = {}
    hi = ctx.measure("household_income")
    hi = hi[
        (hi["statistic"] == "median")
        & hi["unit"].astype(str).str.startswith("NAC")
        & hi["household_type"].astype(str).str.fullmatch("Two adults", case=False)
    ]
    for iso3, g in hi.groupby("iso3"):
        inc[iso3] = g.sort_values("year")["value"].iloc[-1] * 1.5  # equivalised -> couple
    ratios = [prices[c][0] / inc[c] for c in prices if c in inc and c in EUROPE]
    if ratios:
        pti = float(pd.Series(ratios).median())
        for iso3 in EUROPE:
            if iso3 not in prices and iso3 in inc:
                prices[iso3] = (
                    pti * inc[iso3],
                    LOCAL_CURRENCY.get(iso3, "EUR"),
                    f"ESTIMATE: median European price-to-income ratio "
                    f"({pti:.1f}x couple disposable income, {len(ratios)} countries)"
                    " x local EU-SILC median",
                    ctx.latest_year,
                    "C",
                )
    for iso3, (price, unit, src, yr, q) in prices.items():
        rows.add(
            iso3,
            "house",
            "home_price",
            price,
            unit,
            yr,
            src,
            "national level; suburban resale homes are often cheaper",
            q,
        )
        dp = ctx.ref(iso3, "down_payment_pct_first_time")
        if iso3 == "CAN":
            dp_amt = (
                0.05 * min(price, 500000) + 0.10 * max(min(price, 1.5e6) - 500000, 0)
                if price < 1.5e6
                else 0.2 * price
            )
            dp_pct = dp_amt / price * 100
            dp_src = "Canadian minimum down payment rules"
        elif dp is not None:
            dp_pct, dp_src = dp["value"], dp["source"]
        else:
            dp_pct, dp_src = 20, "assumption"
        rows.add(iso3, "house", "down_payment_pct", dp_pct, "%", None, dp_src, "", "B")
        rows.add(
            iso3, "house", "down_payment_amount", price * dp_pct / 100, unit, yr, dp_src, "", "B"
        )
        tt = ctx.ref(iso3, "transfer_tax_pct")
        if tt is not None and pd.notna(tt["value"]):
            rows.add(
                iso3,
                "house",
                "transfer_tax_pct",
                tt["value"],
                "%",
                tt["as_of"],
                tt["source"],
                str(tt["note"]),
                "B",
            )
            rows.add(
                iso3,
                "house",
                "transfer_tax_amount",
                price * tt["value"] / 100,
                unit,
                yr,
                tt["source"],
                f"{tt['value']}% ({tt['note']})",
                "B",
            )
    for iso3 in COUNTRIES:
        rr = rates[rates["iso3"] == iso3]
        rate = None
        for st in rate_pref:
            s_ = rr[rr["statistic"] == st].sort_values("period")
            if not s_.empty:
                rate, per, rsrc, rst = (
                    float(s_["value"].iloc[-1]),
                    s_["period"].iloc[-1],
                    s_["source"].iloc[-1],
                    st,
                )
                q = "A"
                break
        if rate is None:
            ref = ctx.ref(iso3, "mortgage_rate")
            if ref is not None and ref["iso3"] == iso3:
                rate, per, rsrc, rst, q = (
                    float(ref["value"]),
                    ref["as_of"],
                    ref["source"],
                    "reference",
                    ref["quality"],
                )
        if rate is None:
            continue
        rows.add(iso3, "house", "mortgage_rate", rate, "%", per, rsrc, rst, q)
        if iso3 in prices:
            price, unit = prices[iso3][0], prices[iso3][1]
            dp_pct = next(
                x["value"] for x in rows if x["iso3"] == iso3 and x["item"] == "down_payment_pct"
            )
            for yrs in (25, 30):
                rows.add(
                    iso3,
                    "house",
                    f"mortgage_payment_{yrs}y",
                    mortgage_payment(price * (1 - dp_pct / 100), rate, yrs),
                    unit + "/month",
                    None,
                    "derived",
                    f"annuity on price x (1-down payment) at {rate:.2f}%",
                    "B",
                )


def _rents(rows: _Rows, ctx: Ctx) -> None:
    """Rents (and how they rise with children)."""
    # EU-SILC total housing cost of TENANTS (tenure=RENT; includes reduced-rent
    # tenants) for TWO-ADULT households, incl. utilities. Ratios for households with
    # children let the simulation scale rent when kids arrive.
    silc = ctx.measure("housing")
    silc = silc[silc["statistic"] == "mean_total_housing_cost"]
    tenant = silc[silc["category"].astype(str).str.contains("tenant|market price", case=False)]
    for iso3, g in tenant.groupby("iso3"):
        y = int(g["year"].max())
        g = g[g["year"] == y]
        hh = g.set_index(g["household_type"].astype(str).str.lower())["value"]
        base = hh.get("two adults")
        label = "two adults"
        if base is None:
            base, label = hh.get("total"), "all households"
        nac = ctx.nac_per_pps(iso3, y)
        if base is None or not nac or "PPS" not in str(g["unit"].iloc[0]):
            continue
        rows.add(
            iso3,
            "rent",
            "tenant_total_housing_cost",
            base * nac,
            LOCAL_CURRENCY.get(iso3, "EUR") + "/month",
            y,
            "Eurostat EU-SILC",
            f"MEAN total housing cost of tenants ({label}) incl. utilities; "
            "includes reduced-rent tenants",
            "A",
        )
        for k, lab in (
            ("two adults with one dependent child", "rent_factor_one_child"),
            ("two adults with two dependent children", "rent_factor_two_children"),
        ):
            if k in hh and label == "two adults":
                rows.add(
                    iso3,
                    "rent",
                    lab,
                    hh[k] / base,
                    "ratio",
                    y,
                    "Eurostat EU-SILC",
                    f"tenant housing cost, {k} / two adults",
                    "A",
                )
    acs = ctx.mi("housing", "USA")
    acs = acs[acs["statistic"] == "median_gross_rent"].sort_values("year")
    if not acs.empty:
        rows.add(
            "USA",
            "rent",
            "median_gross_rent",
            acs["value"].iloc[-1],
            "USD/month",
            int(acs["year"].iloc[-1]),
            "Census ACS",
            "contract rent + utilities",
            "A",
        )
    else:
        r = ctx.ref("USA", "median_gross_rent")
        if r is not None:
            rows.add(
                "USA",
                "rent",
                "median_gross_rent",
                r["value"],
                "USD/month",
                r["as_of"],
                r["source"],
                "contract rent + utilities (reference fallback)",
                r["quality"],
            )
    cmhc = ctx.mi("housing", "CAN")
    cmhc = cmhc[cmhc["statistic"] == "average_rent_2br"]
    if not cmhc.empty:
        y = int(cmhc["year"].max())
        rows.add(
            "CAN",
            "rent",
            "median_of_centres_avg_rent_2br",
            cmhc[cmhc["year"] == y]["value"].median(),
            "CAD/month",
            y,
            "CMHC",
            "median across all centres 10k+ (unweighted; smaller centres count equally)",
            "B",
        )
    uk = ctx.ref("GBR", "average_monthly_rent")
    if uk is not None:
        rows.add(
            "GBR",
            "rent",
            "average_private_rent",
            uk["value"],
            "GBP/month",
            uk["as_of"],
            uk["source"],
            "mean",
            "A",
        )


def _emergencies(rows: _Rows, long: pd.DataFrame, ctx: Ctx) -> None:
    """Unexpected expenses, job-loss replacement rates and health-spending risk."""
    em = long[long["measure"] == "emergency"]
    for iso3, g in em.groupby("iso3"):
        for stat in ("share_cannot_cover_unexpected_expense", "unexpected_expense_size"):
            s = g[g["statistic"] == stat]
            if stat.startswith("share"):
                pick = s[
                    s["household_type"].astype(str).str.contains("two adults", case=False)
                    & ~s["household_type"]
                    .astype(str)
                    .str.contains("child|65 years or over|at least one", case=False)
                ]
                s = (
                    pick
                    if not pick.empty
                    else s[s["household_type"].astype(str).str.contains("total", case=False)]
                )
            if s.empty:
                continue
            r = s.sort_values("year").iloc[-1]
            rows.add(
                iso3,
                "emergency",
                stat,
                r["value"],
                r["unit"],
                int(r["year"]),
                r["source"],
                str(r["household_type"]),
                "A",
            )
        nrr = g[g["statistic"].astype(str).str.startswith("net_replacement_rate")]
        for (stat, hh), s in nrr.groupby(["statistic", "household_type"]):
            r = s.sort_values("year").iloc[-1]
            rows.add(
                iso3,
                "emergency",
                f"{stat} ({hh})",
                r["value"],
                r["unit"],
                int(r["year"]),
                "OECD TaxBEN",
                "job loss: household net income kept",
                "A",
            )
    hl = long[
        (long["measure"] == "health")
        & long["statistic"].isin(
            [
                "share_oop_over_10pct_income",
                "share_oop_over_25pct_income",
                "out_of_pocket_per_capita",
            ]
        )
    ]
    for (iso3, stat), s in hl.groupby(["iso3", "statistic"]):
        r = s.sort_values("year").iloc[-1]
        rows.add(
            iso3,
            "emergency",
            stat,
            r["value"],
            r["unit"],
            int(r["year"]),
            "WHO/World Bank",
            "catastrophic health spending risk",
            "A",
        )
    for _, r in ctx.refs[ctx.refs["topic"] == "emergency"].iterrows():
        rows.add(
            r["iso3"],
            "emergency",
            r["item"],
            r["value"],
            r["unit"],
            r["as_of"],
            r["source"],
            r["note"],
            r["quality"],
        )


def _survey_households(rows: _Rows, long: pd.DataFrame, ctx: Ctx) -> None:
    """Income and housing costs of the households behind the survey budget."""
    # ---- income of the households behind the survey budget (all years; the app picks
    # the one matching the HBS survey year and scales the budget with the simulated
    # couple's income relative to it)
    si = long[
        (long["measure"] == "household_income")
        & (long["series"].astype(str) == "ilc_di04:A2_2LT65")
        & long["unit"].astype(str).str.startswith("NAC")
    ]
    for (iso3, year), s in si.groupby(["iso3", "year"]):
        rows.add(
            iso3,
            "budget",
            "silc_median_equivalised_A2_lt65",
            float(s["value"].iloc[0]),
            LOCAL_CURRENCY.get(iso3, "EUR") + "/year (equivalised)",
            int(year),
            "Eurostat EU-SILC ilc_di04 (two adults younger than 65)",
            "median equivalised net income; SILC year = income year + 1",
            "A",
        )
    # housing costs of the same (two-adult, all tenures) households: the budget scaling
    # works on income AFTER housing, so renters are not given owners' spending power
    hc = long[
        (long["statistic"] == "mean_total_housing_cost")
        & (long["category"].astype(str) == "Total")
        & (long["household_type"].astype(str) == "Two adults")
    ]
    for (iso3, year), s in hc.groupby(["iso3", "year"]):
        nac = ctx.nac_per_pps(iso3, int(year))
        if nac:
            rows.add(
                iso3,
                "budget",
                "silc_A2_mean_housing_cost",
                float(s["value"].iloc[0]) * nac,
                LOCAL_CURRENCY.get(iso3, "EUR") + "/month",
                int(year),
                "Eurostat EU-SILC ilc_mded03 (two adults, all tenures)",
                "mean total housing cost incl. utilities, net of allowances; PPS x PPP",
                "A",
            )


# ---------- Families, survey coverage, pensions -------------------------------------
FAM_EDU = {
    "all": "all",
    "below_upper_secondary": "below_upper_secondary",
    "upper_secondary": "upper_secondary",
    "tertiary": "tertiary",
}


def _latest_by(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return df.sort_values("year").groupby(keys, dropna=False).tail(1)


def derive_family(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Child penalty in employment and hours for the primary parent, and the age children
    leave home. earnings_factor = (employment rate x mean hours of employed) of women
    25-49 with n children and the youngest in a band / the same for women without
    children (Europe: mean hours = FT hours x (1 - PT share) + PT hours x PT share)."""
    rows = []
    fw = long[long["measure"] == "family_work"].dropna(subset=["year", "value"])
    for iso, g in fw.groupby("iso3"):
        g = _latest_by(g, ["statistic", "category", "household_type"])
        get = lambda st, ht, cat="all": g.loc[
            (g.statistic == st)
            & (g.household_type == ht)  # noqa
            & (g.category == cat),
            "value",
        ]
        src = str(g["source"].iloc[0])
        yr = int(g["year"].max())
        hft, hpt = get("hours_ft", "any"), get("hours_pt", "any")

        def hours(ht):
            mh = get("mean_hours", ht)
            if not mh.empty:
                return float(mh.iloc[0])
            pt = get("part_time_share", ht)
            if pt.empty:
                return None
            ft_h = float(hft.iloc[0]) if not hft.empty else 40.0
            pt_h = float(hpt.iloc[0]) if not hpt.empty else 0.55 * ft_h
            return ft_h * (1 - pt.iloc[0] / 100) + pt_h * pt.iloc[0] / 100

        h0 = hours("0|none")
        for ht in sorted(set(g["household_type"]) - {"0|none", "any"}):
            n, yb = ht.split("|")
            hk = hours(ht)
            for cat in FAM_EDU:
                e0, ek = get("employment_rate", "0|none", cat), get("employment_rate", ht, cat)
                if e0.empty or ek.empty or not e0.iloc[0]:
                    continue
                er = float(ek.iloc[0] / e0.iloc[0])
                hr = (hk / h0) if hk and h0 else 1.0
                for item, v in (
                    ("employment_ratio", er),
                    ("hours_ratio", hr),
                    ("earnings_factor", er * hr),
                ):
                    rows.append(
                        {
                            "iso3": iso,
                            "item": item,
                            "n_children": n,
                            "youngest": yb,
                            "education": cat,
                            "value": round(min(v, 1.1), 3),
                            "year": yr,
                            "source": src,
                            "estimated": True,
                            "method": "women 25-49 with children vs without",
                        }
                    )
    lh = long[long["measure"] == "age_leave_home"].dropna(subset=["year", "value"])
    for iso, g in lh.groupby("iso3"):
        r = g.sort_values("year").iloc[-1]
        rows.append(
            {
                "iso3": iso,
                "item": "age_leave_home",
                "n_children": "",
                "youngest": "",
                "education": "all",
                "value": round(float(r["value"]), 1),
                "year": int(r["year"]),
                "source": str(r["source"]),
                "estimated": int(r["year"]) < datetime.now().year - 3,
                "method": "" if pd.isna(r["method"]) else str(r["method"]),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # countries with no data of their own: LIE <- CHE; otherwise nothing (engine default)
    if "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        df = pd.concat(
            [
                df,
                df[df["iso3"] == "CHE"].assign(
                    iso3="LIE", estimated=True, method="proxy: Switzerland"
                ),
            ]
        )
    return df


def derive_pensions(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """One row per (country, item): net replacement rates at 0.5 / 1 / 2 x the average wage
    (mandatory), the retirement age for today's labour-market entrants, and life
    expectancy at 65 now and projected for 2065, by sex."""
    rows = []
    p = long[long["measure"] == "pension_replacement"].dropna(subset=["year", "value"])
    if not p.empty:
        p = _latest_by(p, ["iso3", "statistic", "category"])
        item = {
            "net_rate_half_average": "nrr50",
            "net_rate_average_earner": "nrr100",
            "net_rate_double_average": "nrr200",
        }
        for r in p.itertuples(index=False):
            if r.statistic in item:
                rows.append(
                    {
                        "iso3": r.iso3,
                        "item": item[r.statistic]
                        + ("" if r.category == "mandatory" else "_voluntary"),
                        "value": float(r.value),
                        "year": int(r.year),
                        "source": str(r.source),
                    }
                )
    ret = long[long["measure"] == "retirement"].dropna(subset=["year", "value"])
    for (iso, stat), g in ret.groupby(["iso3", "statistic"]):
        if stat == "life_expectancy_at_65":
            now = (
                int(g.loc[g["year"] <= datetime.now().year, "year"].max())
                if (g["year"] <= datetime.now().year).any()
                else None
            )
            for (yr, sex), h in g.groupby(["year", "category"]):
                tag = "now" if now is not None and int(yr) == now else str(int(yr))
                if tag == "now" or int(yr) > datetime.now().year:
                    rows.append(
                        {
                            "iso3": iso,
                            "item": f"le65_{sex}_{tag}",
                            "value": float(h["value"].mean()),
                            "year": int(yr),
                            "source": "OECD Pensions at a Glance (UN WPP)",
                        }
                    )
        else:
            g = g[g["year"] == g["year"].max()]
            rows.append(
                {
                    "iso3": iso,
                    "item": {
                        "normal_age_future_entrant": "retirement_age",
                        "normal_age_current": "retirement_age_now",
                    }[stat],
                    "value": round(float(g["value"].mean()), 1),  # men/women average
                    "year": int(g["year"].iloc[0]),
                    "source": str(g["source"].iloc[0]),
                }
            )
    return pd.DataFrame(rows)


def derive_university(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Latest household payment to tertiary institutions per FTE student (local money)."""
    u = long[long["measure"] == "tertiary_private_cost"].dropna(subset=["year", "value"])
    rows = []
    for iso, g in u.groupby("iso3"):
        for stat in ("households", "all_private"):
            h = g[g["statistic"] == stat]
            if not h.empty:
                h = h.sort_values("year").iloc[-1]
                rows.append(
                    {
                        "iso3": iso,
                        "per_student_year": float(h["value"]),
                        "year": int(h["year"]),
                        "basis": stat,
                        "source": str(h["source"]),
                        "estimated": stat != "households",
                    }
                )
                break
    return pd.DataFrame(rows)

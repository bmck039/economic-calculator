"""One row per country with the headline numbers."""

from __future__ import annotations

import pandas as pd

from econdata.config import COUNTRIES, LOCAL_CURRENCY
from econdata.derive.budget import HOUSING_TENURE_CATS


# ---------- Country snapshot -----------------------------------------------
def derive_snapshot(long, ctx, budget, life, epp) -> pd.DataFrame:
    rows = []
    for iso3, (_, _, name, region) in COUNTRIES.items():
        r = {
            "iso3": iso3,
            "country": name,
            "region": region,
            "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
        }

        def last(measure, stat, **eq):
            s = ctx.mi(measure, iso3)  # pre-indexed: no full-table scan per call
            s = s[s["statistic"] == stat]
            for k, v in eq.items():
                s = s[s[k].astype(str).str.contains(v, case=False, regex=True, na=False)]
            if s.empty:
                return None, None
            s = s.sort_values(["year", "period"]).iloc[-1]
            return s["value"], s["period"]

        if not epp.empty:
            e = epp[(epp["iso3"] == iso3) & (epp["statistic"] == "median")]
            for stage in ("early", "mid"):
                s = e[e["career_stage"] == stage].sort_values("year")
                if not s.empty:
                    s = s[s["year"] == s["year"].max()]
                    r[f"median_ft_earnings_{stage}_intl$"] = s["annual_intl_dollars"].median()
                    r[f"median_ft_earnings_{stage}_year"] = int(s["year"].iloc[0])
                    r[f"median_ft_earnings_{stage}_estimated"] = bool(s["estimated"].any())
        v, p = last(
            "household_income",
            "median",
            unit="^NAC",
            household_type="two adults younger|^two adults$",
        )
        if v is not None:
            r["silc_median_equivalised_2adults"], r["silc_year"] = v, p
            r["silc_couple_household_net_x1.5"] = v * 1.5
        v, p = last("unemployment_rate", "rate", age_band="15-24|15T24")
        r["youth_unemployment_rate"], r["youth_unemployment_year"] = v, p
        v, p = last("job_vacancy_rate", "rate")
        r["job_vacancy_rate"], r["job_vacancy_period"] = v, p
        v, p = last("saving_rate_net", "rate")
        r["household_net_saving_rate"], r["saving_year"] = v, p
        v, p = last("uncertainty", "wui_smoothed")
        r["wui_smoothed"] = v
        v, p = last("uncertainty", "political_stability")
        r["wgi_political_stability"] = v
        if not budget.empty:
            b = budget[budget["iso3"] == iso3]
            if not b.empty:
                r["couple_budget_monthly_local_excl_housing"] = b[
                    ~b["category"].isin(HOUSING_TENURE_CATS)
                ]["monthly_local"].sum()
                r["couple_budget_source"] = b["source"].iloc[0]
        if not life.empty:
            l = life[life["iso3"] == iso3]
            for item in (
                "home_price",
                "down_payment_amount",
                "mortgage_rate",
                "mortgage_payment_25y",
                "tenant_total_housing_cost",
                "median_gross_rent",
                "average_private_rent",
                "median_of_centres_avg_rent_2br",
                "share_cannot_cover_unexpected_expense",
                "unexpected_expense_size",
            ):
                s = l[l["item"] == item]
                if not s.empty:
                    r[item] = s["value"].iloc[0]
        ppp, _ = ctx.wb("ppp_private_consumption", iso3, ctx.latest_year)
        r["ppp_lcu_per_intl$"] = ppp
        rows.append(r)
    return pd.DataFrame(rows)

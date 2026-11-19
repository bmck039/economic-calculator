"""Sources: every number the plan used, with where it comes from."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from econsim import params as PRM


def render(page) -> None:
    ccare, cd, disp, iso, p1, p2, plan, res = (
        page.ccare,
        page.cd,
        page.disp,
        page.iso,
        page.p1,
        page.p2,
        page.plan,
        page.res,
    )
    rows = []

    def add(item, s, used=None):
        rows.append(
            {
                "input": item,
                "value used": used if used is not None else s.value,
                "unit": s.unit,
                "source": s.source,
                "year": str(s.year or ""),
                "estimated": s.estimated,
                "quality": s.quality,
                "note": s.note,
            }
        )

    for k, v in res.inputs.items():
        rows.append(
            {
                "input": k,
                "value used": v["value"],
                "unit": v.get("unit", ""),
                "source": v["source"],
                "year": str(v.get("year") or ""),
                "estimated": v.get("estimated", False),
                "quality": v.get("quality", ""),
                "note": v.get("note", ""),
            }
        )
    add("earnings profile", cd.earnings_src, f"{p1} / {p2}")
    add("tax curve", cd.tax_src)
    add("average wage (tax curve)", cd.avg_wage)
    add("child benefit per child /yr", cd.child_benefit_per_child)
    add("childcare per child /yr", cd.childcare_per_child, ccare)
    add("down payment (data default)", cd.down_payment_pct)
    add("mortgage rate (data default)", cd.mortgage_rate)
    add(
        "rent factor with 1 / 2 children",
        cd.rent_factor_src,
        " / ".join(f"{x:.2f}" for x in cd.rent_factor_per_child),
    )
    if cd.rent_elasticity is not None:
        add("new-lease rent vs income (elasticity)", cd.rent_elasticity)
        add(
            "renters' moves vs the US (and the tenure discount)",
            cd.rent_elasticity.with_value(cd.renter_moves),
        )
    add("unemployment rate", cd.unemployment_rate)
    add("exchange rate", cd.lcu_per_usd)
    add("PPP", cd.lcu_per_intl)
    for m, s in sorted(cd.nrr.items()):
        add(f"net replacement rate {m} months", s)
    for k, v in cd.budget.items():
        add(f"budget: {k} /mo", v)
    src_df = pd.DataFrame(rows)
    src_df["value used"] = src_df["value used"].map(
        lambda v: f"{v:,.2f}" if isinstance(v, float) else ("" if v is None else str(v))
    )
    st.dataframe(src_df, hide_index=True, width="stretch", height=600)
    st.caption(
        "Quality: A = official statistics, B = industry survey / model, C = rough or "
        "derived. ESTIMATE = not directly observed (nowcast, interpolation, proxy, "
        "assumption). Money values are in the source's price year and are rolled to "
        f"{plan.start_year} with actual CPI, then the inflation assumption. Curated "
        "rules and market levels (rents, home prices, lending rules, health, cars, "
        "leave, pensions, US states) live in econsim/params/*.csv with their sources."
    )
    hh_rows = [
        {
            "parameter": k,
            "value": v if isinstance(v, str) else f"{v:,.6g}",
            "source": PRM._hh_sources().get(k, ("", ""))[0],
            "quality": PRM._hh_sources().get(k, ("", ""))[1],
        }
        for k, v in (cd.hh or {}).items()
        if v == v
    ]
    if hh_rows:
        st.markdown("**Household rules for this country** (econsim/params/household.csv)")
        st.dataframe(pd.DataFrame(hh_rows), hide_index=True, width="stretch")
    st.download_button(
        "Download simulation (CSV)", disp.to_csv(index=False), f"life_plan_{iso}.csv", "text/csv"
    )

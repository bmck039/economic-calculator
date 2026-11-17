"""The monthly budget: what the household spends, by group, year by year."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui.charts import GROUP_ORDER, PALETTE, add_event_markers, base_layout, group_of


def render(page) -> None:
    cd, disp, res, money_note = page.cd, page.disp, page.res, page.money_note
    exp_cols = [
        c
        for c in disp.columns
        if c.startswith(("budget:", "oneoff:"))
        or c
        in (
            "rent",
            "mortgage_payment",
            "owner_costs",
            "children",
            "childcare",
            "emergencies",
            "cars",
            "public_transport",
            "health",
            "mortgage_insurance",
            "university",
            "long_term_care",
            "mortgage_interest_relief",
            "wealth_tax",
            "student_loan",
        )
    ]
    grp = pd.DataFrame({"age": disp["age1"]})
    for c in exp_cols:
        g = group_of(c)
        grp[g] = grp.get(g, 0) + disp[c] / 12
    fig = go.Figure()
    for i, g in enumerate(GROUP_ORDER):
        if g in grp and grp[g].abs().sum() > 0:
            fig.add_bar(
                x=grp["age"],
                y=grp[g],
                name=g,
                marker_color=PALETTE[i],
                marker_line=dict(width=0.5, color="white"),
                hovertemplate=f"{g}: %{{y:,.0f}}<extra></extra>",
            )
    fig.add_scatter(
        x=disp["age1"],
        y=disp["net_income"] / 12,
        name="Net income",
        mode="lines",
        line=dict(color="#0b0b0b", width=2.5),
        hovertemplate="Net income: %{y:,.0f}<extra></extra>",
    )
    fig.add_scatter(
        x=disp["age1"],
        y=disp["saving"] / 12,
        name="Saving (income − spending)",
        mode="lines",
        line=dict(color=PALETTE[2], width=2, dash="dash"),
        hovertemplate="Saving: %{y:,.0f}<extra></extra>",
    )
    fig.update_layout(barmode="relative")
    add_event_markers(fig, res.events)
    st.plotly_chart(
        base_layout(fig, "Where the money goes each month", f"per month ({money_note})"),
        width="stretch",
    )
    st.caption(
        "Bars = spending by category (one-off costs like the wedding and house deposit "
        "show as a spike in their year, spread over 12 months). Sources: "
        + "; ".join(sorted({v.source for v in cd.budget.values()}))[:400]
        + f". Rent: {cd.rent.label}."
    )
    with st.expander("Baseline couple budget today (per month, local, before scaling)"):
        bt = pd.DataFrame(
            [
                {
                    "category": k,
                    "monthly": v.value,
                    "source": v.source,
                    "price year": v.year,
                    "estimated": v.estimated,
                    "method": v.note,
                }
                for k, v in sorted(cd.budget.items(), key=lambda kv: -kv[1].value)
            ]
        )
        st.dataframe(
            bt,
            width="stretch",
            hide_index=True,
            column_config={"monthly": st.column_config.NumberColumn(format="%.0f")},
        )

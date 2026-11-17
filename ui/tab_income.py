"""Income: each partner's pay, taxes and benefits over life."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from ui.charts import PALETTE, add_event_markers, base_layout
from ui.common import flag


def render(page) -> None:
    cd, disp, ds, iso, money_note, res, rwg = (
        page.cd,
        page.disp,
        page.ds,
        page.iso,
        page.money_note,
        page.res,
        page.rwg,
    )
    fig = go.Figure()
    for col, nm, c, dash in (
        ("gross1", "P1 gross", PALETTE[0], "dot"),
        ("net1", "P1 net", PALETTE[0], "solid"),
        ("gross2", "P2 gross", PALETTE[1], "dot"),
        ("net2", "P2 net", PALETTE[1], "solid"),
    ):
        fig.add_scatter(
            x=disp["age1"],
            y=disp[col] / 12,
            name=nm,
            mode="lines",
            line=dict(color=c, width=2, dash=dash),
            hovertemplate=nm + ": %{y:,.0f}<extra></extra>",
        )
    if disp["pension_income"].abs().sum() > 0:
        fig.add_scatter(
            x=disp["age1"],
            y=disp["pension_income"] / 12,
            name="Pensions (net)",
            mode="lines",
            line=dict(color=PALETTE[2], width=2.5),
            hovertemplate="Pensions: %{y:,.0f}<extra></extra>",
        )
    fig.add_scatter(
        x=disp["age1"],
        y=disp["gross"] / 12,
        name="Household gross",
        mode="lines",
        line=dict(color="#0b0b0b", width=2, dash="dot"),
        hovertemplate="Household gross: %{y:,.0f}<extra></extra>",
    )
    fig.add_scatter(
        x=disp["age1"],
        y=disp["net_income"] / 12,
        name="Household net",
        mode="lines",
        line=dict(color="#0b0b0b", width=2.5),
        hovertemplate="Household net: %{y:,.0f}<extra></extra>",
    )
    add_event_markers(fig, res.events)
    st.plotly_chart(
        base_layout(
            fig,
            "Income over the career: gross (dotted) vs net (solid)",
            f"per month ({money_note})",
        ),
        width="stretch",
    )
    st.caption(
        f"Earnings: {cd.earnings_src.label}{flag(cd.earnings_src)}. Career path = "
        "cross-sectional age-earnings profile at the chosen percentile × career-growth "
        f"exponent, plus {rwg:.2f}%/yr economy-wide real wage growth. Household net "
        "includes child benefits and any US tax. Person net = gross − own local taxes."
    )
    if not ds.mob.empty:
        m = ds.mob[(ds.mob["iso3"] == iso) & (ds.mob["metric"] == "pseudo_cohort_real_growth_8y")]
        if not m.empty:
            r = m.sort_values("year").iloc[-1]
            st.info(
                f"Reality check — {r['detail']}: real pay grew ×{r['value']:.2f} "
                f"({r['note'].split(';')[-1].strip()}). Source: {r['source']}."
            )

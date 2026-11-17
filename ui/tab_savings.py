"""Savings: net worth and its parts, emergency fund, money that ran short."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from ui.charts import PALETTE, add_event_markers, base_layout


def render(page) -> None:
    disp, efund, money_note, res = page.disp, page.efund, page.money_note, page.res
    fig = go.Figure()
    fig.add_bar(
        x=disp["age1"],
        y=disp["cash"],
        name="Bank (emergency fund + deposits)",
        marker_color=PALETTE[3],
        hovertemplate="Bank: %{y:,.0f}<extra></extra>",
    )
    fig.add_bar(
        x=disp["age1"],
        y=disp["invest"],
        name="Investments",
        marker_color=PALETTE[0],
        hovertemplate="Investments: %{y:,.0f}<extra></extra>",
    )
    if disp["home_equity"].abs().sum() > 0:
        fig.add_bar(
            x=disp["age1"],
            y=disp["home_equity"],
            name="Home equity",
            marker_color=PALETTE[1],
            hovertemplate="Home equity: %{y:,.0f}<extra></extra>",
        )
    if disp.get("retirement_account") is not None and disp["retirement_account"].abs().sum() > 0:
        fig.add_bar(
            x=disp["age1"],
            y=disp["retirement_account"],
            name="Employer pension account",
            marker_color=PALETTE[2],
            hovertemplate="Employer pension: %{y:,.0f}<extra></extra>",
        )
    fig.update_layout(barmode="relative")
    fig.add_scatter(
        x=disp["age1"],
        y=disp["net_worth"],
        name="Net worth",
        mode="lines",
        line=dict(color="#0b0b0b", width=2.5),
        hovertemplate="Net worth: %{y:,.0f}<extra></extra>",
    )
    add_event_markers(fig, res.events)
    st.plotly_chart(
        base_layout(fig, "Cumulative savings and net worth", money_note), width="stretch"
    )
    st.caption(
        f"Emergency fund ({efund:g} months of spending) is kept in cash at "
        f"{res.inputs['cash_return']['value']:g}%; of the rest "
        f"{res.inputs['deposit_share']['value']:.0f}% stays in bank deposits at "
        f"{res.inputs['deposit_return']['value']:g}% and the remainder is invested "
        f"({res.inputs['equity_share']['value']:.0f}% in shares) at "
        f"{res.inputs['return_nominal']['value']:g}% nominal, rebalanced yearly. "
        "Home equity = home value − "
        "mortgage. Pension pots become spendable at retirement. "
        "Employer pension contributions (UK auto-enrolment is in the state pension "
        "figure instead; US 401(k), Canada) build a retirement account that can't be "
        "used for a deposit."
    )
    if (disp.get("spending_squeeze") is not None) and (disp["spending_squeeze"] > 0).any():
        sq = disp[disp["spending_squeeze"] > 0]
        fig2 = go.Figure()
        fig2.add_bar(
            x=sq["age1"],
            y=sq["spending_squeeze"] / 12,
            marker_color=PALETTE[7],
            name="Spending cut",
            hovertemplate="Cut: %{y:,.0f}/mo<extra></extra>",
        )
        lay = base_layout(
            fig2, "Money ran short: discretionary spending cut (per month)", money_note
        )
        lay.update_layout(height=280, showlegend=False)
        st.plotly_chart(lay, width="stretch")
        st.caption(
            "When income doesn't cover the survey-typical budget and savings are gone, "
            "the household cuts recreation, eating out, clothes and furnishings (and "
            "some of the children's spending), up to half of them, before borrowing - "
            "what people mean by 'making ends meet with difficulty'."
        )

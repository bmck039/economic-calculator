"""Taxes: local income tax, social security and US tax for citizens abroad."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui.charts import PALETTE, base_layout
from ui.common import flag


def render(page) -> None:
    CUR, cd, disp, ds, iso, money_note, res, us, us_cit = (
        page.CUR,
        page.cd,
        page.disp,
        page.ds,
        page.iso,
        page.money_note,
        page.res,
        page.us,
        page.us_cit,
    )
    fig = go.Figure()
    for i, (col, nm) in enumerate(
        (
            ("income_tax_local", "Local income tax"),
            ("ssc_local", "Employee social security"),
            ("us_tax", "Residual US federal tax"),
        )
    ):
        fig.add_bar(
            x=disp["age1"],
            y=disp[col] / 12,
            name=nm,
            marker_color=PALETTE[i],
            hovertemplate=nm + ": %{y:,.0f}<extra></extra>",
        )
    fig.add_scatter(
        x=disp["age1"],
        y=-disp["child_benefits"] / 12,
        name="Child benefits (−)",
        mode="lines",
        line=dict(color=PALETTE[5], width=2),
        hovertemplate="Child benefits: %{y:,.0f}<extra></extra>",
    )
    fig.update_layout(barmode="relative")
    st.plotly_chart(
        base_layout(fig, "Taxes per month", f"per month ({money_note})"), width="stretch"
    )
    st.caption(
        f"Local tax curve: {cd.tax_src.label}{flag(cd.tax_src)} — average income-tax "
        "and employee social-security rates at multiples of the national average "
        f"wage ({cd.avg_wage.value:,.0f} {cd.currency}/yr); income splitting for "
        "married couples in DE/LU/PT/PL, quotient familial in FR, youth exemptions "
        "(PL, HU, HR, PT, GR); compulsory occupational pension contributions (CH, NL, "
        "DK, IS, LI). US residents: federal + state + local tax and FICA computed "
        "directly. US citizens abroad: 2026 IRS brackets indexed by inflation; FEIE "
        f"${us.feie:,.0f}/person + housing exclusion vs FTC, cheaper route each year."
    )
    if us_cit and iso != "USA":
        meth = res.years["us_method"].replace("", pd.NA).dropna()
        st.write(
            f"**US route chosen:** {meth.mode().iloc[0] if not meth.empty else 'n/a'}. "
            f"Lifetime residual US tax: {disp['us_tax'].sum():,.0f} {CUR}."
        )
    if not ds.expat.empty:
        ex = ds.expat[(ds.expat["iso3"] == iso) & (ds.expat["measure"] == "AV_TW")]
        if not ex.empty:
            st.dataframe(
                ex[
                    ["household_type", "year", "local_value", "usa_value", "local_minus_usa"]
                ].rename(columns={"local_value": "tax wedge %", "usa_value": "USA %"}),
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "OECD Taxing Wages tax wedge (income tax + employee & employer SSC as % "
                "of labour cost) for model households vs the US."
            )

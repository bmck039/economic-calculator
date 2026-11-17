"""Compare: the same plan in other countries and US states."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from econsim.engine import simulate, to_display
from ui.charts import PALETTE, base_layout


def render(page) -> None:
    PLACES, build_plan, cd, cmp_names, country_with_overrides, cur, ds, here, real, us = (
        page.PLACES,
        page.build_plan,
        page.cd,
        page.cmp_names,
        page.country_with_overrides,
        page.cur,
        page.ds,
        page.here,
        page.real,
        page.us,
    )
    sel = [(cd.name, here)] + [(n, PLACES[n]) for n in cmp_names]
    rows, curves = [], []
    for n, (c_iso, c_st) in sel:
        main_ = (c_iso, c_st) == here
        c = ds.country(c_iso, c_st)
        p = build_plan(c_iso, main_, c_st)
        r = simulate(p, country_with_overrides(c, main_), us)
        d = to_display(r, c, real, "intl$" if cur == "local" else cur)
        d["country"] = n
        d.attrs["buys"] = [e["age"] for e in r.events if e["event"] == "House purchase"]
        curves.append(d)
        f = d.iloc[0]
        rows.append(
            {
                "country": n,
                "net income /mo": f["net_income"] / 12,
                "spending /mo": f["expenses_recurring"] / 12,
                "saving /mo": f["saving"] / 12,
                "saving rate": f["saving_rate"],
                "avg tax rate": f["local_tax_rate"],
                "retires at": r.summary.get("retirement_age"),
                "net worth at retirement": float(
                    d.loc[d["age1"] == r.summary.get("retirement_age"), "net_worth"].iloc[0]
                )
                if (d["age1"] == r.summary.get("retirement_age")).any()
                else float("nan"),
                f"net worth at end ({int(d['age1'].iloc[-1])})": d["net_worth"].iloc[-1],
                "US residual (lifetime)": d["us_tax"].sum(),
                # text, so a column of ages and "—" stays one type (Arrow)
                "house bought at": next(
                    (str(e["age"]) for e in r.events if e["event"] == "House purchase"), "—"
                ),
                "warnings": len(r.warnings),
            }
        )
    ccur = "intl $ (PPP)" if cur in ("local", "intl$") else "USD"
    st.caption(
        f"Same life plan in each country, using each country's own data defaults for "
        f"wages, prices, taxes and housing (your sidebar overrides apply to {cd.name} "
        f"only). Shown in {ccur}{', real' if real else ', nominal'} so countries are "
        "comparable."
    )

    def cmp_chart(col, title, ytitle, scale=1.0, mask=None, houses=True):
        """One line per place; 🏠 marks the year each place's household buys its home."""
        fig = go.Figure()
        for i, d in enumerate(curves):
            nm = d["country"].iloc[0]
            y = d[col] * scale
            if mask is not None:
                y = y.where(mask(d))
            fig.add_scatter(
                x=d["age1"],
                y=y,
                name=nm,
                mode="lines",
                legendgroup=nm,
                line=dict(color=PALETTE[i % len(PALETTE)], width=2.5),
                hovertemplate=nm + ": %{y:,.0f}<extra></extra>",
            )
            at = d[d["age1"].isin(d.attrs.get("buys", []))] if houses else d.iloc[:0]
            if len(at):
                fig.add_scatter(
                    x=at["age1"],
                    y=at[col] * scale,
                    mode="markers+text",
                    text=["🏠"] * len(at),
                    textposition="top center",
                    textfont=dict(size=16),
                    legendgroup=nm,
                    showlegend=False,
                    marker=dict(
                        color=PALETTE[i % len(PALETTE)],
                        size=10,
                        symbol="diamond",
                        line=dict(color="white", width=1.5),
                    ),
                    hovertemplate=nm + " buys a home at %{x}<extra></extra>",
                )
        return base_layout(fig, title, ytitle)

    st.plotly_chart(cmp_chart("net_worth", "Net worth over life", ccur), width="stretch")
    st.caption("🏠 = the year each household buys its home (none if it keeps renting).")
    st.plotly_chart(
        cmp_chart("saving", "Monthly saving", f"per month ({ccur})", 1 / 12), width="stretch"
    )
    c1, c2 = st.columns(2)
    c1.plotly_chart(
        cmp_chart(
            "gross",
            "Wages over time (household, before tax)",
            f"per month ({ccur}{', real' if real else ''})",
            1 / 12,
            mask=lambda d: d["gross"] > 0,
        ),
        width="stretch",
    )
    c2.plotly_chart(
        cmp_chart(
            "price_factor",
            "Consumer prices over time",
            f"index, {int(curves[0]['year'].iloc[0])} = 100",
            100,
            houses=False,
        ),
        width="stretch",
    )
    st.caption(
        "Wages: both partners' pay before tax, in years someone works"
        f"{' - after inflation, so they show buying power' if real else ''}. "
        "Prices: each place's consumer price index as the model projects it (your "
        "inflation setting applies to the main place), always nominal."
    )
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            k: st.column_config.NumberColumn(format="%.0f")
            for k in (
                "net income /mo",
                "spending /mo",
                "saving /mo",
                "net worth at retirement",
                "US residual (lifetime)",
            )
        }
        | {
            k: st.column_config.NumberColumn(format="percent")
            for k in ("saving rate", "avg tax rate")
        },
    )

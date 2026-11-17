"""The page's title, warnings and headline numbers."""

from __future__ import annotations

from types import SimpleNamespace

import streamlit as st


def render(v: SimpleNamespace) -> None:
    res, disp, money_note = v.res, v.disp, v.money_note
    st.title(f"🌍 A life in {v.cd.name}")
    st.caption(
        f"Couple aged {v.a1} & {v.a2}, {v.pct_lbl[v.p1]} / {v.pct_lbl[v.p2]} earners · "
        f"{money_note}"
        " · hover any number's ⓘ for source · ⚠️ = estimate"
    )
    for n in v.cd.notes:
        st.warning(n)
    for w in res.warnings:
        st.warning(w)

    d0 = disp.iloc[0]
    last = disp.iloc[-1]
    kc = st.columns(5)
    kc[0].metric(
        "Household net income now",
        f"{d0['net_income'] / 12:,.0f}/mo",
        help="After local income tax, social security and any US tax.",
    )
    kc[1].metric("Spending now", f"{d0['expenses_recurring'] / 12:,.0f}/mo")
    kc[2].metric(
        "Saving now", f"{d0['saving'] / 12:,.0f}/mo", f"{d0['saving_rate'] * 100:.0f}% of net"
    )
    if res.summary.get("own_inflation") is not None:
        hc = res.inputs.get("household_costs_5y")
        st.caption(
            f"Your own inflation over the next {res.summary['own_inflation_years']} years: "
            f"{res.summary['own_inflation']:.2f}%/yr (your rent or mortgage and each budget line "
            f"at its own price trend) vs {res.summary['cpi_inflation']:.2f}%/yr consumer prices."
            + (
                f" Over the last 5 years what US households pay for housing and everything else "
                f"rose {hc['value']:.1f}%/yr - see the inputs table."
                if hc
                else ""
            )
        )
    conv = {"local": 1.0, "USD": v.cd.lcu_per_usd.value, "intl$": v.cd.lcu_per_intl.value}[v.cur]
    S_ = res.summary
    ra_ = S_.get("retirement_age", v.retire)
    r_row = disp[disp["age1"] == ra_]
    nw_ret = r_row["net_worth"].iloc[0] if not r_row.empty else last["net_worth"]
    pen_m = (S_.get("pension_income_real", 0.0) / conv / 12) if v.real else None
    kc[3].metric(
        f"Net worth at retirement ({ra_})",
        f"{nw_ret:,.0f}",
        (
            f"pension {pen_m:,.0f}/mo"
            + (
                f" ({S_['pension_vs_last_net']:.0%} of last pay)"
                if S_.get("pension_vs_last_net")
                else ""
            )
        )
        if pen_m
        else None,
        delta_color="off",
        delta_arrow="off",
        help="Liquid savings + home equity + pension pots. Pension: both partners' "
        "career-average pay relative to the average wage x the OECD net "
        "replacement rate at that pay level (mandatory schemes, full career, "
        f"legislated rules; {S_.get('pension_nrr', 0):.0f}% at the average wage), "
        "today's money."
        + (
            f" Savings run out at {S_['savings_run_out_age']}."
            if S_.get("savings_run_out_age")
            else f" At {int(last['age1'])}: {last['net_worth']:,.0f}."
        ),
    )
    kc[4].metric(
        "Avg tax rate now",
        f"{d0['local_tax_rate'] * 100:.0f}%",
        f"US residual {d0['us_tax'] / 12:,.0f}/mo" if v.us_cit and v.iso != "USA" else None,
        delta_color="off",
        delta_arrow="off",
    )

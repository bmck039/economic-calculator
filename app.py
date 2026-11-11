"""Economic Calculator and Planner - Streamlit UI.

    .venv/bin/streamlit run app.py

All modelling lives in econsim/ (engine, tax, data); this file only collects inputs
and draws charts. Every default shows its source, year and estimate flag; the
'Sources' tab lists every number used.
"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from econsim import params as PRM
from econsim.data import DataStore
from econsim.engine import simulate, to_display
from econsim.model import Earner, JobLoss, Plan

st.set_page_config(page_title="Life Abroad Planner", layout="wide", page_icon="🌍")

# Validated categorical palette (fixed order, never cycled) - dataviz reference.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6",
           "#e34948"]
INK, MUTED = "#52514e", "#8a8984"

GROUPS = {  # expense column -> display group (8 groups = 8 palette slots)
    "rent": "Housing", "mortgage_payment": "Housing", "owner_costs": "Housing",
    "budget:groceries": "Food", "budget:restaurants_hotels": "Food",
    "budget:energy_home": "Utilities & home", "budget:water_refuse": "Utilities & home",
    "budget:home_maintenance": "Utilities & home", "budget:furnishings_household": "Utilities & home",
    "budget:phone_internet": "Utilities & home", "budget:household_operations": "Utilities & home",
    "budget:utilities_phone": "Utilities & home", "budget:council_tax": "Utilities & home",
    "budget:vehicle_purchase": "Transport", "budget:fuel_vehicle": "Transport",
    "budget:vehicle_maintenance": "Transport", "budget:vehicle_other": "Transport",
    "budget:transport_services": "Transport", "budget:insurance_vehicle": "Transport",
    "budget:vehicle_all": "Transport", "budget:transport_all": "Transport",
    "budget:vehicle_running": "Transport",
    "budget:health_out_of_pocket": "Health & insurance",
    "budget:insurance_health": "Health & insurance",
    "budget:health_insurance_mandatory": "Health & insurance",
    "budget:insurance_home": "Health & insurance", "budget:insurance_other": "Health & insurance",
    "children": "Children", "childcare": "Children",
    "cars": "Transport", "public_transport": "Transport",
    "health": "Health & insurance", "mortgage_insurance": "Housing",
    "mortgage_interest_relief": "Housing", "wealth_tax": "Leisure & other",
    "student_loan": "Emergencies & one-offs",
    "emergencies": "Emergencies & one-offs", "oneoff:wedding": "Emergencies & one-offs",
    "oneoff:house_purchase": "Emergencies & one-offs",
}
GROUP_ORDER = ["Housing", "Food", "Transport", "Utilities & home", "Health & insurance",
               "Children", "Leisure & other", "Emergencies & one-offs"]


def group_of(col: str) -> str:
    return GROUPS.get(col, "Leisure & other")


@st.cache_resource
def store() -> DataStore:
    return DataStore()


ds = store()
countries = ds.countries()
if not countries:
    st.error("No data found in econ_data/. Run `python build_econ_dataset.py` first.")
    st.stop()
names = dict(countries)
iso_by_name = {v: k for k, v in countries}


def src(s, fmt="{:,.0f}") -> str:
    """Help text for a sourced default."""
    try:
        v = fmt.format(s.value)
    except (TypeError, ValueError):
        v = str(s.value)
    est = " ⚠️ ESTIMATE" if s.estimated else ""
    return f"Default {v} {s.unit} — {s.source}" + (f" ({s.year})" if s.year else "") + \
        (f", quality {s.quality}" if s.quality else "") + est + (f". {s.note}" if s.note else "")


def flag(s) -> str:
    return " ⚠️" if s.estimated else ""


# ======================================================================= sidebar
sb = st.sidebar
sb.title("Life plan")
default_c = "Netherlands" if "Netherlands" in iso_by_name else countries[0][1]
cname = sb.selectbox("Country", [n for _, n in countries],
                     index=[n for _, n in countries].index(default_c))
iso = iso_by_name[cname]
state = None
if iso == "USA":
    st_names = PRM.us_state_names()
    st_codes = [None] + sorted(st_names, key=st_names.get)
    state = sb.selectbox("US state", st_codes, format_func=lambda c: "National average"
                         if c is None else st_names[c],
                         help="State earnings, rents, home prices, property tax, state income "
                              "tax, price level, health premiums, car insurance and paid "
                              "family leave.")
cd = ds.country(iso, state)
cmp_names = sb.multiselect("Compare with (up to 3 total)", [n for _, n in countries if n != cname],
                           default=[n for n in ("United States", "Germany") if n != cname
                                    and n in iso_by_name][:2], max_selections=2)
K = lambda name: f"{name}_{iso}_{state}"  # noqa: E731  (per-country keys -> defaults reset)

with sb.expander("👫 Partners & career", expanded=True):
    c1, c2 = st.columns(2)
    a1 = c1.number_input("Partner 1 age", 18, 70, 23)
    a2 = c2.number_input("Partner 2 age", 18, 70, 24)
    pct_opts = [p for p in ("p10", "p25", "median", "p75", "p90") if p in cd.earnings] or ["median"]
    pct_lbl = {"p10": "10th pct", "p25": "25th pct", "median": "Median", "p75": "75th pct",
               "p90": "90th pct"}
    p1 = c1.selectbox("P1 income", pct_opts, index=pct_opts.index("median")
                      if "median" in pct_opts else 0, format_func=pct_lbl.get)
    p2 = c2.selectbox("P2 income", pct_opts, index=pct_opts.index("median")
                      if "median" in pct_opts else 0, format_func=pct_lbl.get)
    g1 = c1.slider("P1 career growth", 0.0, 2.0, 1.0, 0.1,
                   help="1.0 = follow the country's age-earnings profile (cross-section of "
                        "today's workers by age). 0 = flat career; 2 = twice as steep.")
    g2 = c2.slider("P2 career growth", 0.0, 2.0, 1.0, 0.1)
    retire = st.slider("Retirement age (earnings stop)", 50, 75, 67)
    horizon = st.slider("Simulate until P1 age", int(a1) + 5, 95, max(retire - 1, int(a1) + 5),
                        help="Default: the last working year. Pension rights are shown "
                             "separately (OECD net replacement rate x final net pay).")
    savings0 = st.number_input(f"Current savings ({cd.currency})", 0.0, 1e9, 0.0, 1000.0,
                               key=K("sav0"))
    st.caption(f"Earnings: {cd.earnings_src.label}{flag(cd.earnings_src)}. "
               f"{cd.earnings_src.note[:160]}")

with sb.expander("💍 Marriage & kids"):
    married = st.checkbox("Get married", True)
    m_age = st.number_input("Marriage at P1 age", 18, 80, max(int(a1), 28), disabled=not married)
    wedding = st.number_input(f"Wedding cost ({cd.currency})", 0.0, 1e7,
                              float(round(cd.wedding_cost.value, -2)), 500.0, key=K("wed"),
                              help=src(cd.wedding_cost), disabled=not married)
    kid_opts = ["none"] + list(range(int(a1), 56))
    k1 = st.selectbox("Child 1 born at P1 age", kid_opts,
                      index=kid_opts.index(max(int(a1), 30)) if max(int(a1), 30) in kid_opts else 0)
    k2 = st.selectbox("Child 2 born at P1 age", kid_opts,
                      index=kid_opts.index(max(int(a1), 33)) if max(int(a1), 33) in kid_opts else 0)
    hh = cd.hh or {}
    cc_from = st.slider("Paid childcare from child age", 0, 3, 1,
                        help="Usually when paid parental leave ends.")
    st.caption(f"Full-time paid care until free pre-school at age "
               f"{hh.get('free_childcare_from', 5):g}, then after-school care (20%) to 10; "
               f"school from {hh.get('school_age', 6):g}. Child living costs: OECD-modified "
               "equivalence scale (+20% of the couple budget per child, +33% from 14).")
    cc_to = None
    leave_who = st.radio("Main parental leave taken by", ["Partner 1", "Partner 2"], index=1,
                         horizontal=True)
    lv1 = hh.get('leave1_months', 0) + hh.get('leave2_months', 0)
    leave_scale = st.slider("Parental leave vs country typical", 0.0, 2.0, 1.0, 0.1,
                            help=f"Typical: {lv1:.1f} months for the main parent, "
                                 f"{hh.get('partner_leave_months', 0):.1f} for the other "
                                 "(OECD Family Database; national rules).")
    ccare = st.number_input(f"Childcare per child in care ({cd.currency}/year)", 0.0, 1e7,
                            float(round(cd.childcare_per_child.value)), 100.0, key=K("cc"),
                            help=src(cd.childcare_per_child))

with sb.expander("🏡 Housing"):
    mode = st.radio("Tenure", ["Buy a home", "Rent forever"], horizontal=True)
    rent = st.number_input(f"Rent before buying ({cd.currency}/month, excl. utilities)", 0.0,
                           1e6, float(round(cd.rent.value)), 25.0, key=K("rent"),
                           help=src(cd.rent))
    buy_age = st.number_input("Buy at P1 age", 18, 90, max(int(a1), 30),
                              disabled=mode != "Buy a home")
    price = st.number_input(f"Home price today ({cd.currency})", 0.0, 1e9,
                            float(round(cd.home_price.value, -3)), 5000.0, key=K("hp"),
                            help=src(cd.home_price))
    c1, c2 = st.columns(2)
    dp = c1.number_input("Down payment %", 0.0, 100.0, float(round(cd.down_payment_pct.value, 1)),
                         0.5, key=K("dp"), help=src(cd.down_payment_pct, "{:.1f}"))
    tt = c2.number_input("Transfer tax %", 0.0, 20.0, float(cd.transfer_tax_pct.value), 0.25,
                         key=K("tt"), help=src(cd.transfer_tax_pct, "{:.2f}"))
    mr = c1.number_input("Mortgage rate %", 0.0, 25.0, float(round(cd.mortgage_rate.value, 2)),
                         0.05, key=K("mr"), help=src(cd.mortgage_rate, "{:.2f}"))
    L = cd.lend or {}
    term_d = int(L.get("term") or 0) or 25
    term = c2.number_input("Term (years)", 5, 50, term_d, key=K("term"),
                           help="Country typical maximum. Switzerland: the first mortgage is "
                                "interest-only; the part above 2/3 of the price is repaid in 15y.")
    owner_d = round(0.7 + (L.get("property_tax_pct") or 0) + (L.get("home_ins_pct") or 0), 2) \
        if L else 1.0
    owner = c1.number_input("Upkeep + property tax + insurance %/yr", 0.0, 5.0, owner_d, 0.05,
                            key=K("own"), help=f"Default: 0.7% upkeep + "
                            f"{L.get('property_tax_pct', 0):g}% property tax + "
                            f"{L.get('home_ins_pct', 0):g}% home insurance.")
    appr_d = float(round(cd.real_wage_growth_hist.value, 2))
    appr = c2.number_input("Real house-price & rent growth %/yr", -5.0, 5.0, appr_d, 0.1,
                           key=K("appr"),
                           help="Default = real wage growth (price-to-income stays constant).")
    rules = st.checkbox("Use the country's lending rules", True,
                        help=str(L.get("source", "")) or "No curated rules: 40% of net income.")
    dsti = st.slider("Max mortgage payment, % of net income", 20, 60, 40, disabled=rules)
    if L:
        st.caption(f"Lending: min. down {L['down_pct']:g}%"
                   + (f" ({L['down_pct_young']:g}% to age {L['young_max_age']:g})"
                      if L.get('young_max_age') and L['down_pct_young'] != L['down_pct'] else "")
                   + (f", payment ≤ {L['dsti_max']:g}% of {L['dsti_basis']} income"
                      if L.get('dsti_max') else "")
                   + (f", loan ≤ {L['lti_max']:g}× gross" if L.get('lti_max') else "")
                   + f"; rate fixed {L['fixation_years']:g}y, then {L['rate_longrun']:g}% "
                   "(2005-2025 average).")
    cars_d = int(hh.get("cars", 1) or 1) if hh else 1
    cars = st.number_input("Cars owned", 0, 3, cars_d, key=K("cars"),
                           help="Depreciation, insurance (young-driver surcharge), road tax, "
                                "fuel, maintenance, tolls; adults without a car get a "
                                "public-transport pass.")
    budget_scale = st.slider("Lifestyle vs survey average couple", 0.5, 2.0, 1.0, 0.05,
                             help="Scales the survey budget (food, transport, leisure, ...).")

with sb.expander("🚨 Emergencies & job loss"):
    emerg = st.number_input(f"Extra unexpected costs ({cd.currency}/year)", 0.0, 1e7,
                            float(round(cd.emergency_expense_size.value)), 50.0, key=K("em"),
                            help=src(cd.emergency_expense_size) +
                            " Stress test: e.g. one such shock per year.")
    urisk = st.checkbox("Deduct the average cost of unemployment", True,
                        help="Each year: unemployment rate for the age (youth rate under 25) x "
                             "net pay x (1 - benefit replacement).")
    efund = st.slider("Emergency fund target (months of spending)", 0.0, 24.0, 6.0, 0.5)
    jl_on = st.checkbox("Simulate a job loss")
    c1, c2, c3 = st.columns(3)
    jl_who = c1.selectbox("Who", [1, 2], disabled=not jl_on)
    jl_age = c2.number_input("At P1 age", 18, 90, int(a1) + 10, disabled=not jl_on)
    jl_m = c3.number_input("Months", 1, 36, 6, disabled=not jl_on)
    if cd.nrr:
        st.caption("Net replacement rates (OECD TaxBEN, couple, latest): " + ", ".join(
            f"{m} mo {s.value:.0f}%" for m, s in sorted(cd.nrr.items())))

with sb.expander("🧾 Other costs & options"):
    sloan = st.number_input(f"Student loans at the start ({cd.currency}, household)", 0.0, 1e7,
                            0.0, 1000.0, key=K("sl"),
                            help="UK: 9% of pay above GBP 25,000 (Plan 5), written off after 40"
                                 " years. Elsewhere: 10-year repayment at 6.4%.")
    church = st.checkbox("Church members (Germany: +8-9% of income tax)", False,
                         disabled=iso != "DEU")
    priv_h = st.checkbox("Private health insurance (Ireland)", False, disabled=iso != "IRL")
    elast = st.slider("How much spending follows income", 0.0, 1.0, 0.6, 0.05,
                      help="Living costs = survey couple's budget x (your income after housing"
                           " / theirs)^this. 0 = always the survey average; 1 = proportional.")

with sb.expander("📈 Returns, growth & display"):
    c1, c2 = st.columns(2)
    ret = c1.number_input("Investment return %/yr (nominal, after fees, before tax)", -5.0, 15.0,
                          5.0, 0.25, help=f"Tax drag for {cd.name}: "
                          f"{(cd.hh or {}).get('invest_tax_drag', 0):g} pp/yr (main tax-"
                          "advantaged route).")
    cash_r = c2.number_input("Cash (emergency fund) %/yr", 0.0, 10.0, 2.0, 0.25)
    infl = c1.number_input("Inflation %/yr", -2.0, 15.0, 2.0, 0.1,
                           help=src(cd.inflation_hist, "{:.2f}") + " Default 2% (target).")
    rwg_d = float(cd.real_wage_growth_hist.value) \
        if cd.real_wage_growth_hist.value == cd.real_wage_growth_hist.value else 0.5
    rwg = c2.number_input("Real wage growth %/yr (economy-wide)", -3.0, 5.0, round(rwg_d, 2),
                          0.05, key=K("rwg"), help=src(cd.real_wage_growth_hist, "{:.2f}"))
    real = st.toggle("Show in today's money (real)", True)
    cur = st.radio("Currency", ["local", "USD", "intl$"], horizontal=True,
                   format_func={"local": f"Local ({cd.currency})", "USD": "USD",
                                "intl$": "Intl $ (PPP)"}.get)

with sb.expander("🇺🇸 US citizens abroad"):
    us_cit = st.checkbox("We are US citizens (citizenship-based taxation)", True)
    us_emp = st.checkbox("Employed by a US employer / self-employed", False,
                         help="Adds US FICA where there is no totalization agreement.")
    st.caption(("✅ US totalization agreement in force — pay social security in one country "
                "only." if cd.totalization else "⚠️ No US totalization agreement.")
               + " FEIE vs FTC is chosen each year (whichever is cheaper); electing and "
               "revoking FEIE has a 5-year lock-in in reality. Also: FBAR above $10k, "
               "FATCA Form 8938, and PFIC rules for non-US funds (not modelled).")


def build_plan(iso3: str, override: bool) -> Plan:
    """Plan for iso3. `override` = use the sidebar's money inputs (primary country);
    comparison countries use their own data defaults for local prices."""
    jl = JobLoss(jl_who - 1, int(jl_age), int(jl_m)) if jl_on else None
    p = Plan(
        country=iso3, earners=(Earner(int(a1), p1, g1), Earner(int(a2), p2, g2)),
        horizon_age=int(horizon), retirement_age=int(retire),
        marriage_age=int(m_age) if married else None,
        child_ages=(None if k1 == "none" else int(k1), None if k2 == "none" else int(k2)),
        childcare_from=cc_from, childcare_to=cc_to,
        buy_house_age=int(buy_age) if mode == "Buy a home" else None,
        max_payment_to_income=None if rules else float(dsti),
        cars=int(cars) if override else None, unemployment_risk=urisk,
        primary_parent=0 if leave_who == "Partner 1" else 1, leave_scale=leave_scale,
        private_health=priv_h, church_tax=church, student_loan=float(sloan) if override else 0.0,
        budget_elasticity=elast, state=state if iso3 == "USA" else None,
        budget_scale=budget_scale, emergency_fund_months=efund, job_loss=jl,
        return_nominal=ret, cash_return=cash_r,
        inflation=None if abs(infl - 2.0) < 1e-9 else infl, us_citizens=us_cit,
        us_employer=us_emp, starting_savings=float(savings0) if override else
        float(savings0) / cd.lcu_per_intl.value * ds.country(iso3).lcu_per_intl.value)
    if override:
        # pass a value only when the user changed it, so provenance stays on defaults
        def ov(val, default, nd=0):
            return None if abs(val - round(default, nd)) < 1e-9 else val
        p = replace(p, wedding_cost=ov(wedding, cd.wedding_cost.value, -2),
                    home_price=ov(price, cd.home_price.value, -3),
                    down_payment_pct=ov(dp, cd.down_payment_pct.value, 1),
                    transfer_tax_pct=ov(tt, cd.transfer_tax_pct.value, 9),
                    mortgage_rate=ov(mr, cd.mortgage_rate.value, 2),
                    rent=ov(rent, cd.rent.value), emergency_annual_cost=ov(
                        emerg, cd.emergency_expense_size.value),
                    real_wage_growth=ov(rwg, rwg_d, 2),
                    owner_cost_pct=ov(owner, owner_d, 2),
                    home_real_appreciation=ov(appr, appr_d, 2),
                    mortgage_years=None if int(term) == term_d else int(term))
    return p


def country_with_overrides(c):
    return replace(c, childcare_per_child=c.childcare_per_child.with_value(ccare)) \
        if c.iso3 == iso else c


us = ds.us_params()
plan = build_plan(iso, True)
cd_run = country_with_overrides(cd)
res = simulate(plan, cd_run, us)
disp = to_display(res, cd_run, real, cur)
CUR = {"local": cd.currency, "USD": "USD", "intl$": "intl $"}[cur]
money_note = ("today's money" if real else "nominal") + f", {CUR}"

# ======================================================================= header
st.title(f"🌍 A life in {cd.name}")
st.caption(f"Couple aged {a1} & {a2}, {pct_lbl[p1]} / {pct_lbl[p2]} earners · {money_note}"
           " · hover any number's ⓘ for source · ⚠️ = estimate")
for n in cd.notes:
    st.warning(n)
for w in res.warnings:
    st.warning(w)

d0 = disp.iloc[0]
last = disp.iloc[-1]
kc = st.columns(5)
kc[0].metric("Household net income now", f"{d0['net_income'] / 12:,.0f}/mo",
             help="After local income tax, social security and any US tax.")
kc[1].metric("Spending now", f"{d0['expenses_recurring'] / 12:,.0f}/mo")
kc[2].metric("Saving now", f"{d0['saving'] / 12:,.0f}/mo",
             f"{d0['saving_rate'] * 100:.0f}% of net")
conv = {"local": 1.0, "USD": cd.lcu_per_usd.value, "intl$": cd.lcu_per_intl.value}[cur]
pw = res.summary.get("pension_wealth_real", 0.0) / conv
kc[3].metric(f"Net worth at {int(last['age1'])}", f"{last['net_worth']:,.0f}",
             f"+ pension rights ≈ {pw:,.0f}", delta_color="off", delta_arrow="off",
             help="Liquid savings + home equity. Pension rights: OECD net replacement rate "
                  f"({res.summary.get('pension_nrr', 0):.0f}%, mandatory schemes) x final net "
                  "pay x 15 (value of a pension from 67, today's money).")
kc[4].metric("Avg tax rate now", f"{d0['local_tax_rate'] * 100:.0f}%",
             f"US residual {d0['us_tax'] / 12:,.0f}/mo" if us_cit and iso != "USA" else None,
             delta_color="off", delta_arrow="off")


def base_layout(fig, title, ytitle):
    fig.update_layout(title=dict(text=title, x=0, font=dict(size=16)), height=430,
                      margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=-0.18, x=0),
                      yaxis=dict(title=ytitle, gridcolor="rgba(128,128,128,0.15)",
                                 zerolinecolor="rgba(128,128,128,0.4)"),
                      xaxis=dict(title="Partner 1 age", gridcolor="rgba(0,0,0,0)"),
                      bargap=0.15, plot_bgcolor="rgba(0,0,0,0)")
    return fig


def add_event_markers(fig, events, y=None):
    for e in events:
        fig.add_vline(x=e["age"], line=dict(color=MUTED, width=1, dash="dot"))
        fig.add_annotation(x=e["age"], y=1.02, yref="paper", text=e["event"], showarrow=False,
                           font=dict(size=11, color=INK), textangle=-30, xanchor="left")


tabs = st.tabs(["💶 Monthly budget", "📈 Savings", "💼 Income", "🧾 Taxes", "🌐 Compare",
                "🧭 Context", "📚 Sources"])

# ---------------------------------------------------------------- monthly budget
with tabs[0]:
    exp_cols = [c for c in disp.columns if c.startswith(("budget:", "oneoff:")) or c in
                ("rent", "mortgage_payment", "owner_costs", "children", "childcare",
                 "emergencies", "cars", "public_transport", "health", "mortgage_insurance",
                 "mortgage_interest_relief", "wealth_tax", "student_loan")]
    grp = pd.DataFrame({"age": disp["age1"]})
    for c in exp_cols:
        g = group_of(c)
        grp[g] = grp.get(g, 0) + disp[c] / 12
    fig = go.Figure()
    for i, g in enumerate(GROUP_ORDER):
        if g in grp and grp[g].abs().sum() > 0:
            fig.add_bar(x=grp["age"], y=grp[g], name=g, marker_color=PALETTE[i],
                        marker_line=dict(width=0.5, color="white"),
                        hovertemplate=f"{g}: %{{y:,.0f}}<extra></extra>")
    fig.add_scatter(x=disp["age1"], y=disp["net_income"] / 12, name="Net income",
                    mode="lines", line=dict(color="#0b0b0b", width=2.5),
                    hovertemplate="Net income: %{y:,.0f}<extra></extra>")
    fig.add_scatter(x=disp["age1"], y=disp["saving"] / 12, name="Saving (income − spending)",
                    mode="lines", line=dict(color=PALETTE[2], width=2, dash="dash"),
                    hovertemplate="Saving: %{y:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    add_event_markers(fig, res.events)
    st.plotly_chart(base_layout(fig, "Where the money goes each month",
                                f"per month ({money_note})"), width="stretch")
    st.caption("Bars = spending by category (one-off costs like the wedding and house deposit "
               "show as a spike in their year, spread over 12 months). Sources: "
               + "; ".join(sorted({v.source for v in cd.budget.values()}))[:400]
               + f". Rent: {cd.rent.label}.")
    with st.expander("Baseline couple budget today (per month, local, before scaling)"):
        bt = pd.DataFrame([{"category": k, "monthly": v.value, "source": v.source,
                            "price year": v.year, "estimated": v.estimated, "method": v.note}
                           for k, v in sorted(cd.budget.items(), key=lambda kv: -kv[1].value)])
        st.dataframe(bt, width="stretch", hide_index=True,
                     column_config={"monthly": st.column_config.NumberColumn(format="%.0f")})

# ---------------------------------------------------------------- savings
with tabs[1]:
    fig = go.Figure()
    fig.add_bar(x=disp["age1"], y=disp["liquid"], name="Liquid savings",
                marker_color=PALETTE[0], hovertemplate="Liquid: %{y:,.0f}<extra></extra>")
    if disp["home_equity"].abs().sum() > 0:
        fig.add_bar(x=disp["age1"], y=disp["home_equity"], name="Home equity",
                    marker_color=PALETTE[1],
                    hovertemplate="Home equity: %{y:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    fig.add_scatter(x=disp["age1"], y=disp["net_worth"], name="Net worth", mode="lines",
                    line=dict(color="#0b0b0b", width=2.5),
                    hovertemplate="Net worth: %{y:,.0f}<extra></extra>")
    add_event_markers(fig, res.events)
    st.plotly_chart(base_layout(fig, "Cumulative savings and net worth", money_note),
                    width="stretch")
    st.caption(f"Emergency fund ({efund:g} months of spending) is kept in cash at {cash_r}%; "
               f"the rest is invested at {ret}% nominal. Home equity = home value − mortgage.")

# ---------------------------------------------------------------- income
with tabs[2]:
    fig = go.Figure()
    for col, nm, c, dash in (("gross1", "P1 gross", PALETTE[0], "dot"),
                             ("net1", "P1 net", PALETTE[0], "solid"),
                             ("gross2", "P2 gross", PALETTE[1], "dot"),
                             ("net2", "P2 net", PALETTE[1], "solid")):
        fig.add_scatter(x=disp["age1"], y=disp[col] / 12, name=nm, mode="lines",
                        line=dict(color=c, width=2, dash=dash),
                        hovertemplate=nm + ": %{y:,.0f}<extra></extra>")
    fig.add_scatter(x=disp["age1"], y=disp["gross"] / 12, name="Household gross", mode="lines",
                    line=dict(color="#0b0b0b", width=2, dash="dot"),
                    hovertemplate="Household gross: %{y:,.0f}<extra></extra>")
    fig.add_scatter(x=disp["age1"], y=disp["net_income"] / 12, name="Household net",
                    mode="lines", line=dict(color="#0b0b0b", width=2.5),
                    hovertemplate="Household net: %{y:,.0f}<extra></extra>")
    add_event_markers(fig, res.events)
    st.plotly_chart(base_layout(fig, "Income over the career: gross (dotted) vs net (solid)",
                                f"per month ({money_note})"), width="stretch")
    st.caption(f"Earnings: {cd.earnings_src.label}{flag(cd.earnings_src)}. Career path = "
               "cross-sectional age-earnings profile at the chosen percentile × career-growth "
               f"exponent, plus {rwg:.2f}%/yr economy-wide real wage growth. Household net "
               "includes child benefits and any US tax. Person net = gross − own local taxes.")
    if not ds.mob.empty:
        m = ds.mob[(ds.mob["iso3"] == iso) & (ds.mob["metric"] == "pseudo_cohort_real_growth_8y")]
        if not m.empty:
            r = m.sort_values("year").iloc[-1]
            st.info(f"Reality check — {r['detail']}: real pay grew ×{r['value']:.2f} "
                    f"({r['note'].split(';')[-1].strip()}). Source: {r['source']}.")

# ---------------------------------------------------------------- taxes
with tabs[3]:
    fig = go.Figure()
    for i, (col, nm) in enumerate((("income_tax_local", "Local income tax"),
                                   ("ssc_local", "Employee social security"),
                                   ("us_tax", "Residual US federal tax"))):
        fig.add_bar(x=disp["age1"], y=disp[col] / 12, name=nm, marker_color=PALETTE[i],
                    hovertemplate=nm + ": %{y:,.0f}<extra></extra>")
    fig.add_scatter(x=disp["age1"], y=-disp["child_benefits"] / 12, name="Child benefits (−)",
                    mode="lines", line=dict(color=PALETTE[5], width=2),
                    hovertemplate="Child benefits: %{y:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    st.plotly_chart(base_layout(fig, "Taxes per month", f"per month ({money_note})"),
                    width="stretch")
    st.caption(f"Local tax curve: {cd.tax_src.label}{flag(cd.tax_src)} — average income-tax "
               "and employee social-security rates at multiples of the national average "
               f"wage ({cd.avg_wage.value:,.0f} {cd.currency}/yr); income splitting for "
               "married couples in DE/LU/PT/PL, quotient familial in FR, youth exemptions "
               "(PL, HU, HR, PT, GR); compulsory occupational pension contributions (CH, NL, "
               "DK, IS, LI). US residents: federal + state + local tax and FICA computed "
               "directly. US citizens abroad: 2026 IRS brackets indexed by inflation; FEIE "
               f"${us.feie:,.0f}/person + housing exclusion vs FTC, cheaper route each year.")
    if us_cit and iso != "USA":
        meth = res.years["us_method"].replace("", pd.NA).dropna()
        st.write(f"**US route chosen:** {meth.mode().iloc[0] if not meth.empty else 'n/a'}. "
                 f"Lifetime residual US tax: {disp['us_tax'].sum():,.0f} {CUR}.")
    if not ds.expat.empty:
        ex = ds.expat[(ds.expat["iso3"] == iso) & (ds.expat["measure"] == "AV_TW")]
        if not ex.empty:
            st.dataframe(ex[["household_type", "year", "local_value", "usa_value",
                             "local_minus_usa"]].rename(columns={
                                 "local_value": "tax wedge %", "usa_value": "USA %"}),
                         hide_index=True, width="stretch")
            st.caption("OECD Taxing Wages tax wedge (income tax + employee & employer SSC as % "
                       "of labour cost) for model households vs the US.")

# ---------------------------------------------------------------- compare
with tabs[4]:
    sel = [cname] + cmp_names
    rows, curves = [], []
    for n in sel:
        c_iso = iso_by_name[n]
        c = ds.country(c_iso, state if c_iso == "USA" else None)
        p = build_plan(c_iso, c_iso == iso)
        r = simulate(p, country_with_overrides(c), us)
        d = to_display(r, c, real, "intl$" if cur == "local" else cur)
        d["country"] = n
        curves.append(d)
        f = d.iloc[0]
        rows.append({"country": n, "net income /mo": f["net_income"] / 12,
                     "spending /mo": f["expenses_recurring"] / 12, "saving /mo": f["saving"] / 12,
                     "saving rate": f["saving_rate"], "avg tax rate": f["local_tax_rate"],
                     f"net worth at {horizon}": d["net_worth"].iloc[-1],
                     "US residual (lifetime)": d["us_tax"].sum(),
                     "house bought at": next((e["age"] for e in r.events
                                              if e["event"] == "House purchase"), "—"),
                     "warnings": len(r.warnings)})
    ccur = "intl $ (PPP)" if cur in ("local", "intl$") else "USD"
    st.caption(f"Same life plan in each country, using each country's own data defaults for "
               f"wages, prices, taxes and housing (your sidebar overrides apply to {cname} "
               f"only). Shown in {ccur}{', real' if real else ', nominal'} so countries are "
               "comparable.")
    fig = go.Figure()
    for i, d in enumerate(curves):
        fig.add_scatter(x=d["age1"], y=d["net_worth"], name=d["country"].iloc[0], mode="lines",
                        line=dict(color=PALETTE[i], width=2.5),
                        hovertemplate=d["country"].iloc[0] + ": %{y:,.0f}<extra></extra>")
    st.plotly_chart(base_layout(fig, "Net worth over life", ccur), width="stretch")
    fig = go.Figure()
    for i, d in enumerate(curves):
        fig.add_scatter(x=d["age1"], y=d["saving"] / 12, name=d["country"].iloc[0],
                        mode="lines", line=dict(color=PALETTE[i], width=2),
                        hovertemplate=d["country"].iloc[0] + ": %{y:,.0f}<extra></extra>")
    st.plotly_chart(base_layout(fig, "Monthly saving", f"per month ({ccur})"),
                    width="stretch")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={k: st.column_config.NumberColumn(format="%.0f") for k in
                                ("net income /mo", "spending /mo", "saving /mo",
                                 f"net worth at {horizon}", "US residual (lifetime)")} |
                 {k: st.column_config.NumberColumn(format="percent") for k in
                  ("saving rate", "avg tax rate")})

# ---------------------------------------------------------------- context
with tabs[5]:
    ctx_iso = [iso_by_name[n] for n in [cname] + cmp_names]

    def ctx_chart(indicator, title, ytitle, col=None):
        fig = go.Figure()
        srcs = set()
        for i, c in enumerate(ctx_iso):
            d = ds.context(c, indicator)
            d = d[d["year"] >= 2005]
            if d.empty:
                continue
            srcs |= set(d["source"].astype(str))
            fig.add_scatter(x=d["year"], y=d["value"], name=names[c], mode="lines+markers",
                            line=dict(color=PALETTE[i], width=2), marker=dict(size=5),
                            hovertemplate=names[c] + ": %{y:.1f}<extra></extra>")
        lay = base_layout(fig, title, ytitle)
        lay.update_layout(height=320, xaxis_title=None)
        (col or st).plotly_chart(lay, width="stretch")
        (col or st).caption("Source: " + ", ".join(sorted(srcs)) if srcs else "No data.")

    # real wage history (stagnation)
    fig = go.Figure()
    srcs = set()
    for i, c in enumerate(ctx_iso):
        r = ds.rwh[(ds.rwh["iso3"] == c) & (ds.rwh["measure"] == "avg_wage") &
                   (ds.rwh["source"] == "OECD average wages")]
        if r.empty:
            r = ds.rwh[(ds.rwh["iso3"] == c) & (ds.rwh["measure"] == "avg_wage")]
        if r.empty:
            continue
        r = r[r["unit_nominal"] == r["unit_nominal"].iloc[0]].sort_values("year")
        srcs.add(str(r["source"].iloc[0]))
        fig.add_scatter(x=r["year"], y=r["index_first_year_100"], name=names[c],
                        mode="lines", line=dict(color=PALETTE[i], width=2),
                        hovertemplate=names[c] + ": %{y:.0f}<extra></extra>")
    lay = base_layout(fig, "Real average wage (first year = 100) — wage stagnation",
                      "index")
    lay.update_layout(height=340, xaxis_title="Year")
    st.plotly_chart(lay, width="stretch")
    st.caption("MEAN wages per full-time equivalent, deflated by CPI. Source: "
               + ", ".join(sorted(srcs)))
    c1, c2 = st.columns(2)
    ctx_chart("unemployment_rate", "Unemployment rate", "% of labour force", c1)
    ctx_chart("job_vacancy_rate", "Job vacancy rate", "%", c2)
    ctx_chart("saving_rate_net", "Household net saving rate", "% of disposable income", c1)
    ctx_chart("world_uncertainty_index", "World Uncertainty Index", "index", c2)
    ctx_chart("youth_unemployment_rate", "Youth unemployment (15-24)", "%", c1)
    ctx_chart("political_stability", "Political stability (WGI)", "-2.5 … 2.5", c2)

# ---------------------------------------------------------------- sources
with tabs[6]:
    rows = []

    def add(item, s, used=None):
        rows.append({"input": item, "value used": used if used is not None else s.value,
                     "unit": s.unit, "source": s.source, "year": str(s.year or ""),
                     "estimated": s.estimated, "quality": s.quality, "note": s.note})
    for k, v in res.inputs.items():
        rows.append({"input": k, "value used": v["value"], "unit": v.get("unit", ""),
                     "source": v["source"], "year": str(v.get("year") or ""),
                     "estimated": v.get("estimated", False), "quality": v.get("quality", ""),
                     "note": v.get("note", "")})
    add("earnings profile", cd.earnings_src, f"{p1} / {p2}")
    add("tax curve", cd.tax_src)
    add("average wage (tax curve)", cd.avg_wage)
    add("child benefit per child /yr", cd.child_benefit_per_child)
    add("childcare per child /yr", cd.childcare_per_child, ccare)
    add("down payment (data default)", cd.down_payment_pct)
    add("mortgage rate (data default)", cd.mortgage_rate)
    add("rent factor with 1 / 2 children", cd.rent_factor_src,
        " / ".join(f"{x:.2f}" for x in cd.rent_factor_per_child))
    add("unemployment rate", cd.unemployment_rate)
    add("exchange rate", cd.lcu_per_usd)
    add("PPP", cd.lcu_per_intl)
    for m, s in sorted(cd.nrr.items()):
        add(f"net replacement rate {m} months", s)
    for k, v in cd.budget.items():
        add(f"budget: {k} /mo", v)
    src_df = pd.DataFrame(rows)
    src_df["value used"] = src_df["value used"].map(
        lambda v: f"{v:,.2f}" if isinstance(v, float) else ("" if v is None else str(v)))
    st.dataframe(src_df, hide_index=True, width="stretch", height=600)
    st.caption("Quality: A = official statistics, B = industry survey / model, C = rough or "
               "derived. ESTIMATE = not directly observed (nowcast, interpolation, proxy, "
               "assumption). Money values are in the source's price year and are rolled to "
               f"{plan.start_year} with actual CPI, then the inflation assumption. Curated "
               "rules and market levels (rents, home prices, lending rules, health, cars, "
               "leave, pensions, US states) live in econsim/params/*.csv with their sources.")
    hh_rows = [{"parameter": k, "value": v, "source": PRM._hh_sources().get(k, ("", ""))[0],
                "quality": PRM._hh_sources().get(k, ("", ""))[1]}
               for k, v in (cd.hh or {}).items() if v == v]
    if hh_rows:
        st.markdown("**Household rules for this country** (econsim/params/household.csv)")
        st.dataframe(pd.DataFrame(hh_rows), hide_index=True, width="stretch")
    st.download_button("Download simulation (CSV)", disp.to_csv(index=False),
                       f"life_plan_{iso}.csv", "text/csv")

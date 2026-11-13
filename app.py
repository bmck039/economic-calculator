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
from econsim.engine import horizon_default, resolve, simulate, to_display
from econsim.model import EDUCATION, TERTIARY, Earner, JobLoss, Plan, age_band

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
    "children": "Children", "childcare": "Children", "university": "Children",
    "long_term_care": "Health & insurance",
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


EDU_SHORT = {"all": "Not specified (all)", "below_upper_secondary": "No high-school diploma",
             "upper_secondary": "High school", "short_tertiary": "Associate's",
             "bachelor": "Bachelor's", "master_plus": "Master's or higher"}


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
    edu_opts = [None] + list(EDUCATION)
    edu_help = ("Highest completed education. Sets the unemployment risk (and the Context "
                "tab's job-market figures) for people of that age and degree, and - if 'Pay "
                "follows the degree' is on - scales pay by the country's full-time earnings "
                "gap for that degree (OECD Education at a Glance). " + "; ".join(
                    f"{EDU_SHORT[k]} = {v}" for k, v in EDUCATION.items()))
    d1 = c1.selectbox("P1 highest degree", edu_opts, format_func=lambda k: EDU_SHORT[k or "all"],
                      help=edu_help)
    d2 = c2.selectbox("P2 highest degree", edu_opts, format_func=lambda k: EDU_SHORT[k or "all"],
                      help=edu_help)
    CAR = cd.career
    job_lbl = {None: "Typical for our age & degree", "FT_grad": "Full time, job needs my degree",
               "FT_nongrad": "Full time, job doesn't need a degree", "PT": "Part time",
               "U": "Looking for work"}
    job_opts = list(job_lbl) if CAR is not None else [None]
    mix_help = ""
    if CAR is not None:
        def _mix(a, e):
            m = CAR.mix(a, e)
            return ", ".join(f"{job_lbl[k].lower()} {v:.0%}" for k, v in m.items())
        mix_help = (f" Today ({'CPS ' + CAR.window if CAR.measured_moves else 'national LFS'})"
                    ", people your age with your degree who work "
                    f"or look for work: P1 {_mix(int(a1), d1)}; P2 {_mix(int(a2), d2)}.")
    j1 = c1.selectbox("P1 now", job_opts, format_func=job_lbl.get, key=K("j1"),
                      help="Your situation today. 'Typical' = today's mix for people of your "
                           "age and degree (students excluded)." + mix_help)
    j2 = c2.selectbox("P2 now", job_opts, format_func=job_lbl.get, key=K("j2"))
    pn1 = c1.number_input(f"P1 gross pay now ({cd.currency}/month)", 0.0, 1e6, 0.0, 100.0,
                          key=K("pn1"), disabled=j1 == "U",
                          help="Optional (0 = typical). Before tax, all jobs. Sets where you "
                               "are in the pay distribution for your situation, age and "
                               "degree; you keep that place as you move on.")
    pn2 = c2.number_input(f"P2 gross pay now ({cd.currency}/month)", 0.0, 1e6, 0.0, 100.0,
                          key=K("pn2"), disabled=j2 == "U")
    career_on = CAR is not None and st.checkbox(
        "Careers follow today's job market", True,
        help="Each year a partner can be full time in a job that needs a degree, full time "
             "in one that doesn't, part time, or looking for work - the same model in every "
             "country. Starting from the typical mix you have, at each age, the mix of "
             "people that age today (surveys show no lasting generation gaps in "
             "unemployment or full-time work). Starting from your own situation, the "
             "difference closes as fast as people really move (US CPS households a year "
             "apart; underemployment as sticky as Strada / Burning Glass measured: 73% of "
             "graduates who start underemployed still are 10 years later). Mix: US CPS; "
             "elsewhere unemployment by age and degree, the part-time share and graduates "
             "in lower-level jobs from the national LFS. Pay grows as your generation's "
             "full-time pay (today's age brackets + the gap that lasts). Off: full-time "
             "earnings by age.")
    jm = st.radio("Job market ahead", ["Stays as it is now", "Keeps changing as in the last "
                                       "year (5 more years)"], disabled=not career_on,
                  help="Keeps changing: the change in year-on-year moves between the prior "
                       "12 months and the last 12 continues for 5 more years, then holds.")
    edu_pay = st.checkbox("Pay follows the degree", True, disabled=not (d1 or d2),
                          help="On: earnings = the country's age profile for all workers x "
                               "the full-time pay of that degree relative to all degrees, by "
                               "age (e.g. a bachelor's in the US ~1.3x at 25-34). Off: the "
                               "all-degree profile (use the income percentile instead).")
    g1 = c1.slider("P1 career growth", 0.0, 2.0, 1.0, 0.1, disabled=bool(career_on),
                   help="1.0 = follow the country's age-earnings profile (cross-section of "
                        "today's workers by age). 0 = flat career; 2 = twice as steep. (Not "
                        "used when careers follow today's job market.)")
    g2 = c2.slider("P2 career growth", 0.0, 2.0, 1.0, 0.1, disabled=bool(career_on))
    ra = cd.retirement_age
    retire_d = int(round(ra.value)) if ra is not None else 67
    retire = st.slider("Retirement age (pay stops, pension starts)", 50, 80, retire_d,
                       key=K("ret"), help=(src(ra, "{:g}") if ra is not None else
                                           "No OECD data: 67.") + " Today's young workers "
                       "retire later than today's retirees where the retirement age is "
                       "linked to life expectancy (e.g. Denmark, Estonia, Italy, Sweden).")
    hz = horizon_default(Plan(country=iso, earners=(Earner(int(a1)), Earner(int(a2)))), cd)
    horizon_d = max(int(hz.value), int(a1) + 5)
    horizon = st.slider("Simulate until P1 age", int(a1) + 5, 105, horizon_d, key=K("hz"),
                        help=src(hz, "{:g}") + " The pension is paid from the retirement age, "
                             "indexed to prices; savings are drawn down slowly.")
    coh_opts = {"history": "As much as gaps have lasted in past surveys",
                "full": "The whole gap lasts", "off": "None (today's age brackets)"}
    coh_mode = st.radio(
        "How far our generation's gap to the one before us lasts", list(coh_opts),
        format_func=coh_opts.get, key=K("coh"),
        help="People of your age now vs people 10 years older when they were your age "
             "(pay relative to all workers; wealth; unemployment). History (US: CPS ASEC "
             "every year since 2003, SCF since 1989) says how much of such a gap is still "
             "there years later; the rest closes as you reach today's older brackets.")
    if career_on:
        st.caption(f"Careers: {CAR.source} ({CAR.window}) - situations and pay for your "
                   "age and degree.")
    else:
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
    fe = cd.family_earn or {}
    pen_lt6 = fe.get(("2", "lt6", "all"))
    work_mode = st.radio("Main parent's work while children are young",
                         ["Country typical", "Full time"], horizontal=True,
                         help="Country typical: the main parent's pay follows the employment "
                              "rate x usual hours of mothers aged 25-49 relative to women "
                              "without children, by the youngest child's age (Eurostat LFS; "
                              "US: CPS microdata; Canada: StatCan), until the youngest is 18. "
                              "Childcare falls with it. Full time: both keep working full "
                              "time (only parental leave).")
    if pen_lt6 is not None:
        st.caption(f"{cd.name}: with 2 children and the youngest under 6, mothers' paid "
                   f"work is {min(pen_lt6, 1.0):.0%} of women without children "
                   f"({cd.family_src.source if cd.family_src else ''} "
                   f"{cd.family_src.year if cd.family_src else ''}).")
    lh = cd.leave_home_age
    home_d = int(round(lh.value)) if lh is not None and lh.value == lh.value else 18
    home_to = st.number_input("Children live at home (and cost money) until age", 16, 35,
                              max(home_d, 18), key=K("home"),
                              help=(f"Default: average age of leaving the parental home in "
                                    f"{cd.name}, {lh.value:.1f} ({lh.source}, {lh.year})."
                                    if lh is not None else "No data: 18.")
                              + " Child benefits and tax allowances still end at 18.")
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
    HP_ = resolve(Plan(country=iso, earners=(Earner(int(a1), education=d1),
                                             Earner(int(a2), education=d2)),
                       buy_house_age=int(buy_age)), cd)["home_price"]
    hp_d = float(round(HP_["value"], -3))
    price = st.number_input(f"Home price today ({cd.currency})", 0.0, 1e9, hp_d, 5000.0,
                            key=K(f"hp{d1}{d2}{int(buy_age)}"),
                            help=HP_["source"] + " Prices follow the house-price outlook "
                            "below until you buy.")
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
    h_opts = ["With incomes", "Recent trend (10 yrs)", "Custom"]
    ht, rt = cd.home_trend, cd.rent_trend
    h_out = st.radio("House prices & rents", h_opts, index=1 if (ht or rt) else 0,
                     horizontal=True, key=K("hout"),
                     help="With incomes: prices and rents grow with real wages (constant "
                          "price-to-income, the long-run norm). Recent trend: the last ~20 "
                          "years' real growth continues for 10 years, then with incomes"
                          + (f" (homes {ht.value:+.1f}%/yr, {ht.year}" if ht else " (no index")
                          + (f"; rents {rt.value:+.1f}%/yr)." if rt else ").")
                          + " Custom: your own rate for both.")
    appr_d = float(round(cd.real_wage_growth_hist.value, 2))
    appr = c2.number_input("Real house-price & rent growth %/yr", -5.0, 5.0, appr_d, 0.1,
                           key=K("appr"), disabled=h_out != "Custom",
                           help="Custom only. Default = real wage growth.")
    r_out = st.radio("After the fixed period, the mortgage rate is", ["Market outlook",
                                                                       "20-year average"],
                     horizontal=True, help="Market outlook: today's mortgage rate moved by "
                     "the change in short rates the market expects (10-year yield - 0.5 term "
                     "premium vs today's 3-month rate)"
                     + (f": {R_m['mortgage_rate_future']:.2f}%" if (R_m := cd.rates or {})
                        .get("mortgage_rate_future") is not None else " (no data here)")
                     + ". 20-year average: the mean rate on new mortgages since ~2005"
                     + (f": {L['rate_longrun']:g}%." if (L := cd.lend or {}).get("rate_longrun")
                        else "."))
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
                   + f"; rate fixed {L['fixation_years']:g}y.")
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
                        help="Each year: unemployment rate for the partner's age band and "
                             "degree (headline ILO rate: people who lose all pay) x net pay x "
                             "(1 - benefit replacement). See the Context tab.")
    efund = st.slider("Emergency fund target (months of spending)", 0.0, 24.0, 6.0, 0.5)
    jl_on = st.checkbox("Simulate a job loss")
    c1, c2, c3 = st.columns(3)
    jl_who = c1.selectbox("Who", [1, 2], disabled=not jl_on)
    jl_age = c2.number_input("At P1 age", 18, 90, int(a1) + 10, disabled=not jl_on)
    jl_m = c3.number_input("Months", 1, 36, 6, disabled=not jl_on)
    if cd.nrr:
        st.caption("Net replacement rates (OECD TaxBEN, couple, latest): " + ", ".join(
            f"{m} mo {s.value:.0f}%" for m, s in sorted(cd.nrr.items())))

# defaults the engine would use for the primary country (rates, portfolio, loans)
_buy = int(buy_age) if mode == "Buy a home" else None
_E = (Earner(int(a1), p1, g1, education=d1), Earner(int(a2), p2, g2, education=d2))
D_ = resolve(Plan(country=iso, earners=_E, buy_house_age=_buy), cd)

with sb.expander("💰 What we have now"):
    own_d = bool(D_["own_home"]["value"])
    own = st.checkbox("We already own our home", own_d, key=K(f"own{a1}{d1}{_buy}"),
                      help=D_["own_home"]["source"])
    DW = resolve(Plan(country=iso, earners=_E, buy_house_age=_buy, own_home=own), cd)

    def _wnum(label, k, step=1000.0, fmt=None):
        d = float(DW[k]["value"])
        v = st.number_input(label, 0.0, 1e9, d, step, key=K(f"{k}{a1}{d1}{own}"),
                            help=DW[k]["source"] + (f" ({DW[k]['note']})"
                                                    if DW[k].get("note") else ""))
        return v, d
    savings0, sav_d = _wnum(f"Savings: bank + investments ({cd.currency})",
                            "starting_savings")
    ret0, ret_d0 = _wnum(f"Retirement accounts ({cd.currency})", "starting_retirement")
    if own:
        hv0, hv_d = _wnum(f"Home value ({cd.currency})", "home_value_now", 5000.0)
        mb0, mb_d = _wnum(f"Mortgage left ({cd.currency})", "mortgage_now", 5000.0)
        mr0 = st.number_input("Rate on our mortgage %", 0.0, 20.0,
                              float(round(DW["mortgage_rate_now"]["value"], 2)), 0.05,
                              key=K(f"mr0{own}"), help=DW["mortgage_rate_now"]["source"])
        my0 = st.number_input("Years left on it", 0, 40,
                              int(DW["mortgage_years_left"]["value"]), 1,
                              key=K(f"my0{a1}{own}"),
                              help=DW["mortgage_years_left"]["source"])
    _sw_src = DW["starting_savings"].get("source", "")
    st.caption("Defaults: what families of your age " + (
        "and degree hold now (Federal Reserve Survey of Consumer Finances, "
        f"{DW['starting_savings'].get('year', '')})." if iso == "USA" and
        DW["starting_savings"].get("year") else
        f"hold (ECB Household Finance and Consumption Survey, "
        f"{DW['starting_savings'].get('year', '')}; all households, by age only)."
        if "HFCS" in _sw_src else
        "- no survey of wealth by age is loaded for this country, so enter yours."))

with sb.expander("🎓 Student debt"):
    sd = cd.student_debt or {}
    # typical borrower's debt per partner's degree (the engine moves it to today's money;
    # only partners up to 30 are assumed to still owe it)
    sl_typ = round(sum(sd[e].value for e, a in ((d1, a1), (d2, a2)) if e in sd and a <= 30),
                   -2)
    sloan = st.number_input(f"Student debt at the start ({cd.currency}, both of us)", 0.0,
                            1e7, float(sl_typ), 1000.0, key=K(f"sl{d1}{d2}"),
                            help=("Default: the typical borrower's debt for each partner's "
                                  "degree - " + "; ".join(
                                      f"{EDU_SHORT.get(k, k)}: {v.value:,.0f} ({v.source}, "
                                      f"{v.year})" for k, v in sd.items()) + ". Not everyone "
                                  "borrows: set 0 if you have none.") if sd else
                            "No national figure: tuition is low and loans small or absent.")
    c1, c2 = st.columns(2)
    slr_d = float(D_["student_loan_rate"]["value"])
    sl_rate = c1.number_input("Interest %/yr", 0.0, 20.0, slr_d, 0.05, key=K("slr"),
                              disabled=sloan == 0,
                              help=D_["student_loan_rate"]["source"] + (
                                  f" ({D_['student_loan_rate']['note']})"
                                  if D_["student_loan_rate"].get("note") else ""))
    sl_years = c2.number_input("Repaid over (years)", 1, 30, 10, key=K("sly"),
                               disabled=sloan == 0 or iso == "GBR",
                               help="Fixed monthly payments (US standard plan: 10 years; "
                                    "income-driven plans run 20-25). UK: 9% of pay above "
                                    "GBP 25,000 instead, written off after 40 years.")

with sb.expander("🧾 Other costs & options"):
    church = st.checkbox("Church members (Germany: +8-9% of income tax)", False,
                         disabled=iso != "DEU")
    priv_h = st.checkbox("Private health insurance (Ireland)", False, disabled=iso != "IRL")
    elast = st.slider("How much spending follows income (below a typical couple's)",
                      0.0, 1.5, 1.0, 0.05,
                      help="Living costs = survey couple's budget x (your income after housing"
                           " / theirs, both growing with average wages)^this. 0 = always "
                           "the survey average; 1 = proportional. The defaults (1.0 below a "
                           "typical couple's income, 0.4 above it) fit best what couples "
                           "actually accumulated in both backtests (US: SCF households, "
                           "including the spread within each generation; euro area: HFCS "
                           "tables).")
    elast_hi = st.slider("... and above a typical couple's income", 0.0, 1.5, 0.4, 0.05,
                         help="Richer households save a larger share of each extra dollar "
                              "(Dynan, Skinner & Zeldes 2004): with one elasticity for all "
                              "incomes, the top third of earners in the backtest ended up "
                              "with too little.")
    surv_d = 1.0
    surv = st.slider("Add back spending that surveys miss", 0.0, 1.0, surv_d, 0.05,
                     key=K("surv"),
                     help="Household budget surveys record less than national accounts "
                          "(e.g. food ~65-80%, clothes, alcohol and eating out about half; "
                          "Eurostat budget surveys vs national accounts, US Consumer "
                          "Expenditure Survey vs BEA consumption). 1 = scale each category up "
                          "to the national-accounts level; 0 = raw survey. Part of the gap is "
                          "the very rich, non-profits and the informal economy, but the "
                          "default adds back all of it: with it, couples accumulate about what "
                          "they really did (backtests on the Fed's SCF and the ECB's HFCS) "
                          "and each country saves its national household saving rate.")

with sb.expander("👪 Family help & later life"):
    c1, c2 = st.columns(2)
    gift = c1.number_input(f"Gift from family ({cd.currency}, today's money)", 0.0, 1e9, 0.0,
                           5000.0, key=K("gift"),
                           help="E.g. help with a deposit. Arrives at the age on the right "
                                "(default: the planned buying age). Common in Southern and "
                                "Eastern Europe, where many young people buy with family help.")
    gift_age = c2.number_input("Gift at P1 age (0 = buying age)", 0, 100, 0, key=K("gifta"))
    inh = c1.number_input(f"Inheritance ({cd.currency}, after tax)", 0.0, 1e9, 0.0, 10000.0,
                          key=K("inh"))
    le_now = cd.le65.get(("female", max((y for s_, y in cd.le65 if s_ == "female"),
                                        default=0))) if cd.le65 else None
    inh_d = int(round(le_now)) - 30 if le_now else 55
    inh_age = c2.number_input("Inheritance at P1 age", 18, 100, max(inh_d, int(a1)),
                              key=K("inha"), help="Default: when parents ~30 years older "
                              "reach today's life expectancy at 65 (women).")
    uc = cd.university_cost
    uni = st.slider("Parents pay this share of children's university", 0, 100, 0, 10,
                    help=(f"{src(uc)}. " if uc is not None else "No OECD data. ")
                    + "Paid per child for the years below from age 18; living costs while "
                      "at home are already counted (children live at home until the leaving "
                      "age).")
    uni_y = st.number_input("University years per child", 1, 8, 4)
    wpen = st.slider("Main parent's lasting pay penalty after children (%)", 0, 20, 0,
                     help="Hourly pay that never catches up (missed promotions, more family-"
                          "friendly jobs) on top of the hours and employment effect above. In "
                          "Denmark the long-run earnings gap after children is ~20%, about a "
                          "third each from participation, hours and wage rates (Kleven, "
                          "Landais & Søgaard 2019) - i.e. ~7%.")
    c1, c2 = st.columns(2)
    ltc_y = c1.number_input("Years of paid long-term care", 0, 15, 0,
                            help="For one partner, at the end of the plan.")
    ltc_d = 74_400 * cd.lcu_per_usd.value if iso == "USA" else 0.0
    ltc_c = c2.number_input(f"Care cost per year ({cd.currency})", 0.0, 1e7, float(ltc_d),
                            1000.0, key=K("ltc"), disabled=ltc_y == 0,
                            help="US default: CareScout 2025 median assisted living ($74,400; "
                                 "private nursing-home room $129,575). Elsewhere public "
                                 "long-term care covers most of the cost: enter the local "
                                 "out-of-pocket amount.")
    spend_down = st.radio("In retirement we spend", ["Pension + savings",
                                                     "Only the pension"],
                          horizontal=True,
                          help="Pension + savings: spending = pension + savings / years "
                               "left, as US minimum distributions (which most retirees' "
                               "withdrawals track). Only the pension: savings are left "
                               "untouched (to heirs, or for care) unless the pension can't "
                               "cover basic costs.") == "Pension + savings"

with sb.expander("📈 Returns, growth & display"):
    c1, c2 = st.columns(2)
    R_ = cd.rates or {}
    dep_hh, eq_hh = D_["deposit_share"]["value"], D_["equity_share"]["value"]
    hold = st.radio("How we hold our savings", [f"Like typical households",
                                               "Invest everything", "Custom"],
                    help="Above the emergency fund. Typical: the split households in "
                         f"{cd.name} actually hold - {dep_hh:.0f}% in bank deposits; of the "
                         f"rest {eq_hh:.0f}% in shares and funds (OECD financial balance "
                         "sheets; national totals lean toward wealthier households, so a "
                         "typical family holds even more in the bank). Invest everything: no "
                         "deposits beyond the emergency fund.")
    c3, c4 = st.columns(2)
    dep = c3.slider("% kept in bank deposits", 0, 100, int(round(dep_hh)) if hold.startswith(
        "Like") else 0, 5, disabled=hold != "Custom", key=K(f"dep{hold}"))
    eq = c4.slider("% of investments in shares", 0, 100, int(round(eq_hh)) if hold.startswith(
        "Like") else 60, 5, disabled=hold == "Like typical households", key=K(f"eq{hold}"),
        help="The rest in bonds. Default return = today's 10-year government yield (the "
             "market's price for the next decade) + this share x a 3.5% equity premium - "
             "0.2% fees. 3.5% lies between the world equity premium over bonds since 1900 "
             "(~3%, Dimson-Marsh-Staunton) and current implied estimates (~4%).") / 100
    y10 = R_.get("y10")
    ret_d = round(y10 + eq * 3.5 - 0.2, 2) if y10 is not None else 5.0
    ret = c1.number_input("Investments %/yr (nominal, after fees, before tax)", -5.0, 15.0,
                          ret_d, 0.25, key=K(f"ret{eq}"),
                          help=(f"10-year yield {y10:.2f}% ({R_.get('y10_source')}, "
                                f"{R_.get('y10_period')}). " if y10 is not None else
                                "No yield data: 5%. ") + f"Tax drag for {cd.name}: "
                          f"{(cd.hh or {}).get('invest_tax_drag', 0):g} pp/yr (main tax-"
                          "advantaged route).")
    cash_d = round(float(D_["cash_return"]["value"]), 2)
    cash_r = c2.number_input("Emergency fund %/yr", 0.0, 10.0, cash_d, 0.05,
                             key=K("cash"), help=D_["cash_return"]["source"])
    depr_d = round(float(D_["deposit_return"]["value"]), 2)
    dep_r = c2.number_input("Bank deposits %/yr", 0.0, 10.0, depr_d, 0.05, key=K("depr"),
                            disabled=hold == "Invest everything",
                            help=D_["deposit_return"]["source"])
    infl_d = round(float(D_["inflation"]["value"]), 2)
    near_i = D_.get("inflation_next_5y")
    infl = c1.number_input("Inflation %/yr" + (" (after 5 years)" if near_i else ""), -2.0,
                           15.0, infl_d, 0.1, key=K("infl"),
                           help=D_["inflation"]["source"] + (
                               f". Next 5 years: {near_i['value']:.2f}% ({near_i['source']})."
                               " Changing this sets one rate for all years."
                               if near_i else ""))
    pt_ = cd.price_trends or {}
    costs_out = st.radio("Costs ahead", ["Each cost's recent trend", "All rise with prices"],
                         horizontal=True, disabled=not pt_,
                         help="Recent trend: each budget line's price trend relative to all "
                              "prices over the last 10 years continues for the next 10 "
                              "(HICP by category in Europe, CPI components in the US), "
                              "then moves with prices and incomes like the rest. Costs that "
                              "already rise with wages (childcare, health premiums, cars) "
                              "only take the part beyond wage growth." + (
                                  " Here: " + ", ".join(
                                      f"{k.replace('_', ' ')} {v[0]:+.1f}%/yr"
                                      for k, v in sorted(pt_.items(), key=lambda x: -x[1][0])
                                      [:6]) + "." if pt_ else ""))
    wt = cd.wage_trend
    w_out = st.radio("Wage growth outlook", ["Official projection", "Recent 20-year trend"],
                     index=1 if wt is not None else 0, horizontal=True, disabled=wt is None,
                     help="Official projection: long-run productivity growth assumed by the EC "
                          "Ageing Report / OBR / CBO / PBO (expert forecasts that include "
                          "convergence of poorer countries). Trend: the last ~20 years of "
                          "real average-wage growth continue"
                          + (f" ({wt.value:.2f}%/yr, {wt.year})." if wt is not None else "."))
    rwg_src = wt if (w_out != "Official projection" and wt is not None) else \
        cd.real_wage_growth_hist
    rwg_d = float(rwg_src.value) if rwg_src.value == rwg_src.value else 0.5
    rwg = c2.number_input("Real wage growth %/yr (economy-wide)", -3.0, 5.0, round(rwg_d, 2),
                          0.05, key=K(f"rwg{w_out}"), help=src(rwg_src, "{:.2f}"))
    real = st.toggle("Show in today's money (real)", True)
    defl = st.radio("Today's money means", ["cpi", "own"], horizontal=True, key=K("defl"),
                    format_func={"cpi": "Consumer prices (CPI)",
                                 "own": "What our own spending costs"}.get,
                    disabled=not real,
                    help="CPI: the national basket. Own: your household's own basket "
                         "year by year - your rent at market trends, your mortgage (fixed "
                         "payments do not rise), and each budget line at its own price "
                         "trend - like the UK's Household Costs Indices.")
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
    # money amounts entered for the main country move to others at PPP
    ppp_f = 1.0 if override else \
        ds.country(iso3).lcu_per_intl.value / cd.lcu_per_intl.value
    p = Plan(
        country=iso3, earners=(
            Earner(int(a1), p1, g1, education=d1, job=j1,
                   pay_now=float(pn1) if override and pn1 > 0 and j1 != "U" else None),
            Earner(int(a2), p2, g2, education=d2, job=j2,
                   pay_now=float(pn2) if override and pn2 > 0 and j2 != "U" else None)),
        education_pay=edu_pay, career_model=bool(career_on) or CAR is None,
        job_market="trend" if jm.startswith("Keeps") else "now",
        price_outlook="trend" if costs_out.startswith("Each") else "flat",
        horizon_age=int(horizon) if override and int(horizon) != horizon_d else None,
        retirement_age=int(retire) if override and int(retire) != retire_d else None,
        equity_share=None if hold == "Like typical households" else eq,
        deposit_share=None if hold == "Like typical households" else float(dep),
        deposit_return=dep_r if override and abs(dep_r - depr_d) > 1e-9 else None,
        wage_outlook="trend" if w_out != "Official projection" else
        "projection", home_outlook="trend" if h_out == "Recent trend (10 yrs)" else "incomes",
        rate_outlook="market" if r_out == "Market outlook" else "history",
        spend_down=spend_down, family_gift=float(gift) * ppp_f,
        family_gift_age=int(gift_age) or None, inheritance=float(inh) * ppp_f,
        inheritance_age=int(inh_age) if override and int(inh_age) != max(inh_d, int(a1))
        else None, university_share=float(uni), university_years=int(uni_y),
        child_wage_penalty=float(wpen), ltc_years=int(ltc_y),
        ltc_cost=float(ltc_c) if override and abs(ltc_c - ltc_d) > 1e-9 else None,
        marriage_age=int(m_age) if married else None,
        child_ages=(None if k1 == "none" else int(k1), None if k2 == "none" else int(k2)),
        childcare_from=cc_from, childcare_to=cc_to,
        buy_house_age=int(buy_age) if mode == "Buy a home" else None,
        max_payment_to_income=None if rules else float(dsti),
        cars=int(cars) if override else None, unemployment_risk=urisk,
        primary_parent=0 if leave_who == "Partner 1" else 1, leave_scale=leave_scale,
        private_health=priv_h, church_tax=church, student_loan=float(sloan) if override and abs(sloan - sl_typ) > 1e-9 else 0.0,
        typical_student_debt=bool(sl_typ) and abs(sloan - sl_typ) < 1e-9,
        student_loan_rate=float(sl_rate) if override and abs(sl_rate - slr_d) > 1e-9
        else None, student_loan_years=int(sl_years) if int(sl_years) != 10 else None,
        budget_elasticity=elast, budget_elasticity_high=elast_hi, state=state if iso3 == "USA" else None,
        survey_correction=surv if abs(surv - surv_d) > 1e-9 else None, child_penalty=work_mode == "Country typical",
        child_dependent_to=int(home_to) if override and int(home_to) != max(home_d, 18)
        else None,
        budget_scale=budget_scale, emergency_fund_months=efund, job_loss=jl,
        return_nominal=ret if override and abs(ret - ret_d) > 1e-9 else None,
        cash_return=cash_r if override and abs(cash_r - cash_d) > 1e-9 else None,
        inflation=infl if override and abs(infl - infl_d) > 1e-9 else None,
        us_citizens=us_cit,
        us_employer=us_emp, cohort_effects=coh_mode,
        starting_savings=(float(savings0) if abs(savings0 - sav_d) > 1e-9 else None)
        if override else float(savings0) / cd.lcu_per_intl.value *
        ds.country(iso3).lcu_per_intl.value,
        starting_retirement=(float(ret0) if abs(ret0 - ret_d0) > 1e-9 else None)
        if override else float(ret0) / cd.lcu_per_intl.value *
        ds.country(iso3).lcu_per_intl.value,
        own_home=own if override else False)
    if override and own:
        p = replace(p, home_value_now=float(hv0) if abs(hv0 - hv_d) > 1e-9 else None,
                    mortgage_now=float(mb0) if abs(mb0 - mb_d) > 1e-9 else None,
                    mortgage_rate_now=float(mr0) if abs(
                        mr0 - round(DW["mortgage_rate_now"]["value"], 2)) > 1e-9 else None,
                    mortgage_years_left=int(my0) if int(my0) != int(
                        DW["mortgage_years_left"]["value"]) else None)
    if override:
        # pass a value only when the user changed it, so provenance stays on defaults
        def ov(val, default, nd=0):
            return None if abs(val - round(default, nd)) < 1e-9 else val
        p = replace(p, wedding_cost=ov(wedding, cd.wedding_cost.value, -2),
                    home_price=ov(price, hp_d, -3),
                    down_payment_pct=ov(dp, cd.down_payment_pct.value, 1),
                    transfer_tax_pct=ov(tt, cd.transfer_tax_pct.value, 9),
                    mortgage_rate=ov(mr, cd.mortgage_rate.value, 2),
                    rent=ov(rent, cd.rent.value), emergency_annual_cost=ov(
                        emerg, cd.emergency_expense_size.value),
                    real_wage_growth=ov(rwg, rwg_d, 2),
                    owner_cost_pct=ov(owner, owner_d, 2),
                    home_real_appreciation=appr if h_out == "Custom" else None,
                    mortgage_years=None if int(term) == term_d else int(term))
    return p


def country_with_overrides(c):
    return replace(c, childcare_per_child=c.childcare_per_child.with_value(ccare)) \
        if c.iso3 == iso else c


us = ds.us_params()
plan = build_plan(iso, True)
cd_run = country_with_overrides(cd)
res = simulate(plan, cd_run, us)
disp = to_display(res, cd_run, real, cur, defl)
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
if res.summary.get("own_inflation") is not None:
    hc = res.inputs.get("household_costs_5y")
    st.caption(
        f"Your own inflation over the next {res.summary['own_inflation_years']} years: "
        f"{res.summary['own_inflation']:.2f}%/yr (your rent or mortgage and each budget line "
        f"at its own price trend) vs {res.summary['cpi_inflation']:.2f}%/yr consumer prices."
        + (f" Over the last 5 years what US households pay for housing and everything else "
           f"rose {hc['value']:.1f}%/yr - see the inputs table." if hc else ""))
conv = {"local": 1.0, "USD": cd.lcu_per_usd.value, "intl$": cd.lcu_per_intl.value}[cur]
S_ = res.summary
ra_ = S_.get("retirement_age", retire)
r_row = disp[disp["age1"] == ra_]
nw_ret = r_row["net_worth"].iloc[0] if not r_row.empty else last["net_worth"]
pen_m = (S_.get("pension_income_real", 0.0) / conv / 12) if real else None
kc[3].metric(f"Net worth at retirement ({ra_})", f"{nw_ret:,.0f}",
             (f"pension {pen_m:,.0f}/mo" + (f" ({S_['pension_vs_last_net']:.0%} of last pay)"
                                             if S_.get("pension_vs_last_net") else ""))
             if pen_m else None, delta_color="off", delta_arrow="off",
             help="Liquid savings + home equity + pension pots. Pension: both partners' "
                  "career-average pay relative to the average wage x the OECD net "
                  "replacement rate at that pay level (mandatory schemes, full career, "
                  f"legislated rules; {S_.get('pension_nrr', 0):.0f}% at the average wage), "
                  "today's money."
                  + (f" Savings run out at {S_['savings_run_out_age']}."
                     if S_.get("savings_run_out_age") else
                     f" At {int(last['age1'])}: {last['net_worth']:,.0f}."))
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
                 "university", "long_term_care",
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
    fig.add_bar(x=disp["age1"], y=disp["cash"], name="Bank (emergency fund + deposits)",
                marker_color=PALETTE[3], hovertemplate="Bank: %{y:,.0f}<extra></extra>")
    fig.add_bar(x=disp["age1"], y=disp["invest"], name="Investments",
                marker_color=PALETTE[0], hovertemplate="Investments: %{y:,.0f}<extra></extra>")
    if disp["home_equity"].abs().sum() > 0:
        fig.add_bar(x=disp["age1"], y=disp["home_equity"], name="Home equity",
                    marker_color=PALETTE[1],
                    hovertemplate="Home equity: %{y:,.0f}<extra></extra>")
    if disp.get("retirement_account") is not None and disp["retirement_account"].abs().sum() > 0:
        fig.add_bar(x=disp["age1"], y=disp["retirement_account"],
                    name="Employer pension account", marker_color=PALETTE[2],
                    hovertemplate="Employer pension: %{y:,.0f}<extra></extra>")
    fig.update_layout(barmode="relative")
    fig.add_scatter(x=disp["age1"], y=disp["net_worth"], name="Net worth", mode="lines",
                    line=dict(color="#0b0b0b", width=2.5),
                    hovertemplate="Net worth: %{y:,.0f}<extra></extra>")
    add_event_markers(fig, res.events)
    st.plotly_chart(base_layout(fig, "Cumulative savings and net worth", money_note),
                    width="stretch")
    st.caption(f"Emergency fund ({efund:g} months of spending) is kept in cash at "
               f"{res.inputs['cash_return']['value']:g}%; of the rest "
               f"{res.inputs['deposit_share']['value']:.0f}% stays in bank deposits at "
               f"{res.inputs['deposit_return']['value']:g}% and the remainder is invested "
               f"({res.inputs['equity_share']['value']:.0f}% in shares) at "
               f"{res.inputs['return_nominal']['value']:g}% nominal, rebalanced yearly. "
               "Home equity = home value − "
               "mortgage. Pension pots become spendable at retirement. "
               "Employer pension contributions (UK auto-enrolment is in the state pension "
               "figure instead; US 401(k), Canada) build a retirement account that can't be "
               "used for a deposit.")
    if (disp.get("spending_squeeze") is not None) and (disp["spending_squeeze"] > 0).any():
        sq = disp[disp["spending_squeeze"] > 0]
        fig2 = go.Figure()
        fig2.add_bar(x=sq["age1"], y=sq["spending_squeeze"] / 12, marker_color=PALETTE[7],
                     name="Spending cut", hovertemplate="Cut: %{y:,.0f}/mo<extra></extra>")
        lay = base_layout(fig2, "Money ran short: discretionary spending cut (per month)",
                          money_note)
        lay.update_layout(height=280, showlegend=False)
        st.plotly_chart(lay, width="stretch")
        st.caption("When income doesn't cover the survey-typical budget and savings are gone, "
                   "the household cuts recreation, eating out, clothes and furnishings (and "
                   "some of the children's spending), up to half of them, before borrowing - "
                   "what people mean by 'making ends meet with difficulty'.")

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
    if disp["pension_income"].abs().sum() > 0:
        fig.add_scatter(x=disp["age1"], y=disp["pension_income"] / 12, name="Pensions (net)",
                        mode="lines", line=dict(color=PALETTE[2], width=2.5),
                        hovertemplate="Pensions: %{y:,.0f}<extra></extra>")
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
                     "retires at": r.summary.get("retirement_age"),
                     "net worth at retirement": float(d.loc[d["age1"] == r.summary.get(
                         "retirement_age"), "net_worth"].iloc[0]) if (d["age1"] == r.summary.get(
                             "retirement_age")).any() else float("nan"),
                     f"net worth at end ({int(d['age1'].iloc[-1])})": d["net_worth"].iloc[-1],
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
                                 "net worth at retirement", "US residual (lifetime)")} |
                 {k: st.column_config.NumberColumn(format="percent") for k in
                  ("saving rate", "avg tax rate")})

# ---------------------------------------------------------------- context
with tabs[5]:
    ctx_iso = [iso_by_name[n] for n in [cname] + cmp_names]

    # ---- job market for each partner's age and degree
    st.subheader("Job market for your age and degree")
    st.caption("**Functional unemployment** counts everyone who wants a job and can't get "
               "one: the unemployed + part-timers who want full-time hours + people who want "
               "work but have stopped looking (Eurostat 'labour-market slack', the "
               "international version of the US U-6). **Headline** is the official ILO rate. "
               "Rates are for people of the partner's age band and highest degree (e.g. a "
               "bachelor's at 23 = 20-24-year-old graduates, i.e. recent graduates). "
               "Same definitions in every country; the US is computed from Census/BLS CPS "
               "microdata, Europe from Eurostat's labour-force survey, Canada and the UK "
               "from OECD + StatCan.")

    def pct(s):
        return "—" if s is None else f"{s.value:.1f}%" + (" ⚠️" if s.estimated else "")

    def lm_help(s, what):
        if s is None:
            return f"No comparable {what} data for this group."
        return f"{s.source}, {s.year}" + (" — ESTIMATE" if s.estimated else "") + \
            (f". {s.note}" if s.note else "")

    who = [(int(a1), d1 or "all"), (int(a2), d2 or "all")]
    for col, (i, (age, edu)) in zip(st.columns(2), enumerate(who)):
        band = age_band(age)
        lab = cd.labour or {}
        f_ = lab.get(("functional", band, edu))
        u_ = lab.get(("unemployment", band, edu))
        h_ = lab.get(("hours_underemployment", band, edu))
        o_ = lab.get(("overqualification", band, edu)) if edu in TERTIARY else None
        col.markdown(f"**Partner {i + 1}** — ages {band}, {EDU_SHORT[edu].lower()}"
                     + (f" · {u_.year}" if u_ is not None else ""))
        m1, m2 = col.columns(2)
        m1.metric("Functional unemployment", pct(f_), help=lm_help(f_, "slack"))
        m2.metric("Headline unemployment", pct(u_), help=lm_help(u_, "unemployment"))
        m3, m4 = col.columns(2)
        m3.metric("Underemployed (want more hours)", pct(h_), help=lm_help(h_, "hours"))
        m4.metric("Graduates in non-graduate jobs", pct(o_) if edu in TERTIARY else "n/a",
                  help=(lm_help(o_, "over-qualification") +
                        " Over-qualification: employed graduates in jobs that do not "
                        "usually need a degree (clerical, sales, service, trades, "
                        "operators). US: outside management/business/financial/"
                        "professional occupations (approximation of the EU's ISCO 4-9).")
                  if edu in TERTIARY else "Applies to degree holders.")

    def lm_chart(indicator, title, age, edu, dashed=None, col=None):
        band, fig, srcs = age_band(age), go.Figure(), set()
        for i, c in enumerate(ctx_iso):
            for ind, dash in [(indicator, None)] + ([(dashed, "dot")] if dashed else []):
                d = ds.labour_history(c, ind, band, edu)
                if d.empty:
                    continue
                srcs.add(str(d["source"].iloc[-1]))
                nm = names[c] + ("" if dash is None else " (headline)")
                fig.add_scatter(x=d["year"], y=d["value"], name=nm, mode="lines+markers",
                                line=dict(color=PALETTE[i], width=2, dash=dash),
                                marker=dict(size=5), showlegend=dash is None or i == 0,
                                hovertemplate=nm + ": %{y:.1f}%<extra></extra>")
        lay = base_layout(fig, title, "%")
        lay.update_layout(height=340, xaxis_title=None)
        (col or st).plotly_chart(lay, width="stretch")
        (col or st).caption(("Sources: " + "; ".join(sorted(srcs))) if srcs else
                            "No data for this age band and degree.")

    a_, e_ = who[0]
    c1, c2 = st.columns(2)
    lm_chart("functional", f"Functional unemployment — ages {age_band(a_)}, "
             f"{EDU_SHORT[e_].lower()} (dotted: headline)", a_, e_, "unemployment", c1)
    if e_ in TERTIARY:
        lm_chart("overqualification", f"Graduates in non-graduate jobs — ages "
                 f"{age_band(a_)}", a_, e_, None, c2)
    else:
        lm_chart("hours_underemployment", f"Underemployed part-timers — ages {age_band(a_)}, "
                 f"{EDU_SHORT[e_].lower()}", a_, e_, None, c2)
    with st.expander("Why youth unemployment looks so different across countries"):
        st.markdown(
            "All countries use the same ILO definition (no work, looked in the last 4 weeks, "
            "can start within 2 weeks), but who is in the 15-24 labour force differs:\n\n"
            "* **Students.** A full-time student looking for a part-time or summer job counts "
            "as unemployed. In Sweden about 60% of unemployed 15-24-year-olds are full-time "
            "students (SCB: 102,000 of 170,000 in 2025); in the US about 30% (CPS 2026).\n"
            "* **Rate vs share of all young people.** The rate divides by the youth labour "
            "force, not all young people. As a share of ALL 15-24-year-olds, ~14% of Swedes "
            "and ~5% of Americans are unemployed (2.7x); counting only those who are not "
            "students, ~5.5% vs ~3.4% (1.6x). Sweden's youth market is weaker, but far less "
            "than 24% vs 9% suggests.\n"
            "* **Apprenticeships.** In Germany, Austria and Switzerland apprentices are "
            "employed, which lowers youth rates.\n\n"
            "That is why this section uses ages 20-24 / 25-29 / 30-34 and the partner's "
            "degree: it compares people like you rather than all teenagers.")

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
    ctx_chart("inflation_cpi", "Inflation (CPI)", "% per year", c1)
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

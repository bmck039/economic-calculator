"""The sidebar: every input of the plan, one function per section. Each section reads
what earlier sections chose from one namespace, `v`, and adds its own choices to it."""

from __future__ import annotations

from types import SimpleNamespace

import streamlit as st

from econsim import params as PRM
from econsim.inputs import horizon_default, resolve
from econsim.model import EDUCATION, Earner, Plan
from ui import settings
from ui.common import EDU_SHORT, flag, src
from ui.names import global_key as G
from ui.names import place_key
from ui.settings import follow


def render(ds, countries: list[tuple[str, str]]) -> SimpleNamespace:
    """Draw the sidebar; returns every choice (and the defaults they were compared with)."""
    v = SimpleNamespace(
        ds=ds,
        countries=countries,
        names=dict(countries),
        iso_by_name={name: iso for iso, name in countries},
    )
    st.sidebar.title("Life plan")
    v.share_box = settings.restore(st.sidebar)
    for section in (
        _place,
        _partners,
        _family,
        _housing,
        _emergencies,
        _wealth_now,
        _student_debt,
        _other_costs,
        _later_life,
        _returns,
        _us_abroad,
    ):
        section(v)
    return v


def _place(v: SimpleNamespace) -> None:
    """Country (and US state), and the places to compare with."""
    default_c = "Netherlands" if "Netherlands" in v.iso_by_name else v.countries[0][1]
    cname = st.sidebar.selectbox(
        "Country",
        [n for _, n in v.countries],
        index=[n for _, n in v.countries].index(default_c),
        key=G("country"),
    )
    v.iso = v.iso_by_name[cname]
    v.state = None
    if v.iso == "USA":
        st_names = PRM.us_state_names()
        st_codes = [None] + sorted(st_names, key=st_names.get)
        v.state = st.sidebar.selectbox(
            "US state",
            st_codes,
            format_func=lambda c: "National average" if c is None else st_names[c],
            help="State earnings, rents, home prices, property tax, state income "
            "tax, price level, health premiums, car insurance and paid "
            "family leave.",
            key=G("us_state"),
        )
    v.cd = v.ds.country(v.iso, v.state)
    # places to compare: every country, and (US) every state - except the one chosen above
    ST_NAMES = PRM.us_state_names()
    v.PLACES = {n: (i, None) for i, n in v.countries}
    if "USA" in v.names:
        for c_ in sorted(ST_NAMES, key=ST_NAMES.get):
            v.PLACES[f"{v.names['USA']} – {ST_NAMES[c_]}"] = ("USA", c_)
    v.here = (v.iso, v.state)
    v.cmp_names = st.sidebar.multiselect(
        "Compare with (up to 3 total)",
        [n for n, pl in v.PLACES.items() if pl != v.here],
        default=[
            n for n in ("United States", "Germany") if n in v.PLACES and v.PLACES[n] != v.here
        ][:2],
        max_selections=2,
        key=G("compare_with"),
        help="Countries, or US states (type a state's name).",
    )
    v.cmp_names = [n for n in v.cmp_names if n in v.PLACES and v.PLACES[n] != v.here]

    def K(name: str) -> str:
        """Session key of a per-place setting (its default differs by country and state)."""
        return place_key(name, v.iso, v.state)

    v.K = K


def _partners(v: SimpleNamespace) -> None:
    """Ages, pay, degrees, jobs, careers, retirement and the horizon."""
    with st.sidebar.expander("👫 Partners & career", expanded=True):
        c1, c2 = st.columns(2)
        v.a1 = c1.number_input("Partner 1 age", 18, 70, 23, key=G("p1_age"))
        v.a2 = c2.number_input("Partner 2 age", 18, 70, 24, key=G("p2_age"))
        pct_opts = [p for p in ("p10", "p25", "median", "p75", "p90") if p in v.cd.earnings] or [
            "median"
        ]
        v.pct_lbl = {
            "p10": "10th pct",
            "p25": "25th pct",
            "median": "Median",
            "p75": "75th pct",
            "p90": "90th pct",
        }
        v.p1 = c1.selectbox(
            "P1 income",
            pct_opts,
            index=pct_opts.index("median") if "median" in pct_opts else 0,
            format_func=v.pct_lbl.get,
            key=G("p1_pay_level"),
        )
        v.p2 = c2.selectbox(
            "P2 income",
            pct_opts,
            index=pct_opts.index("median") if "median" in pct_opts else 0,
            format_func=v.pct_lbl.get,
            key=G("p2_pay_level"),
        )
        edu_opts = [None] + list(EDUCATION)
        edu_help = (
            "Highest completed education. Sets the unemployment risk (and the Context "
            "tab's job-market figures) for people of that age and degree, and - if 'Pay "
            "follows the degree' is on - scales pay by the country's full-time earnings "
            "gap for that degree (OECD Education at a Glance). "
            + "; ".join(f"{EDU_SHORT[k]} = {lbl}" for k, lbl in EDUCATION.items())
        )
        v.d1 = c1.selectbox(
            "P1 highest degree",
            edu_opts,
            format_func=lambda k: EDU_SHORT[k or "all"],
            help=edu_help,
            key=G("p1_degree"),
        )
        v.d2 = c2.selectbox(
            "P2 highest degree",
            edu_opts,
            format_func=lambda k: EDU_SHORT[k or "all"],
            help=edu_help,
            key=G("p2_degree"),
        )
        v.CAR = v.cd.career
        job_lbl = {
            None: "Typical for our age & degree",
            "FT_grad": "Full time, job needs my degree",
            "FT_nongrad": "Full time, job doesn't need a degree",
            "PT": "Part time",
            "U": "Looking for work",
        }
        job_opts = list(job_lbl) if v.CAR is not None else [None]
        mix_help = ""
        if v.CAR is not None:

            def _mix(a, e):
                m = v.CAR.mix(a, e)
                return ", ".join(f"{job_lbl[k].lower()} {share:.0%}" for k, share in m.items())

            mix_help = (
                f" Today ({'CPS ' + v.CAR.window if v.CAR.measured_moves else 'national LFS'})"
                ", people your age with your degree who work "
                f"or look for work: P1 {_mix(int(v.a1), v.d1)}; P2 {_mix(int(v.a2), v.d2)}."
            )
        v.j1 = c1.selectbox(
            "P1 now",
            job_opts,
            format_func=job_lbl.get,
            key=v.K("p1_job_now"),
            help="Your situation today. 'Typical' = today's mix for people of your "
            "age and degree (students excluded)." + mix_help,
        )
        v.j2 = c2.selectbox("P2 now", job_opts, format_func=job_lbl.get, key=v.K("p2_job_now"))
        v.pn1 = c1.number_input(
            f"P1 gross pay now ({v.cd.currency}/month)",
            0.0,
            1e6,
            0.0,
            100.0,
            key=v.K("p1_pay_now"),
            disabled=v.j1 == "U",
            help="Optional (0 = typical). Before tax, all jobs. Sets where you "
            "are in the pay distribution for your situation, age and "
            "degree; you keep that place as you move on.",
        )
        v.pn2 = c2.number_input(
            f"P2 gross pay now ({v.cd.currency}/month)",
            0.0,
            1e6,
            0.0,
            100.0,
            key=v.K("p2_pay_now"),
            disabled=v.j2 == "U",
        )
        v.career_on = v.CAR is not None and st.checkbox(
            "Careers follow today's job market",
            True,
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
            "earnings by age.",
            key=G("careers_follow_market"),
        )
        v.jm = st.radio(
            "Job market ahead",
            ["Stays as it is now", "Keeps changing as in the last year (5 more years)"],
            disabled=not v.career_on,
            help="Keeps changing: the change in year-on-year moves between the prior "
            "12 months and the last 12 continues for 5 more years, then holds.",
            key=G("job_market_ahead"),
        )
        v.edu_pay = st.checkbox(
            "Pay follows the degree",
            True,
            disabled=not (v.d1 or v.d2),
            help="On: earnings = the country's age profile for all workers x "
            "the full-time pay of that degree relative to all degrees, by "
            "age (e.g. a bachelor's in the US ~1.3x at 25-34). Off: the "
            "all-degree profile (use the income percentile instead).",
            key=G("pay_follows_degree"),
        )
        v.g1 = c1.slider(
            "P1 career growth",
            0.0,
            2.0,
            1.0,
            0.1,
            disabled=bool(v.career_on),
            help="1.0 = follow the country's age-earnings profile (cross-section of "
            "today's workers by age). 0 = flat career; 2 = twice as steep. (Not "
            "used when careers follow today's job market.)",
            key=G("p1_career_growth"),
        )
        v.g2 = c2.slider(
            "P2 career growth",
            0.0,
            2.0,
            1.0,
            0.1,
            disabled=bool(v.career_on),
            key=G("p2_career_growth"),
        )
        ra = v.cd.retirement_age
        v.retire_d = int(round(ra.value)) if ra is not None else 67
        v.retire = st.slider(
            "Retirement age (pay stops, pension starts)",
            50,
            80,
            v.retire_d,
            key=v.K("retirement_age"),
            help=(src(ra, "{:g}") if ra is not None else "No OECD data: 67.")
            + " Today's young workers "
            "retire later than today's retirees where the retirement age is "
            "linked to life expectancy (e.g. Denmark, Estonia, Italy, Sweden).",
        )
        hz = horizon_default(
            Plan(country=v.iso, earners=(Earner(int(v.a1)), Earner(int(v.a2)))), v.cd
        )
        v.horizon_d = max(int(hz.value), int(v.a1) + 5)
        v.horizon = st.slider(
            "Simulate until P1 age",
            int(v.a1) + 5,
            105,
            v.horizon_d,
            key=v.K("horizon_age"),
            help=src(hz, "{:g}") + " The pension is paid from the retirement age, "
            "indexed to prices; savings are drawn down slowly.",
        )
        coh_opts = {
            "history": "As much as gaps have lasted in past surveys",
            "full": "The whole gap lasts",
            "off": "None (today's age brackets)",
        }
        v.coh_mode = st.radio(
            "How far our generation's gap to the one before us lasts",
            list(coh_opts),
            format_func=coh_opts.get,
            key=v.K("generation_gap"),
            help="People of your age now vs people 10 years older when they were your age "
            "(pay relative to all workers; wealth; unemployment). History (US: CPS ASEC "
            "every year since 2003, SCF since 1989) says how much of such a gap is still "
            "there years later; the rest closes as you reach today's older brackets.",
        )
        if v.career_on:
            st.caption(
                f"Careers: {v.CAR.source} ({v.CAR.window}) - situations and pay for your "
                "age and degree."
            )
        else:
            st.caption(
                f"Earnings: {v.cd.earnings_src.label}{flag(v.cd.earnings_src)}. "
                f"{v.cd.earnings_src.note[:160]}"
            )


def _family(v: SimpleNamespace) -> None:
    """Marriage, children, parental leave, childcare."""
    with st.sidebar.expander("💍 Marriage & kids"):
        v.married = st.checkbox("Get married", True, key=G("get_married"))
        v.m_age = st.number_input(
            "Marriage at P1 age",
            18,
            80,
            max(int(v.a1), 28),
            disabled=not v.married,
            key=follow(G("marriage_age"), max(int(v.a1), 28)),
        )
        v.wedding = st.number_input(
            f"Wedding cost ({v.cd.currency})",
            0.0,
            1e7,
            float(round(v.cd.wedding_cost.value, -2)),
            500.0,
            key=v.K("wedding_cost"),
            help=src(v.cd.wedding_cost),
            disabled=not v.married,
        )
        kid_opts = ["none"] + list(range(int(v.a1), 56))
        v.k1 = st.selectbox(
            "Child 1 born at P1 age",
            kid_opts,
            index=kid_opts.index(max(int(v.a1), 30)) if max(int(v.a1), 30) in kid_opts else 0,
            key=follow(
                G("child1_age"), max(int(v.a1), 30) if max(int(v.a1), 30) in kid_opts else "none"
            ),
        )
        v.k2 = st.selectbox(
            "Child 2 born at P1 age",
            kid_opts,
            index=kid_opts.index(max(int(v.a1), 33)) if max(int(v.a1), 33) in kid_opts else 0,
            key=follow(
                G("child2_age"), max(int(v.a1), 33) if max(int(v.a1), 33) in kid_opts else "none"
            ),
        )
        v.hh = v.cd.hh or {}
        v.cc_from = st.slider(
            "Paid childcare from child age",
            0,
            3,
            1,
            help="Usually when paid parental leave ends.",
            key=G("childcare_from_age"),
        )
        st.caption(
            f"Full-time paid care until free pre-school at age "
            f"{v.hh.get('free_childcare_from', 5):g}, then after-school care (20%) to 10; "
            f"school from {v.hh.get('school_age', 6):g}. Child living costs: OECD-modified "
            "equivalence scale (+20% of the couple budget per child, +33% from 14)."
        )
        v.cc_to = None
        v.leave_who = st.radio(
            "Main parental leave taken by",
            ["Partner 1", "Partner 2"],
            index=1,
            horizontal=True,
            key=G("main_leave_taker"),
        )
        lv1 = v.hh.get("leave1_months", 0) + v.hh.get("leave2_months", 0)
        fe = v.cd.family_earn or {}
        pen_lt6 = fe.get(("2", "lt6", "all"))
        v.work_mode = st.radio(
            "Main parent's work while children are young",
            ["Country typical", "Full time"],
            horizontal=True,
            help="Country typical: the main parent's pay follows the employment "
            "rate x usual hours of mothers aged 25-49 relative to women "
            "without children, by the youngest child's age (Eurostat LFS; "
            "US: CPS microdata; Canada: StatCan), until the youngest is 18. "
            "Childcare falls with it. Full time: both keep working full "
            "time (only parental leave).",
            key=G("main_parent_work"),
        )
        if pen_lt6 is not None:
            st.caption(
                f"{v.cd.name}: with 2 children and the youngest under 6, mothers' paid "
                f"work is {min(pen_lt6, 1.0):.0%} of women without children "
                f"({v.cd.family_src.source if v.cd.family_src else ''} "
                f"{v.cd.family_src.year if v.cd.family_src else ''})."
            )
        lh = v.cd.leave_home_age
        v.home_d = int(round(lh.value)) if lh is not None and lh.value == lh.value else 18
        v.home_to = st.number_input(
            "Children live at home (and cost money) until age",
            16,
            35,
            max(v.home_d, 18),
            key=v.K("children_home_until"),
            help=(
                f"Default: average age of leaving the parental home in "
                f"{v.cd.name}, {lh.value:.1f} ({lh.source}, {lh.year})."
                if lh is not None
                else "No data: 18."
            )
            + " Child benefits and tax allowances still end at 18.",
        )
        v.leave_scale = st.slider(
            "Parental leave vs country typical",
            0.0,
            2.0,
            1.0,
            0.1,
            help=f"Typical: {lv1:.1f} months for the main parent, "
            f"{v.hh.get('partner_leave_months', 0):.1f} for the other "
            "(OECD Family Database; national rules).",
            key=G("parental_leave_scale"),
        )
        v.ccare = st.number_input(
            f"Childcare per child in care ({v.cd.currency}/year)",
            0.0,
            1e7,
            float(round(v.cd.childcare_per_child.value)),
            100.0,
            key=v.K("childcare_cost"),
            help=src(v.cd.childcare_per_child),
        )


def _housing(v: SimpleNamespace) -> None:
    """Renting or buying, the home, the mortgage, lending rules, cars, lifestyle."""
    with st.sidebar.expander("🏡 Housing"):
        v.mode = st.radio(
            "Tenure", ["Buy a home", "Rent forever"], horizontal=True, key=G("tenure")
        )
        v.rent = st.number_input(
            f"Rent before buying ({v.cd.currency}/month, excl. utilities)",
            0.0,
            1e6,
            float(round(v.cd.rent.value)),
            25.0,
            key=v.K("rent"),
            help=src(v.cd.rent),
        )
        v.buy_age = st.number_input(
            "Buy at P1 age",
            18,
            90,
            max(int(v.a1), 30),
            disabled=v.mode != "Buy a home",
            key=follow(G("buy_age"), max(int(v.a1), 30)),
        )
        HP_ = resolve(
            Plan(
                country=v.iso,
                earners=(Earner(int(v.a1), education=v.d1), Earner(int(v.a2), education=v.d2)),
                buy_house_age=int(v.buy_age),
            ),
            v.cd,
        )["home_price"]
        v.hp_d = float(round(HP_["value"], -3))
        v.price = st.number_input(
            f"Home price today ({v.cd.currency})",
            0.0,
            1e9,
            v.hp_d,
            5000.0,
            key=follow(v.K("home_price"), v.hp_d),
            help=HP_["source"] + " Prices follow the house-price outlook below until you buy.",
        )
        c1, c2 = st.columns(2)
        v.dp = c1.number_input(
            "Down payment %",
            0.0,
            100.0,
            float(round(v.cd.down_payment_pct.value, 1)),
            0.5,
            key=v.K("down_payment_pct"),
            help=src(v.cd.down_payment_pct, "{:.1f}"),
        )
        v.tt = c2.number_input(
            "Transfer tax %",
            0.0,
            20.0,
            float(v.cd.transfer_tax_pct.value),
            0.25,
            key=v.K("transfer_tax_pct"),
            help=src(v.cd.transfer_tax_pct, "{:.2f}"),
        )
        v.mr = c1.number_input(
            "Mortgage rate %",
            0.0,
            25.0,
            float(round(v.cd.mortgage_rate.value, 2)),
            0.05,
            key=v.K("mortgage_rate"),
            help=src(v.cd.mortgage_rate, "{:.2f}"),
        )
        L = v.cd.lend or {}
        v.term_d = int(L.get("term") or 0) or 25
        v.term = c2.number_input(
            "Term (years)",
            5,
            50,
            v.term_d,
            key=v.K("mortgage_term"),
            help="Country typical maximum. Switzerland: the first mortgage is "
            "interest-only; the part above 2/3 of the price is repaid in 15y.",
        )
        v.owner_d = (
            round(0.7 + (L.get("property_tax_pct") or 0) + (L.get("home_ins_pct") or 0), 2)
            if L
            else 1.0
        )
        v.owner = c1.number_input(
            "Upkeep + property tax + insurance %/yr",
            0.0,
            5.0,
            v.owner_d,
            0.05,
            key=v.K("owner_costs_pct"),
            help=f"Default: 0.7% upkeep + "
            f"{L.get('property_tax_pct', 0):g}% property tax + "
            f"{L.get('home_ins_pct', 0):g}% home insurance.",
        )
        h_opts = ["With incomes", "Recent trend (10 yrs)", "Custom"]
        ht, rt = v.cd.home_trend, v.cd.rent_trend
        v.h_out = st.radio(
            "House prices & rents",
            h_opts,
            index=1 if (ht or rt) else 0,
            horizontal=True,
            key=v.K("home_outlook"),
            help="With incomes: prices and rents grow with real wages (constant "
            "price-to-income, the long-run norm). Recent trend: the last ~20 "
            "years' real growth continues for 10 years, then with incomes"
            + (f" (homes {ht.value:+.1f}%/yr, {ht.year}" if ht else " (no index")
            + (f"; rents {rt.value:+.1f}%/yr)." if rt else ").")
            + " Custom: your own rate for both.",
        )
        appr_d = float(round(v.cd.real_wage_growth_hist.value, 2))
        v.appr = c2.number_input(
            "Real house-price & rent growth %/yr",
            -5.0,
            5.0,
            appr_d,
            0.1,
            key=v.K("home_growth"),
            disabled=v.h_out != "Custom",
            help="Custom only. Default = real wage growth.",
        )
        v.r_out = st.radio(
            "After the fixed period, the mortgage rate is",
            ["Market outlook", "20-year average"],
            horizontal=True,
            help="Market outlook: today's mortgage rate moved by "
            "the change in short rates the market expects (10-year yield - 0.5 term "
            "premium vs today's 3-month rate)"
            + (
                f": {R_m['mortgage_rate_future']:.2f}%"
                if (R_m := v.cd.rates or {}).get("mortgage_rate_future") is not None
                else " (no data here)"
            )
            + ". 20-year average: the mean rate on new mortgages since ~2005"
            + (
                f": {L['rate_longrun']:g}%." if (L := v.cd.lend or {}).get("rate_longrun") else "."
            ),
            key=G("mortgage_rate_after_fixed"),
        )
        v.rules = st.checkbox(
            "Use the country's lending rules",
            True,
            help=str(L.get("source", "")) or "No curated rules: 40% of net income.",
            key=G("country_lending_rules"),
        )
        v.dsti = st.slider(
            "Max mortgage payment, % of net income",
            20,
            60,
            40,
            disabled=v.rules,
            key=G("max_payment_pct"),
        )
        if L:
            st.caption(
                f"Lending: min. down {L['down_pct']:g}%"
                + (
                    f" ({L['down_pct_young']:g}% to age {L['young_max_age']:g})"
                    if L.get("young_max_age") and L["down_pct_young"] != L["down_pct"]
                    else ""
                )
                + (
                    f", payment ≤ {L['dsti_max']:g}% of {L['dsti_basis']} income"
                    if L.get("dsti_max")
                    else ""
                )
                + (f", loan ≤ {L['lti_max']:g}× gross" if L.get("lti_max") else "")
                + f"; rate fixed {L['fixation_years']:g}y."
            )
        cars_d = int(v.hh.get("cars", 1) or 1) if v.hh else 1
        v.cars = st.number_input(
            "Cars owned",
            0,
            3,
            cars_d,
            key=v.K("cars"),
            help="Depreciation, insurance (young-driver surcharge), road tax, "
            "fuel, maintenance, tolls; adults without a car get a "
            "public-transport pass.",
        )
        v.budget_scale = st.slider(
            "Lifestyle vs survey average couple",
            0.5,
            2.0,
            1.0,
            0.05,
            help="Scales the survey budget (food, transport, leisure, ...).",
            key=G("lifestyle_scale"),
        )


def _emergencies(v: SimpleNamespace) -> None:
    """Unexpected costs, unemployment, the emergency fund, a job loss."""
    with st.sidebar.expander("🚨 Emergencies & job loss"):
        v.emerg = st.number_input(
            f"Extra unexpected costs ({v.cd.currency}/year)",
            0.0,
            1e7,
            float(round(v.cd.emergency_expense_size.value)),
            50.0,
            key=v.K("unexpected_costs"),
            help=src(v.cd.emergency_expense_size) + " Stress test: e.g. one such shock per year.",
        )
        v.urisk = st.checkbox(
            "Deduct the average cost of unemployment",
            True,
            help="Each year: unemployment rate for the partner's age band and "
            "degree (headline ILO rate: people who lose all pay) x net pay x "
            "(1 - benefit replacement). See the Context tab.",
            key=G("unemployment_risk"),
        )
        v.efund = st.slider(
            "Emergency fund target (months of spending)",
            0.0,
            24.0,
            6.0,
            0.5,
            key=G("emergency_fund_months"),
        )
        v.protect = st.checkbox(
            "Cut optional spending before using savings",
            False,
            key=G("cut_before_savings"),
            help="In any working year when income doesn't cover the regular bills (a job loss, "
            "a new baby, rent rising faster than pay), cut eating out, recreation, "
            "clothing, furnishings and similar - by up to half - before touching savings. "
            "Off: like most households, keep spending and use savings, and cut only once "
            "they run out (then borrow a little, then cut everything else).",
        )
        v.jl_on = st.checkbox("Simulate a job loss", key=G("job_loss"))
        c1, c2, c3 = st.columns(3)
        v.jl_who = c1.selectbox("Who", [1, 2], disabled=not v.jl_on, key=G("job_loss_partner"))
        v.jl_age = c2.number_input(
            "At P1 age",
            18,
            90,
            int(v.a1) + 10,
            disabled=not v.jl_on,
            key=follow(G("job_loss_age"), int(v.a1) + 10),
        )
        v.jl_m = c3.number_input(
            "Months", 1, 36, 6, disabled=not v.jl_on, key=G("job_loss_months")
        )
        if v.cd.nrr:
            st.caption(
                "Net replacement rates (OECD TaxBEN, couple, latest): "
                + ", ".join(f"{m} mo {s.value:.0f}%" for m, s in sorted(v.cd.nrr.items()))
            )


def _wealth_now(v: SimpleNamespace) -> None:
    """Savings, retirement accounts and a home already owned."""
    # defaults the engine would use for the primary country (rates, portfolio, loans)
    _buy = int(v.buy_age) if v.mode == "Buy a home" else None
    _E = (
        Earner(int(v.a1), v.p1, v.g1, education=v.d1),
        Earner(int(v.a2), v.p2, v.g2, education=v.d2),
    )
    v.D_ = resolve(Plan(country=v.iso, earners=_E, buy_house_age=_buy), v.cd)

    with st.sidebar.expander("💰 What we have now"):
        own_d = bool(v.D_["own_home"]["value"])
        v.own = st.checkbox(
            "We already own our home",
            own_d,
            key=follow(v.K("own_home"), own_d),
            help=v.D_["own_home"]["source"],
        )
        v.DW = resolve(Plan(country=v.iso, earners=_E, buy_house_age=_buy, own_home=v.own), v.cd)

        def _wnum(label, k, step=1000.0):
            d = float(v.DW[k]["value"])
            val = st.number_input(
                label,
                0.0,
                1e9,
                d,
                step,
                key=follow(v.K(k), d),
                help=v.DW[k]["source"] + (f" ({v.DW[k]['note']})" if v.DW[k].get("note") else ""),
            )
            return val, d

        v.savings0, v.sav_d = _wnum(
            f"Savings: bank + investments ({v.cd.currency})", "starting_savings"
        )
        v.ret0, v.ret_d0 = _wnum(f"Retirement accounts ({v.cd.currency})", "starting_retirement")
        if v.own:
            v.hv0, v.hv_d = _wnum(f"Home value ({v.cd.currency})", "home_value_now", 5000.0)
            v.mb0, v.mb_d = _wnum(f"Mortgage left ({v.cd.currency})", "mortgage_now", 5000.0)
            mr0_d = float(round(v.DW["mortgage_rate_now"]["value"], 2))
            my0_d = int(v.DW["mortgage_years_left"]["value"])
            v.mr0 = st.number_input(
                "Rate on our mortgage %",
                0.0,
                20.0,
                mr0_d,
                0.05,
                key=follow(v.K("own_mortgage_rate"), mr0_d),
                help=v.DW["mortgage_rate_now"]["source"],
            )
            v.my0 = st.number_input(
                "Years left on it",
                0,
                40,
                my0_d,
                1,
                key=follow(v.K("own_mortgage_years"), my0_d),
                help=v.DW["mortgage_years_left"]["source"],
            )
        _sw_src = v.DW["starting_savings"].get("source", "")
        st.caption(
            "Defaults: what families of your age "
            + (
                "and degree hold now (Federal Reserve Survey of Consumer Finances, "
                f"{v.DW['starting_savings'].get('year', '')})."
                if v.iso == "USA" and v.DW["starting_savings"].get("year")
                else f"hold (ECB Household Finance and Consumption Survey, "
                f"{v.DW['starting_savings'].get('year', '')}; all households, by age only)."
                if "HFCS" in _sw_src
                else "- no survey of wealth by age is loaded for this country, so enter yours."
            )
        )


def _student_debt(v: SimpleNamespace) -> None:
    """Student debt, its rate and term."""
    with st.sidebar.expander("🎓 Student debt"):
        sd = v.cd.student_debt or {}
        # typical borrower's debt per partner's degree (the engine moves it to today's money;
        # only partners up to 30 are assumed to still owe it)
        v.sl_typ = round(
            sum(sd[e].value for e, a in ((v.d1, v.a1), (v.d2, v.a2)) if e in sd and a <= 30), -2
        )
        v.sloan = st.number_input(
            f"Student debt at the start ({v.cd.currency}, both of us)",
            0.0,
            1e7,
            float(v.sl_typ),
            1000.0,
            key=follow(v.K("student_debt"), float(v.sl_typ)),
            help=(
                "Default: the typical borrower's debt for each partner's "
                "degree - "
                + "; ".join(
                    f"{EDU_SHORT.get(k, k)}: {d.value:,.0f} ({d.source}, {d.year})"
                    for k, d in sd.items()
                )
                + ". Not everyone "
                "borrows: set 0 if you have none."
            )
            if sd
            else "No national figure: tuition is low and loans small or absent.",
        )
        c1, c2 = st.columns(2)
        v.slr_d = float(v.D_["student_loan_rate"]["value"])
        v.sl_rate = c1.number_input(
            "Interest %/yr",
            0.0,
            20.0,
            v.slr_d,
            0.05,
            key=v.K("student_loan_rate"),
            disabled=v.sloan == 0,
            help=v.D_["student_loan_rate"]["source"]
            + (
                f" ({v.D_['student_loan_rate']['note']})"
                if v.D_["student_loan_rate"].get("note")
                else ""
            ),
        )
        v.sl_years = c2.number_input(
            "Repaid over (years)",
            1,
            30,
            10,
            key=v.K("student_loan_years"),
            disabled=v.sloan == 0 or v.iso == "GBR",
            help="Fixed monthly payments (US standard plan: 10 years; "
            "income-driven plans run 20-25). UK: 9% of pay above "
            "GBP 25,000 instead, written off after 40 years.",
        )


def _other_costs(v: SimpleNamespace) -> None:
    """Church tax, private health insurance, how spending follows income."""
    with st.sidebar.expander("🧾 Other costs & options"):
        v.church = st.checkbox(
            "Church members (Germany: +8-9% of income tax)",
            False,
            disabled=v.iso != "DEU",
            key=G("church_tax"),
        )
        v.priv_h = st.checkbox(
            "Private health insurance (Ireland)",
            False,
            disabled=v.iso != "IRL",
            key=G("private_health"),
        )
        v.elast = st.slider(
            "How much spending follows income (below a typical couple's)",
            0.0,
            1.5,
            1.0,
            0.05,
            help="Living costs = survey couple's budget x (your income after housing"
            " / theirs, both growing with average wages)^this. 0 = always "
            "the survey average; 1 = proportional. The defaults (1.0 below a "
            "typical couple's income, 0.4 above it) fit best what couples "
            "actually accumulated in both backtests (US: SCF households, "
            "including the spread within each generation; euro area: HFCS "
            "tables).",
            key=G("spending_elasticity"),
        )
        v.elast_hi = st.slider(
            "... and above a typical couple's income",
            0.0,
            1.5,
            0.4,
            0.05,
            help="Richer households save a larger share of each extra dollar "
            "(Dynan, Skinner & Zeldes 2004): with one elasticity for all "
            "incomes, the top third of earners in the backtest ended up "
            "with too little.",
            key=G("spending_elasticity_high"),
        )
        v.surv_d = 1.0
        v.surv = st.slider(
            "Add back spending that surveys miss",
            0.0,
            1.0,
            v.surv_d,
            0.05,
            key=v.K("survey_correction"),
            help="Household budget surveys record less than national accounts "
            "(e.g. food ~65-80%, clothes, alcohol and eating out about half; "
            "Eurostat budget surveys vs national accounts, US Consumer "
            "Expenditure Survey vs BEA consumption). 1 = scale each category up "
            "to the national-accounts level; 0 = raw survey. Part of the gap is "
            "the very rich, non-profits and the informal economy, but the "
            "default adds back all of it: with it, couples accumulate about what "
            "they really did (backtests on the Fed's SCF and the ECB's HFCS) "
            "and each country saves its national household saving rate.",
        )


def _later_life(v: SimpleNamespace) -> None:
    """Family gifts, inheritance, university, pay penalty, care, retirement."""
    with st.sidebar.expander("👪 Family help & later life"):
        c1, c2 = st.columns(2)
        v.gift = c1.number_input(
            f"Gift from family ({v.cd.currency}, today's money)",
            0.0,
            1e9,
            0.0,
            5000.0,
            key=v.K("family_gift"),
            help="E.g. help with a deposit. Arrives at the age on the right "
            "(default: the planned buying age). Common in Southern and "
            "Eastern Europe, where many young people buy with family help.",
        )
        v.gift_age = c2.number_input(
            "Gift at P1 age (0 = buying age)", 0, 100, 0, key=v.K("gift_age")
        )
        v.inh = c1.number_input(
            f"Inheritance ({v.cd.currency}, after tax)",
            0.0,
            1e9,
            0.0,
            10000.0,
            key=v.K("inheritance"),
        )
        le_now = (
            v.cd.le65.get(("female", max((y for s_, y in v.cd.le65 if s_ == "female"), default=0)))
            if v.cd.le65
            else None
        )
        v.inh_d = int(round(le_now)) - 30 if le_now else 55
        v.inh_age = c2.number_input(
            "Inheritance at P1 age",
            18,
            100,
            max(v.inh_d, int(v.a1)),
            key=v.K("inheritance_age"),
            help="Default: when parents ~30 years older "
            "reach today's life expectancy at 65 (women).",
        )
        uc = v.cd.university_cost
        v.uni = st.slider(
            "Parents pay this share of children's university",
            0,
            100,
            0,
            10,
            help=(f"{src(uc)}. " if uc is not None else "No OECD data. ")
            + "Paid per child for the years below from age 18; living costs while "
            "at home are already counted (children live at home until the leaving "
            "age).",
            key=G("university_share"),
        )
        v.uni_y = st.number_input("University years per child", 1, 8, 4, key=G("university_years"))
        v.wpen = st.slider(
            "Main parent's lasting pay penalty after children (%)",
            0,
            20,
            0,
            help="Hourly pay that never catches up (missed promotions, more family-"
            "friendly jobs) on top of the hours and employment effect above. In "
            "Denmark the long-run earnings gap after children is ~20%, about a "
            "third each from participation, hours and wage rates (Kleven, "
            "Landais & Søgaard 2019) - i.e. ~7%.",
            key=G("child_pay_penalty"),
        )
        c1, c2 = st.columns(2)
        v.ltc_y = c1.number_input(
            "Years of paid long-term care",
            0,
            15,
            0,
            help="For one partner, at the end of the plan.",
            key=G("care_years"),
        )
        v.ltc_d = 74_400 * v.cd.lcu_per_usd.value if v.iso == "USA" else 0.0
        v.ltc_c = c2.number_input(
            f"Care cost per year ({v.cd.currency})",
            0.0,
            1e7,
            float(v.ltc_d),
            1000.0,
            key=v.K("care_cost"),
            disabled=v.ltc_y == 0,
            help="US default: CareScout 2025 median assisted living ($74,400; "
            "private nursing-home room $129,575). Elsewhere public "
            "long-term care covers most of the cost: enter the local "
            "out-of-pocket amount.",
        )
        v.spend_down = (
            st.radio(
                "In retirement we spend",
                ["Pension + savings", "Only the pension"],
                horizontal=True,
                help="Pension + savings: spending = pension + savings / years "
                "left, as US minimum distributions (which most retirees' "
                "withdrawals track). Only the pension: savings are left "
                "untouched (to heirs, or for care) unless the pension can't "
                "cover basic costs.",
                key=G("retirement_spending"),
            )
            == "Pension + savings"
        )


def _returns(v: SimpleNamespace) -> None:
    """Savings mix, returns, inflation, outlooks and how results are shown."""
    with st.sidebar.expander("📈 Returns, growth & display"):
        c1, c2 = st.columns(2)
        R_ = v.cd.rates or {}
        dep_hh, eq_hh = v.D_["deposit_share"]["value"], v.D_["equity_share"]["value"]
        v.hold = st.radio(
            "How we hold our savings",
            ["Like typical households", "Invest everything", "Custom"],
            help="Above the emergency fund. Typical: the split households in "
            f"{v.cd.name} actually hold - {dep_hh:.0f}% in bank deposits; of the "
            f"rest {eq_hh:.0f}% in shares and funds (OECD financial balance "
            "sheets; national totals lean toward wealthier households, so a "
            "typical family holds even more in the bank). Invest everything: no "
            "deposits beyond the emergency fund.",
            key=G("savings_mix"),
        )
        c3, c4 = st.columns(2)
        typical = v.hold.startswith("Like")
        dep_d = int(round(dep_hh)) if typical else 0
        eq_d = int(round(eq_hh)) if typical else 60
        v.dep = c3.slider(
            "% kept in bank deposits",
            0,
            100,
            dep_d,
            5,
            disabled=v.hold != "Custom",
            key=follow(v.K("deposit_pct"), dep_d),
        )
        v.eq = (
            c4.slider(
                "% of investments in shares",
                0,
                100,
                eq_d,
                5,
                disabled=v.hold == "Like typical households",
                key=follow(v.K("equity_pct"), eq_d),
                help="The rest in bonds. Default return = today's 10-year government yield (the "
                "market's price for the next decade) + this share x a 3.5% equity premium - "
                "0.2% fees. 3.5% lies between the world equity premium over bonds since 1900 "
                "(~3%, Dimson-Marsh-Staunton) and current implied estimates (~4%).",
            )
            / 100
        )
        y10 = R_.get("y10")
        v.ret_d = round(y10 + v.eq * 3.5 - 0.2, 2) if y10 is not None else 5.0
        v.ret = c1.number_input(
            "Investments %/yr (nominal, after fees, before tax)",
            -5.0,
            15.0,
            v.ret_d,
            0.25,
            key=follow(v.K("investment_return"), v.ret_d),
            help=(
                f"10-year yield {y10:.2f}% ({R_.get('y10_source')}, {R_.get('y10_period')}). "
                if y10 is not None
                else "No yield data: 5%. "
            )
            + f"Tax drag for {v.cd.name}: "
            f"{(v.cd.hh or {}).get('invest_tax_drag', 0):g} pp/yr (main tax-"
            "advantaged route).",
        )
        v.cash_d = round(float(v.D_["cash_return"]["value"]), 2)
        v.cash_r = c2.number_input(
            "Emergency fund %/yr",
            0.0,
            10.0,
            v.cash_d,
            0.05,
            key=v.K("cash_return"),
            help=v.D_["cash_return"]["source"],
        )
        v.depr_d = round(float(v.D_["deposit_return"]["value"]), 2)
        v.dep_r = c2.number_input(
            "Bank deposits %/yr",
            0.0,
            10.0,
            v.depr_d,
            0.05,
            key=v.K("deposit_return"),
            disabled=v.hold == "Invest everything",
            help=v.D_["deposit_return"]["source"],
        )
        v.infl_d = round(float(v.D_["inflation"]["value"]), 2)
        near_i = v.D_.get("inflation_next_5y")
        v.infl = c1.number_input(
            "Inflation %/yr" + (" (after 5 years)" if near_i else ""),
            -2.0,
            15.0,
            v.infl_d,
            0.1,
            key=v.K("inflation"),
            help=v.D_["inflation"]["source"]
            + (
                f". Next 5 years: {near_i['value']:.2f}% ({near_i['source']})."
                " Changing this sets one rate for all years."
                if near_i
                else ""
            ),
        )
        pt_ = v.cd.price_trends or {}
        v.costs_out = st.radio(
            "Costs ahead",
            ["Each cost's recent trend", "All rise with prices"],
            horizontal=True,
            disabled=not pt_,
            help="Recent trend: each budget line's price trend relative to all "
            "prices over the last 10 years continues for the next 10 "
            "(HICP by category in Europe, CPI components in the US), "
            "then moves with prices and incomes like the rest. Costs that "
            "already rise with wages (childcare, health premiums, cars) "
            "only take the part beyond wage growth."
            + (
                " Here: "
                + ", ".join(
                    f"{k.replace('_', ' ')} {tr[0]:+.1f}%/yr"
                    for k, tr in sorted(pt_.items(), key=lambda x: -x[1][0])[:6]
                )
                + "."
                if pt_
                else ""
            ),
            key=G("costs_outlook"),
        )
        wt = v.cd.wage_trend
        v.w_out = st.radio(
            "Wage growth outlook",
            ["Official projection", "Recent 20-year trend"],
            index=1 if wt is not None else 0,
            horizontal=True,
            disabled=wt is None,
            help="Official projection: long-run productivity growth assumed by the EC "
            "Ageing Report / OBR / CBO / PBO (expert forecasts that include "
            "convergence of poorer countries). Trend: the last ~20 years of "
            "real average-wage growth continue"
            + (f" ({wt.value:.2f}%/yr, {wt.year})." if wt is not None else "."),
            key=v.K("wage_outlook"),
        )
        rwg_src = (
            wt
            if (v.w_out != "Official projection" and wt is not None)
            else v.cd.real_wage_growth_hist
        )
        v.rwg_d = float(rwg_src.value) if rwg_src.value == rwg_src.value else 0.5
        v.rwg = c2.number_input(
            "Real wage growth %/yr (economy-wide)",
            -3.0,
            5.0,
            round(v.rwg_d, 2),
            0.05,
            key=follow(v.K("real_wage_growth"), round(v.rwg_d, 2)),
            help=src(rwg_src, "{:.2f}"),
        )
        v.real = st.toggle("Show in today's money (real)", True, key=G("real_terms"))
        v.defl = st.radio(
            "Today's money means",
            ["cpi", "own"],
            horizontal=True,
            key=v.K("deflator"),
            format_func={"cpi": "Consumer prices (CPI)", "own": "What our own spending costs"}.get,
            disabled=not v.real,
            help="CPI: the national basket. Own: your household's own basket "
            "year by year - your rent at market trends, your mortgage (fixed "
            "payments do not rise), and each budget line at its own price "
            "trend - like the UK's Household Costs Indices.",
        )
        v.cur = st.radio(
            "Currency",
            ["local", "USD", "intl$"],
            horizontal=True,
            format_func={
                "local": f"Local ({v.cd.currency})",
                "USD": "USD",
                "intl$": "Intl $ (PPP)",
            }.get,
            key=G("currency"),
        )


def _us_abroad(v: SimpleNamespace) -> None:
    """US citizens abroad."""
    with st.sidebar.expander("🇺🇸 US citizens abroad"):
        v.us_cit = st.checkbox(
            "We are US citizens (citizenship-based taxation)", True, key=G("us_citizens")
        )
        v.us_emp = st.checkbox(
            "Employed by a US employer / self-employed",
            False,
            help="Adds US FICA where there is no totalization agreement.",
            key=G("us_employer"),
        )
        st.caption(
            (
                "✅ US totalization agreement in force — pay social security in one country only."
                if v.cd.totalization
                else "⚠️ No US totalization agreement."
            )
            + " FEIE vs FTC is chosen each year (whichever is cheaper); electing and "
            "revoking FEIE has a 5-year lock-in in reality. Also: FBAR above $10k, "
            "FATCA Form 8938, and PFIC rules for non-US funds (not modelled)."
        )

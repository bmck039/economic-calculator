"""The parts of one simulated household year: price paths, pay, leave and job loss, living
costs, loans and the mortgage, spending cuts, and the summary of a run."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .assumptions import (
    BUDGET_ELASTICITY_HIGH,
    CH_STYLE,
    CHILD_DISCRETIONARY,
    DISCRETIONARY,
    HEALTH_INS_CATS,
    IRL_PRIVATE_HEALTH,
    LTC_DEFAULT,
    MAX_SQUEEZE,
    OWNER_MAINTENANCE_PCT,
    REFI_COST,
    REFI_GAP,
    SCALE_BOUNDS,
    SUBSIST_CUT,
    SURVEY_CORRECTION,
    VEHICLE_CATS,
)
from .career import STATE_LABEL, career_path
from .inputs import edu_pay_factor, life_expectancy_65
from .model import CountryData, Plan
from .money import annuity, annuity_pv, interp_profile, nan0, nrr_for_month
from .tax import uk_sdlt_first_time


def pension_contribution(iso3: str, gross: float, age: int, hh: dict) -> float:
    """Employee contribution to compulsory occupational pensions (not in OECD SSC)."""
    rate = nan0(hh.get("pension_emp_rate"))
    if rate <= 0 or gross <= 0:
        return 0.0
    if iso3 in CH_STYLE and iso3 == "CHE":
        if gross < 22_680 or age < 25:
            return 0.0
        coord = min(max(min(gross, 90_720) - 26_460, 3_780), 64_260)
        credit = 0.07 if age < 35 else 0.10 if age < 45 else 0.15 if age < 55 else 0.18
        return coord * credit / 2
    return rate * max(gross - nan0(hh.get("pension_franchise")), 0.0)


def leave_schedule(n_years: int, birth_ts: list, hh: dict, plan: Plan) -> list[list[tuple]]:
    """Per earner, per month: (rr, cap, floor) while on paid leave, else None."""
    months = n_years * 12
    sched: list[list] = [[None] * months, [None] * months]
    if not hh:
        return sched
    s = plan.leave_scale
    prim = plan.primary_parent if plan.primary_parent in (0, 1) else 1
    other = 1 - prim
    stages = [
        (
            nan0(hh.get("leave1_months")) * s,
            nan0(hh.get("leave1_rr")),
            nan0(hh.get("leave1_cap")),
            nan0(hh.get("leave1_floor")),
        ),
        (
            nan0(hh.get("leave2_months")) * s,
            nan0(hh.get("leave2_rr")),
            nan0(hh.get("leave2_cap")),
            nan0(hh.get("leave2_floor")),
        ),
    ]
    partner = (
        nan0(hh.get("partner_leave_months")) * s,
        nan0(hh.get("partner_leave_rr")),
        nan0(hh.get("partner_leave_cap")),
        nan0(hh.get("partner_leave_floor")),
    )
    for b in birth_ts:
        if b is None or b < 0:
            continue
        m0 = b * 12 + 6  # births mid-year
        m = m0
        for n, rr, cap, floor in stages:
            for k in range(int(round(n))):
                if 0 <= m + k < months:
                    sched[prim][m + k] = (rr, cap, floor)
            m += int(round(n))
        n, rr, cap, floor = partner
        for k in range(int(round(n))):
            if 0 <= m0 + k < months:
                sched[other][m0 + k] = (rr, cap, floor)
    return sched


def cohort_pay_path(coh, age0: int, edu: str | None, years: int, mode: str):
    """(growth, gap): the cohort's typical pay relative to economy-wide wages, h = 0..
    years-1 (today's age brackets + the gap that lasts; ratio to h = 0), and the lasting
    gap alone as a factor on a cross-sectional profile. None where no cohort data."""
    if coh is None:
        return None
    if coh.get("earn_ftyr", edu) is not None:  # US: CPS ASEC
        # full-time year-round workers: a pay RATE, like the model's (all earners' annual
        # pay also mixes in part-year work - new graduates, people easing into retirement)
        p = coh.path("earn_ftyr", edu, age0, years, mode)
        ok = ~np.isnan(p)
        if not ok.any():
            return None
        p = np.where(ok, p, p[ok][-1])
        gap = np.exp([coh.adjustment("earn_ftyr", edu, age0, h, mode) for h in range(years)])
        return np.exp(p - p[0]), gap
    if coh.get("earn_ses", "all") is not None:  # Europe: SES (age bands)
        gap = np.exp([coh.adjustment("earn_ses", "all", age0, h, mode) for h in range(years)])
        return None, gap
    return None


def price_paths(
    n_years: int,
    infl: float,
    near: float | None,
    g_real: float,
    HS,
    home_tr: float | None,
    rent_tr: float | None,
    g_house: float,
):
    """Inflation by year, the price level, economy-wide real wages and the real paths of
    home prices and rents: the forecast, or (backtests) what actually happened."""
    # prices: the market's expectation for the next 5 years (US breakevens), then the
    # long-run rate
    INF = [near / 100 if near is not None and t < 5 else infl for t in range(n_years + 2)]
    if HS is not None:
        INF = [
            HS.infl[t] if t < HS.n() and HS.infl[t] == HS.infl[t] else INF[t]
            for t in range(len(INF))
        ]
    Pt = [1.0]
    for t in range(n_years + 1):
        Pt.append(Pt[-1] * (1 + INF[t]))
    GW = [1.0]
    for t in range(n_years + 1):
        g_t = (
            HS.wage_real[t]
            if HS is not None and t < HS.n() and HS.wage_real[t] == HS.wage_real[t]
            else g_real
        )
        GW.append(GW[-1] * (1 + g_t))
    Hh, Hr = [1.0], [1.0]
    for t in range(1, n_years + 1):
        gh = home_tr / 100 if home_tr is not None and t <= 10 else g_house
        gr = rent_tr / 100 if rent_tr is not None and t <= 10 else g_house
        if HS is not None and t - 1 < HS.n():
            gh = HS.home_real[t - 1] if HS.home_real[t - 1] == HS.home_real[t - 1] else gh
            gr = HS.rent_real[t - 1] if HS.rent_real[t - 1] == HS.rent_real[t - 1] else gr
        Hh.append(Hh[-1] * (1 + gh))
        Hr.append(Hr[-1] * (1 + gr))
    return INF, Pt, GW, Hh, Hr


def childcare_need(kid_ages: list[int], plan: Plan, hh: dict, childcare0: float) -> float:
    """Paid care for this year's children (start-year money), before fewer working
    parents are taken into account: full-time until free pre-school, then after-school
    care (a fifth) to 10 - or the plan's own ages."""
    free_from = nan0(hh.get("free_childcare_from")) if hh else None
    cc = 0.0
    for k in kid_ages:
        if plan.childcare_to is not None or not hh:
            last = plan.childcare_to if plan.childcare_to is not None else 5
            cc += childcare0 if plan.childcare_from <= k <= last else 0.0
        else:
            if plan.childcare_from <= k < max(free_from, plan.childcare_from):
                cc += childcare0
            elif plan.childcare_from <= k <= 10:
                cc += 0.2 * childcare0  # after-school / part-day care
    return cc


def health_cost(
    hh: dict,
    iso3: str,
    ages,
    kid_ages: list[int],
    dep_to: int,
    have_health: bool,
    have_oop: bool,
    fpl: float,
    earned: float,
    year: int,
    private_health: bool,
    n_dep: int,
) -> float:
    """Health insurance and out-of-pocket costs for the year (2025 money)."""
    hc = 0.0
    for a in ages:
        if have_health:
            young = hh.get("health_premium_young", float("nan"))
            prem = young if (a < 26 and young == young) else hh["health_premium_adult"]
            hc += nan0(prem) * 12
        if have_oop:
            hc += nan0(hh.get("health_oop_adult"))
    kid_hc = 0.0
    for k in kid_ages:
        if k <= dep_to:
            kid_hc += (
                nan0(hh.get("health_premium_child")) * 12 * have_health
                + nan0(hh.get("health_oop_child")) * have_oop
            )
    if iso3 == "USA":
        # means-tested public cover: CHIP for children below 200% of the federal poverty
        # guideline (state limits: median ~250%), Medicaid for adults below 138% (ACA
        # expansion, from 2014; 41 states)
        inc_f = earned / fpl if fpl > 0 else 99.0
        if inc_f < 2.0:
            kid_hc = 0.0
        if inc_f < 1.38 and year >= 2014:
            hc = 0.0
    hc += kid_hc
    if private_health and iso3 == "IRL":
        hc += 2 * IRL_PRIVATE_HEALTH[0] + n_dep * IRL_PRIVATE_HEALTH[1]
    return hc


def student_loan_year(
    iso3: str,
    bal: float,
    principal0: float,
    rate: float,
    years: int,
    gross: list[float],
    P: float,
    t: int,
) -> tuple[float, float]:
    """(payment this year, balance after it). England: 9% of pay above GBP 25,000,
    written off after 40 years; elsewhere a fixed monthly annuity."""
    if iso3 == "GBR":
        pay = sum(0.09 * max(g - 25_000 * P, 0) for g in gross)
        bal *= 1 + rate
        pay = min(pay, bal)
        bal -= pay
    else:
        m_pay, pay = annuity(principal0, rate, years) / 12, 0.0
        for _ in range(12):  # monthly, as the payment is computed
            if bal <= 0.005:
                break
            bal *= 1 + rate / 12
            p_m = min(m_pay, bal)
            pay += p_m
            bal -= p_m
    if iso3 == "GBR" and t >= 40:
        bal = 0.0
    return pay, bal


def loan_limits(
    L: dict,
    plan: Plan,
    iso3: str,
    price: float,
    rate: float,
    term: float,
    dp_min: float,
    gross_hh: float,
    net_hh: float,
    ch_style: bool,
) -> dict:
    """The most a lender would lend under each of the country's rules."""
    limits = {"down payment (LTV)": price * (1 - dp_min)}
    stress = max(rate + nan0(L.get("stress_add")) / 100, nan0(L.get("stress_floor")) / 100)
    dsti = nan0(L.get("dsti_max")) / 100
    other = 0.0
    if iso3 in ("USA", "CAN"):
        other = price * (nan0(L.get("property_tax_pct")) + nan0(L.get("home_ins_pct"))) / 100
    if ch_style:
        # SBA rule: 5% imputed interest + 1% upkeep + amortisation to 2/3 LTV in 15 years
        # <= 1/3 of gross income
        cap = gross_hh / 3 - 0.01 * price
        # loan*0.05 + (loan - 2/3 price)/15 <= cap  (amortisation part if > 2/3)
        lo = cap / 0.05
        if lo > 2 / 3 * price:
            lo = (cap + 2 / 3 * price / 15) / (0.05 + 1 / 15)
        limits["affordability (1/3 of gross at 5%)"] = max(lo, 0)
    elif dsti > 0 and L:
        basis = gross_hh if L.get("dsti_basis") == "gross" else net_hh
        limits[f"payment-to-income ({dsti * 100:.0f}% of {L.get('dsti_basis')})"] = annuity_pv(
            dsti * basis - other, stress, term
        )
    if nan0(L.get("lti_max")) > 0:
        limits[f"loan-to-income ({L['lti_max']:g}x gross)"] = L["lti_max"] * gross_hh
    if plan.max_payment_to_income is not None or not L:
        cap_pct = plan.max_payment_to_income if plan.max_payment_to_income is not None else 40.0
        limits[f"payment-to-income ({cap_pct:g}% of net)"] = annuity_pv(
            cap_pct / 100 * max(net_hh, 0), rate, term
        )
    return limits


def purchase_tax(
    iso3: str, L: dict, inp: dict, user_rate: bool, price: float, P: float, a1: int
) -> float:
    """Transfer tax / stamp duty on buying (first-time-buyer reliefs where they exist)."""
    if user_rate or not L:
        return price * inp["transfer_tax_pct"]["value"] / 100
    if iso3 == "GBR":
        return uk_sdlt_first_time(price / P) * P
    if iso3 == "NLD":
        return 0.0 if a1 < 35 and price / P <= 555_000 else 0.02 * price
    if iso3 == "PRT":
        return 0.0 if a1 <= 35 and price / P <= 330_539 else 0.06 * price
    return price * nan0(L.get("transfer_pct")) / 100


def owner_costs(
    plan: Plan,
    L: dict,
    iso3: str,
    home_value: float,
    mort_bal: float,
    buy_price: float,
    loan0: float,
    interest: float,
    P: float,
) -> dict:
    """An owner's yearly costs beyond the mortgage payment: upkeep, property tax,
    insurance, mortgage insurance, and mortgage-interest relief (negative)."""
    if plan.owner_cost_pct is not None or not L:
        pct = plan.owner_cost_pct if plan.owner_cost_pct is not None else 1.0
        return {"owner_costs": home_value * pct / 100}
    out = {
        "owner_costs": home_value
        * (OWNER_MAINTENANCE_PCT + nan0(L.get("property_tax_pct")) + nan0(L.get("home_ins_pct")))
        / 100
    }
    extra = 0.0
    if iso3 == "USA" and mort_bal > 0.8 * buy_price:
        extra += 0.005 * mort_bal  # PMI
    if nan0(L.get("borrower_ins_pct")) and mort_bal > 0:
        extra += loan0 * L["borrower_ins_pct"] / 100  # FR assurance emprunteur
    if extra:
        out["mortgage_insurance"] = extra
    if iso3 == "NLD":
        relief = 0.3748 * max(interest - 0.0035 * home_value, 0)
    else:
        cap = nan0(L.get("relief_cap"))
        relief = nan0(L.get("relief_rate")) * (min(interest, cap * P) if cap else interest)
    if relief:
        out["mortgage_interest_relief"] = -relief
    return out


def cut_extras(exp: dict, need: float, retired: bool) -> float:
    """A shortfall's first answer: discretionary spending cut by up to MAX_SQUEEZE of it
    (the unrecorded line as discretionary as the recorded basket, children's costs 40%
    discretionary; retirees: the whole basket). Changes `exp`; returns the cut."""
    keys = [k for k in exp if k in DISCRETIONARY or (retired and k.startswith("budget:"))]
    bl = sum(
        v for k, v in exp.items() if k.startswith("budget:") and k != "budget:other_unrecorded"
    )
    o_share = (
        sum(exp[k] for k in keys if k != "budget:other_unrecorded") / bl
        if bl > 0 and not retired
        else 1.0
    )
    oth = exp.get("budget:other_unrecorded", 0.0)
    keys = [k for k in keys if k != "budget:other_unrecorded"]
    pool = (
        sum(exp[k] for k in keys) + CHILD_DISCRETIONARY * exp.get("children", 0.0) + o_share * oth
    )
    cut = min(need, MAX_SQUEEZE * pool)
    if not cut > 0:
        return 0.0
    f = cut / pool
    for k in keys:
        exp[k] *= 1 - f
    if "children" in exp:
        exp["children"] *= 1 - CHILD_DISCRETIONARY * f
    if oth:
        exp["budget:other_unrecorded"] = oth * (1 - o_share * f)
    return cut


def cut_living_costs(exp: dict, need: float) -> float:
    """Beyond what can be borrowed: everything but housing, care, health and loans cut by
    up to SUBSIST_CUT. Changes `exp`; returns the cut."""
    keys = [
        k
        for k in exp
        if k.startswith("budget:") or k in ("children", "cars", "public_transport", "emergencies")
    ]
    pool = sum(exp[k] for k in keys)
    cut = min(need, SUBSIST_CUT * pool)
    if not (cut > 0 and pool > 0):
        return 0.0
    f = cut / pool
    for k in keys:
        exp[k] *= 1 - f
    return cut


def own_cost_index(rows: list[dict]) -> list[float]:
    """The household's own cost of living, chained with last year's spending as weights."""
    ci = [1.0]
    for a, b in zip(rows[:-1], rows[1:]):
        w = {
            k: v
            for k, v in a["_exp"].items()
            if k in a["_pfac"] and k in b["_pfac"] and a["_pfac"][k] > 0
        }
        tot = sum(w.values())
        ci.append(
            ci[-1]
            * (
                sum(v * b["_pfac"][k] / a["_pfac"][k] for k, v in w.items()) / tot
                if tot > 0
                else b["price_factor"] / a["price_factor"]
            )
        )
    return ci


def living_budget(
    cd: CountryData, plan: Plan, start, have_cars: bool, have_health: bool, have_oop: bool
) -> dict:
    """The survey basket per year (start-year money), raised to national-accounts totals,
    without the lines the car and health modules replace."""
    surv_k = plan.survey_correction if plan.survey_correction is not None else SURVEY_CORRECTION
    budget0 = {}
    for k, v in cd.budget.items():
        val = start(v.value, v.year) * 12
        if have_cars and k in VEHICLE_CATS:
            continue
        if have_cars and k in ("transport_all", "cp07_unsplit"):
            val *= 0.2  # no COICOP 07 split (UK, PT, RO): the transport-services part (~20%)
        if (have_health and k in HEALTH_INS_CATS) or (have_oop and k == "health_out_of_pocket"):
            continue
        cov = (cd.budget_coverage or {}).get(k, 1.0)
        budget0[k] = val * plan.budget_scale * max(cov, 1.0) ** surv_k
    return budget0


def mortgage_terms(
    L: dict,
    inp: dict,
    plan: Plan,
    iso3: str,
    a1: int,
    t: int,
    m_rate_now: float,
    rate_lr: float,
    HS,
    dp_user: bool,
    price: float,
    P: float,
) -> tuple[float, float, float, bool]:
    """(minimum down payment share, term, rate, fixed for the whole term) for a home
    bought this year."""
    young = a1 <= nan0(L.get("young_max_age"))
    if dp_user or not L:
        dp_min = inp["down_payment_pct"]["value"] / 100
    else:
        dp_min = (L["down_pct_young"] if young else L["down_pct"]) / 100
    term = (L.get("term_young") if young else L.get("term")) if L else None
    if plan.mortgage_years is not None:
        term = plan.mortgage_years
    elif term is None or term != term:
        term = 25
    rate = new_loan_rate(t, m_rate_now, rate_lr, HS)
    fixed_all = False
    if iso3 == "HUN" and price / P <= 100e6 and a1 < 99:  # Otthon Start
        rate, dp_min, term, fixed_all = 0.03, 0.10, 25, True
    return dp_min, term, rate, fixed_all


def summarise(
    df: pd.DataFrame,
    plan: Plan,
    ret_age: int,
    horizon: int,
    nrr_tab: dict,
    hh: dict,
    warnings: list[str],
) -> dict:
    """Retirement figures (start-year money) and the warnings that need the whole run."""
    sq = df[df["spending_squeeze"] > 0]
    if not sq.empty:
        cut = sq["spending_squeeze"] / (sq["expenses_recurring"] + sq["spending_squeeze"])
        warnings.append(
            f"Money ran short in {len(sq)} year(s) (ages {int(sq['age1'].min())}-"
            f"{int(sq['age1'].max())}): discretionary spending (recreation, eating "
            "out, clothes, furnishings - then, beyond a small overdraft, other "
            f"living costs) was cut by up to {cut.max():.0%} of the budget."
        )
    if (df["liquid"] < 0).any():
        a = int(df.loc[df["liquid"] < 0, "age1"].iloc[0])
        low = df.loc[df["liquid"].idxmin()]
        warnings.append(
            f"Liquid savings go below zero at age {a} (lowest {low['liquid']:,.0f} "
            f"at age {int(low['age1'])}); the gap is financed at {plan.debt_rate}% "
            "debt. Add current savings, delay big events or trim spending."
        )
    ret_rows = df[df["retired"].astype(bool)]
    work = df[(df["gross"] > 0)]
    summary = {
        "retirement_age": ret_age,
        "horizon_age": horizon,
        "pension_nrr": nrr_tab.get(1.0, nan0(hh.get("pension_nrr"))),
    }
    n10 = min(10, len(df) - 1)
    if n10 > 0:  # %/yr over the first (up to) 10 years: own costs vs consumer prices
        summary["own_inflation"] = 100 * (
            (df["cost_index"].iloc[n10] / df["cost_index"].iloc[0]) ** (1 / n10) - 1
        )
        summary["cpi_inflation"] = 100 * (
            (df["price_factor"].iloc[n10] / df["price_factor"].iloc[0]) ** (1 / n10) - 1
        )
        summary["own_inflation_years"] = n10
    if not ret_rows.empty:
        r0 = ret_rows.iloc[0]
        summary["pension_income_real"] = float(r0["pension_income"] / r0["price_factor"])
        summary["net_worth_at_retirement_real"] = float(r0["net_worth"] / r0["price_factor"])
        last_work = work.tail(5)
        if not last_work.empty:
            sp_w = (last_work["expenses_recurring"] / last_work["price_factor"]).mean()
            sp_r = (ret_rows["expenses_recurring"] / ret_rows["price_factor"]).mean()
            summary["retirement_spending_ratio"] = float(sp_r / sp_w) if sp_w else None
            inc_w = (last_work["net_income"] / last_work["price_factor"]).mean()
            summary["pension_vs_last_net"] = (
                float(summary["pension_income_real"] / inc_w) if inc_w else None
            )
        short = ret_rows[(ret_rows["liquid"] < 0) | (ret_rows["old_age_assistance"] > 0)]
        if not short.empty:
            a = int(short["age1"].iloc[0])
            summary["savings_run_out_age"] = a
            n_ass = int((ret_rows["old_age_assistance"] > 0).sum())
            warnings.append(
                f"In retirement, savings run out at age {a}: the pension does "
                "not cover a basic budget"
                + (
                    ", so living costs are cut and the gap is borrowed against the home"
                    if df["owned"].any()
                    else ", so living costs are cut"
                )
                + (
                    f"; in {n_ass} year(s) the state's minimum income for older "
                    "people would have to fill the rest"
                    if n_ass
                    else ""
                )
                + "."
            )
    return summary


def earnings_setup(
    plan: Plan, cd: CountryData, inp: dict, n_years: int, start, warnings: list[str]
):
    """Each earner's pay at the start and how it grows: the country's profile by age (x
    degree), the generation's lasting gap, and (US) careers from today's job market. Adds
    the career and cohort notes to `inp`. Returns (profiles, cohort paths, career paths,
    career data, factors for a stated current pay)."""

    def pay_f(i: int, age: float) -> float:
        e = plan.earners[i]
        return edu_pay_factor(cd, e.education, age) if plan.education_pay else 1.0

    # ---- earnings at the start, per earner (start-year money)
    prof = []
    for e in plan.earners:
        pts = cd.earnings.get(e.percentile) or cd.earnings.get("median")
        if e.percentile not in cd.earnings:
            warnings.append(f"{e.percentile} earnings not available for {cd.name}; using median")
        prof.append(pts)
    earn_year = cd.earnings_src.year

    # ---- cohorts: each partner's cohort vs the people before it at the same age
    coh, cmode = cd.cohort, plan.cohort_effects
    pay_g = [
        cohort_pay_path(coh, e.age, e.education if plan.education_pay else None, n_years, cmode)
        for e in plan.earners
    ]
    for i, e in enumerate(plan.earners):
        if pay_g[i] is None or pay_g[i][0] is None:
            # no full-time series by single age: the country's full-time pay by age
            # (today's brackets) x the generation gap that lasts, where measured
            pts = cd.earnings.get(e.percentile) or cd.earnings.get("median")
            if pts:
                ed = e.education if plan.education_pay else None
                b0 = interp_profile(pts, e.age) * edu_pay_factor(cd, ed, e.age)
                shape = np.array(
                    [
                        interp_profile(pts, e.age + h) * edu_pay_factor(cd, ed, e.age + h) / b0
                        if b0 > 0
                        else 1.0
                        for h in range(n_years)
                    ]
                )
                gap = pay_g[i][1] if pay_g[i] is not None else np.ones(n_years)
                pay_g[i] = (shape * gap, gap)
    # ---- careers from today's job market (US): situation mix, moves, raises
    paths = [None, None]
    cdat = cd.career if plan.career_model else None
    if cdat is not None:
        for i, e in enumerate(plan.earners):
            pn = e.pay_now / start(1.0, cdat.year) if e.pay_now else None
            paths[i] = career_path(
                cdat,
                e.age,
                e.education if plan.education_pay else None,
                n_years,
                percentile=e.percentile,
                job=e.job,
                pay_now=pn,
                window="trend" if plan.job_market == "trend" else "current",
                growth=pay_g[i][0] if pay_g[i] else None,
            )
        for i, pth in enumerate(paths):
            m0 = pth.mix[0]
            inp[f"P{i + 1} situation now"] = {
                "value": round(100 * m0.get(pth.start_state or "FT_grad", 0.0), 1),
                "unit": "%",
                "year": cdat.year,
                "quality": "A",
                "estimated": False,
                "source": f"{cdat.source} {cdat.window}: "
                + (
                    f"{STATE_LABEL[pth.start_state]} (your input)"
                    if pth.start_state
                    else "today's mix for this age and degree - "
                    + ", ".join(f"{STATE_LABEL[k]} {v:.0%}" for k, v in m0.items())
                ),
                "note": f"pay at the {pth.quantile:.0f}th percentile of each situation; "
                "moves between situations as people of this age and degree make them now"
                + (
                    "; the last year's change continues 5 years"
                    if plan.job_market == "trend"
                    else ""
                ),
            }
    for i, e in enumerate(plan.earners):
        if pay_g[i] is None or coh is None:
            continue
        m = "earn_ftyr" if coh.get("earn_ftyr", None) is not None else "earn_ses"
        edu = e.education if plan.education_pay else None
        g10 = coh.gap_now(m, edu, e.age, 10)
        a10 = coh.adjustment(m, edu, e.age, 10, cmode)
        src = coh.source.get(m, "")
        inp[f"P{i + 1} cohort pay gap"] = {
            "value": round(100 * (np.exp(a10) - 1), 1),
            "unit": "%",
            "quality": "B",
            "estimated": True,
            "year": coh.latest_year(m),
            "source": f"{src}: people aged {e.age} now earn {100 * (np.exp(g10) - 1):+.1f}% "
            + ("(full time, year round) " if m == "earn_ftyr" else "")
            + "vs people 10 years older when they were this age (relative to all workers; "
            "last 3 surveys)",
            "note": (
                "in 10 years: this much vs today's people aged "
                f"{e.age + 10}; "
                + (
                    "the whole gap lasts (your choice)"
                    if cmode == "full"
                    else "no gap carried (your choice)"
                    if cmode == "off"
                    else f"{100 * coh.phi(m, 10):.0f}% of such gaps have lasted 10 years "
                    "in past surveys" + (f" ({coh.borrowed[m]})" if m in coh.borrowed else "")
                )
            ),
        }
    # elsewhere: a stated current pay sets the level (the profile keeps its shape)
    pay_k = [1.0, 1.0]
    for i, e in enumerate(plan.earners):
        if paths[i] is None and e.pay_now:
            b0 = start(interp_profile(prof[i], e.age) * pay_f(i, e.age), earn_year)
            pay_k[i] = e.pay_now * 12 / b0 if b0 > 0 else 1.0

    return prof, pay_g, paths, cdat, pay_k


def life_events(plan: Plan, cd: CountryData, start, age0: int, n_years: int):
    """Years (from the start) of a family gift and an inheritance, and the yearly cost of
    university and of long-term care (start-year money) with the year care begins."""
    gift_t = (
        (plan.family_gift_age if plan.family_gift_age is not None else plan.buy_house_age)
        if plan.family_gift
        else None
    )
    gift_t = None if gift_t is None else max(gift_t - age0, 0)
    inh_t = None
    if plan.inheritance:
        if plan.inheritance_age is not None:
            inh_t = plan.inheritance_age - age0
        else:  # when the parents (30 years older) reach today's life expectancy at 65
            le_now = life_expectancy_65(cd, plan.start_year) or 85.0
            inh_t = int(round(le_now)) - 30 - age0
        inh_t = max(inh_t, 0)
    uni0 = (
        start(cd.university_cost.value, cd.university_cost.year)
        if cd.university_cost is not None
        else 0.0
    )
    if plan.ltc_cost is not None:
        ltc0 = plan.ltc_cost
    elif cd.iso3 in LTC_DEFAULT:
        ltc0 = start(
            LTC_DEFAULT[cd.iso3][0] * cd.lcu_per_usd.value
            if cd.currency != "USD"
            else LTC_DEFAULT[cd.iso3][0],
            LTC_DEFAULT[cd.iso3][2],
        )
    else:
        ltc0 = 0.0
    ltc_from = n_years - plan.ltc_years if plan.ltc_years else None
    return gift_t, inh_t, uni0, ltc0, ltc_from


def spending_scale(plan: Plan, y_perm: float, ref0: float, G: float) -> float:
    """The living budget vs the survey couple's: income relative to the survey couple's
    (whose income grows with economy-wide real wages - spending norms rise with average
    incomes, so only income RELATIVE to peers moves the budget) to the budget
    elasticity - in proportion below the survey couple's income, less above it."""
    r_perm = y_perm / (ref0 * G)
    e_hi = (
        plan.budget_elasticity_high
        if plan.budget_elasticity_high is not None
        else BUDGET_ELASTICITY_HIGH
    )
    if e_hi is None:
        e_hi = plan.budget_elasticity
    e_b = plan.budget_elasticity if r_perm <= 1 else e_hi
    return G * (
        min(max(r_perm**e_b, SCALE_BOUNDS[0]), SCALE_BOUNDS[1]) if y_perm > 0 else SCALE_BOUNDS[0]
    )


def new_loan_rate(t: int, m_rate_now: float, rate_lr: float, HS) -> float:
    """What a new mortgage costs t years from now: today's rate moving to the forward rate
    over 5 years - or (backtests) what new loans cost that year."""
    rate = m_rate_now + (rate_lr - m_rate_now) * min(t / 5, 1.0)
    if HS is not None and t < len(HS.years) and HS.mortgage_rate[t] == HS.mortgage_rate[t]:
        rate = HS.mortgage_rate[t] / 100
    return rate


def mortgage_year(
    bal: float,
    rate: float,
    left: float,
    pay: float,
    fix_eff: float,
    age: int,
    *,
    ch_style: bool,
    buy_price: float,
    loan0: float,
    refi: bool,
    new_rate: float,
    rate_lr: float,
):
    """One year of a mortgage: refinancing where loans can be repaid without penalty (US,
    Denmark: when new loans cost REFI_GAP points less, a new 30-year loan at that rate,
    costs added - so equity builds more slowly than the original schedule), the long-run
    rate once the fixed period is over, then twelve monthly payments (Swiss style:
    interest only, the part above 2/3 of the price repaid over 15 years).
    Returns (balance, rate, years left, payment, fixed period, age, interest, principal)."""
    interest = principal = 0.0
    if refi and new_rate < rate - REFI_GAP and left < 28:
        bal *= 1 + REFI_COST
        rate, left = new_rate, 30
        pay = annuity(bal, rate, left)
        fix_eff = age + 99
    if age >= fix_eff and abs(rate - rate_lr) > 1e-9:
        rate = rate_lr  # fixed period over: long-run average rate
        if not ch_style:
            pay = annuity(bal, rate, max(left, 1))
    if ch_style:
        interest = bal * rate
        excess = bal - 2 / 3 * buy_price
        principal = min(max(excess, 0), max(loan0 - 2 / 3 * buy_price, 0) / 15)
        bal -= principal
    else:
        for _ in range(12):
            i_m = bal * rate / 12
            p_m = min(pay / 12 - i_m, bal)
            interest += i_m
            principal += p_m
            bal -= p_m
    return bal, rate, left - 1, pay, fix_eff, age + 1, interest, principal


def leave_months(
    leave: list, t: int, normal: list[float], net_i: list[float], pp: int, fam_k: float, W: float
):
    """Parental leave this year: (share of the year each earner is paid, leave benefits,
    months on leave per earner). Benefits replace a share of the earner's usual net pay
    (the primary parent's before the child penalty), within the country's cap and floor."""
    paid_frac = [1.0, 1.0]
    leave_ben = 0.0
    leave_m = [0, 0]
    for i in (0, 1):
        if normal[i] <= 0:
            continue
        for mm in range(t * 12, t * 12 + 12):
            spec = leave[i][mm] if mm < len(leave[i]) else None
            if spec is None:
                continue
            rr, cap, floor = spec
            base_i = net_i[i] / fam_k if i == pp and fam_k > 0 else net_i[i]
            b = rr * base_i / 12
            if cap > 0:
                b = min(b, cap * W)
            b = max(b, floor * W)
            leave_ben += b
            leave_m[i] += 1
        paid_frac[i] -= leave_m[i] / 12
    return paid_frac, leave_ben, leave_m


def job_loss_months(
    jl, t: int, start_t: int, nrr: dict, normal_net: float, earner_net: float
) -> tuple[int, float]:
    """A simulated job loss: (months out of work this year, benefits). Benefits bring the
    household to the net replacement rate of its usual net income for each month."""
    months_this_year = [
        m for m in range(1, jl.months + 1) if start_t * 12 + m - 1 in range(t * 12, t * 12 + 12)
    ]
    if not months_this_year:
        return 0, 0.0
    Hm, Em = normal_net / 12, earner_net / 12
    return len(months_this_year), sum(
        max(nrr_for_month(nrr, m) * Hm - (Hm - Em), 0) for m in months_this_year
    )

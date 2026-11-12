"""Year-by-year life-plan simulation (no UI code: testable on its own).

Conventions
-----------
* All money is simulated in NOMINAL local currency. Data defaults are in the
  country's data year prices and are first rolled to the start year. Prices grow
  with `inflation`; wages with inflation + real wage growth; a fixed-rate mortgage
  payment stays fixed in nominal terms (so it shrinks in real terms).
* `to_display()` converts the result to real (start-year prices) and/or USD /
  international dollars at start-year exchange rates / PPPs.
* Ages: events are keyed to partner 1's age.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .model import EDUCATION, CountryData, Plan, Sourced, age_band
from .tax import (USParams, household_income_tax, local_rates, uk_sdlt_first_time,
                  us_expat_tax, us_fica, us_resident_tax, youth_tax_factor)

HOUSING_CATS = {"rent", "mortgage_payment", "owner_costs", "mortgage_insurance",
                "mortgage_interest_relief"}


def interp_profile(points: list[tuple[float, float]], age: float) -> float:
    xs = [a for a, _ in points]
    ys = [v for _, v in points]
    return float(np.interp(age, xs, ys))


def nrr_for_month(nrr: dict[int, float], month: int) -> float:
    """Household net replacement rate (fraction) in the m-th month of unemployment,
    from OECD TaxBEN values at 2, 6, 12 and 24 months."""
    for m in sorted(nrr):
        if month <= m:
            return nrr[m] / 100
    return nrr[max(nrr)] / 100 if nrr else 0.0


@dataclass
class Result:
    years: pd.DataFrame                  # one row per simulated year, nominal local
    events: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)   # resolved inputs with provenance
    summary: dict = field(default_factory=dict)  # pension rights etc. (start-year money)


VEHICLE_CATS = {"vehicle_purchase", "fuel_vehicle", "vehicle_maintenance", "vehicle_other",
                "insurance_vehicle", "vehicle_running", "vehicle_all"}
HEALTH_INS_CATS = {"insurance_health", "health_insurance_mandatory"}
CH_STYLE = {"CHE", "LIE"}          # interest-only first mortgage, 2nd tranche amortised
OWNER_MAINTENANCE_PCT = 0.7        # upkeep beyond the minor repairs already in the budget
LTC_DEFAULT = {"USA": (74_400, "CareScout Cost of Care 2025: national median assisted "
                           "living (private nursing-home room: 129,575)", 2025)}
CHURCH_TAX = {"DEU": 0.085}        # share of income tax (8% BY/BW, 9% elsewhere)
YOUNG_DRIVER = ((25, 1.7), (30, 1.25))
MOVING_PCT = 1.0                   # removals + furnishing a bigger home, % of the price
IRL_PRIVATE_HEALTH = (1600.0, 400.0)  # EUR/yr adult, child (VHI/Laya/Irish Life mid plan)
# Budget constraint: a household that would run out of money cuts discretionary spending
# (up to MAX_SQUEEZE of it) before borrowing - liquidity constraints (Deaton 1991, Zeldes
# 1989). Children's costs count 40% discretionary.
DISCRETIONARY = {"budget:recreation", "budget:restaurants_hotels", "budget:clothing",
                 "budget:furnishings_household", "budget:alcohol_tobacco", "budget:personal_misc"}
MAX_SQUEEZE = 0.5
PER_PERSON_CATS = {"groceries", "clothing", "recreation", "restaurants_hotels", "personal_misc",
                   "transport_services", "transport_all", "cp07_unsplit", "education",
                   "health_out_of_pocket", "cp01_unsplit"}
CHILD_DISCRETIONARY = 0.4


PAY_BANDS = ((30, "25-34"), (40, "35-44"), (50, "45-54"), (60, "55-64"))  # band midpoints


def edu_pay_factor(cd: CountryData, education: str | None, age: float) -> float:
    """Full-time pay for `education` relative to all education levels at `age` (OECD EAG
    relative earnings), interpolated between age-band midpoints; 1 if unknown."""
    if not education or not cd.edu_pay:
        return 1.0
    pts = [(m, cd.edu_pay[(education, b)]) for m, b in PAY_BANDS
           if (education, b) in cd.edu_pay]
    if not pts:
        return 1.0
    return float(np.interp(age, [a for a, _ in pts], [f for _, f in pts]))


def unemployment_rate_for(cd: CountryData, age: int, education: str | None):
    """Unemployment rate (%) for the age band and degree, with its Sourced (or None)."""
    s = cd.labour.get(("unemployment", age_band(age), education or "all")) if cd.labour \
        else None
    return (s.value, s) if s is not None else (None, None)


def _nan0(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


def resolve(plan: Plan, cd: CountryData) -> dict:
    """Merge plan overrides with data defaults; returns value + provenance."""
    def pick(override, sourced, unit=""):
        if override is not None:
            return {"value": float(override), "source": "user input", "estimated": False,
                    "year": None, "quality": "", "unit": unit or sourced.unit}
        return {"value": float(sourced.value), "source": sourced.source,
                "estimated": sourced.estimated, "year": sourced.year,
                "quality": sourced.quality, "unit": sourced.unit, "note": sourced.note}

    R = cd.rates or {}
    infl_default = float(R.get("inflation_target", 2.0))
    rw = cd.real_wage_growth_hist.value
    if cd.hh.get("real_wage_growth", float("nan")) == cd.hh.get("real_wage_growth", 0):
        rw_default = float(rw)                       # curated projection
    else:
        rw_default = float(min(max(rw, 0.0), 1.0)) if rw == rw else 0.5
    out = {
        "inflation": pick(plan.inflation, cd.inflation_hist.with_value(
            infl_default, source=f"central-bank inflation target ({infl_default:g}%); "
            f"10-year history {cd.inflation_hist.value:.1f}%/yr", estimated=True), "%"),
        "real_wage_growth": pick(plan.real_wage_growth, (
            cd.wage_trend if plan.wage_outlook == "trend" and cd.wage_trend is not None
            else cd.real_wage_growth_hist.with_value(rw_default, estimated=True)), "%"),
        "wedding_cost": pick(plan.wedding_cost, cd.wedding_cost),
        "home_price": pick(plan.home_price, cd.home_price),
        "down_payment_pct": pick(plan.down_payment_pct, cd.down_payment_pct, "%"),
        "transfer_tax_pct": pick(plan.transfer_tax_pct, cd.transfer_tax_pct, "%"),
        "mortgage_rate": pick(plan.mortgage_rate, cd.mortgage_rate, "%"),
        "rent": pick(plan.rent, cd.rent),
        "emergency_annual_cost": pick(plan.emergency_annual_cost, cd.emergency_expense_size),
    }
    for i, e in enumerate(plan.earners):
        if e.education:
            lbl = f"P{i + 1} degree"
            out[f"{lbl}: pay vs all degrees"] = {
                "value": edu_pay_factor(cd, e.education, e.age) if plan.education_pay
                else 1.0, "unit": "x",
                "source": (cd.edu_pay_src.source if cd.edu_pay_src and plan.education_pay
                           else "not applied (pay = all-degree profile)"),
                "year": cd.edu_pay_src.year if cd.edu_pay_src else None,
                "estimated": bool(cd.edu_pay_src and cd.edu_pay_src.estimated),
                "quality": "B", "note": f"{EDUCATION.get(e.education, e.education)}, at age "
                f"{e.age}; {cd.edu_pay_src.note if cd.edu_pay_src else ''}"}
        v, s = unemployment_rate_for(cd, e.age, e.education)
        if s is not None:
            out[f"P{i + 1} unemployment rate (age {age_band(e.age)}, "
                f"{(e.education or 'all degrees').replace('_', ' ')})"] = {
                "value": v, "unit": "%", "source": s.source, "year": s.year,
                "estimated": s.estimated, "quality": s.quality, "note": s.note}
    g = out["real_wage_growth"]["value"]
    out["home_real_appreciation"] = (
        {"value": float(plan.home_real_appreciation), "source": "user input", "unit": "%"}
        if plan.home_real_appreciation is not None else
        {"value": g, "source": "= real wage growth (house prices and rents keep pace with "
         "incomes; long-run price-to-income ratios are stable)", "unit": "%",
         "estimated": True})
    if plan.home_real_appreciation is None and plan.home_outlook == "trend":
        for key, tr in (("home_trend_10y", cd.home_trend), ("rent_trend_10y", cd.rent_trend)):
            if tr is not None:
                out[key] = {"value": tr.value, "unit": "%", "source": tr.source,
                            "year": tr.year, "estimated": False, "quality": "A",
                            "note": f"{tr.note}; for the next 10 years, then with incomes"}
    # ---- the years ahead
    ra = cd.retirement_age
    out["retirement_age"] = pick(plan.retirement_age, ra if ra is not None else Sourced(
        67.0, "age", "assumption: no OECD data", None, True, "C"), "age")
    out["horizon_age"] = pick(plan.horizon_age, horizon_default(plan, cd), "age")
    # ---- savings: how households hold them, and what each part earns
    PF = cd.portfolio or {}
    pf_src = (f"{PF.get('source', 'OECD')} {PF.get('year', '')}".strip() +
              "; whole-household-sector totals, so weighted toward wealthier savers")
    if plan.equity_share is not None:
        out["equity_share"] = {"value": round(float(plan.equity_share) * 100, 1),
                               "unit": "% of investments", "source": "user input"}
    elif PF.get("equity_share") is not None:
        out["equity_share"] = {
            "value": round(float(PF["equity_share"]) * 100, 1), "unit": "% of investments",
            "source": pf_src, "year": PF.get("year"), "quality": "B",
            "estimated": bool(PF.get("estimated")),
            "note": "listed shares + investment funds (counted as shares) vs bonds + life "
            "insurance (insurers hold mostly bonds)"}
    else:
        out["equity_share"] = {"value": 60.0, "unit": "% of investments",
                               "source": "assumption: no balance-sheet data",
                               "estimated": True, "quality": "C"}
    eq = out["equity_share"]["value"] / 100
    if plan.deposit_share is not None:
        out["deposit_share"] = {"value": float(plan.deposit_share), "unit": "% of savings",
                                "source": "user input"}
    elif PF.get("deposit_share") is not None:
        out["deposit_share"] = {
            "value": round(float(PF["deposit_share"]) * 100, 1),
            "unit": "% of savings above the emergency fund", "source": pf_src,
            "year": PF.get("year"), "quality": "B", "estimated": bool(PF.get("estimated")),
            "note": "bank deposits as a share of households' free financial savings "
            "(deposits, bonds, listed shares, funds, life insurance; not pensions or own "
            "businesses); a typical household holds even more in deposits"}
    else:
        out["deposit_share"] = {"value": 0.0, "unit": "% of savings",
                                "source": "assumption: no balance-sheet data (all invested)",
                                "estimated": True, "quality": "C"}
    y10 = R.get("y10")
    if plan.return_nominal is not None:
        out["return_nominal"] = pick(plan.return_nominal, Sourced(0.0), "%")
    elif y10 is not None:
        v = y10 + eq * plan.equity_premium - plan.fund_fees
        out["return_nominal"] = {
            "value": round(v, 2), "unit": "%", "year": R.get("y10_period"),
            "estimated": True, "quality": "B",
            "source": f"today's 10-year government yield {y10:.2f}% ({R.get('y10_source')}, "
            f"{R.get('y10_period')}) + {eq:.0%} in shares x "
            f"{plan.equity_premium:g}% equity premium - {plan.fund_fees:g}% fees",
            "note": "the yield is the market's price for the next decade; the premium is "
            "between the long-run world equity premium over bonds since 1900 (~3%, "
            "Dimson-Marsh-Staunton) and current implied estimates (~4%)"}
    else:
        out["return_nominal"] = {"value": 5.0, "unit": "%", "source": "assumption: no "
                                 "yield data", "estimated": True, "quality": "C"}
    sr = R.get("short_rate")
    on, term = R.get("deposit_overnight"), R.get("deposit_term")
    if plan.cash_return is not None:
        out["cash_return"] = pick(plan.cash_return, Sourced(0.0), "%")
    elif on is not None:
        out["cash_return"] = {
            "value": round(float(on), 2), "unit": "%", "year": R.get("deposit_overnight_period"),
            "estimated": bool(R.get("deposit_overnight_estimated")), "quality": "B",
            "source": "what banks pay households on overnight / savings deposits now: "
            f"{R.get('deposit_overnight_source')} ({R.get('deposit_overnight_period')})",
            "note": "the emergency fund sits in an instant-access account"}
    else:
        out["cash_return"] = (
            {"value": round(max(sr - 0.5, 0.0), 2), "unit": "%", "estimated": True,
             "year": R.get("short_rate_period"), "quality": "B",
             "source": f"today's 3-month rate {sr:.2f}% ({R.get('short_rate_source')}, "
             f"{R.get('short_rate_period')}) - 0.5 bank margin"} if sr is not None else
            {"value": 2.0, "unit": "%", "source": "assumption", "estimated": True})
    if plan.deposit_return is not None:
        out["deposit_return"] = pick(plan.deposit_return, Sourced(0.0), "%")
    elif on is not None and term is not None:
        w = PF.get("overnight_share")
        w = 0.6 if w is None else float(w)
        out["deposit_return"] = {
            "value": round(w * on + (1 - w) * term, 2), "unit": "%", "quality": "B",
            "year": R.get("deposit_term_period"),
            "estimated": bool(R.get("deposit_term_estimated")),
            "source": f"{w:.0%} overnight at {on:.2f}% + {1 - w:.0%} fixed-term at "
            f"{term:.2f}% ({R.get('deposit_term_source')}); mix of households' deposits "
            "from the OECD balance sheets",
            "note": "banks pass on only part of market rates to savers"}
    else:
        out["deposit_return"] = dict(out["cash_return"])
    # ---- student loans
    us_rate = (6.52, "US federal Direct Loans for undergraduates 2026-27 (May 2026 10-year "
               "Treasury auction 4.47% + 2.05; fixed for the life of the loan)", 2026)
    if plan.student_loan_rate is not None:
        out["student_loan_rate"] = {"value": float(plan.student_loan_rate), "unit": "%",
                                    "source": "user input"}
    elif cd.iso3 == "USA":
        out["student_loan_rate"] = {"value": us_rate[0], "unit": "%", "source": us_rate[1],
                                    "year": us_rate[2], "quality": "A",
                                    "note": "graduate loans 8.07%, PLUS 9.07%"}
    elif cd.iso3 == "GBR":
        out["student_loan_rate"] = {"value": out["inflation"]["value"], "unit": "%",
                                    "source": "England Plan 5 loans (from 2023): RPI "
                                    "inflation (here: the inflation input)", "quality": "A",
                                    "note": "repaid at 9% of pay above GBP 25,000; "
                                    "written off after 40 years"}
    elif cd.iso3 == "CAN":
        out["student_loan_rate"] = {"value": 0.0, "unit": "%", "quality": "B",
                                    "source": "federal Canada Student Loans: interest-free "
                                    "since April 2023", "note": "provincial parts vary; "
                                    "set the rate if yours has one"}
    else:
        out["student_loan_rate"] = {"value": round(float(y10), 2) if y10 is not None else
                                    3.0, "unit": "%", "estimated": True, "quality": "C",
                                    "source": "assumption: state student loans track "
                                    "government borrowing costs (= today's 10-year yield; "
                                    "e.g. NL DUO uses the 5-year bond rate)"}
    out["student_loan_years"] = (
        {"value": int(plan.student_loan_years), "unit": "years", "source": "user input"}
        if plan.student_loan_years is not None else
        {"value": 10, "unit": "years", "source": "US standard repayment plan (10 years)"
         + ("; in the UK repayments are income-based instead" if cd.iso3 == "GBR" else "")})
    fut = R.get("mortgage_rate_future")
    if plan.rate_outlook == "market" and fut is not None:
        out["mortgage_rate_after_fixation"] = {
            "value": float(fut), "unit": "%", "year": R.get("y10_period"), "quality": "B",
            "estimated": True, "source": "today's mortgage rate moved by the change in short "
            "rates the market expects: " + (
                f"{R['mortgage_rate_now']:.2f}% + ({y10:.2f}% 10-year yield - 0.5 term "
                f"premium - {R['short_rate']:.2f}% 3-month rate)"
                if {"mortgage_rate_now", "short_rate"} <= set(R) and y10 is not None
                else "rates.csv")}
    else:
        lr = _nan0((cd.lend or {}).get("rate_longrun"))
        out["mortgage_rate_after_fixation"] = {
            "value": lr or out["mortgage_rate"]["value"], "unit": "%", "estimated": True,
            "source": (cd.lend or {}).get("rate_longrun_source", "lending.csv long-run "
                                          "average") if lr else "= today's rate"}
    return out


def interp_nrr(tab: dict, rel: float) -> float:
    """Net replacement rate (%) at `rel` x the average wage: linear in log(earnings)
    between the OECD's 0.5 / 1 / 2 x AW points, flat outside them."""
    pts = sorted(tab.items())
    if rel <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if rel <= x1:
            w = (np.log(rel) - np.log(x0)) / (np.log(x1) - np.log(x0))
            return y0 + (y1 - y0) * w
    return pts[-1][1]


def life_expectancy_65(cd: CountryData, year: int, sex: str = "female") -> float | None:
    """Age a 65-year-old is expected to reach in `year`, interpolated between today's value
    and the 2065 projection (OECD Pensions at a Glance / UN WPP); flat outside."""
    pts = sorted((y, v) for (s_, y), v in (cd.le65 or {}).items() if s_ == sex)
    if not pts:
        return None
    if year <= pts[0][0]:
        return pts[0][1]
    for (y0, v0), (y1, v1) in zip(pts, pts[1:]):
        if year <= y1:
            return v0 + (v1 - v0) * (year - y0) / (y1 - y0)
    return pts[-1][1]


LAST_SURVIVOR_YEARS = 2.0   # a couple's second death comes ~2-4 years after the longer
#                             single life expectancy (independent lives); 2 is conservative


def horizon_default(plan: Plan, cd: CountryData) -> Sourced:
    """P1 age to plan to: the longer-living partner's projected age at death (women's
    life expectancy at 65 in the year each partner turns 65) + a last-survivor margin."""
    best, how = None, ""
    for e in plan.earners:
        le = life_expectancy_65(cd, plan.start_year + 65 - e.age)
        if le is None:
            continue
        h = le + LAST_SURVIVOR_YEARS + (plan.earners[0].age - e.age)
        if best is None or h > best:
            best = h
            how = (f"women's life expectancy at 65 in {plan.start_year + 65 - e.age} = "
                   f"{le:.1f}")
    if best is None:
        return Sourced(90.0, "age", "assumption: no life-expectancy data", None, True, "C")
    return Sourced(float(min(round(best), 105)), "age", "OECD Pensions at a Glance (UN World "
                   "Population Prospects): projected life expectancy at 65",
                   None, True, "B", f"{how}, + {LAST_SURVIVOR_YEARS:g} years because a couple"
                   " plans for the longer-living partner")


def roll(value: float, from_year, to_year: int, inflation: float,
         cpi: dict | None = None) -> float:
    """Move a money value from its data year to the start year: actual CPI where it is
    known, `inflation` after that."""
    try:
        fy = int(str(from_year)[:4])
    except (TypeError, ValueError):
        return value
    f = 1.0
    if cpi and fy in cpi:
        last = min(max(cpi), to_year)
        if last > fy:
            f = cpi[last] / cpi[fy]
            fy = last
    return value * f * (1 + inflation / 100) ** max(to_year - fy, 0)


def annuity(principal: float, rate: float, years: float) -> float:
    """Yearly payment (rate as a fraction)."""
    if years <= 0:
        return principal * rate
    if rate <= 0:
        return principal / years
    r = rate / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n) * 12


def annuity_pv(payment_year: float, rate: float, years: float) -> float:
    """Loan supported by a yearly payment."""
    if payment_year <= 0:
        return 0.0
    if years <= 0:
        return payment_year / rate if rate > 0 else float("inf")
    if rate <= 0:
        return payment_year * years
    r = rate / 12
    n = years * 12
    return payment_year / 12 * (1 - (1 + r) ** -n) / r


def pension_contribution(iso3: str, gross: float, age: int, hh: dict) -> float:
    """Employee contribution to compulsory occupational pensions (not in OECD SSC)."""
    rate = _nan0(hh.get("pension_emp_rate"))
    if rate <= 0 or gross <= 0:
        return 0.0
    if iso3 in CH_STYLE and iso3 == "CHE":
        if gross < 22_680 or age < 25:
            return 0.0
        coord = min(max(min(gross, 90_720) - 26_460, 3_780), 64_260)
        credit = 0.07 if age < 35 else 0.10 if age < 45 else 0.15 if age < 55 else 0.18
        return coord * credit / 2
    return rate * max(gross - _nan0(hh.get("pension_franchise")), 0.0)


def leave_schedule(n_years: int, birth_ts: list, hh: dict, plan: Plan) -> list[list[tuple]]:
    """Per earner, per month: (rr, cap, floor) while on paid leave, else None."""
    months = n_years * 12
    sched: list[list] = [[None] * months, [None] * months]
    if not hh:
        return sched
    s = plan.leave_scale
    prim = plan.primary_parent if plan.primary_parent in (0, 1) else 1
    other = 1 - prim
    stages = [(_nan0(hh.get("leave1_months")) * s, _nan0(hh.get("leave1_rr")),
               _nan0(hh.get("leave1_cap")), _nan0(hh.get("leave1_floor"))),
              (_nan0(hh.get("leave2_months")) * s, _nan0(hh.get("leave2_rr")),
               _nan0(hh.get("leave2_cap")), _nan0(hh.get("leave2_floor")))]
    partner = (_nan0(hh.get("partner_leave_months")) * s, _nan0(hh.get("partner_leave_rr")),
               _nan0(hh.get("partner_leave_cap")), _nan0(hh.get("partner_leave_floor")))
    for b in birth_ts:
        if b is None or b < 0:
            continue
        m0 = b * 12 + 6          # births mid-year
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


def simulate(plan: Plan, cd: CountryData, us: USParams | None = None) -> Result:
    inp = resolve(plan, cd)
    infl = inp["inflation"]["value"] / 100
    g_real = inp["real_wage_growth"]["value"] / 100
    g_house = inp["home_real_appreciation"]["value"] / 100
    hh, L = cd.hh or {}, cd.lend or {}
    drag = _nan0(hh.get("invest_tax_drag"))
    r_inv = (inp["return_nominal"]["value"] - drag) / 100
    r_cash, r_debt = inp["cash_return"]["value"] / 100, plan.debt_rate / 100
    r_dep = inp["deposit_return"]["value"] / 100
    dep_share = min(max(inp["deposit_share"]["value"] / 100, 0.0), 1.0)
    eq_share = inp["equity_share"]["value"] / 100
    sl_rate = inp["student_loan_rate"]["value"] / 100
    sl_years = max(int(inp["student_loan_years"]["value"]), 1)
    ret_age = int(round(inp["retirement_age"]["value"]))
    horizon = int(round(inp["horizon_age"]["value"]))
    home_tr = inp.get("home_trend_10y", {}).get("value")
    rent_tr = inp.get("rent_trend_10y", {}).get("value", home_tr)
    y0 = plan.start_year
    dy = cd.data_year
    cpi = cd.cpi or None

    def start(v, year=None):  # data-year money -> start-year money
        return roll(v, year if year is not None else dy, y0, infl * 100, cpi)

    e1, e2 = plan.earners
    n_years = max(horizon - e1.age + 1, 1)
    warnings: list[str] = []
    events: list[dict] = []
    us_resident = cd.iso3 == "USA" and us is not None

    # ---- earnings at the start, per earner (start-year money)
    prof = []
    for e in plan.earners:
        pts = cd.earnings.get(e.percentile) or cd.earnings.get("median")
        if e.percentile not in cd.earnings:
            warnings.append(f"{e.percentile} earnings not available for {cd.name}; using median")
        prof.append(pts)
    earn_year = cd.earnings_src.year

    def pay_f(i: int, age: float) -> float:
        e = plan.earners[i]
        return edu_pay_factor(cd, e.education, age) if plan.education_pay else 1.0

    def gross_at(i: int, age: int, t: int) -> float:
        e = plan.earners[i]
        if not e.works or age >= ret_age or age < 18:
            return 0.0
        base0 = interp_profile(prof[i], e.age) * pay_f(i, e.age)
        shape = interp_profile(prof[i], age) * pay_f(i, age) / base0 if base0 else 1.0
        rel = shape ** e.career_growth if shape > 0 else 0.0
        pen_w = 1.0
        if i == plan.primary_parent and plan.child_wage_penalty and first_birth is not None \
                and t >= first_birth:
            pen_w = 1 - plan.child_wage_penalty / 100    # lasting hourly-pay penalty
        return start(base0, earn_year) * rel * pen_w * ((1 + g_real) * (1 + infl)) ** t

    first_birth = min((a - e1.age for a in plan.child_ages if a is not None), default=None)
    aw0 = start(cd.avg_wage.value, cd.avg_wage.year)
    # ---- living budget: survey lines minus what the car and health modules replace
    have_cars = bool(hh) and _nan0(hh.get("car_price")) > 0
    have_health = bool(hh) and _nan0(hh.get("health_premium_adult")) > 0
    have_oop = bool(hh) and hh.get("health_oop_adult", float("nan")) == \
        hh.get("health_oop_adult", 0)
    budget0, removed0 = {}, {}
    for k, v in cd.budget.items():
        val = start(v.value, v.year) * 12
        if have_cars and k in VEHICLE_CATS:
            removed0[k] = val
            continue
        if have_cars and k in ("transport_all", "cp07_unsplit"):
            # no COICOP 07 split (UK, PT, RO): keep the transport-services part (~20%)
            removed0[k] = 0.8 * val
            val *= 0.2
        if have_health and k in HEALTH_INS_CATS:
            removed0[k] = val
            continue
        if have_oop and k == "health_out_of_pocket":
            removed0[k] = val
            continue
        cov = (cd.budget_coverage or {}).get(k, 1.0)
        budget0[k] = val * plan.budget_scale * max(cov, 1.0) ** plan.survey_correction
    ref = cd.budget_ref_income
    ref0 = None
    bp = next(iter(cd.budget.values())).year if cd.budget else dy
    if ref is not None:   # ref.value is already in the budget's price year
        ref0 = start(ref.value, bp)
        if cd.budget_ref_housing is not None:   # income left after housing costs
            ref0 -= start(cd.budget_ref_housing.value, bp) * 12
    rent0 = start(inp["rent"]["value"], inp["rent"]["year"]) * 12
    child_spend0 = start(cd.child_extra_spending.value, cd.child_extra_spending.year) * 12
    childcare0 = start(cd.childcare_per_child.value, cd.childcare_per_child.year)
    child_ben0 = 0.0 if us_resident else \
        start(cd.child_benefit_per_child.value, cd.child_benefit_per_child.year)
    emerg0 = start(inp["emergency_annual_cost"]["value"], inp["emergency_annual_cost"]["year"])
    wedding0 = start(inp["wedding_cost"]["value"], inp["wedding_cost"]["year"])
    price0 = start(inp["home_price"]["value"], inp["home_price"]["year"])
    hp25 = lambda v: start(_nan0(v), 2025)  # noqa: E731  (curated params: 2025 money)
    # ---- cars (per car, per year, start-year money)
    n_cars = plan.cars if plan.cars is not None else int(_nan0(hh.get("cars")) or 1)
    car_fixed0 = car_ins0 = transit0 = 0.0
    if have_cars:
        car_fixed0 = hp25(0.10 * _nan0(hh["car_price"]) + _nan0(hh["car_tax"]) +
                          _nan0(hh["km_year"]) * _nan0(hh["l_per_100km"]) / 100 *
                          _nan0(hh["fuel_price"]) + _nan0(hh["car_maintenance"]) +
                          _nan0(hh["parking_tolls"]))
        car_ins0 = hp25(hh["car_insurance"])
        transit0 = hp25(hh["transit_pass"]) * 12
    leave = leave_schedule(n_years, [None if a is None else a - e1.age for a in plan.child_ages],
                           hh, plan)
    nrr = {m: s.value for m, s in cd.nrr.items()}
    nrr6 = nrr_for_month(nrr, 6) if nrr else 0.6
    fx = cd.lcu_per_usd.value
    abroad = plan.us_citizens and cd.iso3 != "USA" and us is not None
    st = cd.state_tax

    birth_t = [None if a is None else a - e1.age for a in plan.child_ages]
    marry_t = None if plan.marriage_age is None else plan.marriage_age - e1.age
    married_from_start = plan.marriage_age is not None and plan.marriage_age <= e1.age
    buy_t_plan = None if plan.buy_house_age is None else plan.buy_house_age - e1.age
    # ---- lending rules (fallback: plan defaults when the country has no curated rules)
    m_rate_now = inp["mortgage_rate"]["value"] / 100
    rate_lr = inp["mortgage_rate_after_fixation"]["value"] / 100 or m_rate_now
    fix_years = _nan0(L.get("fixation_years")) or (plan.mortgage_years or 25)
    tt_user = plan.transfer_tax_pct is not None
    dp_user = plan.down_payment_pct is not None

    def unemp_rate(age: int, education: str | None = None) -> float:
        v, _ = unemployment_rate_for(cd, age, education)
        if v is not None:   # by age band and degree (labour_market.csv)
            return v
        y, tot = _nan0(cd.unemp_youth), _nan0(cd.unemp_total)
        if age < 25:
            return y or tot
        if age < 30:
            return (y + tot) / 2 if y else tot
        return tot

    # children live at home (and cost money) until the country's average leaving age
    if plan.child_dependent_to is not None:
        dep_to = plan.child_dependent_to
    elif cd.leave_home_age is not None and cd.leave_home_age.value == cd.leave_home_age.value:
        dep_to = max(int(round(cd.leave_home_age.value)), 18)
    else:
        dep_to = 18
    pp = plan.primary_parent

    def family_factor(table: dict, kid_ages_now: list[int]) -> float:
        """Primary parent's typical work vs no children, by number of children and the
        youngest's age (1 once the youngest is 18)."""
        if not plan.child_penalty or not table or not kid_ages_now:
            return 1.0
        y = min(kid_ages_now)
        if y >= 18:
            return 1.0
        band = "lt6" if y < 6 else "6-11" if y < 12 else "ge12"
        n = str(min(len(kid_ages_now), 3))
        edu = plan.earners[pp].education
        e = {"short_tertiary": "tertiary", "bachelor": "tertiary",
             "master_plus": "tertiary"}.get(edu or "all", edu or "all")
        for key in ((n, band, e), (n, band, "all"), ("2", band, "all"), ("1", band, "all"),
                    (n, "6-11" if band == "ge12" else band, "all")):
            if key in table:
                return min(table[key], 1.0)
        return 1.0

    Hh, Hr = [1.0], [1.0]
    for t in range(1, n_years + 1):
        gh = home_tr / 100 if home_tr is not None and t <= 10 else g_house
        gr = rent_tr / 100 if rent_tr is not None and t <= 10 else g_house
        Hh.append(Hh[-1] * (1 + gh))
        Hr.append(Hr[-1] * (1 + gr))
    cash = plan.starting_savings
    invest = retire = 0.0
    owned = False
    home_value = mort_bal = mort_pay = 0.0
    mort_rate = m_rate_now
    mort_left = 0.0
    mort_age = 0
    loan0 = buy_price = 0.0
    ch_style = False
    fix_years_eff = fix_years
    sl0 = plan.student_loan
    if plan.typical_student_debt and not sl0:
        for e in plan.earners:
            d = (cd.student_debt or {}).get(e.education or "")
            if d is not None and e.age <= 30:
                sl0 += start(d.value, d.year)
    sl_bal = sl0
    # ---- optional life factors
    gift_t = (plan.family_gift_age if plan.family_gift_age is not None else
              plan.buy_house_age) if plan.family_gift else None
    gift_t = None if gift_t is None else max(gift_t - e1.age, 0)
    inh_t = None
    if plan.inheritance:
        if plan.inheritance_age is not None:
            inh_t = plan.inheritance_age - e1.age
        else:
            le_now = life_expectancy_65(cd, plan.start_year) or 85.0
            inh_t = int(round(le_now)) - 30 - e1.age
        inh_t = max(inh_t, 0)
    uni0 = start(cd.university_cost.value, cd.university_cost.year) \
        if cd.university_cost is not None else 0.0
    if plan.ltc_cost is not None:
        ltc0 = plan.ltc_cost
    elif cd.iso3 in LTC_DEFAULT:
        ltc0 = start(LTC_DEFAULT[cd.iso3][0] * cd.lcu_per_usd.value
                     if cd.currency != "USD" else LTC_DEFAULT[cd.iso3][0],
                     LTC_DEFAULT[cd.iso3][2])
    else:
        ltc0 = 0.0
    ltc_from = n_years - plan.ltc_years if plan.ltc_years else None
    # ---- retirement: career record for the pension, then price-indexed payments
    nrr_tab = dict(cd.pension_nrr or {})
    if not nrr_tab and _nan0(hh.get("pension_nrr")):
        nrr_tab = {1.0: _nan0(hh.get("pension_nrr"))}
    rel_sum, rel_n, nr_sum = [0.0, 0.0], [0, 0], [0.0, 0.0]
    pension0 = [None, None]     # (nominal payment in its first year, price level then)
    jl = plan.job_loss
    prev_recurring = 0.0
    prev_housing = 0.0
    tgt_prev = 0.0

    def grow_cash(c: float) -> float:
        """One year's interest on cash: the emergency fund earns the overnight rate,
        deposits above it the household deposit rate; overdrafts cost the debt rate."""
        if c < 0:
            return c * (1 + r_debt_now)
        b = min(c, tgt_prev)
        return c + b * r_cash + (c - b) * r_dep

    r_debt_now = r_debt
    rows = []
    last_normal_net = 0.0
    for t in range(n_years):
        a1, a2 = e1.age + t, e2.age + t
        ages = (a1, a2)
        P = (1 + infl) ** t
        W = ((1 + g_real) * (1 + infl)) ** t
        # glide path: the share in shares falls over the 15 years before retirement to
        # half of it (as target-date funds and most retirees' portfolios)
        if plan.glide_path:
            k = min(max((a1 - (ret_age - 15)) / 15, 0.0), 1.0)
            r_inv_t = r_inv - 0.5 * k * eq_share * plan.equity_premium / 100
        else:
            r_inv_t = r_inv
        H = P * Hr[t]                 # rents
        HH = P * Hh[t]                # home prices
        aw = aw0 * W
        married = married_from_start or (marry_t is not None and t >= marry_t)
        kid_ages = [t - b for b in birth_t if b is not None and t >= b]
        n_kids = len(kid_ages)
        n_dep = sum(1 for k in kid_ages if k <= 18)           # benefits and taxes
        n_home = sum(1 for k in kid_ages if k <= dep_to)      # living at home (costs)
        n_ctc = sum(1 for k in kid_ages if k < 17)
        transfers = 0.0
        if gift_t is not None and t == gift_t:
            transfers += plan.family_gift * P
            cash += plan.family_gift * P
            events.append({"t": t, "age": a1, "event": "Family gift"})
        if inh_t is not None and t == inh_t:
            transfers += plan.inheritance * P
            invest += plan.inheritance * P
            events.append({"t": t, "age": a1, "event": "Inheritance"})

        normal = [gross_at(0, a1, t), gross_at(1, a2, t)]
        fam_k = family_factor(cd.family_earn, kid_ages)
        normal[pp] *= fam_k

        # ---- normal (no leave / job loss) taxes and net, per earner and household
        def taxes(gross: list[float], interest: float = 0.0, prop_tax: float = 0.0):
            if us_resident:
                p_us = us.indexed((1 + infl) ** (y0 - 2026 + t))
                r = us_resident_tax([g / fx for g in gross], married, n_ctc, n_dep, p_us, st,
                                    interest / fx, prop_tax / fx)
                tot = sum(gross) or 1.0
                return ([r.income_tax * fx * g / tot for g in gross],
                        [r.ssc * fx * g / tot for g in gross])
            it, ssc = zip(*[local_rates(g, aw, cd.tax_points, cd.marginal_rate_top)
                            for g in gross])
            it = household_income_tax(cd.iso3, list(gross), married, n_dep, aw, cd.tax_points,
                                      cd.marginal_rate_top)
            it = [x * youth_tax_factor(cd.iso3, a, g, aw, max(a - 23, 0),
                                       i == plan.primary_parent and n_kids >= 2)
                  for i, (x, a, g) in enumerate(zip(it, ages, gross))]
            if plan.church_tax and cd.iso3 in CHURCH_TAX:
                it = [x * (1 + CHURCH_TAX[cd.iso3]) for x in it]
            return list(it), list(ssc)

        # mortgage interest this year (owned from an earlier year) for itemising (US)
        it_n, ssc_n = taxes(normal)
        pen_n = [pension_contribution(cd.iso3, g, a, hh) for g, a in zip(normal, ages)]
        net_i = [g - i - s - p for g, i, s, p in zip(normal, it_n, ssc_n, pen_n)]
        child_ben = child_ben0 * P * n_dep * ((1 + g_real) ** t)
        # ---- pensions: career-average earnings relative to the average wage (incl. the
        # child penalty) x the OECD net replacement rate at that level, from the
        # retirement age; then indexed to prices
        pen_inc = [0.0, 0.0]
        for i in (0, 1):
            if normal[i] > 0:
                rel_sum[i] += normal[i] / aw
                rel_n[i] += 1
                nr_sum[i] += net_i[i] / normal[i]
            elif ages[i] >= ret_age and rel_n[i] and nrr_tab:
                if pension0[i] is None:
                    rel = rel_sum[i] / rel_n[i]
                    rate_p = interp_nrr(nrr_tab, rel) / 100
                    pension0[i] = (rate_p * rel * aw * nr_sum[i] / rel_n[i], P)
                    events.append({"t": t, "age": a1, "event": f"Partner {i + 1} retires"})
                pen_inc[i] = pension0[i][0] * P / pension0[i][1]
        pension_income = sum(pen_inc)
        normal_net = sum(net_i) + child_ben + pension_income
        if sum(normal):
            last_normal_net = sum(net_i) / P

        # ---- parental leave and job loss: months without pay, replaced by benefits
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
        unemp_months = 0
        jl_ben = 0.0
        if jl is not None and normal[jl.earner] > 0:
            start_t = jl.age - e1.age
            months_this_year = [m for m in range(1, jl.months + 1)
                                if start_t * 12 + m - 1 in range(t * 12, t * 12 + 12)]
            if months_this_year:
                unemp_months = len(months_this_year)
                Hm, Em = normal_net / 12, net_i[jl.earner] / 12
                jl_ben = sum(max(nrr_for_month(nrr, m) * Hm - (Hm - Em), 0)
                             for m in months_this_year)
                if t == start_t:
                    events.append({"t": t, "age": a1, "event": f"Job loss (partner "
                                   f"{jl.earner + 1}, {jl.months} mo)"})
        gross = [g * f for g, f in zip(normal, paid_frac)]
        interest_prev = rows[-1]["mortgage_interest"] if rows and owned else 0.0
        ptax_prev = home_value * _nan0(L.get("property_tax_pct")) / 100 if owned else 0.0
        it, ssc = taxes(gross, interest_prev, ptax_prev)
        pen = [pension_contribution(cd.iso3, g, a, hh) for g, a in zip(gross, ages)]
        if unemp_months:  # the earner's pay, taxes and contributions stop for those months
            j, keep = jl.earner, max(paid_frac[jl.earner] - unemp_months / 12, 0)
            f = keep / paid_frac[j] if paid_frac[j] else 0.0
            gross[j] *= f
            it[j] *= f
            ssc[j] *= f
            pen[j] *= f
            paid_frac[j] = keep
        # expected cost of unemployment (risk-weighted, outside a simulated job loss)
        unemp_cost = 0.0
        if plan.unemployment_risk:
            for i in (0, 1):
                if gross[i] > 0 and not (jl is not None and i == jl.earner and unemp_months):
                    unemp_cost += unemp_rate(ages[i], plan.earners[i].education) / 100 * net_i[i] * paid_frac[i] * \
                        (1 - nrr6)

        # ---- US citizens abroad
        us_tax = 0.0
        us_method = ""
        if abroad:
            f_us = (1 + infl) ** (y0 - 2026 + t)
            p_us = us.indexed(f_us)
            housing_usd = (rent0 * H / fx) if not owned else 0.0
            res = us_expat_tax([g / fx for g in gross], [x / fx for x in it], married,
                               n_ctc, housing_usd, p_us)
            us_tax = res.residual * fx
            us_method = res.method
            if plan.us_employer and not cd.totalization:
                us_tax += sum(us_fica(g / fx, p_us) for g in gross) * fx
        net = sum(gross) - sum(it) - sum(ssc) - sum(pen) + child_ben + leave_ben + jl_ben \
            - unemp_cost - us_tax + pension_income
        retired = sum(normal) == 0 and min(ages) >= ret_age

        # ---- expenses (nominal)
        G = (1 + g_real) ** t      # living standards rise with economy-wide real wages
        scale = G
        if ref0 and plan.budget_elasticity:
            if owned:
                housing_now = prev_housing
            else:
                housing_now = rent0 * H * (cd.rent_factor_per_child[min(n_home, 2) - 1]
                                           if n_home else 1.0)
            y_perm = (normal_net - child_ben - (housing_now if cd.budget_ref_housing
                                                is not None else 0.0)) / P
            if retired and plan.spend_down:
                # retirees also live off their savings, but draw them down slowly: about
                # 1 / years left, as US minimum distributions (which most retirees'
                # withdrawals track: Poterba, Venti & Wise) - returns above that build up
                fin = max(cash + invest + retire, 0.0) / P
                y_perm += fin / max(n_years - t, 1)
            # the survey couple's income grows with economy-wide real wages too: spending
            # norms rise with average incomes (saving rates do not trend up as countries
            # grow), so only income RELATIVE to peers moves the budget
            scale = G * (min(max((y_perm / (ref0 * G)) ** plan.budget_elasticity, 0.5), 2.0)
                         if y_perm > 0 else 0.5)
        exp = {f"budget:{k}": v * P * scale for k, v in budget0.items()}
        # children: OECD-modified equivalence scale (couple 1.5; child <14 +0.3, 14+ +0.5)
        # applied to the couple's non-housing living budget; housing via the rent factor,
        # childcare, health and cars separately
        if hh:
            # OECD-modified scale on PER-PERSON spending only (food, clothes, recreation,
            # ...): energy, internet, furnishings, insurance or council tax barely change
            # with children, and children do not drink or smoke
            w = sum(0.3 / 1.5 if k < 14 else 0.5 / 1.5 for k in kid_ages
                    if k <= dep_to)
            exp["children"] = sum(v for k, v in budget0.items() if k in PER_PERSON_CATS) \
                * P * scale * w
        else:
            exp["children"] = child_spend0 * P * n_home * scale
        free_from = _nan0(hh.get("free_childcare_from")) if hh else None
        cc = 0.0
        for k in kid_ages:
            if plan.childcare_to is not None or not hh:
                last = plan.childcare_to if plan.childcare_to is not None else 5
                cc += childcare0 if plan.childcare_from <= k <= last else 0.0
            else:
                if plan.childcare_from <= k < max(free_from, plan.childcare_from):
                    cc += childcare0
                elif plan.childcare_from <= k <= 10:
                    cc += 0.2 * childcare0          # after-school / part-day care
        # fewer working parents -> less paid care (expected value)
        cc *= family_factor(cd.family_emp, kid_ages)
        exp["childcare"] = cc * P * G           # labour-intensive: rises with wages
        if plan.university_share and uni0:
            n_uni = sum(1 for k in kid_ages if plan.university_from <= k <
                        plan.university_from + plan.university_years)
            if n_uni:
                exp["university"] = plan.university_share / 100 * uni0 * n_uni * P * G
        if ltc_from is not None and t >= ltc_from and ltc0:
            exp["long_term_care"] = ltc0 * P * G
        # health insurance and out-of-pocket costs
        if have_health or have_oop:
            hc = 0.0
            for a in ages:
                if have_health:
                    young = hh.get("health_premium_young", float("nan"))
                    prem = young if (a < 26 and young == young) else hh["health_premium_adult"]
                    hc += _nan0(prem) * 12
                if have_oop:
                    hc += _nan0(hh.get("health_oop_adult"))
            for k in kid_ages:
                if k <= dep_to:
                    hc += _nan0(hh.get("health_premium_child")) * 12 * have_health + \
                        _nan0(hh.get("health_oop_child")) * have_oop
            if plan.private_health and cd.iso3 == "IRL":
                hc += 2 * IRL_PRIVATE_HEALTH[0] + n_dep * IRL_PRIVATE_HEALTH[1]
            exp["health"] = start(hc, 2025) * P * G
        # cars and public transport
        if have_cars:
            car = 0.0
            for k in range(n_cars):
                age = ages[min(k, 1)]
                f = next((m for lim, m in YOUNG_DRIVER if age < lim), 1.0)
                car += car_fixed0 + car_ins0 * f
            exp["cars"] = car * P * G
            exp["public_transport"] = max(2 - n_cars, 0) * transit0 * P * G
        exp["emergencies"] = emerg0 * P * G
        # student loan
        if sl_bal > 0.5:
            if cd.iso3 == "GBR":
                pay = sum(0.09 * max(g - 25_000 * P, 0) for g in gross)
                sl_bal *= 1 + sl_rate
                pay = min(pay, sl_bal)
                sl_bal -= pay
            else:
                m_pay, pay = annuity(sl0, sl_rate, sl_years) / 12, 0.0
                for _ in range(12):            # monthly, as the payment is computed
                    if sl_bal <= 0.005:
                        break
                    sl_bal *= 1 + sl_rate / 12
                    p_m = min(m_pay, sl_bal)
                    pay += p_m
                    sl_bal -= p_m
            if cd.iso3 == "GBR" and t >= 40:
                sl_bal = 0.0
            exp["student_loan"] = pay
        one_off = {}
        if marry_t is not None and t == marry_t and not married_from_start:
            one_off["wedding"] = wedding0 * P * G
            events.append({"t": t, "age": a1, "event": "Marriage"})
        for k, b in enumerate(birth_t):
            if b is not None and t == b:
                events.append({"t": t, "age": a1, "event": f"Child {k + 1}"})
                bc = _nan0(hh.get("birth_cost"))
                if bc:
                    one_off[f"birth_{k + 1}"] = start(bc, 2025) * P * G

        # ---- housing
        rent_f = 1.0
        if n_home:
            rent_f = cd.rent_factor_per_child[min(n_home, 2) - 1]
        if not owned and buy_t_plan is not None and t >= buy_t_plan:
            price = price0 * HH
            young = a1 <= _nan0(L.get("young_max_age"))
            if dp_user or not L:
                dp_min = inp["down_payment_pct"]["value"] / 100
            else:
                dp_min = (L["down_pct_young"] if young else L["down_pct"]) / 100
            term = (L.get("term_young") if young else L.get("term")) if L else None
            if plan.mortgage_years is not None:
                term = plan.mortgage_years
            elif term is None or term != term:
                term = 25
            # buying later: today's rate moves to the forward rate over 5 years
            rate = m_rate_now + (rate_lr - m_rate_now) * min(t / 5, 1.0)
            fixed_all = False
            if cd.iso3 == "HUN" and price / P <= 100e6 and a1 < 99:   # Otthon Start
                rate, dp_min, term, fixed_all = 0.03, 0.10, 25, True
            ch_style = cd.iso3 in CH_STYLE and not (term and term > 0)
            gross_hh, net_hh = sum(normal), normal_net
            limits = {"down payment (LTV)": price * (1 - dp_min)}
            stress = max(rate + _nan0(L.get("stress_add")) / 100,
                         _nan0(L.get("stress_floor")) / 100)
            dsti = _nan0(L.get("dsti_max")) / 100
            other = 0.0
            if cd.iso3 in ("USA", "CAN"):
                other = price * (_nan0(L.get("property_tax_pct")) +
                                 _nan0(L.get("home_ins_pct"))) / 100
            if ch_style:
                # SBA rule: 5% imputed interest + 1% upkeep + amortisation to 2/3 LTV in
                # 15 years <= 1/3 of gross income
                cap = gross_hh / 3 - 0.01 * price
                # loan*0.05 + (loan - 2/3 price)/15 <= cap  (amortisation part if > 2/3)
                lo = cap / 0.05
                if lo > 2 / 3 * price:
                    lo = (cap + 2 / 3 * price / 15) / (0.05 + 1 / 15)
                limits["affordability (1/3 of gross at 5%)"] = max(lo, 0)
            elif dsti > 0 and L:
                basis = gross_hh if L.get("dsti_basis") == "gross" else net_hh
                limits[f"payment-to-income ({dsti * 100:.0f}% of "
                       f"{L.get('dsti_basis')})"] = annuity_pv(dsti * basis - other, stress,
                                                               term)
            if _nan0(L.get("lti_max")) > 0:
                limits[f"loan-to-income ({L['lti_max']:g}x gross)"] = L["lti_max"] * gross_hh
            if plan.max_payment_to_income is not None or not L:
                cap_pct = plan.max_payment_to_income if plan.max_payment_to_income is not None \
                    else 40.0
                limits[f"payment-to-income ({cap_pct:g}% of net)"] = annuity_pv(
                    cap_pct / 100 * max(net_hh, 0), rate, term)
            binding = min(limits, key=limits.get)
            loan = max(min(limits.values()), 0.0)
            # up-front costs
            if tt_user or not L:
                tax_buy = price * inp["transfer_tax_pct"]["value"] / 100
            elif cd.iso3 == "GBR":
                tax_buy = uk_sdlt_first_time(price / P) * P
            elif cd.iso3 == "NLD":
                tax_buy = 0.0 if a1 < 35 and price / P <= 555_000 else 0.02 * price
            elif cd.iso3 == "PRT":
                tax_buy = 0.0 if a1 <= 35 and price / P <= 330_539 else 0.06 * price
            else:
                tax_buy = price * _nan0(L.get("transfer_pct")) / 100
            closing = price * (_nan0(L.get("closing_pct")) if L else plan.closing_cost_pct) / 100
            prem = 0.0
            if cd.iso3 == "CAN" and loan > 0.8 * price:
                ltv = loan / price
                prem = loan * (0.04 if ltv > 0.9 else 0.031 if ltv > 0.85 else 0.028)
            equity = price - loan + tax_buy + closing
            buffer = plan.emergency_fund_months * prev_recurring / 12
            ok_loan = loan >= 0.5 * price * (1 - dp_min) or cash + invest >= price
            if ok_loan and cash + invest - equity >= buffer:
                owned = True
                one_off["house_purchase"] = equity
                one_off["moving"] = MOVING_PCT / 100 * price
                home_value = buy_price = price
                mort_bal = loan0 = loan + prem
                mort_rate = rate
                fix_years_eff = term if fixed_all else fix_years
                mort_left = term
                mort_age = 0
                mort_pay = annuity(mort_bal, mort_rate, mort_left) if not ch_style else 0.0
                events.append({"t": t, "age": a1, "event": "House purchase"})
                if t > buy_t_plan:
                    warnings.append(f"House purchase delayed from age {plan.buy_house_age} "
                                    f"to {a1}: the binding limit was {binding} and/or "
                                    f"savings for the {equity / price * 100:.0f}% up-front "
                                    "(down payment + taxes + fees)")
            elif t == n_years - 1:
                why = (f"up-front {equity / P:,.0f} {cd.currency} in today's money (down "
                       "payment, taxes, fees) > savings minus the emergency buffer"
                       if ok_loan else f"the {binding} limit allows a loan of only "
                       f"{loan / P:,.0f} {cd.currency} in today's money")
                warnings.append(f"House never affordable within the horizon ({why}). "
                                "Try a cheaper home, a larger down payment or a longer term.")
        interest = principal = 0.0
        if owned:
            if mort_bal > 0.5:
                if mort_age >= fix_years_eff and abs(mort_rate - rate_lr) > 1e-9:
                    mort_rate = rate_lr        # fixed period over: long-run average rate
                    if not ch_style:
                        mort_pay = annuity(mort_bal, mort_rate, max(mort_left, 1))
                if ch_style:
                    interest = mort_bal * mort_rate
                    excess = mort_bal - 2 / 3 * buy_price
                    principal = min(max(excess, 0), max(loan0 - 2 / 3 * buy_price, 0) / 15)
                    mort_bal -= principal
                else:
                    for _ in range(12):
                        i_m = mort_bal * mort_rate / 12
                        p_m = min(mort_pay / 12 - i_m, mort_bal)
                        interest += i_m
                        principal += p_m
                        mort_bal -= p_m
                mort_left -= 1
                mort_age += 1
                exp["mortgage_payment"] = interest + principal
            if plan.owner_cost_pct is not None or not L:
                pct = plan.owner_cost_pct if plan.owner_cost_pct is not None else 1.0
                exp["owner_costs"] = home_value * pct / 100
            else:
                exp["owner_costs"] = home_value * (OWNER_MAINTENANCE_PCT +
                                                   _nan0(L.get("property_tax_pct")) +
                                                   _nan0(L.get("home_ins_pct"))) / 100
                extra = 0.0
                if cd.iso3 == "USA" and mort_bal > 0.8 * buy_price:
                    extra += 0.005 * mort_bal                       # PMI
                if _nan0(L.get("borrower_ins_pct")) and mort_bal > 0:
                    extra += loan0 * L["borrower_ins_pct"] / 100     # FR assurance emprunteur
                if extra:
                    exp["mortgage_insurance"] = extra
                if cd.iso3 == "NLD":
                    relief = 0.3748 * max(interest - 0.0035 * home_value, 0)
                else:
                    cap = _nan0(L.get("relief_cap"))
                    relief = _nan0(L.get("relief_rate")) * (min(interest, cap * P) if cap
                                                            else interest)
                if relief:
                    exp["mortgage_interest_relief"] = -relief
            home_value *= (1 + infl) * Hh[t + 1] / Hh[t]
        else:
            exp["rent"] = rent0 * H * rent_f
        # wealth tax (Norway, Switzerland)
        wt = _nan0(hh.get("wealth_tax_rate"))
        if wt:
            base = cash + invest * (0.8 if cd.iso3 == "NOR" else 1.0) + \
                home_value * _nan0(hh.get("wealth_home_share")) - mort_bal
            thr = _nan0(hh.get("wealth_tax_threshold")) * (2 if married else 2) * P
            if base > thr:
                exp["wealth_tax"] = wt * (base - thr)
        recurring = sum(exp.values())
        prev_housing = (interest + exp.get("owner_costs", 0.0) + exp.get("mortgage_insurance", 0.0)
                        + exp.get("mortgage_interest_relief", 0.0)) * (1 + infl)
        total_out = recurring + sum(one_off.values())
        saving = net - total_out
        # ---- budget constraint: cut discretionary spending rather than run up debt
        squeeze = 0.0
        # retirees cannot borrow unsecured: owners borrow against the home at mortgage
        # rates (equity release), and everyone cuts living costs harder first
        r_debt_now = (rate_lr + 0.01 if owned else r_debt) if retired else r_debt
        avail = grow_cash(cash) + invest * (1 + r_inv_t) + saving
        if avail < 0:
            keys = [k for k in exp if k in DISCRETIONARY or (retired and
                                                              k.startswith("budget:"))]
            pool = sum(exp[k] for k in keys) + CHILD_DISCRETIONARY * exp.get("children", 0.0)
            cut = min(-avail, MAX_SQUEEZE * pool)
            if cut > 0:
                f = cut / pool
                for k in keys:
                    exp[k] *= 1 - f
                if "children" in exp:
                    exp["children"] *= 1 - CHILD_DISCRETIONARY * f
                squeeze = cut
                recurring = sum(exp.values())
                total_out = recurring + sum(one_off.values())
                saving = net - total_out
        # old age with savings gone: owners borrow against the home (up to its equity);
        # beyond that the state's minimum income for older people (safety-net pension,
        # housing benefit) fills the gap - it is not run up as debt
        assistance = 0.0
        if retired:
            avail = grow_cash(cash) + invest * (1 + r_inv_t) + saving
            floor = -(home_value - mort_bal) * 0.8 if owned else 0.0
            if avail < floor:
                assistance = floor - avail
                saving += assistance
                net += assistance
        prev_recurring = recurring

        # ---- balances: emergency fund (cash) first; the rest split between bank
        # deposits and investments as the country's households hold their savings
        target = plan.emergency_fund_months * recurring / 12
        cash = grow_cash(cash)
        invest *= 1 + r_inv_t
        # employer pension contributions: a retirement account (net worth, not liquid)
        match = _nan0(hh.get("employer_match_pct")) / 100 * sum(gross)
        retire = retire * (1 + r_inv_t) + match
        if a1 >= ret_age and retire:      # pension pots become spendable at retirement
            invest += retire
            retire = 0.0
        cash += saving
        free = cash + invest - target
        if free > 0:
            cash, invest = target + dep_share * free, (1 - dep_share) * free
        else:
            cash, invest = cash + invest, 0.0
        tgt_prev = target
        rows.append({
            "t": t, "year": y0 + t, "age1": a1, "age2": a2, "married": married,
            "n_children": n_kids, "owned": owned, "price_factor": P,
            "gross1": gross[0], "gross2": gross[1], "gross": sum(gross),
            "income_tax_local": sum(it), "ssc_local": sum(ssc), "pension_contrib": sum(pen),
            "us_tax": us_tax, "us_method": us_method, "child_benefits": child_ben,
            "leave_benefits": leave_ben, "leave_months": sum(leave_m),
            "unemployment_months": unemp_months, "job_loss_benefits": jl_ben,
            "unemployment_risk_cost": unemp_cost, "employer_pension": match,
            "budget_scale": scale, "child_penalty_factor": fam_k, "spending_squeeze": squeeze,
            "retirement_account": retire, "pension_income": pension_income,
            "family_transfers": transfers, "retired": retired,
            "old_age_assistance": assistance,
            "net1": gross[0] - it[0] - ssc[0] - pen[0], "net2": gross[1] - it[1] - ssc[1] - pen[1],
            "net_income": net, **exp, **{f"oneoff:{k}": v for k, v in one_off.items()},
            "expenses_recurring": recurring, "expenses_total": total_out, "saving": saving,
            "cash": cash, "invest": invest, "liquid": cash + invest, "home_value": home_value
            if owned else 0.0, "mortgage_balance": mort_bal if owned else 0.0,
            "mortgage_interest": interest, "mortgage_rate": mort_rate * 100 if owned else 0.0,
        })
    df = pd.DataFrame(rows).fillna(0.0)
    df["home_equity"] = df["home_value"] - df["mortgage_balance"]
    df["net_worth"] = df["liquid"] + df["home_equity"] + df["retirement_account"]
    sq = df[df["spending_squeeze"] > 0]
    if not sq.empty:
        warnings.append(f"Money ran short in {len(sq)} year(s) (ages {int(sq['age1'].min())}-"
                        f"{int(sq['age1'].max())}): discretionary spending (recreation, eating "
                        "out, clothes, furnishings) was cut by up to "
                        f"{(sq['spending_squeeze'] / sq['expenses_recurring']).max():.0%} of "
                        "the budget instead of borrowing.")
    eff = df["income_tax_local"] + df["ssc_local"]
    df["local_tax_rate"] = np.where(df["gross"] > 0, eff / df["gross"].replace(0, np.nan), 0)
    df["saving_rate"] = np.where(df["net_income"] > 0,
                                 df["saving"] / df["net_income"].replace(0, np.nan), 0)
    if (df["liquid"] < 0).any():
        a = int(df.loc[df["liquid"] < 0, "age1"].iloc[0])
        low = df.loc[df["liquid"].idxmin()]
        warnings.append(f"Liquid savings go below zero at age {a} (lowest {low['liquid']:,.0f} "
                        f"at age {int(low['age1'])}); the gap is financed at {plan.debt_rate}% "
                        "debt. Add current savings, delay big events or trim spending.")
    # ---- retirement summary (start-year money, real)
    ret_rows = df[df["retired"].astype(bool)]
    work = df[(df["gross"] > 0)]
    summary = {"retirement_age": ret_age, "horizon_age": horizon,
               "pension_nrr": nrr_tab.get(1.0, _nan0(hh.get("pension_nrr")))}
    if not ret_rows.empty:
        r0 = ret_rows.iloc[0]
        summary["pension_income_real"] = float(r0["pension_income"] / r0["price_factor"])
        summary["net_worth_at_retirement_real"] = float(r0["net_worth"] / r0["price_factor"]) \
            if len(ret_rows) else 0.0
        last_work = work.tail(5)
        if not last_work.empty:
            sp_w = (last_work["expenses_recurring"] / last_work["price_factor"]).mean()
            sp_r = (ret_rows["expenses_recurring"] / ret_rows["price_factor"]).mean()
            summary["retirement_spending_ratio"] = float(sp_r / sp_w) if sp_w else None
            inc_w = (last_work["net_income"] / last_work["price_factor"]).mean()
            summary["pension_vs_last_net"] = float(summary["pension_income_real"] / inc_w) \
                if inc_w else None
        short = ret_rows[(ret_rows["liquid"] < 0) | (ret_rows["old_age_assistance"] > 0)]
        if not short.empty:
            a = int(short["age1"].iloc[0])
            summary["savings_run_out_age"] = a
            n_ass = int((ret_rows["old_age_assistance"] > 0).sum())
            warnings.append(f"In retirement, savings run out at age {a}: the pension does "
                            "not cover a basic budget" + (", so living costs are cut and the "
                            "gap is borrowed against the home" if df["owned"].any() else
                            ", so living costs are cut") + (
                                f"; in {n_ass} year(s) the state's minimum income for older "
                                "people would have to fill the rest" if n_ass else "") + ".")
    return Result(df, events, warnings, inp, summary)


def to_display(res: Result, cd: CountryData, real: bool = True,
               currency: str = "local") -> pd.DataFrame:
    """Convert money columns: real (start-year prices) and/or USD / intl $ at the
    start-year exchange rate / PPP (held constant)."""
    df = res.years.copy()
    skip = {"t", "year", "age1", "age2", "married", "n_children", "owned", "price_factor",
            "us_method", "unemployment_months", "local_tax_rate", "saving_rate",
            "budget_scale", "leave_months", "mortgage_rate", "child_penalty_factor",
            "retired"}
    money = [c for c in df.columns if c not in skip and pd.api.types.is_numeric_dtype(df[c])]
    div = df["price_factor"] if real else 1.0
    conv = {"local": 1.0, "USD": cd.lcu_per_usd.value, "intl$": cd.lcu_per_intl.value}[currency]
    for c in money:
        df[c] = df[c] / div / conv
    return df


def compare(plans: dict[str, tuple[Plan, CountryData]], us: USParams | None,
            real: bool = True, currency: str = "intl$") -> pd.DataFrame:
    """Same plan in several countries -> one long table for comparison charts."""
    out = []
    for label, (p, cd) in plans.items():
        r = simulate(p, cd, us)
        d = to_display(r, cd, real, currency)
        d["country"] = label
        out.append(d)
    return pd.concat(out, ignore_index=True)

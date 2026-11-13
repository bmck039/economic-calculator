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
    career: list = field(default_factory=list)   # career.CareerPath per earner (US)


VEHICLE_CATS = {"vehicle_purchase", "fuel_vehicle", "vehicle_maintenance", "vehicle_other",
                "insurance_vehicle", "vehicle_running", "vehicle_all"}
HEALTH_INS_CATS = {"insurance_health", "health_insurance_mandatory"}
CH_STYLE = {"CHE", "LIE"}          # interest-only first mortgage, 2nd tranche amortised
OWNER_MAINTENANCE_PCT = 0.7        # upkeep beyond the minor repairs already in the budget
LTC_DEFAULT = {"USA": (74_400, "CareScout Cost of Care 2025: national median assisted "
                           "living (private nursing-home room: 129,575)", 2025)}
CHURCH_TAX = {"DEU": 0.085}        # share of income tax (8% BY/BW, 9% elsewhere)
YOUNG_DRIVER = ((25, 1.7), (30, 1.25))
from .career import RANK_PERSISTENCE  # noqa: E402
# Calibrated in econsim.backtest (SCF couples 2004-2022 run through what happened, vs the
# same birth years in later waves) and econsim.calibrate (national saving rates):
SURVEY_CORRECTION = 1.0           # the survey basket raised to national-accounts totals
WEALTH_MPC = 0.04                 # share of free savings spent per year before retirement
#                                   (not by renters saving for a home); Carroll, Otsuka &
#                                   Slacalek 2011: 2-6 cents per dollar of wealth - the
#                                   backtest cannot tell values apart (few couples hold
#                                   much above the emergency fund)
HOUSING_ELASTICITY = 0.75         # rent / home price vs income relative to the survey couple
HOME_DRIFT = ((999, 0.0),)       # owned home vs the price index, by age: none - a repeat-
#                                   sales index follows the same houses, so it already
#                                   includes their ageing; SCF owner cohorts' homes kept pace
#                                   with it (or gained, trading up), 1989-2025, nominal
REFI_COUNTRIES = {"USA", "DNK"}
REFI_GAP = 0.01                   # refinance when new loans cost this much less
REFI_COST = 0.01                  # closing costs, added to the balance
SCALE_BOUNDS = (0.25, 4.0)        # spending vs the survey couple's, at the extremes of income
BORROW_LIMIT = 0.25               # unsecured debt a household can carry, x a year's net income
SUBSIST_CUT = 0.3                 # beyond it, living costs are cut by up to this share
FPL_2025 = (10_150.0, 5_500.0)    # HHS poverty guideline 2025: 10,150 + 5,500 per person
CAR_ELASTICITY = 0.25             # car costs vs the same
PRICE_TREND_YEARS = 10             # how long today's relative price trends continue
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
    IO = cd.inflation_outlook or {}
    if plan.inflation is None and "inflation_5y5y" in IO:
        v, per, src_ = IO["inflation_5y5y"]
        infl_src = cd.inflation_hist.with_value(
            round(v, 2), source=f"what bond markets expect after the next 5 years: 5-year, "
            f"5-year-forward inflation {v:.2f}% ({src_}, {per}); CPI-linked Treasuries vs "
            f"nominal ones", estimated=True)
    else:
        infl_src = cd.inflation_hist.with_value(
            infl_default, source=f"central-bank inflation target ({infl_default:g}%); "
            f"10-year history {cd.inflation_hist.value:.1f}%/yr", estimated=True)
    out = {
        "inflation": pick(plan.inflation, infl_src, "%"),
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
    if plan.inflation is None and "inflation_5y" in IO:
        v, per, src_ = IO["inflation_5y"]
        out["inflation_next_5y"] = {"value": round(v, 2), "unit": "%", "year": per,
                                    "estimated": True, "quality": "B",
                                    "source": f"5-year breakeven inflation ({src_}, {per}): "
                                    "what bond markets expect for the next 5 years"}
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
    elif PF.get("equity_share_typical") is not None:
        out["equity_share"] = {
            "value": round(float(PF["equity_share_typical"]) * 100, 1),
            "unit": "% of investments", "quality": "B", "estimated": False,
            "year": PF.get("year"),
            "source": "typical households (bottom 70% by wealth): "
            + str(PF.get("equity_share_typical_source", "")),
            "note": f"the whole household sector holds {100 * float(PF['equity_share']):.0f}% "
            "(OECD), led by the wealthiest"}
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
    elif PF.get("deposit_share_typical") is not None:
        out["deposit_share"] = {
            "value": round(float(PF["deposit_share_typical"]) * 100, 1),
            "unit": "% of savings", "quality": "B", "estimated": False,
            "year": PF.get("year"),
            "source": "typical households (bottom 70% by wealth): "
            + str(PF.get("deposit_share_typical_source", "")),
            "note": f"the whole household sector: {100 * float(PF['deposit_share']):.0f}% "
            "(OECD)"}
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
    if "hci_5y" in IO:
        h, c = IO["hci_5y"], IO.get("cpi_5y", (float("nan"),))
        h1, c1 = IO.get("hci_12m", (float("nan"),)), IO.get("cpi_12m", (float("nan"),))
        h10, c10 = IO.get("hci_10y", (float("nan"),)), IO.get("cpi_10y", (float("nan"),))
        out["household_costs_5y"] = {
            "value": round(h[0], 2), "unit": "%/yr", "year": h[1], "quality": "B",
            "estimated": True, "source": h[2],
            "note": f"what households pay rose {h1[0]:.1f}% in the last 12 months, "
            f"{h[0]:.1f}%/yr over 5 years and {h10[0]:.1f}%/yr over 10 (CPI: {c1[0]:.1f}%, "
            f"{c[0]:.1f}%, {c10[0]:.1f}%). The gap is housing: the CPI counts owners' "
            "costs as an imputed rent, not home prices and mortgage interest. The "
            "simulation prices your own housing, rent and each budget line directly - "
            "see 'your own inflation' in the results - so this is shown, not added again"}
    out.update(start_wealth_inputs(plan, cd, out))
    return out


EDU_RANK = ("below_upper_secondary", "upper_secondary", "short_tertiary", "bachelor",
            "master_plus")


def home_price_for(plan: Plan, cd: CountryData, hp: dict) -> dict | None:
    """Households buy homes in line with their means: the median price x what owner
    couples with the household's education live in vs all owner couples, at the
    planned buying age (US: SCF). None where unknown or the price is the user's."""
    if plan.home_price is not None or cd.start_wealth is None:
        return None
    eds = [e.education for e in plan.earners if e.education in EDU_RANK]
    if not eds:
        return None
    edu = max(eds, key=EDU_RANK.index)
    a = plan.buy_house_age or 35
    mine = start_wealth_value(cd, "owner_home", a, edu)
    allc = start_wealth_value(cd, "owner_home", a, None)
    if not mine or not allc or mine[2] == "all" or allc[1] != mine[1] or allc[0] <= 0:
        return None
    k = mine[0] / allc[0]
    return dict(hp, value=hp["value"] * k,
                source=hp["source"] + f"; x {k:.2f}: owner couples "
                f"{'with' if mine[2] == 'degree' else 'without'} a bachelor's degree aged "
                f"{a} live in homes worth {k:.2f}x those of all owner couples (SCF "
                f"{mine[1]})", estimated=True)


SW_EDU = {"bachelor": "degree", "master_plus": "degree", "short_tertiary": "no_degree",
          "upper_secondary": "no_degree", "below_upper_secondary": "no_degree"}


def start_wealth_value(cd: CountryData, stat: str, age: int, edu: str | None):
    """What couples of this age (head) and degree hold now (US: SCF), with its year."""
    t = cd.start_wealth
    if t is None or len(t) == 0:
        return None
    for e, ht in ((SW_EDU.get(edu or "", "all"), "couple"), ("all", "couple"),
                  (SW_EDU.get(edu or "", "all"), "all"), ("all", "all")):
        r = t[(t["statistic"] == stat) & (t["education"] == e) & (t["household_type"] == ht)]
        # only where families of about this age were surveyed (no extrapolation)
        if len(r) >= 2 and r["age"].min() - 1 <= age <= r["age"].max() + 1:
            r = r.sort_values("age")
            v = float(np.interp(age, r["age"], r["value"]))
            src = str(r["source"].iloc[0]) if "source" in r else ""
            return v, int(r["year"].iloc[0]), e, ht, src
    return None


def owns_at_start(plan: Plan, cd: CountryData) -> bool:
    if plan.own_home is not None:
        return bool(plan.own_home)
    e1 = plan.earners[0]
    return plan.buy_house_age is not None and e1.age >= plan.buy_house_age and \
        start_wealth_value(cd, "owner_home", e1.age, e1.education) is not None


def start_wealth_inputs(plan: Plan, cd: CountryData, out: dict) -> dict:
    """Starting balances: what you enter, else what families like yours hold at your age
    (US: Survey of Consumer Finances; euro area: ECB HFCS; elsewhere none - enter yours)."""
    e1 = plan.earners[0]
    res = {}
    own = owns_at_start(plan, cd)

    def sw(key, override, stat, note):
        if override is not None:
            res[key] = {"value": float(override), "unit": cd.currency, "source": "user input"}
            return
        v = start_wealth_value(cd, stat, e1.age, e1.education)
        if v is None:
            res[key] = {"value": 0.0, "unit": cd.currency, "estimated": True,
                        "source": "none assumed (no survey of household wealth by age "
                        "loaded for this country): enter yours"}
            return
        val, yr, e, ht, src = v
        if "HFCS" in src:
            text = (f"{src}: median for households with the reference person aged "
                    f"{e1.age} (between the survey's 10-year age bands)")
        else:
            text = (f"Federal Reserve SCF {yr}: median for "
                    f"{'couples' if ht == 'couple' else 'families'} with the head aged "
                    f"{e1.age} (7-year window)"
                    + ("" if e == "all" else f", {'with' if e == 'degree' else 'without'} "
                       "a bachelor's degree"))
        res[key] = {"value": round(val, -2), "unit": cd.currency, "year": yr,
                    "quality": "A" if "HFCS" not in src else "B", "estimated": False,
                    "source": text, "note": note}

    res["own_home"] = {"value": float(own), "unit": "", "source": "user input"
                       if plan.own_home is not None else
                       "planned buying age reached: owning, as the owners of this age "
                       "surveyed" if own else "renting at the start"}
    if own:
        sw("starting_savings", plan.starting_savings, "owner_fin",
           "home owners' financial assets (incl. retirement accounts, which are split off)")
        sw("starting_retirement", plan.starting_retirement, "owner_retirement",
           "home owners' retirement accounts (401k, IRA)")
        sw("home_value_now", plan.home_value_now, "owner_home", "owners' primary home")
        sw("mortgage_now", plan.mortgage_now, "owner_mortgage",
           "owners' mortgage and home-equity debt (0 for the many who have paid off)")
        if res["home_value_now"]["value"] <= 0:
            res["home_value_now"] = {"value": out["home_price"]["value"], "unit": cd.currency,
                                     "estimated": True, "source": "the typical home price "
                                     "(enter your home's value)"}
            if plan.mortgage_now is None:
                res["mortgage_now"] = {"value": 0.5 * out["home_price"]["value"],
                                       "unit": cd.currency, "estimated": True,
                                       "source": "assumption: half the value still owed "
                                       "(enter your balance)"}
        lr = _nan0((cd.lend or {}).get("rate_longrun"))
        res["mortgage_rate_now"] = (
            {"value": float(plan.mortgage_rate_now), "unit": "%", "source": "user input"}
            if plan.mortgage_rate_now is not None else
            {"value": lr or out["mortgage_rate"]["value"], "unit": "%", "estimated": True,
             "quality": "B", "source": "average rate on new loans over the last 20 years ("
             + (cd.lend or {}).get("rate_longrun_source", "") + "): existing loans were "
             "taken out and refinanced over many years (FHFA reports ~4% on outstanding US "
             "mortgages)"})
        bal, val = res["mortgage_now"]["value"], max(res["home_value_now"]["value"], 1.0)
        res["mortgage_years_left"] = (
            {"value": int(plan.mortgage_years_left), "unit": "years", "source": "user input"}
            if plan.mortgage_years_left is not None else
            {"value": int(min(max(round(30 * bal / (0.85 * val)), 5), 30)) if bal > 0 else 0,
             "unit": "years", "estimated": True, "quality": "C",
             "source": "assumption: a 30-year loan for 85% of the value, paid down in "
             "proportion to the balance left"})
    else:
        sw("starting_savings", plan.starting_savings, "renter_fin",
           "renters' financial assets incl. retirement accounts (split off below)")
        sw("starting_retirement", plan.starting_retirement, "renter_retirement",
           "renters' retirement accounts (401k, IRA)")
    # the medians of a total and of a part do not subtract exactly; the part is capped
    tot = res["starting_savings"]
    rt = res["starting_retirement"]
    if tot.get("source") != "user input" and rt.get("source") != "user input":
        r = min(rt["value"], tot["value"])
        res["starting_retirement"] = dict(rt, value=round(r, -2))
        res["starting_savings"] = dict(tot, value=round(max(tot["value"] - r, 0.0), -2),
                                       note=tot.get("note", "") + " - less the retirement "
                                       "accounts")
    return res


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
    if cpi and to_year < fy and to_year in cpi:          # back in time: actual CPI
        top = min(fy, max(cpi))
        return value * cpi[to_year] / cpi[top] / (1 + inflation / 100) ** (fy - top)
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


def cohort_pay_path(coh, age0: int, edu: str | None, years: int, mode: str):
    """(growth, gap): the cohort's typical pay relative to economy-wide wages, h = 0..
    years-1 (today's age brackets + the gap that lasts; ratio to h = 0), and the lasting
    gap alone as a factor on a cross-sectional profile. None where no cohort data."""
    if coh is None:
        return None
    if coh.get("earn_ftyr", edu) is not None:         # US: CPS ASEC
        # full-time year-round workers: a pay RATE, like the model's (all earners' annual
        # pay also mixes in part-year work - new graduates, people easing into retirement)
        p = coh.path("earn_ftyr", edu, age0, years, mode)
        ok = ~np.isnan(p)
        if not ok.any():
            return None
        p = np.where(ok, p, p[ok][-1])
        gap = np.exp([coh.adjustment("earn_ftyr", edu, age0, h, mode)
                      for h in range(years)])
        return np.exp(p - p[0]), gap
    if coh.get("earn_ses", "all") is not None:        # Europe: SES (age bands)
        gap = np.exp([coh.adjustment("earn_ses", "all", age0, h, mode)
                      for h in range(years)])
        return None, gap
    return None


def simulate(plan: Plan, cd: CountryData, us: USParams | None = None,
             history=None) -> Result:
    """history: an econsim.history.History - what actually happened from the start year
    (prices, wages, home prices, rents, rates, returns, unemployment) in place of the
    forecasts; levels (budget, rents, home prices) are taken back to the start year by
    their own price indices. For backtests."""
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

    HS = history

    def start(v, year=None):  # data-year money -> start-year money
        if HS is not None:     # back to a past start year with the actual CPI
            return roll(v, year if year is not None else dy, dy, infl * 100, cpi) * \
                HS.cpi_level
        return roll(v, year if year is not None else dy, y0, infl * 100, cpi)

    e1, e2 = plan.earners
    n_years = max(horizon - e1.age + 1, 1)
    # price path: the market's expectation for the next 5 years (US breakevens), then the
    # long-run rate
    near = inp.get("inflation_next_5y", {}).get("value")
    INF = [near / 100 if near is not None and t < 5 else infl for t in range(n_years + 2)]
    if HS is not None:
        INF = [HS.infl[t] if t < HS.n() and HS.infl[t] == HS.infl[t] else INF[t]
               for t in range(len(INF))]
    Pt = [1.0]
    for t in range(n_years + 1):
        Pt.append(Pt[-1] * (1 + INF[t]))
    # economy-wide real wages (forecast, or what they did)
    GW = [1.0]
    for t in range(n_years + 1):
        g_t = HS.wage_real[t] if HS is not None and t < HS.n() and \
            HS.wage_real[t] == HS.wage_real[t] else g_real
        GW.append(GW[-1] * (1 + g_t))
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

    # ---- cohorts: each partner's cohort vs the people before it at the same age
    coh, cmode = cd.cohort, plan.cohort_effects
    pay_g = [cohort_pay_path(coh, e.age, e.education if plan.education_pay else None,
                             n_years, cmode) for e in plan.earners]
    for i, e in enumerate(plan.earners):
        if pay_g[i] is None or pay_g[i][0] is None:
            # no full-time series by single age: the country's full-time pay by age
            # (today's brackets) x the generation gap that lasts, where measured
            pts = cd.earnings.get(e.percentile) or cd.earnings.get("median")
            if pts:
                ed = e.education if plan.education_pay else None
                b0 = interp_profile(pts, e.age) * edu_pay_factor(cd, ed, e.age)
                shape = np.array([interp_profile(pts, e.age + h) *
                                  edu_pay_factor(cd, ed, e.age + h) / b0 if b0 > 0 else 1.0
                                  for h in range(n_years)])
                gap = pay_g[i][1] if pay_g[i] is not None else np.ones(n_years)
                pay_g[i] = (shape * gap, gap)
    # ---- careers from today's job market (US): situation mix, moves, raises
    paths = [None, None]
    cdat = cd.career if plan.career_model else None
    if cdat is not None:
        from .career import STATE_LABEL, career_path
        for i, e in enumerate(plan.earners):
            pn = e.pay_now / start(1.0, cdat.year) if e.pay_now else None
            paths[i] = career_path(cdat, e.age, e.education if plan.education_pay else None,
                                   n_years, percentile=e.percentile, job=e.job, pay_now=pn,
                                   window="trend" if plan.job_market == "trend" else
                                   "current", growth=pay_g[i][0] if pay_g[i] else None)
        for i, pth in enumerate(paths):
            m0 = pth.mix[0]
            inp[f"P{i + 1} situation now"] = {
                "value": round(100 * m0.get(pth.start_state or "FT_grad", 0.0), 1),
                "unit": "%", "year": cdat.year, "quality": "A", "estimated": False,
                "source": f"{cdat.source} {cdat.window}: " + (
                    f"{STATE_LABEL[pth.start_state]} (your input)" if pth.start_state else
                    "today's mix for this age and degree - " + ", ".join(
                        f"{STATE_LABEL[k]} {v:.0%}" for k, v in m0.items())),
                "note": f"pay at the {pth.quantile:.0f}th percentile of each situation; "
                "moves between situations as people of this age and degree make them now"
                + ("; the last year's change continues 5 years" if plan.job_market == "trend"
                   else "")}
    for i, e in enumerate(plan.earners):
        if pay_g[i] is None or coh is None:
            continue
        m = "earn_ftyr" if coh.get("earn_ftyr", None) is not None else "earn_ses"
        edu = e.education if plan.education_pay else None
        g10 = coh.gap_now(m, edu, e.age, 10)
        a10 = coh.adjustment(m, edu, e.age, 10, cmode)
        src = coh.source.get(m, "")
        inp[f"P{i + 1} cohort pay gap"] = {
            "value": round(100 * (np.exp(a10) - 1), 1), "unit": "%", "quality": "B",
            "estimated": True, "year": coh.latest_year(m),
            "source": f"{src}: people aged {e.age} now earn {100 * (np.exp(g10) - 1):+.1f}% "
            + ("(full time, year round) " if m == "earn_ftyr" else "") +
            "vs people 10 years older when they were this age (relative to all workers; "
            "last 3 surveys)",
            "note": ("in 10 years: this much vs today's people aged "
                     f"{e.age + 10}; " + (
                         "the whole gap lasts (your choice)" if cmode == "full" else
                         "no gap carried (your choice)" if cmode == "off" else
                         f"{100 * coh.phi(m, 10):.0f}% of such gaps have lasted 10 years "
                         "in past surveys" + (f" ({coh.borrowed[m]})" if m in coh.borrowed
                                              else "")))}
    # elsewhere: a stated current pay sets the level (the profile keeps its shape)
    pay_k = [1.0, 1.0]
    for i, e in enumerate(plan.earners):
        if paths[i] is None and e.pay_now:
            b0 = start(interp_profile(prof[i], e.age) * pay_f(i, e.age), earn_year)
            pay_k[i] = e.pay_now * 12 / b0 if b0 > 0 else 1.0

    def gross_at(i: int, age: int, t: int) -> float:
        e = plan.earners[i]
        if not e.works or age >= ret_age or age < 18:
            return 0.0
        pen_w = 1.0
        if e.pay_path is not None:            # given pay (backtests: what it was)
            return float(e.pay_path[min(t, len(e.pay_path) - 1)])
        if i == plan.primary_parent and plan.child_wage_penalty and first_birth is not None \
                and t >= first_birth:
            pen_w = 1 - plan.child_wage_penalty / 100    # lasting hourly-pay penalty
        if paths[i] is not None:
            return start(paths[i].pay[min(t, len(paths[i].pay) - 1)], cdat.year) * pen_w \
                * GW[t] * Pt[t]
        base0 = interp_profile(prof[i], e.age) * pay_f(i, e.age)
        shape = interp_profile(prof[i], age) * pay_f(i, age) / base0 if base0 else 1.0
        if pay_g[i] is not None and pay_g[i][1] is not None:  # the cohort's lasting gap
            shape *= pay_g[i][1][min(t, len(pay_g[i][1]) - 1)]
        rel = shape ** e.career_growth if shape > 0 else 0.0
        k = 1 + (pay_k[i] - 1) * RANK_PERSISTENCE ** t    # stated pay fades to the profile
        return start(base0, earn_year) * k * rel * pen_w * GW[t] * Pt[t]

    first_birth = min((a - e1.age for a in plan.child_ages if a is not None), default=None)
    aw0 = start(cd.avg_wage.value, cd.avg_wage.year)
    # ---- living budget: survey lines minus what the car and health modules replace
    have_cars = bool(hh) and _nan0(hh.get("car_price")) > 0
    have_health = bool(hh) and _nan0(hh.get("health_premium_adult")) > 0
    have_oop = bool(hh) and hh.get("health_oop_adult", float("nan")) == \
        hh.get("health_oop_adult", 0)
    budget0, removed0 = {}, {}
    lvl = plan.spending_level if plan.spending_level is not None else (
        float(cd.spending_level.value) if plan.spending_fit and cd.spending_level is not None
        else 1.0)
    surv_k = plan.survey_correction if plan.survey_correction is not None else \
        SURVEY_CORRECTION
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
        budget0[k] = val * plan.budget_scale * max(cov, 1.0) ** surv_k
    ref = cd.budget_ref_income
    ref0 = None
    bp = next(iter(cd.budget.values())).year if cd.budget else dy
    ref_all0 = None
    if ref is not None:   # ref.value is already in the budget's price year
        ref0 = ref_all0 = start(ref.value, bp)
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
    hc_k = 1.0
    if HS is not None:   # back then: each price vs the CPI, spending norms vs real wages
        RL = HS.rel_level.get
        aw0 *= HS.wage_level
        for k in budget0:
            budget0[k] *= RL(k, 1.0) / HS.wage_level
        if ref0:
            ref0 /= HS.wage_level
            ref_all0 /= HS.wage_level
        rent0 *= HS.rent_level
        price0 *= HS.home_level
        childcare0 *= RL("childcare", 1.0)
        child_spend0 /= HS.wage_level
        car_fixed0 *= RL("vehicle_running", 1.0)
        car_ins0 *= RL("insurance_vehicle", 1.0)
        transit0 *= RL("transport_services", 1.0)
        hc_k = RL("insurance_health", 1.0)
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

    def unemp_rate(age: int, education: str | None = None, i: int | None = None) -> float:
        u = _unemp_rate(age, education, i)
        if HS is not None and i is not None:
            t = age - plan.earners[i].age
            u *= HS.unemp_factor[min(max(t, 0), len(HS.unemp_factor) - 1)]
        return u

    def _unemp_rate(age: int, education: str | None = None, i: int | None = None) -> float:
        if i is not None and paths[i] is not None:
            pth = paths[i]
            return 100 * pth.unemployment[min(age - plan.earners[i].age,
                                              len(pth.unemployment) - 1)]
        v, _ = unemployment_rate_for(cd, age, education)
        if v is not None:   # by age band and degree (labour_market.csv)
            if i is not None and coh is not None and cmode != "off":
                from .cohort import from_rel, to_rel
                a_ = coh.adjustment("unemp", education, plan.earners[i].age,
                                    age - plan.earners[i].age, cmode)
                if a_:
                    v = 100 * float(from_rel(to_rel(v / 100, 0.5, "logit") + a_, 0.5,
                                             "logit"))
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

    h_scale0 = 1.0
    Hh, Hr = [1.0], [1.0]
    for t in range(1, n_years + 1):
        gh = home_tr / 100 if home_tr is not None and t <= 10 else g_house
        gr = rent_tr / 100 if rent_tr is not None and t <= 10 else g_house
        if HS is not None and t - 1 < HS.n():
            gh = HS.home_real[t - 1] if HS.home_real[t - 1] == HS.home_real[t - 1] else gh
            gr = HS.rent_real[t - 1] if HS.rent_real[t - 1] == HS.rent_real[t - 1] else gr
        Hh.append(Hh[-1] * (1 + gh))
        Hr.append(Hr[-1] * (1 + gr))
    sw_year = lambda k: inp[k].get("year") or y0  # noqa: E731
    cash = start(inp["starting_savings"]["value"], sw_year("starting_savings"))
    retire = start(inp["starting_retirement"]["value"], sw_year("starting_retirement"))
    invest = 0.0
    owned = False
    home_value = mort_bal = mort_pay = 0.0
    mort_rate = m_rate_now
    mort_left = 0.0
    mort_age = 0
    loan0 = buy_price = 0.0
    ch_style = False
    fix_years_eff = fix_years
    if inp["own_home"]["value"]:  # already own: today's value, balance, rate, term left
        owned = True
        home_value = buy_price = start(inp["home_value_now"]["value"],
                                       sw_year("home_value_now"))
        mort_bal = loan0 = start(inp["mortgage_now"]["value"], sw_year("mortgage_now"))
        mort_rate = inp["mortgage_rate_now"]["value"] / 100
        mort_left = inp["mortgage_years_left"]["value"]
        fix_years_eff = 99 if cd.iso3 in ("USA", "DNK") else max(fix_years / 2, 1)
        mort_pay = annuity(mort_bal, mort_rate, max(mort_left, 1)) if mort_bal > 0 else 0.0
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
    # ---- current price trends: each cost's last-10-year trend vs prices in general
    # continues for the next 10 years (then it moves with prices / wages like the rest)
    PTR = cd.price_trends if plan.price_outlook == "trend" else {}

    def RT(k: str, t: int) -> float:
        if HS is not None and k in HS.rel:
            v = HS.rel[k][min(t, len(HS.rel[k]) - 1)]
            if v == v:
                return v
        r = PTR.get(k)
        return (1 + min(max(r[0], -4.0), 4.0) / 100) ** min(t, PRICE_TREND_YEARS) if r \
            else 1.0

    def RT_w(k: str, t: int) -> float:
        """For costs that already rise with wages: only the part of the trend beyond."""
        if HS is not None and k in HS.rel:
            v = HS.rel[k][min(t, len(HS.rel[k]) - 1)]
            if v == v:
                return v / GW[t]                     # what that price actually did
        r = PTR.get(k)
        if not r:
            return 1.0
        x = (1 + min(r[0], 4.0) / 100) / (1 + g_real)
        return max(x, 1.0) ** min(t, PRICE_TREND_YEARS)

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
        P = Pt[t]
        W = GW[t] * Pt[t]
        # glide path: the share in shares falls over the 15 years before retirement to
        # half of it (as target-date funds and most retirees' portfolios)
        if plan.glide_path:
            k = min(max((a1 - (ret_age - 15)) / 15, 0.0), 1.0)
            r_inv_t = r_inv - 0.5 * k * eq_share * plan.equity_premium / 100
        else:
            k = 0.0
            r_inv_t = r_inv
        if HS is not None and t < len(HS.years) and HS.equity[t] == HS.equity[t]:
            eq_t = eq_share * (1 - 0.5 * k)          # what markets actually returned
            bond = HS.bond[t] if HS.bond[t] == HS.bond[t] else HS.cash[t]
            r_inv_t = (eq_t * HS.equity[t] + (1 - eq_t) * bond - plan.fund_fees -
                       drag) / 100
            r_cash = HS.cash[t] / 100 if HS.cash[t] == HS.cash[t] else r_cash
            r_dep = HS.deposit[t] / 100 if HS.deposit[t] == HS.deposit[t] else r_dep
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
                p_us = us.indexed((HS.cpi_level if HS is not None else
                                   (1 + infl) ** (y0 - 2026)) * Pt[t])
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
        child_ben = child_ben0 * P * n_dep * GW[t]
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
                    unemp_cost += unemp_rate(ages[i], plan.earners[i].education, i) / 100 \
                        * net_i[i] * paid_frac[i] * \
                        (1 - nrr6)

        # ---- US citizens abroad
        us_tax = 0.0
        us_method = ""
        if abroad:
            f_us = (HS.cpi_level if HS is not None else (1 + infl) ** (y0 - 2026)) * Pt[t]
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
        G = GW[t]                  # living standards rise with economy-wide real wages
        scale = G
        rel_inc = 1.0              # income vs the survey couple's (same year)
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
            scale = G * (min(max((y_perm / (ref0 * G)) ** plan.budget_elasticity,
                                 SCALE_BOUNDS[0]), SCALE_BOUNDS[1])
                         if y_perm > 0 else SCALE_BOUNDS[0])
            # what the household can afford in housing and cars: its income before
            # housing vs the survey couple's (after housing it would feed back on itself)
            y_all = (normal_net - child_ben) / P
            rel_inc = y_all / (ref_all0 * G) if y_all > 0 and ref_all0 else 0.25
        # housing and cars follow income too (cheaper flats and older cars on less)
        h_el = plan.housing_elasticity if plan.housing_elasticity is not None else \
            HOUSING_ELASTICITY
        c_el = plan.car_elasticity if plan.car_elasticity is not None else CAR_ELASTICITY
        h_scale = min(max(rel_inc, 0.25), 4.0) ** h_el
        if t == 0:
            h_scale0 = h_scale
        if plan.rent is not None:     # your own rent is your rent now: only later changes
            h_scale = h_scale / h_scale0 if h_scale0 > 0 else 1.0
        c_scale = min(max(rel_inc, 0.25), 4.0) ** c_el
        exp = {f"budget:{k}": v * P * scale * RT(k, t) for k, v in budget0.items()}
        # what households spend beyond any survey category, even at national-accounts
        # level (interest on consumer debt, gifts and transfers, giving, irregular big
        # costs): the calibrated level (econsim.backtest / .calibrate), as its own line
        if lvl > 1.0:
            exp["budget:other_unrecorded"] = (lvl - 1.0) * sum(exp.values())
        elif lvl < 1.0:        # the national-accounts basket overshoots: scaled down
            exp = {k: v * lvl for k, v in exp.items()}
        # wealth effect: before retirement households spend a share of their savings
        # above the emergency fund each year (bigger homes, cars, trips, helping family);
        # calibrated in econsim.backtest on what SCF couples actually accumulated
        w_mpc = plan.wealth_mpc if plan.wealth_mpc is not None else WEALTH_MPC
        saving_for_home = not owned and buy_t_plan is not None
        if w_mpc and not retired and not saving_for_home:
            free_w = max(cash + invest - tgt_prev, 0.0)
            base_b = sum(exp.values())
            if base_b > 0 and free_w > 0:
                f_w = 1 + w_mpc * free_w / base_b
                exp = {k: v * f_w for k, v in exp.items()}
        # children: OECD-modified equivalence scale (couple 1.5; child <14 +0.3, 14+ +0.5)
        # applied to the couple's non-housing living budget; housing via the rent factor,
        # childcare, health and cars separately
        if hh:
            # OECD-modified scale on PER-PERSON spending only (food, clothes, recreation,
            # ...): energy, internet, furnishings, insurance or council tax barely change
            # with children, and children do not drink or smoke
            w = sum(0.3 / 1.5 if k < 14 else 0.5 / 1.5 for k in kid_ages
                    if k <= dep_to)
            exp["children"] = lvl * sum(v for k, v in budget0.items() if k in PER_PERSON_CATS) \
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
        exp["childcare"] = cc * P * G * RT_w("childcare", t)  # rises with wages (or faster)
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
            kid_hc = 0.0
            for k in kid_ages:
                if k <= dep_to:
                    kid_hc += _nan0(hh.get("health_premium_child")) * 12 * have_health + \
                        _nan0(hh.get("health_oop_child")) * have_oop
            if cd.iso3 == "USA":
                # means-tested public cover: CHIP for children below 200% of the federal
                # poverty guideline (state limits: median ~250%), Medicaid for adults below
                # 138% (ACA expansion, from 2014; 41 states)
                fpl = start(FPL_2025[0] + FPL_2025[1] * (2 + n_home), 2025) * P
                inc_f = sum(normal) / fpl if fpl > 0 else 99.0
                if inc_f < 2.0:
                    kid_hc = 0.0
                if inc_f < 1.38 and y0 + t >= 2014:
                    hc = 0.0
            hc += kid_hc
            if plan.private_health and cd.iso3 == "IRL":
                hc += 2 * IRL_PRIVATE_HEALTH[0] + n_dep * IRL_PRIVATE_HEALTH[1]
            exp["health"] = start(hc, 2025) * hc_k * P * G * RT_w("insurance_health", t)
        # cars and public transport
        if have_cars:
            car = 0.0
            for k in range(n_cars):
                age = ages[min(k, 1)]
                f = next((m for lim, m in YOUNG_DRIVER if age < lim), 1.0)
                car += car_fixed0 + car_ins0 * f
            exp["cars"] = car * P * G * RT_w("insurance_vehicle", t) * c_scale
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
            price = price0 * HH * h_scale
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
            if HS is not None and t < len(HS.years) and \
                    HS.mortgage_rate[t] == HS.mortgage_rate[t]:
                rate = HS.mortgage_rate[t] / 100       # what new loans cost that year
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
                # refinancing where fixed loans can be repaid without penalty (US,
                # Denmark): when new loans cost REFI_GAP points less, owners take a new
                # 30-year loan at that rate (costs added to the loan) - so equity builds
                # more slowly than the original schedule
                if cd.iso3 in REFI_COUNTRIES and not ch_style and t > 0:
                    m_new = m_rate_now + (rate_lr - m_rate_now) * min(t / 5, 1.0)
                    if HS is not None and t < len(HS.years) and \
                            HS.mortgage_rate[t] == HS.mortgage_rate[t]:
                        m_new = HS.mortgage_rate[t] / 100
                    if m_new < mort_rate - REFI_GAP and mort_left < 28:
                        mort_bal *= 1 + REFI_COST
                        mort_rate, mort_left = m_new, 30
                        mort_pay = annuity(mort_bal, mort_rate, mort_left)
                        fix_years_eff = mort_age + 99
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
            # an owned home follows the (repeat-sales) price index
            drift = next(d for lim, d in HOME_DRIFT if a1 < lim)
            home_value *= Pt[t + 1] / Pt[t] * Hh[t + 1] / Hh[t] * (1 + drift)
        else:
            exp["rent"] = rent0 * H * rent_f * h_scale
        # wealth tax (Norway, Switzerland)
        wt = _nan0(hh.get("wealth_tax_rate"))
        if wt:
            base = cash + invest * (0.8 if cd.iso3 == "NOR" else 1.0) + \
                home_value * _nan0(hh.get("wealth_home_share")) - mort_bal
            thr = _nan0(hh.get("wealth_tax_threshold")) * (2 if married else 2) * P
            if base > thr:
                exp["wealth_tax"] = wt * (base - thr)
        # ---- what each line's PRICE did this year (quantities aside): the household's
        # own cost of living, like the UK Household Costs Indices - its own basket, with
        # market rents and what its mortgage actually costs (fixed loans: nothing more)
        pfac = {"rent": H, "owner_costs": HH, "children": P, "emergencies": P * G,
                "childcare": P * G * RT_w("childcare", t),
                "health": P * G * RT_w("insurance_health", t),
                "cars": P * G * RT_w("insurance_vehicle", t),
                "public_transport": P * G, "university": P * G, "long_term_care": P * G,
                "mortgage_payment": 1.0, "mortgage_insurance": 1.0, "student_loan": 1.0}
        for k in budget0:
            pfac[f"budget:{k}"] = P * RT(k, t)
        pfac["budget:other_unrecorded"] = P
        recurring = sum(exp.values())
        prev_housing = (interest + exp.get("owner_costs", 0.0) + exp.get("mortgage_insurance", 0.0)
                        + exp.get("mortgage_interest_relief", 0.0)) * (1 + INF[t])
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
            # the unrecorded line is as discretionary as the recorded basket
            bl = sum(v for k, v in exp.items() if k.startswith("budget:") and
                     k != "budget:other_unrecorded")
            o_share = (sum(exp[k] for k in keys if k != "budget:other_unrecorded") / bl
                       if bl > 0 and not retired else 1.0)
            oth = exp.get("budget:other_unrecorded", 0.0)
            keys = [k for k in keys if k != "budget:other_unrecorded"]
            pool = sum(exp[k] for k in keys) + CHILD_DISCRETIONARY * exp.get("children", 0.0) \
                + o_share * oth
            cut = min(-avail, MAX_SQUEEZE * pool)
            if cut > 0:
                f = cut / pool
                for k in keys:
                    exp[k] *= 1 - f
                if "children" in exp:
                    exp["children"] *= 1 - CHILD_DISCRETIONARY * f
                if oth:
                    exp["budget:other_unrecorded"] = oth * (1 - o_share * f)
                squeeze = cut
                recurring = sum(exp.values())
                total_out = recurring + sum(one_off.values())
                saving = net - total_out
        # beyond what a household can borrow unsecured (credit cards, overdrafts: a share
        # of a year's income), everything but housing, care, health and loans is cut
        limit = 0.0 if retired else BORROW_LIMIT * max(net, 0.0)
        avail = grow_cash(cash) + invest * (1 + r_inv_t) + saving
        if avail < -limit:
            keys = [k for k in exp if k.startswith("budget:") or k in (
                "children", "cars", "public_transport", "emergencies")]
            pool = sum(exp[k] for k in keys)
            cut = min(-limit - avail, SUBSIST_CUT * pool)
            if cut > 0 and pool > 0:
                f = cut / pool
                for k in keys:
                    exp[k] *= 1 - f
                squeeze += cut
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
            "student_loan_balance": max(sl_bal, 0.0),
            "_pfac": pfac, "_exp": {k: v for k, v in exp.items() if v > 0},
        })
    # own cost-of-living index: chained, last year's spending as the weights
    ci = [1.0]
    for a, b in zip(rows[:-1], rows[1:]):
        w = {k: v for k, v in a["_exp"].items() if k in a["_pfac"] and k in b["_pfac"] and
             a["_pfac"][k] > 0}
        tot = sum(w.values())
        ci.append(ci[-1] * (sum(v * b["_pfac"][k] / a["_pfac"][k] for k, v in w.items()) /
                            tot if tot > 0 else b["price_factor"] / a["price_factor"]))
    for r_, c_ in zip(rows, ci):
        r_.pop("_pfac")
        r_.pop("_exp")
        r_["cost_index"] = c_
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
    n10 = min(10, len(df) - 1)
    if n10 > 0:   # %/yr over the first (up to) 10 years: own costs vs consumer prices
        summary["own_inflation"] = 100 * ((df["cost_index"].iloc[n10] /
                                           df["cost_index"].iloc[0]) ** (1 / n10) - 1)
        summary["cpi_inflation"] = 100 * ((df["price_factor"].iloc[n10] /
                                           df["price_factor"].iloc[0]) ** (1 / n10) - 1)
        summary["own_inflation_years"] = n10
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
    return Result(df, events, warnings, inp, summary, [p for p in paths if p is not None])


def to_display(res: Result, cd: CountryData, real: bool = True,
               currency: str = "local", deflator: str = "cpi") -> pd.DataFrame:
    """Convert money columns: real (start-year prices; deflator "cpi" = consumer prices,
    "own" = what the household's own spending costs) and/or USD / intl $ at the
    start-year exchange rate / PPP (held constant)."""
    df = res.years.copy()
    skip = {"t", "year", "age1", "age2", "married", "n_children", "owned", "price_factor",
            "cost_index",
            "us_method", "unemployment_months", "local_tax_rate", "saving_rate",
            "budget_scale", "leave_months", "mortgage_rate", "child_penalty_factor",
            "retired"}
    money = [c for c in df.columns if c not in skip and pd.api.types.is_numeric_dtype(df[c])]
    div = (df["cost_index"] if deflator == "own" and "cost_index" in df else
           df["price_factor"]) if real else 1.0
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

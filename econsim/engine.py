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

from .model import CountryData, Plan
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
ANNUITY_YEARS = 15.0               # real annuity factor for pension rights (~19y at 2%)
CHURCH_TAX = {"DEU": 0.085}        # share of income tax (8% BY/BW, 9% elsewhere)
YOUNG_DRIVER = ((25, 1.7), (30, 1.25))
STUDENT_LOAN_RATE = 6.4            # % (US federal undergraduate 2025-26)
MOVING_PCT = 1.0                   # removals + furnishing a bigger home, % of the price
IRL_PRIVATE_HEALTH = (1600.0, 400.0)  # EUR/yr adult, child (VHI/Laya/Irish Life mid plan)


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

    infl_default = 2.0
    rw = cd.real_wage_growth_hist.value
    if cd.hh.get("real_wage_growth", float("nan")) == cd.hh.get("real_wage_growth", 0):
        rw_default = float(rw)                       # curated projection
    else:
        rw_default = float(min(max(rw, 0.0), 1.0)) if rw == rw else 0.5
    out = {
        "inflation": pick(plan.inflation, cd.inflation_hist.with_value(
            infl_default, source="assumption: 2% (central-bank target); "
            f"history {cd.inflation_hist.value:.1f}%/yr", estimated=True), "%"),
        "real_wage_growth": pick(plan.real_wage_growth, cd.real_wage_growth_hist.with_value(
            rw_default, estimated=True), "%"),
        "wedding_cost": pick(plan.wedding_cost, cd.wedding_cost),
        "home_price": pick(plan.home_price, cd.home_price),
        "down_payment_pct": pick(plan.down_payment_pct, cd.down_payment_pct, "%"),
        "transfer_tax_pct": pick(plan.transfer_tax_pct, cd.transfer_tax_pct, "%"),
        "mortgage_rate": pick(plan.mortgage_rate, cd.mortgage_rate, "%"),
        "rent": pick(plan.rent, cd.rent),
        "emergency_annual_cost": pick(plan.emergency_annual_cost, cd.emergency_expense_size),
    }
    g = out["real_wage_growth"]["value"]
    out["home_real_appreciation"] = (
        {"value": float(plan.home_real_appreciation), "source": "user input", "unit": "%"}
        if plan.home_real_appreciation is not None else
        {"value": g, "source": "= real wage growth (house prices and rents keep pace with "
         "incomes; long-run price-to-income ratios are stable)", "unit": "%",
         "estimated": True})
    return out


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
    r_inv = (plan.return_nominal - drag) / 100
    r_cash, r_debt = plan.cash_return / 100, plan.debt_rate / 100
    y0 = plan.start_year
    dy = cd.data_year
    cpi = cd.cpi or None

    def start(v, year=None):  # data-year money -> start-year money
        return roll(v, year if year is not None else dy, y0, infl * 100, cpi)

    e1, e2 = plan.earners
    n_years = max(plan.horizon_age - e1.age + 1, 1)
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

    def gross_at(i: int, age: int, t: int) -> float:
        e = plan.earners[i]
        if not e.works or age >= plan.retirement_age or age < 18:
            return 0.0
        base0 = interp_profile(prof[i], e.age)
        shape = interp_profile(prof[i], age) / base0 if base0 else 1.0
        rel = shape ** e.career_growth if shape > 0 else 0.0
        return start(base0, earn_year) * rel * ((1 + g_real) * (1 + infl)) ** t

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
        budget0[k] = val * plan.budget_scale
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
    rate_lr = _nan0(L.get("rate_longrun")) / 100 or m_rate_now
    fix_years = _nan0(L.get("fixation_years")) or (plan.mortgage_years or 25)
    tt_user = plan.transfer_tax_pct is not None
    dp_user = plan.down_payment_pct is not None

    def unemp_rate(age: int) -> float:
        y, tot = _nan0(cd.unemp_youth), _nan0(cd.unemp_total)
        if age < 25:
            return y or tot
        if age < 30:
            return (y + tot) / 2 if y else tot
        return tot

    cash = plan.starting_savings
    invest = 0.0
    owned = False
    home_value = mort_bal = mort_pay = 0.0
    mort_rate = m_rate_now
    mort_left = 0.0
    mort_age = 0
    loan0 = buy_price = 0.0
    ch_style = False
    fix_years_eff = fix_years
    sl_bal = plan.student_loan
    jl = plan.job_loss
    prev_recurring = 0.0
    prev_housing = 0.0
    rows = []
    last_normal_net = 0.0
    for t in range(n_years):
        a1, a2 = e1.age + t, e2.age + t
        ages = (a1, a2)
        P = (1 + infl) ** t
        W = ((1 + g_real) * (1 + infl)) ** t
        H = P * (1 + g_house) ** t
        aw = aw0 * W
        married = married_from_start or (marry_t is not None and t >= marry_t)
        kid_ages = [t - b for b in birth_t if b is not None and t >= b]
        n_kids = len(kid_ages)
        n_dep = sum(1 for k in kid_ages if k <= plan.child_dependent_to)
        n_ctc = sum(1 for k in kid_ages if k < 17)

        normal = [gross_at(0, a1, t), gross_at(1, a2, t)]

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
        normal_net = sum(net_i) + child_ben
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
                b = rr * net_i[i] / 12
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
                    unemp_cost += unemp_rate(ages[i]) / 100 * net_i[i] * paid_frac[i] * \
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
            - unemp_cost - us_tax

        # ---- expenses (nominal)
        scale = 1.0
        if ref0 and plan.budget_elasticity:
            if owned:
                housing_now = prev_housing
            else:
                housing_now = rent0 * H * (cd.rent_factor_per_child[min(n_dep, 2) - 1]
                                           if n_dep else 1.0)
            y_perm = (normal_net - child_ben - (housing_now if cd.budget_ref_housing
                                                is not None else 0.0)) / P
            scale = min(max((y_perm / ref0) ** plan.budget_elasticity, 0.5), 2.0) \
                if y_perm > 0 else 0.5
        exp = {f"budget:{k}": v * P * scale for k, v in budget0.items()}
        # children: OECD-modified equivalence scale (couple 1.5; child <14 +0.3, 14+ +0.5)
        # applied to the couple's non-housing living budget; housing via the rent factor,
        # childcare, health and cars separately
        if hh:
            w = sum(0.3 / 1.5 if k < 14 else 0.5 / 1.5 for k in kid_ages
                    if k <= plan.child_dependent_to)
            exp["children"] = sum(budget0.values()) * P * scale * w
        else:
            exp["children"] = child_spend0 * P * n_dep * scale
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
        exp["childcare"] = cc * P
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
                if k <= plan.child_dependent_to:
                    hc += _nan0(hh.get("health_premium_child")) * 12 * have_health + \
                        _nan0(hh.get("health_oop_child")) * have_oop
            if plan.private_health and cd.iso3 == "IRL":
                hc += 2 * IRL_PRIVATE_HEALTH[0] + n_dep * IRL_PRIVATE_HEALTH[1]
            exp["health"] = start(hc, 2025) * P
        # cars and public transport
        if have_cars:
            car = 0.0
            for k in range(n_cars):
                age = ages[min(k, 1)]
                f = next((m for lim, m in YOUNG_DRIVER if age < lim), 1.0)
                car += car_fixed0 + car_ins0 * f
            exp["cars"] = car * P
            exp["public_transport"] = max(2 - n_cars, 0) * transit0 * P
        exp["emergencies"] = emerg0 * P
        # student loan
        if sl_bal > 0:
            if cd.iso3 == "GBR":
                pay = sum(0.09 * max(g - 25_000 * P, 0) for g in gross)
                sl_bal *= 1 + infl
            else:
                pay = annuity(plan.student_loan, STUDENT_LOAN_RATE / 100, 10)
                sl_bal *= 1 + STUDENT_LOAN_RATE / 100
            pay = min(pay, sl_bal)
            sl_bal -= pay
            if cd.iso3 == "GBR" and t >= 40:
                sl_bal = 0.0
            exp["student_loan"] = pay
        one_off = {}
        if marry_t is not None and t == marry_t and not married_from_start:
            one_off["wedding"] = wedding0 * P
            events.append({"t": t, "age": a1, "event": "Marriage"})
        for k, b in enumerate(birth_t):
            if b is not None and t == b:
                events.append({"t": t, "age": a1, "event": f"Child {k + 1}"})
                bc = _nan0(hh.get("birth_cost"))
                if bc:
                    one_off[f"birth_{k + 1}"] = start(bc, 2025) * P

        # ---- housing
        rent_f = 1.0
        if n_dep:
            rent_f = cd.rent_factor_per_child[min(n_dep, 2) - 1]
        if not owned and buy_t_plan is not None and t >= buy_t_plan:
            price = price0 * H
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
            rate = m_rate_now
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
            home_value *= (1 + infl) * (1 + g_house)
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
        prev_recurring = recurring
        prev_housing = (interest + exp.get("owner_costs", 0.0) + exp.get("mortgage_insurance", 0.0)
                        + exp.get("mortgage_interest_relief", 0.0)) * (1 + infl)
        total_out = recurring + sum(one_off.values())
        saving = net - total_out

        # ---- balances: emergency fund (cash) first, then investments
        target = plan.emergency_fund_months * recurring / 12
        cash *= 1 + (r_cash if cash >= 0 else r_debt)
        invest *= 1 + r_inv
        match = _nan0(hh.get("employer_match_pct")) / 100 * sum(gross)
        invest += match
        cash += saving
        if cash > target:
            invest += cash - target
            cash = target
        elif cash < 0 and invest > 0:
            take = min(-cash, invest)
            invest -= take
            cash += take
        rows.append({
            "t": t, "year": y0 + t, "age1": a1, "age2": a2, "married": married,
            "n_children": n_kids, "owned": owned, "price_factor": P,
            "gross1": gross[0], "gross2": gross[1], "gross": sum(gross),
            "income_tax_local": sum(it), "ssc_local": sum(ssc), "pension_contrib": sum(pen),
            "us_tax": us_tax, "us_method": us_method, "child_benefits": child_ben,
            "leave_benefits": leave_ben, "leave_months": sum(leave_m),
            "unemployment_months": unemp_months, "job_loss_benefits": jl_ben,
            "unemployment_risk_cost": unemp_cost, "employer_pension": match,
            "budget_scale": scale,
            "net1": gross[0] - it[0] - ssc[0] - pen[0], "net2": gross[1] - it[1] - ssc[1] - pen[1],
            "net_income": net, **exp, **{f"oneoff:{k}": v for k, v in one_off.items()},
            "expenses_recurring": recurring, "expenses_total": total_out, "saving": saving,
            "cash": cash, "invest": invest, "liquid": cash + invest, "home_value": home_value
            if owned else 0.0, "mortgage_balance": mort_bal if owned else 0.0,
            "mortgage_interest": interest, "mortgage_rate": mort_rate * 100 if owned else 0.0,
        })
    df = pd.DataFrame(rows).fillna(0.0)
    df["home_equity"] = df["home_value"] - df["mortgage_balance"]
    df["net_worth"] = df["liquid"] + df["home_equity"]
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
    # pension rights at retirement (start-year money, real)
    nrr_p = _nan0(hh.get("pension_nrr")) / 100
    pension = nrr_p * last_normal_net
    summary = {"pension_income_real": pension, "pension_wealth_real": pension * ANNUITY_YEARS,
               "pension_nrr": nrr_p * 100}
    return Result(df, events, warnings, inp, summary)


def to_display(res: Result, cd: CountryData, real: bool = True,
               currency: str = "local") -> pd.DataFrame:
    """Convert money columns: real (start-year prices) and/or USD / intl $ at the
    start-year exchange rate / PPP (held constant)."""
    df = res.years.copy()
    skip = {"t", "year", "age1", "age2", "married", "n_children", "owned", "price_factor",
            "us_method", "unemployment_months", "local_tax_rate", "saving_rate",
            "budget_scale", "leave_months", "mortgage_rate"}
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

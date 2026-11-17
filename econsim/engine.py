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

from .assumptions import (
    BORROW_LIMIT,
    CAR_ELASTICITY,
    CH_STYLE,
    CHURCH_TAX,
    FPL_2025,
    HOME_DRIFT,
    HOUSING_ELASTICITY,
    MOVING_PCT,
    PER_PERSON_CATS,
    PORTFOLIO_K,
    PRICE_TREND_YEARS,
    REFI_COUNTRIES,
    WEALTH_MPC,
    WEALTH_MPC_DECLINE,
    YOUNG_DRIVER,
)
from .career import RANK_PERSISTENCE
from .cohort import from_rel, to_rel
from .household import (
    childcare_need,
    cut_extras,
    cut_living_costs,
    earnings_setup,
    health_cost,
    job_loss_months,
    leave_months,
    leave_schedule,
    life_events,
    living_budget,
    loan_limits,
    mortgage_terms,
    mortgage_year,
    new_loan_rate,
    own_cost_index,
    owner_costs,
    pension_contribution,
    price_paths,
    purchase_tax,
    spending_scale,
    student_loan_year,
    summarise,
)
from .inputs import edu_pay_factor, resolve, unemployment_rate_for
from .model import CountryData, Plan
from .money import annuity, interp_nrr, interp_profile, nan0, nrr_for_month, roll
from .tax import (
    USParams,
    household_income_tax,
    local_rates,
    us_expat_tax,
    us_fica,
    us_resident_tax,
    youth_tax_factor,
)


@dataclass
class Result:
    years: pd.DataFrame  # one row per simulated year, nominal local
    events: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)  # resolved inputs with provenance
    summary: dict = field(default_factory=dict)  # pension rights etc. (start-year money)
    career: list = field(default_factory=list)  # career.CareerPath per earner (US)


def simulate(plan: Plan, cd: CountryData, us: USParams | None = None, history=None) -> Result:
    """history: an econsim.history.History - what actually happened from the start year
    (prices, wages, home prices, rents, rates, returns, unemployment) in place of the
    forecasts; levels (budget, rents, home prices) are taken back to the start year by
    their own price indices. For backtests."""
    return _Simulation(plan, cd, us, history).run()


class _Year:
    """What one simulated year has worked out so far (its ages, price levels, incomes,
    taxes and spending), passed from one stage of the year to the next."""

    def __init__(self, t: int, ages: tuple[int, int]):
        self.t = t
        self.a1, self.a2 = ages
        self.ages = ages


class _Simulation:
    """One household from the start age to the horizon. __init__ resolves the inputs and
    sets up everything fixed for the whole run (price paths, pay, the living budget,
    lending rules, the starting balances); `_year` then runs one year, stage by stage,
    and moves the balances on."""

    # ------------------------------------------------------------------ setup
    def __init__(self, plan: Plan, cd: CountryData, us: USParams | None, history):
        self.plan, self.cd, self.us, self.HS = plan, cd, us, history
        inp = self.inp = resolve(plan, cd)
        self.infl = inp["inflation"]["value"] / 100
        self.g_real = inp["real_wage_growth"]["value"] / 100
        g_house = inp["home_real_appreciation"]["value"] / 100
        self.hh, self.L = cd.hh or {}, cd.lend or {}
        self.drag = nan0(self.hh.get("invest_tax_drag"))
        self.r_inv = (inp["return_nominal"]["value"] - self.drag) / 100
        self.r_cash, self.r_debt = inp["cash_return"]["value"] / 100, plan.debt_rate / 100
        self.r_dep = inp["deposit_return"]["value"] / 100
        self.dep_share = min(max(inp["deposit_share"]["value"] / 100, 0.0), 1.0)
        self.eq_share = inp["equity_share"]["value"] / 100
        self.sl_rate = inp["student_loan_rate"]["value"] / 100
        self.sl_years = max(int(inp["student_loan_years"]["value"]), 1)
        self.ret_age = int(round(inp["retirement_age"]["value"]))
        self.horizon = int(round(inp["horizon_age"]["value"]))
        home_tr = inp.get("home_trend_10y", {}).get("value")
        rent_tr = inp.get("rent_trend_10y", {}).get("value", home_tr)
        self.y0 = plan.start_year
        self.dy = cd.data_year
        self.cpi = cd.cpi or None

        self.e1, self.e2 = plan.earners
        self.n_years = max(self.horizon - self.e1.age + 1, 1)
        self.INF, self.Pt, self.GW, self.Hh, self.Hr = price_paths(
            self.n_years,
            self.infl,
            inp.get("inflation_next_5y", {}).get("value"),
            self.g_real,
            self.HS,
            home_tr,
            rent_tr,
            g_house,
        )
        self.warnings: list[str] = []
        self.events: list[dict] = []
        self.rows: list[dict] = []
        self.us_resident = cd.iso3 == "USA" and us is not None
        self.coh, self.cmode = cd.cohort, plan.cohort_effects
        self.prof, self.pay_g, self.paths, self.cdat, self.pay_k = earnings_setup(
            plan, cd, inp, self.n_years, self.start, self.warnings
        )
        self.earn_year = cd.earnings_src.year
        self.first_birth = min(
            (a - self.e1.age for a in plan.child_ages if a is not None), default=None
        )
        self.aw0 = self.start(cd.avg_wage.value, cd.avg_wage.year)
        self._setup_costs()
        self._setup_family()
        self._setup_housing()
        self._setup_balances()

    def start(self, v, year=None):
        """Data-year money -> start-year money (back to a past start year with the actual
        CPI in backtests)."""
        if self.HS is not None:
            return (
                roll(v, year if year is not None else self.dy, self.dy, self.infl * 100, self.cpi)
                * self.HS.cpi_level
            )
        return roll(v, year if year is not None else self.dy, self.y0, self.infl * 100, self.cpi)

    def _setup_costs(self) -> None:
        """The living budget (survey lines minus what the car and health modules
        replace), the survey couple's income it is scaled against, and every other cost
        in start-year money - taken back to their own price levels in backtests."""
        plan, cd, inp, hh, start = self.plan, self.cd, self.inp, self.hh, self.start
        self.have_cars = bool(hh) and nan0(hh.get("car_price")) > 0
        self.have_health = bool(hh) and nan0(hh.get("health_premium_adult")) > 0
        self.have_oop = bool(hh) and hh.get("health_oop_adult", float("nan")) == hh.get(
            "health_oop_adult", 0
        )
        self.lvl = (
            plan.spending_level
            if plan.spending_level is not None
            else (
                float(cd.spending_level.value)
                if plan.spending_fit and cd.spending_level is not None
                else 1.0
            )
        )
        self.budget0 = living_budget(
            cd, plan, start, self.have_cars, self.have_health, self.have_oop
        )
        ref = cd.budget_ref_income
        self.ref0 = None
        bp = next(iter(cd.budget.values())).year if cd.budget else self.dy
        self.ref_all0 = None
        if ref is not None:  # ref.value is already in the budget's price year
            self.ref0 = self.ref_all0 = start(ref.value, bp)
            if cd.budget_ref_housing is not None:  # income left after housing costs
                self.ref0 -= start(cd.budget_ref_housing.value, bp) * 12
        self.rent0 = start(inp["rent"]["value"], inp["rent"]["year"]) * 12
        self.child_spend0 = start(cd.child_extra_spending.value, cd.child_extra_spending.year) * 12
        self.childcare0 = start(cd.childcare_per_child.value, cd.childcare_per_child.year)
        self.child_ben0 = (
            0.0
            if self.us_resident
            else start(cd.child_benefit_per_child.value, cd.child_benefit_per_child.year)
        )
        self.emerg0 = start(
            inp["emergency_annual_cost"]["value"], inp["emergency_annual_cost"]["year"]
        )
        self.wedding0 = start(inp["wedding_cost"]["value"], inp["wedding_cost"]["year"])
        self.price0 = start(inp["home_price"]["value"], inp["home_price"]["year"])

        def hp25(v):  # curated params: 2025 money
            return start(nan0(v), 2025)

        # cars (per car, per year, start-year money)
        self.n_cars = plan.cars if plan.cars is not None else int(nan0(hh.get("cars")) or 1)
        self.car_fixed0 = self.car_ins0 = self.transit0 = 0.0
        if self.have_cars:
            self.car_fixed0 = hp25(
                0.10 * nan0(hh["car_price"])
                + nan0(hh["car_tax"])
                + nan0(hh["km_year"]) * nan0(hh["l_per_100km"]) / 100 * nan0(hh["fuel_price"])
                + nan0(hh["car_maintenance"])
                + nan0(hh["parking_tolls"])
            )
            self.car_ins0 = hp25(hh["car_insurance"])
            self.transit0 = hp25(hh["transit_pass"]) * 12
        self.hc_k = 1.0
        HS = self.HS
        if HS is not None:  # back then: each price vs the CPI, spending norms vs real wages
            RL = HS.rel_level.get
            self.aw0 *= HS.wage_level
            for k in self.budget0:
                self.budget0[k] *= RL(k, 1.0) / HS.wage_level
            if self.ref0:
                self.ref0 /= HS.wage_level
                self.ref_all0 /= HS.wage_level
            self.rent0 *= HS.rent_level
            self.price0 *= HS.home_level
            self.childcare0 *= RL("childcare", 1.0)
            self.child_spend0 /= HS.wage_level
            self.car_fixed0 *= RL("vehicle_running", 1.0)
            self.car_ins0 *= RL("insurance_vehicle", 1.0)
            self.transit0 *= RL("transport_services", 1.0)
            self.hc_k = RL("insurance_health", 1.0)
        # current price trends: each cost's last-10-year trend vs prices in general
        # continues for the next 10 years (then it moves with prices / wages like the rest)
        self.PTR = cd.price_trends if plan.price_outlook == "trend" else {}

    def _setup_family(self) -> None:
        """Leave, benefits, marriage, children and the plan's other life events."""
        plan, cd, e1 = self.plan, self.cd, self.e1
        self.leave = leave_schedule(
            self.n_years,
            [None if a is None else a - e1.age for a in plan.child_ages],
            self.hh,
            plan,
        )
        self.nrr = {m: s.value for m, s in cd.nrr.items()}
        self.nrr6 = nrr_for_month(self.nrr, 6) if self.nrr else 0.6
        self.fx = cd.lcu_per_usd.value
        self.abroad = plan.us_citizens and cd.iso3 != "USA" and self.us is not None
        self.birth_t = [None if a is None else a - e1.age for a in plan.child_ages]
        self.marry_t = None if plan.marriage_age is None else plan.marriage_age - e1.age
        self.married_from_start = plan.marriage_age is not None and plan.marriage_age <= e1.age
        # children live at home (and cost money) until the country's average leaving age
        if plan.child_dependent_to is not None:
            self.dep_to = plan.child_dependent_to
        elif cd.leave_home_age is not None and cd.leave_home_age.value == cd.leave_home_age.value:
            self.dep_to = max(int(round(cd.leave_home_age.value)), 18)
        else:
            self.dep_to = 18
        self.pp = plan.primary_parent
        self.gift_t, self.inh_t, self.uni0, self.ltc0, self.ltc_from = life_events(
            plan, cd, self.start, e1.age, self.n_years
        )
        # retirement: career record for the pension, then price-indexed payments
        self.nrr_tab = dict(cd.pension_nrr or {})
        if not self.nrr_tab and nan0(self.hh.get("pension_nrr")):
            self.nrr_tab = {1.0: nan0(self.hh.get("pension_nrr"))}
        self.rel_sum, self.rel_n, self.nr_sum = [0.0, 0.0], [0, 0], [0.0, 0.0]
        self.pension0 = [None, None]  # (nominal payment in its first year, price level then)
        self.jl = plan.job_loss

    def _setup_housing(self) -> None:
        """Lending rules (plan defaults where the country has no curated rules) and a home
        already owned at the start."""
        plan, cd, inp = self.plan, self.cd, self.inp
        self.buy_t_plan = None if plan.buy_house_age is None else plan.buy_house_age - self.e1.age
        self.why_not = None  # why the planned home is not yet affordable
        self.m_rate_now = inp["mortgage_rate"]["value"] / 100
        self.rate_lr = inp["mortgage_rate_after_fixation"]["value"] / 100 or self.m_rate_now
        self.fix_years = nan0(self.L.get("fixation_years")) or (plan.mortgage_years or 25)
        self.tt_user = plan.transfer_tax_pct is not None
        self.dp_user = plan.down_payment_pct is not None
        self.owned = False
        self.home_value = self.mort_bal = self.mort_pay = 0.0
        self.mort_rate = self.m_rate_now
        self.mort_left = 0.0
        self.mort_age = 0
        self.loan0 = self.buy_price = 0.0
        self.ch_style = False
        self.fix_years_eff = self.fix_years
        if inp["own_home"]["value"]:  # already own: today's value, balance, rate, term left
            self.owned = True
            self.home_value = self.buy_price = self.start(
                inp["home_value_now"]["value"], self.sw_year("home_value_now")
            )
            self.mort_bal = self.loan0 = self.start(
                inp["mortgage_now"]["value"], self.sw_year("mortgage_now")
            )
            self.mort_rate = inp["mortgage_rate_now"]["value"] / 100
            self.mort_left = inp["mortgage_years_left"]["value"]
            self.fix_years_eff = 99 if cd.iso3 in ("USA", "DNK") else max(self.fix_years / 2, 1)
            self.mort_pay = (
                annuity(self.mort_bal, self.mort_rate, max(self.mort_left, 1))
                if self.mort_bal > 0
                else 0.0
            )

    def sw_year(self, k: str) -> int:
        return self.inp[k].get("year") or self.y0

    def _setup_balances(self) -> None:
        """Starting savings, student loans and how savings are invested."""
        plan, cd, inp = self.plan, self.cd, self.inp
        self.h_scale0 = 1.0
        self.cash = self.start(inp["starting_savings"]["value"], self.sw_year("starting_savings"))
        self.retire = self.start(
            inp["starting_retirement"]["value"], self.sw_year("starting_retirement")
        )
        self.invest = 0.0
        self.sl0 = plan.student_loan
        if plan.typical_student_debt and not self.sl0:
            for e in plan.earners:
                d = (cd.student_debt or {}).get(e.education or "")
                if d is not None and e.age <= 30:
                    self.sl0 += self.start(d.value, d.year)
        self.sl_bal = self.sl0
        self.prev_recurring = 0.0
        self.prev_housing = 0.0
        self.tgt_prev = 0.0
        self.r_debt_now = self.r_debt
        # savings are held as typical households hold them, moving toward the whole
        # household sector's mix (wealth-weighted: more in shares, less in deposits) as
        # they grow
        self.pf_k = plan.portfolio_wealth_k if plan.portfolio_wealth_k is not None else PORTFOLIO_K
        PF_ = cd.portfolio or {}
        self.pf_hi = None
        if (
            self.pf_k
            and plan.equity_share is None
            and plan.deposit_share is None
            and PF_.get("deposit_share_typical") is not None
            and PF_.get("deposit_share") is not None
            and PF_.get("equity_share") is not None
        ):
            self.pf_hi = (float(PF_["equity_share"]), float(PF_["deposit_share"]))
        self.eq_now, self.dep_now = self.eq_share, self.dep_share

    # ------------------------------------------------------------------ lookups
    def pay_f(self, i: int, age: float) -> float:
        e = self.plan.earners[i]
        return edu_pay_factor(self.cd, e.education, age) if self.plan.education_pay else 1.0

    def gross_at(self, i: int, age: int, t: int) -> float:
        """Earner i's usual gross pay at this age (before leave, job loss and the family
        employment factor)."""
        plan = self.plan
        e = plan.earners[i]
        if not e.works or age >= self.ret_age or age < 18:
            return 0.0
        pen_w = 1.0
        if e.pay_path is not None:  # given pay (backtests: what it was)
            return float(e.pay_path[min(t, len(e.pay_path) - 1)])
        if (
            i == plan.primary_parent
            and plan.child_wage_penalty
            and self.first_birth is not None
            and t >= self.first_birth
        ):
            pen_w = 1 - plan.child_wage_penalty / 100  # lasting hourly-pay penalty
        GW, Pt = self.GW, self.Pt
        if self.paths[i] is not None:
            pay = self.paths[i].pay
            return self.start(pay[min(t, len(pay) - 1)], self.cdat.year) * pen_w * GW[t] * Pt[t]
        prof = self.prof
        base0 = interp_profile(prof[i], e.age) * self.pay_f(i, e.age)
        shape = interp_profile(prof[i], age) * self.pay_f(i, age) / base0 if base0 else 1.0
        pay_g = self.pay_g
        if pay_g[i] is not None and pay_g[i][1] is not None:  # the cohort's lasting gap
            shape *= pay_g[i][1][min(t, len(pay_g[i][1]) - 1)]
        rel = shape**e.career_growth if shape > 0 else 0.0
        k = 1 + (self.pay_k[i] - 1) * RANK_PERSISTENCE**t  # stated pay fades to the profile
        return self.start(base0, self.earn_year) * k * rel * pen_w * GW[t] * Pt[t]

    def unemp_rate(self, age: int, education: str | None = None, i: int | None = None) -> float:
        u = self._unemp_rate(age, education, i)
        HS = self.HS
        if HS is not None and i is not None:
            t = age - self.plan.earners[i].age
            u *= HS.unemp_factor[min(max(t, 0), len(HS.unemp_factor) - 1)]
        return u

    def _unemp_rate(self, age: int, education: str | None = None, i: int | None = None) -> float:
        plan, cd = self.plan, self.cd
        if i is not None and self.paths[i] is not None:
            pth = self.paths[i]
            return (
                100 * pth.unemployment[min(age - plan.earners[i].age, len(pth.unemployment) - 1)]
            )
        v, _ = unemployment_rate_for(cd, age, education)
        if v is not None:  # by age band and degree (labour_market.csv)
            if i is not None and self.coh is not None and self.cmode != "off":
                a_ = self.coh.adjustment(
                    "unemp", education, plan.earners[i].age, age - plan.earners[i].age, self.cmode
                )
                if a_:
                    v = 100 * float(from_rel(to_rel(v / 100, 0.5, "logit") + a_, 0.5, "logit"))
            return v
        y, tot = nan0(cd.unemp_youth), nan0(cd.unemp_total)
        if age < 25:
            return y or tot
        if age < 30:
            return (y + tot) / 2 if y else tot
        return tot

    def family_factor(self, table: dict, kid_ages_now: list[int]) -> float:
        """Primary parent's typical work vs no children, by number of children and the
        youngest's age (1 once the youngest is 18)."""
        plan = self.plan
        if not plan.child_penalty or not table or not kid_ages_now:
            return 1.0
        y = min(kid_ages_now)
        if y >= 18:
            return 1.0
        band = "lt6" if y < 6 else "6-11" if y < 12 else "ge12"
        n = str(min(len(kid_ages_now), 3))
        edu = plan.earners[self.pp].education
        e = {"short_tertiary": "tertiary", "bachelor": "tertiary", "master_plus": "tertiary"}.get(
            edu or "all", edu or "all"
        )
        for key in (
            (n, band, e),
            (n, band, "all"),
            ("2", band, "all"),
            ("1", band, "all"),
            (n, "6-11" if band == "ge12" else band, "all"),
        ):
            if key in table:
                return min(table[key], 1.0)
        return 1.0

    def RT(self, k: str, t: int) -> float:
        """A cost's price vs prices in general: what it did (backtests) or its trend."""
        HS = self.HS
        if HS is not None and k in HS.rel:
            v = HS.rel[k][min(t, len(HS.rel[k]) - 1)]
            if v == v:
                return v
        r = self.PTR.get(k)
        return (1 + min(max(r[0], -4.0), 4.0) / 100) ** min(t, PRICE_TREND_YEARS) if r else 1.0

    def RT_w(self, k: str, t: int) -> float:
        """For costs that already rise with wages: only the part of the trend beyond."""
        HS = self.HS
        if HS is not None and k in HS.rel:
            v = HS.rel[k][min(t, len(HS.rel[k]) - 1)]
            if v == v:
                return v / self.GW[t]  # what that price actually did
        r = self.PTR.get(k)
        if not r:
            return 1.0
        x = (1 + min(r[0], 4.0) / 100) / (1 + self.g_real)
        return max(x, 1.0) ** min(t, PRICE_TREND_YEARS)

    def grow_cash(self, c: float) -> float:
        """One year's interest on cash: the emergency fund earns the overnight rate,
        deposits above it the household deposit rate; overdrafts cost the debt rate."""
        if c < 0:
            return c * (1 + self.r_debt_now)
        b = min(c, self.tgt_prev)
        return c + b * self.r_cash + (c - b) * self.r_dep

    def taxes(
        self, y: _Year, gross: list[float], interest: float = 0.0, prop_tax: float = 0.0
    ) -> tuple[list[float], list[float]]:
        """Income tax and social contributions per earner (US residents: the federal and
        state system, mortgage interest and property tax itemised)."""
        cd, plan, t = self.cd, self.plan, y.t
        if self.us_resident:
            p_us = self.us.indexed(
                (self.HS.cpi_level if self.HS is not None else (1 + self.infl) ** (self.y0 - 2026))
                * self.Pt[t]
            )
            fx = self.fx
            r = us_resident_tax(
                [g / fx for g in gross],
                y.married,
                y.n_ctc,
                y.n_dep,
                p_us,
                cd.state_tax,
                interest / fx,
                prop_tax / fx,
            )
            tot = sum(gross) or 1.0
            return (
                [r.income_tax * fx * g / tot for g in gross],
                [r.ssc * fx * g / tot for g in gross],
            )
        it, ssc = zip(*[local_rates(g, y.aw, cd.tax_points, cd.marginal_rate_top) for g in gross])
        it = household_income_tax(
            cd.iso3, list(gross), y.married, y.n_dep, y.aw, cd.tax_points, cd.marginal_rate_top
        )
        it = [
            x
            * youth_tax_factor(
                cd.iso3, a, g, y.aw, max(a - 23, 0), i == plan.primary_parent and y.n_kids >= 2
            )
            for i, (x, a, g) in enumerate(zip(it, y.ages, gross))
        ]
        if plan.church_tax and cd.iso3 in CHURCH_TAX:
            it = [x * (1 + CHURCH_TAX[cd.iso3]) for x in it]
        return list(it), list(ssc)

    # ------------------------------------------------------------------ one year
    def run(self) -> Result:
        for t in range(self.n_years):
            self.rows.append(self._year(t))
        return self._result()

    def _year(self, t: int) -> dict:
        y = _Year(t, (self.e1.age + t, self.e2.age + t))
        self._prices_and_family(y)
        self._transfers(y)
        self._usual_income(y)
        self._actual_income(y)
        self._living_costs(y)
        self._one_offs(y)
        self._housing(y)
        self._wealth_tax(y)
        self._balance_budget(y)
        self._move_balances(y)
        return self._row(y)

    def _prices_and_family(self, y: _Year) -> None:
        """This year's price levels, investment returns (glide path; backtests: what
        markets did), marriage and children."""
        t, plan, HS = y.t, self.plan, self.HS
        y.P = self.Pt[t]
        y.W = self.GW[t] * self.Pt[t]
        # glide path: the share in shares falls over the 15 years before retirement to
        # half of it (as target-date funds and most retirees' portfolios)
        y.r_inv = self.r_inv + (self.eq_now - self.eq_share) * plan.equity_premium / 100
        if plan.glide_path:
            k = min(max((y.a1 - (self.ret_age - 15)) / 15, 0.0), 1.0)
            y.r_inv -= 0.5 * k * self.eq_now * plan.equity_premium / 100
        else:
            k = 0.0
        if HS is not None and t < len(HS.years) and HS.equity[t] == HS.equity[t]:
            eq_t = self.eq_now * (1 - 0.5 * k)  # what markets actually returned
            bond = HS.bond[t] if HS.bond[t] == HS.bond[t] else HS.cash[t]
            y.r_inv = (eq_t * HS.equity[t] + (1 - eq_t) * bond - plan.fund_fees - self.drag) / 100
            self.r_cash = HS.cash[t] / 100 if HS.cash[t] == HS.cash[t] else self.r_cash
            self.r_dep = HS.deposit[t] / 100 if HS.deposit[t] == HS.deposit[t] else self.r_dep
        y.H = y.P * self.Hr[t]  # rents
        y.HH = y.P * self.Hh[t]  # home prices
        y.aw = self.aw0 * y.W
        y.married = self.married_from_start or (self.marry_t is not None and t >= self.marry_t)
        y.kid_ages = [t - b for b in self.birth_t if b is not None and t >= b]
        y.n_kids = len(y.kid_ages)
        y.n_dep = sum(1 for k in y.kid_ages if k <= 18)  # benefits and taxes
        y.n_home = sum(1 for k in y.kid_ages if k <= self.dep_to)  # living at home (costs)
        y.n_ctc = sum(1 for k in y.kid_ages if k < 17)

    def _transfers(self, y: _Year) -> None:
        """A family gift (to cash) or an inheritance (invested) this year."""
        plan, t = self.plan, y.t
        y.transfers = 0.0
        if self.gift_t is not None and t == self.gift_t:
            y.transfers += plan.family_gift * y.P
            self.cash += plan.family_gift * y.P
            self.events.append({"t": t, "age": y.a1, "event": "Family gift"})
        if self.inh_t is not None and t == self.inh_t:
            y.transfers += plan.inheritance * y.P
            self.invest += plan.inheritance * y.P
            self.events.append({"t": t, "age": y.a1, "event": "Inheritance"})

    def _usual_income(self, y: _Year) -> None:
        """Pay, taxes and net income without leave or job loss, child benefits and
        pensions: career-average earnings relative to the average wage (incl. the child
        penalty) x the OECD net replacement rate at that level, from the retirement age;
        then indexed to prices."""
        t, a1 = y.t, y.a1
        y.normal = [self.gross_at(0, a1, t), self.gross_at(1, y.a2, t)]
        y.fam_k = self.family_factor(self.cd.family_earn, y.kid_ages)
        y.normal[self.pp] *= y.fam_k
        it_n, ssc_n = self.taxes(y, y.normal)
        pen_n = [
            pension_contribution(self.cd.iso3, g, a, self.hh) for g, a in zip(y.normal, y.ages)
        ]
        y.net_i = [g - i - s - p for g, i, s, p in zip(y.normal, it_n, ssc_n, pen_n)]
        y.child_ben = self.child_ben0 * y.P * y.n_dep * self.GW[t]
        pen_inc = [0.0, 0.0]
        for i in (0, 1):
            if y.normal[i] > 0:
                self.rel_sum[i] += y.normal[i] / y.aw
                self.rel_n[i] += 1
                self.nr_sum[i] += y.net_i[i] / y.normal[i]
            elif y.ages[i] >= self.ret_age and self.rel_n[i] and self.nrr_tab:
                if self.pension0[i] is None:
                    rel = self.rel_sum[i] / self.rel_n[i]
                    rate_p = interp_nrr(self.nrr_tab, rel) / 100
                    self.pension0[i] = (rate_p * rel * y.aw * self.nr_sum[i] / self.rel_n[i], y.P)
                    self.events.append({"t": t, "age": a1, "event": f"Partner {i + 1} retires"})
                pen_inc[i] = self.pension0[i][0] * y.P / self.pension0[i][1]
        y.pension_income = sum(pen_inc)
        y.normal_net = sum(y.net_i) + y.child_ben + y.pension_income

    def _actual_income(self, y: _Year) -> None:
        """Parental leave and job loss (months without pay, replaced by benefits), the
        expected cost of unemployment, US tax for citizens abroad - and the net income."""
        plan, cd, t, jl = self.plan, self.cd, y.t, self.jl
        y.paid_frac, y.leave_ben, y.leave_m = leave_months(
            self.leave, t, y.normal, y.net_i, self.pp, y.fam_k, y.W
        )
        y.unemp_months, y.jl_ben = 0, 0.0
        if jl is not None and y.normal[jl.earner] > 0:
            start_t = jl.age - self.e1.age
            y.unemp_months, y.jl_ben = job_loss_months(
                jl, t, start_t, self.nrr, y.normal_net, y.net_i[jl.earner]
            )
            if y.unemp_months and t == start_t:
                self.events.append(
                    {
                        "t": t,
                        "age": y.a1,
                        "event": f"Job loss (partner {jl.earner + 1}, {jl.months} mo)",
                    }
                )
        y.gross = [g * f for g, f in zip(y.normal, y.paid_frac)]
        # mortgage interest and property tax (owned from an earlier year) for itemising (US)
        interest_prev = self.rows[-1]["mortgage_interest"] if self.rows and self.owned else 0.0
        ptax_prev = (
            self.home_value * nan0(self.L.get("property_tax_pct")) / 100 if self.owned else 0.0
        )
        y.it, y.ssc = self.taxes(y, y.gross, interest_prev, ptax_prev)
        y.pen = [pension_contribution(cd.iso3, g, a, self.hh) for g, a in zip(y.gross, y.ages)]
        if y.unemp_months:  # the earner's pay, taxes and contributions stop for those months
            j, keep = jl.earner, max(y.paid_frac[jl.earner] - y.unemp_months / 12, 0)
            f = keep / y.paid_frac[j] if y.paid_frac[j] else 0.0
            y.gross[j] *= f
            y.it[j] *= f
            y.ssc[j] *= f
            y.pen[j] *= f
            y.paid_frac[j] = keep
        # expected cost of unemployment (risk-weighted, outside a simulated job loss)
        y.unemp_cost = 0.0
        if plan.unemployment_risk:
            for i in (0, 1):
                if y.gross[i] > 0 and not (jl is not None and i == jl.earner and y.unemp_months):
                    y.unemp_cost += (
                        self.unemp_rate(y.ages[i], plan.earners[i].education, i)
                        / 100
                        * y.net_i[i]
                        * y.paid_frac[i]
                        * (1 - self.nrr6)
                    )
        # US citizens abroad
        y.us_tax = 0.0
        y.us_method = ""
        if self.abroad:
            fx = self.fx
            f_us = (
                self.HS.cpi_level if self.HS is not None else (1 + self.infl) ** (self.y0 - 2026)
            ) * self.Pt[t]
            p_us = self.us.indexed(f_us)
            housing_usd = (self.rent0 * y.H / fx) if not self.owned else 0.0
            res = us_expat_tax(
                [g / fx for g in y.gross],
                [x / fx for x in y.it],
                y.married,
                y.n_ctc,
                housing_usd,
                p_us,
            )
            y.us_tax = res.residual * fx
            y.us_method = res.method
            if plan.us_employer and not cd.totalization:
                y.us_tax += sum(us_fica(g / fx, p_us) for g in y.gross) * fx
        y.net = (
            sum(y.gross)
            - sum(y.it)
            - sum(y.ssc)
            - sum(y.pen)
            + y.child_ben
            + y.leave_ben
            + y.jl_ben
            - y.unemp_cost
            - y.us_tax
            + y.pension_income
        )
        y.retired = sum(y.normal) == 0 and min(y.ages) >= self.ret_age

    def _living_costs(self, y: _Year) -> None:
        """The year's regular spending (nominal): the living budget scaled with income,
        children, childcare, university, care, health, cars, emergencies, student loans."""
        plan, cd, hh, t, P = self.plan, self.cd, self.hh, y.t, y.P
        G = y.G = self.GW[t]  # living standards rise with economy-wide real wages
        y.scale = G
        rel_inc = 1.0  # income vs the survey couple's (same year)
        if self.ref0 and plan.budget_elasticity:
            if self.owned:
                housing_now = self.prev_housing
            else:
                housing_now = (
                    self.rent0
                    * y.H
                    * (cd.rent_factor_per_child[min(y.n_home, 2) - 1] if y.n_home else 1.0)
                )
            y_perm = (
                y.normal_net
                - y.child_ben
                - (housing_now if cd.budget_ref_housing is not None else 0.0)
            ) / P
            if y.retired and plan.spend_down:
                # retirees also live off their savings, but draw them down slowly: about
                # 1 / years left, as US minimum distributions (which most retirees'
                # withdrawals track: Poterba, Venti & Wise) - returns above that build up
                fin = max(self.cash + self.invest + self.retire, 0.0) / P
                y_perm += fin / max(self.n_years - t, 1)
            y.scale = spending_scale(plan, y_perm, self.ref0, G)
            # what the household can afford in housing and cars: its income before
            # housing vs the survey couple's (after housing it would feed back on itself)
            y_all = (y.normal_net - y.child_ben) / P
            rel_inc = y_all / (self.ref_all0 * G) if y_all > 0 and self.ref_all0 else 0.25
        # housing and cars follow income too (cheaper flats and older cars on less)
        h_el = (
            plan.housing_elasticity if plan.housing_elasticity is not None else HOUSING_ELASTICITY
        )
        c_el = plan.car_elasticity if plan.car_elasticity is not None else CAR_ELASTICITY
        y.h_scale = min(max(rel_inc, 0.25), 4.0) ** h_el
        if t == 0:
            self.h_scale0 = y.h_scale
        if plan.rent is not None:  # your own rent is your rent now: only later changes
            y.h_scale = y.h_scale / self.h_scale0 if self.h_scale0 > 0 else 1.0
        c_scale = min(max(rel_inc, 0.25), 4.0) ** c_el
        lvl = self.lvl
        exp = y.exp = {
            f"budget:{k}": v * P * y.scale * self.RT(k, t) for k, v in self.budget0.items()
        }
        # what households spend beyond any survey category, even at national-accounts
        # level (interest on consumer debt, gifts and transfers, giving, irregular big
        # costs): the calibrated level (econsim.backtest / .calibrate), as its own line
        if lvl > 1.0:
            exp["budget:other_unrecorded"] = (lvl - 1.0) * sum(exp.values())
        elif lvl < 1.0:  # the national-accounts basket overshoots: scaled down
            exp = y.exp = {k: v * lvl for k, v in exp.items()}
        # wealth effect: before retirement households spend a share of their savings
        # above the emergency fund each year (bigger homes, cars, trips, helping family);
        # calibrated in econsim.backtest on what SCF couples actually accumulated
        w_mpc = plan.wealth_mpc if plan.wealth_mpc is not None else WEALTH_MPC
        saving_for_home = not self.owned and self.buy_t_plan is not None
        if w_mpc and not y.retired and not saving_for_home:
            free_w = max(self.cash + self.invest - self.tgt_prev, 0.0)
            base_b = sum(exp.values())
            if base_b > 0 and free_w > 0:
                th = (
                    plan.wealth_mpc_decline
                    if plan.wealth_mpc_decline is not None
                    else WEALTH_MPC_DECLINE
                )
                if th and y.normal_net > 0:  # less of each dollar spent as savings pile up
                    free_w *= (1 + free_w / y.normal_net) ** -th
                f_w = 1 + w_mpc * free_w / base_b
                exp = y.exp = {k: v * f_w for k, v in exp.items()}
        # children: OECD-modified equivalence scale (couple 1.5; child <14 +0.3, 14+ +0.5)
        # applied to the couple's non-housing living budget; housing via the rent factor,
        # childcare, health and cars separately
        if hh:
            # OECD-modified scale on PER-PERSON spending only (food, clothes, recreation,
            # ...): energy, internet, furnishings, insurance or council tax barely change
            # with children, and children do not drink or smoke
            w = sum(0.3 / 1.5 if k < 14 else 0.5 / 1.5 for k in y.kid_ages if k <= self.dep_to)
            exp["children"] = (
                lvl
                * sum(v for k, v in self.budget0.items() if k in PER_PERSON_CATS)
                * P
                * y.scale
                * w
            )
        else:
            exp["children"] = self.child_spend0 * P * y.n_home * y.scale
        # fewer working parents -> less paid care (expected value)
        cc = childcare_need(y.kid_ages, plan, hh, self.childcare0) * self.family_factor(
            cd.family_emp, y.kid_ages
        )
        exp["childcare"] = cc * P * G * self.RT_w("childcare", t)  # rises with wages (or faster)
        if plan.university_share and self.uni0:
            n_uni = sum(
                1
                for k in y.kid_ages
                if plan.university_from <= k < plan.university_from + plan.university_years
            )
            if n_uni:
                exp["university"] = plan.university_share / 100 * self.uni0 * n_uni * P * G
        if self.ltc_from is not None and t >= self.ltc_from and self.ltc0:
            exp["long_term_care"] = self.ltc0 * P * G
        # health insurance and out-of-pocket costs
        if self.have_health or self.have_oop:
            fpl = self.start(FPL_2025[0] + FPL_2025[1] * (2 + y.n_home), 2025) * P
            hc = health_cost(
                hh,
                cd.iso3,
                y.ages,
                y.kid_ages,
                self.dep_to,
                self.have_health,
                self.have_oop,
                fpl,
                sum(y.normal),
                self.y0 + t,
                plan.private_health,
                y.n_dep,
            )
            exp["health"] = (
                self.start(hc, 2025) * self.hc_k * P * G * self.RT_w("insurance_health", t)
            )
        # cars and public transport
        if self.have_cars:
            car = 0.0
            for k in range(self.n_cars):
                age = y.ages[min(k, 1)]
                f = next((m for lim, m in YOUNG_DRIVER if age < lim), 1.0)
                car += self.car_fixed0 + self.car_ins0 * f
            exp["cars"] = car * P * G * self.RT_w("insurance_vehicle", t) * c_scale
            exp["public_transport"] = max(2 - self.n_cars, 0) * self.transit0 * P * G
        exp["emergencies"] = self.emerg0 * P * G
        # student loan
        if self.sl_bal > 0.5:
            exp["student_loan"], self.sl_bal = student_loan_year(
                cd.iso3, self.sl_bal, self.sl0, self.sl_rate, self.sl_years, y.gross, P, t
            )

    def _one_offs(self, y: _Year) -> None:
        """A wedding and births this year."""
        t, P, G = y.t, y.P, y.G
        y.one_off = {}
        if self.marry_t is not None and t == self.marry_t and not self.married_from_start:
            y.one_off["wedding"] = self.wedding0 * P * G
            self.events.append({"t": t, "age": y.a1, "event": "Marriage"})
        for k, b in enumerate(self.birth_t):
            if b is not None and t == b:
                self.events.append({"t": t, "age": y.a1, "event": f"Child {k + 1}"})
                bc = nan0(self.hh.get("birth_cost"))
                if bc:
                    y.one_off[f"birth_{k + 1}"] = self.start(bc, 2025) * P * G

    def _housing(self, y: _Year) -> None:
        """Rent - or, once the planned home is affordable, its purchase - then the
        mortgage and owner's costs, and the owned home following the price index."""
        cd, L, t, P = self.cd, self.L, y.t, y.P
        rent_f = 1.0
        if y.n_home:
            rent_f = cd.rent_factor_per_child[min(y.n_home, 2) - 1]
        if not self.owned and self.buy_t_plan is not None and t >= self.buy_t_plan:
            self._try_to_buy(y)
        y.interest = 0.0
        if self.owned:
            if self.mort_bal > 0.5:
                (
                    self.mort_bal,
                    self.mort_rate,
                    self.mort_left,
                    self.mort_pay,
                    self.fix_years_eff,
                    self.mort_age,
                    y.interest,
                    principal,
                ) = mortgage_year(
                    self.mort_bal,
                    self.mort_rate,
                    self.mort_left,
                    self.mort_pay,
                    self.fix_years_eff,
                    self.mort_age,
                    ch_style=self.ch_style,
                    buy_price=self.buy_price,
                    loan0=self.loan0,
                    refi=cd.iso3 in REFI_COUNTRIES and not self.ch_style and t > 0,
                    new_rate=new_loan_rate(t, self.m_rate_now, self.rate_lr, self.HS),
                    rate_lr=self.rate_lr,
                )
                y.exp["mortgage_payment"] = y.interest + principal
            y.exp.update(
                owner_costs(
                    self.plan,
                    L,
                    cd.iso3,
                    self.home_value,
                    self.mort_bal,
                    self.buy_price,
                    self.loan0,
                    y.interest,
                    P,
                )
            )
            # an owned home follows the (repeat-sales) price index
            drift = next(d for lim, d in HOME_DRIFT if y.a1 < lim)
            self.home_value *= (
                self.Pt[t + 1] / self.Pt[t] * self.Hh[t + 1] / self.Hh[t] * (1 + drift)
            )
        else:
            y.exp["rent"] = self.rent0 * y.H * rent_f * y.h_scale

    def _try_to_buy(self, y: _Year) -> None:
        """Buy the planned home if the lenders' limits and the savings allow it."""
        plan, cd, L, t, P, a1 = self.plan, self.cd, self.L, y.t, y.P, y.a1
        price = self.price0 * y.HH * y.h_scale
        dp_min, term, rate, fixed_all = mortgage_terms(
            L,
            self.inp,
            plan,
            cd.iso3,
            a1,
            t,
            self.m_rate_now,
            self.rate_lr,
            self.HS,
            self.dp_user,
            price,
            P,
        )
        self.ch_style = cd.iso3 in CH_STYLE and not (term and term > 0)
        gross_hh = sum(y.normal)
        limits = loan_limits(
            L, plan, cd.iso3, price, rate, term, dp_min, gross_hh, y.normal_net, self.ch_style
        )
        binding = min(limits, key=limits.get)
        loan = max(min(limits.values()), 0.0)
        # up-front costs
        tax_buy = purchase_tax(cd.iso3, L, self.inp, self.tt_user, price, P, a1)
        closing = price * (nan0(L.get("closing_pct")) if L else plan.closing_cost_pct) / 100
        prem = 0.0
        if cd.iso3 == "CAN" and loan > 0.8 * price:
            ltv = loan / price
            prem = loan * (0.04 if ltv > 0.9 else 0.031 if ltv > 0.85 else 0.028)
        equity = price - loan + tax_buy + closing
        buffer = plan.emergency_fund_months * self.prev_recurring / 12
        liquid = self.cash + self.invest
        ok_loan = loan >= 0.5 * price * (1 - dp_min) or liquid >= price
        if ok_loan and liquid - equity >= buffer:
            self.owned = True
            y.one_off["house_purchase"] = equity
            y.one_off["moving"] = MOVING_PCT / 100 * price
            self.home_value = self.buy_price = price
            self.mort_bal = self.loan0 = loan + prem
            self.mort_rate = rate
            self.fix_years_eff = term if fixed_all else self.fix_years
            self.mort_left = term
            self.mort_age = 0
            self.mort_pay = (
                annuity(self.mort_bal, self.mort_rate, self.mort_left)
                if not self.ch_style
                else 0.0
            )
            self.events.append({"t": t, "age": a1, "event": "House purchase"})
            if t > self.buy_t_plan:
                self.warnings.append(
                    f"House purchase delayed from age {plan.buy_house_age} to {a1}: the "
                    f"binding limit was {binding} and/or savings for the "
                    f"{equity / price * 100:.0f}% up-front (down payment + taxes + fees)"
                )
            return
        # why not (kept from the last year anyone still earned: after retirement no
        # lender would lend, which would hide the reason)
        if gross_hh > 0 or self.why_not is None:
            self.why_not = (
                f"up-front {equity / P:,.0f} {cd.currency} in today's money "
                "(down payment, taxes, fees) > savings minus the buffer kept"
                if ok_loan
                else f"the {binding} limit allows a loan of only "
                f"{loan / P:,.0f} {cd.currency} in today's money"
            )
        if t == self.n_years - 1:
            self.warnings.append(
                f"House never affordable within the horizon "
                f"({self.why_not}). Try a cheaper home, a larger down "
                "payment or a longer term."
            )

    def _wealth_tax(self, y: _Year) -> None:
        """Norway, Switzerland."""
        hh = self.hh
        wt = nan0(hh.get("wealth_tax_rate"))
        if wt:
            base = (
                self.cash
                + self.invest * (0.8 if self.cd.iso3 == "NOR" else 1.0)
                + self.home_value * nan0(hh.get("wealth_home_share"))
                - self.mort_bal
            )
            thr = nan0(hh.get("wealth_tax_threshold")) * 2 * y.P
            if base > thr:
                y.exp["wealth_tax"] = wt * (base - thr)

    def _price_factors(self, y: _Year) -> dict[str, float]:
        """What each line's PRICE did this year (quantities aside): the household's own
        cost of living, like the UK Household Costs Indices - its own basket, with market
        rents and what its mortgage actually costs (fixed loans: nothing more)."""
        t, P, G = y.t, y.P, y.G
        pfac = {
            "rent": y.H,
            "owner_costs": y.HH,
            "children": P,
            "emergencies": P * G,
            "childcare": P * G * self.RT_w("childcare", t),
            "health": P * G * self.RT_w("insurance_health", t),
            "cars": P * G * self.RT_w("insurance_vehicle", t),
            "public_transport": P * G,
            "university": P * G,
            "long_term_care": P * G,
            "mortgage_payment": 1.0,
            "mortgage_insurance": 1.0,
            "student_loan": 1.0,
        }
        for k in self.budget0:
            pfac[f"budget:{k}"] = P * self.RT(k, t)
        pfac["budget:other_unrecorded"] = P
        return pfac

    def _balance_budget(self, y: _Year) -> None:
        """Saving = net income - spending; when the money runs out: cut discretionary
        spending, borrow (within a limit), cut living costs; in old age, borrow against the
        home and then the state's minimum income for older people."""
        plan, exp = self.plan, y.exp

        def totals():
            y.recurring = sum(exp.values())
            y.total_out = y.recurring + sum(y.one_off.values())
            y.saving = y.net - y.total_out

        totals()
        self.prev_housing = (
            y.interest
            + exp.get("owner_costs", 0.0)
            + exp.get("mortgage_insurance", 0.0)
            + exp.get("mortgage_interest_relief", 0.0)
        ) * (1 + self.INF[y.t])
        y.squeeze = 0.0
        # retirees cannot borrow unsecured: owners borrow against the home at mortgage
        # rates (equity release), and everyone cuts living costs harder first
        self.r_debt_now = (
            (self.rate_lr + 0.01 if self.owned else self.r_debt) if y.retired else self.r_debt
        )

        def available():
            return self.grow_cash(self.cash) + self.invest * (1 + y.r_inv) + y.saving

        avail = available()
        # how much to find: what savings cannot cover - or, for households that protect
        # their savings, the whole of this year's shortfall on regular costs
        need = max(-avail, y.recurring - y.net if plan.protect_savings and not y.retired else 0.0)
        if need > 0 and plan.budget_constraint:
            y.squeeze = cut_extras(exp, need, y.retired)
            if y.squeeze:
                totals()
        # beyond what a household can borrow unsecured (credit cards, overdrafts: a share
        # of a year's income), everything but housing, care, health and loans is cut
        limit = 0.0 if y.retired else BORROW_LIMIT * max(y.net, 0.0)
        avail = available()
        if avail < -limit and plan.budget_constraint:
            cut = cut_living_costs(exp, -limit - avail)
            if cut:
                y.squeeze += cut
                totals()
        # old age with savings gone: owners borrow against the home (up to its equity);
        # beyond that the state's minimum income for older people (safety-net pension,
        # housing benefit) fills the gap - it is not run up as debt
        y.assistance = 0.0
        if y.retired:
            avail = available()
            floor = -(self.home_value - self.mort_bal) * 0.8 if self.owned else 0.0
            if avail < floor:
                y.assistance = floor - avail
                y.saving += y.assistance
                y.net += y.assistance
        self.prev_recurring = y.recurring

    def _move_balances(self, y: _Year) -> None:
        """Interest and returns, then the saving: the emergency fund (cash) first; the
        rest split between bank deposits and investments as the country's households
        hold their savings."""
        plan = self.plan
        target = plan.emergency_fund_months * y.recurring / 12
        self.cash = self.grow_cash(self.cash)
        self.invest *= 1 + y.r_inv
        # employer pension contributions: a retirement account (net worth, not liquid)
        y.match = nan0(self.hh.get("employer_match_pct")) / 100 * sum(y.gross)
        self.retire = self.retire * (1 + y.r_inv) + y.match
        if y.a1 >= self.ret_age and self.retire:  # pension pots become spendable at retirement
            self.invest += self.retire
            self.retire = 0.0
        self.cash += y.saving
        free = self.cash + self.invest - target
        if self.pf_hi is not None:
            x = max(free, 0.0) / max(y.net, 1.0)  # free savings in years of income
            b = x / (x + self.pf_k)
            self.eq_now = self.eq_share + b * (self.pf_hi[0] - self.eq_share)
            self.dep_now = self.dep_share + b * (self.pf_hi[1] - self.dep_share)
        if free > 0:
            self.cash, self.invest = target + self.dep_now * free, (1 - self.dep_now) * free
        else:
            self.cash, self.invest = self.cash + self.invest, 0.0
        self.tgt_prev = target

    def _row(self, y: _Year) -> dict:
        g, it, ssc, pen, exp, owned = y.gross, y.it, y.ssc, y.pen, y.exp, self.owned
        return {
            "t": y.t,
            "year": self.y0 + y.t,
            "age1": y.a1,
            "age2": y.a2,
            "married": y.married,
            "n_children": y.n_kids,
            "owned": owned,
            "price_factor": y.P,
            "gross1": g[0],
            "gross2": g[1],
            "gross": sum(g),
            "income_tax_local": sum(it),
            "ssc_local": sum(ssc),
            "pension_contrib": sum(pen),
            "us_tax": y.us_tax,
            "us_method": y.us_method,
            "child_benefits": y.child_ben,
            "leave_benefits": y.leave_ben,
            "leave_months": sum(y.leave_m),
            "unemployment_months": y.unemp_months,
            "job_loss_benefits": y.jl_ben,
            "unemployment_risk_cost": y.unemp_cost,
            "employer_pension": y.match,
            "budget_scale": y.scale,
            "child_penalty_factor": y.fam_k,
            "spending_squeeze": y.squeeze,
            "retirement_account": self.retire,
            "pension_income": y.pension_income,
            "family_transfers": y.transfers,
            "retired": y.retired,
            "old_age_assistance": y.assistance,
            "net1": g[0] - it[0] - ssc[0] - pen[0],
            "net2": g[1] - it[1] - ssc[1] - pen[1],
            "net_income": y.net,
            **exp,
            **{f"oneoff:{k}": v for k, v in y.one_off.items()},
            "expenses_recurring": y.recurring,
            "expenses_total": y.total_out,
            "saving": y.saving,
            "cash": self.cash,
            "invest": self.invest,
            "liquid": self.cash + self.invest,
            "home_value": self.home_value if owned else 0.0,
            "mortgage_balance": self.mort_bal if owned else 0.0,
            "mortgage_interest": y.interest,
            "mortgage_rate": self.mort_rate * 100 if owned else 0.0,
            "student_loan_balance": max(self.sl_bal, 0.0),
            "_pfac": self._price_factors(y),
            "_exp": {k: v for k, v in exp.items() if v > 0},
        }

    # ------------------------------------------------------------------ result
    def _result(self) -> Result:
        rows = self.rows
        for r_, c_ in zip(rows, own_cost_index(rows)):
            r_.pop("_pfac")
            r_.pop("_exp")
            r_["cost_index"] = c_
        df = pd.DataFrame(rows).fillna(0.0)
        df["home_equity"] = df["home_value"] - df["mortgage_balance"]
        df["net_worth"] = df["liquid"] + df["home_equity"] + df["retirement_account"]
        eff = df["income_tax_local"] + df["ssc_local"]
        df["local_tax_rate"] = np.where(df["gross"] > 0, eff / df["gross"].replace(0, np.nan), 0)
        df["saving_rate"] = np.where(
            df["net_income"] > 0, df["saving"] / df["net_income"].replace(0, np.nan), 0
        )
        summary = summarise(
            df, self.plan, self.ret_age, self.horizon, self.nrr_tab, self.hh, self.warnings
        )
        return Result(
            df,
            self.events,
            self.warnings,
            self.inp,
            summary,
            [p for p in self.paths if p is not None],
        )


def to_display(
    res: Result, cd: CountryData, real: bool = True, currency: str = "local", deflator: str = "cpi"
) -> pd.DataFrame:
    """Convert money columns: real (start-year prices; deflator "cpi" = consumer prices,
    "own" = what the household's own spending costs) and/or USD / intl $ at the
    start-year exchange rate / PPP (held constant)."""
    df = res.years.copy()
    skip = {
        "t",
        "year",
        "age1",
        "age2",
        "married",
        "n_children",
        "owned",
        "price_factor",
        "cost_index",
        "us_method",
        "unemployment_months",
        "local_tax_rate",
        "saving_rate",
        "budget_scale",
        "leave_months",
        "mortgage_rate",
        "child_penalty_factor",
        "retired",
    }
    money = [c for c in df.columns if c not in skip and pd.api.types.is_numeric_dtype(df[c])]
    div = (
        (df["cost_index"] if deflator == "own" and "cost_index" in df else df["price_factor"])
        if real
        else 1.0
    )
    conv = {"local": 1.0, "USD": cd.lcu_per_usd.value, "intl$": cd.lcu_per_intl.value}[currency]
    for c in money:
        df[c] = df[c] / div / conv
    return df

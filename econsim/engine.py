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
from .tax import USParams, local_rates, us_expat_tax, us_fica

HOUSING_CATS = {"rent", "mortgage_payment", "owner_costs"}


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
    rw_default = float(min(max(rw, 0.0), 1.0)) if rw == rw else 0.5
    return {
        "inflation": pick(plan.inflation, cd.inflation_hist.with_value(
            infl_default, source="assumption: 2% (central-bank target); "
            f"history {cd.inflation_hist.value:.1f}%/yr", estimated=True), "%"),
        "real_wage_growth": pick(plan.real_wage_growth, cd.real_wage_growth_hist.with_value(
            rw_default, source=f"history {rw:.2f}%/yr ({cd.real_wage_growth_hist.source}), "
            "clipped to 0-1%", estimated=True), "%"),
        "wedding_cost": pick(plan.wedding_cost, cd.wedding_cost),
        "home_price": pick(plan.home_price, cd.home_price),
        "down_payment_pct": pick(plan.down_payment_pct, cd.down_payment_pct, "%"),
        "transfer_tax_pct": pick(plan.transfer_tax_pct, cd.transfer_tax_pct, "%"),
        "mortgage_rate": pick(plan.mortgage_rate, cd.mortgage_rate, "%"),
        "rent": pick(plan.rent, cd.rent),
        "emergency_annual_cost": pick(plan.emergency_annual_cost, cd.emergency_expense_size),
    }


def roll(value: float, from_year, to_year: int, inflation: float) -> float:
    """Move a money value from its data year to the start year at `inflation`."""
    try:
        fy = int(str(from_year)[:4])
    except (TypeError, ValueError):
        return value
    return value * (1 + inflation / 100) ** max(to_year - fy, 0)


def simulate(plan: Plan, cd: CountryData, us: USParams | None = None) -> Result:
    inp = resolve(plan, cd)
    infl = inp["inflation"]["value"] / 100
    g_real = inp["real_wage_growth"]["value"] / 100
    r_inv, r_cash, r_debt = plan.return_nominal / 100, plan.cash_return / 100, plan.debt_rate / 100
    y0 = plan.start_year
    dy = cd.data_year

    def start(v, year=None):  # data-year money -> start-year money
        return roll(v, year if year is not None else dy, y0, infl * 100)

    e1, e2 = plan.earners
    n_years = max(plan.horizon_age - e1.age + 1, 1)
    warnings: list[str] = []
    events: list[dict] = []

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
    budget0 = {k: start(v.value, v.year) * 12 * plan.budget_scale for k, v in cd.budget.items()}
    rent0 = start(inp["rent"]["value"], inp["rent"]["year"]) * 12
    child_spend0 = start(cd.child_extra_spending.value, cd.child_extra_spending.year) * 12
    childcare0 = start(cd.childcare_per_child.value, cd.childcare_per_child.year)
    child_ben0 = start(cd.child_benefit_per_child.value, cd.child_benefit_per_child.year)
    emerg0 = start(inp["emergency_annual_cost"]["value"], inp["emergency_annual_cost"]["year"])
    wedding0 = start(inp["wedding_cost"]["value"], inp["wedding_cost"]["year"])
    price0 = start(inp["home_price"]["value"], inp["home_price"]["year"])
    dp_pct = inp["down_payment_pct"]["value"] / 100
    tt_pct = inp["transfer_tax_pct"]["value"] / 100
    m_rate = inp["mortgage_rate"]["value"] / 100
    nrr = {m: s.value for m, s in cd.nrr.items()}
    fx = cd.lcu_per_usd.value
    abroad = plan.us_citizens and cd.iso3 != "USA" and us is not None

    birth_t = [None if a is None else a - e1.age for a in plan.child_ages]
    marry_t = None if plan.marriage_age is None else plan.marriage_age - e1.age
    married_from_start = plan.marriage_age is not None and plan.marriage_age <= e1.age
    buy_t_plan = None if plan.buy_house_age is None else plan.buy_house_age - e1.age

    cash = plan.starting_savings
    invest = 0.0
    owned = False
    home_value = mort_bal = mort_pay = 0.0
    mort_left = 0
    jl = plan.job_loss
    prev_recurring = 0.0
    rows = []
    for t in range(n_years):
        a1, a2 = e1.age + t, e2.age + t
        P = (1 + infl) ** t
        aw = aw0 * ((1 + g_real) * (1 + infl)) ** t
        married = married_from_start or (marry_t is not None and t >= marry_t)
        kid_ages = [t - b for b in birth_t if b is not None and t >= b]
        n_kids = len(kid_ages)
        n_dep = sum(1 for k in kid_ages if k <= plan.child_dependent_to)
        n_care = sum(1 for k in kid_ages if plan.childcare_from <= k <= plan.childcare_to)

        gross = [gross_at(0, a1, t), gross_at(1, a2, t)]
        it, ssc = zip(*[local_rates(g, aw, cd.tax_points, cd.marginal_rate_top) for g in gross])
        it, ssc = list(it), list(ssc)
        child_ben = child_ben0 * P * n_dep * ((1 + g_real) ** t)
        normal_net = sum(gross) - sum(it) - sum(ssc) + child_ben

        # ---- job loss: unemployed months replace household net with NRR x normal net
        unemp_months = 0
        lost_net = 0.0
        if jl is not None and gross[jl.earner] > 0:
            start_t = jl.age - e1.age
            months_this_year = [m for m in range(1, jl.months + 1)
                                if start_t * 12 + m - 1 in range(t * 12, t * 12 + 12)]
            if months_this_year:
                unemp_months = len(months_this_year)
                monthly = normal_net / 12
                kept = sum(nrr_for_month(nrr, m) for m in months_this_year) * monthly
                lost_net = unemp_months * monthly - kept
                frac = unemp_months / 12  # the earner's own gross & taxes for those months
                gross[jl.earner] *= 1 - frac
                it[jl.earner] *= 1 - frac
                ssc[jl.earner] *= 1 - frac
                if t == start_t:
                    events.append({"t": t, "age": a1, "event": f"Job loss (partner "
                                   f"{jl.earner + 1}, {jl.months} mo)"})

        # ---- US layer
        us_tax = 0.0
        us_method = ""
        if abroad:
            f_us = (1 + infl) ** (y0 - 2026 + t)
            p_us = us.indexed(f_us)
            housing_usd = (rent0 * P / fx) if not owned else 0.0
            n_ctc = sum(1 for k in kid_ages if k < 17)   # child tax credit: under 17
            res = us_expat_tax([g / fx for g in gross], [x / fx for x in it], married,
                               n_ctc, housing_usd, p_us)
            us_tax = res.residual * fx
            us_method = res.method
            if plan.us_employer and not cd.totalization:
                us_tax += sum(us_fica(g / fx, p_us) for g in gross) * fx
        # unemployment benefit: household kept NRR share; fold the shortfall in
        net = sum(gross) - sum(it) - sum(ssc) + child_ben - us_tax
        if unemp_months:
            # normal_net already reflects full-year taxes; replace earner months
            net = normal_net - lost_net - us_tax

        # ---- expenses (nominal)
        exp = {f"budget:{k}": v * P for k, v in budget0.items()}
        exp["children"] = child_spend0 * P * n_dep
        exp["childcare"] = childcare0 * P * n_care
        exp["emergencies"] = emerg0 * P
        one_off = {}
        if marry_t is not None and t == marry_t and not married_from_start:
            one_off["wedding"] = wedding0 * P
            events.append({"t": t, "age": a1, "event": "Marriage"})
        for k, b in enumerate(birth_t):
            if b is not None and t == b:
                events.append({"t": t, "age": a1, "event": f"Child {k + 1}"})
        # housing
        rent_f = 1.0
        if n_dep:
            rent_f = cd.rent_factor_per_child[min(n_dep, 2) - 1]
        if not owned and buy_t_plan is not None and t >= buy_t_plan:
            price = price0 * (1 + infl) ** t * (1 + plan.home_real_appreciation / 100) ** t
            upfront = price * (dp_pct + tt_pct + plan.closing_cost_pct / 100)
            loan = price * (1 - dp_pct)
            r_m = m_rate / 12
            n_m = plan.mortgage_years * 12
            pay = loan * r_m / (1 - (1 + r_m) ** -n_m) * 12 if r_m else \
                loan / plan.mortgage_years
            affordable = pay <= plan.max_payment_to_income / 100 * max(net, 0)
            # keep the emergency fund intact after paying the deposit + costs
            buffer = plan.emergency_fund_months * prev_recurring / 12
            if cash + invest - upfront >= buffer and affordable:
                owned = True
                one_off["house_purchase"] = upfront
                home_value = price
                mort_bal = loan
                mort_pay = pay
                mort_left = plan.mortgage_years
                events.append({"t": t, "age": a1, "event": "House purchase"})
                if t > buy_t_plan:
                    warnings.append(f"House purchase delayed from age {plan.buy_house_age} "
                                    f"to {a1}: savings for the "
                                    f"{(dp_pct + tt_pct) * 100 + plan.closing_cost_pct:.1f}% "
                                    "up-front cost and/or the payment-to-income limit "
                                    f"({plan.max_payment_to_income:g}% of net) not met earlier")
            elif t == n_years - 1:
                why = (f"payment {pay / 12:,.0f}/month > {plan.max_payment_to_income:g}% of "
                       f"net income" if not affordable else
                       f"up-front {upfront:,.0f} {cd.currency} > savings")
                warnings.append(f"House never affordable within the horizon ({why}). "
                                "Try a cheaper home, a larger down payment or a longer term.")
        interest = principal = 0.0
        if owned:
            if mort_left > 0:
                for _ in range(12):
                    i_m = mort_bal * m_rate / 12
                    p_m = min(mort_pay / 12 - i_m, mort_bal)
                    interest += i_m
                    principal += p_m
                    mort_bal -= p_m
                mort_left -= 1
                exp["mortgage_payment"] = interest + principal
            exp["owner_costs"] = home_value * plan.owner_cost_pct / 100
            home_value *= (1 + infl) * (1 + plan.home_real_appreciation / 100)
        else:
            exp["rent"] = rent0 * P * rent_f
        recurring = sum(exp.values())
        prev_recurring = recurring
        total_out = recurring + sum(one_off.values())
        saving = net - total_out

        # ---- balances: emergency fund (cash) first, then investments
        target = plan.emergency_fund_months * recurring / 12
        cash *= 1 + (r_cash if cash >= 0 else r_debt)
        invest *= 1 + r_inv
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
            "income_tax_local": sum(it), "ssc_local": sum(ssc), "us_tax": us_tax,
            "us_method": us_method, "child_benefits": child_ben,
            "unemployment_months": unemp_months, "job_loss_income_lost": lost_net,
            "net1": gross[0] - it[0] - ssc[0], "net2": gross[1] - it[1] - ssc[1],
            "net_income": net, **exp, **{f"oneoff:{k}": v for k, v in one_off.items()},
            "expenses_recurring": recurring, "expenses_total": total_out, "saving": saving,
            "cash": cash, "invest": invest, "liquid": cash + invest, "home_value": home_value
            if owned else 0.0, "mortgage_balance": mort_bal if owned else 0.0,
            "mortgage_interest": interest,
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
    return Result(df, events, warnings, inp)


def to_display(res: Result, cd: CountryData, real: bool = True,
               currency: str = "local") -> pd.DataFrame:
    """Convert money columns: real (start-year prices) and/or USD / intl $ at the
    start-year exchange rate / PPP (held constant)."""
    df = res.years.copy()
    skip = {"t", "year", "age1", "age2", "married", "n_children", "owned", "price_factor",
            "us_method", "unemployment_months", "local_tax_rate", "saving_rate"}
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

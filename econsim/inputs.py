"""Inputs of a simulation: every default resolved with its source (resolve), starting
wealth, the horizon, pay and unemployment by age and degree."""

from __future__ import annotations

import numpy as np

from .assumptions import LAST_SURVIVOR_YEARS, PAY_BANDS, SW_EDU
from .model import EDUCATION, CountryData, Plan, Sourced, age_band
from .money import nan0


def edu_pay_factor(cd: CountryData, education: str | None, age: float) -> float:
    """Full-time pay for `education` relative to all education levels at `age` (OECD EAG
    relative earnings), interpolated between age-band midpoints; 1 if unknown."""
    if not education or not cd.edu_pay:
        return 1.0
    pts = [(m, cd.edu_pay[(education, b)]) for m, b in PAY_BANDS if (education, b) in cd.edu_pay]
    if not pts:
        return 1.0
    return float(np.interp(age, [a for a, _ in pts], [f for _, f in pts]))


def unemployment_rate_for(cd: CountryData, age: int, education: str | None):
    """Unemployment rate (%) for the age band and degree, with its Sourced (or None)."""
    s = cd.labour.get(("unemployment", age_band(age), education or "all")) if cd.labour else None
    return (s.value, s) if s is not None else (None, None)


def resolve(plan: Plan, cd: CountryData) -> dict:
    """Merge plan overrides with data defaults; returns value + provenance."""
    out: dict = {}
    for part in (
        _prices_and_costs,
        _earners,
        _price_outlook,
        _years_ahead,
        _savings,
        _student_loans,
        _mortgage_outlook,
        _household_costs,
    ):
        part(plan, cd, out)
    out.update(start_wealth_inputs(plan, cd, out))
    return out


def _pick(override, sourced, unit=""):
    """A plan's own value (user input), else the data default with its source."""
    if override is not None:
        return {
            "value": float(override),
            "source": "user input",
            "estimated": False,
            "year": None,
            "quality": "",
            "unit": unit or sourced.unit,
        }
    return {
        "value": float(sourced.value),
        "source": sourced.source,
        "estimated": sourced.estimated,
        "year": sourced.year,
        "quality": sourced.quality,
        "unit": sourced.unit,
        "note": sourced.note,
    }


def _prices_and_costs(plan: Plan, cd: CountryData, out: dict) -> None:
    """Inflation, real wage growth and the plan's main costs (wedding, home, rent,
    emergencies)."""
    R = cd.rates or {}
    infl_default = float(R.get("inflation_target", 2.0))
    rw = cd.real_wage_growth_hist.value
    if cd.hh.get("real_wage_growth", float("nan")) == cd.hh.get("real_wage_growth", 0):
        rw_default = float(rw)  # curated projection
    else:
        rw_default = float(min(max(rw, 0.0), 1.0)) if rw == rw else 0.5
    IO = cd.inflation_outlook or {}
    if plan.inflation is None and "inflation_5y5y" in IO:
        v, per, src_ = IO["inflation_5y5y"]
        infl_src = cd.inflation_hist.with_value(
            round(v, 2),
            source=f"what bond markets expect after the next 5 years: 5-year, "
            f"5-year-forward inflation {v:.2f}% ({src_}, {per}); CPI-linked Treasuries vs "
            f"nominal ones",
            estimated=True,
        )
    else:
        infl_src = cd.inflation_hist.with_value(
            infl_default,
            source=f"central-bank inflation target ({infl_default:g}%); "
            f"10-year history {cd.inflation_hist.value:.1f}%/yr",
            estimated=True,
        )
    out.update(
        {
            "inflation": _pick(plan.inflation, infl_src, "%"),
            "real_wage_growth": _pick(
                plan.real_wage_growth,
                (
                    cd.wage_trend
                    if plan.wage_outlook == "trend" and cd.wage_trend is not None
                    else cd.real_wage_growth_hist.with_value(rw_default, estimated=True)
                ),
                "%",
            ),
            "wedding_cost": _pick(plan.wedding_cost, cd.wedding_cost),
            "home_price": _pick(plan.home_price, cd.home_price),
            "down_payment_pct": _pick(plan.down_payment_pct, cd.down_payment_pct, "%"),
            "transfer_tax_pct": _pick(plan.transfer_tax_pct, cd.transfer_tax_pct, "%"),
            "mortgage_rate": _pick(plan.mortgage_rate, cd.mortgage_rate, "%"),
            "rent": _pick(plan.rent, cd.rent),
            "emergency_annual_cost": _pick(plan.emergency_annual_cost, cd.emergency_expense_size),
        }
    )


def _earners(plan: Plan, cd: CountryData, out: dict) -> None:
    """Each earner's pay factor for their degree and unemployment rate."""
    for i, e in enumerate(plan.earners):
        if e.education:
            lbl = f"P{i + 1} degree"
            out[f"{lbl}: pay vs all degrees"] = {
                "value": edu_pay_factor(cd, e.education, e.age) if plan.education_pay else 1.0,
                "unit": "x",
                "source": (
                    cd.edu_pay_src.source
                    if cd.edu_pay_src and plan.education_pay
                    else "not applied (pay = all-degree profile)"
                ),
                "year": cd.edu_pay_src.year if cd.edu_pay_src else None,
                "estimated": bool(cd.edu_pay_src and cd.edu_pay_src.estimated),
                "quality": "B",
                "note": f"{EDUCATION.get(e.education, e.education)}, at age "
                f"{e.age}; {cd.edu_pay_src.note if cd.edu_pay_src else ''}",
            }
        v, s = unemployment_rate_for(cd, e.age, e.education)
        if s is not None:
            out[
                f"P{i + 1} unemployment rate (age {age_band(e.age)}, "
                f"{(e.education or 'all degrees').replace('_', ' ')})"
            ] = {
                "value": v,
                "unit": "%",
                "source": s.source,
                "year": s.year,
                "estimated": s.estimated,
                "quality": s.quality,
                "note": s.note,
            }


def _price_outlook(plan: Plan, cd: CountryData, out: dict) -> None:
    """Home prices and rents vs incomes, their recent trends, near-term inflation."""
    IO = cd.inflation_outlook or {}
    g = out["real_wage_growth"]["value"]
    out["home_real_appreciation"] = (
        {"value": float(plan.home_real_appreciation), "source": "user input", "unit": "%"}
        if plan.home_real_appreciation is not None
        else {
            "value": g,
            "source": "= real wage growth (house prices and rents keep pace with "
            "incomes; long-run price-to-income ratios are stable)",
            "unit": "%",
            "estimated": True,
        }
    )
    if plan.home_real_appreciation is None and plan.home_outlook == "trend":
        for key, tr in (("home_trend_10y", cd.home_trend), ("rent_trend_10y", cd.rent_trend)):
            if tr is not None:
                out[key] = {
                    "value": tr.value,
                    "unit": "%",
                    "source": tr.source,
                    "year": tr.year,
                    "estimated": False,
                    "quality": "A",
                    "note": f"{tr.note}; for the next 10 years, then with incomes",
                }
    if plan.inflation is None and "inflation_5y" in IO:
        v, per, src_ = IO["inflation_5y"]
        out["inflation_next_5y"] = {
            "value": round(v, 2),
            "unit": "%",
            "year": per,
            "estimated": True,
            "quality": "B",
            "source": f"5-year breakeven inflation ({src_}, {per}): "
            "what bond markets expect for the next 5 years",
        }


def _years_ahead(plan: Plan, cd: CountryData, out: dict) -> None:
    """Retirement age and the horizon."""
    ra = cd.retirement_age
    out["retirement_age"] = _pick(
        plan.retirement_age,
        ra
        if ra is not None
        else Sourced(67.0, "age", "assumption: no OECD data", None, True, "C"),
        "age",
    )
    out["horizon_age"] = _pick(plan.horizon_age, horizon_default(plan, cd), "age")


def _savings(plan: Plan, cd: CountryData, out: dict) -> None:
    """How households hold their savings, and what each part earns."""
    R = cd.rates or {}
    PF = cd.portfolio or {}
    pf_src = (
        f"{PF.get('source', 'OECD')} {PF.get('year', '')}".strip()
        + "; whole-household-sector totals, so weighted toward wealthier savers"
    )
    if plan.equity_share is not None:
        out["equity_share"] = {
            "value": round(float(plan.equity_share) * 100, 1),
            "unit": "% of investments",
            "source": "user input",
        }
    elif PF.get("equity_share_typical") is not None:
        out["equity_share"] = {
            "value": round(float(PF["equity_share_typical"]) * 100, 1),
            "unit": "% of investments",
            "quality": "B",
            "estimated": False,
            "year": PF.get("year"),
            "source": "typical households (bottom 70% by wealth): "
            + str(PF.get("equity_share_typical_source", "")),
            "note": f"the whole household sector holds {100 * float(PF['equity_share']):.0f}% "
            "(OECD), led by the wealthiest",
        }
    elif PF.get("equity_share") is not None:
        out["equity_share"] = {
            "value": round(float(PF["equity_share"]) * 100, 1),
            "unit": "% of investments",
            "source": pf_src,
            "year": PF.get("year"),
            "quality": "B",
            "estimated": bool(PF.get("estimated")),
            "note": "listed shares + investment funds (counted as shares) vs bonds + life "
            "insurance (insurers hold mostly bonds)",
        }
    else:
        out["equity_share"] = {
            "value": 60.0,
            "unit": "% of investments",
            "source": "assumption: no balance-sheet data",
            "estimated": True,
            "quality": "C",
        }
    eq = out["equity_share"]["value"] / 100
    if plan.deposit_share is not None:
        out["deposit_share"] = {
            "value": float(plan.deposit_share),
            "unit": "% of savings",
            "source": "user input",
        }
    elif PF.get("deposit_share_typical") is not None:
        out["deposit_share"] = {
            "value": round(float(PF["deposit_share_typical"]) * 100, 1),
            "unit": "% of savings",
            "quality": "B",
            "estimated": False,
            "year": PF.get("year"),
            "source": "typical households (bottom 70% by wealth): "
            + str(PF.get("deposit_share_typical_source", "")),
            "note": f"the whole household sector: {100 * float(PF['deposit_share']):.0f}% (OECD)",
        }
    elif PF.get("deposit_share") is not None:
        out["deposit_share"] = {
            "value": round(float(PF["deposit_share"]) * 100, 1),
            "unit": "% of savings above the emergency fund",
            "source": pf_src,
            "year": PF.get("year"),
            "quality": "B",
            "estimated": bool(PF.get("estimated")),
            "note": "bank deposits as a share of households' free financial savings "
            "(deposits, bonds, listed shares, funds, life insurance; not pensions or own "
            "businesses); a typical household holds even more in deposits",
        }
    else:
        out["deposit_share"] = {
            "value": 0.0,
            "unit": "% of savings",
            "source": "assumption: no balance-sheet data (all invested)",
            "estimated": True,
            "quality": "C",
        }
    y10 = R.get("y10")
    if plan.return_nominal is not None:
        out["return_nominal"] = _pick(plan.return_nominal, Sourced(0.0), "%")
    elif y10 is not None:
        v = y10 + eq * plan.equity_premium - plan.fund_fees
        out["return_nominal"] = {
            "value": round(v, 2),
            "unit": "%",
            "year": R.get("y10_period"),
            "estimated": True,
            "quality": "B",
            "source": f"today's 10-year government yield {y10:.2f}% ({R.get('y10_source')}, "
            f"{R.get('y10_period')}) + {eq:.0%} in shares x "
            f"{plan.equity_premium:g}% equity premium - {plan.fund_fees:g}% fees",
            "note": "the yield is the market's price for the next decade; the premium is "
            "between the long-run world equity premium over bonds since 1900 (~3%, "
            "Dimson-Marsh-Staunton) and current implied estimates (~4%)",
        }
    else:
        out["return_nominal"] = {
            "value": 5.0,
            "unit": "%",
            "source": "assumption: no yield data",
            "estimated": True,
            "quality": "C",
        }
    sr = R.get("short_rate")
    on, term = R.get("deposit_overnight"), R.get("deposit_term")
    if plan.cash_return is not None:
        out["cash_return"] = _pick(plan.cash_return, Sourced(0.0), "%")
    elif on is not None:
        out["cash_return"] = {
            "value": round(float(on), 2),
            "unit": "%",
            "year": R.get("deposit_overnight_period"),
            "estimated": bool(R.get("deposit_overnight_estimated")),
            "quality": "B",
            "source": "what banks pay households on overnight / savings deposits now: "
            f"{R.get('deposit_overnight_source')} ({R.get('deposit_overnight_period')})",
            "note": "the emergency fund sits in an instant-access account",
        }
    else:
        out["cash_return"] = (
            {
                "value": round(max(sr - 0.5, 0.0), 2),
                "unit": "%",
                "estimated": True,
                "year": R.get("short_rate_period"),
                "quality": "B",
                "source": f"today's 3-month rate {sr:.2f}% ({R.get('short_rate_source')}, "
                f"{R.get('short_rate_period')}) - 0.5 bank margin",
            }
            if sr is not None
            else {"value": 2.0, "unit": "%", "source": "assumption", "estimated": True}
        )
    if plan.deposit_return is not None:
        out["deposit_return"] = _pick(plan.deposit_return, Sourced(0.0), "%")
    elif on is not None and term is not None:
        w = PF.get("overnight_share")
        w = 0.6 if w is None else float(w)
        out["deposit_return"] = {
            "value": round(w * on + (1 - w) * term, 2),
            "unit": "%",
            "quality": "B",
            "year": R.get("deposit_term_period"),
            "estimated": bool(R.get("deposit_term_estimated")),
            "source": f"{w:.0%} overnight at {on:.2f}% + {1 - w:.0%} fixed-term at "
            f"{term:.2f}% ({R.get('deposit_term_source')}); mix of households' deposits "
            "from the OECD balance sheets",
            "note": "banks pass on only part of market rates to savers",
        }
    else:
        out["deposit_return"] = dict(out["cash_return"])


def _student_loans(plan: Plan, cd: CountryData, out: dict) -> None:
    """Student-loan interest rate and repayment years."""
    y10 = (cd.rates or {}).get("y10")
    us_rate = (
        6.52,
        "US federal Direct Loans for undergraduates 2026-27 (May 2026 10-year "
        "Treasury auction 4.47% + 2.05; fixed for the life of the loan)",
        2026,
    )
    if plan.student_loan_rate is not None:
        out["student_loan_rate"] = {
            "value": float(plan.student_loan_rate),
            "unit": "%",
            "source": "user input",
        }
    elif cd.iso3 == "USA":
        out["student_loan_rate"] = {
            "value": us_rate[0],
            "unit": "%",
            "source": us_rate[1],
            "year": us_rate[2],
            "quality": "A",
            "note": "graduate loans 8.07%, PLUS 9.07%",
        }
    elif cd.iso3 == "GBR":
        out["student_loan_rate"] = {
            "value": out["inflation"]["value"],
            "unit": "%",
            "source": "England Plan 5 loans (from 2023): RPI "
            "inflation (here: the inflation input)",
            "quality": "A",
            "note": "repaid at 9% of pay above GBP 25,000; written off after 40 years",
        }
    elif cd.iso3 == "CAN":
        out["student_loan_rate"] = {
            "value": 0.0,
            "unit": "%",
            "quality": "B",
            "source": "federal Canada Student Loans: interest-free since April 2023",
            "note": "provincial parts vary; set the rate if yours has one",
        }
    else:
        out["student_loan_rate"] = {
            "value": round(float(y10), 2) if y10 is not None else 3.0,
            "unit": "%",
            "estimated": True,
            "quality": "C",
            "source": "assumption: state student loans track "
            "government borrowing costs (= today's 10-year yield; "
            "e.g. NL DUO uses the 5-year bond rate)",
        }
    out["student_loan_years"] = (
        {"value": int(plan.student_loan_years), "unit": "years", "source": "user input"}
        if plan.student_loan_years is not None
        else {
            "value": 10,
            "unit": "years",
            "source": "US standard repayment plan (10 years)"
            + ("; in the UK repayments are income-based instead" if cd.iso3 == "GBR" else ""),
        }
    )


def _mortgage_outlook(plan: Plan, cd: CountryData, out: dict) -> None:
    """What mortgages cost once today's fixed period is over."""
    R = cd.rates or {}
    y10 = R.get("y10")
    fut = R.get("mortgage_rate_future")
    if plan.rate_outlook == "market" and fut is not None:
        out["mortgage_rate_after_fixation"] = {
            "value": float(fut),
            "unit": "%",
            "year": R.get("y10_period"),
            "quality": "B",
            "estimated": True,
            "source": "today's mortgage rate moved by the change in short "
            "rates the market expects: "
            + (
                f"{R['mortgage_rate_now']:.2f}% + ({y10:.2f}% 10-year yield - 0.5 term "
                f"premium - {R['short_rate']:.2f}% 3-month rate)"
                if {"mortgage_rate_now", "short_rate"} <= set(R) and y10 is not None
                else "rates.csv"
            ),
        }
    else:
        lr = nan0((cd.lend or {}).get("rate_longrun"))
        out["mortgage_rate_after_fixation"] = {
            "value": lr or out["mortgage_rate"]["value"],
            "unit": "%",
            "estimated": True,
            "source": (cd.lend or {}).get("rate_longrun_source", "lending.csv long-run average")
            if lr
            else "= today's rate",
        }


def _household_costs(plan: Plan, cd: CountryData, out: dict) -> None:
    """What households pay (incl. housing) vs the CPI: shown, not simulated."""
    IO = cd.inflation_outlook or {}
    if "hci_5y" in IO:
        h, c = IO["hci_5y"], IO.get("cpi_5y", (float("nan"),))
        h1, c1 = IO.get("hci_12m", (float("nan"),)), IO.get("cpi_12m", (float("nan"),))
        h10, c10 = IO.get("hci_10y", (float("nan"),)), IO.get("cpi_10y", (float("nan"),))
        out["household_costs_5y"] = {
            "value": round(h[0], 2),
            "unit": "%/yr",
            "year": h[1],
            "quality": "B",
            "estimated": True,
            "source": h[2],
            "note": f"what households pay rose {h1[0]:.1f}% in the last 12 months, "
            f"{h[0]:.1f}%/yr over 5 years and {h10[0]:.1f}%/yr over 10 (CPI: {c1[0]:.1f}%, "
            f"{c[0]:.1f}%, {c10[0]:.1f}%). The gap is housing: the CPI counts owners' "
            "costs as an imputed rent, not home prices and mortgage interest. The "
            "simulation prices your own housing, rent and each budget line directly - "
            "see 'your own inflation' in the results - so this is shown, not added again",
        }


def start_wealth_value(cd: CountryData, stat: str, age: int, edu: str | None):
    """What couples of this age (head) and degree hold now (US: SCF), with its year."""
    t = cd.start_wealth
    if t is None or len(t) == 0:
        return None
    for e, ht in (
        (SW_EDU.get(edu or "", "all"), "couple"),
        ("all", "couple"),
        (SW_EDU.get(edu or "", "all"), "all"),
        ("all", "all"),
    ):
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
    return (
        plan.buy_house_age is not None
        and e1.age >= plan.buy_house_age
        and start_wealth_value(cd, "owner_home", e1.age, e1.education) is not None
    )


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
            res[key] = {
                "value": 0.0,
                "unit": cd.currency,
                "estimated": True,
                "source": "none assumed (no survey of household wealth by age "
                "loaded for this country): enter yours",
            }
            return
        val, yr, e, ht, src = v
        if "HFCS" in src:
            text = (
                f"{src}: median for households with the reference person aged "
                f"{e1.age} (between the survey's 10-year age bands)"
            )
        else:
            text = (
                f"Federal Reserve SCF {yr}: median for "
                f"{'couples' if ht == 'couple' else 'families'} with the head aged "
                f"{e1.age} (7-year window)"
                + (
                    ""
                    if e == "all"
                    else f", {'with' if e == 'degree' else 'without'} a bachelor's degree"
                )
            )
        res[key] = {
            "value": round(val, -2),
            "unit": cd.currency,
            "year": yr,
            "quality": "A" if "HFCS" not in src else "B",
            "estimated": False,
            "source": text,
            "note": note,
        }

    res["own_home"] = {
        "value": float(own),
        "unit": "",
        "source": "user input"
        if plan.own_home is not None
        else "planned buying age reached: owning, as the owners of this age surveyed"
        if own
        else "renting at the start",
    }
    if own:
        sw(
            "starting_savings",
            plan.starting_savings,
            "owner_fin",
            "home owners' financial assets (incl. retirement accounts, which are split off)",
        )
        sw(
            "starting_retirement",
            plan.starting_retirement,
            "owner_retirement",
            "home owners' retirement accounts (401k, IRA)",
        )
        sw("home_value_now", plan.home_value_now, "owner_home", "owners' primary home")
        sw(
            "mortgage_now",
            plan.mortgage_now,
            "owner_mortgage",
            "owners' mortgage and home-equity debt (0 for the many who have paid off)",
        )
        if res["home_value_now"]["value"] <= 0:
            res["home_value_now"] = {
                "value": out["home_price"]["value"],
                "unit": cd.currency,
                "estimated": True,
                "source": "the typical home price (enter your home's value)",
            }
            if plan.mortgage_now is None:
                res["mortgage_now"] = {
                    "value": 0.5 * out["home_price"]["value"],
                    "unit": cd.currency,
                    "estimated": True,
                    "source": "assumption: half the value still owed (enter your balance)",
                }
        lr = nan0((cd.lend or {}).get("rate_longrun"))
        res["mortgage_rate_now"] = (
            {"value": float(plan.mortgage_rate_now), "unit": "%", "source": "user input"}
            if plan.mortgage_rate_now is not None
            else {
                "value": lr or out["mortgage_rate"]["value"],
                "unit": "%",
                "estimated": True,
                "quality": "B",
                "source": "average rate on new loans over the last 20 years ("
                + (cd.lend or {}).get("rate_longrun_source", "")
                + "): existing loans were "
                "taken out and refinanced over many years (FHFA reports ~4% on outstanding US "
                "mortgages)",
            }
        )
        bal, val = res["mortgage_now"]["value"], max(res["home_value_now"]["value"], 1.0)
        res["mortgage_years_left"] = (
            {"value": int(plan.mortgage_years_left), "unit": "years", "source": "user input"}
            if plan.mortgage_years_left is not None
            else {
                "value": int(min(max(round(30 * bal / (0.85 * val)), 5), 30)) if bal > 0 else 0,
                "unit": "years",
                "estimated": True,
                "quality": "C",
                "source": "assumption: a 30-year loan for 85% of the value, paid down in "
                "proportion to the balance left",
            }
        )
    else:
        sw(
            "starting_savings",
            plan.starting_savings,
            "renter_fin",
            "renters' financial assets incl. retirement accounts (split off below)",
        )
        sw(
            "starting_retirement",
            plan.starting_retirement,
            "renter_retirement",
            "renters' retirement accounts (401k, IRA)",
        )
    # the medians of a total and of a part do not subtract exactly; the part is capped
    tot = res["starting_savings"]
    rt = res["starting_retirement"]
    if tot.get("source") != "user input" and rt.get("source") != "user input":
        r = min(rt["value"], tot["value"])
        res["starting_retirement"] = dict(rt, value=round(r, -2))
        res["starting_savings"] = dict(
            tot,
            value=round(max(tot["value"] - r, 0.0), -2),
            note=tot.get("note", "") + " - less the retirement accounts",
        )
    return res


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
            how = f"women's life expectancy at 65 in {plan.start_year + 65 - e.age} = {le:.1f}"
    if best is None:
        return Sourced(90.0, "age", "assumption: no life-expectancy data", None, True, "C")
    return Sourced(
        float(min(round(best), 105)),
        "age",
        "OECD Pensions at a Glance (UN World "
        "Population Prospects): projected life expectancy at 65",
        None,
        True,
        "B",
        f"{how}, + {LAST_SURVIVOR_YEARS:g} years because a couple"
        " plans for the longer-living partner",
    )

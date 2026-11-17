"""The plan the sidebar describes: for the chosen place, or (with its own data defaults
for local prices) for a place it is compared with."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from econsim.model import CountryData, Earner, JobLoss, Plan


def build_plan(v: SimpleNamespace, iso3: str, override: bool, st_: str | None = None) -> Plan:
    """Plan for iso3 (st_: US state). `override` = use the sidebar's money inputs (the
    place chosen above); comparison places use their own data defaults for local prices."""
    st_ = v.state if override else st_
    jl = JobLoss(v.jl_who - 1, int(v.jl_age), int(v.jl_m)) if v.jl_on else None
    cc_ = v.ds.country(iso3, st_ if iso3 == "USA" else None)
    # money amounts entered for the main country move to others at PPP
    ppp_f = 1.0 if override else cc_.lcu_per_intl.value / v.cd.lcu_per_intl.value
    p = Plan(
        country=iso3,
        earners=(
            Earner(
                int(v.a1),
                v.p1,
                v.g1,
                education=v.d1,
                job=v.j1,
                pay_now=float(v.pn1) if override and v.pn1 > 0 and v.j1 != "U" else None,
            ),
            Earner(
                int(v.a2),
                v.p2,
                v.g2,
                education=v.d2,
                job=v.j2,
                pay_now=float(v.pn2) if override and v.pn2 > 0 and v.j2 != "U" else None,
            ),
        ),
        education_pay=v.edu_pay,
        career_model=bool(v.career_on) or v.CAR is None,
        job_market="trend" if v.jm.startswith("Keeps") else "now",
        price_outlook="trend" if v.costs_out.startswith("Each") else "flat",
        horizon_age=int(v.horizon) if override and int(v.horizon) != v.horizon_d else None,
        retirement_age=int(v.retire) if override and int(v.retire) != v.retire_d else None,
        equity_share=None if v.hold == "Like typical households" else v.eq,
        deposit_share=None if v.hold == "Like typical households" else float(v.dep),
        deposit_return=v.dep_r if override and abs(v.dep_r - v.depr_d) > 1e-9 else None,
        wage_outlook="trend" if v.w_out != "Official projection" else "projection",
        home_outlook="trend" if v.h_out == "Recent trend (10 yrs)" else "incomes",
        rate_outlook="market" if v.r_out == "Market outlook" else "history",
        spend_down=v.spend_down,
        protect_savings=v.protect,
        family_gift=float(v.gift) * ppp_f,
        family_gift_age=int(v.gift_age) or None,
        inheritance=float(v.inh) * ppp_f,
        inheritance_age=int(v.inh_age)
        if override and int(v.inh_age) != max(v.inh_d, int(v.a1))
        else None,
        university_share=float(v.uni),
        university_years=int(v.uni_y),
        child_wage_penalty=float(v.wpen),
        ltc_years=int(v.ltc_y),
        ltc_cost=float(v.ltc_c) if override and abs(v.ltc_c - v.ltc_d) > 1e-9 else None,
        marriage_age=int(v.m_age) if v.married else None,
        child_ages=(None if v.k1 == "none" else int(v.k1), None if v.k2 == "none" else int(v.k2)),
        childcare_from=v.cc_from,
        childcare_to=v.cc_to,
        buy_house_age=int(v.buy_age) if v.mode == "Buy a home" else None,
        max_payment_to_income=None if v.rules else float(v.dsti),
        cars=int(v.cars) if override else None,
        unemployment_risk=v.urisk,
        primary_parent=0 if v.leave_who == "Partner 1" else 1,
        leave_scale=v.leave_scale,
        private_health=v.priv_h,
        church_tax=v.church,
        student_loan=float(v.sloan) if override and abs(v.sloan - v.sl_typ) > 1e-9 else 0.0,
        typical_student_debt=bool(v.sl_typ) and abs(v.sloan - v.sl_typ) < 1e-9,
        student_loan_rate=float(v.sl_rate)
        if override and abs(v.sl_rate - v.slr_d) > 1e-9
        else None,
        student_loan_years=int(v.sl_years) if int(v.sl_years) != 10 else None,
        budget_elasticity=v.elast,
        budget_elasticity_high=v.elast_hi,
        state=st_ if iso3 == "USA" else None,
        survey_correction=v.surv if abs(v.surv - v.surv_d) > 1e-9 else None,
        child_penalty=v.work_mode == "Country typical",
        child_dependent_to=int(v.home_to)
        if override and int(v.home_to) != max(v.home_d, 18)
        else None,
        budget_scale=v.budget_scale,
        emergency_fund_months=v.efund,
        job_loss=jl,
        return_nominal=v.ret if override and abs(v.ret - v.ret_d) > 1e-9 else None,
        cash_return=v.cash_r if override and abs(v.cash_r - v.cash_d) > 1e-9 else None,
        inflation=v.infl if override and abs(v.infl - v.infl_d) > 1e-9 else None,
        us_citizens=v.us_cit,
        us_employer=v.us_emp,
        cohort_effects=v.coh_mode,
        starting_savings=(float(v.savings0) if abs(v.savings0 - v.sav_d) > 1e-9 else None)
        if override
        else float(v.savings0) / v.cd.lcu_per_intl.value * cc_.lcu_per_intl.value,
        starting_retirement=(float(v.ret0) if abs(v.ret0 - v.ret_d0) > 1e-9 else None)
        if override
        else float(v.ret0) / v.cd.lcu_per_intl.value * cc_.lcu_per_intl.value,
        own_home=v.own if override else False,
    )
    if override and v.own:
        p = replace(
            p,
            home_value_now=float(v.hv0) if abs(v.hv0 - v.hv_d) > 1e-9 else None,
            mortgage_now=float(v.mb0) if abs(v.mb0 - v.mb_d) > 1e-9 else None,
            mortgage_rate_now=float(v.mr0)
            if abs(v.mr0 - round(v.DW["mortgage_rate_now"]["value"], 2)) > 1e-9
            else None,
            mortgage_years_left=int(v.my0)
            if int(v.my0) != int(v.DW["mortgage_years_left"]["value"])
            else None,
        )
    if override:
        # pass a value only when the user changed it, so provenance stays on defaults
        def ov(val, default, nd=0):
            return None if abs(val - round(default, nd)) < 1e-9 else val

        p = replace(
            p,
            wedding_cost=ov(v.wedding, v.cd.wedding_cost.value, -2),
            home_price=ov(v.price, v.hp_d, -3),
            down_payment_pct=ov(v.dp, v.cd.down_payment_pct.value, 1),
            transfer_tax_pct=ov(v.tt, v.cd.transfer_tax_pct.value, 9),
            mortgage_rate=ov(v.mr, v.cd.mortgage_rate.value, 2),
            rent=ov(v.rent, v.cd.rent.value),
            emergency_annual_cost=ov(v.emerg, v.cd.emergency_expense_size.value),
            real_wage_growth=ov(v.rwg, v.rwg_d, 2),
            owner_cost_pct=ov(v.owner, v.owner_d, 2),
            home_real_appreciation=v.appr if v.h_out == "Custom" else None,
            mortgage_years=None if int(v.term) == v.term_d else int(v.term),
        )
    return p


def country_with_overrides(v: SimpleNamespace, c: CountryData, main: bool = True) -> CountryData:
    """The main place's data with the sidebar's childcare cost."""
    return (
        replace(c, childcare_per_child=c.childcare_per_child.with_value(v.ccare))
        if main and c.iso3 == v.iso
        else c
    )

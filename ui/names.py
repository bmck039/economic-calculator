"""The settings' fixed names, as they are saved in the browser and carried in shared links.

A setting is stored under its session key:
    "w:<name>"           one value for every place (e.g. "w:p1_age")
    "k:<name>@<place>"   one value per country or US state (e.g. "k:rent@NLD",
                         "k:rent@USA-CA"): defaults differ by place, so does what you enter

Names never change once released: a renamed setting gets an entry in `migrate` instead, so
older links and saved settings keep working. Version 1 (v0.8) keyed some settings by what
their default depended on ("w:m_age23"); from version 2 they have one name and follow
their default until changed (ui.settings.follow).
"""

from __future__ import annotations

import re

VERSION = 2


def place_of(iso3: str, state: str | None) -> str:
    return iso3 if state is None else f"{iso3}-{state}"


def global_key(name: str) -> str:
    return f"w:{name}"


def place_key(name: str, iso3: str, state: str | None) -> str:
    return f"k:{name}@{place_of(iso3, state)}"


# ---------------------------------------------------------------- version 1 (v0.8.x)
V1_GLOBAL = {
    "cname": "country",
    "state": "us_state",
    "cmp_names": "compare_with",
    "a1": "p1_age",
    "a2": "p2_age",
    "p1": "p1_pay_level",
    "p2": "p2_pay_level",
    "d1": "p1_degree",
    "d2": "p2_degree",
    "career_on": "careers_follow_market",
    "jm": "job_market_ahead",
    "edu_pay": "pay_follows_degree",
    "g1": "p1_career_growth",
    "g2": "p2_career_growth",
    "married": "get_married",
    "cc_from": "childcare_from_age",
    "leave_who": "main_leave_taker",
    "work_mode": "main_parent_work",
    "leave_scale": "parental_leave_scale",
    "mode": "tenure",
    "r_out": "mortgage_rate_after_fixed",
    "rules": "country_lending_rules",
    "dsti": "max_payment_pct",
    "budget_scale": "lifestyle_scale",
    "urisk": "unemployment_risk",
    "efund": "emergency_fund_months",
    "protect": "cut_before_savings",
    "jl_on": "job_loss",
    "jl_who": "job_loss_partner",
    "jl_m": "job_loss_months",
    "church": "church_tax",
    "priv_h": "private_health",
    "elast": "spending_elasticity",
    "elast_hi": "spending_elasticity_high",
    "uni": "university_share",
    "uni_y": "university_years",
    "wpen": "child_pay_penalty",
    "ltc_y": "care_years",
    "spend_down": "retirement_spending",
    "hold": "savings_mix",
    "costs_out": "costs_outlook",
    "real": "real_terms",
    "cur": "currency",
    "us_cit": "us_citizens",
    "us_emp": "us_employer",
}
# keyed by partner 1's age in version 1
V1_BY_AGE = {
    "m_age": "marriage_age",
    "k1": "child1_age",
    "k2": "child2_age",
    "buy_age": "buy_age",
    "jl_age": "job_loss_age",
}
V1_PLACE = {
    "j1": "p1_job_now",
    "j2": "p2_job_now",
    "pn1": "p1_pay_now",
    "pn2": "p2_pay_now",
    "ret": "retirement_age",
    "hz": "horizon_age",
    "coh": "generation_gap",
    "wed": "wedding_cost",
    "home": "children_home_until",
    "cc": "childcare_cost",
    "rent": "rent",
    "dp": "down_payment_pct",
    "tt": "transfer_tax_pct",
    "mr": "mortgage_rate",
    "term": "mortgage_term",
    "own": "owner_costs_pct",
    "hout": "home_outlook",
    "appr": "home_growth",
    "cars": "cars",
    "em": "unexpected_costs",
    "slr": "student_loan_rate",
    "sly": "student_loan_years",
    "surv": "survey_correction",
    "gift": "family_gift",
    "gifta": "gift_age",
    "inh": "inheritance",
    "inha": "inheritance_age",
    "ltc": "care_cost",
    "cash": "cash_return",
    "depr": "deposit_return",
    "infl": "inflation",
    "w_out": "wage_outlook",
    "defl": "deflator",
}
# keyed by what their default depended on in version 1: (old prefix, new name), longest
# prefixes first so "own" (owner costs) is not mistaken for "own..." (already own a home)
V1_PLACE_DEP = [
    ("starting_savings", "starting_savings"),
    ("starting_retirement", "starting_retirement"),
    ("home_value_now", "home_value_now"),
    ("mortgage_now", "mortgage_now"),
    ("hp", "home_price"),
    ("own", "own_home"),
    ("mr0", "own_mortgage_rate"),
    ("my0", "own_mortgage_years"),
    ("sl", "student_debt"),
    ("dep", "deposit_pct"),
    ("eq", "equity_pct"),
    ("ret", "investment_return"),
    ("rwg", "real_wage_growth"),
]
_V1_PLACE_RX = re.compile(r"^k:(?P<name>.+)_(?P<iso>[A-Z]{3})_(?P<state>[A-Z]{2}|None)$")


def _v1_place_name(name: str) -> str | None:
    if name in V1_PLACE:
        return V1_PLACE[name]
    for old, new in V1_PLACE_DEP:
        if name.startswith(old) and name != old:
            return new
    return None


def migrate(version: int, settings: dict) -> dict:
    """Settings saved by an older version -> today's names (unknown ones are dropped).
    Version 1 could hold one value per dependency (partner 1's age, ...): for those keyed
    by age, the value for the saved age is kept; otherwise the first one."""
    if version >= VERSION:
        return dict(settings)
    out: dict = {}
    a1 = settings.get("w:a1", 23)
    for k, v in settings.items():
        if not isinstance(k, str):
            continue
        if k.startswith("w:"):
            name = k[2:]
            if name in V1_GLOBAL:
                out[global_key(V1_GLOBAL[name])] = v
                continue
            for old, new in V1_BY_AGE.items():
                if name == f"{old}{a1}":
                    out[global_key(new)] = v
        elif m := _V1_PLACE_RX.match(k):
            new = _v1_place_name(m["name"])
            if new is not None:
                state = None if m["state"] == "None" else m["state"]
                out.setdefault(place_key(new, m["iso"], state), v)
    return out

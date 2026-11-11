"""Data model for the life-plan simulator.

`Sourced` carries every number together with its provenance so the UI can show
source, year, estimated flag and quality next to each default. `CountryData` is
everything the engine needs for one country (built from the pipeline CSVs by
econsim.data); `Plan` is what the user chooses.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass(frozen=True)
class Sourced:
    value: float
    unit: str = ""
    source: str = ""
    year: object = None
    estimated: bool = False
    quality: str = ""          # A official, B survey/industry, C rough/derived
    note: str = ""

    def with_value(self, v: float, **kw) -> "Sourced":
        return replace(self, value=v, **kw)

    @property
    def label(self) -> str:
        bits = [self.source or "?"]
        if self.year not in (None, ""):
            bits.append(str(self.year))
        if self.estimated:
            bits.append("ESTIMATE")
        if self.quality:
            bits.append(f"quality {self.quality}")
        return " · ".join(bits)


USER = "user input"


def user(v: float, unit: str = "") -> Sourced:
    return Sourced(v, unit, USER, None, False, "", "")


@dataclass
class TaxPoint:
    aw_multiple: float
    income_tax_rate: float      # fraction of gross
    ssc_rate: float             # employee social security, fraction of gross


@dataclass
class CountryData:
    iso3: str
    name: str
    currency: str
    data_year: int                                  # price year all money is expressed in
    # earnings: percentile -> sorted [(age, annual gross local)]
    earnings: dict[str, list[tuple[float, float]]]
    earnings_src: Sourced
    tax_points: list[TaxPoint]
    avg_wage: Sourced                                # AW for the tax curve (annual, local)
    marginal_rate_top: Optional[float]
    tax_src: Sourced
    child_benefit_per_child: Sourced                 # annual, local
    budget: dict[str, Sourced]                       # monthly, local, baseline couple (no housing)
    rent: Sourced                                    # monthly couple rent excl. utilities
    rent_factor_per_child: list[float]               # multiplier for 1, 2 children
    rent_factor_src: Sourced
    home_price: Sourced
    down_payment_pct: Sourced
    transfer_tax_pct: Sourced
    mortgage_rate: Sourced
    wedding_cost: Sourced
    child_extra_spending: Sourced                    # monthly per child, local
    childcare_per_child: Sourced                     # annual per child in care, local
    emergency_expense_size: Sourced                  # one unexpected expense, local
    nrr: dict[int, Sourced]                          # months -> household net replacement %
    unemployment_rate: Sourced
    inflation_hist: Sourced                          # % per year, 10y CPI average
    real_wage_growth_hist: Sourced                   # % per year
    lcu_per_usd: Sourced
    lcu_per_intl: Sourced
    totalization: bool
    notes: list[str] = field(default_factory=list)
    # ---- curated rules (econsim/params) -------------------------------------------
    hh: dict = field(default_factory=dict)           # household.csv row (cars, health, ...)
    lend: dict = field(default_factory=dict)         # lending.csv row (limits, taxes, ...)
    budget_ref_income: Optional[Sourced] = None      # net income of the survey households
    budget_ref_housing: Optional[Sourced] = None     # their housing costs excl. utilities
    rent_src_year: Optional[int] = None
    unemp_youth: float = float("nan")                # % of labour force, 15-24
    unemp_total: float = float("nan")                # %, 15-74
    cpi: dict = field(default_factory=dict)          # year -> CPI index (rolls old data)
    state: Optional[str] = None                      # US state code (USA only)
    state_tax: Optional[dict] = None                 # us_state_tax.csv row
    state_info: Optional[dict] = None                # us_states.csv row


@dataclass
class Earner:
    age: int
    percentile: str = "median"       # p25 / median / p75 (p10, p90 where available)
    career_growth: float = 1.0       # 1 = follow the data's age-earnings profile
    works: bool = True


@dataclass
class JobLoss:
    earner: int                      # 0 or 1
    age: int                         # partner-1 age when it starts
    months: int = 6


@dataclass
class Plan:
    country: str
    earners: tuple[Earner, Earner]
    start_year: int = 2026
    horizon_age: int = 67            # partner-1 age at which the simulation ends
    retirement_age: int = 67         # earnings stop (both partners, own age)
    marriage_age: Optional[int] = 30
    wedding_cost: Optional[float] = None          # None -> data default
    child_ages: tuple[Optional[int], Optional[int]] = (32, 34)   # partner-1 age at birth
    childcare_from: int = 1          # child age childcare starts
    childcare_to: Optional[int] = None  # last child age in full-time paid care; None ->
    #                                  country (until free pre-school, then after-school care)
    child_dependent_to: int = 18     # last age with extra household spending
    buy_house_age: Optional[int] = 35             # None -> rent forever
    home_price: Optional[float] = None
    down_payment_pct: Optional[float] = None
    transfer_tax_pct: Optional[float] = None
    closing_cost_pct: float = 1.5    # notary, valuation, fees on top of transfer tax
    mortgage_rate: Optional[float] = None
    mortgage_years: Optional[int] = None  # None -> country typical term (25 without rules)
    max_payment_to_income: Optional[float] = None  # % of net; None -> country lending rules
    owner_cost_pct: Optional[float] = None  # upkeep+property tax+insurance %/yr; None -> country
    home_real_appreciation: Optional[float] = None  # %/yr above inflation (homes AND rents);
    #                                  None -> the real wage growth (constant price-to-income)
    rent: Optional[float] = None     # monthly, local; None -> data default
    budget_scale: float = 1.0        # lifestyle multiplier on the survey budget
    emergency_annual_cost: Optional[float] = None  # None -> 1 unexpected expense / yr
    emergency_fund_months: float = 6.0
    job_loss: Optional[JobLoss] = None
    return_nominal: float = 5.0      # % per year on invested savings, after fees, BEFORE tax
    #                                  (the country's tax drag is subtracted)
    cash_return: float = 2.0         # % per year on the emergency fund
    debt_rate: float = 9.0           # % per year if savings go negative
    inflation: Optional[float] = None             # None -> 2.0
    real_wage_growth: Optional[float] = None      # None -> min(hist, 1.0), floor 0
    us_citizens: bool = True
    us_employer: bool = False        # True -> US FICA where no totalization agreement
    starting_savings: float = 0.0
    # ---- realism options --------------------------------------------------------------
    cars: Optional[int] = None       # cars owned; None -> country typical
    unemployment_risk: bool = True   # deduct the expected cost of unemployment each year
    primary_parent: int = 1          # earner index (0/1) taking the main parental leave
    leave_scale: float = 1.0         # x the country's typical leave take-up
    private_health: bool = False     # optional private cover (e.g. Ireland)
    church_tax: bool = False         # Germany: 8-9% of income tax for church members
    student_loan: float = 0.0        # balance at the start (local currency, per household)
    budget_elasticity: float = 0.6   # spending rises with income^elasticity (vs survey couple)
    state: Optional[str] = None      # US state (USA only); None -> national averages

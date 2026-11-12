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

# Highest completed education (ISCED 2011), as used by labour_market / education_pay.
EDUCATION = {
    "below_upper_secondary": "No high-school diploma / below upper secondary",
    "upper_secondary": "High school / upper secondary (incl. some college, vocational)",
    "short_tertiary": "Associate's / short-cycle tertiary",
    "bachelor": "Bachelor's degree",
    "master_plus": "Master's, professional degree or doctorate",
}
TERTIARY = {"short_tertiary", "bachelor", "master_plus"}


def age_band(age: int) -> str:
    """labour_market age band for an age."""
    return "20-24" if age < 25 else "25-29" if age < 30 else "30-34" if age < 35 else "25-64"


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
    # job market by age band and education (labour_market.csv), latest year:
    # (indicator, age_band, education) -> Sourced; indicators: unemployment, functional,
    # hours_underemployment, overqualification
    labour: dict = field(default_factory=dict)
    # full-time pay by education relative to all levels (education_pay.csv):
    # (education, age_band) -> factor; bands 25-34 / 35-44 / 45-54 / 55-64
    edu_pay: dict = field(default_factory=dict)
    edu_pay_src: Optional[Sourced] = None
    # child penalty for the primary parent (family.csv): (n_children "1"/"2"/"3",
    # youngest "lt6"/"6-11"/"ge12", education) -> factor vs women without children
    family_earn: dict = field(default_factory=dict)   # employment x hours
    family_emp: dict = field(default_factory=dict)    # employment only (childcare need)
    family_src: Optional[Sourced] = None
    leave_home_age: Optional[Sourced] = None          # children stop being dependants
    # survey under-recording: budget category -> national accounts / HBS ratio (>= 1)
    budget_coverage: dict = field(default_factory=dict)
    # ---- the years ahead (forward-looking) ------------------------------------------
    retirement_age: Optional[Sourced] = None          # normal age for today's entrants
    le65: dict = field(default_factory=dict)          # (sex, year) -> age reached at 65
    pension_nrr: dict = field(default_factory=dict)   # x average wage -> net repl. %
    pension_src: Optional[Sourced] = None
    rates: dict = field(default_factory=dict)         # rates.csv row (10y yield, ...)
    university_cost: Optional[Sourced] = None         # household pays per student-year
    student_debt: dict = field(default_factory=dict)  # education -> Sourced (borrowers)
    wage_trend: Optional[Sourced] = None              # real avg-wage growth, ~20 years
    home_trend: Optional[Sourced] = None              # real house-price growth, ~20 years
    rent_trend: Optional[Sourced] = None              # real rent growth, ~20 years
    portfolio: dict = field(default_factory=dict)     # portfolio.csv row: deposit_share,
    #                                                   equity_share, overnight_share


@dataclass
class Earner:
    age: int
    percentile: str = "median"       # p25 / median / p75 (p10, p90 where available)
    career_growth: float = 1.0       # 1 = follow the data's age-earnings profile
    works: bool = True
    education: Optional[str] = None  # highest degree (see EDUCATION); None = all levels


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
    horizon_age: Optional[int] = None   # partner-1 age at which the simulation ends; None
    #                                  -> the longer-living partner's projected lifespan
    retirement_age: Optional[int] = None  # earnings stop, pension starts (own age); None ->
    #                                  the country's normal age for today's entrants
    marriage_age: Optional[int] = 30
    wedding_cost: Optional[float] = None          # None -> data default
    child_ages: tuple[Optional[int], Optional[int]] = (32, 34)   # partner-1 age at birth
    childcare_from: int = 1          # child age childcare starts
    childcare_to: Optional[int] = None  # last child age in full-time paid care; None ->
    #                                  country (until free pre-school, then after-school care)
    child_dependent_to: Optional[int] = None  # last age living at home (extra spending);
    #                                  None -> the country's average age of leaving home
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
    return_nominal: Optional[float] = None  # % per year on invested savings, after fees,
    #                                  BEFORE tax (the country's tax drag is subtracted);
    #                                  None -> today's 10-year yield + equity_share x
    #                                  equity_premium - fund_fees
    equity_share: Optional[float] = None  # share of invested savings in shares (rest:
    #                                  bonds); None -> the country's households (OECD)
    deposit_share: Optional[float] = None  # % of savings above the emergency fund kept in
    #                                  bank deposits; None -> the country's households
    #                                  (OECD balance sheets); 0 = invest everything
    deposit_return: Optional[float] = None  # % per year on those deposits; None -> what
    #                                  banks pay households now (ECB / FDIC)
    equity_premium: float = 3.5      # expected return of shares over 10-year govt bonds
    fund_fees: float = 0.2           # % per year (index funds)
    glide_path: bool = True          # shares fall to half of equity_share by retirement
    cash_return: Optional[float] = None  # % per year on the emergency fund; None -> the
    #                                  overnight-deposit rate banks pay households now
    debt_rate: float = 9.0           # % per year if savings go negative
    inflation: Optional[float] = None             # None -> 2.0
    real_wage_growth: Optional[float] = None      # None -> see wage_outlook
    # ---- outlook: how today's conditions carry forward ---------------------------------
    wage_outlook: str = "projection"  # "projection" (official long-run productivity
    #                                  projections) | "trend" (last ~20 years continue)
    home_outlook: str = "incomes"    # "incomes" (prices and rents keep pace with wages) |
    #                                  "trend" (last ~20 years' real trend for 10 years,
    #                                  then with incomes)
    rate_outlook: str = "market"     # rate a fixed mortgage resets to: "market" (today's
    #                                  10-year yield + usual mortgage spread) | "history"
    #                                  (20-year average mortgage rate)
    spend_down: bool = True          # True: retirees spend pension + savings / years left;
    #                                  False: only the pension (savings left to heirs)
    # ---- optional life factors (off by default) ----------------------------------------
    family_gift: float = 0.0         # help from family (today's money), e.g. a deposit
    family_gift_age: Optional[int] = None   # P1 age; None -> the planned buying age
    inheritance: float = 0.0         # after inheritance tax (today's money)
    inheritance_age: Optional[int] = None   # P1 age; None -> when the parents (30 years
    #                                  older) reach today's life expectancy at 65
    university_share: float = 0.0    # % of each child's university costs the parents pay
    university_years: int = 4
    university_from: int = 18        # child age at the start
    typical_student_debt: bool = False  # start with the country's typical debt for each
    #                                  partner's degree (borrowers' average; aged <= 30)
    child_wage_penalty: float = 0.0  # % lasting cut in the main parent's hourly pay after
    #                                  the first child (Kleven et al.: ~1/3 of a ~20% gap)
    ltc_years: int = 0               # years of paid long-term care for one partner at the end
    ltc_cost: Optional[float] = None  # per year, today's money; None -> country default
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
    student_loan_rate: Optional[float] = None  # %/yr; None -> the country's main scheme
    student_loan_years: Optional[int] = None   # repayment term; None -> the scheme's
    #                                  (US standard plan 10; UK: 9% above the threshold)
    budget_elasticity: float = 0.8   # spending rises with income^elasticity relative to the
    #                                  survey couple (calibrated on Eurostat saving by age)
    survey_correction: float = 0.0   # share (exponent) of the national-accounts gap in
    #                                  survey spending that is added back (0 = raw survey;
    #                                  0.5 fits hardship surveys best but makes buying
    #                                  implausibly hard where the gap is only estimated)
    child_penalty: bool = True       # primary parent works the country's typical hours
    #                                  while children are young (employment x hours of
    #                                  mothers vs women without children)
    state: Optional[str] = None      # US state (USA only); None -> national averages
    education_pay: bool = True       # scale pay to each earner's degree (if one is set)

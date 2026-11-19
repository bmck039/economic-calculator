"""The model's assumptions in one place: behaviour calibrated on the backtests, fixed rules,
and the categories of the living budget the rules work on."""

from __future__ import annotations

# ---------------------------------------------------------------- calibrated behaviour
# Calibrated in econsim.backtest (SCF couples 2004-2022 run through what happened, vs the
# same birth years in later waves) and econsim.calibrate (national saving rates):
SURVEY_CORRECTION = 1.0  # the survey basket raised to national-accounts totals
WEALTH_MPC = 0.04  # share of free savings spent per year before retirement
#                                   (not by renters saving for a home); Carroll, Otsuka &
#                                   Slacalek 2011: 2-6 cents per dollar of wealth - the
#                                   backtest cannot tell values apart (few couples hold
#                                   much above the emergency fund)
BUDGET_ELASTICITY_HIGH = 0.4  # spending vs income above the survey couple's (below:
#                                   Plan.budget_elasticity, 1.0); both fitted on the US and
#                                   euro-area backtests incl. the spread within cohorts
PORTFOLIO_K = 1.0  # free savings (years of income) at which a household's
#                                   mix is halfway from typical households' (bottom 70%) to
#                                   the whole household sector's (led by the wealthiest)
WEALTH_MPC_DECLINE = 0.0  # share of savings spent x (1 + savings/income)^-this
HOUSING_ELASTICITY = 0.75  # home price vs income relative to the survey couple (owners'
#                                   permanent-income elasticity 0.5-0.7 in the literature,
#                                   Mayo 1981; ~1 with education as the instrument, ACS)
CAR_ELASTICITY = 0.25  # car costs vs the same
SCALE_BOUNDS = (0.25, 4.0)  # spending vs the survey couple's, at the extremes of income
HOME_DRIFT = ((999, 0.0),)  # owned home vs the price index, by age: none - a repeat-
#                                   sales index follows the same houses, so it already
#                                   includes their ageing; SCF owner cohorts' homes kept pace
#                                   with it (or gained, trading up), 1989-2025, nominal

# ---------------------------------------------------------------- when money runs short
# A household that would run out of money cuts discretionary spending (up to MAX_SQUEEZE
# of it) before borrowing - liquidity constraints (Deaton 1991, Zeldes 1989); children's
# costs count CHILD_DISCRETIONARY. Beyond BORROW_LIMIT, living costs are cut by up to
# SUBSIST_CUT.
DISCRETIONARY = {
    "budget:recreation",
    "budget:restaurants_hotels",
    "budget:clothing",
    "budget:furnishings_household",
    "budget:alcohol_tobacco",
    "budget:personal_misc",
}
MAX_SQUEEZE = 0.5
CHILD_DISCRETIONARY = 0.4
BORROW_LIMIT = 0.25  # unsecured debt a household can carry, x a year's net income
SUBSIST_CUT = 0.3  # beyond it, living costs are cut by up to this share

# ---------------------------------------------------------------- living budget
VEHICLE_CATS = {
    "vehicle_purchase",
    "fuel_vehicle",
    "vehicle_maintenance",
    "vehicle_other",
    "insurance_vehicle",
    "vehicle_running",
    "vehicle_all",
}
HEALTH_INS_CATS = {"insurance_health", "health_insurance_mandatory"}
# spending per person (scaled for children): food, clothes, recreation, ... - energy,
# internet, furnishings, insurance or council tax barely change with children
PER_PERSON_CATS = {
    "groceries",
    "clothing",
    "recreation",
    "restaurants_hotels",
    "personal_misc",
    "transport_services",
    "transport_all",
    "cp07_unsplit",
    "education",
    "health_out_of_pocket",
    "cp01_unsplit",
}
PRICE_TREND_YEARS = 10  # how long today's relative price trends continue
YOUNG_DRIVER = ((25, 1.7), (30, 1.25))  # car insurance x this below these ages
FPL_2025 = (10_150.0, 5_500.0)  # HHS poverty guideline 2025: 10,150 + 5,500 per person
IRL_PRIVATE_HEALTH = (1600.0, 400.0)  # EUR/yr adult, child (VHI/Laya/Irish Life mid plan)
LTC_DEFAULT = {
    "USA": (
        74_400,
        "CareScout Cost of Care 2025: national median assisted "
        "living (private nursing-home room: 129,575)",
        2025,
    )
}
CHURCH_TAX = {"DEU": 0.085}  # share of income tax (8% BY/BW, 9% elsewhere)

# ---------------------------------------------------------------- renting
# Measured on US microdata (tools/rent_research.py: Census ACS PUMS 2013/2018/2023, married
# renters, within the same area; CPS ASEC 2023-2025). Each country's elasticity and how
# often its renters move relative to the US: params/renting.csv (OECD, EU-SILC).
RENT_ELASTICITY = 0.4  # US: rent of a new lease vs (permanent) income: 0.41-0.43 with
#                        education x age as the instrument (0.2 on one year's income);
#                        renter-movers 0.38 (Goodman & Kawai 1986), most studies 0.3-0.5
#                        (Mayo 1981)
TENURE_DISCOUNT = 0.015  # sitting tenants' rent rises this much less a year than market
#                          rents: -7 to -8% after 2-4 years, -13 to -17% after 5-9 (2013,
#                          2018; same size of home, area, income - partly tenants keeping
#                          cheap homes); Austria 1.3%/yr (Knell 2025), Canada -19% after
#                          5+ years (StatCan 2025). x the country's mover_scale: the
#                          rent index follows all tenancies, and leases reset on moving
#                          are what lift it above a sitting tenant's rent
RENTER_MOVES = (  # US: married renters who moved in the last year, by age (CPS ASEC);
    #               x the country's renting.csv mover_scale
    (25, 0.35),
    (30, 0.285),
    (35, 0.19),
    (40, 0.163),
    (45, 0.116),
    (55, 0.096),
    (65, 0.102),
    (999, 0.054),
)
RENT_BURDEN_MAX = 0.6  # rent above this share of net income (HUD's "severe" burden: half
#                        of gross income) and a cheaper place is found: move
RENT_CHILD_FACTOR = (1.07, 1.16)  # new lease with 1 / 2+ children vs none, same income and
#                                   area (ACS: +5-8% / +11-16%); a country's own data first

# ---------------------------------------------------------------- homes and mortgages
CH_STYLE = {"CHE", "LIE"}  # interest-only first mortgage, 2nd tranche amortised
OWNER_MAINTENANCE_PCT = 0.7  # upkeep beyond the minor repairs already in the budget
MOVING_PCT = 1.0  # removals + furnishing a bigger home, % of the price
REFI_COUNTRIES = {"USA", "DNK"}  # loans repaid without penalty: refinanced when cheaper
REFI_GAP = 0.01  # refinance when new loans cost this much less
REFI_COST = 0.01  # closing costs, added to the balance

# ---------------------------------------------------------------- people
PAY_BANDS = ((30, "25-34"), (40, "35-44"), (50, "45-54"), (60, "55-64"))  # band midpoints
SW_EDU = {
    "bachelor": "degree",
    "master_plus": "degree",
    "short_tertiary": "no_degree",
    "upper_secondary": "no_degree",
    "below_upper_secondary": "no_degree",
}
LAST_SURVIVOR_YEARS = 2.0  # a couple's second death comes ~2-4 years after the longer
#                             single life expectancy (independent lives); 2 is conservative

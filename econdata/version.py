"""The data version: bump it when tables built by older code must be rebuilt."""

from __future__ import annotations

# Bump when a pipeline change means tables built by older code must be rebuilt (run.py
# compares it with <out>/.data-version, which a successful build writes).
#   2: FRED fix - builds before it could silently lack all US earnings (no USA rows).
#   3: PRT/ROU budgets no longer include rent (unsplit COICOP 04); Canada mortgage rate
#      = rate paid on new insured loans, not the posted rate.
#   4: life_events gains the SILC income and housing costs of the survey households
#      (the app scales living costs with income after housing; v0.2.0 needs them).
#   5: labour_market + education_pay tables (job market by age and degree, pay by degree),
#      US CPI 2025 gap fill, current-year unemployment from monthly data.
#   6: family (child penalty, leaving-home age), budget_coverage (HBS vs national
#      accounts), pensions (OECD replacement rates); us_states from the ACS/BEA APIs.
#   7: pensions gains replacement rates at 0.5x/2x the average wage, future retirement
#      ages and projected life expectancy; rates (10-year yields, short rates, mortgage
#      spreads) for the retirement phase and forward-looking rates and returns.
#   8: rates gains what banks pay households on deposits (ECB, FDIC); portfolio (how
#      households split financial savings between deposits, bonds and shares: OECD).
#   9: us_career (CPS: situations, pay, year-on-year moves and raises by age and degree),
#      price_outlook (relative price trends by budget line; US market inflation outlook).
#  10: cohort (statistics by age x survey year for following cohorts: CPS ASEC, SCF, LFS,
#      SES), start_wealth (what US families of each age hold now: SCF), household cost
#      index (US: what households pay, incl. market rents and mortgage interest),
#      us_history + scf_households (backtests), part_time (EU LFS), typical household
#      portfolios (ECB DWA, SCF), spending_level (calibrated per country).
#  11: SCF waves in their own year's dollars (the Fed states all in the latest survey's),
#      scf_tables (the SCF in the HFCS layout), hfcs_tables (ECB HFCS statistical tables
#      2010-2021, compiled from the PDFs: reference/hfcs_tables.csv), eu_history (European
#      prices, wages, home prices, rents, rates, returns, unemployment), euro-area starting
#      wealth by age (HFCS), BIS house prices for Greece.
#  12: spending levels refitted with spending that follows income by income level (1.0
#      below a typical couple's, 0.4 above) and portfolios that grow with savings.
#  13: spending levels fitted on what households plan to spend (with the cuts made when
#      money runs short, saving could not fall much below zero, so a level far too high
#      still "fitted" - e.g. the US 1.49 instead of ~1.3, as the US backtest also finds).
#  14: labour_market: US history back to 2015 (CPS, one month a quarter), Eurostat slack
#      by age back-cast before 2021, underemployment (too few hours or over-qualified).
#  15: spending levels refitted with the renting rule (stay or move; rent ^0.4 with income).
DATA_VERSION = "15"

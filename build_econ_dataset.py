#!/usr/bin/env python3
"""
build_econ_dataset.py  (v2)
===========================

Builds a cached, tidy dataset for answering "what would the financial picture
of my life look like in country X?" for Europe (EU27 + EFTA + UK), the United
States and Canada.

What it pulls
-------------
INCOME
  earnings_by_age          Full-time gross earnings by age band (median + percentiles
                           where published). Eurostat SES 2002-2022 (4-yearly), ONS ASHE,
                           BLS CPS, StatCan LFS.  SES is *nowcast* forward to the latest
                           year with Eurostat's annual full-time-adjusted salary series
                           (rows flagged estimated=True).
  household_income         Equivalised disposable household income: EU-SILC (annual,
                           median + full percentile cut-offs, by age, by household type
                           e.g. "two adults < 65", by degree of urbanisation) and the
                           OECD Income Distribution Database (US, Canada, UK, others).
  net_income_tax_model     Gross->net for model households (two-earner couple 100%+67% of
                           the average wage, with/without 2 children): OECD Taxing Wages
                           and Eurostat net-earnings tables.
  avg_wage                 OECD average annual wages 2000+ (mean only - flagged) and
                           Eurostat average full-time-adjusted salary - used for 20-year
                           wage-stagnation analysis together with CPI.
MOBILITY / RAISES
  wage_growth_individual   Atlanta Fed Wage Growth Tracker (job stayers vs switchers, by
                           age, metro vs non-metro).
  income_transitions       Eurostat EU-SILC year-to-year moves between pay deciles;
                           StatCan one-year income mobility.
  (derived) age-earnings profiles and pseudo-cohort growth from SES waves.
JOB MARKET                 unemployment by age, job vacancy rate (as v1).
SAVING                     household saving rate (net OECD; gross Eurostat).
COST OF LIVING (raw costs, couple household, towns & suburbs where possible)
  household_spending       Household budget surveys by category: Eurostat HBS (couple
                           households, adjusted to "towns and suburbs"), BLS CE (married
                           couple only / with children), StatCan SHS (couples; by size
                           of area), ONS Family Spending (best effort).
  housing                  Rents, housing costs of market-rent tenants (EU-SILC),
                           house prices / indices, mortgage rates.
  energy / fuel prices     Electricity & gas unit prices, gasoline.
  health                   Out-of-pocket spending, catastrophic-spending shares.
  price_level              PPP & price level ratios (World Bank, Eurostat).
LIFE EVENTS (kept separate from the baseline budget)
  childcare_net_cost       OECD net childcare costs (couple, 2 children).
  child spending deltas    (derived) spending of couples with children minus couples.
  wedding, down payment, transfer tax, home-size price  -> reference table + derived.
EMERGENCIES / SHOCKS
  emergency                Share unable to face an unexpected expense, size of that
                           expense, catastrophic health spending, US out-of-pocket max,
                           unemployment net replacement rates (OECD).
UNCERTAINTY                World Uncertainty Index, WGI political stability (as v1).
US-CITIZEN-ABROAD TAX      Reference parameters (FEIE, housing base, FBAR/FATCA,
                           totalization agreements) + derived comparison of local vs US
                           average tax rates for the same model couple.

Outputs (in --out, default ./econ_data)
---------------------------------------
  econ_long.csv            Every observation (tidy). Key columns: iso3, measure,
                           statistic, age_band, career_stage, household_type, area_type,
                           category, period, year, value, unit, estimated, method,
                           source, series, comparability_note.
  econ_annual.csv          Sub-annual series averaged to years.
  reference_values.csv     Hand-curated values with sources (edit and pass back with
                           --reference to override).
  couple_budget.csv        Monthly cost of living for a couple, by category, in local
                           currency and international $ (latest prices).
  life_events.csv          Wedding, per-child costs, house purchase (price, down payment,
                           transfer tax, mortgage payment), emergency measures.
  real_wage_history.csv    Wages/incomes deflated to latest-year prices, indexed, CAGR.
  mobility_summary.csv     Age-earnings profiles, pseudo-cohort growth, raise metrics.
  expat_tax.csv            Local vs US tax rates for the model couple + US rules.
  country_snapshot.csv     One row per country with the headline numbers.
  earnings_ppp.csv         Earnings annualised in international $ (PPP).
  fetch_log.csv            Every request: cache hit/miss, URL.

Usage
-----
  pip install requests pandas openpyxl        # pyarrow optional (parquet copies)
  python build_econ_dataset.py                 # everything (first run ~10-20 min)
  python build_econ_dataset.py --only income cost_of_living
  python build_econ_dataset.py --refresh       # ignore cache
  python build_econ_dataset.py --home-m2 110   # size of the home used for prices
  python build_econ_dataset.py --reference my_refs.csv
  Optional: set CENSUS_API_KEY (free) for ACS rents; without it the US rent is a reference value.
  Optional: set FRED_API_KEY (free) to read FRED series through the official API instead of
  the fredgraph.csv download endpoint.

Exit status: 0 all sources fetched; 2 some sources failed but every country still has the
tables the app needs (fallbacks used); 1 fatal - nothing written except fetch_log.csv, any
previous build in --out is left untouched.

READ THE COMPARABILITY NOTES. Every row says what population, period and
definition it uses. Mixing sources is fine for "accurate-enough" pictures,
not for decimal-point comparisons.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode, urlparse

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("econ")

# ==========================================================================
# Countries
# ==========================================================================
# iso3 -> (iso2, Eurostat geo code, name, region)
COUNTRIES: dict[str, tuple[str, str, str, str]] = {
    "AUT": ("AT", "AT", "Austria", "EU"), "BEL": ("BE", "BE", "Belgium", "EU"),
    "BGR": ("BG", "BG", "Bulgaria", "EU"), "HRV": ("HR", "HR", "Croatia", "EU"),
    "CYP": ("CY", "CY", "Cyprus", "EU"), "CZE": ("CZ", "CZ", "Czechia", "EU"),
    "DNK": ("DK", "DK", "Denmark", "EU"), "EST": ("EE", "EE", "Estonia", "EU"),
    "FIN": ("FI", "FI", "Finland", "EU"), "FRA": ("FR", "FR", "France", "EU"),
    "DEU": ("DE", "DE", "Germany", "EU"), "GRC": ("GR", "EL", "Greece", "EU"),
    "HUN": ("HU", "HU", "Hungary", "EU"), "IRL": ("IE", "IE", "Ireland", "EU"),
    "ITA": ("IT", "IT", "Italy", "EU"), "LVA": ("LV", "LV", "Latvia", "EU"),
    "LTU": ("LT", "LT", "Lithuania", "EU"), "LUX": ("LU", "LU", "Luxembourg", "EU"),
    "MLT": ("MT", "MT", "Malta", "EU"), "NLD": ("NL", "NL", "Netherlands", "EU"),
    "POL": ("PL", "PL", "Poland", "EU"), "PRT": ("PT", "PT", "Portugal", "EU"),
    "ROU": ("RO", "RO", "Romania", "EU"), "SVK": ("SK", "SK", "Slovakia", "EU"),
    "SVN": ("SI", "SI", "Slovenia", "EU"), "ESP": ("ES", "ES", "Spain", "EU"),
    "SWE": ("SE", "SE", "Sweden", "EU"),
    "ISL": ("IS", "IS", "Iceland", "EFTA"), "LIE": ("LI", "LI", "Liechtenstein", "EFTA"),
    "NOR": ("NO", "NO", "Norway", "EFTA"), "CHE": ("CH", "CH", "Switzerland", "EFTA"),
    "GBR": ("GB", "UK", "United Kingdom", "UK"),
    "USA": ("US", "US", "United States", "North America"),
    "CAN": ("CA", "CA", "Canada", "North America"),
}
EUROPE = [k for k, v in COUNTRIES.items() if v[3] in ("EU", "EFTA", "UK")]
EUROSTAT_TO_ISO3 = {v[1]: k for k, v in COUNTRIES.items()}
ISO2_TO_ISO3 = {v[0]: k for k, v in COUNTRIES.items()}
ISO2_TO_ISO3["EL"] = "GRC"
ISO2_TO_ISO3["UK"] = "GBR"
OECD_MEMBERS = {"AUT", "BEL", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN",
                "IRL", "ITA", "LVA", "LTU", "LUX", "NLD", "POL", "PRT", "SVK", "SVN",
                "ESP", "SWE", "ISL", "NOR", "CHE", "GBR", "USA", "CAN"}
LOCAL_CURRENCY = {"USA": "USD", "CAN": "CAD", "GBR": "GBP", "CHE": "CHF", "NOR": "NOK",
                  "ISL": "ISK", "SWE": "SEK", "DNK": "DKK", "POL": "PLN", "CZE": "CZK",
                  "HUN": "HUF", "ROU": "RON", "LIE": "CHF",
                  # Bulgaria adopted the euro on 1 Jan 2026, but every historical series
                  # (World Bank FX/PPP, Eurostat NAC) is in lev: values stay in BGN
                  # (1 EUR = 1.95583 BGN, fixed).
                  "BGR": "BGN"}

USER_AGENT = "econ-dataset-builder/2.0 (+python-requests; personal research)"

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
DATA_VERSION = "9"

# ==========================================================================
# Comparability notes
# ==========================================================================
NOTES = {
    "ses": "Eurostat SES: gross monthly earnings in October, full-time employees, "
           "enterprises 10+ employees, NACE B-S excl. O; excludes annual bonuses. 4-yearly.",
    "ses_nowcast": "ESTIMATE: latest SES wave scaled by growth of the national mean "
                   "full-time-adjusted salary (Eurostat nama_10_fte; OECD average wage "
                   "where missing). Assumes the shape of the distribution is unchanged.",
    "ashe": "ONS ASHE: gross weekly pay in April, full-time employees on adult rates "
            "whose pay was unaffected by absence; employer-reported.",
    "cps": "BLS CPS: usual weekly earnings, full-time wage and salary workers, "
           "self-reported, annual average.",
    "lfs_ca": "StatCan LFS: usual weekly wages, full-time employees, self-reported, "
              "annual average.",
    "silc": "EU-SILC: equivalised disposable household income (modified OECD scale: "
            "1 for first adult, 0.5 other 14+, 0.3 children). Income year = survey year-1. "
            "For a couple's actual household income multiply by 1.5.",
    "idd": "OECD IDD: equivalised disposable household income (square-root scale). "
           "Not identical to EU-SILC's scale; compare trends rather than levels.",
    "taxing_wages": "OECD Taxing Wages model household; average wage (AW) = mean full-time "
                    "earnings in industry & services. Statutory calculation, not survey.",
    "earn_nt": "Eurostat net earnings: OECD-EC tax-benefit model for model households.",
    "avg_wage": "MEAN wage (not median): OECD average annual wages per full-time "
                "equivalent employee. Used only for trends/stagnation.",
    "nama_fte": "MEAN: Eurostat average full-time-adjusted salary per employee "
                "(national accounts). Used for trends and SES nowcast.",
    "une_eu": "Eurostat LFS: ILO unemployment rate, annual average.",
    "une_oecd": "OECD harmonised (ILO) unemployment rate; bands 15-24 / 25+.",
    "jvr_eu": "Eurostat JVS: vacancies/(vacancies+occupied posts), NACE B-S.",
    "jvr_us": "JOLTS job openings rate: openings/(employment+openings), SA.",
    "jvr_ca": "StatCan JVWS: vacancies/(payroll employees+vacancies), unadjusted.",
    "jvr_uk": "ONS vacancies per 100 employee jobs (AP2Z) converted to v/(100+v)*100.",
    "sav_net": "OECD: household + NPISH NET saving / net disposable income.",
    "sav_gross": "Eurostat: household + NPISH GROSS saving / gross disposable income.",
    "ppp": "World Bank ICP/WDI PPP; national averages, not city rents.",
    "hbs": "Eurostat HBS (2020 wave latest; COVID year): MEAN consumption expenditure "
           "per household. Category levels = couple-household total x category shares, "
           "adjusted to 'towns and suburbs', rolled to latest year with HICP sub-indices.",
    "ce": "BLS Consumer Expenditure Survey: MEAN annual spending per consumer unit; "
          "'married couple only' includes retirees (older than a young couple).",
    "shs": "StatCan Survey of Household Spending: MEAN spending per household.",
    "ons_fs": "ONS Family Spending: MEAN weekly spending per household (FYE).",
    "silc_housing": "EU-SILC total housing cost per household (rent or mortgage interest, "
                    "utilities, maintenance, insurance), PPS per month.",
    "acs": "Census ACS 1-year: median gross rent (contract rent + utilities) and median "
           "value of owner-occupied homes, all US.",
    "energy": "Unit prices incl. all taxes for a typical household consumption band.",
    "wgt": "Atlanta Fed Wage Growth Tracker: median 12-month change in hourly wage of "
           "the SAME individuals (CPS matched), 12-month moving average.",
    "lvhl": "EU-SILC longitudinal: share of employees moving between pay deciles "
            "from one year to the next.",
    "ncc": "OECD TaxBEN net childcare cost (fees minus benefits/tax credits) for full-time "
           "centre-based care, couple 100%+67% AW, 2 children aged 2 and 3.",
    "nrr": "OECD TaxBEN net replacement rate: household net income while unemployed as "
           "% of net income before job loss.",
    "mdes": "EU-SILC: share of households unable to pay an unexpected expense equal to "
            "the monthly national poverty threshold (one-person) from own resources.",
    "wui": "World Uncertainty Index (smoothed); text-based (EIU country reports).",
    "wgi": "World Bank WGI Political Stability estimate, approx. -2.5..2.5.",
    "ref": "Hand-curated reference value; see source/as_of. Edit reference_values.csv.",
}

# ==========================================================================
# Reference values (things with no free stable API). Edit freely; the script
# writes these to reference_values.csv and you can pass your own with
# --reference. quality: A official, B industry survey, C rough/scaled.
# ==========================================================================
REFERENCE_VALUES: list[dict] = [
    # ---- Weddings (MEANS - no medians published; they skew high) -------------
    dict(iso3="USA", topic="wedding", item="average_wedding_cost", value=34200, unit="USD",
         as_of="2025", quality="B", source="The Knot 2026 Real Weddings Study (10,474 couples)",
         url="https://www.theknot.com/content/average-wedding-cost",
         note="Mean; average guest count 117. Excludes engagement ring."),
    dict(iso3="USA", topic="wedding", item="average_engagement_ring", value=4600, unit="USD",
         as_of="2025", quality="B", source="The Knot 2026 Real Weddings Study",
         url="https://www.theknot.com/content/average-wedding-cost", note="Mean."),
    dict(iso3="GBR", topic="wedding", item="average_wedding_cost", value=20604, unit="GBP",
         as_of="2025", quality="B", source="Bridebook UK Wedding Report 2026 (7,000 couples)",
         url="https://bridebook.com/uk/article/how-much-does-a-wedding-cost-the-2025-uk-average",
         note="Mean; 25% spend <=10,000 GBP, 25% >=26,000 GBP; ~74 guests. Excl. honeymoon, ring."),
    # ---- Down payments / LTV --------------------------------------------------
    dict(iso3="USA", topic="house", item="down_payment_pct_first_time", value=10, unit="%",
         as_of="2025", quality="B", source="NAR 2025 Profile of Home Buyers and Sellers (median)",
         url="https://www.nar.realtor/press-releases/first-time-home-buyer-share-falls-to-historic-low-of-21-median-age-rises-to-40",
         note="Median first-time buyer age 40; repeat buyers 23%."),
    dict(iso3="GBR", topic="house", item="down_payment_pct_first_time", value=14.4, unit="%",
         as_of="2025", quality="A", source="ONS: median first-time-buyer LTV 85.6% in 2025",
         url="https://www.mpamag.com/uk/mortgage-types/first-time-buyer/first-time-buyer-ltv-hits-pre-crisis-high-of-856/590432",
         note="Deposit = 100 - median LTV."),
    dict(iso3="CAN", topic="house", item="down_payment_rule", value=float("nan"), unit="rule",
         as_of="2026", quality="A",
         source="Federal insured-mortgage rules: 5% of first $500k, 10% of $500k-$1.5M; >= $1.5M needs 20%",
         url="https://pegasuslending.com/blog/cmhc-mortgage-rules-2026-canada/",
         note="Insurance premium ~4.0% (5-9.99% down), ~3.1% (10-14.99%), ~2.8% (15-19.99%) added to loan."),
    dict(iso3="_EUROPE_DEFAULT", topic="house", item="down_payment_pct_first_time", value=20,
         unit="%", as_of="assumption", quality="C",
         source="Assumption: no harmonised free source for typical first-time-buyer LTV in Europe",
         url="", note="Many countries cap LTV at 80-90% (e.g. Sweden 85%). Override per country."),
    # ---- House price levels ----------------------------------------------------
    dict(iso3="CAN", topic="house", item="average_home_price", value=666538, unit="CAD",
         as_of="2026-09", quality="B", source="CREA national average MLS sale price, Sept 2026",
         url="https://storeys.com/canada-homes-cheaper-september-buyers/",
         note="MEAN of all sales (skewed by Toronto/Vancouver); not a suburban median."),
    dict(iso3="GBR", topic="house", item="average_home_price", value=273000, unit="GBP",
         as_of="2026-08", quality="A", source="ONS/HM Land Registry UK HPI, Aug 2026 (provisional)",
         url="https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/privaterentandhousepricesuk/october2026",
         note="Mix-adjusted average price, all property types."),
    dict(iso3="GBR", topic="rent", item="average_monthly_rent", value=1406, unit="GBP",
         as_of="2026-09", quality="A", source="ONS Price Index of Private Rents, Sept 2026",
         url="https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/privaterentandhousepricesuk/october2026",
         note="All private rented dwellings; England 1,466, Wales 848, Scotland 1,011, NI 879."),
] + [
    # Deloitte Property Index 2026 (2025 data): average BID price of NEW dwellings,
    # EUR/m2 (# = transaction price). City-weighted and new-build -> above typical
    # suburban resale prices. Rolled forward/back with Eurostat HPI by the script.
    dict(iso3=c, topic="house", item="new_dwelling_price_per_m2", value=v, unit="EUR/m2",
         as_of="2025", quality="B", source="Deloitte Property Index 2026",
         url="https://www.deloitte.com/content/dam/assets-zone2/at/de/docs/studien/2026/deloitte-property-index-2026.pdf",
         note=n)
    for c, v, n in [
        ("LUX", 9160, "apartments only"), ("CZE", 5412, ""), ("DEU", 5200, ""),
        ("AUT", 5199, ""), ("NLD", 5186, "transaction price"), ("FIN", 4962, ""),
        ("PRT", 4946, ""), ("NOR", 4134, ""), ("ESP", 4071, ""),
        ("IRL", 3860, "transaction price"), ("SVK", 3656, ""), ("HUN", 3438, ""),
        ("BEL", 3269, ""), ("POL", 3008, ""), ("GBR", 2912, "transaction price"),
        ("HRV", 2861, ""), ("ITA", 2734, ""), ("GRC", 1931, ""),
        ("ROU", 1917, "all dwellings, not only new")]
] + [
    # Real-estate transfer tax paid by buyer (standard residential), PwC RETT
    # overview May 2023. Ranges -> midpoint used as value; check local rules.
    dict(iso3=c, topic="house", item="transfer_tax_pct", value=v, unit="%",
         as_of="2023", quality="B", source="PwC Real Estate Transfer Tax overview (May 2023)",
         url="https://www.pwc.com/gx/en/financial-services/publications/assets/pwc-real-estate-transfer-tax.pdf",
         note=n)
    for c, v, n in [
        ("AUT", 3.5, ""), ("BEL", 12.25, "12-12.5% by region; reduced rates for own home"),
        ("DNK", 0.6, ""), ("FIN", 4.0, "2% for housing-company shares (most apartments)"),
        ("FRA", 5.8, "older buildings; VAT on new"), ("DEU", 5.0, "3.5-6.5% by state"),
        ("IRL", 1.0, "1% up to EUR 1m"), ("ITA", 2.0, "2% first home; 9% otherwise"),
        ("LUX", 7.0, "10% in Luxembourg City"),
        ("NLD", 2.0, "2% primary residence (10.4% otherwise)"), ("NOR", 2.5, ""),
        ("POL", 2.0, "used homes; VAT on new"), ("PRT", 7.5, "5-10% by price"),
        ("ESP", 8.5, "6-11% by region"), ("SWE", 1.5, ""), ("CHE", 1.65, "0-3.3% by canton"),
        ("GBR", float("nan"), "SDLT bands 0-12% (England/NI); compute from price")]
] + [
    # ---- Health ------------------------------------------------------------------
    dict(iso3="USA", topic="health", item="employer_premium_family_total", value=26993,
         unit="USD/yr", as_of="2025", quality="A", source="KFF Employer Health Benefits Survey 2025",
         url="https://files.kff.org/attachment/Employer-Health-Benefits-Survey-2025-Annual-Survey-Summary-of-Findings.pdf",
         note="Mean total premium; employer pays most."),
    dict(iso3="USA", topic="health", item="employer_premium_family_worker_share", value=6850,
         unit="USD/yr", as_of="2025", quality="A", source="KFF EHBS 2025",
         url="https://files.kff.org/attachment/Employer-Health-Benefits-Survey-2025-Annual-Survey-Summary-of-Findings.pdf",
         note="Mean worker contribution, family coverage (26%)."),
    dict(iso3="USA", topic="health", item="employer_premium_single_worker_share", value=1440,
         unit="USD/yr", as_of="2025", quality="A", source="KFF EHBS 2025",
         url="https://files.kff.org/attachment/Employer-Health-Benefits-Survey-2025-Annual-Survey-Summary-of-Findings.pdf",
         note="Mean worker contribution, single coverage (16%); single premium 9,325."),
    dict(iso3="USA", topic="health", item="average_deductible_single", value=1886,
         unit="USD/yr", as_of="2025", quality="A", source="KFF EHBS 2025",
         url="https://files.kff.org/attachment/Employer-Health-Benefits-Survey-2025-Annual-Survey-Summary-of-Findings.pdf",
         note="Workers with a general deductible."),
    dict(iso3="USA", topic="emergency", item="max_out_of_pocket_family", value=21200,
         unit="USD/yr", as_of="2026", quality="A", source="HHS 2026 ACA cost-sharing limit (revised)",
         url="https://blog.nisbenefits.com/cost-sharing-limits-revised-2026-plan-years",
         note="Worst-case in-network medical bill per year for an insured family; self-only 10,600."),
    # ---- Emergencies ---------------------------------------------------------------
    dict(iso3="USA", topic="emergency", item="share_cannot_cover_unexpected_expense",
         value=37, unit="% of adults", as_of="2025", quality="A",
         source="Federal Reserve SHED 2025: 63% would cover a $400 expense with cash or equivalent",
         url="https://mondovisione.com/media-and-resources/news/federal-reserve-board-issues-economic-well-being-of-us-households-in-2025-repo-2026513/",
         note="Unexpected expense size = 400 USD."),
    dict(iso3="USA", topic="emergency", item="unexpected_expense_size", value=400, unit="USD",
         as_of="2025", quality="A", source="Federal Reserve SHED", url="", note=""),
    dict(iso3="CAN", topic="emergency", item="share_cannot_cover_unexpected_expense",
         value=26, unit="% of people", as_of="2022-fall", quality="A",
         source="Statistics Canada: 26% unable to cover an unexpected $500 expense",
         url="https://www.statcan.gc.ca/o1/en/node/2966", note="Unexpected expense size = 500 CAD."),
    dict(iso3="CAN", topic="emergency", item="unexpected_expense_size", value=500, unit="CAD",
         as_of="2022", quality="A", source="Statistics Canada Canadian Social Survey", url="", note=""),
    # ---- US citizen abroad -----------------------------------------------------------
    dict(iso3="USA", topic="expat_tax", item="feie_max_exclusion", value=132900, unit="USD/person",
         as_of="2026", quality="A", source="IRS inflation adjustment (2025: 130,000)",
         url="https://www.cpapracticeadvisor.com/2026/04/29/irs-increases-foreign-earned-income-tax-breaks/182444/",
         note="Per qualifying person: each spouse can exclude own earned income."),
    dict(iso3="USA", topic="expat_tax", item="foreign_housing_base_amount", value=21264,
         unit="USD", as_of="2026", quality="A", source="IRS Notice 2026-25",
         url="https://www.cpapracticeadvisor.com/2026/04/29/irs-increases-foreign-earned-income-tax-breaks/182444/",
         note="Housing costs above this (up to a location limit) can be excluded."),
    dict(iso3="USA", topic="expat_tax", item="fbar_threshold", value=10000, unit="USD",
         as_of="statutory", quality="A", source="FinCEN Form 114 (aggregate foreign accounts)",
         url="https://www.fincen.gov/report-foreign-bank-and-financial-accounts", note=""),
    dict(iso3="USA", topic="expat_tax", item="fatca_8938_threshold_mfj_abroad_year_end",
         value=400000, unit="USD", as_of="statutory", quality="A",
         source="IRS Form 8938 instructions (married filing jointly, living abroad; 600k any time)",
         url="https://www.irs.gov/businesses/corporations/summary-of-fatca-reporting-for-us-taxpayers",
         note="Single abroad: 200k year-end / 300k any time."),
    # ---- US federal tax parameters used by the expat tax layer (2026) ----------------
    dict(iso3="USA", topic="us_tax", item="std_deduction_mfj", value=32200, unit="USD",
         as_of="2026", quality="A", source="IRS Rev. Proc. 2025-32 (IR-2025-103)",
         url="https://www.cpapracticeadvisor.com/2025/10/09/irs-adjusts-tax-brackets-standard-deduction-for-2026/170661/",
         note="Single 16,100."),
    dict(iso3="USA", topic="us_tax", item="std_deduction_single", value=16100, unit="USD",
         as_of="2026", quality="A", source="IRS Rev. Proc. 2025-32", url="", note=""),
    dict(iso3="USA", topic="us_tax", item="brackets_mfj", value=float("nan"),
         unit="USD thresholds", as_of="2026", quality="A", source="IRS Rev. Proc. 2025-32",
         url="https://www.cpapracticeadvisor.com/2025/10/09/irs-adjusts-tax-brackets-standard-deduction-for-2026/170661/",
         note="10:0;12:24800;22:100800;24:211400;32:403550;35:512450;37:768700"),
    dict(iso3="USA", topic="us_tax", item="brackets_single", value=float("nan"),
         unit="USD thresholds", as_of="2026", quality="A", source="IRS Rev. Proc. 2025-32",
         url="", note="10:0;12:12400;22:50400;24:105700;32:201775;35:256225;37:640600"),
    dict(iso3="USA", topic="us_tax", item="child_tax_credit", value=2200, unit="USD/child",
         as_of="2026", quality="A", source="One Big Beautiful Bill Act / IRS",
         url="", note="Refundable part (ACTC) up to 1,700; ACTC not allowed when claiming FEIE."),
    dict(iso3="USA", topic="us_tax", item="child_tax_credit_refundable", value=1700,
         unit="USD/child", as_of="2026", quality="A", source="IRS", url="", note=""),
    dict(iso3="USA", topic="us_tax", item="fica_employee_rate", value=7.65, unit="%",
         as_of="2026", quality="A", source="SSA", url="",
         note="6.2% OASDI up to the wage base + 1.45% Medicare."),
    dict(iso3="USA", topic="us_tax", item="ss_wage_base", value=184500, unit="USD",
         as_of="2026", quality="A", source="SSA 2026 COLA fact sheet", url="", note=""),
    dict(iso3="USA", topic="expat_tax", item="foreign_housing_limit_default", value=39870,
         unit="USD", as_of="2026", quality="A",
         source="IRS: 30% of the FEIE maximum (higher limits for listed high-cost cities)",
         url="https://www.irs.gov/individuals/international-taxpayers/foreign-housing-exclusion-or-deduction",
         note="Housing exclusion = housing cost - base amount, capped at limit - base."),
    # ---- Mandatory health premiums NOT in tax/SSC or HBS (COICOP treats them as social
    #      insurance) ------------------------------------------------------------------
    dict(iso3="CHE", topic="health", item="mandatory_health_premium_adult_monthly",
         value=465.30, unit="CHF/month", as_of="2026", quality="A",
         source="FOPH: average 2026 basic insurance premium, adults 26+ (all ages 393.30)",
         url="https://www.bag.admin.ch/en/premiums-and-costs-answers-to-frequently-asked-questions",
         note="Per adult, standard 300 CHF deductible; 2027 = 487.60. Children 122.50."),
    dict(iso3="CHE", topic="health", item="mandatory_health_premium_child_monthly",
         value=122.50, unit="CHF/month", as_of="2026", quality="A", source="FOPH 2026",
         url="", note=""),
    dict(iso3="LIE", topic="health", item="mandatory_health_premium_adult_monthly",
         value=465.30, unit="CHF/month", as_of="2026", quality="C",
         source="ASSUMPTION: Swiss FOPH average used for Liechtenstein", url="",
         note="Liechtenstein premiums are set separately; override if known."),
    dict(iso3="NLD", topic="health", item="mandatory_health_premium_adult_monthly",
         value=159.30, unit="EUR/month", as_of="2026", quality="B",
         source="Zorgwijzer: average 2026 Dutch basic health insurance premium",
         url="https://www.iamexpat.nl/expat-info/dutch-news/dutch-health-insurance-premiums-2026-announced-price-differences-grow",
         note="Per adult 18+; children free. Plus 385 EUR/yr mandatory deductible."),
    # ---- UK council tax (not in Family Spending COICOP 1-12) --------------------------
    dict(iso3="GBR", topic="housing", item="council_tax_annual", value=2392, unit="GBP/year",
         as_of="2026-27", quality="A",
         source="MHCLG: average Band D council tax, England 2026-27",
         url="https://www.gov.uk/government/statistics/council-tax-levels-set-by-local-authorities-in-england-2026-to-2027/council-tax-levels-set-by-local-authorities-in-england-2026-to-2027",
         note="Band D incl. adult social care and parish precepts; paid by renters and owners."),
    # ---- Rent / mortgage fallbacks -------------------------------------------------------
    dict(iso3="USA", topic="rent", item="median_gross_rent", value=1557, unit="USD/month",
         as_of="2025", quality="A", source="Census ACS 2025 1-year, table B25064",
         url="https://data.census.gov/table/ACSDT1Y2025.B25064",
         note="Fallback when CENSUS_API_KEY is not set. Contract rent + utilities."),
    dict(iso3="ISL", topic="house", item="mortgage_rate", value=9.25, unit="%",
         as_of="2026-07", quality="C",
         source="ESTIMATE: Central Bank of Iceland key rate 7.75% + ~1.5pp typical spread "
                "on non-indexed variable mortgages",
         url="https://www.landsbankinn.is/en/news/2026/05/21/landsbankinn-changes-interest-rates-may-2026",
         note="Many Icelandic mortgages are CPI-indexed (lower nominal rate + indexed "
              "principal). Override with a bank quote."),
] + [
    dict(iso3=c, topic="expat_tax", item="us_totalization_agreement", value=1, unit="flag",
         as_of="2026-09", quality="A", source=f"SSA totalization agreements (in force since {d})",
         url="https://secure.ssa.gov/poms.Nsf/lnx/0201701005",
         note="Avoids paying social security in both countries.")
    for c, d in [("AUT", "1991"), ("BEL", "1984"), ("CAN", "1984"), ("CZE", "2009"),
                 ("DNK", "2008"), ("FIN", "1992"), ("FRA", "1988"), ("DEU", "1979"),
                 ("GRC", "1994"), ("HUN", "2016"), ("ISL", "2019"), ("IRL", "1993"),
                 ("ITA", "1978"), ("LUX", "1993"), ("NLD", "1990"), ("NOR", "1984"),
                 ("POL", "2009"), ("PRT", "1989"), ("ROU", "2026"), ("SVK", "2014"),
                 ("SVN", "2019"), ("ESP", "1988"), ("SWE", "1987"), ("CHE", "1980"),
                 ("GBR", "1985")]
]


# ==========================================================================
# HTTP + cache
# ==========================================================================
class FetchError(RuntimeError):
    pass


@dataclass
class CachedFetcher:
    cache_dir: Path
    ttl_days: float = 30.0
    refresh: bool = False
    min_interval: dict[str, float] = field(default_factory=lambda: {
        "sdmx.oecd.org": 4.0,          # OECD rate-limits anonymous use hard
        "fred.stlouisfed.org": 1.0,
        "api.stlouisfed.org": 0.6,     # FRED API: 120 requests/minute per key
        "www150.statcan.gc.ca": 1.0,
        "www.ons.gov.uk": 2.5,         # ONS returns HTTP 429 below ~2s spacing
        "api.census.gov": 1.0,
        "www.bankofengland.co.uk": 2.0,
        "data-api.ecb.europa.eu": 1.0,
    })
    default_interval: float = 0.5
    log_rows: list[dict] = field(default_factory=list)
    _last_hit: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    @staticmethod
    def full_url(url: str, params: Iterable[tuple[str, str]] | dict | None) -> str:
        if not params:
            return url
        items = list(params.items()) if isinstance(params, dict) else list(params)
        return url + ("&" if "?" in url else "?") + urlencode(items, safe="+,:")

    def _paths(self, full: str) -> tuple[Path, Path]:
        h = hashlib.sha256(full.encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.bin", self.cache_dir / f"{h}.json"

    def _throttle(self, host: str) -> None:
        gap = self.min_interval.get(host, self.default_interval)
        wait = self._last_hit.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # Retry policy. Timeouts are the expensive failure (each costs `timeout` seconds),
    # so they get at most one retry; 429/5xx get a few backed-off retries; a host that
    # keeps failing is short-circuited for the rest of the run.
    max_attempts: int = 3
    max_timeout_retries: int = 1
    host_fail_limit: int = 3
    _host_fails: dict[str, int] = field(default_factory=dict)

    def get(self, url: str, params=None, *, label: str = "", timeout: float = 60,
            headers: dict | None = None, validate: Callable[[bytes], bool] | None = None,
            method: str = "GET", json_body: dict | None = None) -> bytes:
        """Fetch with cache. `validate(body)` must return True for the body to be
        cached; otherwise FetchError (so an HTML error page is never cached as data)."""
        full = self.full_url(url, params)
        key = full if json_body is None else full + "#" + json.dumps(json_body, sort_keys=True)
        body_p, meta_p = self._paths(key)
        if body_p.exists() and meta_p.exists() and not self.refresh:
            meta = json.loads(meta_p.read_text())
            age_days = (time.time() - meta["fetched_ts"]) / 86400
            body = body_p.read_bytes()
            if age_days < self.ttl_days and (validate is None or validate(body)):
                self._log(label, full, "cache", age_days=round(age_days, 2), elapsed_s=0.0)
                return body

        host = urlparse(full).netloc
        if self._host_fails.get(host, 0) >= self.host_fail_limit:
            self._log(label, full, "skipped")
            return self._fallback(body_p, label, full,
                                  FetchError(f"{host} skipped: failed {self.host_fail_limit}x"))
        last_err: Exception | None = None
        timeouts = 0
        for attempt in range(self.max_attempts):
            self._throttle(host)
            t0 = time.monotonic()
            try:
                r = self.session.request(method, full, timeout=(15, timeout), headers=headers,
                                         json=json_body)
                el = round(time.monotonic() - t0, 2)
                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() \
                        else 5 * 2 ** attempt
                    log.warning("%s -> HTTP %s, retrying in %.0fs", host, r.status_code, delay)
                    self._log(label, full, f"http-{r.status_code}", elapsed_s=el)
                    time.sleep(min(delay, 60))
                    last_err = FetchError(f"HTTP {r.status_code}")
                    continue
                if r.status_code >= 400:
                    self._log(label, full, f"http-{r.status_code}", elapsed_s=el)
                    raise FetchError(f"HTTP {r.status_code}: {r.text[:300]}")
                if validate is not None and not validate(r.content):
                    self._log(label, full, "invalid-body", elapsed_s=el, bytes=len(r.content))
                    raise FetchError(f"unexpected response body: {r.content[:160]!r}")
                body_p.write_bytes(r.content)
                meta_p.write_text(json.dumps({
                    "url": redact(full), "fetched_ts": time.time(),
                    "fetched_utc": datetime.now(timezone.utc).isoformat(),
                    "bytes": len(r.content), "label": label,
                }))
                self._host_fails[host] = 0
                self._log(label, full, "network", bytes=len(r.content), elapsed_s=el)
                return r.content
            except FetchError:
                raise
            except requests.Timeout as e:
                last_err = e
                timeouts += 1
                self._log(label, full, "timeout", elapsed_s=round(time.monotonic() - t0, 2))
                if timeouts > self.max_timeout_retries:
                    break
            except requests.RequestException as e:
                last_err = e
                self._log(label, full, "conn-error", elapsed_s=round(time.monotonic() - t0, 2))
                time.sleep(3 * 2 ** attempt)
        self._host_fails[host] = self._host_fails.get(host, 0) + 1
        return self._fallback(body_p, label, full, last_err)

    def _fallback(self, body_p: Path, label: str, full: str, err) -> bytes:
        if body_p.exists():  # stale cache beats nothing
            log.warning("Using STALE cache for %s (%s)", label or full, err)
            self._log(label, full, "stale-cache", error=str(err))
            return body_p.read_bytes()
        raise FetchError(f"{redact(full)}: {err}")

    def _log(self, label: str, url: str, how: str, **kw) -> None:
        self.log_rows.append({"label": label, "url": redact(url), "how": how,
                              "time_utc": datetime.now(timezone.utc).isoformat(), **kw})


def redact(url: str) -> str:
    """Hide API keys in URLs that end up in logs, fetch_log.csv and cache metadata."""
    return re.sub(r"([?&](?:api_key|key)=)[^&]+", r"\1***", url)


def is_json(b: bytes) -> bool:
    return b.lstrip()[:1] in (b"{", b"[")


def is_zip(b: bytes) -> bool:
    return b[:2] == b"PK"


# ==========================================================================
# Generic source clients
# ==========================================================================
def parse_jsonstat(raw: bytes) -> pd.DataFrame:
    """Eurostat JSON-stat 2.0 -> long DataFrame: one column per dimension, plus
    '<dim>_label' columns, 'value' and 'flag'."""
    js = json.loads(raw)
    if "error" in js:
        raise FetchError(f"Eurostat error: {js['error']}")
    ids: list[str] = js["id"]
    sizes: list[int] = js["size"]
    cats, labels = [], []
    for d in ids:
        cat = js["dimension"][d]["category"]
        idx = cat.get("index")
        if idx is None:
            codes = list(cat.get("label", {}).keys())
        elif isinstance(idx, list):
            codes = idx
        else:
            codes = [c for c, _ in sorted(idx.items(), key=lambda kv: kv[1])]
        cats.append(codes)
        labels.append(cat.get("label", {}))
    values = js.get("value", {})
    items = values.items() if isinstance(values, dict) else \
        ((i, v) for i, v in enumerate(values) if v is not None)
    status = js.get("status", {}) if isinstance(js.get("status"), dict) else {}
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    rows = []
    for k, v in items:
        k = int(k)
        rec, rem = {}, k
        for d, s, codes, lab in zip(ids, strides, cats, labels):
            pos, rem = divmod(rem, s)
            rec[d] = codes[pos]
            rec[d + "_label"] = lab.get(codes[pos], codes[pos])
        rec["value"] = v
        rec["flag"] = status.get(str(k), "")
        rows.append(rec)
    cols = [c for d in ids for c in (d, d + "_label")] + ["value", "flag"]
    return pd.DataFrame(rows, columns=cols)


EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"


def eurostat(f: CachedFetcher, dataset: str, required: dict[str, list[str]] | None = None,
             optional: dict[str, list[str]] | None = None, since: int | None = None,
             geo: list[str] | None = None) -> pd.DataFrame:
    """Query Eurostat. `optional` filters are dropped (and applied locally) if the
    server rejects them, e.g. because a code doesn't exist in that dataset."""
    required = dict(required or {})
    optional = dict(optional or {})

    def build(filters: dict[str, list[str]], geos):
        p: list[tuple[str, str]] = [("format", "JSON"), ("lang", "EN")]
        for k, vals in filters.items():
            p += [(k, v) for v in vals]
        p += [("geo", g) for g in (geos or [])]
        if since:
            p.append(("sinceTimePeriod", str(since)))
        return p

    try:
        df = parse_jsonstat(f.get(EUROSTAT_API + dataset,
                                  build({**required, **optional}, geo),
                                  label=f"eurostat:{dataset}", validate=is_json))
    except FetchError as e:
        if "HTTP 400" not in str(e):
            raise
        # Ask the dataset for its real structure (one tiny request), then keep only the
        # filters that exist. The country filter is never dropped: dropping it used to
        # download every country in the world.
        dims = eurostat_dims(f, dataset)
        bad_req = {k: v for k, v in required.items()
                   if k not in dims or not set(v) & dims[k]}
        if bad_req:
            raise FetchError(f"Eurostat {dataset}: required filters {bad_req} not in "
                             f"structure {{{', '.join(f'{k}: {sorted(v)[:8]}' for k, v in dims.items())}}}")
        pruned = {k: [c for c in v if c in dims[k]] for k, v in optional.items() if k in dims}
        pruned = {k: v for k, v in pruned.items() if v}
        dropped = sorted(set(optional) - set(pruned))
        geos = [g for g in (geo or []) if g in dims.get("geo", set())] or None
        log.warning("Eurostat %s rejected the query; dimensions are %s. Dropping filters %s "
                    "and retrying (country filter kept)", dataset, list(dims), dropped)
        df = parse_jsonstat(f.get(EUROSTAT_API + dataset,
                                  build({**required, **pruned}, geos),
                                  label=f"eurostat:{dataset}:pruned", validate=is_json))
    if "geo" in df:
        df = df.assign(iso3=df["geo"].map(EUROSTAT_TO_ISO3)).dropna(subset=["iso3"])
    return df


def eurostat_dims(f: CachedFetcher, dataset: str) -> dict[str, set[str]]:
    """Dimension -> set of codes, from a 1-period, 1-country query."""
    js = json.loads(f.get(EUROSTAT_API + dataset,
                          [("format", "JSON"), ("lang", "EN"), ("lastTimePeriod", "1"),
                           ("geo", "DE")], label=f"eurostat:{dataset}:structure",
                          validate=is_json))
    dims = {}
    for d in js["id"]:
        cat = js["dimension"][d]["category"]
        dims[d] = set(cat.get("label", {}).keys()) | set(
            cat["index"] if isinstance(cat.get("index"), list) else cat.get("index", {}))
    dims["geo"] = set(EU_GEOS)  # the probe only had DE; geo codes are standard
    return dims


def dimcol(df: pd.DataFrame, *names: str) -> str:
    """First of `names` that is a column (Eurostat renames dimensions, e.g.
    indic_il -> statinfo, hhtyp -> hhcomp, quantile -> quant_inc)."""
    for n in names:
        if n in df.columns:
            return n
    raise FetchError(f"none of {names} in columns {[c for c in df.columns if not c.endswith('_label')]}")


EU_GEOS = [COUNTRIES[c][1] for c in EUROPE]

OECD_API = "https://sdmx.oecd.org/public/rest/data/"


def sdmx_key(dims: list[str], filters: dict[str, list[str]]) -> str:
    return ".".join("+".join(filters.get(d, [])) for d in dims)


def oecd(f: CachedFetcher, flow: str, dims: list[str], filters: dict[str, list[str]],
         since: int | None, version: str = "latest") -> pd.DataFrame:
    """OECD SDMX query. On rejection, retries with only REF_AREA filtered and
    applies the other filters locally."""
    def run(flt):
        ref = flow if version == "latest" else f"{flow},{version}"
        key = sdmx_key(dims, flt)
        url = f"{OECD_API}{ref}/{key if key.strip('.') else 'all'}"
        params = {"format": "csvfile", "dimensionAtObservation": "AllDimensions"}
        if since:
            params["startPeriod"] = str(since)
        raw = f.get(url, params, label=f"oecd:{flow}")
        if not raw.strip() or raw.lstrip().startswith(b"<"):
            raise FetchError(f"OECD {flow}: non-CSV response")
        return pd.read_csv(io.BytesIO(raw), dtype=str)

    try:
        return run(filters)
    except FetchError as e:
        minimal = {k: v for k, v in filters.items() if k == "REF_AREA"}
        if minimal == filters:
            raise
        log.warning("OECD %s rejected key (%s); retrying with REF_AREA only", flow, str(e)[:120])
        try:
            df = run(minimal)
        except FetchError:
            log.warning("OECD %s: retrying with no key at all (larger download)", flow)
            df = run({})
        for k, vals in filters.items():
            if k in df.columns and df[k].isin(vals).any():
                df = df[df[k].isin(vals)]
        return df


FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
# fred.stlouisfed.org's edge can hold a request open until the read times out depending on
# the User-Agent: from one home network, this pipeline's UA and "Mozilla/5.0" stalled every
# time while requests' default UA (and curl) got a 200 in <1 s. So the CSV endpoint gets the
# default UA, and a short read timeout so a stall costs seconds, not the whole FRED budget.
FRED_CSV_HEADERS = {"User-Agent": requests.utils.default_user_agent()}
FRED_CSV_TIMEOUT = 20


def fred(f: CachedFetcher, series_id: str) -> pd.DataFrame:
    """One FRED series as date/value. Uses the official API when FRED_API_KEY is set (falls
    back to the CSV endpoint if the API fails), otherwise the fredgraph.csv endpoint."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    api_host = urlparse(FRED_API).netloc
    if key and f._host_fails.get(api_host, 0) < f.host_fail_limit:
        try:
            raw = f.get(FRED_API, {"series_id": series_id, "api_key": key, "file_type": "json"},
                        label=f"fred-api:{series_id}", validate=is_json, timeout=30)
            return fred_api_frame(raw, series_id)
        except FetchError as e:
            if "api_key" in str(e):  # bad key: say so once, then use the CSV endpoint
                log.error("FRED_API_KEY was rejected (%s); using the CSV endpoint", str(e)[:200])
                f._host_fails[api_host] = f.host_fail_limit
            else:
                log.warning("FRED API %s failed (%s); trying the CSV endpoint", series_id, e)
    raw = f.get(FRED_CSV, {"id": series_id}, label=f"fred:{series_id}",
                headers=FRED_CSV_HEADERS, timeout=FRED_CSV_TIMEOUT)
    return fred_csv_frame(raw, series_id)


def fred_api_frame(raw: bytes, series_id: str) -> pd.DataFrame:
    obs = json.loads(raw).get("observations")
    if obs is None:
        raise FetchError(f"FRED API {series_id}: no observations in the response")
    df = pd.DataFrame(obs, columns=["date", "value"]) if obs else \
        pd.DataFrame(columns=["date", "value"])
    df = df[["date", "value"]].astype({"date": str})
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED uses "." for NA
    return df.dropna()


def fred_csv_frame(raw: bytes, series_id: str) -> pd.DataFrame:
    head = raw[:40].lstrip(b"\xef\xbb\xbf").lower()
    if not (head.startswith(b"observation_date") or head.startswith(b"date")):
        raise FetchError(f"FRED {series_id}: not a CSV (series may not exist)")
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED uses "." for NA
    return df.dropna()


WB_API = "https://api.worldbank.org/v2/country/{c}/indicator/{i}"


def worldbank(f: CachedFetcher, indicator: str, iso3: list[str], since: int | None,
              sources: tuple[str | None, ...] = (None,)) -> pd.DataFrame:
    last = None
    for src in sources:
        params = {"format": "json", "per_page": "20000"}
        if since:
            params["date"] = f"{since}:{datetime.now().year}"
        if src:
            params["source"] = src
        url = WB_API.format(c=";".join(iso3), i=indicator)
        try:
            js = json.loads(f.get(url, params, label=f"worldbank:{indicator}:{src}",
                                  validate=is_json))
        except FetchError as e:
            last = e
            continue
        if not isinstance(js, list) or len(js) < 2 or js[1] is None:
            last = FetchError(f"World Bank {indicator} source={src}: {str(js)[:200]}")
            continue
        rows = [{"iso3": r["countryiso3code"], "date": r["date"], "value": r["value"]}
                for r in js[1] if r.get("value") is not None]
        if rows:
            return pd.DataFrame(rows)
        last = FetchError(f"World Bank {indicator}: no data")
    raise FetchError(str(last))


STATCAN_ZIP = "https://www150.statcan.gc.ca/n1/tbl/csv/{pid}-eng.zip"
STATCAN_STD = {"REF_DATE", "GEO", "DGUID", "UOM", "UOM_ID", "SCALAR_FACTOR", "SCALAR_ID",
               "VECTOR", "COORDINATE", "VALUE", "STATUS", "SYMBOL", "TERMINATED", "DECIMALS"}


def statcan(f: CachedFetcher, pid: str, geo: str | list[str] | None = "Canada",
            match: dict[str, str | list[str] | None] | None = None,
            since: int | None = None) -> pd.DataFrame:
    """Download a full StatCan table and filter it.

    `match` maps a keyword found in a dimension column's name (case-insensitive)
    to a substring (or list of substrings) its value must contain; None keeps all."""
    raw = f.get(STATCAN_ZIP.format(pid=pid), label=f"statcan:{pid}", timeout=900,
                validate=is_zip)
    match = match or {}
    geos = [geo] if isinstance(geo, str) else geo
    # Parsing a multi-million-row CSV costs 1-2 minutes even from cache: keep the
    # filtered result, keyed by the zip's content hash and the filters.
    key = hashlib.sha256(raw[:1 << 20] + str(len(raw)).encode() +
                         json.dumps([geos, match, since], sort_keys=True, default=str).encode()
                         ).hexdigest()[:24]
    filtered = f.cache_dir / f"statcan_{pid}_{key}.pkl"
    if filtered.exists() and not f.refresh:
        return pd.read_pickle(filtered)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist()
                    if n.lower().endswith(".csv") and "metadata" not in n.lower())
        out = []
        with z.open(name) as fh:
            for chunk in pd.read_csv(fh, dtype=str, chunksize=200_000, encoding="utf-8-sig"):
                if geos is not None:
                    chunk = chunk[chunk["GEO"].isin(geos)]
                if since:
                    chunk = chunk[pd.to_numeric(chunk["REF_DATE"].str[:4],
                                                errors="coerce") >= since]
                dims = [c for c in chunk.columns if c not in STATCAN_STD]
                for kw, want in match.items():
                    cols = [c for c in dims if kw.lower() in c.lower()]
                    if not cols:
                        raise FetchError(f"StatCan {pid}: no column matching '{kw}' in {dims}")
                    if want is None:
                        continue
                    wants = [want] if isinstance(want, str) else want
                    s = chunk[cols[0]].str.lower()
                    keep = pd.Series(False, index=chunk.index)
                    for w in wants:
                        keep |= s.str.contains(w.lower(), regex=False, na=False)
                    chunk = chunk[keep]
                out.append(chunk)
    df = pd.concat(out, ignore_index=True)
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df.to_pickle(filtered)
    return df


def statcan_dims(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in STATCAN_STD]


def col_like(df: pd.DataFrame, *keywords: str) -> str | None:
    for kw in keywords:
        for c in df.columns:
            if kw.lower() in c.lower():
                return c
    return None


def pick_codes(df: pd.DataFrame, col: str, patterns: list[str], label_col: str | None = None,
               exclude: str | None = None) -> list[str]:
    """Return the codes of `col` whose label (or code) matches the FIRST pattern in
    `patterns` that matches anything. Lets us select e.g. 'Two adults younger than 65'
    without knowing the exact code."""
    label_col = label_col or (col + "_label" if col + "_label" in df else col)
    pairs = df[[col, label_col]].drop_duplicates()
    for pat in patterns:
        rx = re.compile(pat, re.I)
        hit = pairs[pairs[label_col].astype(str).map(lambda s: bool(rx.search(s))) |
                    pairs[col].astype(str).map(lambda s: bool(rx.fullmatch(s)))]
        if exclude:
            ex = re.compile(exclude, re.I)
            hit = hit[~hit[label_col].astype(str).map(lambda s: bool(ex.search(s)))]
        if not hit.empty:
            return hit[col].tolist()
    return []


# ==========================================================================
# Tidy rows
# ==========================================================================
TIDY_COLS = ["iso3", "country", "region", "measure", "statistic", "age_band",
             "career_stage", "household_type", "area_type", "category", "period", "year",
             "frequency", "value", "unit", "estimated", "method", "source", "series",
             "comparability_note"]

AGE_RX = re.compile(r"(\d{2})\s*(?:-|to|T)\s*(\d{2})")
AGE_PLUS = re.compile(r"(\d{2})\s*(?:\+|years and over|years or over|and over)|_GE(\d{2})")
AGE_LT = re.compile(r"(?:less than|under|_LT)\s*(\d{2})", re.I)


def career_stage(age_band) -> str:
    """Map an age band to early (<30) / mid (30-49) / late (50+) by its midpoint.
    Broad bands map to 'all'. Age is only a proxy for career stage."""
    a = str(age_band or "").strip()
    if a.upper() in ("TOTAL", "ALL", "", "_T", "NAN"):
        return "all"
    if m := AGE_LT.search(a):
        return "early" if int(m.group(1)) <= 30 else "all"
    if m := AGE_RX.search(a):
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi - lo > 35:
            return "all"
        mid = (lo + hi) / 2
    elif m := AGE_PLUS.search(a):
        lo = int(next(g for g in m.groups() if g))
        if lo <= 25:
            return "all"
        mid = lo + 5
    else:
        return "unknown"
    return "early" if mid < 30 else ("mid" if mid < 50 else "late")


def frame(rows: pd.DataFrame, **const) -> pd.DataFrame:
    df = rows.copy()
    for k, v in const.items():
        df[k] = v
    df["country"] = df["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", c, ""))[2])
    df["region"] = df["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", "", "?"))[3])
    if "age_band" not in df:
        df["age_band"] = "total"
    if "career_stage" not in df:
        df["career_stage"] = df["age_band"].map(career_stage)
    if "estimated" not in df:
        df["estimated"] = False
    df["period"] = df["period"].astype(str)
    df["year"] = pd.to_numeric(df["period"].str[:4], errors="coerce").astype("Int64")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    for c in TIDY_COLS:
        if c not in df:
            df[c] = pd.NA
    return df[TIDY_COLS].dropna(subset=["value"])


def latest(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Keep the latest year per group."""
    if df.empty:
        return df
    return df.sort_values("year").groupby(by, dropna=False).tail(1)


# ==========================================================================
# INCOME 1: earnings by age (SES, ASHE, CPS, LFS)
# ==========================================================================
SES_STATS = {"MED_E_EUR": ("median", "EUR/month"), "D1_E_EUR": ("p10", "EUR/month"),
             "D9_E_EUR": ("p90", "EUR/month"), "MED_E_PPS": ("median", "PPS/month"),
             "D1_E_PPS": ("p10", "PPS/month"), "D9_E_PPS": ("p90", "PPS/month")}


def earnings_europe_ses(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "earn_ses_monthly",
                  required={"nace_r2": ["B-S_X_O"], "isco08": ["TOTAL"],
                            "worktime": ["FT"], "sex": ["T"]},
                  optional={"indic_se": list(SES_STATS)}, since=since)
    df = df[df["indic_se"].isin(SES_STATS)]
    out = pd.DataFrame({
        "iso3": df["iso3"], "period": df["time"], "value": df["value"], "age_band": df["age"],
        "statistic": df["indic_se"].map(lambda s: SES_STATS[s][0]),
        "unit": df["indic_se"].map(lambda s: SES_STATS[s][1]),
        "series": "earn_ses_monthly:" + df["indic_se"]})
    ses = frame(out, measure="earnings_by_age", frequency="4-yearly",
                source="Eurostat SES", comparability_note=NOTES["ses"])
    # Annual earnings INCLUDE bonuses and 13th/14th-month pay, which the October monthly
    # figures miss (annual / (monthly x 12) is 1.12 DE, 1.22 AT, 1.27 PT, 1.34 EL).
    # Kept as a separate measure and used as a per-country scaling factor.
    try:
        a = eurostat(f, "earn_ses_annual",
                     required={"nace_r2": ["B-S_X_O"], "isco08": ["TOTAL"], "worktime": ["FT"],
                               "sex": ["T"]},
                     optional={"indic_se": ["MED_E_EUR", "D1_E_EUR", "D9_E_EUR"]}, since=since)
        a = a[a["indic_se"].isin(["MED_E_EUR", "D1_E_EUR", "D9_E_EUR"])]
        ann = frame(pd.DataFrame({
            "iso3": a["iso3"], "period": a["time"], "value": a["value"], "age_band": a["age"],
            "statistic": a["indic_se"].map(lambda x: SES_STATS[x][0]), "unit": "EUR/year",
            "series": "earn_ses_annual:" + a["indic_se"]}),
            measure="earnings_annual_ses", frequency="4-yearly", source="Eurostat SES annual",
            comparability_note="Eurostat SES gross ANNUAL earnings incl. bonuses, full-time "
                               "employees in employment the whole year.")
        ses = pd.concat([ses, ann], ignore_index=True)
    except FetchError as e:
        log.warning("earn_ses_annual skipped: %s", e)
    return ses


# ASHE Table 6 = UK by age group (Table 3.4 is region x age and has NO UK-level age
# rows, which is why the old parser found nothing). Release URLs are discovered from
# the dataset's /data JSON; the list below is the fallback if discovery fails.
ONS = "https://www.ons.gov.uk"
ASHE6_DATASET = ("/employmentandlabourmarket/peopleinwork/earningsandworkinghours/datasets/"
                 "agegroupashetable6")
ASHE6_FALLBACK = ["2026provisional", "2025revised"]
ASHE_PCTS = {"10": "p10", "20": "p20", "25": "p25", "30": "p30", "40": "p40",
             "60": "p60", "70": "p70", "75": "p75", "80": "p80", "90": "p90"}


def ons_editions(f: CachedFetcher, dataset_uri: str) -> list[str]:
    """Edition URIs of an ONS dataset, newest first (e.g. .../2026provisional)."""
    try:
        js = json.loads(f.get(ONS + dataset_uri + "/data", label=f"ons:list:{dataset_uri[-30:]}",
                              validate=is_json))
        return [d["uri"] for d in js.get("datasets", [])]
    except (FetchError, ValueError, KeyError) as e:
        log.warning("ONS edition discovery failed for %s: %s", dataset_uri, e)
        return []


def ons_edition_files(f: CachedFetcher, edition_uri: str) -> list[str]:
    try:
        js = json.loads(f.get(ONS + edition_uri + "/data", label=f"ons:edition:{edition_uri[-30:]}",
                              validate=is_json))
        return [edition_uri + "/" + d["file"] for d in js.get("downloads", [])]
    except (FetchError, ValueError, KeyError):
        return []


def parse_ashe_xlsx(content: bytes, year: str) -> pd.DataFrame:
    """Full-Time sheet of ASHE Table 6.1a: rows 'All employees', '16-17b', '18-21',
    '22-29', ..., '60+'; header row has Median and percentile columns 10..90."""
    xl = pd.ExcelFile(io.BytesIO(content))
    sheet = next((s for s in xl.sheet_names if s.strip().lower() == "full-time"), None)
    if sheet is None:
        raise FetchError(f"ASHE: no Full-Time sheet in {xl.sheet_names}")
    grid = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str)
    hdr_row = next(i for i, r in grid.iterrows()
                   if any(str(v).strip().lower() == "median" for v in r.values))
    header = [re.sub(r"\.0$", "", str(v).strip()) for v in grid.iloc[hdr_row].values]
    rows = []
    for i in range(hdr_row + 1, len(grid)):
        desc = str(grid.iat[i, 0]).strip()
        if desc.lower().startswith("all employees"):
            band = "total"
        elif m := re.fullmatch(r"(\d{2})\s*-\s*(\d{2})[a-z]?", desc):
            band = f"{m.group(1)}-{m.group(2)}"
        elif m := re.fullmatch(r"(\d{2})\s*\+[a-z]?", desc):
            band = f"{m.group(1)}+"
        else:
            continue
        for j, col in enumerate(header):
            stat = "median" if col.lower() == "median" else ASHE_PCTS.get(col)
            if stat:
                rows.append({"iso3": "GBR", "period": year, "age_band": band, "statistic": stat,
                             "value": pd.to_numeric(grid.iat[i, j], errors="coerce")})
    if not rows:
        raise FetchError(f"ASHE: no age rows in sheet {sheet}")
    return pd.DataFrame(rows)


def earnings_uk_ashe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    editions = ons_editions(f, ASHE6_DATASET) or [ASHE6_DATASET + "/" + e for e in ASHE6_FALLBACK]
    parts, seen_years = [], set()
    # newest first; one edition per year; only the 4 newest (older zips use other file
    # names and crawling them all trips ONS rate limits: HTTP 429)
    for ed in editions[:4]:
        year = re.search(r"/(\d{4})[a-z]*$", ed)
        if not year or year.group(1) in seen_years or (since and int(year.group(1)) < since):
            continue
        year = year.group(1)
        files = [u for u in ons_edition_files(f, ed) if u.endswith(".zip")] or \
            [f"{ed}/ashetable6{ed.rsplit('/', 1)[1]}.zip"]
        try:
            raw = f.get(ONS + "/file?uri=" + files[0], label=f"ons:ashe6:{year}", validate=is_zip)
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                name = next(n for n in z.namelist()
                            if re.search(r"weekly pay\s*-\s*gross", n, re.I)
                            and n.lower().endswith(".xlsx") and " cv" not in n.lower())
                parts.append(parse_ashe_xlsx(z.read(name), year))
                seen_years.add(year)
        except (FetchError, StopIteration, zipfile.BadZipFile, ValueError) as e:
            log.warning("ASHE %s skipped: %s", ed, e)
    if not parts:
        raise FetchError("ASHE: nothing parsed")
    df = pd.concat(parts).assign(unit="GBP/week", series="ASHE Table 6.1a Full-Time")
    return frame(df, measure="earnings_by_age", frequency="A", source="ONS ASHE",
                 comparability_note=NOTES["ashe"])


CPS_SERIES = {  # titles verified on FRED
    "LEU0252881500A": ("16 years and over", "median"),
    "LEU0252911300A": ("16 years and over", "p25"),
    "LEU0252911400A": ("16 years and over", "p75"),
    "LEU0252887100A": ("20 to 24 years", "median"),
    "LEU0252888500A": ("25 to 34 years", "median"),
    "LEU0252889100A": ("35 to 44 years", "median"),
    "LEU0252889700A": ("45 to 54 years", "median"),
    "LEU0252890900A": ("55 to 64 years", "median"),
    "LEU0252891500A": ("65 years and over", "median"),
    "LEU0252887900A": ("25 to 54 years", "median"),
}


def earnings_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, (band, stat) in CPS_SERIES.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        parts.append(d.assign(iso3="USA", period=d["date"].str[:4], age_band=band,
                              statistic=stat, series=sid))
    if not parts:
        raise FetchError("No CPS series fetched")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].astype(int) >= since]
    return frame(df, measure="earnings_by_age", frequency="A", unit="USD/week",
                 source="BLS CPS (via FRED)", comparability_note=NOTES["cps"])


def earnings_canada_lfs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = statcan(f, "14100417", "Canada", {
        "wage": "median weekly", "type of work": "full-time",
        "occupation": "total", "gender": "total", "age group": None})
    age_col = next(c for c in d.columns if c.lower().startswith("age"))
    out = pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"].str[:4], "value": d["VALUE"],
                        "age_band": d[age_col], "statistic": "median",
                        "series": "14-10-0417-01"})
    if since:
        out = out[out["period"].astype(int) >= since]
    return frame(out, measure="earnings_by_age", frequency="A", unit="CAD/week",
                 source="StatCan LFS", comparability_note=NOTES["lfs_ca"])


# ==========================================================================
# INCOME 2: household income (EU-SILC, OECD IDD)
# ==========================================================================
SILC_UNITS = ["EUR", "PPS", "NAC"]
# Eurostat restructured EU-SILC (2025): indic_il -> statinfo, MED_E -> MED_EI,
# MEI_E -> MEAN_EI, hhtyp -> hhcomp, quantile -> quant_inc, currency -> unit.
SILC_MEDIAN = {"MED_EI", "MED_E"}
SILC_STAT = {"MED_EI": "median", "MED_E": "median", "MEAN_EI": "mean", "MEI_E": "mean"}


def _silc_rows(df: pd.DataFrame, **extra):
    st = dimcol(df, "statinfo", "indic_il")
    unit_col = dimcol(df, "unit", "currency")
    out = pd.DataFrame({
        "iso3": df["iso3"], "period": df["time"], "value": df["value"],
        "statistic": df[st].map(SILC_STAT).fillna(df[st]),
        "unit": df[unit_col] + "/year (equivalised)", **extra})
    return out


def _silc_median(df: pd.DataFrame) -> pd.DataFrame:
    return df[df[dimcol(df, "statinfo", "indic_il")].isin(SILC_MEDIAN)]


def income_silc_by_age(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "ilc_di03", optional={"statinfo": ["MED_EI"], "sex": ["T"],
                                           "unit": SILC_UNITS}, since=since, geo=EU_GEOS)
    df = _silc_median(df)  # mean kept out on purpose (outliers)
    st = dimcol(df, "statinfo", "indic_il")
    out = _silc_rows(df, age_band=df["age"], series="ilc_di03:" + df[st])
    return frame(out, measure="household_income", household_type="all persons",
                 frequency="A", source="Eurostat EU-SILC", comparability_note=NOTES["silc"])


def income_silc_by_household(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "ilc_di04", optional={"statinfo": ["MED_EI"], "unit": SILC_UNITS},
                  since=since, geo=EU_GEOS)
    df = _silc_median(df)
    hh = dimcol(df, "hhcomp", "hhtyp")
    out = _silc_rows(df, household_type=df[hh + "_label"], series="ilc_di04:" + df[hh])
    return frame(out, measure="household_income", frequency="A",
                 source="Eurostat EU-SILC", comparability_note=NOTES["silc"])


def income_silc_by_urbanisation(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "ilc_di17", optional={"statinfo": ["MED_EI"], "sex": ["T"],
                                           "unit": SILC_UNITS}, since=since, geo=EU_GEOS)
    df = _silc_median(df)
    out = _silc_rows(df, area_type=df["deg_urb_label"],
                     age_band=df["age"] if "age" in df else "total",
                     series="ilc_di17:" + df["deg_urb"])
    return frame(out, measure="household_income", household_type="all persons",
                 frequency="A", source="Eurostat EU-SILC", comparability_note=NOTES["silc"])


def income_silc_distribution(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Percentile cut-offs (P1..P99, deciles, quartiles) of equivalised income:
    the closest thing to a raw distribution available for free."""
    df = eurostat(f, "ilc_di01", optional={"statinfo": ["TC"], "unit": SILC_UNITS},
                  since=since, geo=EU_GEOS)
    df = df[df[dimcol(df, "statinfo", "indic_il")] == "TC"]
    unit_col = dimcol(df, "unit", "currency")
    qcol = dimcol(df, "quant_inc", "quantile")

    def q2stat(q: str) -> str:
        q = str(q).upper()
        if m := re.fullmatch(r"P(\d{1,2})", q):
            return f"p{int(m.group(1))}"
        if m := re.fullmatch(r"D(\d)", q):
            return f"p{int(m.group(1)) * 10}"
        if m := re.fullmatch(r"Q(\d)", q):
            return f"p{int(m.group(1)) * 25}"
        if m := re.fullmatch(r"QU(\d)", q):
            return f"p{int(m.group(1)) * 20}"
        return q.lower()

    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": df[qcol].map(q2stat),
                        "unit": df[unit_col] + "/year (equivalised)",
                        "series": "ilc_di01:TC:" + df[qcol]})
    out = out.drop_duplicates(["iso3", "period", "statistic", "unit"])
    return frame(out, measure="household_income_distribution", household_type="all persons",
                 frequency="A", source="Eurostat EU-SILC", comparability_note=NOTES["silc"])


IDD_DIMS = ["REF_AREA", "FREQ", "MEASURE", "STATISTICAL_OPERATION", "UNIT_MEASURE", "AGE",
            "METHODOLOGY", "DEFINITION", "POVERTY_LINE"]


def income_oecd_idd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(f, "OECD.WISE.INE,DSD_WISE_IDD@DF_IDD", IDD_DIMS,
              {"REF_AREA": areas, "FREQ": ["A"],
               "MEASURE": ["INC_DISP", "D9_1_INC_DISP", "D5_1_INC_DISP"]}, since)
    if "METHODOLOGY" in df and (df["METHODOLOGY"] == "METH2012").any():
        df = df[df["METHODOLOGY"] == "METH2012"]
    if "DEFINITION" in df and (df["DEFINITION"] == "D_CUR").any():
        df = df[df["DEFINITION"] == "D_CUR"]
    stat = df.get("STATISTICAL_OPERATION", pd.Series("", index=df.index)).fillna("")
    meas = df["MEASURE"]
    statistic = meas.where(meas != "INC_DISP",
                           "INC_DISP_" + stat).str.lower()
    statistic = statistic.replace({"inc_disp_median": "median", "inc_disp_mean": "mean",
                                   "inc_disp__z": "mean", "d9_1_inc_disp": "p90_p10_ratio",
                                   "d5_1_inc_disp": "p50_p10_ratio"})
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": statistic,
                        "age_band": df.get("AGE", "total"),
                        "unit": df.get("UNIT_MEASURE", "") + "/year (equivalised)",
                        "series": "IDD:" + meas})
    out = out[~out["statistic"].isin(["mean"])]  # medians & ratios only
    return frame(out, measure="household_income", household_type="all persons",
                 frequency="A", source="OECD IDD", comparability_note=NOTES["idd"])


# ==========================================================================
# INCOME 3: tax models (net income of a model couple)
# ==========================================================================
TW_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "HOUSEHOLD_TYPE", "INCOME_PRINCIPAL",
           "INCOME_SPOUSE", "FREQ"]


def net_income_taxing_wages(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(f, "OECD.CTP.TPS,DSD_TAX_WAGES_COMP@DF_TW_COMP", TW_DIMS,
              {"REF_AREA": areas,
               "MEASURE": ["AV_ITR", "AV_R_EMPEE_SSC", "AV_RITEESSC", "NPMTR_PE", "GEBT",
                           "NIAT", "AV_TW", "NPATR"],
               "UNIT_MEASURE": ["XDC", "PT_WG_EARN_G", "PT_COS_LB"],
               "HOUSEHOLD_TYPE": ["S_C0", "C_C0", "C_C2"],
               "INCOME_PRINCIPAL": ["AW67", "AW100", "AW167"], "FREQ": ["A"]}, since)
    hh = (df["HOUSEHOLD_TYPE"].fillna("") + "|" + df["INCOME_PRINCIPAL"].fillna("") + "+" +
          df["INCOME_SPOUSE"].fillna(""))
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": df["MEASURE"],
                        "household_type": hh, "unit": df["UNIT_MEASURE"],
                        "series": "TW_COMP:" + df["MEASURE"]})
    return frame(out, measure="net_income_tax_model", frequency="A",
                 source="OECD Taxing Wages", comparability_note=NOTES["taxing_wages"])


def net_income_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "earn_nt_net", optional={
        "currency": ["EUR", "NAC"], "estruct": ["GRS", "TAX", "SOC", "FAM", "NET"]},
        since=since, geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": df["estruct"], "household_type": df["ecase_label"],
                        "unit": df["currency"] + "/year", "series": "earn_nt_net:" + df["ecase"]})
    return frame(out, measure="net_income_tax_model", frequency="A",
                 source="Eurostat net earnings", comparability_note=NOTES["earn_nt"])


# ==========================================================================
# INCOME 4: wage history (means; for stagnation/trend analysis)
# ==========================================================================
AVW_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "PAY_PERIOD", "PRICE_BASE",
            "AGGREGATION_OPERATION", "SEX"]


def avg_wage_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c in OECD_MEMBERS]
    df = oecd(f, "OECD.ELS.SAE,DSD_EARNINGS@AV_AN_WAGE", AVW_DIMS, {"REF_AREA": areas}, since)
    if "SEX" in df and (df["SEX"] == "_T").any():
        df = df[df["SEX"].isin(["_T", "_Z"])]
    unit = df["UNIT_MEASURE"].fillna("") + "|" + df.get("PRICE_BASE", "").fillna("")
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": "mean", "unit": unit,
                        "series": "AV_AN_WAGE"})
    return frame(out, measure="avg_wage", frequency="A", source="OECD average wages",
                 comparability_note=NOTES["avg_wage"])


def avg_salary_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "nama_10_fte", since=since, geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": "mean", "unit": df["unit"] + "/year",
                        "series": "nama_10_fte"})
    return frame(out, measure="avg_wage", frequency="A", source="Eurostat national accounts",
                 comparability_note=NOTES["nama_fte"])


def cpi_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = worldbank(f, "FP.CPI.TOTL", list(COUNTRIES), since)
    d = d.rename(columns={"date": "period"}).assign(statistic="cpi_index", unit="2010=100",
                                                    series="FP.CPI.TOTL")
    out = frame(d, measure="price_level", frequency="A", source="World Bank WDI",
                comparability_note="Consumer price index, national definitions.")
    try:
        out = pd.concat([out, cpi_us_gap_fill(f, out)], ignore_index=True)
    except FetchError as e:
        log.warning("US CPI gap fill skipped: %s", e)
    return out


def cpi_us_gap_fill(f: CachedFetcher, wb: pd.DataFrame) -> pd.DataFrame:
    """The World Bank (IMF) has no 2025 CPI for the US: BLS never collected October 2025
    prices (federal shutdown), so there is no official 12-month average. Fill complete
    past years from the monthly CPI-U (FRED CPIAUCNS, average of the months published),
    chained to the World Bank index at the last year both have."""
    u = wb[(wb["iso3"] == "USA")].set_index("year")["value"]
    if u.empty:
        return wb.iloc[0:0]
    m = fred(f, "CPIAUCNS")
    m["year"] = m["date"].str[:4].astype(int)
    a = m.groupby("year")["value"].agg(["mean", "size"])
    base = int(max(y for y in u.index if y in a.index and a.loc[y, "size"] == 12))
    k = u.loc[base] / a.loc[base, "mean"]
    rows = [{"iso3": "USA", "period": str(y), "value": a.loc[y, "mean"] * k,
             "statistic": "cpi_index", "unit": "2010=100", "series": "CPIAUCNS",
             "estimated": a.loc[y, "size"] < 12,
             "method": f"BLS CPI-U, mean of {a.loc[y, 'size']} months, chained to WDI {base}"}
            for y in a.index if y > base and y < datetime.now().year and y not in u.index]
    return frame(pd.DataFrame(rows), measure="price_level", frequency="A",
                 source="World Bank WDI (gap filled from BLS CPI-U)",
                 comparability_note="Consumer price index, national definitions.") \
        if rows else wb.iloc[0:0]


# ==========================================================================
# MOBILITY / RAISES
# ==========================================================================
WGT_SERIES = {  # verified on FRED (Atlanta Fed Wage Growth Tracker)
    "FRBATLWGT12MMUMHGO": "overall",
    "FRBATLWGT12MMUMHWGJST": "job stayer",
    "FRBATLWGT12MMUMHWGJSW": "job switcher",
    "FRBATLWGT12MMUMHWGA1644Y": "age 16-24",
    "FRBATLWGT12MMUMHWGA2554Y": "age 25-54",
    "FRBATLWGT12MMUMHWG55O": "age 55+",
    "FRBATLWGT12MMUMHWGMSA": "metro (MSA)",
    "FRBATLWGT12MMUMHWGNMSA": "non-metro",
}


def wage_growth_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, group in WGT_SERIES.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("WGT %s skipped: %s", sid, e)
            continue
        band = group.replace("age ", "") if group.startswith("age") else "total"
        parts.append(d.assign(iso3="USA", period=d["date"].str[:7], statistic=group,
                              age_band=band, series=sid,
                              area_type=group if "metro" in group or "MSA" in group else pd.NA))
    if not parts:
        raise FetchError("No Wage Growth Tracker series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, measure="wage_growth_individual", frequency="M",
                 unit="% y/y (median, same person)", source="Atlanta Fed WGT (via FRED)",
                 comparability_note=NOTES["wgt"])


def income_transitions_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "ilc_lvhl34", optional={"sex": ["T"]}, since=since, geo=EU_GEOS)
    trans_col = next((c for c in df.columns if not c.endswith("_label") and c not in
                      ("freq", "sex", "unit", "geo", "time", "value", "flag", "iso3")
                      and df[c].astype(str).str.contains(r"UP|DW|CHG|NONE", regex=True).any()),
                     None)
    q_col = next((c for c in df.columns if not c.endswith("_label") and c not in
                  ("freq", "sex", "unit", "geo", "time", "value", "flag", "iso3", trans_col)),
                 None)
    if trans_col is None:
        raise FetchError(f"ilc_lvhl34: transition dimension not found in {list(df.columns)}")
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": df[trans_col + "_label"],
                        "category": df[q_col + "_label"] if q_col else "total",
                        "unit": "% of employees", "series": "ilc_lvhl34:" + df[trans_col]})
    return frame(out, measure="income_transitions", frequency="A",
                 source="Eurostat EU-SILC longitudinal", comparability_note=NOTES["lvhl"])


def income_mobility_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # 6.4M raw rows (42 regions x 4 income concepts x 35 groups x 28 statistics);
    # keep Canada, employment income, decile/age/couple groups, mobility statistics.
    d = statcan(f, "11100061", "Canada", {
        "income concept": "employment income",
        "groups": ["total, all deciles", "decile", "years", "couple families"],
        "statistic": ["median income", "rising income", "falling income",
                      "same decile", "higher deciles", "lower deciles", "moved up by",
                      "moved down by"]}, since=since)
    gcol, scol = col_like(d, "groups"), col_like(d, "statistic")
    out = pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"].str[-4:], "value": d["VALUE"],
                        "statistic": d[scol], "category": d[gcol],
                        "age_band": d[gcol].where(d[gcol].str.contains("years"), "total"),
                        "unit": d.get("UOM", ""), "series": "11-10-0061-01"})
    return frame(out, measure="income_transitions", frequency="A", source="StatCan LAD",
                 comparability_note="StatCan one-year income mobility (tax-file based).")


# ==========================================================================
# JOB MARKET (as v1)
# ==========================================================================
def unemployment_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "une_rt_a", required={"unit": ["PC_ACT"], "sex": ["T"]},
                  optional={"age": ["Y15-24", "Y25-54", "Y25-74", "Y55-74", "Y15-74"]},
                  since=since, geo=EU_GEOS)
    df = df[df["iso3"] != "GBR"]  # UK from OECD (Eurostat stops 2019)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "age_band": df["age"], "series": "une_rt_a"})
    return frame(out, measure="unemployment_rate", statistic="rate", frequency="A",
                 unit="% of labour force", source="Eurostat LFS", comparability_note=NOTES["une_eu"])


UNE_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "TRANSFORMATION", "ADJUSTMENT", "SEX",
            "AGE", "ACTIVITY", "FREQ"]


def annual_from_monthly(m: pd.DataFrame, have: set, keys: list[str]) -> pd.DataFrame:
    """Calendar-year means of monthly rows (period 'YYYY-MM') for (keys, year) groups
    that have no published annual value: the current year so far, and years whose annual
    average was never published (the US skipped October 2025: federal shutdown)."""
    if m.empty:
        return m
    m = m.assign(year=m["period"].str[:4])
    g = m.groupby(keys + ["year"], as_index=False).agg(
        value=("value", "mean"), n=("value", "size"), first=("period", "min"),
        last=("period", "max"))
    g = g[[tuple(r) not in have for r in g[keys + ["year"]].itertuples(index=False)]]
    return g.assign(period=g["year"], estimated=g["n"] < 12,
                    method=[f"mean of {n} monthly values ({a} to {b}), seasonally adjusted"
                            for n, a, b in zip(g["n"], g["first"], g["last"])]) \
        .drop(columns=["year", "n", "first", "last"])


def unemployment_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(f, "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M", UNE_DIMS,
              {"REF_AREA": ["USA", "CAN", "GBR"], "MEASURE": ["UNE_LF_M"],
               "UNIT_MEASURE": ["PT_LF_SUB"], "TRANSFORMATION": ["_Z"], "SEX": ["_T"],
               "AGE": ["Y15T24", "Y_GE25", "Y_GE15"], "ACTIVITY": ["_Z"], "FREQ": ["A", "M"]},
              since, version="1.0")
    df["_rank"] = df["ADJUSTMENT"].map({"Y": 0, "N": 1}).fillna(2)
    df = df.sort_values("_rank").drop_duplicates(["REF_AREA", "AGE", "FREQ", "TIME_PERIOD"])
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    a = df[df["FREQ"] == "A"]
    out = pd.DataFrame({"iso3": a["REF_AREA"], "period": a["TIME_PERIOD"],
                        "value": a["OBS_VALUE"], "age_band": a["AGE"]})
    # monthly -> annual where the annual is missing (US 2025: no October survey) or the
    # year is not over yet; SA months only, so a part year has no seasonal bias
    m = df[(df["FREQ"] == "M") & (df["ADJUSTMENT"] == "Y")]
    m = pd.DataFrame({"iso3": m["REF_AREA"], "period": m["TIME_PERIOD"].astype(str),
                      "value": m["OBS_VALUE"], "age_band": m["AGE"]}).dropna()
    have = {(r.iso3, r.age_band, str(r.period)[:4]) for r in out.itertuples()}
    out = pd.concat([out, annual_from_monthly(m, have, ["iso3", "age_band"])],
                    ignore_index=True)
    return frame(out.assign(series="DF_IALFS_UNE_M"), measure="unemployment_rate",
                 statistic="rate", frequency="A", unit="% of labour force",
                 source="OECD IALFS", comparability_note=NOTES["une_oecd"])


def unemployment_europe_monthly(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Current-year (year-to-date) unemployment for Europe from the monthly series; the
    annual table (une_rt_a) only has complete years."""
    yr = datetime.now().year
    df = eurostat(f, "une_rt_m", required={"unit": ["PC_ACT"], "sex": ["T"], "s_adj": ["SA"]},
                  optional={"age": ["TOTAL", "Y_LT25"]}, since=yr - 1, geo=EU_GEOS)
    df = df[df["iso3"] != "GBR"]
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"].astype(str),
                        "value": pd.to_numeric(df["value"], errors="coerce"),
                        "age_band": df["age"].map({"TOTAL": "Y15-74", "Y_LT25": "Y15-24"})})
    out = out.dropna()
    out = annual_from_monthly(out[out["period"].str[:4] == str(yr)], set(),
                              ["iso3", "age_band"])
    return frame(out.assign(series="une_rt_m"), measure="unemployment_rate",
                 statistic="rate", frequency="A", unit="% of labour force",
                 source="Eurostat LFS (monthly)", comparability_note=NOTES["une_eu"])


# ---- unemployment, slack and underemployment by age and education ---------------------
# Education keys used everywhere downstream (ISCED 2011 attainment):
EDU_EUROSTAT = {"TOTAL": "all", "ED0-2": "below_upper_secondary", "ED3_4": "upper_secondary",
                "ED5-8": "tertiary"}
EDU_OECD = {"_T": "all", "ISCED11A_0T2": "below_upper_secondary",
            "ISCED11A_3_4": "upper_secondary", "ISCED11A_5": "short_tertiary",
            "ISCED11A_6": "bachelor", "ISCED11A_7_8": "master_plus", "ISCED11A_5T8": "tertiary"}


def _age(code: str) -> str:
    """'Y15-24' / 'Y25T34' / 'Y_GE15' -> '15-24' / '25-34' / '15+'."""
    s = str(code)
    if m := re.fullmatch(r"Y(\d+)[-T](\d+)", s):
        return f"{m.group(1)}-{m.group(2)}"
    if m := re.fullmatch(r"Y_GE(\d+)", s):
        return f"{m.group(1)}+"
    return s


def unemp_by_education_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "lfsa_urgaed", required={"unit": ["PC"], "sex": ["T"]},
                  optional={"age": ["Y15-24", "Y20-24", "Y25-29", "Y30-34", "Y25-64"],
                            "isced11": list(EDU_EUROSTAT)},
                  since=max(since or 2010, 2010), geo=EU_GEOS)
    df = df[df["isced11"].isin(EDU_EUROSTAT)]
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "age_band": df["age"].map(_age),
                        "category": df["isced11"].map(EDU_EUROSTAT), "series": "lfsa_urgaed"})
    return frame(out, measure="unemployment_by_education", statistic="rate", frequency="A",
                 unit="% of labour force", source="Eurostat LFS",
                 comparability_note="ILO unemployment rate by highest level of education "
                 "completed (ISCED 2011).")


SLACK_STATUS = ["UNE", "UEMP_PT", "SEEK_NAVL", "NSEEK_AVL", "SLACK"]


def slack_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Eurostat labour-market slack (the EU's U-6): unemployed + underemployed part-timers
    + people available but not seeking + seeking but not available, % of the extended
    labour force (labour force + the last two groups)."""
    parts = []
    a = eurostat(f, "lfsi_sla_a", required={"sex": ["T"], "unit": ["PC_ELF", "THS_PER"]},
                 optional={"age": ["Y15-24", "Y15-29", "Y25-54", "Y15-74"],
                           "wstatus": SLACK_STATUS}, since=max(since or 2010, 2010),
                 geo=EU_GEOS)
    parts.append(pd.DataFrame({"iso3": a["iso3"], "period": a["time"], "value": a["value"],
                               "age_band": a["age"].map(_age), "category": "all",
                               "statistic": a["wstatus"].str.lower(),
                               "unit": a["unit"].map({"PC_ELF": "% of extended labour force",
                                                      "THS_PER": "thousand persons"}),
                               "series": "lfsi_sla_a"}))
    e = eurostat(f, "lfsa_sup_edu", required={"sex": ["T"], "unit": ["PC_ELF", "THS_PER"]},
                 optional={"age": ["Y25-74"], "isced11": list(EDU_EUROSTAT),
                           "wstatus": SLACK_STATUS}, since=max(since or 2010, 2010),
                 geo=EU_GEOS)
    e = e[e["isced11"].isin(EDU_EUROSTAT)]
    parts.append(pd.DataFrame({"iso3": e["iso3"], "period": e["time"], "value": e["value"],
                               "age_band": e["age"].map(_age),
                               "category": e["isced11"].map(EDU_EUROSTAT),
                               "statistic": e["wstatus"].str.lower(),
                               "unit": e["unit"].map({"PC_ELF": "% of extended labour force",
                                                      "THS_PER": "thousand persons"}),
                               "series": "lfsa_sup_edu"}))
    return frame(pd.concat(parts, ignore_index=True), measure="labour_slack", frequency="A",
                 source="Eurostat LFS",
                 comparability_note="Eurostat labour-market slack: UNE unemployed, UEMP_PT "
                 "part-timers who want and are available for more hours, NSEEK_AVL available "
                 "but not seeking, SEEK_NAVL seeking but not available.")


def overqualification_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "lfsa_eoqgan", required={"sex": ["T"], "unit": ["PC"]},
                  optional={"citizen": ["TOTAL"], "age": ["Y20-34", "Y25-34", "Y20-64"]},
                  since=max(since or 2010, 2010), geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "age_band": df["age"].map(_age), "category": "tertiary",
                        "series": "lfsa_eoqgan"})
    return frame(out, measure="overqualification", statistic="rate", frequency="A",
                 unit="% of employed tertiary graduates", source="Eurostat LFS",
                 comparability_note="Over-qualification: tertiary graduates employed in "
                 "ISCO 4-9 jobs (clerical, service, sales, trades, operators, elementary), "
                 "which do not usually require a degree.")


EAG_DIMS = ["REF_AREA", "SEX", "AGE", "ATTAINMENT_LEV", "EDUCATION_FIELD", "MEASURE", "INCOME",
            "BIRTH_PLACE", "MIGRATION_AGE", "EDU_STATUS", "LABOUR_FORCE_STATUS",
            "DURATION_UNEMP", "UNIT_MEASURE", "STATISTICAL_OPERATION", "WORK_TIME_ARNGMNT",
            "QUESTIONNAIRE", "FREQ"]
EAG_FLOWS = {  # flow -> (measure, statistic, unit)
    "DF_LSO_NEAC_UNEMP": ("unemployment_by_education", "rate", "% of labour force"),
    "DF_LSO_NEAC_EMP": ("employment_by_education", "rate", "% of population"),
    "DF_LSO_NEAC_DISTR_EA": ("attainment_share", "share", "% of population"),
    "DF_LSO_EARN_REL_UPPER": ("relative_earnings", "index_upper_secondary_100",
                              "upper secondary = 100"),
}


def education_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Education at a Glance: unemployment, employment, attainment shares and relative
    full-time earnings by education level and age (all OECD countries + BG/HR/RO)."""
    parts = []
    for flow, (measure, stat, unit) in EAG_FLOWS.items():
        df = oecd(f, f"OECD.EDU.IMEP,DSD_EAG_LSO_EA@{flow}", EAG_DIMS,
                  {"REF_AREA": [c for c in COUNTRIES if c != "LIE"], "SEX": ["_T"],
                   "AGE": ["Y25T34", "Y35T44", "Y45T54", "Y55T64", "Y25T64"],
                   "ATTAINMENT_LEV": list(EDU_OECD)}, max(since or 2010, 2010))
        df = df[(df.get("STATISTICAL_OPERATION", "OBS") == "OBS") &
                df["ATTAINMENT_LEV"].isin(EDU_OECD) & (df["SEX"] == "_T")]
        if "WORK_TIME_ARNGMNT" in df and flow == "DF_LSO_EARN_REL_UPPER":
            df = df[df["WORK_TIME_ARNGMNT"] == "FT"]
        parts.append(frame(pd.DataFrame({
            "iso3": df["REF_AREA"], "period": df["TIME_PERIOD"], "value": df["OBS_VALUE"],
            "age_band": df["AGE"].map(_age), "category": df["ATTAINMENT_LEV"].map(EDU_OECD),
            "series": flow}), measure=measure, statistic=stat, unit=unit, frequency="A",
            source="OECD Education at a Glance",
            comparability_note="Labour-force survey data by highest attainment (ISCED 2011)."
            + (" Earnings: full-time full-year earners, upper secondary = 100."
               if measure == "relative_earnings" else "")))
    return pd.concat(parts, ignore_index=True)


INVPT_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "WORKER_STATUS", "FREQ"]


def involuntary_pt_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(f, "OECD.ELS.SAE,DSD_INVPT_EMP@DF_INVPT_I", INVPT_DIMS,
              {"REF_AREA": ["USA", "CAN", "GBR"], "UNIT_MEASURE": ["PT_LF"], "SEX": ["_T"],
               "AGE": ["Y15T24", "Y25T54"], "WORKER_STATUS": ["_T"]}, max(since or 2010, 2010))
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "age_band": df["AGE"].map(_age),
                        "category": "all", "series": "DF_INVPT_I"})
    return frame(out, measure="involuntary_part_time", statistic="rate", frequency="A",
                 unit="% of labour force", source="OECD Employment Database",
                 comparability_note="Part-timers (<30 usual hours) who could not find a "
                 "full-time job (OECD-harmonised definition).")


def slack_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """StatCan supplementary unemployment rates (monthly, unadjusted): R4 = official,
    R6 = + discouraged searchers + waiting group (people who want work and would take it
    but are not counted as unemployed)."""
    df = statcan(f, "14100077", match={"supplementary": ["R4 -", "R6 -"],
                                       "gender": "Total", "age": None},
                 since=max(since or 2010, 2010))
    agec = col_like(df, "age")
    rc = col_like(df, "supplementary")
    bands = {"15 to 24 years": "15-24", "25 to 44 years": "25-44", "25 to 54 years": "25-54",
             "15 years and over": "15+"}
    df = df[df[agec].isin(bands)]
    m = pd.DataFrame({"iso3": "CAN", "period": df["REF_DATE"].astype(str).str[:7],
                      "value": df["VALUE"], "age_band": df[agec].map(bands),
                      "statistic": df[rc].str[:2].str.lower()}).dropna()
    m = m.assign(year=m["period"].str[:4])
    g = m.groupby(["iso3", "age_band", "statistic", "year"], as_index=False).agg(
        value=("value", "mean"), n=("value", "size"), last=("period", "max"))
    g = g.assign(period=g["year"], estimated=g["n"] < 12, category="all",
                 method=[f"mean of {n} months (to {b})" for n, b in zip(g["n"], g["last"])])
    return frame(g.drop(columns=["year", "n", "last"]).assign(series="14-10-0077-01"),
                 measure="labour_slack", frequency="A", unit="% of labour force",
                 source="StatCan LFS",
                 comparability_note="StatCan supplementary unemployment rates R4 / R6.")


ONS_LMS = "https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/" \
    "employmentandemployeetypes/timeseries/{cdid}/lms/data"


def slack_uk(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """UK discouraged workers (ONS LF8B, 16-64) as % of the extended labour force (LF2K
    economically active 16-64 + discouraged). Eurostat stopped UK slack data in 2019."""
    def series(cdid):
        js = json.loads(f.get(ONS_LMS.format(cdid=cdid), label=f"ons:{cdid}",
                              validate=is_json))
        out = {}
        for p in js.get("months") or []:
            try:
                out.setdefault(int(p["year"]), []).append(float(p["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out
    disc, lf = series("lf8b"), series("lf2k")
    rows = [{"iso3": "GBR", "period": str(y), "value": 100 * (sum(d) / len(d)) /
             (sum(lf[y]) / len(lf[y]) + sum(d) / len(d)), "estimated": len(d) < 12,
             "method": f"mean of {len(d)} months"} for y, d in disc.items()
            if y in lf and y >= max(since or 2010, 2010)]
    if not rows:
        raise FetchError("ONS LF8B/LF2K: no observations")
    return frame(pd.DataFrame(rows).assign(series="ONS LF8B"), measure="labour_slack",
                 statistic="discouraged", age_band="16-64", category="all", frequency="A",
                 unit=PC_ELF, source="ONS LFS",
                 comparability_note="ONS discouraged workers (inactive, want a job, believe "
                 "no jobs are available): narrower than US 'marginally attached'.")


CPS_BASIC = "https://api.census.gov/data/{y}/cps/basic/{m}"
CPS_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
              "dec"]
CPS_VARS = ("PRTAGE,PEEDUCA,PEMLR,PRWKSTAT,PRDISC,PEHRWANT,PEHRAVL,PRMJOCC1,PTIO1OCD,"
            "PWCMPWGT,PESEX,PTERNWA,PWORWGT,PRERELG,HRMIS,HRHHID,HRHHID2,PULINENO,PESCHENR,"
            "PEHRUSL1")
_CPS_MEMO: dict = {}


def cps_month(f: CachedFetcher, y: int, i: int) -> pd.DataFrame | None:
    """One month of CPS basic microdata (ages 16-64), or None if not released / not
    collected (October 2025: federal shutdown). i = month index 0-11."""
    if (y, i) in _CPS_MEMO:
        return _CPS_MEMO[(y, i)]
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("CPS microdata needs CENSUS_API_KEY (free: "
                         "https://api.census.gov/data/key_signup.html)")
    try:
        js = json.loads(f.get(CPS_BASIC.format(y=y, m=CPS_MONTHS[i]),
                              {"get": CPS_VARS, "PRTAGE": "16:64", "key": key},
                              label=f"census:cps:{y}-{CPS_MONTHS[i]}", validate=is_json,
                              timeout=180))
    except FetchError as e:
        if "HTTP 404" in str(e) or "unexpected response" in str(e) or "HTTP 204" in str(e):
            _CPS_MEMO[(y, i)] = None
            return None
        raise
    d = pd.DataFrame(js[1:], columns=js[0]).loc[:, lambda x: ~x.columns.duplicated()]
    ids = d[["HRHHID", "HRHHID2"]].astype(str)
    d = d.drop(columns=["HRHHID", "HRHHID2"]).apply(pd.to_numeric, errors="coerce")
    d = pd.concat([d, ids], axis=1).assign(month=i + 1, year=y)
    _CPS_MEMO[(y, i)] = d
    return d
# Sales / office occupations that ISCO-08 puts in major groups 2-3 (professionals,
# associate professionals): graduate-level jobs for the over-qualification measure.
CPS_GRAD_OCC = {4710, 4800, 4810, 4820, 4840, 4850, 4920, 4930, 5000, 5250, 5500, 5710,
                5720, 5730}
CPS_BANDS = {"15-24": (16, 24), "20-24": (20, 24), "25-29": (25, 29), "30-34": (30, 34),
             "25-34": (25, 34), "25-54": (25, 54), "25-64": (25, 64), "15-64": (16, 64)}


def cps_education(peeduca: pd.Series) -> pd.Series:
    """CPS PEEDUCA -> education key. 'Some college, no degree' is upper secondary."""
    return pd.cut(peeduca, [0, 38, 40, 42, 43, 46],
                  labels=["below_upper_secondary", "upper_secondary", "short_tertiary",
                          "bachelor", "master_plus"]).astype(str)


def cps_rates(d: pd.DataFrame) -> dict:
    """Weighted labour-market rates for a CPS microdata frame (columns as CPS_VARS)."""
    w = d["PWCMPWGT"]
    lf = d["PEMLR"].between(1, 4)
    un = d["PEMLR"].isin([3, 4])
    emp = d["PEMLR"].isin([1, 2])
    # marginally attached (BLS): want a job, available, searched in the past 12 months
    ma = d["PEMLR"].between(5, 7) & d["PRDISC"].isin([1, 2])
    # part-timers who want full-time hours and are available (Eurostat UEMP_PT concept)
    ptw = emp & (d["PEHRWANT"] == 1) & (d["PEHRAVL"] != 2)
    pte = emp & d["PRWKSTAT"].isin([3, 6])          # BLS 'part time for economic reasons'
    occ = emp & d["PRMJOCC1"].between(1, 10)
    # non-graduate job ~ ISCO 4-9: outside management/business/financial/professional,
    # except the sales and office jobs ISCO classes as associate professional
    ocd = d["PTIO1OCD"] if "PTIO1OCD" in d else pd.Series(-1, index=d.index)
    nongrad_job = occ & d["PRMJOCC1"].between(3, 10) & ~ocd.isin(CPS_GRAD_OCC)
    W = lambda m: float(w[m].sum())  # noqa: E731
    lfw, elf = W(lf), W(lf) + W(ma)
    if lfw <= 0:
        return {}
    return {"unemployment": 100 * W(un) / lfw,
            "functional": 100 * (W(un) + W(ptw) + W(ma)) / elf,
            "u6": 100 * (W(un) + W(pte) + W(ma)) / elf,
            "hours_underemployment": 100 * W(ptw) / elf,
            "slack_extra": 100 * (W(ptw) + W(ma)) / elf,
            "marginally_attached": 100 * W(ma) / elf,
            "overqualification": 100 * W(nongrad_job) / W(occ) if W(occ) else float("nan"),
            "n_lf": float(lf.sum())}


def labour_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US rates by age and education straight from the CPS basic monthly microdata (Census
    API), pooled by calendar year: the current year so far and last year. BLS publishes
    no age x education U-6, and OECD/BLS annual tables lag (or, for 2025, are missing)."""
    now = datetime.now()
    rows = []
    for y in (now.year - 1, now.year):
        months = []
        for i in range(12):
            if y == now.year and i + 1 >= now.month:
                break
            d = cps_month(f, y, i)
            if d is not None:
                months.append(d.copy())
        if not months:
            continue
        d = pd.concat(months, ignore_index=True)
        d["PWCMPWGT"] = d["PWCMPWGT"] / d["month"].nunique()   # pooled = average month
        d["edu"] = cps_education(d["PEEDUCA"])
        tert = d["edu"].isin(["short_tertiary", "bachelor", "master_plus"])
        got = sorted(d["month"].unique())
        span = f"{CPS_MONTHS[got[0] - 1].title()}-{CPS_MONTHS[got[-1] - 1].title()} {y}"
        for band, (lo, hi) in CPS_BANDS.items():
            b = d[d["PRTAGE"].between(lo, hi)]
            for edu, sub in [("all", b), ("tertiary", b[tert.loc[b.index]])] + \
                    [(e, g) for e, g in b.groupby("edu")]:
                if edu == "nan":
                    continue
                for stat, v in cps_rates(sub).items():
                    rows.append({"iso3": "USA", "period": str(y), "age_band": band,
                                 "category": edu, "statistic": stat, "value": v,
                                 "estimated": len(got) < 12,
                                 "method": f"{len(got)} months pooled ({span})"})
    if not rows:
        raise FetchError("CPS microdata: no months fetched")
    return frame(pd.DataFrame(rows).assign(series="CPS basic monthly"), measure="us_cps_labour",
                 frequency="A", unit="% (see statistic)", source="Census/BLS CPS microdata",
                 comparability_note="Computed from CPS public-use microdata with composite "
                 "weights. functional = (unemployed + part-timers wanting full-time + "
                 "marginally attached) / (labour force + marginally attached); "
                 "overqualification = employed outside management, business, financial and "
                 "professional occupations (approximates ISCO 4-9).")


CAREER_STATES = ["FT_grad", "FT_nongrad", "PT", "U"]
CAREER_BANDS = {"20-24": (20, 24), "25-29": (25, 29), "30-34": (30, 34), "35-39": (35, 39),
                "40-44": (40, 44), "45-49": (45, 49), "50-54": (50, 54), "55-59": (55, 59),
                "60-64": (60, 64)}
CAREER_GROUPS = {"20-24": (20, 24), "25-29": (25, 29), "30-34": (30, 34), "35-44": (35, 44),
                 "45-54": (45, 54), "55-64": (55, 64)}
CAREER_EDU = {"below_upper_secondary": ["below_upper_secondary"],
              "upper_secondary": ["upper_secondary"], "short_tertiary": ["short_tertiary"],
              "bachelor": ["bachelor"], "master_plus": ["master_plus"],
              "no_degree": ["below_upper_secondary", "upper_secondary"],
              "degree": ["bachelor", "master_plus"], "all": None}
EARN_Q = (10, 25, 50, 75, 90)


def cps_career_state(d: pd.DataFrame) -> pd.Series:
    """Each person's situation: full-time in a graduate-level job (management, business,
    financial, professional, or the sales/office jobs ISCO puts in groups 2-3), full-time
    in another job, part-time (< 35 usual hours), or unemployed (incl. discouraged and
    other marginally attached workers). Students (enrolled, 16-24) and people not looking
    for work are left out."""
    emp = d["PEMLR"].isin([1, 2])
    un = d["PEMLR"].isin([3, 4]) | (d["PEMLR"].between(5, 7) & d["PRDISC"].isin([1, 2]))
    hrs = d["PEHRUSL1"]
    pt = emp & (hrs.between(1, 34) | ((hrs < 0) & d["PRWKSTAT"].isin([3, 4, 5, 6])))
    grad = d["PRMJOCC1"].isin([1, 2]) | d["PTIO1OCD"].isin(CPS_GRAD_OCC)
    st = np.select([un, pt, emp & grad, emp], ["U", "PT", "FT_grad", "FT_nongrad"], "NILF")
    st = pd.Series(st, index=d.index)
    st[d["PESCHENR"] == 1] = "student"
    return st


def _wq(x: pd.Series, w: pd.Series, qs) -> list[float]:
    o = np.argsort(x.to_numpy())
    xv, wv = x.to_numpy()[o], w.to_numpy()[o]
    c = np.cumsum(wv) / wv.sum()
    return [float(xv[min(np.searchsorted(c, q / 100), len(xv) - 1)]) for q in qs]


def _edu_mask(edu: pd.Series, key: str) -> pd.Series:
    v = CAREER_EDU[key]
    return pd.Series(True, index=edu.index) if v is None else edu.isin(v)


def career_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """The current US career picture from CPS microdata, last 12 months vs the 12 before:
    shares of each situation by age and degree; pay distributions in each (outgoing
    rotation groups); year-on-year moves between situations and individual pay growth
    (the same people matched one year apart, months-in-sample 1-4 -> 5-8 and 4 -> 8)."""
    now = pd.Period(datetime.now(), "M")
    got = {}
    for k in range(1, 40):                       # newest release back ~3 years
        per = now - k
        d = cps_month(f, per.year, per.month - 1)
        if d is not None:
            d = d.copy()
            d["edu"] = cps_education(d["PEEDUCA"]).astype(str)
            d["state"] = cps_career_state(d)
            got[per] = d
    if not got:
        raise FetchError("CPS microdata: no months fetched")
    last = max(got)
    cpi = fred(f, "CPIAUCSL").assign(per=lambda x: pd.PeriodIndex(x["date"].str[:7], freq="M"))
    cpi = cpi.set_index("per")["value"]
    rows = []

    def add(window, stat, band, edu, value, n, method):
        rows.append({"iso3": "USA", "period": str(window[1]), "age_band": band,
                     "category": edu, "statistic": stat, "value": value,
                     "method": f"{method}; n={n}; {window[0]}..{window[1]}",
                     "estimated": False})

    for wname, (w0, w1) in {"current": (last - 11, last),
                            "prior": (last - 23, last - 12)}.items():
        months = [m for m in got if w0 <= m <= w1]
        if not months:
            continue
        win = (w0, w1)
        tag = lambda s: s if wname == "current" else f"prior:{s}"  # noqa: E731
        d = pd.concat([got[m] for m in months], ignore_index=True)
        d["wc"] = d["PWCMPWGT"] / len(months)
        d["wo"] = d["PWORWGT"] / len(months)
        inlf = d["state"].isin(CAREER_STATES)
        org = d["HRMIS"].isin([4, 8]) & d["state"].isin(CAREER_STATES[:3]) & \
            (d["PRERELG"] == 1) & (d["PTERNWA"] > 0)
        for band, (lo, hi) in CAREER_BANDS.items():
            b = d["PRTAGE"].between(lo, hi)
            for ek in CAREER_EDU:
                m = b & _edu_mask(d["edu"], ek)
                s = d[m & inlf]
                if len(s) < 50:
                    continue
                tot = s["wc"].sum()
                for stn in CAREER_STATES:
                    add(win, tag(f"share|{stn}"), band, ek,
                        float(s.loc[s["state"] == stn, "wc"].sum() / tot), len(s),
                        "share of the labour force (incl. discouraged), students excluded")
                if wname != "current":
                    continue
                for stn in CAREER_STATES[:3]:
                    e = d[m & org & (d["state"] == stn)]
                    if len(e) < 30:
                        continue
                    for q, v in zip(EARN_Q, _wq(e["PTERNWA"], e["wo"], EARN_Q)):
                        add(win, f"earn_q{q}|{stn}", band, ek, v * 52 / 12, len(e),
                            "usual weekly earnings x 52 / 12, outgoing rotation groups")
        # ---- the same people one year apart
        pairs, grow = [], []
        for m in months:
            prev = m - 12
            if prev not in got:
                continue
            a, b = got[prev], got[m]
            k = ["HRHHID", "HRHHID2", "PULINENO"]
            x = a[a["HRMIS"].between(1, 4)].merge(b[b["HRMIS"].between(5, 8)], on=k,
                                                  suffixes=("0", "1"))
            ok = (x["PESEX0"] == x["PESEX1"]) & (x["PRTAGE1"] - x["PRTAGE0"]).between(0, 2)
            x = x[ok]
            pairs.append(x)
            g = x[(x["HRMIS0"] == 4) & (x["HRMIS1"] == 8) & (x["PRERELG0"] == 1) &
                  (x["PRERELG1"] == 1) & (x["PTERNWA0"] > 0) & (x["PTERNWA1"] > 0) &
                  x["state0"].isin(CAREER_STATES[:3]) & (x["state1"] == x["state0"])]
            if prev in cpi.index and m in cpi.index:
                g = g.assign(cpi_g=float(cpi[m] / cpi[prev] - 1))
                grow.append(g)
        if pairs:
            x = pd.concat(pairs, ignore_index=True)
            x = x[x["state0"].isin(CAREER_STATES) & x["state1"].isin(CAREER_STATES)]
            for grp, (lo, hi) in CAREER_GROUPS.items():
                for ek in CAREER_EDU:
                    sub = x[x["PRTAGE0"].between(lo, hi) & _edu_mask(x["edu0"], ek)]
                    for s0 in CAREER_STATES:
                        o = sub[sub["state0"] == s0]
                        if len(o) < 40:
                            continue
                        tot = o["PWCMPWGT0"].sum()
                        for s1 in CAREER_STATES:
                            add(win, tag(f"trans|{s0}>{s1}"), grp, ek,
                                float(o.loc[o["state1"] == s1, "PWCMPWGT0"].sum() / tot),
                                len(o), "share of those in the first situation who are in "
                                "the second a year later (matched CPS households)")
        if grow and wname == "current":
            g = pd.concat(grow, ignore_index=True)
            g["lg"] = np.log(g["PTERNWA1"] / g["PTERNWA0"])
            g = g[g["lg"].abs() < 1.5]          # coding errors (e.g. weekly vs annual)
            infl = float(np.log(1 + g["cpi_g"]).mean())
            add(win, "cpi_yoy", "total", "all", 100 * (np.exp(infl) - 1), len(g),
                "CPI-U change over the matched years")
            ahe = fred(f, "CES0500000003")
            ahe = ahe.assign(per=pd.PeriodIndex(ahe["date"].str[:7], freq="M")).set_index(
                "per")["value"]
            yo = [ahe[m] / ahe[m - 12] - 1 for m in months if m in ahe.index and
                  (m - 12) in ahe.index]
            if yo:
                add(win, "agg_wage_yoy", "total", "all", 100 * float(np.mean(yo)), len(yo),
                    "average hourly earnings, all private employees (BLS CES), change over "
                    "a year")
            for grp, (lo, hi) in list(CAREER_GROUPS.items()) + [("25-54", (25, 54)),
                                                                 ("16-64", (16, 64))]:
                for ek in ("all", "degree", "no_degree", "short_tertiary", "bachelor",
                           "master_plus"):
                    sub = g[g["PRTAGE0"].between(lo, hi) & _edu_mask(g["edu0"], ek)]
                    if len(sub) < 60:
                        continue
                    med = _wq(sub["lg"], sub["PWORWGT0"], (50,))[0]
                    add(win, "pay_growth_median", grp, ek, 100 * (np.exp(med) - 1),
                        len(sub), "median change in the same worker's usual weekly pay over "
                        "a year, nominal, workers in the same situation both years (moves "
                        "between situations are modelled separately)")
    if not rows:
        raise FetchError("CPS career: nothing computed")
    return frame(pd.DataFrame(rows).assign(series="CPS basic monthly"), measure="us_career",
                 frequency="A", unit="share / USD per month / %",
                 source="Census/BLS CPS microdata (Census API)")


def vacancies_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # The rate code was renamed JOBRATE -> JVR. The query used to "succeed" with 0 rows.
    df = eurostat(f, "jvs_q_nace2",
                  optional={"nace_r2": ["B-S"], "indic_em": ["JVR"], "s_adj": ["NSA"],
                            "sizeclas": ["TOTAL", "GE10"]},
                  since=since, geo=EU_GEOS)
    if "indic_em" in df:
        df = df[df["indic_em"].isin(["JVR", "JOBRATE"])]
    if df.empty:  # rate-only headline table as a fallback
        df = eurostat(f, "ei_lmjv_q_r2", optional={"nace_r2": ["B-S"], "s_adj": ["NSA"],
                                                   "sizeclas": ["TOTAL", "GE10"]},
                      since=since, geo=EU_GEOS)
    if df.empty:
        raise FetchError("jvs_q_nace2 / ei_lmjv_q_r2: no job-vacancy-rate rows")
    if "sizeclas" in df:
        df["_r"] = df["sizeclas"].map({"TOTAL": 0, "GE10": 1}).fillna(2)
        df = df.sort_values("_r").drop_duplicates(["geo", "time"])
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "series": "jvs_q_nace2:JVR"})
    return frame(out, measure="job_vacancy_rate", statistic="rate", frequency="Q", unit="%",
                 source="Eurostat JVS", comparability_note=NOTES["jvr_eu"])


def vacancies_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = fred(f, "JTSJOR")
    out = pd.DataFrame({"iso3": "USA", "period": d["date"].str[:7], "value": d["value"],
                        "series": "JTSJOR"})
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    return frame(out, measure="job_vacancy_rate", statistic="rate", frequency="M", unit="%",
                 source="BLS JOLTS (via FRED)", comparability_note=NOTES["jvr_us"])


def vacancies_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    d = statcan(f, "14100325", "Canada", {"statistic": "job vacancy rate"})
    out = pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"], "value": d["VALUE"],
                        "series": "14-10-0325-01"})
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    return frame(out, measure="job_vacancy_rate", statistic="rate", frequency="Q", unit="%",
                 source="StatCan JVWS", comparability_note=NOTES["jvr_ca"])


ONS_AP2Z = ("https://www.ons.gov.uk/employmentandlabourmarket/peopleinwork/"
            "employmentandemployeetypes/timeseries/ap2z/lms/data")


def vacancies_uk(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    js = json.loads(f.get(ONS_AP2Z, label="ons:AP2Z"))
    pts = js.get("quarters") or js.get("months") or []
    rows = []
    for p in pts:
        try:
            v = float(p["value"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({"iso3": "GBR", "period": p.get("date", "").replace(" ", "-"),
                     "value": v / (100 + v) * 100})
    out = pd.DataFrame(rows)
    if out.empty:
        raise FetchError("ONS AP2Z: no observations")
    if since:
        out = out[out["period"].str[:4].astype(int) >= since]
    out["series"] = "ONS AP2Z (converted)"
    return frame(out, measure="job_vacancy_rate", statistic="rate",
                 frequency="Q" if js.get("quarters") else "M", unit="%",
                 source="ONS Vacancy Survey", comparability_note=NOTES["jvr_uk"])


# ==========================================================================
# SAVING (as v1)
# ==========================================================================
NAAG_DIMS = ["FREQ", "REF_AREA", "MEASURE", "UNIT_MEASURE", "CHAPTER"]


def saving_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(f, "OECD.SDD.NAD,DSD_NAAG@DF_NAAG_V", NAAG_DIMS,
              {"FREQ": ["A"], "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
               "MEASURE": ["B8NS1M"], "UNIT_MEASURE": ["PT_B6N_S1M"], "CHAPTER": ["NAAG_V"]},
              since)
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "series": "NAAG_V:B8NS1M"})
    out = frame(out, measure="saving_rate_net", statistic="rate", frequency="A",
                unit="% of net disposable income", source="OECD NAAG",
                comparability_note=NOTES["sav_net"])
    try:
        out = pd.concat([out, saving_us_gap_fill(f, out)], ignore_index=True)
    except FetchError as e:
        log.warning("US saving-rate gap fill skipped: %s", e)
    return out


def saving_us_gap_fill(f: CachedFetcher, oecd_rows: pd.DataFrame) -> pd.DataFrame:
    """OECD has no US household saving rate after 2024 (BEA releases were delayed by the
    2025 shutdown). Fill later years, including the current year so far, from BEA's monthly
    personal saving rate (FRED PSAVERT), scaled to the OECD level at the last common year."""
    u = oecd_rows[oecd_rows["iso3"] == "USA"].set_index("year")["value"]
    if u.empty:
        return oecd_rows.iloc[0:0]
    m = fred(f, "PSAVERT")
    m["year"] = m["date"].str[:4].astype(int)
    a = m.groupby("year")["value"].agg(["mean", "size"])
    base = int(max(y for y in u.index if y in a.index and a.loc[y, "size"] == 12))
    k = u.loc[base] / a.loc[base, "mean"]
    rows = [{"iso3": "USA", "period": str(y), "value": a.loc[y, "mean"] * k,
             "series": "PSAVERT", "estimated": True,
             "method": f"BEA personal saving rate, mean of {a.loc[y, 'size']} months, x{k:.2f} "
                       f"(OECD/BEA in {base})"} for y in a.index if y > base and y not in u.index]
    return frame(pd.DataFrame(rows), measure="saving_rate_net", statistic="rate",
                 frequency="A", unit="% of net disposable income",
                 source="OECD NAAG (gap filled from BEA via FRED)",
                 comparability_note=NOTES["sav_net"]) if rows else oecd_rows.iloc[0:0]


def saving_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "tec00131", since=since, geo=EU_GEOS)
    if "na_item" in df:
        df = df[df["na_item"] == "SRG_S14_S15"]
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "series": "tec00131:SRG_S14_S15"})
    return frame(out, measure="saving_rate_gross", statistic="rate", frequency="A",
                 unit="% of gross disposable income", source="Eurostat",
                 comparability_note=NOTES["sav_gross"])


# ==========================================================================
# PRICE LEVELS / PPP
# ==========================================================================
WB_PPP = {
    "PA.NUS.PRVT.PP": ("ppp_private_consumption", "LCU per international $"),
    "PA.NUS.PPP": ("ppp_gdp", "LCU per international $"),
    "PA.NUS.FCRF": ("exchange_rate", "LCU per USD"),
}
# PA.NUS.PPPC.RF (price level ratio) was archived by the World Bank; it is now derived
# as PPP (private consumption) / market exchange rate.


def price_levels_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for ind, (stat, unit) in WB_PPP.items():
        try:  # one failed indicator must not take the whole module down
            d = worldbank(f, ind, list(COUNTRIES) + ["EMU"], since)
        except FetchError as e:
            log.warning("WB %s skipped: %s", ind, e)
            continue
        parts.append(d.rename(columns={"date": "period"})
                     .assign(statistic=stat, unit=unit, series=ind))
    if not parts:
        raise FetchError("No World Bank PPP/FX indicators")
    df = pd.concat(parts)
    piv = df.pivot_table(index=["iso3", "period"], columns="statistic", values="value")
    if {"ppp_private_consumption", "exchange_rate"} <= set(piv.columns):
        plr = (piv["ppp_private_consumption"] / piv["exchange_rate"]).dropna().reset_index()
        plr.columns = ["iso3", "period", "value"]
        parts.append(plr.assign(statistic="price_level_ratio",
                                unit="PPP / market exchange rate", estimated=False,
                                method="PA.NUS.PRVT.PP / PA.NUS.FCRF",
                                series="derived:PRVT.PP/FCRF"))
    df = pd.concat(parts)
    return frame(df, measure="price_level", frequency="A", source="World Bank WDI",
                 comparability_note=NOTES["ppp"])


def price_levels_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """PPPs (NAC per PPS) and price level indices (EU27=100) by consumption category."""
    df = eurostat(f, "prc_ppp_ind", optional={"na_item": ["PPP_EU27_2020", "PLI_EU27_2020"]},
                  since=since, geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": df["na_item"].str.lower(),
                        "category": df["ppp_cat_label"],
                        "unit": df["na_item"].map({"PPP_EU27_2020": "NAC per PPS",
                                                   "PLI_EU27_2020": "EU27=100"}),
                        "series": "prc_ppp_ind:" + df["ppp_cat"]})
    return frame(out, measure="price_level", frequency="A", source="Eurostat PPP",
                 comparability_note="Eurostat-OECD PPP programme, by ESA/COICOP category.")


# ==========================================================================
# COST OF LIVING: household spending surveys
# ==========================================================================
# Map COICOP (1999) codes to budget categories. Level-3/4 codes refine level-2.
COICOP_CATS = {
    "CP011": "groceries", "CP012": "groceries", "CP02": "alcohol_tobacco",
    "CP03": "clothing", "CP041": "rent_actual", "CP042": "rent_imputed_owners",
    "CP043": "home_maintenance", "CP044": "water_refuse", "CP045": "energy_home",
    "CP05": "furnishings_household", "CP06": "health_out_of_pocket",
    "CP071": "vehicle_purchase", "CP0722": "fuel_vehicle", "CP0721": "vehicle_maintenance",
    "CP0723": "vehicle_maintenance", "CP0724": "vehicle_other", "CP073": "transport_services",
    "CP08": "phone_internet", "CP09": "recreation", "CP10": "education",
    "CP11": "restaurants_hotels", "CP1252": "insurance_home", "CP1253": "insurance_health",
    "CP1254": "insurance_vehicle", "CP125": "insurance_other", "CP12": "personal_misc",
}
LEVEL2 = ["CP01", "CP02", "CP03", "CP04", "CP05", "CP06", "CP07", "CP08", "CP09", "CP10",
          "CP11", "CP12"]

HH_COUPLE = [r"two adults younger than 65", r"^two adults$",
             r"two adults(?!.*child)(?!.*65 years or over)"]
HH_1CHILD = [r"two adults (?:with|and) one dependent child"]
HH_2CHILD = [r"two adults (?:with|and) two dependent children"]
HH_ANYCHILD = [r"^two adults with dependent children$"]
HBS_AVG_CHILDREN = 1.7   # approx. EU mean children in 2-adult households with children
SHS_AVG_CHILDREN = 1.8   # approx. Canada, couples with children
CE_AVG_CHILDREN = 1.9    # BLS CE: married couple with children (~1.9 children)
HH_EXCLUDE = r"at least one aged 65|65 years or over"


def _hbs(f: CachedFetcher, code: str, since: int | None) -> pd.DataFrame:
    return eurostat(f, code, since=since, geo=EU_GEOS)


def spending_eu_hbs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Raw HBS tables; the couple budget is assembled in derive_couple_budget()."""
    parts = []
    for code, kind in [("hbs_exp_t134", "total_by_hhtype"), ("hbs_exp_t136", "total_by_urb"),
                       ("hbs_str_t224", "share_by_hhtype"), ("hbs_str_t226", "share_by_urb"),
                       ("hbs_exp_t121", "level_by_coicop")]:
        try:
            df = _hbs(f, code, since)
        except FetchError as e:
            log.warning("HBS %s skipped: %s", code, e)
            continue
        hh_col = col_like(df, "hhtyp", "hhcomp")
        hh_col = hh_col if hh_col and not hh_col.endswith("_label") else None
        out = pd.DataFrame({
            "iso3": df["iso3"], "period": df["time"], "value": df["value"],
            "statistic": kind,
            "category": df["coicop"] if "coicop" in df else "TOTAL",
            "household_type": df[hh_col + "_label"] if hh_col else "all households",
            "area_type": df["deg_urb_label"] if "deg_urb" in df else "all areas",
            "unit": df["unit"] if "unit" in df else "",
            "series": code + (":" + df[hh_col] if hh_col else "")})
        parts.append(out)
    if not parts:
        raise FetchError("No HBS tables fetched")
    return frame(pd.concat(parts), measure="household_spending_raw", frequency="5-yearly",
                 source="Eurostat HBS", comparability_note=NOTES["hbs"])


HICP_CODES = ["CP00", "CP01", "CP011", "CP012", "CP02", "CP03", "CP04", "CP041", "CP043",
              "CP044", "CP045", "CP05", "CP06", "CP07", "CP071", "CP072", "CP0721", "CP0722",
              "CP0723", "CP0724", "CP073", "CP08", "CP09", "CP10", "CP11", "CP12", "CP124",
              "CP125", "CP1252", "CP1253", "CP1254"]
# US CPI components (FRED, seasonally adjusted where available) for each budget line
US_CPI_PARTS = {"all": "CPIAUCSL", "groceries": "CUSR0000SAF11",
                "restaurants_hotels": "CUSR0000SEFV", "alcohol_tobacco": "CUSR0000SAF116",
                "clothing": "CUSR0000SAA", "furnishings_household": "CUSR0000SAH3",
                "household_operations": "CUSR0000SAH3", "utilities_phone": "CUSR0000SAH2",
                "health_out_of_pocket": "CUSR0000SAM", "insurance_health": "CUSR0000SAM2",
                "recreation": "CUSR0000SAR", "education": "CUSR0000SAE1",
                "transport_services": "CUSR0000SAS4", "personal_misc": "CUSR0000SAG",
                "vehicle_purchase": "CUSR0000SETA02", "vehicle_running": "CUSR0000SETD",
                "insurance_vehicle": "CUSR0000SETE", "childcare": "CUUR0000SEEB03",
                "rent": "CUSR0000SEHA"}


def hicp_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "prc_hicp_aind", optional={"unit": ["INX_A_AVG"], "coicop": HICP_CODES},
                  since=since, geo=EU_GEOS)
    df = df[df["unit"] == "INX_A_AVG"] if "unit" in df else df
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": "hicp_index", "category": df["coicop"],
                        "unit": "index", "series": "prc_hicp_aind:" + df["coicop"]})
    return frame(out, measure="price_level", frequency="A", source="Eurostat HICP",
                 comparability_note="Harmonised index of consumer prices, annual average.")


def prices_us_cpi_parts(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US CPI by component (annual averages of complete years), to see which costs have
    been rising faster than prices in general."""
    parts = []
    for key, sid in US_CPI_PARTS.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        d = d.assign(year=d["date"].str[:4])
        n = d.groupby("year")["value"].transform("size")
        a = d[n == 12].groupby("year")["value"].mean().reset_index()
        parts.append(pd.DataFrame({"iso3": "USA", "period": a["year"], "value": a["value"],
                                   "category": key, "series": f"FRED {sid}"}))
    if not parts:
        raise FetchError("no US CPI components")
    return frame(pd.concat(parts, ignore_index=True), measure="price_level",
                 statistic="cpi_component", unit="index", frequency="A",
                 source="BLS CPI by component (FRED)")


def inflation_outlook_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What bond markets expect US inflation to be: 5-year breakeven (TIPS vs nominal
    Treasuries) and the 5-year, 5-year-forward rate (FRED, daily -> monthly means)."""
    parts = []
    for sid, stat in (("T5YIE", "breakeven_5y"), ("T5YIFR", "forward_5y5y")):
        d = fred(f, sid)
        m = d.assign(period=d["date"].str[:7]).groupby("period")["value"].mean()
        parts.append(pd.DataFrame({"iso3": "USA", "period": m.index, "value": m.values,
                                   "statistic": stat, "series": f"FRED {sid}"}))
    return frame(pd.concat(parts, ignore_index=True), measure="inflation_outlook",
                 unit="% p.a.", frequency="M", source="FRED (Federal Reserve Board)")


CE_ITEMS = ["TOTALEXP", "FOODHOME", "FOODAWAY", "ALCBEVG", "HOUSING", "SHELTER", "OWNDWELL",
            "RNTDWELL", "UTILS", "HHOPER", "HKPGSUPP", "HHFURNSH", "APPAREL", "TRANS",
            "VEHPURCH", "GASOIL", "VEHOTHER", "VEHINSUR", "PUBTRANS", "HEALTH", "HLTHINSR",
            "ENTRTAIN", "PERSCARE", "EDUCATN", "MISC", "CASHCONT", "PERINSPN"]
CE_GROUPS = {"LB0603M": "married couple only",
             "LB0605M": "married couple, oldest child under 6",
             "LB0606M": "married couple, oldest child 6-17"}
CE_AGE = {"LB0403M": "age 25-34", "LB0404M": "age 35-44"}


def spending_us_ce(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    combos = [(i, g, lab) for i in CE_ITEMS for g, lab in CE_GROUPS.items()]
    combos += [("TOTALEXP", g, lab) for g, lab in CE_AGE.items()]
    for item, grp, lab in combos:
        sid = f"CXU{item}{grp}"
        try:
            d = fred(f, sid)
        except FetchError:
            continue
        parts.append(d.assign(iso3="USA", period=d["date"].str[:4], category=item,
                              household_type=lab, series=sid))
    if not parts:
        raise FetchError("No CE series on FRED")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].astype(int) >= since]
    return frame(df, measure="household_spending", statistic="mean", frequency="A",
                 unit="USD/year", area_type="all areas", source="BLS CE (via FRED)",
                 comparability_note=NOTES["ce"])


def spending_canada_shs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for pid, dim_kw, kind in [("11100224", "household type", "household_type"),
                              ("11100226", "size of area", "area_type")]:
        try:
            d = statcan(f, pid, "Canada", since=since)
        except FetchError as e:
            log.warning("SHS %s skipped: %s", pid, e)
            continue
        cat_col = col_like(d, "expenditure", "spending")
        grp_col = col_like(d, dim_kw)
        stat_col = col_like(d, "statistic")
        if stat_col:
            avg = d[stat_col].str.contains("average", case=False, na=False)
            if avg.any():
                d = d[avg]
        if cat_col is None or grp_col is None:
            log.warning("SHS %s: unexpected columns %s", pid, statcan_dims(d))
            continue
        row = {"iso3": "CAN", "period": d["REF_DATE"].str[:4], "value": d["VALUE"],
               "category": d[cat_col], "series": pid}
        row[kind] = d[grp_col]
        parts.append(pd.DataFrame(row))
    if not parts:
        raise FetchError("No SHS tables parsed")
    return frame(pd.concat(parts), measure="household_spending", statistic="mean",
                 frequency="A", unit="CAD/year", source="StatCan SHS",
                 comparability_note=NOTES["shs"])


# Household composition is Table A23, in WORKBOOK 2 ("expenditure by income"), not
# workbook 4. Editions (fye2025, fye2026, ...) are discovered from the /data JSON.
FS_WB2 = "/peoplepopulationandcommunity/personalandhouseholdfinances/expenditure/datasets/" \
         "familyspendingworkbook2expenditurebyincome"
FS_WB1 = "/peoplepopulationandcommunity/personalandhouseholdfinances/expenditure/datasets/" \
         "familyspendingworkbook1detailedexpenditureandtrends"


def _ons_latest_xlsx(f: CachedFetcher, dataset: str, default_file: str) -> tuple[bytes, str]:
    eds = ons_editions(f, dataset) or [dataset + "/fye2025"]
    for ed in eds[:2]:
        files = [u for u in ons_edition_files(f, ed) if u.endswith(".xlsx")] or \
            [f"{ed}/{default_file}"]
        try:
            return f.get(ONS + "/file?uri=" + files[0], label=f"ons:{ed[-40:]}",
                         validate=is_zip), ed.rsplit("/", 1)[1]
        except FetchError as e:
            log.warning("ONS %s: %s", ed, e)
    raise FetchError(f"ONS {dataset}: no downloadable edition")


def _num(s) -> float:
    return pd.to_numeric(str(s).replace(",", "").strip(), errors="coerce")


def parse_fs_a23(content: bytes) -> list[dict]:
    """Table A23: the column header is spread over ~7 rows ('Two adults' / 'with one
    child' / 'with two children'); rows are COICOP 1..13 and 'Total expenditure'."""
    grid = pd.read_excel(io.BytesIO(content), sheet_name="A23", header=None, dtype=str).fillna("")
    first = next(i for i in range(len(grid))
                 if str(grid.iat[i, 0]).lower().startswith("weighted number"))
    top = next(i for i in range(first) if "retired" in " ".join(grid.iloc[i]).lower())
    wanted, group, sup = {}, "", ""
    for j in range(2, grid.shape[1]):
        s = str(grid.iat[top, j]).strip().lower()
        sup = s or sup  # 'Retired households' / 'Non-retired' / 'Retired and non-retired'
        h = " ".join(" ".join(grid.iloc[top + 1:first, j].astype(str)).split()).lower()
        if not h:
            continue
        if m := re.match(r"^(.*?(?:one|two|three or more) (?:adults?|person))", h):
            group = m.group(1)
        full = f"{sup} | " + (h if not h.startswith("with") else f"{group} {h}")
        if sup.startswith("non-retired") and full.endswith("two adults"):
            wanted[j] = "two adults"
        elif re.search(r"two adults with one child", full):
            wanted[j] = "two adults, one child"
        elif re.search(r"two adults.*with two children", full):
            wanted[j] = "two adults, two children"
    rows = []
    for i in range(first, len(grid)):
        code, lab = str(grid.iat[i, 0]).strip(), str(grid.iat[i, 1]).strip()
        if re.fullmatch(r"\d{1,2}", code) and 1 <= int(code) <= 13:
            cat = f"CP{int(code):02d}"
        elif code.lower().startswith("total expenditure"):
            cat = "TOTAL"
        else:
            continue
        for j, hh in wanted.items():
            v = _num(grid.iat[i, j])
            if pd.notna(v):
                rows.append({"category": cat, "household_type": hh, "value": v})
        if cat == "TOTAL":
            break
    return rows


def parse_fs_a1_housing_split(content: bytes) -> dict[str, float]:
    """All-household shares of COICOP 4 sub-groups (4.1 net rent, 4.2 maintenance,
    4.3 water & dwelling services, 4.4 energy) from Table A1."""
    grid = pd.read_excel(io.BytesIO(content), sheet_name="A1", header=None, dtype=str).fillna("")
    vals = {}
    for i in range(len(grid)):
        cells = [str(x).strip() for x in grid.iloc[i] if str(x).strip()]
        if len(cells) >= 3 and cells[0] in ("4", "4.1.3", "4.2", "4.3", "4.4"):
            vals.setdefault(cells[0], _num(cells[2]))
    if not vals.get("4"):
        return {}
    return {k: v / vals["4"] for k, v in vals.items() if k != "4" and pd.notna(v)}


def spending_uk_family(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """ONS Family Spending A23 (GBP/week): 2 non-retired adults, 2 adults + 1 child,
    2 adults + 2 children. COICOP 4 ('housing (net), fuel & power') is split into
    rent / maintenance / water / energy using the all-household structure of A1."""
    raw, edition = _ons_latest_xlsx(f, FS_WB2, "workbook2expenditurebyincome.xlsx")
    rows = parse_fs_a23(raw)
    if not rows:
        raise FetchError("Family Spending: Table A23 two-adult columns not found")
    fy_end = int(re.search(r"(\d{4})", edition).group(1))
    split = {}
    try:
        raw1, _ = _ons_latest_xlsx(f, FS_WB1, "workbook1detailedexpenditureandtrends.xlsx")
        split = parse_fs_a1_housing_split(raw1)
    except (FetchError, ValueError, StopIteration) as e:
        log.warning("Family Spending A1 split skipped: %s", e)
    out = []
    for r in rows:
        if r["category"] == "CP04" and split:
            for code, sub in [("4.1.3", "CP041"), ("4.2", "CP043"), ("4.3", "CP044"),
                              ("4.4", "CP045")]:
                if code in split:
                    out.append({**r, "category": sub, "value": r["value"] * split[code],
                                "estimated": True,
                                "method": f"CP04 x all-household share of {code} (A1)"})
        out.append(r)
    df = pd.DataFrame(out).assign(iso3="GBR", period=str(fy_end - 1),
                                  series=f"Family Spending {edition} A23")
    df["estimated"] = df.get("estimated", False)
    df["estimated"] = df["estimated"].fillna(False).astype(bool)
    return frame(df.drop_duplicates(["category", "household_type"]),
                 measure="household_spending", statistic="mean", frequency="A",
                 unit="GBP/week", area_type="all areas",
                 source=f"ONS Family Spending {edition.upper()}",
                 comparability_note=NOTES["ons_fs"])


def spending_canada_mbm(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Market Basket Measure: cost of a *modest* basket for 2 adults + 2 children,
    by region incl. rural and small-population areas. A floor, not a typical spend."""
    d = statcan(f, "11100066", None, since=since)
    comp = col_like(d, "component")
    base = col_like(d, "base")
    if base:
        newest = sorted(d[base].dropna().unique())[-1]
        d = d[d[base] == newest]
    out = pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"], "value": d["VALUE"],
                        "category": d[comp] if comp else "total",
                        "area_type": d["GEO"], "statistic": "mbm_threshold",
                        "household_type": "2 adults + 2 children (reference family)",
                        "series": "11-10-0066-01"})
    return frame(out, measure="cost_floor", frequency="A", unit="CAD/year",
                 source="StatCan MBM", comparability_note=(
                     "Poverty-line basket (modest living standard) for 2 adults + 2 kids; "
                     "divide by sqrt(4)/sqrt(2)=1.414 for a couple."))


# ==========================================================================
# COST OF LIVING: housing
# ==========================================================================
def housing_costs_silc(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # tenure is now TOTAL/OWN/RENT (labels 'Owner'/'Tenant'); poverty status rskpovth
    df = eurostat(f, "ilc_mded03", optional={
        "rskpovth": ["TOTAL"],
        "hhcomp": ["TOTAL", "A2", "A2_DCH1", "A2_DCH2", "A2_DCH_GE3"]},
        since=since, geo=EU_GEOS)
    ten = col_like(df, "tenure")
    ten = ten if ten and not ten.endswith("_label") else None
    hh = col_like(df, "hhtyp", "hhcomp")
    hh = hh if hh and not hh.endswith("_label") else None
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": "mean_total_housing_cost",
                        "category": df[ten + "_label"] if ten else "all tenures",
                        "household_type": df[hh + "_label"] if hh else "all",
                        "unit": (df["unit"] if "unit" in df else "PPS") + "/month",
                        "series": "ilc_mded03"})
    if "incgrp" in df and (df["incgrp"] == "TOTAL").any():
        out = out[df["incgrp"].values == "TOTAL"]
    return frame(out, measure="housing", frequency="A", source="Eurostat EU-SILC",
                 comparability_note=NOTES["silc_housing"])


def house_price_index_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "prc_hpi_a", optional={"purchase": ["TOTAL"], "unit": ["I15_A_AVG"]},
                  since=since, geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": "house_price_index", "unit": "2015=100",
                        "series": "prc_hpi_a"})
    parts = [out]
    try:  # current year so far from the quarterly index (the annual one lags a year)
        q = eurostat(f, "prc_hpi_q", optional={"purchase": ["TOTAL"], "unit": ["I15_Q"]},
                     since=datetime.now().year - 1, geo=EU_GEOS)
        q = pd.DataFrame({"iso3": q["iso3"], "period": q["time"].astype(str),
                          "value": pd.to_numeric(q["value"], errors="coerce")}).dropna()
        have = {(r.iso3, str(r.period)) for r in out.itertuples()}
        q = q.assign(y=q["period"].str[:4])
        g = q.groupby(["iso3", "y"], as_index=False).agg(value=("value", "mean"),
                                                        n=("value", "size"))
        g = g[[(i, y) not in have for i, y in zip(g["iso3"], g["y"])]]
        parts.append(pd.DataFrame({"iso3": g["iso3"], "period": g["y"], "value": g["value"],
                                   "statistic": "house_price_index", "unit": "2015=100",
                                   "series": "prc_hpi_q", "estimated": True,
                                   "method": [f"mean of {n} quarters" for n in g["n"]]}))
    except FetchError as e:
        log.warning("Eurostat quarterly HPI skipped: %s", e)
    return frame(pd.concat(parts, ignore_index=True), measure="housing", frequency="A",
                 source="Eurostat HPI",
                 comparability_note="House price index, all dwellings, purchase prices.")


def rent_index_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """HICP actual rentals for housing (CP041), monthly -> calendar-year means (the
    current year so far): moves curated new-lease rents to the latest month."""
    df = eurostat(f, "prc_hicp_midx", required={"unit": ["I15"], "coicop": ["CP041"]},
                  since=max(since or 2015, 2015), geo=EU_GEOS)
    m = pd.DataFrame({"iso3": df["iso3"], "period": df["time"].astype(str),
                      "value": pd.to_numeric(df["value"], errors="coerce")}).dropna()
    out = annual_from_monthly(m, set(), ["iso3"])
    return frame(out.assign(series="prc_hicp_midx:CP041"), measure="housing",
                 statistic="rent_index", unit="2015=100", frequency="A",
                 source="Eurostat HICP (actual rentals)",
                 comparability_note="Rent index of sitting and new tenants (HICP CP041).")


BIS_HPI = {"GBR": "QGBN628BIS", "CAN": "QCAN628BIS", "CHE": "QCHN628BIS",
           "USA": "QUSN628BIS"}


def house_prices_bis(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """BIS nominal residential property prices (via FRED) where Eurostat has none."""
    parts = []
    for iso, sid in BIS_HPI.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        d = d.assign(year=d["date"].str[:4])
        g = d.groupby("year", as_index=False).agg(value=("value", "mean"), n=("value", "size"))
        parts.append(pd.DataFrame({"iso3": iso, "period": g["year"], "value": g["value"],
                                   "estimated": g["n"] < 4, "series": sid}))
    if not parts:
        raise FetchError("BIS house prices: nothing fetched")
    return frame(pd.concat(parts, ignore_index=True), measure="housing",
                 statistic="house_price_index", unit="index", frequency="A",
                 source="BIS residential property prices (via FRED)",
                 comparability_note="Nominal residential property prices.")


CENSUS = "https://api.census.gov/data/{y}/acs/acs1"


def housing_us_acs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # The Census API now answers key-less requests with an HTML "Missing Key" page
    # (HTTP 200), which used to be cached as data and then silently skipped.
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("Census API now requires a key: get one free at "
                         "https://api.census.gov/data/key_signup.html and set CENSUS_API_KEY. "
                         "US rent falls back to reference_values.csv (ACS 2025 median).")
    rows = []
    for y in range(max(since or 2005, 2005), datetime.now().year):
        if y == 2020:  # no standard 1-year release for 2020
            continue
        params = {"get": "NAME,B25064_001E,B25077_001E", "for": "us:1", "key": key}
        try:
            js = json.loads(f.get(CENSUS.format(y=y), params, label=f"census:acs1:{y}",
                                  validate=is_json, timeout=30))
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue  # year not released yet
            raise
        hdr, vals = js[0], js[1]
        rec = dict(zip(hdr, vals))
        rows.append({"period": str(y), "statistic": "median_gross_rent",
                     "value": rec.get("B25064_001E"), "unit": "USD/month", "series": "B25064"})
        rows.append({"period": str(y), "statistic": "median_home_value",
                     "value": rec.get("B25077_001E"), "unit": "USD", "series": "B25077"})
    if not rows:
        raise FetchError("ACS: nothing fetched")
    return frame(pd.DataFrame(rows).assign(iso3="USA"), measure="housing", frequency="A",
                 source="Census ACS", comparability_note=NOTES["acs"])


ACS_STATE_VARS = {"B25113_002E": "rent_recent_gross", "B25064_001E": "rent_median_gross",
                  "B25077_001E": "home_value", "B25103_001E": "re_taxes",
                  "B20017_003E": "earn_ft_male", "B20017_006E": "earn_ft_female",
                  "B19013_001E": "hh_income"}


def us_states_api(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Per state (+ US): ACS 1-year recent-mover rent, median rent, home value, real-estate
    taxes, full-time earnings and household income (latest year released), and the BEA
    regional price parity (FRED {ST}RPPALL)."""
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("ACS by state needs CENSUS_API_KEY")
    rows, yr = [], None
    for y in range(datetime.now().year - 1, datetime.now().year - 4, -1):
        try:
            got = []
            for geo in ("state:*", "us:1"):
                js = json.loads(f.get(CENSUS.format(y=y), {"get": "NAME," + ",".join(
                    ACS_STATE_VARS), "for": geo, "key": key}, label=f"census:acs1-states:{y}",
                    validate=is_json, timeout=60))
                got += [dict(zip(js[0], r)) for r in js[1:]]
            yr = y
            break
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue
            raise
    if yr is None:
        raise FetchError("ACS by state: no year available")
    for r in got:
        nm = "United States" if r["NAME"] == "United States" else r["NAME"]
        for v, col in ACS_STATE_VARS.items():
            rows.append({"category": nm, "statistic": col, "value": r.get(v), "period": str(yr),
                         "series": "ACS1 " + v})
    names = P_STATE_CODES()
    for code, nm in names.items():
        sid = "RPPALL" if code == "US" else f"{code}RPPALL"
        try:
            d = fred(f, sid)
        except FetchError:
            continue
        last = d.iloc[-1]
        rows.append({"category": nm, "statistic": "rpp", "value": last["value"],
                     "period": str(last["date"])[:4], "series": "FRED " + sid})
    return frame(pd.DataFrame(rows).assign(iso3="USA"), measure="us_state", frequency="A",
                 unit="mixed", source="Census ACS 1-year / BEA RPP (FRED)",
                 comparability_note="US states: ACS medians and BEA regional price parities.")


def P_STATE_CODES() -> dict[str, str]:
    """State code -> name, from the curated table shipped with the code."""
    p = Path(__file__).resolve().parent / "econsim" / "params" / "us_states.csv"
    t = pd.read_csv(p, comment="#")
    return dict(zip(t["state"], t["name"].replace({"US": "United States"})))


FRED_HOUSING = {
    "HSFMEDUSM052N": ("median_existing_single_family_price", "USD"),
    "MORTGAGE30US": ("mortgage_rate_30y_fixed", "%"),
    "APU000072610": ("electricity_price", "USD/kWh"),
    "APU000072620": ("utility_gas_price", "USD/therm"),
    "GASREGW": ("gasoline_price", "USD/gallon"),
    "CUUR0000SEHA": ("rent_index", "index"),          # CPI rent of primary residence
}


def housing_energy_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, (stat, unit) in FRED_HOUSING.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        meas = "housing" if stat.startswith(("median", "mortgage", "rent")) else \
            "energy_prices"
        parts.append(d.assign(iso3="USA", period=d["date"].str[:7], statistic=stat,
                              unit=unit, series=sid, measure=meas))
    if not parts:
        raise FetchError("No FRED housing/energy series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, frequency="M", source="FRED (NAR, Freddie Mac, BLS, EIA)",
                 comparability_note="US national series.")


def housing_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    try:  # CMHC average rents, all centres 10k+; we keep two-bedroom
        d = statcan(f, "34100133", None, {"type of unit": ["two bedroom", "two-bedroom"]},
                    since=since)
        st = col_like(d, "type of structure")
        if st:
            d = d[d[st].str.contains("row and apartment|apartment", case=False, na=False)]
        parts.append(pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"].str[:4],
                                   "value": d["VALUE"], "statistic": "average_rent_2br",
                                   "area_type": d["GEO"], "unit": "CAD/month",
                                   "series": "34-10-0133-01", "measure": "housing"}))
    except FetchError as e:
        log.warning("CMHC rents skipped: %s", e)
    try:  # posted 5-year conventional mortgage rate (Bank of Canada Valet)
        js = json.loads(f.get("https://www.bankofcanada.ca/valet/observations/V80691335/json",
                              label="boc:V80691335"))
        obs = [(o["d"], o["V80691335"]["v"]) for o in js.get("observations", [])
               if o.get("V80691335", {}).get("v") not in (None, "")]
        parts.append(pd.DataFrame({"iso3": "CAN", "period": [d[:7] for d, _ in obs],
                                   "value": [v for _, v in obs],
                                   "statistic": "mortgage_rate_5y_posted", "unit": "%",
                                   "series": "BoC V80691335", "measure": "housing"}))
    except (FetchError, ValueError, KeyError) as e:
        log.warning("BoC mortgage rate skipped: %s", e)
    try:  # rate actually paid on new insured 5y+ fixed mortgages (posted rates are ~2 pts
        # higher than what borrowers get); insured = under 20% down, like the default plan
        js = json.loads(f.get("https://www.bankofcanada.ca/valet/observations/V122667780/json",
                              label="boc:V122667780"))
        obs = [(o["d"], o["V122667780"]["v"]) for o in js.get("observations", [])
               if o.get("V122667780", {}).get("v") not in (None, "")]
        parts.append(pd.DataFrame({"iso3": "CAN", "period": [d[:7] for d, _ in obs],
                                   "value": [v for _, v in obs],
                                   "statistic": "mortgage_rate_new_loans", "unit": "%",
                                   "series": "BoC V122667780 (funds advanced, insured, "
                                             "fixed 5y+)", "measure": "housing"}))
    except (FetchError, ValueError, KeyError) as e:
        log.warning("BoC new-mortgage rate skipped: %s", e)
    try:  # regular gasoline, self-serve, by city
        d = statcan(f, "18100001", None, {"type of fuel": "regular unleaded gasoline"},
                    since=since)
        parts.append(pd.DataFrame({"iso3": "CAN", "period": d["REF_DATE"].str[:7],
                                   "value": d["VALUE"], "statistic": "gasoline_price",
                                   "area_type": d["GEO"], "unit": "CAD cents/litre",
                                   "series": "18-10-0001-01", "measure": "energy_prices"}))
    except FetchError as e:
        log.warning("StatCan gasoline skipped: %s", e)
    if not parts:
        raise FetchError("No Canadian housing series")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, frequency="M", source="StatCan / CMHC / Bank of Canada",
                 comparability_note="CMHC rents: purpose-built rentals in centres 10k+; "
                                    "mortgage rate = rate paid on new insured 5y+ fixed loans "
                                    "(BoC), else the posted 5y rate (higher).")


BOE_CSV = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
           "?csv.x=yes&Datefrom=01/Jan/2005&Dateto=now&SeriesCodes={s}&CSVF=TN&UsingCodes=Y")


def housing_uk_boe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    raw = f.get(BOE_CSV.format(s="IUMBV34"), label="boe:IUMBV34",
                headers={"User-Agent": "Mozilla/5.0 (research script)"})
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = ["date", "value"][:len(df.columns)]
    df["period"] = pd.to_datetime(df["date"], format="%d %b %Y", errors="coerce") \
        .dt.strftime("%Y-%m")
    df = df.dropna(subset=["period"])
    out = df.assign(iso3="GBR", statistic="mortgage_rate_2y_fixed_75ltv", unit="%",
                    series="BoE IUMBV34")
    return frame(out, measure="housing", frequency="M", source="Bank of England",
                 comparability_note="Quoted 2-year fixed rate, 75% LTV.")


ECB_API = "https://data-api.ecb.europa.eu/service/data/MIR/"


def mortgage_rates_ecb(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    eu = [COUNTRIES[c][0] for c in COUNTRIES if COUNTRIES[c][3] == "EU"]  # ISO2; GR=Greece
    key = f"M.{'+'.join(eu)}.B.A2C.A.R.A.2250..N"  # currency wildcard (non-euro NAC)
    params = {"format": "csvdata"}
    if since:
        params["startPeriod"] = f"{since}-01"
    df = pd.read_csv(io.BytesIO(f.get(ECB_API + key, params, label="ecb:MIR")), dtype=str)
    df["iso3"] = df["REF_AREA"].map(ISO2_TO_ISO3)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": "mortgage_rate_new_loans",
                        "unit": "% p.a.", "series": "MIR:" + df.get("KEY", "")})
    return frame(out.dropna(subset=["iso3"]), measure="housing", frequency="M",
                 source="ECB MFI interest rates",
                 comparability_note="Agreed rate on NEW lending for house purchase, all "
                                    "fixation periods (actual contracts, not quotes).")


def mortgage_rates_no_ch(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Norway: SSB table 10748, interest on NEW loans secured on dwellings, households,
    all fixation periods (actual contracts). Switzerland: SNB 'zikrepro' MEDIAN offered
    rate on new mortgages (10-year fixed and variable). Iceland has no free API here:
    see reference_values.csv."""
    parts = []
    try:
        q = {"query": [{"code": "Utlanstype", "selection": {"filter": "item", "values": ["04"]}},
                       {"code": "Sektor", "selection": {"filter": "item", "values": ["04b"]}},
                       {"code": "Rentebinding", "selection": {"filter": "item", "values": ["99"]}}],
             "response": {"format": "json-stat2"}}
        js = json.loads(f.get("https://data.ssb.no/api/v0/en/table/10748", label="ssb:10748",
                              method="POST", json_body=q, validate=is_json))
        idx = js["dimension"]["Tid"]["category"]["index"]
        per = sorted(idx, key=idx.get)
        vals = js["value"]
        parts.append(pd.DataFrame({"iso3": "NOR", "period": [p.replace("M", "-") for p in per],
                                   "value": vals, "statistic": "mortgage_rate_new_loans",
                                   "unit": "% p.a.", "series": "SSB 10748",
                                   "source": "Statistics Norway",
                                   "comparability_note": "Agreed rate on NEW household loans "
                                   "secured on dwellings, all fixation periods."}))
    except (FetchError, ValueError, KeyError) as e:
        log.warning("SSB mortgage rate skipped: %s", e)
    try:
        raw = f.get("https://data.snb.ch/api/cube/zikrepro/data/csv/en", label="snb:zikrepro",
                    validate=lambda b: b"zikrepro" in b[:200])
        rows = []
        for line in raw.decode("utf-8-sig").splitlines():
            c = [x.strip('"') for x in line.split(";")]
            if len(c) == 4 and c[1] == "05Q" and c[2] in ("10", "MV") and c[3]:
                rows.append({"period": c[0], "value": float(c[3]),
                             "statistic": "mortgage_rate_10y_fixed_median" if c[2] == "10"
                             else "mortgage_rate_variable_median"})
        d = pd.DataFrame(rows)
        parts.append(d.assign(iso3="CHE", unit="% p.a.", series="SNB zikrepro 05Q",
                              source="Swiss National Bank",
                              comparability_note="Median rate OFFERED on new mortgages "
                              "(bank survey); 10-year fixed is the most common choice."))
    except (FetchError, ValueError) as e:
        log.warning("SNB mortgage rate skipped: %s", e)
    if not parts:
        raise FetchError("No NO/CH mortgage rates")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, measure="housing", frequency="M")


# ==========================================================================
# COST OF LIVING: energy and health
# ==========================================================================
def energy_prices_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for code, band, stat in [("nrg_pc_204", "KWH2500-4999", "electricity_price"),
                             ("nrg_pc_202", "GJ20-199", "natural_gas_price")]:
        try:
            df = eurostat(f, code, optional={"nrg_cons": [band], "tax": ["I_TAX"],
                                             "currency": ["EUR", "NAC"]},
                          since=since, geo=EU_GEOS)
        except FetchError as e:
            log.warning("%s skipped: %s", code, e)
            continue
        unit = df["currency"] + "/" + (df["unit"] if "unit" in df else "unit")
        parts.append(pd.DataFrame({"iso3": df["iso3"], "period": df["time"],
                                   "value": df["value"], "statistic": stat, "unit": unit,
                                   "series": code}))
    if not parts:
        raise FetchError("No Eurostat energy prices")
    return frame(pd.concat(parts), measure="energy_prices", frequency="S",
                 source="Eurostat energy prices", comparability_note=NOTES["energy"])


WB_HEALTH = {
    "SH.XPD.OOPC.PP.CD": ("out_of_pocket_per_capita", "intl $ PPP/person/year"),
    "SH.XPD.CHEX.PP.CD": ("health_spending_per_capita", "intl $ PPP/person/year"),
}
# SDG 3.8.2 (catastrophic out-of-pocket health spending). The SH.UHC.OOPC.* codes were
# archived; the series now lives in the Health Equity and Financial Protection
# Indicators database (source 65) as HF.UHC.OOPC.*. Survey-based: latest year is often
# 2010-2018 for European countries.
WB_HEALTH_HEFPI = {
    "HF.UHC.OOPC.10.ZS": ("share_oop_over_10pct_income", "% of population"),
    "HF.UHC.OOPC.25.ZS": ("share_oop_over_25pct_income", "% of population"),
}


def health_worldbank(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    todo = [(i, v, (None,)) for i, v in WB_HEALTH.items()] + \
        [(i, v, ("65",)) for i, v in WB_HEALTH_HEFPI.items()]
    for ind, (stat, unit), srcs in todo:
        try:
            d = worldbank(f, ind, list(COUNTRIES), None if srcs != (None,) else since,
                          sources=srcs)
        except FetchError as e:
            log.warning("WB %s skipped: %s", ind, e)
            continue
        parts.append(d.rename(columns={"date": "period"})
                     .assign(statistic=stat, unit=unit, series=ind))
    if not parts:
        raise FetchError("No World Bank health indicators")
    return frame(pd.concat(parts), measure="health", frequency="A", source="World Bank / WHO",
                 comparability_note="WHO Global Health Expenditure Database via World Bank.")


# ==========================================================================
# LIFE EVENTS and EMERGENCIES
# ==========================================================================
NCC_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "INCOME_CURR", "HOUSEHOLD_TYPE",
            "AGE_CHILDREN", "INCOME_PART", "CHILDCARE_USE", "SOC_ASS_BENEFIT",
            "HOUSE_BENEFIT", "CHILDCARE_COST", "FREQ"]


# ---- families: mothers' work by children, leaving home, survey under-recording ---------
FAM_CHILD = {"0": "0", "1": "1", "2": "2", "GE3": "3"}
FAM_YOUNGEST = {"NAP": "none", "Y_LT6": "lt6", "Y6-11": "6-11", "Y_GE12": "ge12"}


def family_work_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Women 25-49: employment rate (by education) and part-time share by number of
    children and age of the youngest child, plus usual weekly hours of full-time and
    part-time women: the 'child penalty' in employment and hours."""
    yr = max(since or 2015, 2015)
    parts = []
    e = eurostat(f, "lfst_hheredch", required={"sex": ["F"], "unit": ["RT"], "age": ["Y25-49"]},
                 optional={"n_child": list(FAM_CHILD), "agechild": list(FAM_YOUNGEST),
                           "isced11": list(EDU_EUROSTAT)}, since=yr, geo=EU_GEOS)
    e = e[e["n_child"].isin(FAM_CHILD) & e["agechild"].isin(FAM_YOUNGEST) &
          e["isced11"].isin(EDU_EUROSTAT)]
    parts.append(pd.DataFrame({"iso3": e["iso3"], "period": e["time"], "value": e["value"],
                               "statistic": "employment_rate", "unit": "%",
                               "category": e["isced11"].map(EDU_EUROSTAT),
                               "household_type": e["n_child"].map(FAM_CHILD) + "|" +
                               e["agechild"].map(FAM_YOUNGEST), "series": "lfst_hheredch"}))
    p = eurostat(f, "lfst_hhptechi", required={"sex": ["F"], "unit": ["PC"], "age": ["Y25-49"]},
                 optional={"n_child": list(FAM_CHILD), "agechild": list(FAM_YOUNGEST)},
                 since=yr, geo=EU_GEOS)
    p = p[p["n_child"].isin(FAM_CHILD) & p["agechild"].isin(FAM_YOUNGEST)]
    parts.append(pd.DataFrame({"iso3": p["iso3"], "period": p["time"], "value": p["value"],
                               "statistic": "part_time_share", "unit": "%", "category": "all",
                               "household_type": p["n_child"].map(FAM_CHILD) + "|" +
                               p["agechild"].map(FAM_YOUNGEST), "series": "lfst_hhptechi"}))
    h = eurostat(f, "lfsa_ewhun2", required={"sex": ["F"], "unit": ["HR"], "age": ["Y25-54"]},
                 optional={"nace_r2": ["TOTAL"], "wstatus": ["EMP"], "worktime": ["FT", "PT"]},
                 since=yr, geo=EU_GEOS)
    h = h[h["worktime"].isin(["FT", "PT"])]
    parts.append(pd.DataFrame({"iso3": h["iso3"], "period": h["time"], "value": h["value"],
                               "statistic": "hours_" + h["worktime"].str.lower(), "unit": "hours",
                               "category": "all", "household_type": "any",
                               "series": "lfsa_ewhun2"}))
    return frame(pd.concat(parts, ignore_index=True), measure="family_work", frequency="A",
                 source="Eurostat LFS", age_band="25-49",
                 comparability_note="Women aged 25-49 by number of children and age of the "
                 "youngest child; hours = usual weekly hours in the main job.")


def leave_home_europe(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "yth_demo_030", required={"sex": ["T"], "unit": ["AVG"]},
                  since=max(since or 2010, 2010), geo=EU_GEOS)
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "series": "yth_demo_030"})
    return frame(out, measure="age_leave_home", statistic="mean", unit="years",
                 frequency="A", source="Eurostat LFS",
                 comparability_note="Estimated average age at which young people leave the "
                 "parental household.")


CPS_FAM_VARS = "PRTAGE,PESEX,PERRP,PRCHLD,PRNMCHLD,PEMLR,PEHRUSL1,PEEDUCA,HRHHID,HRHHID2," \
    "PWCMPWGT"
CPS_YOUNGEST = {1: "lt6", 2: "lt6", 5: "lt6", 6: "lt6", 7: "lt6", 8: "lt6", 9: "lt6", 11: "lt6",
                12: "lt6", 13: "lt6", 14: "lt6", 15: "lt6", 3: "6-11", 10: "6-11", 4: "ge12"}


def family_us_cps(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """From the CPS basic monthly microdata (last 3 published months): the age at which
    half of young adults no longer live with a parent, and women 25-49's employment rate
    and usual hours by number of own children and the youngest child's age."""
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("CPS microdata needs CENSUS_API_KEY")
    now = datetime.now()
    got, frames = [], []
    for back in range(1, 16):
        y, m = now.year, now.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        try:
            js = json.loads(f.get(CPS_BASIC.format(y=y, m=CPS_MONTHS[m - 1]),
                                  {"get": CPS_FAM_VARS, "PRTAGE": "16:49", "key": key},
                                  label=f"census:cps-fam:{y}-{m:02d}", validate=is_json,
                                  timeout=120))
        except FetchError as e:
            if "HTTP 404" in str(e) or "unexpected response" in str(e):
                continue
            raise
        d = pd.DataFrame(js[1:], columns=js[0]).loc[:, lambda x: ~x.columns.duplicated()]
        frames.append(d.assign(month=f"{y}-{m:02d}"))
        got.append(f"{y}-{m:02d}")
        if len(got) == 3:
            break
    if not frames:
        raise FetchError("CPS family microdata: no months fetched")
    d = pd.concat(frames, ignore_index=True)
    for c in d.columns:
        if c not in ("HRHHID", "HRHHID2", "month"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
    w = d["PWCMPWGT"]
    hh = d["HRHHID"].astype(str) + d["HRHHID2"].astype(str) + d["month"]
    has_parent = hh.isin(set(hh[d["PERRP"] == 50]))      # a parent of the reference person
    with_par = (d["PERRP"] == 48) | (d["PERRP"].isin([40, 41]) & has_parent)
    period, method = got[0][:4], f"CPS basic monthly {got[-1]} to {got[0]}"
    rows = []
    share = {a: float(w[(d.PRTAGE == a) & with_par].sum() / w[d.PRTAGE == a].sum())
             for a in range(18, 41)}
    below = [a for a in share if share[a] < 0.5]
    if below:
        a = below[0]
        prev = share.get(a - 1, 1.0)
        age = a - 1 + (prev - 0.5) / (prev - share[a]) if prev > share[a] else a
        rows.append({"measure": "age_leave_home", "statistic": "mean", "value": age + 0.5,
                     "unit": "years", "category": "all", "household_type": "any",
                     "series": "CPS age_leave_home",
                     "method": method + "; age at which half no longer live with a parent"})
    women = d[(d.PESEX == 2) & d.PRTAGE.between(25, 49)].copy()
    women["edu"] = cps_education(women["PEEDUCA"]).replace(
        {"short_tertiary": "tertiary", "bachelor": "tertiary", "master_plus": "tertiary"})
    women["young"] = women["PRCHLD"].map(CPS_YOUNGEST).fillna("none")
    women["n"] = women["PRNMCHLD"].clip(lower=0, upper=3).astype(int).astype(str)
    women.loc[women["young"] == "none", "n"] = "0"
    emp = women["PEMLR"].isin([1, 2])
    women["hrs"] = women["PEHRUSL1"].where(emp & (women["PEHRUSL1"] > 0))
    for (n, yg), g in women.groupby(["n", "young"]):
        if n != "0" and yg == "none":
            continue
        for edu, s in [("all", g)] + list(g.groupby("edu")):
            if edu == "nan" or len(s) < 30:
                continue
            ww = s["PWCMPWGT"]
            e = s["PEMLR"].isin([1, 2])
            hrs = s["hrs"].dropna()
            ht = f"{n}|{yg}"
            rows.append({"measure": "family_work", "statistic": "employment_rate",
                         "value": 100 * ww[e].sum() / ww.sum(), "unit": "%", "category": edu,
                         "household_type": ht, "series": "CPS family", "method": method})
            if edu == "all" and len(hrs):
                rows.append({"measure": "family_work", "statistic": "mean_hours",
                             "value": float((hrs * ww[hrs.index]).sum() / ww[hrs.index].sum()),
                             "unit": "hours", "category": "all", "household_type": ht,
                             "series": "CPS family", "method": method})
    out = pd.DataFrame(rows).assign(iso3="USA", period=period)
    return frame(out, frequency="A", source="Census/BLS CPS microdata", age_band="25-49",
                 estimated=True, comparability_note="Computed from CPS public-use microdata.")


def family_canada(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """StatCan 14-10-0120 (discontinued after 2020): women in couple families, employment
    and full-/part-time employment by the youngest child's age."""
    df = statcan(f, "14100120", match={"labour force": ["Population", "Employment",
                                                       "Full-time employment",
                                                       "Part-time employment"],
                                       "family type": "Female reference person/spouse with "
                                                      "employed husband",
                                       "age composition": None}, since=2015)
    lc, ac = col_like(df, "labour force"), col_like(df, "age composition")
    yb = {"Youngest child less than 6 years": "lt6", "Youngest child 6 to 15 years": "6-11",
          "Without children less than 16 years": "none"}
    df = df[df[ac].isin(yb)]
    p = df.pivot_table(index=["REF_DATE", ac], columns=lc, values="VALUE", aggfunc="first")
    rows = []
    for (yr, a), r in p.iterrows():
        if not r.get("Population"):
            continue
        n = "0" if yb[a] == "none" else "2"
        ht = f"{n}|{yb[a]}"
        rows.append({"period": str(yr)[:4], "statistic": "employment_rate",
                     "value": 100 * r["Employment"] / r["Population"], "unit": "%",
                     "household_type": ht})
        if r.get("Employment"):
            rows.append({"period": str(yr)[:4], "statistic": "part_time_share",
                         "value": 100 * r["Part-time employment"] / r["Employment"],
                         "unit": "%", "household_type": ht})
    out = pd.DataFrame(rows).assign(iso3="CAN", category="all", series="14-10-0120-01")
    return frame(out, measure="family_work", frequency="A", source="StatCan LFS",
                 age_band="all", estimated=True,
                 comparability_note="Women in couple families with an employed husband; "
                 "table discontinued after 2020; youngest child 6-15 used for 6-11 and 12+.")


HBS_DIV = [f"CP{i:02d}" for i in range(1, 13)]


def hbs_coverage_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Inputs for the HBS under-recording correction: HBS mean spending per household by
    COICOP division (EUR = PPS shares x EUR total), national-accounts household consumption
    by division, and the number of private households."""
    parts = []
    hb = eurostat(f, "hbs_exp_t121", required={"unit": ["PPS_HH"]},
                  optional={"coicop": HBS_DIV}, geo=EU_GEOS)
    tot = eurostat(f, "hbs_exp_t111", required={"unit": ["EUR_HH"]}, geo=EU_GEOS)
    hb = hb[hb["coicop"].isin(HBS_DIV)].copy()
    hb["share"] = hb["value"] / hb.groupby(["iso3", "time"])["value"].transform("sum")
    t = tot.set_index(["iso3", "time"])["value"]
    hb["eur"] = hb["share"] * [t.get((i, y)) for i, y in zip(hb["iso3"], hb["time"])]
    parts.append(pd.DataFrame({"iso3": hb["iso3"], "period": hb["time"], "value": hb["eur"],
                               "statistic": "hbs_eur_per_household", "category": hb["coicop"],
                               "unit": "EUR/year", "series": "hbs_exp_t121"}))
    co = eurostat(f, "nama_10_co3_p3", required={"unit": ["CP_MEUR"]},
                  optional={"coicop": HBS_DIV}, since=2010, geo=EU_GEOS)
    co = co[co["coicop"].isin(HBS_DIV)]
    parts.append(pd.DataFrame({"iso3": co["iso3"], "period": co["time"], "value": co["value"],
                               "statistic": "na_meur", "category": co["coicop"],
                               "unit": "million EUR", "series": "nama_10_co3_p3"}))
    hh = eurostat(f, "lfst_hhnhtych", required={"unit": ["THS_HH"]},
                  optional={"phhcomp": ["TOTAL"], "n_child": ["TOTAL"], "agechild": ["TOTAL"]},
                  since=2010, geo=EU_GEOS)
    parts.append(pd.DataFrame({"iso3": hh["iso3"], "period": hh["time"], "value": hh["value"],
                               "statistic": "households_ths", "category": "all",
                               "unit": "thousand", "series": "lfst_hhnhtych"}))
    return frame(pd.concat(parts, ignore_index=True), measure="hbs_coverage", frequency="A",
                 source="Eurostat HBS / national accounts / LFS",
                 comparability_note="Household budget survey vs national-accounts household "
                 "consumption by COICOP division.")


# US: BEA personal consumption (NIPA table 2.4.5, $bn) vs the BLS Consumer Expenditure
# Survey (all consumer units, $ per unit), matched to the same COICOP divisions as Europe
US_COVERAGE = {"CP01": (["DFXARC1A027NBEA", "-DAOPRC1A027NBEA"], ["CXUFOODHOMELB0101M"]),
               "CP02": (["DAOPRC1A027NBEA", "DTOBRC1A027NBEA"],
                        ["CXUALCBEVGLB0101M", "CXUTOBACCOLB0101M"]),
               "CP03": (["DCLORC1A027NBEA"], ["CXUAPPARELLB0101M"]),
               "CP05": (["DFDHRC1A027NBEA"], ["CXUHHFURNSHLB0101M"]),
               "CP09": (["DRCARC1A027NBEA", "DREQRC1A027NBEA"], ["CXUENTRTAINLB0101M"]),
               "CP10": (["DEDURC1A027NBEA"], ["CXUEDUCATNLB0101M"]),
               "CP11": (["DFSERC1A027NBEA"], ["CXUFOODAWAYLB0101M"])}


def coverage_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """US version of the survey under-recording inputs: BEA PCE by product vs BLS CE means
    for all consumer units, and the number of households (Census CPS/HVS, FRED)."""
    def annual(sid):
        d = fred(f, sid.lstrip("-"))
        a = d.assign(y=d["date"].str[:4]).groupby("y")["value"].mean()
        return -a if sid.startswith("-") else a
    rows = []
    for cp, (pce, ce) in US_COVERAGE.items():
        na = sum(annual(s) for s in pce) * 1000.0        # $bn -> $m
        sv = sum(annual(s) for s in ce)
        for y, v in na.dropna().items():
            rows.append({"period": y, "value": v, "statistic": "na_meur", "category": cp,
                         "unit": "million USD", "series": "+".join(pce)})
        for y, v in sv.dropna().items():
            rows.append({"period": y, "value": v, "statistic": "hbs_eur_per_household",
                         "category": cp, "unit": "USD/year", "series": "+".join(ce)})
    for y, v in annual("TTLHHM156N").dropna().items():
        rows.append({"period": y, "value": v, "statistic": "households_ths", "category": "all",
                     "unit": "thousand", "series": "TTLHHM156N"})
    df = pd.DataFrame(rows).assign(iso3="USA")
    df = df[df["period"].astype(int) >= 2015]
    return frame(df, measure="hbs_coverage", frequency="A",
                 source="BEA PCE / BLS CE / Census households (FRED)",
                 comparability_note="Consumer Expenditure Survey vs national-accounts "
                 "personal consumption by category.")


PRR_DIMS = ["REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "SEX", "AGE", "OPTIONALITY"]


NPRR_STAT = {"NPRR100": "net_rate_average_earner", "NPRR50": "net_rate_half_average",
             "NPRR200": "net_rate_double_average"}


def pensions_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Pensions at a Glance: net pension replacement rates at 50%, 100% and 200% of
    the average wage (mandatory schemes; mandatory + voluntary where voluntary plans are
    widespread). These are PROSPECTIVE: a person entering the labour market at 22 in the
    reference year, under the rules already legislated for their whole career."""
    df = oecd(f, "OECD.ELS.SPD,DSD_PAG@DF_PRR", PRR_DIMS,
              {"REF_AREA": [c for c in COUNTRIES if c != "LIE"],
               "MEASURE": list(NPRR_STAT)}, None)
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    g = df.groupby(["REF_AREA", "TIME_PERIOD", "OPTIONALITY", "MEASURE"],
                   as_index=False)["OBS_VALUE"].mean()   # average of men and women
    out = pd.DataFrame({"iso3": g["REF_AREA"], "period": g["TIME_PERIOD"],
                        "value": g["OBS_VALUE"], "statistic": g["MEASURE"].map(NPRR_STAT),
                        "category": g["OPTIONALITY"].map({"M": "mandatory",
                                                          "MV": "mandatory_voluntary"}),
                        "series": "DF_PRR " + g["MEASURE"]})
    return frame(out, measure="pension_replacement",
                 unit="% of net pre-retirement earnings", frequency="A",
                 source="OECD Pensions at a Glance",
                 comparability_note="Net replacement rate, full career from age 22 under "
                 "legislated rules; men and women averaged.")


def retirement_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Pensions at a Glance: the normal retirement age for someone who entered the
    labour market at 22 in the reference year (legislated increases, including links to
    life expectancy, applied), today's normal age, and life expectancy at 65 now and as
    projected for 2065 (UN World Population Prospects)."""
    df = oecd(f, "OECD.ELS.SPD,DSD_PAG@DF_PAG", PRR_DIMS,
              {"REF_AREA": [c for c in COUNTRIES if c != "LIE"],
               "MEASURE": ["FRPLF22", "CRPLF22", "LE"]}, None)
    df = df[(df["MEASURE"] != "LE") | (df["AGE"] == "Y65")]
    stat = {"FRPLF22": "normal_age_future_entrant", "CRPLF22": "normal_age_current",
            "LE": "life_expectancy_at_65"}
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": df["MEASURE"].map(stat),
                        "category": df["SEX"].map({"M": "male", "F": "female"}),
                        "series": "DF_PAG " + df["MEASURE"]})
    return frame(out, measure="retirement", unit="age (years)", frequency="A",
                 source="OECD Pensions at a Glance",
                 comparability_note="Retirement ages for a labour-market entrant at 22; "
                 "life expectancy at 65 expressed as the age reached (65 + years left).")


EAG_FIN_DIMS = ["REF_AREA", "MEASURE", "EDUCATION_LEV", "EXP_SOURCE", "EXP_DESTINATION",
                "EXPENDITURE_TYPE", "PRICE_BASE", "UNIT_MEASURE", "Q_SHEET"]


def university_cost_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD Education at a Glance (UOE finance): what households pay to tertiary
    institutions per full-time-equivalent student (tuition and fees net of grants paid to
    the institution, incl. fees financed by student loans), national currency. Where the
    household share is not reported, all private non-educational sources (households and
    firms) are used and flagged."""
    df = oecd(f, "OECD.EDU.IMEP,DSD_EAG_UOE_FIN@DF_UOE_FIN_INDIC_SOURCE_NATURE", EAG_FIN_DIMS,
              {"REF_AREA": [c for c in COUNTRIES if c != "LIE"], "MEASURE": ["FIN_PERSTUD"],
               "EDUCATION_LEV": ["ISCED11_5T8"], "EXP_SOURCE": ["S14", "S1D_NON_EDU"],
               "EXP_DESTINATION": ["INST_EDU"], "EXPENDITURE_TYPE": ["DIR_EXP"],
               "PRICE_BASE": ["V"], "UNIT_MEASURE": ["XDC_ST"]}, max(since or 2015, 2015))
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"],
                        "statistic": df["EXP_SOURCE"].map({"S14": "households",
                                                           "S1D_NON_EDU": "all_private"}),
                        "series": "EAG UOE_FIN FIN_PERSTUD"})
    return frame(out, measure="tertiary_private_cost", unit="national currency per FTE student",
                 frequency="A", source="OECD Education at a Glance",
                 comparability_note="Payments to tertiary institutions per FTE student; an "
                 "average over all students, including those paying nothing.")


# euro members in 2026 (Bulgaria joined on 1 January 2026): short rate = euro area
EURO_MEMBERS = {"AUT", "BEL", "BGR", "HRV", "CYP", "EST", "FIN", "FRA", "DEU", "GRC", "IRL",
                "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "PRT", "SVK", "SVN", "ESP"}
STES_DIMS = ["REF_AREA", "FREQ", "MEASURE", "UNIT_MEASURE", "ACTIVITY", "ADJUSTMENT",
             "TRANSFORMATION", "TIME_HORIZ", "METHODOLOGY"]


def interest_rates_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD financial-market statistics: 10-year government bond yields (IRLT) and
    3-month interbank rates (IR3TIB), monthly, for the non-EU countries."""
    areas = [c for c in COUNTRIES if COUNTRIES[c][3] != "EU" and c != "LIE"]
    df = oecd(f, "OECD.SDD.STES,DSD_STES@DF_FINMARK", STES_DIMS,
              {"REF_AREA": areas, "FREQ": ["M"], "MEASURE": ["IRLT", "IR3TIB"]},
              max(since or 2000, 2000))
    stat = {"IRLT": "govt_bond_10y", "IR3TIB": "interbank_3m"}
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": df["MEASURE"].map(stat),
                        "series": "STES " + df["MEASURE"]})
    return frame(out, measure="interest_rate", unit="% p.a.", frequency="M",
                 source="OECD financial market statistics")


def interest_rates_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """Eurostat: EMU convergence-criterion 10-year bond yields (all EU members) and 3-month
    money-market rates (euro area as one, plus the non-euro members)."""
    s0 = max(since or 2000, 2000)
    lt = eurostat(f, "irt_lt_mcby_m", required={"int_rt": ["MCBY"]}, since=s0,
                  geo=[COUNTRIES[c][1] for c in COUNTRIES if COUNTRIES[c][3] == "EU"])
    lt = lt.assign(statistic="govt_bond_10y", series="irt_lt_mcby_m")
    geos = [COUNTRIES[c][1] for c in COUNTRIES
            if COUNTRIES[c][3] == "EU" and c not in EURO_MEMBERS]
    own = eurostat(f, "irt_st_m", required={"int_rt": ["IRT_M3"]}, since=s0, geo=geos)
    # the euro-area aggregate has no ISO3 code, so eurostat() would drop it: fetch it raw
    ea = parse_jsonstat(f.get(EUROSTAT_API + "irt_st_m",
                              [("format", "JSON"), ("lang", "EN"), ("int_rt", "IRT_M3"),
                               ("geo", "EA"), ("sinceTimePeriod", str(s0))],
                              label="eurostat:irt_st_m:EA", validate=is_json))
    st_ = pd.concat([own] + [ea.assign(iso3=c) for c in sorted(EURO_MEMBERS)],
                    ignore_index=True).assign(statistic="interbank_3m", series="irt_st_m")
    d = pd.concat([lt, st_], ignore_index=True)
    out = pd.DataFrame({"iso3": d["iso3"], "period": d["time"].astype(str), "value": d["value"],
                        "statistic": d["statistic"], "series": d["series"]})
    return frame(out, measure="interest_rate", unit="% p.a.", frequency="M",
                 source="Eurostat (ECB) interest rates")


def deposit_rates_ecb(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """What banks actually pay households: ECB MFI interest rates on overnight deposits
    (new business = the current rate) and on deposits with an agreed maturity
    (outstanding amounts), euro-area members."""
    cc = "+".join(COUNTRIES[c][0] for c in sorted(EURO_MEMBERS))
    params = {"format": "csvdata", "startPeriod": f"{max(since or 2015, 2015)}-01"}
    parts = []
    for item, cov, stat in (("L21", "N", "deposit_overnight"), ("L22", "O", "deposit_term")):
        key = f"M.{cc}.B.{item}.A.R.A.2250.EUR.{cov}"
        d = pd.read_csv(io.BytesIO(f.get(ECB_API + key, params, label=f"ecb:MIR:{stat}")),
                        dtype=str)
        parts.append(pd.DataFrame({"iso3": d["REF_AREA"].map(ISO2_TO_ISO3),
                                   "period": d["TIME_PERIOD"], "value": d["OBS_VALUE"],
                                   "statistic": stat, "series": "MIR:" + d["KEY"]}))
    out = pd.concat(parts, ignore_index=True).dropna(subset=["iso3"])
    return frame(out, measure="interest_rate", unit="% p.a.", frequency="M",
                 source="ECB MFI interest rates (households)")


def deposit_rates_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """FDIC national rates (via FRED): savings accounts and 12-month CDs."""
    parts = []
    for sid, stat in (("SNDR", "deposit_overnight"), ("NDR12MCD", "deposit_term")):
        d = fred(f, sid)
        parts.append(pd.DataFrame({"iso3": "USA", "period": d["date"].str[:7],
                                   "value": d["value"], "statistic": stat,
                                   "series": f"FRED {sid}"}))
    return frame(pd.concat(parts, ignore_index=True), measure="interest_rate",
                 unit="% p.a.", frequency="M", source="FDIC national deposit rates (FRED)")


T720_DIMS = ["FREQ", "ADJUSTMENT", "REF_AREA", "COUNTERPART_AREA", "SECTOR",
             "COUNTERPART_SECTOR", "CONSOLIDATION", "ACCOUNTING_ENTRY", "TRANSACTION",
             "INSTR_ASSET", "MATURITY", "UNIT_MEASURE", "CURRENCY_DENOM", "VALUATION",
             "PRICE_BASE", "TRANSFORMATION", "TABLE_IDENTIFIER", "DEBT_BREAKDOWN"]
PORTFOLIO_ITEMS = {"F2": "deposits", "F22": "deposits_transferable", "F29": "deposits_other",
                   "F3": "debt_securities", "F511": "listed_shares", "F52": "fund_shares",
                   "F62": "life_insurance"}


def household_assets_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    """OECD financial balance sheets (SNA Table 720): what households (and NPISH) hold
    their financial savings in - deposits, bonds, listed shares, funds, life insurance."""
    df = oecd(f, "OECD.SDD.NAD,DSD_NASEC20@DF_T720R_A", T720_DIMS,
              {"FREQ": ["A"], "REF_AREA": [c for c in COUNTRIES if c != "LIE"],
               "SECTOR": ["S1M"], "ACCOUNTING_ENTRY": ["A"], "TRANSACTION": ["LE"],
               "INSTR_ASSET": list(PORTFOLIO_ITEMS), "MATURITY": ["T", "_Z"],
               "UNIT_MEASURE": ["XDC"]},
              max(since or 2015, 2015))
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"],
                        "statistic": df["INSTR_ASSET"].map(PORTFOLIO_ITEMS),
                        "series": "T720 " + df["INSTR_ASSET"]})
    return frame(out.dropna(subset=["statistic"]), measure="household_assets",
                 unit="national currency, millions", frequency="A",
                 source="OECD financial balance sheets (households and NPISH)")


def childcare_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c != "LIE"]
    df = oecd(f, "OECD.ELS.JAI,DSD_TAXBEN_NCC@DF_NCC", NCC_DIMS,
              {"REF_AREA": areas, "INCOME_CURR": ["AW100"], "HOUSEHOLD_TYPE": ["C_C2"],
               "INCOME_PART": ["AW67", "AW100"], "HOUSE_BENEFIT": ["NO"], "FREQ": ["A"]},
              since)
    if "CHILDCARE_COST" in df and (df["CHILDCARE_COST"] == "_T").any():
        df = df[df["CHILDCARE_COST"] == "_T"]
    if "SOC_ASS_BENEFIT" in df and (df["SOC_ASS_BENEFIT"] == "YES").any():
        df = df[df["SOC_ASS_BENEFIT"] == "YES"]
    hh = "couple 100%+" + df["INCOME_PART"].fillna("") + " AW, 2 children"
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "statistic": df["MEASURE"],
                        "household_type": hh, "unit": df["UNIT_MEASURE"],
                        "category": df.get("CHILDCARE_USE", ""), "series": "TAXBEN_NCC"})
    return frame(out, measure="childcare_net_cost", frequency="A", source="OECD TaxBEN",
                 comparability_note=NOTES["ncc"])


NRR_DIMS = ["REF_AREA", "MEASURE", "UNIT_MEASURE", "HOUSEHOLD_TYPE", "AGE_CHILDREN",
            "INCOME_PREV", "INCOME_PART", "UNEMP_DURATION", "SOC_ASS_BENEFIT",
            "HOUSE_BENEFIT", "FREQ"]


def job_loss_replacement_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    areas = [c for c in COUNTRIES if c != "LIE"]
    df = oecd(f, "OECD.ELS.JAI,DSD_TAXBEN_NRR@DF_NRR", NRR_DIMS,
              {"REF_AREA": areas, "HOUSEHOLD_TYPE": ["C_C0", "C_C2"], "INCOME_PREV": ["AW100"],
               "UNEMP_DURATION": ["M2", "M6", "M12", "M24"], "SOC_ASS_BENEFIT": ["YES"],
               "HOUSE_BENEFIT": ["NO"], "FREQ": ["A"]}, since)
    hh = df["HOUSEHOLD_TYPE"] + " | spouse " + df["INCOME_PART"].fillna("")
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"],
                        "statistic": "net_replacement_rate_" + df["UNEMP_DURATION"].str.lower(),
                        "household_type": hh, "unit": "% of pre-job-loss net income",
                        "series": "TAXBEN_NRR"})
    return frame(out, measure="emergency", frequency="A", source="OECD TaxBEN",
                 comparability_note=NOTES["nrr"])


def emergency_eu(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    # incgrp -> rskpovth, hhtyp -> hhcomp (Eurostat restructuring)
    df = eurostat(f, "ilc_mdes04", optional={"rskpovth": ["TOTAL"]}, since=since, geo=EU_GEOS)
    hh = dimcol(df, "hhcomp", "hhtyp")
    parts.append(pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                               "statistic": "share_cannot_cover_unexpected_expense",
                               "household_type": df[hh + "_label"], "unit": "% of persons",
                               "series": "ilc_mdes04:" + df[hh]}))
    try:
        # poverty threshold: was indic_il=LI_C_M60; now statinfo=MED_EI x rskpovth=B_60
        th = eurostat(f, "ilc_li01", optional={"hhcomp": ["A1"], "statinfo": ["MED_EI"],
                                               "rskpovth": ["B_60"], "unit": ["NAC", "EUR"]},
                      since=since, geo=EU_GEOS)
        th = th[th[dimcol(th, "hhcomp", "hhtyp")] == "A1"]
        unit_col = dimcol(th, "unit", "currency")
        th = th.sort_values(unit_col, key=lambda s: s.map({"NAC": 0, "EUR": 1}))
        th = th.drop_duplicates(["iso3", "time"])
        parts.append(pd.DataFrame({
            "iso3": th["iso3"], "period": th["time"], "value": th["value"] / 12,
            "statistic": "unexpected_expense_size", "household_type": "reference amount",
            "unit": th[unit_col] + " (monthly 60% poverty threshold, one person)",
            "series": "ilc_li01:A1:MED_EI:B_60/12"}))
    except FetchError as e:
        log.warning("ilc_li01 skipped: %s", e)
    return frame(pd.concat(parts), measure="emergency", frequency="A",
                 source="Eurostat EU-SILC", comparability_note=NOTES["mdes"])


# ==========================================================================
# UNCERTAINTY (as v1)
# ==========================================================================
def uncertainty_wui(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for iso3 in COUNTRIES:
        for sid, stat in ((f"WUIMA{iso3}", "wui_smoothed"), (f"WUI{iso3}", "wui")):
            try:
                d = fred(f, sid)
            except FetchError:
                continue
            per = pd.to_datetime(d["date"])
            parts.append(d.assign(iso3=iso3, statistic=stat, series=sid,
                                  period=per.dt.year.astype(str) + "-Q" +
                                  per.dt.quarter.astype(str)))
            break
    if not parts:
        raise FetchError("No WUI series fetched")
    df = pd.concat(parts)
    if since:
        df = df[df["period"].str[:4].astype(int) >= since]
    return frame(df, measure="uncertainty", frequency="Q", unit="index",
                 source="World Uncertainty Index (via FRED)", comparability_note=NOTES["wui"])


def uncertainty_wgi(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    try:
        d = worldbank(f, "GOV_WGI_PV_EST", list(COUNTRIES), since, sources=(None, "3", "2"))
        sid = "GOV_WGI_PV_EST"
    except FetchError:
        d = worldbank(f, "PV.EST", list(COUNTRIES), since, sources=("3",))
        sid = "PV.EST"
    d = d.rename(columns={"date": "period"}).assign(statistic="political_stability", series=sid)
    return frame(d, measure="uncertainty", frequency="A", unit="estimate (-2.5..2.5)",
                 source="World Bank WGI", comparability_note=NOTES["wgi"])


# ==========================================================================
# DERIVED TABLES
# ==========================================================================
PER_YEAR = {"month": 12, "week": 52, "year": 1}


class Ctx:
    """Lookup helpers over the long table."""

    def __init__(self, long: pd.DataFrame, refs: pd.DataFrame):
        # Everything is indexed ONCE here. The old helpers re-filtered the full
        # (3M-row) table on every call, thousands of times: that was most of the hour.
        self.long = long
        self.refs = refs
        self.by_measure: dict[str, pd.DataFrame] = {
            m: g for m, g in long.groupby("measure", sort=False)}
        self.by_measure_iso: dict[tuple[str, str], pd.DataFrame] = {}
        for m, g in self.by_measure.items():
            for iso3, gi in g.groupby("iso3", sort=False):
                self.by_measure_iso[(m, iso3)] = gi
        pl = self.measure("price_level")
        wb = pl[pl["source"].astype(str).str.startswith("World Bank WDI")]
        self._wb = {s: {(r.iso3, int(r.year)): r.value for r in g.itertuples()
                        if pd.notna(r.year)}
                    for s, g in wb.groupby("statistic")}
        for s in ("ppp_private_consumption", "exchange_rate", "cpi_index", "price_level_ratio"):
            self._wb.setdefault(s, {})
        self.latest_year = max([y for (_, y) in self._wb["cpi_index"]] or [2024])
        # HICP sub-indices: (iso3, coicop) -> {year: index}
        h = pl[pl["statistic"] == "hicp_index"]
        self._hicp: dict[tuple[str, str], dict[int, float]] = {}
        for r in h.itertuples():
            if pd.notna(r.year):
                self._hicp.setdefault((r.iso3, r.category), {})[int(r.year)] = r.value
        # Eurostat PPP (NAC per PPS): iso3 -> {year: value}, household consumption
        pp = pl[pl["statistic"] == "ppp_eu27_2020"]
        self._nac_pps: dict[str, dict[int, float]] = {}
        self._ppp_cat: dict[tuple[str, str], dict[int, float]] = {}
        for pat in ("actual individual consumption", "household final consumption"):
            s = pp[pp["category"].astype(str).str.contains(pat, case=False, na=False)]
            for r in s.itertuples():
                self._nac_pps.setdefault(r.iso3, {})[int(r.year)] = r.value
        for r in pp.itertuples():
            code = str(r.series).split(":")[-1]
            self._ppp_cat.setdefault((r.iso3, code), {})[int(r.year)] = r.value

    def measure(self, m: str) -> pd.DataFrame:
        return self.by_measure.get(m, self.long.iloc[0:0])

    def mi(self, m: str, iso3: str) -> pd.DataFrame:
        return self.by_measure_iso.get((m, iso3), self.long.iloc[0:0])

    def hicp_factor(self, iso3: str, code: str, from_year: int):
        """Price change from `from_year` to the latest year for COICOP `code`, falling
        back to its parents and finally the all-items index (CP00)."""
        for c in [code[:k] for k in range(len(code), 3, -1)] + ["CP00"]:
            s = self._hicp.get((iso3, c))
            if not s or from_year not in s:
                continue
            ly = max(s)
            return s[ly] / s[from_year], ly, c
        return 1.0, from_year, None

    def ppp_category(self, iso3: str, coicop2: str, year: int):
        """NAC per PPS for a COICOP division (CP01..CP12 -> A0101..A0112)."""
        code = "A01" + coicop2[2:4] if coicop2.startswith("CP") and len(coicop2) >= 4 else "A01"
        for c in (code, "A01"):
            s = self._ppp_cat.get((iso3, c))
            if s:
                y = year if year in s else max(s)
                return s[y]
        return None

    def wb(self, stat, iso3, year, back=6, ahead=2):
        t = self._wb.get(stat, {})
        for y in range(int(year), int(year) - back - 1, -1):
            if (iso3, y) in t:
                return t[(iso3, y)], y
        for y in range(int(year) + 1, int(year) + ahead + 1):  # small look-ahead
            if (iso3, y) in t:
                return t[(iso3, y)], y
        return None, None

    def cpi(self, iso3, year):
        v, _ = self.wb("cpi_index", iso3, year, back=0, ahead=0)
        return v

    def nac_per_pps(self, iso3, year):
        """Eurostat PPP (national currency per PPS) for household consumption."""
        s = self._nac_pps.get(iso3)
        if not s:
            return None
        return s[year] if year in s else s[max(s)]

    def lcu_per_eur(self, iso3, year):
        """Local currency per EUR, from World Bank LCU/USD ratios (euro members -> ~1)."""
        if iso3 in EURO_MEMBERS_NOW:
            return 1.0
        c, _ = self.wb("exchange_rate", iso3, year)
        e, _ = self.wb("exchange_rate", "EMU", year)
        return c / e if c and e else None

    def to_local(self, value, unit, iso3, year):
        """Convert 'EUR...' units to local currency; pass through local units."""
        cur = str(unit).split("/")[0].split(" ")[0]
        if cur == "EUR":
            r = self.lcu_per_eur(iso3, year)
            return value * r if r else None
        return value

    def intl(self, value_local, iso3, year):
        p, py = self.wb("ppp_private_consumption", iso3, year)
        return (value_local / p, py) if p else (None, None)

    def ref(self, iso3, item):
        r = self.refs[(self.refs["iso3"] == iso3) & (self.refs["item"] == item)]
        if r.empty and iso3 in EUROPE:
            r = self.refs[(self.refs["iso3"] == "_EUROPE_DEFAULT") & (self.refs["item"] == item)]
        return None if r.empty else r.iloc[0]


EURO_MEMBERS_NOW = {"AUT", "BEL", "HRV", "CYP", "EST", "FIN", "FRA", "DEU", "GRC", "IRL",
                    "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "PRT", "SVK", "SVN", "ESP"}
# Bulgaria joined 1 Jan 2026; World Bank may still report BGN for earlier years, so
# it is handled through the exchange-rate ratio rather than assumed to be 1.


# ---------- SES nowcast ---------------------------------------------------
def derive_ses_nowcast(long: pd.DataFrame) -> pd.DataFrame:
    ses = long[(long["source"] == "Eurostat SES") & (long["unit"] == "EUR/month")]
    if ses.empty:
        return pd.DataFrame(columns=TIDY_COLS)
    nama = long[(long["series"] == "nama_10_fte") & (long["unit"] == "EUR/year")]
    g = {(r.iso3, int(r.year)): r.value for r in nama.itertuples()}
    avw = long[(long["measure"] == "avg_wage") & (long["source"] == "OECD average wages")]
    avw = avw[avw["unit"].astype(str).str.match(r"^[A-Z]{3}\|V$", na=False)]
    g_oecd = {(r.iso3, int(r.year)): r.value for r in avw.itertuples()}
    out = []
    for iso3, grp in ses.groupby("iso3"):
        base_year = int(grp["year"].max())
        base = grp[grp["year"] == base_year]
        years = sorted({y for (c, y) in g if c == iso3 and y > base_year})
        src = "nama_10_fte (EUR)"
        table = g
        if not years:
            years = sorted({y for (c, y) in g_oecd if c == iso3 and y > base_year})
            src, table = "OECD average wage (national currency, current)", g_oecd
        if not years or (iso3, base_year) not in table:
            continue
        for y in years:
            factor = table[(iso3, y)] / table[(iso3, base_year)]
            rows = base.copy()
            rows["value"] = rows["value"] * factor
            rows["period"] = str(y)
            rows["year"] = y
            rows["estimated"] = True
            rows["method"] = f"SES {base_year} x growth of {src} {base_year}->{y} ({factor:.3f})"
            rows["source"] = "Eurostat SES (nowcast)"
            rows["comparability_note"] = NOTES["ses_nowcast"]
            out.append(rows)
    return pd.concat(out)[TIDY_COLS] if out else pd.DataFrame(columns=TIDY_COLS)


# ---------- Earnings in international $ -----------------------------------
def derive_earnings_ppp(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    e = long[(long["measure"] == "earnings_by_age") &
             ~long["unit"].astype(str).str.startswith("PPS")]
    out = []
    for r in e.itertuples():
        y = int(r.year)
        cur, per = str(r.unit).split("/")
        local = ctx.to_local(r.value * PER_YEAR[per], r.unit, r.iso3, y)
        if local is None:
            continue
        intl, py = ctx.intl(local, r.iso3, y)
        if intl is None:
            continue
        out.append({**{c: getattr(r, c) for c in TIDY_COLS}, "annual_local": local,
                    "ppp_year_used": py, "annual_intl_dollars": intl})
    return pd.DataFrame(out)


# ---------- Real wage history (stagnation) --------------------------------
def derive_real_wages(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Nominal -> real (latest-year prices) with CPI, indexed to the first year, CAGR.
    Vectorised: the old per-group loop over ~8k groups took most of a minute."""
    u = long["unit"].astype(str)
    sel = long[((long["measure"] == "earnings_by_age") & (long["statistic"] == "median") &
                ~u.str.startswith("PPS")) |
               ((long["measure"] == "household_income") & (long["statistic"] == "median") &
                u.str.startswith(("NAC", "XDC", "EUR"))) |
               # OECD units are ISO codes: 'USD|V' = current prices (|Q = constant,
               # USD_PPP = converted); Eurostat 'NAC/year' or 'EUR/year'
               ((long["measure"] == "avg_wage") &
                u.str.match(r"^([A-Z]{3}\|V|NAC/|EUR/)", na=False))]
    keys = ["iso3", "measure", "source", "age_band", "household_type", "area_type", "unit"]
    d = sel[keys + ["year", "value", "estimated"]].dropna(subset=["year"]).copy()
    for k in keys:  # pandas 3: astype(str) keeps <NA>, and groupby would DROP those rows
        d[k] = d[k].fillna("").astype(str)
    d["year"] = d["year"].astype(int)
    d = d.sort_values(keys + ["year"]).drop_duplicates(keys + ["year"], keep="last")
    d = d[d["iso3"] != "_"]
    eur = d["unit"].str.split("/").str[0].str.split(" ").str[0] == "EUR"
    fx = [ctx.lcu_per_eur(c, y) if e else 1.0 for c, y, e in zip(d["iso3"], d["year"], eur)]
    d["local"] = d["value"] * pd.to_numeric(pd.Series(fx, index=d.index), errors="coerce")
    d["cpi"] = [ctx.cpi(c, y) for c, y in zip(d["iso3"], d["year"])]
    ref = {c: ctx.wb("cpi_index", c, ctx.latest_year) for c in d["iso3"].unique()}
    d["cpi_ref"] = d["iso3"].map(lambda c: ref[c][0])
    d["price_base_year"] = d["iso3"].map(lambda c: ref[c][1])
    d = d.dropna(subset=["local", "cpi", "cpi_ref"])
    d["real_local"] = d["local"] * d["cpi_ref"] / d["cpi"]
    ppp = {c: ctx.intl(1.0, c, ref[c][1])[0] for c in d["iso3"].unique() if ref[c][1]}
    d["real_intl_dollars"] = d["real_local"] * d["iso3"].map(ppp)
    g = d.groupby(keys, sort=False)
    d["_n"] = g["year"].transform("size")
    d = d[d["_n"] >= 2]
    g = d.groupby(keys, sort=False)
    first_real, first_year = g["real_local"].transform("first"), g["year"].transform("first")
    d["index_first_year_100"] = d["real_local"] / first_real * 100
    last = g.tail(1).copy()
    span = last["year"] - first_year.loc[last.index]
    last["real_cagr_pct_since_first_year"] = ((last["real_local"] / first_real.loc[last.index])
                                              ** (1 / span.where(span > 0)) - 1) * 100
    # 10-year CAGR: latest point vs the last point at least 10 years earlier
    cagr10 = {}
    for idx, r in last.iterrows():
        k = tuple(r[keys])
        h = d.loc[g.groups[k]]
        old = h[h["year"] <= r["year"] - 10]
        if not old.empty:
            o = old.iloc[-1]
            cagr10[idx] = ((r["real_local"] / o["real_local"]) **
                           (1 / (r["year"] - o["year"])) - 1) * 100
    last["real_cagr_pct_last_10y"] = pd.Series(cagr10)
    d = d.join(last[["real_cagr_pct_since_first_year", "real_cagr_pct_last_10y"]])
    out = d.rename(columns={"unit": "unit_nominal", "value": "nominal"})
    out["country"] = out["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", c))[2])
    cols = ["iso3", "country", "measure", "source", "age_band", "household_type", "area_type",
            "unit_nominal", "year", "nominal", "price_base_year", "real_local",
            "real_intl_dollars", "index_first_year_100", "estimated",
            "real_cagr_pct_since_first_year", "real_cagr_pct_last_10y"]
    return out[cols].reset_index(drop=True)


# ---------- Couple budget ------------------------------------------------
BUDGET_LABELS = {
    "groceries": "Groceries", "alcohol_tobacco": "Alcohol & tobacco", "clothing": "Clothing",
    "rent_actual": "Rent (survey avg over ALL households - see housing rows)",
    "home_maintenance": "Home maintenance", "water_refuse": "Water & refuse",
    "energy_home": "Electricity, gas, heating", "furnishings_household": "Furnishings & household",
    "health_out_of_pocket": "Healthcare out of pocket", "vehicle_purchase": "Vehicle purchase",
    "fuel_vehicle": "Vehicle fuel", "vehicle_maintenance": "Vehicle maintenance & parts",
    "vehicle_other": "Other vehicle services (parking, tolls)",
    "transport_services": "Public transport & other transport services",
    "phone_internet": "Phone & internet", "recreation": "Recreation & culture",
    "education": "Education", "restaurants_hotels": "Restaurants & hotels",
    "insurance_home": "Home insurance", "insurance_health": "Health insurance (private)",
    "insurance_vehicle": "Vehicle insurance", "insurance_other": "Other insurance",
    "personal_misc": "Personal care & misc", "council_tax": "Council tax",
    "health_insurance_mandatory": "Mandatory health insurance premiums",
    "household_operations": "Household operations (incl. phone, services)",
    "utilities_phone": "Utilities & phone", "vehicle_all": "Private transport (all)",
    "transport_all": "Transport (all)", "housing_net_fuel_power": "Housing (net), fuel & power",
    "vehicle_running": "Vehicle running costs (fuel, insurance, repairs)",
}
CE_TO_CAT = {
    "FOODHOME": "groceries", "FOODAWAY": "restaurants_hotels", "ALCBEVG": "alcohol_tobacco",
    "RNTDWELL": "rent_actual", "OWNDWELL": "owner_costs_mortgage_tax_maint",
    "UTILS": "utilities_phone", "HHOPER": "household_operations",
    "HKPGSUPP": "furnishings_household", "HHFURNSH": "furnishings_household",
    "APPAREL": "clothing", "VEHPURCH": "vehicle_purchase", "GASOIL": "fuel_vehicle",
    "VEHINSUR": "insurance_vehicle", "PUBTRANS": "transport_services",
    "HLTHINSR": "insurance_health", "ENTRTAIN": "recreation", "PERSCARE": "personal_misc",
    "EDUCATN": "education", "MISC": "personal_misc",
}


# Rent/owner costs come from the housing rows (rent vs mortgage is the user's switch),
# so they are excluded from the baseline total. UK CP04 mixes rent and energy (noted).
HOUSING_TENURE_CATS = ["rent_actual", "rent_imputed_owners", "owner_costs_mortgage_tax_maint",
                       "other_items_incl_mortgage_interest", "housing_net_fuel_power", "TOTAL"]


def derive_couple_budget(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    eu = pd.DataFrame(_budget_eu(long, ctx))
    uk = pd.DataFrame(_budget_uk(long, ctx))
    if not uk.empty and not eu.empty:  # national survey (FYE2025) beats HBS (2015/2020)
        eu = eu[eu["iso3"] != "GBR"]
    df = pd.concat([eu, uk, pd.DataFrame(_budget_us(long, ctx)),
                    pd.DataFrame(_budget_ca(long, ctx))], ignore_index=True)
    if df.empty:
        return df
    df = pd.concat([df, pd.DataFrame(_budget_proxy(df, ctx))], ignore_index=True)
    df = pd.concat([df, pd.DataFrame(_mandatory_health(df, ctx))], ignore_index=True)
    df["category_label"] = df["category"].map(BUDGET_LABELS).fillna(df["category"])
    return df


def _match_hh(series: pd.Series, patterns: list[str]) -> str | None:
    s = series.astype(str)
    for pat in patterns:
        m = s[s.str.contains(pat, case=False, regex=True) &
              ~s.str.contains(HH_EXCLUDE, case=False, regex=True)]
        if not m.empty:
            return m.iloc[0]
    return None


def _per_household(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Prefer per-household units; if only per-adult-equivalent exists, return it with
    the modified-OECD factor for a couple (1.5) so values become per couple household."""
    u = df["unit"].astype(str)
    hh = df[u.str.contains("HH", case=False)]
    if not hh.empty:
        return hh, 1.0
    ae = df[u.str.contains("AE", case=False)]
    if not ae.empty:
        return ae, 1.5
    return df, 1.0


# Non-rent parts of COICOP 04 (housing, water, electricity, gas): maintenance, water and
# refuse, energy. Rents (CP041 actual, CP042 imputed) are covered by the rent / purchase.
CP04_NON_RENT = ("CP043", "CP044", "CP045")


def _budget_eu(long, ctx):
    hbs = long[long["measure"] == "household_spending_raw"]
    out = []
    cp04_split: dict[str, list[float]] = {c: [] for c in CP04_NON_RENT}  # share of CP04
    for iso3, g in hbs.groupby("iso3"):
        tot_all = g[g["statistic"] == "total_by_hhtype"]
        if tot_all.empty:
            continue
        year = int(tot_all["year"].max())
        tot, ae_factor = _per_household(tot_all[tot_all["year"] == year])
        couple_label = _match_hh(tot["household_type"], HH_COUPLE)
        if couple_label is None:
            continue
        total_pps = tot[tot["household_type"] == couple_label]["value"].iloc[0] * ae_factor
        nat_tot = tot[tot["household_type"].astype(str).str.contains("^total", case=False)]
        # --- towns & suburbs factor on the total ---------------------------------
        urb, _ = _per_household(g[(g["statistic"] == "total_by_urb") & (g["year"] == year)])
        deg2 = urb[urb["area_type"].astype(str).str.contains("towns and suburbs", case=False)]
        allu = urb[urb["area_type"].astype(str).str.contains("^total|all areas", case=False)]
        denom = allu["value"].iloc[0] if not allu.empty else (
            nat_tot["value"].iloc[0] / (1.5 if ae_factor == 1.5 else 1)
            if not nat_tot.empty and ae_factor == 1.0 else None)
        urb_factor = deg2["value"].iloc[0] / denom if (not deg2.empty and denom) else 1.0
        # --- level-2 structure of the couple type, tilted towards towns & suburbs --
        sh = g[(g["statistic"] == "share_by_hhtype") & (g["year"] == year)]
        shares = {r.category: r.value / 1000 for r in
                  sh[sh["household_type"] == couple_label].itertuples() if r.category in LEVEL2}
        national = {r.category: r.value / 1000 for r in
                    sh[sh["household_type"].astype(str).str.contains("^total", case=False)]
                    .itertuples() if r.category in LEVEL2}
        lv = g[(g["statistic"] == "level_by_coicop") & (g["year"] == year)]
        lv, _ = _per_household(lv) if not lv.empty else (lv, 1.0)
        lvl = {r.category: r.value for r in lv.itertuples()}
        if lvl.get("CP04") and all(lvl.get(c) for c in CP04_NON_RENT):
            for c in CP04_NON_RENT:
                cp04_split[c].append(lvl[c] / lvl["CP04"])
        if not national and lvl.get("CP00"):
            national = {c: lvl[c] / lvl["CP00"] for c in LEVEL2 if c in lvl}
        if not shares:
            shares = dict(national)
        if not shares:
            continue
        su = g[(g["statistic"] == "share_by_urb") & (g["year"] == year)]
        su2 = {r.category: r.value / 1000 for r in
               su[su["area_type"].astype(str).str.contains("towns and suburbs", case=False)]
               .itertuples()}
        adj = {c: v * (su2[c] / national[c] if su2.get(c) and national.get(c) else 1.0)
               for c, v in shares.items()}
        s = sum(adj.values()) or 1.0
        adj = {c: v / s for c, v in adj.items()}
        # --- split into budget categories using national detailed structure -------
        cat_share: dict[str, tuple[float, str]] = {}
        for parent in LEVEL2:
            if parent not in adj:
                continue
            kids = [c for c in COICOP_CATS if c.startswith(parent) and len(c) > 4]
            used = 0.0
            if lvl.get(parent):
                for c in kids:
                    if c not in lvl:
                        continue
                    sub = lvl[c]
                    if c == "CP125":  # 'other insurance' = CP125 minus listed subtypes
                        sub = max(lvl[c] - sum(lvl.get(k, 0) for k in
                                               ("CP1252", "CP1253", "CP1254")), 0)
                    sh_c = adj[parent] * sub / lvl[parent]
                    cat_share[c] = (sh_c, COICOP_CATS[c])
                    used += sh_c
            if parent in COICOP_CATS:  # remainder (or all of it) to the parent category
                cat_share[parent] = (max(adj[parent] - used, 0), COICOP_CATS[parent])
            elif used == 0:
                cat_share[parent] = (adj[parent], parent.lower() + "_unsplit")
        nac = ctx.nac_per_pps(iso3, year)
        if not nac:
            continue
        couple_pps = total_pps * urb_factor
        for code, (share, cat) in cat_share.items():
            annual_local = couple_pps * share * nac
            factor, py, used_idx = ctx.hicp_factor(iso3, code, year)
            annual_local *= factor
            intl, _ = ctx.intl(annual_local, iso3, py)
            out.append({"iso3": iso3, "country": COUNTRIES[iso3][2], "category": cat,
                        "coicop": code, "monthly_local": annual_local / 12,
                        "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                        "monthly_intl_dollars": intl / 12 if intl else None,
                        "survey_year": year, "price_year": py,
                        "household_type": couple_label,
                        "area_type": "towns and suburbs" if urb_factor != 1.0 else "all areas",
                        "method": f"HBS couple total x share; urb factor {urb_factor:.3f}; "
                                  f"HICP {used_idx or 'none'} x{factor:.3f}"
                                  + ("; per-adult-equivalent x1.5" if ae_factor != 1 else ""),
                        "source": "Eurostat HBS", "estimated": True})
    return _split_unsplit_cp04(out, cp04_split)


def _split_unsplit_cp04(out: list[dict], cp04_split: dict[str, list[float]]) -> list[dict]:
    """A country without HBS detail for COICOP 04 got the whole group as 'cp04_unsplit',
    and that group is mostly actual + imputed RENT, which the rent / house purchase already
    pays. Keep only its utilities and maintenance, using the median structure of the
    countries that publish the detail."""
    med = {c: float(pd.Series(v).median()) for c, v in cp04_split.items() if v}
    res = []
    for r in out:
        if r["category"] != "cp04_unsplit":
            res.append(r)
            continue
        if len(med) < len(CP04_NON_RENT):
            continue  # no structure to borrow: drop it rather than double-count rent
        for c in CP04_NON_RENT:
            k = dict(r)
            k.update(category=COICOP_CATS[c], coicop=c,
                     monthly_local=r["monthly_local"] * med[c],
                     monthly_intl_dollars=(r["monthly_intl_dollars"] or 0) * med[c] or None,
                     method=r["method"] + f"; no CP04 detail: x{med[c]:.3f} = median {c} share "
                     "of CP04 in countries with detail")
            res.append(k)
    return res


def _budget_us(long, ctx):
    ce = long[(long["source"] == "BLS CE (via FRED)") &
              (long["household_type"] == "married couple only")]
    if ce.empty:
        return []
    year = int(ce["year"].max())
    ce = ce[ce["year"] == year]
    out = []
    v = ce.set_index("category")["value"]
    # Residual categories: FRED only carries the CE aggregates for these groups
    derived = []
    if {"TRANS", "VEHPURCH", "PUBTRANS"} <= set(v.index):
        derived.append(("CE:TRANS-VEHPURCH-PUBTRANS", "vehicle_running",
                        v["TRANS"] - v["VEHPURCH"] - v["PUBTRANS"]))
    if {"HEALTH", "HLTHINSR"} <= set(v.index):
        derived.append(("CE:HEALTH-HLTHINSR", "health_out_of_pocket",
                        v["HEALTH"] - v["HLTHINSR"]))
    for code, cat, val in derived:
        intl, _ = ctx.intl(val, "USA", year)
        out.append({"iso3": "USA", "country": "United States", "category": cat,
                    "coicop": code, "monthly_local": val / 12, "currency": "USD",
                    "monthly_intl_dollars": intl / 12 if intl else None,
                    "survey_year": year, "price_year": year,
                    "household_type": "married couple only", "area_type": "all areas",
                    "method": f"CE mean, residual {code.split(':')[1]} / 12",
                    "source": "BLS CE", "estimated": True})
    for r in ce.itertuples():
        cat = CE_TO_CAT.get(r.category)
        if cat is None:
            continue
        intl, _ = ctx.intl(r.value, "USA", year)
        out.append({"iso3": "USA", "country": "United States", "category": cat,
                    "coicop": r.category, "monthly_local": r.value / 12, "currency": "USD",
                    "monthly_intl_dollars": intl / 12 if intl else None,
                    "survey_year": year, "price_year": year,
                    "household_type": "married couple only", "area_type": "all areas",
                    "method": "CE mean annual spend / 12", "source": "BLS CE",
                    "estimated": False})
    return out


# Anchored to ONE level of the SHS hierarchy: unanchored matching ("rent" also hit
# "Rented living quarters", "Parking at rented ...") double-counted sub-items.
SHS_MAP = [(r"^food purchased from stores$", "groceries"),
           (r"^food purchased from restaurants$", "restaurants_hotels"),
           (r"^rent$", "rent_actual"),
           (r"^owned living quarters$", "owner_costs_mortgage_tax_maint"),
           (r"^water, fuel and electricity for principal accommodation$", "energy_home"),
           (r"^other accommodation$", "restaurants_hotels"),
           (r"^household operations$", "household_operations"),
           (r"^household furnishings and equipment$", "furnishings_household"),
           (r"^clothing and accessories$", "clothing"),
           (r"^private transportation$", "vehicle_all"),
           (r"^public transportation$", "transport_services"),
           (r"^health care$", "health_out_of_pocket"), (r"^personal care$", "personal_misc"),
           (r"^recreation$", "recreation"), (r"^education$", "education"),
           (r"^reading materials", "recreation"), (r"^games of chance", "recreation"),
           (r"^tobacco products", "alcohol_tobacco"),
           (r"^miscellaneous expenditures$", "personal_misc"),
           (r"^total current consumption$", "TOTAL")]
SHS_COUPLE = r"^couples? without children"
SHS_KIDS = r"^couples? with children"


def _shs_by_cat(long, pattern):
    shs = long[(long["source"] == "StatCan SHS") &
               long["household_type"].astype(str).str.contains(pattern, case=False, na=False,
                                                              regex=True)]
    if shs.empty:
        return None, {}
    year = int(shs["year"].max())
    agg: dict[str, float] = {}
    for r in shs[shs["year"] == year].itertuples():
        cat = next((c for pat, c in SHS_MAP if re.search(pat, str(r.category).strip(), re.I)),
                   None)
        if cat:
            agg[cat] = agg.get(cat, 0.0) + r.value
    return year, agg


def _budget_ca(long, ctx):
    year, agg = _shs_by_cat(long, SHS_COUPLE)
    out = []
    for cat, v in agg.items():
        intl, _ = ctx.intl(v, "CAN", year)
        out.append({"iso3": "CAN", "country": "Canada", "category": cat,
                    "coicop": "SHS", "monthly_local": v / 12, "currency": "CAD",
                    "monthly_intl_dollars": intl / 12 if intl else None, "survey_year": year,
                    "price_year": year, "household_type": "Couples without children",
                    "area_type": "all areas", "method": "SHS mean / 12",
                    "source": "StatCan SHS", "estimated": False})
    return out


UK_FS_CATS = {"CP01": "groceries", "CP02": "alcohol_tobacco", "CP03": "clothing",
              "CP041": "rent_actual", "CP043": "home_maintenance", "CP044": "water_refuse",
              "CP045": "energy_home", "CP05": "furnishings_household",
              "CP06": "health_out_of_pocket", "CP07": "transport_all",
              "CP08": "phone_internet", "CP09": "recreation", "CP10": "education",
              "CP11": "restaurants_hotels", "CP12": "personal_misc",
              "CP13": "other_items_incl_mortgage_interest", "TOTAL": "TOTAL"}


def _budget_uk(long, ctx):
    fs = long[(long["source"].astype(str).str.startswith("ONS Family Spending")) &
              (long["household_type"] == "two adults")]
    out = []
    has_split = (fs["category"] == "CP045").any()
    for r in fs.itertuples():
        cat = UK_FS_CATS.get(r.category)
        if r.category == "CP04" and not has_split:
            cat = "housing_net_fuel_power"
        if cat is None:
            continue
        monthly = r.value * 52 / 12
        f, py, _ = ctx.hicp_factor("GBR", "CP00", int(r.year))  # UK HICP ends 2020 -> ~1
        intl, _ = ctx.intl(monthly * 12, "GBR", int(r.year))
        out.append({"iso3": "GBR", "country": "United Kingdom", "category": cat,
                    "coicop": r.category, "monthly_local": monthly, "currency": "GBP",
                    "monthly_intl_dollars": intl / 12 if intl else None,
                    "survey_year": int(r.year), "price_year": int(r.year),
                    "household_type": "two adults (non-retired)", "area_type": "all areas",
                    "method": "weekly x 52/12" + (f"; {r.method}" if isinstance(r.method, str)
                                                  else ""),
                    "source": r.source, "estimated": bool(r.estimated)})
    ct = ctx.ref("GBR", "council_tax_annual")
    if out and ct is not None:
        out.append({"iso3": "GBR", "country": "United Kingdom", "category": "council_tax",
                    "coicop": "ref", "monthly_local": ct["value"] / 12, "currency": "GBP",
                    "monthly_intl_dollars": None, "survey_year": ct["as_of"],
                    "price_year": ct["as_of"], "household_type": "Band D",
                    "area_type": "England average", "method": "annual / 12",
                    "source": ct["source"], "estimated": False})
    return out


# Countries with no household budget survey in the data: same real consumption as a
# neighbour, re-priced with Eurostat category PPPs. ESTIMATE (quality C).
BUDGET_PROXY = {"CHE": "AUT", "LIE": "AUT", "ISL": "NOR"}


def _budget_proxy(budget: pd.DataFrame, ctx: Ctx) -> list[dict]:
    out = []
    have = set(budget["iso3"]) if not budget.empty else set()
    for iso3, nb in BUDGET_PROXY.items():
        if iso3 in have or nb not in have:
            continue
        src = budget[budget["iso3"] == nb]
        for r in src.to_dict("records"):
            year = int(r["survey_year"]) if str(r["survey_year"]).isdigit() else ctx.latest_year
            code = str(r["coicop"])[:4] if str(r["coicop"]).startswith("CP") else "A01"
            p_nb, p_x = ctx.ppp_category(nb, code, year), ctx.ppp_category(iso3, code, year)
            if iso3 == "LIE" and p_x is None:
                p_x = ctx.ppp_category("CHE", code, year)
            if not p_nb or not p_x:
                continue
            local = r["monthly_local"] / p_nb * p_x
            intl, _ = ctx.intl(local * 12, iso3, ctx.latest_year)
            out.append({**r, "iso3": iso3, "country": COUNTRIES[iso3][2],
                        "monthly_local": local, "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                        "monthly_intl_dollars": intl / 12 if intl else None,
                        "method": f"ESTIMATE: {nb} couple quantities x {iso3}/{nb} PPP "
                                  f"for {code} ({p_x / p_nb:.2f})",
                        "source": f"proxy: {r['source']} ({nb}) + Eurostat PPP",
                        "estimated": True})
    return out


def _mandatory_health(budget: pd.DataFrame, ctx: Ctx) -> list[dict]:
    out = []
    for _, r in ctx.refs[ctx.refs["item"] == "mandatory_health_premium_adult_monthly"].iterrows():
        if budget.empty or r["iso3"] not in set(budget["iso3"]):
            continue
        # Some surveys (e.g. Dutch HBS) already record the basic premium under health
        # insurance: only add the reference premium when the survey line is clearly
        # missing it (Swiss/Liechtenstein proxy budgets come from Austria).
        hi = budget[(budget["iso3"] == r["iso3"]) &
                    (budget["category"] == "insurance_health")]["monthly_local"].sum()
        if hi >= 0.5 * 2 * r["value"] and not str(budget[budget["iso3"] == r["iso3"]]
                                                  ["source"].iloc[0]).startswith("proxy"):
            continue
        out.append({"iso3": r["iso3"], "country": COUNTRIES[r["iso3"]][2],
                    "category": "health_insurance_mandatory", "coicop": "ref",
                    "monthly_local": 2 * r["value"], "currency": str(r["unit"]).split("/")[0],
                    "monthly_intl_dollars": None, "survey_year": r["as_of"],
                    "price_year": r["as_of"], "household_type": "2 adults",
                    "area_type": "national average", "method": "2 x adult premium",
                    "source": r["source"], "estimated": r["quality"] == "C"})
    return out


# ---------- Life events ----------------------------------------------------
def mortgage_payment(principal: float, rate_pct: float, years: int) -> float:
    r = rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def derive_life_events(long: pd.DataFrame, ctx: Ctx, home_m2: float) -> pd.DataFrame:
    out = []

    def add(iso3, event, item, value, unit, year=None, source="", method="", quality=""):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        out.append({"iso3": iso3, "country": COUNTRIES.get(iso3, ("", "", iso3))[2],
                    "event": event, "item": item, "value": value, "unit": unit,
                    "year": year, "source": source, "method": method, "quality": quality})

    ly = ctx.latest_year
    # ---- wedding -------------------------------------------------------------
    us_w = ctx.ref("USA", "average_wedding_cost")
    for iso3 in COUNTRIES:
        r = ctx.ref(iso3, "average_wedding_cost")
        if r is not None and r["iso3"] == iso3:
            add(iso3, "wedding", "average_cost", r["value"], r["unit"], r["as_of"],
                r["source"], "reference (mean)", r["quality"])
        elif us_w is not None:
            # scale the US mean by relative price levels (PPP/FX), in local currency
            plr_us, _ = ctx.wb("price_level_ratio", "USA", ly)
            plr, _ = ctx.wb("price_level_ratio", iso3, ly)
            fx, _ = ctx.wb("exchange_rate", iso3, ly)
            if plr and plr_us and fx:
                add(iso3, "wedding", "average_cost", us_w["value"] * (plr / plr_us) * fx,
                    LOCAL_CURRENCY.get(iso3, "EUR"), ly, "derived from The Knot (US)",
                    "US mean x relative price level x FX: VERY rough", "C")
    # ---- children --------------------------------------------------------------
    ncc = long[(long["measure"] == "childcare_net_cost")]
    for iso3, g in ncc.groupby("iso3"):
        g = latest(g, ["household_type", "statistic", "unit", "category"])
        for r in g.itertuples():
            add(iso3, "children", f"childcare_net_cost [{r.statistic}|{r.category}]",
                r.value, r.unit, r.year, "OECD TaxBEN", r.household_type, "A")
    ce = long[(long["source"] == "BLS CE (via FRED)") & (long["category"] == "TOTALEXP")]
    if not ce.empty:
        y = int(ce["year"].max())
        c = ce[ce["year"] == y].set_index("household_type")["value"]
        base = c.get("married couple only")
        for grp in ("married couple, oldest child under 6", "married couple, oldest child 6-17"):
            if base is not None and grp in c:
                add("USA", "children", f"extra_spending_vs_couple ({grp})",
                    (c[grp] - base) / 12, "USD/month", y, "BLS CE",
                    "household with children minus couple-only (all children, ~1.9 avg)", "B")
    # HBS publishes only "two adults with dependent children" (no 1/2-child split): the
    # per-child figure divides by the EU-average number of children in such households.
    hb = ctx.measure("household_spending_raw")
    hb = hb[hb["statistic"] == "total_by_hhtype"]
    for iso3, g in hb.groupby("iso3"):
        y = int(g["year"].max())
        g, _ = _per_household(g[g["year"] == y])
        if g["unit"].astype(str).str.contains("AE", case=False).all():
            continue  # per-adult-equivalent units cannot give a child's extra cost

        def first(pats):
            lab = _match_hh(g["household_type"], pats)
            return None if lab is None else g[g["household_type"] == lab]["value"].iloc[0]
        base, one, two, anyk = (first(HH_COUPLE), first(HH_1CHILD), first(HH_2CHILD),
                                first(HH_ANYCHILD))
        nac = ctx.nac_per_pps(iso3, y)
        if not (base and nac):
            continue
        f, py, _ = ctx.hicp_factor(iso3, "CP00", y)
        cur = LOCAL_CURRENCY.get(iso3, "EUR") + "/month"
        if one:
            add(iso3, "children", "extra_spending_per_child", (one - base) * nac * f / 12,
                cur, py, "Eurostat HBS", f"HBS {y}: 2 adults+1 child minus 2 adults, HICP", "B")
        elif anyk:
            add(iso3, "children", "extra_spending_per_child",
                (anyk - base) / HBS_AVG_CHILDREN * nac * f / 12, cur, py, "Eurostat HBS",
                f"ESTIMATE: HBS {y} (2 adults with dependent children - 2 adults) / "
                f"{HBS_AVG_CHILDREN} avg children, rolled by HICP", "C")
    fs = long[long["source"].astype(str).str.startswith("ONS Family Spending") &
              (long["category"] == "TOTAL")]
    if not fs.empty:
        t = fs.set_index("household_type")["value"]
        if "two adults" in t and "two adults, one child" in t:
            add("GBR", "children", "extra_spending_per_child",
                (t["two adults, one child"] - t["two adults"]) * 52 / 12, "GBP/month",
                int(fs["year"].iloc[0]), "ONS Family Spending",
                "2 adults+1 child minus 2 non-retired adults (total incl. 'other items')", "B")
    yk, kids = _shs_by_cat(long, SHS_KIDS)
    yc, cpl = _shs_by_cat(long, SHS_COUPLE)
    if kids.get("TOTAL") and cpl.get("TOTAL"):
        add("CAN", "children", "extra_spending_per_child",
            (kids["TOTAL"] - cpl["TOTAL"]) / SHS_AVG_CHILDREN / 12, "CAD/month", yk,
            "StatCan SHS", f"ESTIMATE: (couples with children - without) / {SHS_AVG_CHILDREN} "
            "avg children; current consumption", "C")
    if "USA" in COUNTRIES:
        for r in [x for x in out if x["iso3"] == "USA" and "oldest child under 6" in x["item"]]:
            add("USA", "children", "extra_spending_per_child", r["value"] / CE_AVG_CHILDREN,
                "USD/month", r["year"], "BLS CE",
                f"ESTIMATE: (married couple, oldest child <6 - couple only) / {CE_AVG_CHILDREN}",
                "C")
    # ---- house purchase -----------------------------------------------------------
    hpi = ctx.measure("housing")
    hpi = hpi[hpi["statistic"] == "house_price_index"]
    rates = ctx.measure("housing")
    rates = rates[rates["statistic"].astype(str).str.startswith("mortgage_rate")]
    rate_pref = ["mortgage_rate_new_loans", "mortgage_rate_10y_fixed_median",
                 "mortgage_rate_30y_fixed", "mortgage_rate_2y_fixed_75ltv",
                 "mortgage_rate_5y_posted"]
    prices: dict[str, tuple] = {}
    for iso3 in COUNTRIES:
        price, unit, src, yr, q = None, LOCAL_CURRENCY.get(iso3, "EUR"), "", None, "B"
        m2 = ctx.ref(iso3, "new_dwelling_price_per_m2")
        if iso3 == "USA":
            s_ = ctx.mi("housing", "USA")
            s_ = s_[s_["statistic"] == "median_existing_single_family_price"]
            if not s_.empty:
                s_ = s_.sort_values("period").iloc[-1]
                price, src, yr = s_["value"], "NAR via FRED (median existing single-family)", \
                    s_["period"]
        elif iso3 in ("CAN", "GBR"):
            r = ctx.ref(iso3, "average_home_price")
            if r is not None:
                price, src, yr = r["value"], r["source"] + " (mean)", r["as_of"]
        elif m2 is not None and m2["iso3"] == iso3:
            p = m2["value"] * home_m2
            h = hpi[hpi["iso3"] == iso3].sort_values("year")
            if not h.empty and (h["year"] == 2025).any():
                p *= h["value"].iloc[-1] / h[h["year"] == 2025]["value"].iloc[0]
                yr = int(h["year"].iloc[-1])
            else:
                yr = 2025
            price = ctx.to_local(p, "EUR", iso3, yr) if iso3 not in EURO_MEMBERS_NOW else p
            src = f"Deloitte new-dwelling EUR/m2 x {home_m2:g} m2 x Eurostat HPI"
        if price is not None:
            prices[iso3] = (price, unit, src, yr, q)
    # Countries without a price: median price-to-income ratio of the others (Europe)
    # x local median couple disposable income. ESTIMATE, quality C.
    inc = {}
    hi = ctx.measure("household_income")
    hi = hi[(hi["statistic"] == "median") & hi["unit"].astype(str).str.startswith("NAC") &
            hi["household_type"].astype(str).str.fullmatch("Two adults", case=False)]
    for iso3, g in hi.groupby("iso3"):
        inc[iso3] = g.sort_values("year")["value"].iloc[-1] * 1.5  # equivalised -> couple
    ratios = [prices[c][0] / inc[c] for c in prices if c in inc and c in EUROPE]
    if ratios:
        pti = float(pd.Series(ratios).median())
        for iso3 in EUROPE:
            if iso3 not in prices and iso3 in inc:
                prices[iso3] = (pti * inc[iso3], LOCAL_CURRENCY.get(iso3, "EUR"),
                                f"ESTIMATE: median European price-to-income ratio "
                                f"({pti:.1f}x couple disposable income, {len(ratios)} countries)"
                                " x local EU-SILC median", ctx.latest_year, "C")
    for iso3, (price, unit, src, yr, q) in prices.items():
        add(iso3, "house", "home_price", price, unit, yr, src,
            "national level; suburban resale homes are often cheaper", q)
        dp = ctx.ref(iso3, "down_payment_pct_first_time")
        if iso3 == "CAN":
            dp_amt = 0.05 * min(price, 500000) + 0.10 * max(min(price, 1.5e6) - 500000, 0) \
                if price < 1.5e6 else 0.2 * price
            dp_pct = dp_amt / price * 100
            dp_src = "Canadian minimum down payment rules"
        elif dp is not None:
            dp_pct, dp_src = dp["value"], dp["source"]
        else:
            dp_pct, dp_src = 20, "assumption"
        add(iso3, "house", "down_payment_pct", dp_pct, "%", None, dp_src, "", "B")
        add(iso3, "house", "down_payment_amount", price * dp_pct / 100, unit, yr, dp_src, "", "B")
        tt = ctx.ref(iso3, "transfer_tax_pct")
        if tt is not None and pd.notna(tt["value"]):
            add(iso3, "house", "transfer_tax_pct", tt["value"], "%", tt["as_of"],
                tt["source"], str(tt["note"]), "B")
            add(iso3, "house", "transfer_tax_amount", price * tt["value"] / 100, unit, yr,
                tt["source"], f"{tt['value']}% ({tt['note']})", "B")
    for iso3 in COUNTRIES:
        rr = rates[rates["iso3"] == iso3]
        rate = None
        for st in rate_pref:
            s_ = rr[rr["statistic"] == st].sort_values("period")
            if not s_.empty:
                rate, per, rsrc, rst = float(s_["value"].iloc[-1]), s_["period"].iloc[-1], \
                    s_["source"].iloc[-1], st
                q = "A"
                break
        if rate is None:
            ref = ctx.ref(iso3, "mortgage_rate")
            if ref is not None and ref["iso3"] == iso3:
                rate, per, rsrc, rst, q = float(ref["value"]), ref["as_of"], ref["source"], \
                    "reference", ref["quality"]
        if rate is None:
            continue
        add(iso3, "house", "mortgage_rate", rate, "%", per, rsrc, rst, q)
        if iso3 in prices:
            price, unit = prices[iso3][0], prices[iso3][1]
            dp_pct = next(x["value"] for x in out if x["iso3"] == iso3 and
                          x["item"] == "down_payment_pct")
            for yrs in (25, 30):
                add(iso3, "house", f"mortgage_payment_{yrs}y", mortgage_payment(
                    price * (1 - dp_pct / 100), rate, yrs), unit + "/month", None,
                    "derived", f"annuity on price x (1-down payment) at {rate:.2f}%", "B")
    # ---- rents ---------------------------------------------------------------------
    # EU-SILC total housing cost of TENANTS (tenure=RENT; includes reduced-rent
    # tenants) for TWO-ADULT households, incl. utilities. Ratios for households with
    # children let the simulation scale rent when kids arrive.
    silc = ctx.measure("housing")
    silc = silc[silc["statistic"] == "mean_total_housing_cost"]
    tenant = silc[silc["category"].astype(str).str.contains("tenant|market price", case=False)]
    for iso3, g in tenant.groupby("iso3"):
        y = int(g["year"].max())
        g = g[g["year"] == y]
        hh = g.set_index(g["household_type"].astype(str).str.lower())["value"]
        base = hh.get("two adults")
        label = "two adults"
        if base is None:
            base, label = hh.get("total"), "all households"
        nac = ctx.nac_per_pps(iso3, y)
        if base is None or not nac or "PPS" not in str(g["unit"].iloc[0]):
            continue
        add(iso3, "rent", "tenant_total_housing_cost", base * nac,
            LOCAL_CURRENCY.get(iso3, "EUR") + "/month", y, "Eurostat EU-SILC",
            f"MEAN total housing cost of tenants ({label}) incl. utilities; "
            "includes reduced-rent tenants", "A")
        for k, lab in (("two adults with one dependent child", "rent_factor_one_child"),
                       ("two adults with two dependent children", "rent_factor_two_children")):
            if k in hh and label == "two adults":
                add(iso3, "rent", lab, hh[k] / base, "ratio", y, "Eurostat EU-SILC",
                    f"tenant housing cost, {k} / two adults", "A")
    acs = ctx.mi("housing", "USA")
    acs = acs[acs["statistic"] == "median_gross_rent"].sort_values("year")
    if not acs.empty:
        add("USA", "rent", "median_gross_rent", acs["value"].iloc[-1], "USD/month",
            int(acs["year"].iloc[-1]), "Census ACS", "contract rent + utilities", "A")
    else:
        r = ctx.ref("USA", "median_gross_rent")
        if r is not None:
            add("USA", "rent", "median_gross_rent", r["value"], "USD/month", r["as_of"],
                r["source"], "contract rent + utilities (reference fallback)", r["quality"])
    cmhc = ctx.mi("housing", "CAN")
    cmhc = cmhc[cmhc["statistic"] == "average_rent_2br"]
    if not cmhc.empty:
        y = int(cmhc["year"].max())
        add("CAN", "rent", "median_of_centres_avg_rent_2br",
            cmhc[cmhc["year"] == y]["value"].median(), "CAD/month", y, "CMHC",
            "median across all centres 10k+ (unweighted; smaller centres count equally)", "B")
    uk = ctx.ref("GBR", "average_monthly_rent")
    if uk is not None:
        add("GBR", "rent", "average_private_rent", uk["value"], "GBP/month", uk["as_of"],
            uk["source"], "mean", "A")
    # ---- emergencies -------------------------------------------------------------------
    em = long[long["measure"] == "emergency"]
    for iso3, g in em.groupby("iso3"):
        for stat in ("share_cannot_cover_unexpected_expense", "unexpected_expense_size"):
            s = g[g["statistic"] == stat]
            if stat.startswith("share"):
                pick = s[s["household_type"].astype(str).str.contains(
                    "two adults", case=False) & ~s["household_type"].astype(str).str.contains(
                    "child|65 years or over|at least one", case=False)]
                s = pick if not pick.empty else s[s["household_type"].astype(str)
                                                  .str.contains("total", case=False)]
            if s.empty:
                continue
            r = s.sort_values("year").iloc[-1]
            add(iso3, "emergency", stat, r["value"], r["unit"], int(r["year"]), r["source"],
                str(r["household_type"]), "A")
        nrr = g[g["statistic"].astype(str).str.startswith("net_replacement_rate")]
        for (stat, hh), s in nrr.groupby(["statistic", "household_type"]):
            r = s.sort_values("year").iloc[-1]
            add(iso3, "emergency", f"{stat} ({hh})", r["value"], r["unit"], int(r["year"]),
                "OECD TaxBEN", "job loss: household net income kept", "A")
    hl = long[(long["measure"] == "health") & long["statistic"].isin(
        ["share_oop_over_10pct_income", "share_oop_over_25pct_income",
         "out_of_pocket_per_capita"])]
    for (iso3, stat), s in hl.groupby(["iso3", "statistic"]):
        r = s.sort_values("year").iloc[-1]
        add(iso3, "emergency", stat, r["value"], r["unit"], int(r["year"]), "WHO/World Bank",
            "catastrophic health spending risk", "A")
    for _, r in ctx.refs[ctx.refs["topic"] == "emergency"].iterrows():
        add(r["iso3"], "emergency", r["item"], r["value"], r["unit"], r["as_of"], r["source"],
            r["note"], r["quality"])
    # ---- income of the households behind the survey budget (all years; the app picks
    # the one matching the HBS survey year and scales the budget with the simulated
    # couple's income relative to it)
    si = long[(long["measure"] == "household_income") &
              (long["series"].astype(str) == "ilc_di04:A2_2LT65") &
              long["unit"].astype(str).str.startswith("NAC")]
    for (iso3, year), s in si.groupby(["iso3", "year"]):
        add(iso3, "budget", "silc_median_equivalised_A2_lt65", float(s["value"].iloc[0]),
            LOCAL_CURRENCY.get(iso3, "EUR") + "/year (equivalised)", int(year),
            "Eurostat EU-SILC ilc_di04 (two adults younger than 65)",
            "median equivalised net income; SILC year = income year + 1", "A")
    # housing costs of the same (two-adult, all tenures) households: the budget scaling
    # works on income AFTER housing, so renters are not given owners' spending power
    hc = long[(long["statistic"] == "mean_total_housing_cost") &
              (long["category"].astype(str) == "Total") &
              (long["household_type"].astype(str) == "Two adults")]
    for (iso3, year), s in hc.groupby(["iso3", "year"]):
        nac = ctx.nac_per_pps(iso3, int(year))
        if nac:
            add(iso3, "budget", "silc_A2_mean_housing_cost", float(s["value"].iloc[0]) * nac,
                LOCAL_CURRENCY.get(iso3, "EUR") + "/month", int(year),
                "Eurostat EU-SILC ilc_mded03 (two adults, all tenures)",
                "mean total housing cost incl. utilities, net of allowances; PPS x PPP", "A")
    return pd.DataFrame(out)


# ---------- Mobility summary -------------------------------------------------
def derive_mobility(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    out = []
    e = long[(long["measure"] == "earnings_by_age") & (long["statistic"] == "median") &
             ~long["estimated"].fillna(False).astype(bool)]
    # (1) cross-sectional age-earnings profile: each band / earliest band, same year
    for (iso3, src, unit, y), g in e.groupby(["iso3", "source", "unit", "year"]):
        g = g[g["career_stage"].isin(["early", "mid", "late"])]
        if g["career_stage"].nunique() < 2:
            continue
        early = g[g["career_stage"] == "early"]["value"].max()
        for r in g.itertuples():
            out.append({"iso3": iso3, "metric": "age_profile_vs_youngest",
                        "detail": f"{r.age_band} / youngest band", "year": y,
                        "value": r.value / early if early else None, "source": src,
                        "note": "cross-section: today's older workers vs today's young"})
    # (2) pseudo-cohort real growth across SES waves: <30 in wave t -> 30-39 in t+8
    ses = e[(e["source"] == "Eurostat SES") & (e["unit"] == "EUR/month")]
    for iso3, g in ses.groupby("iso3"):
        for y in sorted(g["year"].unique()):
            a = g[(g["year"] == y) & (g["age_band"] == "Y_LT30")]
            b = g[(g["year"] == y + 8) & (g["age_band"] == "Y30-39")]
            if a.empty or b.empty:
                continue
            va = ctx.to_local(a["value"].iloc[0], "EUR", iso3, int(y))
            vb = ctx.to_local(b["value"].iloc[0], "EUR", iso3, int(y) + 8)
            c0, c1 = ctx.cpi(iso3, int(y)), ctx.cpi(iso3, int(y) + 8)
            if va and vb and c0 and c1:
                g_real = (vb / c1) / (va / c0)
                out.append({"iso3": iso3, "metric": "pseudo_cohort_real_growth_8y",
                            "detail": f"median <30 in {y} -> median 30-39 in {y + 8}",
                            "year": int(y) + 8, "value": g_real,
                            "source": "Eurostat SES + World Bank CPI",
                            "note": "x-fold real growth of a cohort's median; "
                                    f"annualised {(g_real ** (1 / 8) - 1) * 100:.1f}%/yr"})
    # (3) Atlanta Fed: stayer vs switcher premium (annual means)
    w = long[long["measure"] == "wage_growth_individual"]
    if not w.empty:
        wa = w.groupby(["statistic", "year"])["value"].mean().unstack(0)
        for y, r in wa.iterrows():
            for col in wa.columns:
                out.append({"iso3": "USA", "metric": "wage_growth_same_person",
                            "detail": col, "year": int(y), "value": r[col],
                            "source": "Atlanta Fed WGT", "note": "% per year, median"})
    # (4) EU-SILC: share moving up >= 1 pay decile per year
    t = long[long["measure"] == "income_transitions"]
    t = t[t["category"].astype(str).str.contains("total", case=False, na=False)]
    for (iso3, stat), g in t.groupby(["iso3", "statistic"]):
        r = g.sort_values("year").iloc[-1]
        out.append({"iso3": iso3, "metric": "pay_decile_transition", "detail": stat,
                    "year": int(r["year"]), "value": r["value"], "source": r["source"],
                    "note": "% of employees, year to year"})
    return pd.DataFrame(out)


# ---------- Expat tax comparison ------------------------------------------
def derive_expat_tax(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    tw = long[(long["source"] == "OECD Taxing Wages")]
    out = []
    if not tw.empty:
        # average personal tax rate measures: pick codes that look like average tax/wedge
        keep = tw[tw["statistic"].astype(str).str.contains("AV_|NPAV|TW", regex=True)]
        keep = latest(keep, ["iso3", "statistic", "household_type"])
        us = keep[keep["iso3"] == "USA"].set_index(["statistic", "household_type"])["value"]
        for r in keep.itertuples():
            usv = us.get((r.statistic, r.household_type))
            out.append({"iso3": r.iso3, "country": COUNTRIES.get(r.iso3, ("", "", ""))[2],
                        "measure": r.statistic, "household_type": r.household_type,
                        "year": r.year, "local_value": r.value, "usa_value": usv,
                        "local_minus_usa": (r.value - usv) if usv is not None else None,
                        "flag": ("local burden below US - residual US tax possible "
                                 "(FEIE/FTC may not fully offset)") if usv is not None and
                        r.value < usv else ""})
    df = pd.DataFrame(out)
    refs = ctx.refs[ctx.refs["topic"] == "expat_tax"].copy()
    tot = refs[refs["item"] == "us_totalization_agreement"]["iso3"].tolist()
    if not df.empty:
        df["us_totalization_agreement"] = df["iso3"].isin(tot)
    return df


# ---------- Simulation inputs ------------------------------------------------
# Compact tables the simulator/UI read, so it never has to load econ_long.
Z = {"p10": -1.2816, "p25": -0.6745, "p75": 0.6745, "p90": 1.2816}
EARN_PREF = {"GBR": "ONS ASHE", "USA": "BLS CPS (via FRED)", "CAN": "StatCan LFS"}


def age_span(band: str) -> tuple[float, float] | None:
    """(low, high) working ages covered by an earnings band; None for all-ages totals.
    Open-ended bands are capped at 64; '<30' starts at 20 (full-time workers)."""
    b = str(band).strip()
    if b.upper() == "TOTAL" or b.lower() == "total":
        return None
    if m := re.fullmatch(r"Y(\d{2})-(\d{2})", b) or re.fullmatch(r"(\d{2})-(\d{2})", b) or \
            re.fullmatch(r"(\d{2}) to (\d{2}) years", b):
        return float(m.group(1)), float(m.group(2))
    if m := re.fullmatch(r"Y_LT(\d{2})", b):
        return 20.0, float(m.group(1)) - 1
    if m := re.fullmatch(r"Y_GE(\d{2})", b) or re.fullmatch(r"(\d{2})\+", b) or \
            re.fullmatch(r"(\d{2}) years and over", b):
        lo = float(m.group(1))
        return (None if lo <= 16 else (lo, max(lo + 4, 64.0)))
    return None


def age_mid(band: str) -> float | None:
    s = age_span(band)
    return None if s is None else (s[0] + s[1]) / 2


def derive_earnings_profile(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Annual gross full-time earnings by age band and percentile (p10..p90), local
    currency, latest year, one source per country. Missing percentiles are ESTIMATED:
    SES has p10/p90 -> p25/p75 by log-normal interpolation; CPS has p25/p75 only for
    all ages -> that ratio applied to each age median; LFS (Canada) has medians only ->
    US ratios. LIE has no survey -> Swiss profile (estimate)."""
    e = ctx.measure("earnings_by_age")
    e = e[~e["unit"].astype(str).str.startswith("PPS")]
    out = []
    for iso3 in COUNTRIES:
        g = e[e["iso3"] == iso3]
        pref = EARN_PREF.get(iso3)
        if pref:
            g = g[g["source"] == pref]
        else:
            g = g[g["source"].astype(str).str.startswith("Eurostat SES")]
        if g.empty:
            continue
        y = int(g["year"].max())
        full = g
        g = g[g["year"] == y]
        src = g["source"].iloc[0]
        est_all = bool(g["estimated"].any())
        if not pref and g["age_band"].map(age_span).notna().sum() == 0:
            # SES 2022 (and its nowcast) has only the TOTAL for most countries in
            # earn_ses_monthly: take the AGE SHAPE (band / total, per statistic) from the
            # latest wave with >= 4 age bands and apply it to the latest level.
            ses = full[full["source"] == "Eurostat SES"]
            def n_bands(w):
                b = ses[ses["year"] == w]["age_band"]
                return b[b.map(age_span).notna()].nunique()
            waves = [w for w in sorted(ses["year"].unique(), reverse=True) if n_bands(w) >= 4]
            if waves:
                w = int(waves[0])
                sw = ses[ses["year"] == w]
                tot_w = sw[sw["age_band"] == "TOTAL"].set_index(["statistic", "unit"])["value"]
                tot_y = g[g["age_band"] == "TOTAL"].set_index(["statistic", "unit"])["value"]
                rows = []
                for r in sw[sw["age_band"] != "TOTAL"].itertuples():
                    k = (r.statistic, r.unit)
                    if k in tot_w.index and k in tot_y.index and tot_w[k]:
                        rows.append({**{c: getattr(r, c) for c in g.columns if hasattr(r, c)},
                                     "value": r.value / tot_w[k] * tot_y[k], "year": y,
                                     "estimated": True})
                if rows:
                    g = pd.concat([g, pd.DataFrame(rows)], ignore_index=True)
                    src = f"{src}; age shape from SES {w}"
                    est_all = True
        for band, gb in g.groupby("age_band"):
            st = {r.statistic: r for r in gb.itertuples()}
            if "median" not in st:
                continue
            unit = str(st["median"].unit)
            cur, per = unit.split("/")
            conv = lambda v: ctx.to_local(v * PER_YEAR[per], unit, iso3, y)  # noqa: E731
            med = conv(st["median"].value)
            if med is None:
                continue
            vals = {"median": (med, est_all, "")}
            for p in ("p10", "p25", "p75", "p90"):
                if p in st:
                    vals[p] = (conv(st[p].value), est_all, "")
            if "p25" not in vals and "p10" in vals:
                sig = (math.log(med) - math.log(vals["p10"][0])) / -Z["p10"]
                vals["p25"] = (med * math.exp(Z["p25"] * sig), True,
                               "log-normal interpolation from p10/median")
            if "p75" not in vals and "p90" in vals:
                sig = (math.log(vals["p90"][0]) - math.log(med)) / Z["p90"]
                vals["p75"] = (med * math.exp(Z["p75"] * sig), True,
                               "log-normal interpolation from median/p90")
            bf = _ses_bonus_factor(ctx, iso3) if not pref else {}
            for p, (v, est, how) in vals.items():
                if p in bf or "median" in bf:
                    k = bf.get(p, bf.get("median"))
                    v = v * k[0]
                    how = (how + "; " if how else f"{per}ly x {PER_YEAR[per]}; ") + \
                        f"x {k[0]:.3f} annual/(Oct monthly x 12) bonus factor, SES {k[1]}"
                span = age_span(band)
                out.append({"iso3": iso3, "age_band": band, "age_mid": age_mid(band),
                            "age_width": None if span is None else span[1] - span[0],
                            "statistic": p, "annual_gross_local": v,
                            "currency": LOCAL_CURRENCY.get(iso3, "EUR"), "year": y,
                            "source": src, "estimated": est, "method": how or
                            f"{per}ly x {PER_YEAR[per]}" + (" (SES nowcast)" if est_all else ""),
                            "quality": "C" if est and how else ("B" if est else "A")})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    # fill p25/p75 where only medians exist, using an all-ages ratio
    ratio_src = {}
    for iso3, g in df.groupby("iso3"):
        tot = g[g["age_mid"].isna()].set_index("statistic")["annual_gross_local"]
        if {"p25", "median", "p75"} <= set(tot.index):
            ratio_src[iso3] = (tot["p25"] / tot["median"], tot["p75"] / tot["median"])
    extra = []
    for iso3, g in df.groupby("iso3"):
        r = ratio_src.get(iso3) or ratio_src.get("USA")
        if r is None:
            continue
        donor = iso3 if iso3 in ratio_src else "USA"
        for band, gb in g.groupby("age_band"):
            have = set(gb["statistic"])
            med = gb[gb["statistic"] == "median"].iloc[0]
            for p, k in (("p25", 0), ("p75", 1)):
                if p not in have:
                    extra.append({**med.to_dict(), "statistic": p,
                                  "annual_gross_local": med["annual_gross_local"] * r[k],
                                  "estimated": True, "quality": "C",
                                  "method": f"ESTIMATE: age median x all-ages {p}/median "
                                            f"ratio of {donor} ({r[k]:.2f})"})
    df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    if "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        lie = df[df["iso3"] == "CHE"].copy()
        lie["iso3"], lie["estimated"], lie["quality"] = "LIE", True, "C"
        lie["method"] = "ESTIMATE: Swiss earnings profile used for Liechtenstein"
        lie["currency"] = "CHF"
        df = pd.concat([df, lie], ignore_index=True)
    df["country"] = df["iso3"].map(lambda c: COUNTRIES[c][2])
    return df


def _ses_bonus_factor(ctx: Ctx, iso3: str) -> dict[str, tuple[float, int]]:
    """statistic -> (annual / (monthly x 12), wave) for all ages, latest common wave."""
    a = ctx.mi("earnings_annual_ses", iso3)
    m = ctx.mi("earnings_by_age", iso3)
    m = m[(m["source"] == "Eurostat SES") & (m["unit"] == "EUR/month") &
          (m["age_band"] == "TOTAL")]
    a = a[a["age_band"] == "TOTAL"]
    out = {}
    for stat in ("median", "p10", "p90"):
        av = {int(r.year): r.value for r in a[a["statistic"] == stat].itertuples()}
        mv = {int(r.year): r.value for r in m[m["statistic"] == stat].itertuples()}
        common = sorted(set(av) & set(mv))
        if common:
            w = common[-1]
            k = av[w] / (mv[w] * 12)
            if 0.9 < k < 1.6:  # guard against unit / coverage mismatches
                out[stat] = (k, w)
    if "median" in out:  # interpolated percentiles use the neighbouring factor
        out.setdefault("p25", ((out["median"][0] + out.get("p10", out["median"])[0]) / 2,
                               out["median"][1]))
        out.setdefault("p75", ((out["median"][0] + out.get("p90", out["median"])[0]) / 2,
                               out["median"][1]))
    return out


ECASE_RX = re.compile(r"P1_NCH_AW(\d+)$")


def derive_tax_curve(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Average income tax and employee social-security rates for a SINGLE person
    without children at several multiples of the national average wage (AW), plus
    the marginal rate at the top point (for extrapolation) and the net cash value of
    two children for a couple. Eurostat net earnings (6 points, 50-167% AW) first;
    OECD Taxing Wages (67/100/167% AW) for countries Eurostat lacks."""
    rows = []
    ne = ctx.measure("net_income_tax_model")
    eu = ne[ne["source"] == "Eurostat net earnings"]
    tw = ne[ne["source"] == "OECD Taxing Wages"]
    for iso3 in COUNTRIES:
        g = eu[(eu["iso3"] == iso3)]
        pts = []
        if not g.empty:
            nac = g[g["unit"].astype(str).str.startswith("NAC")]
            g = nac if not nac.empty else g
            y = int(g["year"].max())
            g = g[g["year"] == y]
            for case, gc in g.groupby("series"):
                m = ECASE_RX.search(str(case))
                if not m:
                    continue
                v = gc.set_index("statistic")["value"]
                if not v.get("GRS"):
                    continue
                grs = ctx.to_local(v["GRS"], str(gc["unit"].iloc[0]), iso3, y)
                pts.append({"aw_multiple": int(m.group(1)) / 100, "gross_local": grs,
                            "income_tax_rate": v.get("TAX", 0) / v["GRS"],
                            "employee_ssc_rate": v.get("SOC", 0) / v["GRS"],
                            "year": y, "source": "Eurostat net earnings (OECD-EC model)"})
        t = tw[(tw["iso3"] == iso3) & tw["household_type"].astype(str).str.startswith("S_C0")]
        stale = bool(pts) and not t.empty and pts[0]["year"] < int(t["year"].max()) - 1
        if len(pts) < 3 or stale:  # e.g. UK: Eurostat stops in 2019, OECD has 2025
            if not t.empty:
                y = int(t["year"].max())
                t = t[t["year"] == y]
                pts = []
                for hh, gt in t.groupby("household_type"):
                    lvl = re.search(r"AW(\d+)", hh)
                    v = {(r.statistic, r.unit): r.value for r in gt.itertuples()}
                    gross = v.get(("GEBT", "XDC"))
                    if not lvl or gross is None:
                        continue
                    pts.append({"aw_multiple": int(lvl.group(1)) / 100, "gross_local": gross,
                                "income_tax_rate": v.get(("AV_ITR", "PT_WG_EARN_G"), 0) / 100,
                                "employee_ssc_rate": v.get(("AV_R_EMPEE_SSC", "PT_WG_EARN_G"),
                                                           0) / 100,
                                "year": y, "source": "OECD Taxing Wages"})
        if not pts:
            continue
        aw = next((p["gross_local"] for p in pts if p["aw_multiple"] == 1.0), None) or \
            pts[0]["gross_local"] / pts[0]["aw_multiple"]
        # marginal net personal tax rate at 167% AW (OECD) for extrapolation
        t = tw[(tw["iso3"] == iso3) & (tw["statistic"] == "NPMTR_PE") &
               tw["household_type"].astype(str).str.startswith("S_C0|AW167")]
        mtr = float(t.sort_values("year")["value"].iloc[-1]) / 100 if not t.empty else None
        # value of two children for a two-earner couple (100% + 67% AW): net income
        # with 2 kids minus without, as a share of AW (cash benefits + tax credits)
        c2 = tw[(tw["iso3"] == iso3) & (tw["statistic"] == "NIAT") & (tw["unit"] == "XDC")]
        kid = None
        if not c2.empty:
            yy = int(c2["year"].max())
            c2 = c2[c2["year"] == yy].set_index("household_type")["value"]
            a, b = c2.get("C_C2|AW100+AW67"), c2.get("C_C0|AW100+AW67")
            if a is not None and b is not None:
                kid = (a - b) / 2 / aw
        if kid is None:  # Eurostat: family allowances, couple 100%+67% with 2 children
            fam = eu[(eu["iso3"] == iso3) & (eu["statistic"] == "FAM") &
                     eu["series"].astype(str).str.endswith("CPL_CH2_AW100_67")]
            if not fam.empty:
                fam = fam.sort_values("year").iloc[-1]
                kid = ctx.to_local(fam["value"], fam["unit"], iso3, int(fam["year"])) / 2 / aw
        for p in sorted(pts, key=lambda p: p["aw_multiple"]):
            rows.append({"iso3": iso3, **p, "avg_wage_local": aw,
                         "total_rate": p["income_tax_rate"] + p["employee_ssc_rate"],
                         "marginal_rate_top": mtr, "child_net_benefit_per_child_frac_aw": kid,
                         "currency": LOCAL_CURRENCY.get(iso3, "EUR"), "estimated": False,
                         "quality": "A"})
    df = pd.DataFrame(rows)
    if not df.empty and "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        lie = df[df["iso3"] == "CHE"].copy()
        lie["iso3"], lie["estimated"], lie["quality"] = "LIE", True, "C"
        lie["source"] = "ESTIMATE: Swiss tax curve used for Liechtenstein"
        df = pd.concat([df, lie], ignore_index=True)
    return df


def derive_fx_ppp(ctx: Ctx) -> pd.DataFrame:
    rows = []
    keys = sorted({k for t in ctx._wb.values() for k in t})
    for iso3, y in keys:
        if iso3 not in COUNTRIES and iso3 != "EMU":
            continue
        rows.append({"iso3": iso3, "year": y,
                     "lcu_per_usd": ctx._wb["exchange_rate"].get((iso3, y)),
                     "lcu_per_intl_dollar": ctx._wb["ppp_private_consumption"].get((iso3, y)),
                     "price_level_ratio": ctx._wb["price_level_ratio"].get((iso3, y)),
                     "cpi_index": ctx._wb["cpi_index"].get((iso3, y)),
                     "source": "World Bank WDI (PA.NUS.FCRF, PA.NUS.PRVT.PP, FP.CPI.TOTL)"})
    return pd.DataFrame(rows)


CONTEXT = [  # (indicator, measure, statistic regex, age_band regex or None)
    ("unemployment_rate", "unemployment_rate", "rate", r"^(Y15-74|Y_GE15)$"),
    ("youth_unemployment_rate", "unemployment_rate", "rate", r"^(Y15-24|Y15T24)$"),
    ("job_vacancy_rate", "job_vacancy_rate", "rate", None),
    ("saving_rate_net", "saving_rate_net", "rate", None),
    ("saving_rate_gross", "saving_rate_gross", "rate", None),
    ("world_uncertainty_index", "uncertainty", "^wui", None),
    ("political_stability", "uncertainty", "political_stability", None),
    ("house_price_index", "housing", "house_price_index", None),
]


def derive_context(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Small annual panel for the UI context charts."""
    parts = []
    for ind, m, stat, age in CONTEXT:
        d = ctx.measure(m)
        d = d[d["statistic"].astype(str).str.contains(stat, regex=True)]
        if age:
            d = d[d["age_band"].astype(str).str.contains(age, regex=True)]
        if d.empty:
            continue
        g = d.groupby(["iso3", "year"], as_index=False).agg(
            value=("value", "mean"), unit=("unit", "first"), source=("source", "first"),
            estimated=("estimated", "any"), n=("value", "size"))
        parts.append(g.assign(indicator=ind))
    cpi = pd.DataFrame([{"iso3": c, "year": y, "value": v} for (c, y), v in
                        ctx._wb["cpi_index"].items()]).sort_values(["iso3", "year"])
    if not cpi.empty:
        cpi["value"] = cpi.groupby("iso3")["value"].pct_change() * 100
        parts.append(cpi.dropna().assign(indicator="inflation_cpi", unit="% y/y",
                                         source="World Bank WDI", estimated=False, n=1))
    df = pd.concat(parts, ignore_index=True)
    return df[df["iso3"].isin(COUNTRIES)]


# ---------- Job market by age and education ----------------------------------------
LM_BANDS = ["20-24", "25-29", "30-34", "25-64"]
LM_EDU = ["all", "below_upper_secondary", "upper_secondary", "short_tertiary", "bachelor",
          "master_plus"]
TERTIARY_SUB = {"short_tertiary", "bachelor", "master_plus"}
LM_SERIES = {"lfsa_urgaed", "lfsi_sla_a", "lfsa_sup_edu", "lfsa_eoqgan", "DF_LSO_NEAC_UNEMP",
             "DF_INVPT_I", "14-10-0077-01", "CPS basic monthly", "DF_IALFS_UNE_M",
             "DF_LSO_NEAC_EMP", "DF_LSO_NEAC_DISTR_EA", "DF_LSO_EARN_REL_UPPER", "ONS LF8B"}
PC_ELF = "% of extended labour force"
# Functional unemployment = unemployed + part-timers who want and can work more hours +
# people who want a job and could start but are not searching (U-6-like). People who are
# searching but cannot start (Eurostat SEEK_NAVL: mostly students) are left out, as in U-6.
SLACK_EXTRA = ["uemp_pt", "nseek_avl"]


class _LM:
    """(iso3, series, statistic, age, education, unit) -> {year: value} lookups."""

    def __init__(self, long: pd.DataFrame):
        d = long[long["series"].isin(LM_SERIES)].dropna(subset=["year", "value"])
        self.s: dict[tuple, dict[str, dict[int, float]]] = {}
        self.meta: dict[tuple, tuple[bool, str]] = {}
        txt = lambda x, d="all": d if pd.isna(x) or str(x) in ("", "nan") else str(x)  # noqa
        for r in d.itertuples(index=False):
            k = (r.iso3, r.series, txt(r.statistic), txt(r.age_band), txt(r.category))
            self.s.setdefault(k, {}).setdefault(txt(r.unit, ""), {})[int(r.year)] = \
                float(r.value)
            self.meta[k + (int(r.year),)] = (bool(r.estimated),
                                            "" if pd.isna(r.method) else str(r.method))

    def get(self, iso, series, stat, age, edu="all", year=None, back=0, unit=None):
        for u, v in self.s.get((iso, series, stat, age, edu), {}).items():
            if unit is not None and u != unit:
                continue
            ys = [y for y in v if year is None or year - back <= y <= year]
            if ys:
                y = max(ys)
                return v[y], y
        return None, None

    def years(self, iso, series, stat, age, edu="all"):
        ys = set()
        for v in self.s.get((iso, series, stat, age, edu), {}).values():
            ys |= set(v)
        return sorted(ys)


def _ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _sup_edu(lm: _LM, src: str, edu: str, Y: int) -> list[float] | None:
    """[part-timers wanting more hours, available but not seeking] for ages 25-74 and an
    education level, % of its extended labour force (Eurostat publishes the components as
    counts only; extended labour force = slack count / slack %)."""
    ths = [lm.get(src, "lfsa_sup_edu", s, "25-74", edu, Y, unit="thousand persons")[0]
           for s in SLACK_EXTRA + ["slack"]]
    pc = lm.get(src, "lfsa_sup_edu", "slack", "25-74", edu, Y, unit=PC_ELF)[0]
    if ths[-1] is None or not pc or all(t is None for t in ths[:-1]):
        return None
    elf = ths[-1] / pc * 100
    return [100 * (t or 0.0) / elf for t in ths[:-1]]


def _lm_europe(lm: _LM, iso: str, src: str, Y: int, band: str, edu: str) -> dict:
    """One cell for a Eurostat country (src = the country whose data is used). Returns
    {'unemployment': (v, est, note), 'extra': (slack beyond unemployment, of which hours,
    est, note), 'overqualification': (...)}; 20-24 slack is filled in afterwards."""
    out, notes, est = {}, [], src != iso
    if src != iso:
        notes.append(f"proxy: {src} data")
    e = edu if edu in ("all", "below_upper_secondary", "upper_secondary") else "tertiary"
    u, _ = lm.get(src, "lfsa_urgaed", "rate", band, e, Y)
    if u is None:  # small sample: band rate x the education gap at 25-64
        r = _ratio(lm.get(src, "lfsa_urgaed", "rate", "25-64", e, Y)[0],
                   lm.get(src, "lfsa_urgaed", "rate", "25-64", "all", Y)[0])
        ua, _ = lm.get(src, "lfsa_urgaed", "rate", band, "all", Y)
        if r is None or ua is None:
            return {}
        u, est = ua * r, True
        notes.append(f"{band} rate x {e.replace('_', ' ')} gap at 25-64 (cell not published)")
    if edu in TERTIARY_SUB:
        r = _ratio(lm.get(src, "DF_LSO_NEAC_UNEMP", "rate", "25-34", edu, Y, back=4)[0],
                   lm.get(src, "DF_LSO_NEAC_UNEMP", "rate", "25-34", "tertiary", Y, back=4)[0])
        if r is not None:
            u, est = u * r, True
            notes.append(f"x{r:.2f} OECD EAG ({edu.replace('_', ' ')} vs all tertiary, 25-34)")
        else:
            notes.append("all tertiary (no split by degree)")
    out["unemployment"] = (u, est, "; ".join(notes))
    if band != "20-24":
        x, xn = None, ""
        if band == "25-29":  # 25-29 = (15-29) - (15-24), in persons
            parts = {}
            for a in ("15-29", "15-24"):
                ths = [lm.get(src, "lfsi_sla_a", s, a, "all", Y, unit="thousand persons")[0]
                       for s in SLACK_EXTRA + ["une"]]
                pc = lm.get(src, "lfsi_sla_a", "une", a, "all", Y, unit=PC_ELF)[0]
                if ths[0] is None or ths[-1] is None or not pc:
                    parts = None
                    break
                parts[a] = ([t or 0.0 for t in ths], ths[-1] / pc * 100)
            if parts and parts["15-29"][1] > parts["15-24"][1]:
                elf = parts["15-29"][1] - parts["15-24"][1]
                x = {s: 100 * (parts["15-29"][0][i] - parts["15-24"][0][i]) / elf
                     for i, s in enumerate(SLACK_EXTRA)}
                xn = "ages 25-29 (15-29 minus 15-24)"
        if x is None:
            x = {s: lm.get(src, "lfsi_sla_a", s, "25-54", "all", Y, unit=PC_ELF)[0]
                 for s in SLACK_EXTRA}
            xn = "ages 25-54"
            if x["uemp_pt"] is None and x["nseek_avl"] is None:
                x = None
        if x is not None:
            x = {k: max(v or 0.0, 0.0) for k, v in x.items()}
            r_all = r_pt = 1.0
            if e != "all":
                xe, x0 = _sup_edu(lm, src, e, Y), _sup_edu(lm, src, "all", Y)
                if xe and x0:
                    r_all = _ratio(sum(xe), sum(x0)) or 1.0
                    r_pt = _ratio(xe[0], x0[0]) or r_all
                    xn += f", x{r_all:.2f} {e.replace('_', ' ')} gap at 25-74"
                else:
                    xn += " (no education split)"
            out["extra"] = (sum(x.values()) * r_all, x["uemp_pt"] * r_pt, est or e != "all",
                            f"Eurostat slack ({xn})")
    if e == "tertiary":
        oa = {"20-24": "20-34", "25-29": "25-34", "30-34": "25-34", "25-64": "20-64"}[band]
        o, _ = lm.get(src, "lfsa_eoqgan", "rate", oa, "tertiary", Y)
        if o is not None:
            out["overqualification"] = (o, src != iso, f"Eurostat, ages {oa}, all tertiary")
    return out


def _lm_us(lm: _LM, Y: int, band: str, edu: str) -> dict:
    out = {}

    def g(stat, b=band, e=edu):
        return lm.get("USA", "CPS basic monthly", stat, b, e, Y)[0]
    n = g("n_lf") or 0
    est, method = lm.meta.get(("USA", "CPS basic monthly", "unemployment", band, edu, Y),
                              (False, ""))
    thin = n < 150 and edu != "all"

    def thin_fix(stat):  # band rate x education gap at 25-64
        r = _ratio(g(stat, "25-64", edu), g(stat, "25-64", "all"))
        b = g(stat, band, "all")
        return b * r if r is not None and b is not None else None
    u = thin_fix("unemployment") if thin else g("unemployment")
    note = method + (f"; {band} rate x education gap at 25-64 (only {int(n)} sample "
                     "members)" if thin else "")
    if u is None:
        return out
    out["unemployment"] = (u, est or thin, note)
    if band != "20-24":
        x = thin_fix("slack_extra") if thin else g("slack_extra")
        h = thin_fix("hours_underemployment") if thin else g("hours_underemployment")
        if x is not None and h is not None:
            out["extra"] = (x, h, est or thin, "CPS: part-timers wanting full-time + "
                            "marginally attached")
    if edu in TERTIARY_SUB:
        o = g("overqualification", "25-34") if thin else g("overqualification")
        if o is not None:
            out["overqualification"] = (o, est or thin, method + ("; ages 25-34" if thin
                                                                  else ""))
    return out


def _lm_ca_uk(lm: _LM, iso: str, Y: int, band: str, edu: str) -> dict:
    """Canada / UK: OECD EAG by education (25-34, 25-64) and OECD youth unemployment;
    slack = OECD involuntary part-time + StatCan R6-R4 (Canada) / ONS discouraged (UK)."""
    out, notes, est = {}, [], False
    eag = lambda a, e: lm.get(iso, "DF_LSO_NEAC_UNEMP", "rate", a, e, Y, back=2)[0]  # noqa
    a = "25-64" if band == "25-64" else "25-34"
    u = eag(a, edu)
    if edu != "all" and eag("25-34", edu) is None:
        return {}   # level not published: derive_labour_market uses the nearest level
    r_edu = (_ratio(eag("25-34", edu), eag("25-34", "all")) or 1.0) if edu != "all" else 1.0
    if band == "20-24":
        y, _ = lm.get(iso, "DF_IALFS_UNE_M", "rate", "Y15T24", "all", Y)
        if y is None:
            return {}
        u, est = y * r_edu, True
        notes.append("OECD 15-24 rate" + (f" x{r_edu:.2f} {edu.replace('_', ' ')} gap at 25-34"
                                          if edu != "all" else ""))
    elif band in ("25-29", "30-34"):
        notes.append("OECD EAG ages 25-34")
    if u is None:
        return {}
    out["unemployment"] = (u, est, "; ".join(notes))
    if band != "20-24":
        ipt, _ = lm.get(iso, "DF_INVPT_I", "rate", "25-54", "all", Y, back=2)
        if iso == "CAN":
            ca = {"25-29": "25-44", "30-34": "25-44", "25-64": "25-54"}[band]
            r6, _ = lm.get(iso, "14-10-0077-01", "r6", ca, "all", Y)
            r4, _ = lm.get(iso, "14-10-0077-01", "r4", ca, "all", Y)
            disc = r6 - r4 if r6 is not None and r4 is not None else None
            dn = f"StatCan R6-R4 discouraged + waiting ({ca})"
        elif iso == "GBR":
            disc, _ = lm.get("GBR", "ONS LF8B", "discouraged", "16-64", "all", Y, back=2)
            dn = "ONS discouraged workers 16-64 (narrow UK definition)"
        else:
            disc, dn = None, ""
        if ipt is not None and disc is not None:
            out["extra"] = ((ipt + disc) * r_edu, ipt * r_edu, True,
                            f"OECD involuntary part-time 25-54 + {dn}"
                            + (f", x{r_edu:.2f} education gap" if edu != "all" else ""))
    return out


def derive_labour_market(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Unemployment, functional unemployment (U-6-like slack), hours underemployment and
    over-qualification by age band and education, on one definition for every country.

    functional = unemployment + 'extra' slack (part-timers wanting more hours + people who
    want work and could start but are not searching). For ages 20-24 the extra slack is
    the 25-29 extra x (20-24 / 25-29 unemployment): 15-24 slack data are dominated by
    students (who want part-time jobs or can't start), the group this comparison avoids."""
    lm = _LM(long)
    rows = []
    srcs = {"EU": "Eurostat LFS (lfsa_urgaed, lfsi_sla_a, lfsa_sup_edu, lfsa_eoqgan) + "
                  "OECD EAG", "USA": "CPS microdata (Census API), computed",
            "CAN": "OECD EAG + OECD involuntary part-time + StatCan 14-10-0077",
            "GBR": "OECD EAG + OECD involuntary part-time + ONS LF8B"}
    srcs["USA_OECD"] = "OECD EAG (no CENSUS_API_KEY: no CPS microdata)"
    units = {"unemployment": "% of labour force", "functional": PC_ELF,
             "hours_underemployment": PC_ELF,
             "overqualification": "% of employed graduates in non-graduate jobs"}
    for iso in COUNTRIES:
        if iso == "USA" and lm.years("USA", "CPS basic monthly", "unemployment", "25-29"):
            years = lm.years("USA", "CPS basic monthly", "unemployment", "25-29", "all")
            cell = lambda Y, b, e: _lm_us(lm, Y, b, e)  # noqa: E731
            src = srcs["USA"]
        elif iso in ("CAN", "GBR", "USA"):  # USA without CPS microdata: OECD, no slack
            years = [y for y in lm.years(iso, "DF_LSO_NEAC_UNEMP", "rate", "25-34", "all")
                     if y >= 2015]
            cell = lambda Y, b, e, i=iso: _lm_ca_uk(lm, i, Y, b, e)  # noqa: E731
            src = srcs["USA_OECD" if iso == "USA" else iso]
        else:
            s = "CHE" if iso == "LIE" else iso
            years = [y for y in lm.years(s, "lfsa_urgaed", "rate", "25-29", "all") if y >= 2015]
            cell = lambda Y, b, e, i=iso, s=s: _lm_europe(lm, i, s, Y, b, e)  # noqa: E731
            src = srcs["EU"]
        for Y in years:
            cells = {(b, e): cell(Y, b, e) for b in LM_BANDS for e in LM_EDU}
            u25 = (cells[("25-29", "all")].get("unemployment") or (None,))[0]
            u20 = (cells[("20-24", "all")].get("unemployment") or (None,))[0]
            k = _ratio(u20, u25)
            for (b, e), c in cells.items():  # degree level not published: nearest level
                if e in TERTIARY_SUB and "unemployment" not in c:
                    for alt in ("bachelor", "short_tertiary", "master_plus"):
                        if "unemployment" in cells[(b, alt)]:
                            cells[(b, e)] = c = {
                                k2: v[:-2] + (True, v[-1] + f"; {alt.replace('_', ' ')} "
                                              "rate (this degree level not published)")
                                for k2, v in cells[(b, alt)].items()}
                            break
            for (b, e), c in cells.items():
                if b == "20-24" and k is not None and "extra" in cells[("25-29", e)]:
                    x, h, est, note = cells[("25-29", e)]["extra"]
                    c["extra"] = (x * k, h * k, True, note + f"; scaled x{k:.2f} (20-24 vs "
                                  "25-29 unemployment): 15-24 slack data are mostly students")
                out = {}
                if "unemployment" in c:
                    out["unemployment"] = c["unemployment"]
                    if "extra" in c:
                        u, ue, un = c["unemployment"]
                        x, h, xe, xn = c["extra"]
                        out["functional"] = (u + x, ue or xe, "; ".join(
                            t for t in (un, "+ " + xn) if t))
                        out["hours_underemployment"] = (h, xe, xn)
                if "overqualification" in c:
                    out["overqualification"] = c["overqualification"]
                for ind, (v, est, note) in out.items():
                    if v is None or v != v:
                        continue
                    rows.append({"iso3": iso, "indicator": ind, "age_band": b, "education": e,
                                 "year": Y, "value": round(v, 2), "unit": units[ind],
                                 "source": src, "method": note, "estimated": bool(est),
                                 "quality": "A" if not est and ind == "unemployment"
                                 else "B"})
    return pd.DataFrame(rows)


PAY_BANDS = ["25-34", "35-44", "45-54", "55-64"]
PAY_LEVELS = ["below_upper_secondary", "upper_secondary", "short_tertiary", "bachelor",
              "master_plus"]


def derive_education_pay(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Full-time earnings by education relative to all education levels, by age band:
    OECD EAG relative earnings (upper secondary = 100), averaged over the latest 3 years,
    divided by their employment-weighted mean (attainment share x employment rate)."""
    lm = _LM(long)
    rows = []
    for iso in COUNTRIES:
        for band in PAY_BANDS:
            rel, w = {}, {}
            for e in PAY_LEVELS + ["tertiary"]:
                ys = lm.years(iso, "DF_LSO_EARN_REL_UPPER", "index_upper_secondary_100", band, e)
                if not ys:
                    continue
                vals = [lm.get(iso, "DF_LSO_EARN_REL_UPPER", "index_upper_secondary_100",
                               band, e, y)[0] for y in ys[-3:]]
                rel[e] = (sum(vals) / len(vals), ys[-1])
                sh = lm.get(iso, "DF_LSO_NEAC_DISTR_EA", "share", band, e)[0]
                em = lm.get(iso, "DF_LSO_NEAC_EMP", "rate", band, e)[0]
                if sh is not None and em is not None:
                    w[e] = sh * em / 100
            for e in ("short_tertiary", "bachelor", "master_plus"):
                if e not in rel and "tertiary" in rel:
                    rel[e] = rel["tertiary"]
            lv = [e for e in PAY_LEVELS if e in rel and e in w]
            if len(lv) < 3 or sum(w[e] for e in lv) <= 0:
                continue
            mean = sum(rel[e][0] * w[e] for e in lv) / sum(w[e] for e in lv)
            for e, (v, y) in rel.items():
                rows.append({"iso3": iso, "education": e, "age_band": band,
                             "factor": round(v / mean, 3), "year": y,
                             "source": "OECD Education at a Glance (relative earnings, "
                                       "full-time full-year)", "estimated": False,
                             "method": "3-year average / employment-weighted mean of all "
                                       "levels"})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    eu = df[df["iso3"].isin([c for c in EUROPE if c != "GBR"])]
    med = eu.groupby(["education", "age_band"], as_index=False)["factor"].median()
    extra = []
    for iso in COUNTRIES:
        if iso in set(df["iso3"]):
            continue
        base = df[df["iso3"] == "CHE"] if iso == "LIE" else med
        note = "proxy: Switzerland" if iso == "LIE" else "ESTIMATE: median of European " \
            "countries (no OECD data)"
        for r in base.itertuples(index=False):
            extra.append({"iso3": iso, "education": r.education, "age_band": r.age_band,
                          "factor": r.factor, "year": getattr(r, "year", None),
                          "source": "OECD Education at a Glance", "estimated": True,
                          "method": note})
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True)


# ---------- Families, survey coverage, pensions -------------------------------------
FAM_EDU = {"all": "all", "below_upper_secondary": "below_upper_secondary",
           "upper_secondary": "upper_secondary", "tertiary": "tertiary"}


def _latest_by(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return df.sort_values("year").groupby(keys, dropna=False).tail(1)


def derive_family(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Child penalty in employment and hours for the primary parent, and the age children
    leave home. earnings_factor = (employment rate x mean hours of employed) of women
    25-49 with n children and the youngest in a band / the same for women without
    children (Europe: mean hours = FT hours x (1 - PT share) + PT hours x PT share)."""
    rows = []
    fw = long[long["measure"] == "family_work"].dropna(subset=["year", "value"])
    for iso, g in fw.groupby("iso3"):
        g = _latest_by(g, ["statistic", "category", "household_type"])
        get = lambda st, ht, cat="all": g.loc[(g.statistic == st) & (g.household_type == ht) &  # noqa
                                              (g.category == cat), "value"]
        src = str(g["source"].iloc[0])
        yr = int(g["year"].max())
        hft, hpt = get("hours_ft", "any"), get("hours_pt", "any")

        def hours(ht):
            mh = get("mean_hours", ht)
            if not mh.empty:
                return float(mh.iloc[0])
            pt = get("part_time_share", ht)
            if pt.empty:
                return None
            ft_h = float(hft.iloc[0]) if not hft.empty else 40.0
            pt_h = float(hpt.iloc[0]) if not hpt.empty else 0.55 * ft_h
            return ft_h * (1 - pt.iloc[0] / 100) + pt_h * pt.iloc[0] / 100
        h0 = hours("0|none")
        for ht in sorted(set(g["household_type"]) - {"0|none", "any"}):
            n, yb = ht.split("|")
            hk = hours(ht)
            for cat in FAM_EDU:
                e0, ek = get("employment_rate", "0|none", cat), get("employment_rate", ht, cat)
                if e0.empty or ek.empty or not e0.iloc[0]:
                    continue
                er = float(ek.iloc[0] / e0.iloc[0])
                hr = (hk / h0) if hk and h0 else 1.0
                for item, v in (("employment_ratio", er), ("hours_ratio", hr),
                                ("earnings_factor", er * hr)):
                    rows.append({"iso3": iso, "item": item, "n_children": n, "youngest": yb,
                                 "education": cat, "value": round(min(v, 1.1), 3),
                                 "year": yr, "source": src, "estimated": True,
                                 "method": "women 25-49 with children vs without"})
    lh = long[long["measure"] == "age_leave_home"].dropna(subset=["year", "value"])
    for iso, g in lh.groupby("iso3"):
        r = g.sort_values("year").iloc[-1]
        rows.append({"iso3": iso, "item": "age_leave_home", "n_children": "", "youngest": "",
                     "education": "all", "value": round(float(r["value"]), 1),
                     "year": int(r["year"]), "source": str(r["source"]),
                     "estimated": int(r["year"]) < datetime.now().year - 3,
                     "method": "" if pd.isna(r["method"]) else str(r["method"])})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # countries with no data of their own: LIE <- CHE; otherwise nothing (engine default)
    if "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        df = pd.concat([df, df[df["iso3"] == "CHE"].assign(iso3="LIE", estimated=True,
                                                           method="proxy: Switzerland")])
    return df


COVERAGE_CATS = {  # budget category -> COICOP division whose HBS/NA ratio applies
    "groceries": "CP01", "alcohol_tobacco": "CP02", "clothing": "CP03",
    "furnishings_household": "CP05", "transport_services": "CP07", "phone_internet": "CP08",
    "recreation": "CP09", "education": "CP10", "restaurants_hotels": "CP11"}
COVERAGE_CAP = {"CP11": 1.5}   # domestic concept: tourists' restaurant/hotel spending


def derive_budget_coverage(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """National-accounts household consumption per household / HBS mean per household, by
    COICOP division and HBS wave (2010, 2015, 2020), averaged. Rents (own model), health
    (own module) and CP12 (financial services, insurance, social protection: conceptual
    differences) are not corrected. Clipped to [1, 2] (restaurants & hotels 1.5). The
    engine applies ratio ** survey_correction (default 0.5). Non-EU countries: EU median."""
    h = long[long["measure"] == "hbs_coverage"].dropna(subset=["year", "value"])
    if h.empty:
        return pd.DataFrame()
    hb = h[h.statistic == "hbs_eur_per_household"].set_index(["iso3", "year", "category"])["value"]
    na = h[h.statistic == "na_meur"].set_index(["iso3", "year", "category"])["value"]
    nh = h[h.statistic == "households_ths"].set_index(["iso3", "year"])["value"]
    rows = []
    for (iso, yr, cp), v in hb.items():
        if (iso, yr, cp) in na.index and (iso, yr) in nh.index and v > 0:
            rows.append({"iso3": iso, "year": int(yr), "coicop": cp,
                         "ratio": na[(iso, yr, cp)] * 1e6 / (nh[(iso, yr)] * 1e3) / v})
    r = pd.DataFrame(rows)
    if r.empty:
        return r
    r = r.groupby(["iso3", "coicop"], as_index=False).agg(ratio=("ratio", "mean"),
                                                          year=("year", "max"))
    out = []
    med = r[r["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", "", ""))[3] == "EU")] \
        .groupby("coicop")["ratio"].median()
    for iso in COUNTRIES:
        for cat, cp in COVERAGE_CATS.items():
            m = r[(r.iso3 == ("CHE" if iso == "LIE" else iso)) & (r.coicop == cp)]
            est = m.empty
            raw = float(med.get(cp, 1.0)) if est else float(m["ratio"].iloc[0])
            out.append({"iso3": iso, "category": cat, "coicop": cp, "raw_ratio": round(raw, 3),
                        "ratio": round(min(max(raw, 1.0), COVERAGE_CAP.get(cp, 2.0)), 3),
                        "year": None if est else int(m["year"].iloc[0]), "estimated": est,
                        "source": ("BEA personal consumption / BLS Consumer Expenditure "
                                   "Survey, per household" if iso == "USA" and not est else
                                   "Eurostat national accounts (nama_10_co3_p3) / HBS "
                                   "(hbs_exp_t121) per household" + (
                                       " - EU median (no survey comparison for this "
                                       "country)" if est else ""))})
    return pd.DataFrame(out)


def derive_pensions(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """One row per (country, item): net replacement rates at 0.5 / 1 / 2 x the average wage
    (mandatory), the retirement age for today's labour-market entrants, and life
    expectancy at 65 now and projected for 2065, by sex."""
    rows = []
    p = long[long["measure"] == "pension_replacement"].dropna(subset=["year", "value"])
    if not p.empty:
        p = _latest_by(p, ["iso3", "statistic", "category"])
        item = {"net_rate_half_average": "nrr50", "net_rate_average_earner": "nrr100",
                "net_rate_double_average": "nrr200"}
        for r in p.itertuples(index=False):
            if r.statistic in item:
                rows.append({"iso3": r.iso3, "item": item[r.statistic] +
                             ("" if r.category == "mandatory" else "_voluntary"),
                             "value": float(r.value), "year": int(r.year),
                             "source": str(r.source)})
    ret = long[long["measure"] == "retirement"].dropna(subset=["year", "value"])
    for (iso, stat), g in ret.groupby(["iso3", "statistic"]):
        if stat == "life_expectancy_at_65":
            now = int(g.loc[g["year"] <= datetime.now().year, "year"].max()) \
                if (g["year"] <= datetime.now().year).any() else None
            for (yr, sex), h in g.groupby(["year", "category"]):
                tag = "now" if now is not None and int(yr) == now else str(int(yr))
                if tag == "now" or int(yr) > datetime.now().year:
                    rows.append({"iso3": iso, "item": f"le65_{sex}_{tag}",
                                 "value": float(h["value"].mean()), "year": int(yr),
                                 "source": "OECD Pensions at a Glance (UN WPP)"})
        else:
            g = g[g["year"] == g["year"].max()]
            rows.append({"iso3": iso, "item": {"normal_age_future_entrant": "retirement_age",
                                              "normal_age_current": "retirement_age_now"}[stat],
                         "value": round(float(g["value"].mean()), 1),    # men/women average
                         "year": int(g["year"].iloc[0]), "source": str(g["source"].iloc[0])})
    return pd.DataFrame(rows)


def derive_university(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Latest household payment to tertiary institutions per FTE student (local money)."""
    u = long[long["measure"] == "tertiary_private_cost"].dropna(subset=["year", "value"])
    rows = []
    for iso, g in u.groupby("iso3"):
        for stat in ("households", "all_private"):
            h = g[g["statistic"] == stat]
            if not h.empty:
                h = h.sort_values("year").iloc[-1]
                rows.append({"iso3": iso, "per_student_year": float(h["value"]),
                             "year": int(h["year"]), "basis": stat,
                             "source": str(h["source"]), "estimated": stat != "households"})
                break
    return pd.DataFrame(rows)


# central-bank inflation targets (statutory; the mid-point where a range is set). Denmark
# pegs to the euro; Switzerland's "price stability" is 0-2%.
INFLATION_TARGET = {"USA": 2.0, "GBR": 2.0, "CAN": 2.0, "CHE": 1.0, "LIE": 1.0, "NOR": 2.0,
                    "SWE": 2.0, "DNK": 2.0, "ISL": 2.5, "CZE": 2.0, "POL": 2.5, "HUN": 3.0,
                    "ROU": 2.5, **{c: 2.0 for c in EURO_MEMBERS}}


TERM_PREMIUM = 0.5   # 10-year yield over the expected average short rate (pp); recent
#                      ACM / Kim-Wright estimates for US Treasuries are ~0.3-0.8


def derive_rates(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Forward-looking interest rates per country. The 10-year government yield, less a
    term premium, is the average short rate markets expect over the next decade; a fixed
    mortgage period resets to today's mortgage rate moved by that expected change in
    short rates, instead of to the historical average. (The usual mortgage spread over
    the 10-year yield is kept for reference.)"""
    ir = long[(long["measure"] == "interest_rate")].dropna(subset=["year", "value"])
    pref = {"mortgage_rate_new_loans": 0, "mortgage_rate_10y_fixed_median": 1,
            "mortgage_rate_2y_fixed_75ltv": 1, "mortgage_rate_30y_fixed": 1}
    m = long[long["statistic"].isin(pref)].dropna(subset=["year", "value"])
    last_full = datetime.now().year - 1
    rows = []
    for iso in COUNTRIES:
        src_iso = "CHE" if iso == "LIE" else iso
        r = {"iso3": iso, "inflation_target": INFLATION_TARGET.get(iso, 2.0)}
        g = ir[ir["iso3"] == src_iso]
        for stat, col in (("govt_bond_10y", "y10"), ("interbank_3m", "short_rate"),
                          ("deposit_overnight", "deposit_overnight"),
                          ("deposit_term", "deposit_term")):
            s = g[g["statistic"] == stat].sort_values("period")
            if s.empty:
                continue
            last = pd.Period(str(s["period"].iloc[-1])[:7], "M")
            if (pd.Period(datetime.now(), "M") - last).n > 18:
                continue          # discontinued (e.g. Iceland in the OECD table): not current
            tail = s.tail(3)                         # last 3 months: smooths one-off moves
            r[col] = round(float(tail["value"].mean()), 3)
            r[f"{col}_period"] = str(tail["period"].iloc[-1])
            r[f"{col}_source"] = str(s["source"].iloc[-1])
        y10 = g[g["statistic"] == "govt_bond_10y"].groupby("year")["value"].mean()
        mm = m[m["iso3"] == src_iso]
        if not mm.empty and not y10.empty:
            mm = mm[mm["statistic"].map(pref) == mm["statistic"].map(pref).min()]
            my = mm.groupby("year")["value"].mean()
            both = pd.concat([my.rename("m"), y10.rename("y")], axis=1).dropna()
            # last 10 years: spreads moved a lot as e.g. Bulgaria and the Baltics converged
            both = both[(both.index >= last_full - 9) & (both.index <= last_full)]
            if len(both) >= 5:
                r["mortgage_spread"] = round(float((both["m"] - both["y"]).median()), 3)
                r["spread_years"] = f"{int(both.index.min())}-{int(both.index.max())}"
                r["mortgage_series"] = str(mm["statistic"].iloc[0])
        if not mm.empty:
            mm2 = mm[mm["statistic"].map(pref) == mm["statistic"].map(pref).min()]
            per = mm2.sort_values("period")
            r["mortgage_rate_now"] = round(float(per.tail(3)["value"].mean()), 3)
            r["mortgage_rate_now_period"] = str(per["period"].iloc[-1])
        if "y10" in r and "short_rate" in r and "mortgage_rate_now" in r:
            # expectations hypothesis: 10-year yield - term premium = the average short
            # rate markets expect over the next decade; mortgage rates move with it
            exp_short = r["y10"] - TERM_PREMIUM
            r["expected_short_rate"] = round(exp_short, 3)
            r["mortgage_rate_future"] = round(max(r["mortgage_rate_now"] + exp_short -
                                                  r["short_rate"], 0.5), 2)
        rows.append(r)
    # deposit rates where no central source publishes them: the 3-month rate times the
    # median pass-through (deposit rate / 3-month rate) of the euro area and the US
    for col in ("deposit_overnight", "deposit_term"):
        ratios = [r[col] / r["short_rate"] for r in rows
                  if col in r and r.get("short_rate", 0) > 0.5]
        if not ratios:
            continue
        k = float(statistics.median(ratios))
        for r in rows:
            if col not in r and "short_rate" in r:
                r[col] = round(max(r["short_rate"] * k, 0.0), 3)
                r[f"{col}_period"] = r["short_rate_period"]
                r[f"{col}_source"] = (f"estimate: 3-month rate x {k:.2f}, the median pass-"
                                      f"through in the {len(ratios)} countries with ECB/FDIC "
                                      "household deposit rates")
                r[f"{col}_estimated"] = True
    return pd.DataFrame(rows)


def derive_us_career(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """us_career.csv: one row per number - kind (share / earn / trans / growth / cpi),
    window (current = last 12 months, prior = the 12 before), age band, education,
    situation (and the situation a year later, for moves), pay quantile."""
    c = long[long["measure"] == "us_career"].dropna(subset=["value"])
    rows = []
    for r in c.itertuples(index=False):
        stat = str(r.statistic)
        window = "current"
        if stat.startswith("prior:"):
            window, stat = "prior", stat[6:]
        name, _, st = stat.partition("|")
        kind, q, s0, s1 = name, None, st or None, None
        if name.startswith("earn_q"):
            kind, q = "earn", int(name[6:])
        elif name == "trans":
            s0, s1 = st.split(">")
        elif name == "pay_growth_median":
            kind = "growth"
        elif name == "cpi_yoy":
            kind = "cpi"
        elif name == "agg_wage_yoy":
            kind = "agg_growth"
        m = re.search(r"n=(\d+); (\S+)\.\.(\S+)", str(r.method))
        rows.append({"kind": kind, "window": window, "age_band": r.age_band,
                     "education": r.category, "state": s0, "to_state": s1, "q": q,
                     "value": float(r.value), "n": int(m.group(1)) if m else None,
                     "from": m.group(2) if m else None, "to": m.group(3) if m else None})
    return pd.DataFrame(rows)


PRICE_TREND_YEARS = 10


def derive_price_outlook(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Current price trends: how much faster (or slower) each budget line's prices rose than
    prices in general over the last 10 complete years (HICP by COICOP in Europe, CPI
    components in the US), and US market inflation expectations."""
    rows = []
    pl = long[long["measure"] == "price_level"].dropna(subset=["year", "value"])
    hicp = pl[pl["statistic"] == "hicp_index"]
    code_for = {}
    for code, key in COICOP_CATS.items():          # most specific code wins
        if code in HICP_CODES and len(code) >= len(code_for.get(key, "")):
            code_for[key] = code
    code_for.update({"childcare": "CP124", "phone_internet": "CP08",
                     "vehicle_running": "CP0723", "rent": "CP041"})
    sources = []
    for iso, g in hicp.groupby("iso3"):
        sources.append((iso, g.pivot_table(index="year", columns="category", values="value"),
                        "CP00", code_for, "Eurostat HICP"))
    us = pl[(pl["statistic"] == "cpi_component") & (pl["iso3"] == "USA")]
    if not us.empty:
        sources.append(("USA", us.pivot_table(index="year", columns="category",
                                              values="value"), "all",
                        {k: k for k in US_CPI_PARTS if k != "all"}, "BLS CPI (FRED)"))
    for iso, t, all_code, cmap, src in sources:
        if all_code not in t:
            continue
        t = t.dropna(subset=[all_code])
        if t.empty:
            continue
        y1 = int(t.index.max())
        y0 = y1 - PRICE_TREND_YEARS
        if y0 not in t.index:
            continue
        base = (t.loc[y1, all_code] / t.loc[y0, all_code]) ** (1 / PRICE_TREND_YEARS)
        for key, code in cmap.items():
            if code not in t or pd.isna(t.loc[y0, code]) or pd.isna(t.loc[y1, code]):
                continue
            g = (t.loc[y1, code] / t.loc[y0, code]) ** (1 / PRICE_TREND_YEARS)
            rows.append({"iso3": iso, "item": f"rel:{key}",
                         "value": round(100 * (g / base - 1), 3),
                         "period": f"{y0}-{y1}", "source": f"{src} {code} vs all items"})
    io = long[long["measure"] == "inflation_outlook"].dropna(subset=["value"])
    for stat, item in (("breakeven_5y", "inflation_5y"), ("forward_5y5y", "inflation_5y5y")):
        s_ = io[io["statistic"] == stat].sort_values("period").tail(3)
        if not s_.empty:
            rows.append({"iso3": "USA", "item": item, "value": round(float(s_["value"].mean()), 3),
                         "period": str(s_["period"].iloc[-1]),
                         "source": f"{s_['series'].iloc[-1]} (last 3 months)"})
    return pd.DataFrame(rows)


# Danish labour-market pensions are run by life insurers (F62): they are quasi-mandatory
# pension saving (modelled with the pension), not free savings
LIFE_INSURANCE_IS_PENSION = {"DNK"}


def derive_portfolio(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """How households hold their free financial savings (deposits, bonds, listed shares,
    funds, life insurance; not pensions or unlisted business equity), latest year with
    deposits and funds reported. Funds count as shares, life insurance as bonds."""
    a = long[long["measure"] == "household_assets"].dropna(subset=["year", "value"])
    rows = []
    for iso in COUNTRIES:
        src = "CHE" if iso == "LIE" else iso
        g = a[a["iso3"] == src]
        best = None
        for yr in sorted(g["year"].unique(), reverse=True):
            p = g[g["year"] == yr].groupby("statistic")["value"].sum(min_count=1)
            if pd.notna(p.get("deposits")) and pd.notna(p.get("fund_shares")):
                best = (int(yr), p)
                break
        if best is None:
            rows.append({"iso3": iso})
            continue
        yr, p = best
        v = lambda k: float(p.get(k)) if pd.notna(p.get(k)) else 0.0  # noqa: E731
        life = 0.0 if iso in LIFE_INSURANCE_IS_PENSION else v("life_insurance")
        dep, shares = v("deposits"), v("listed_shares") + v("fund_shares")
        fin = dep + v("debt_securities") + shares + life
        tr, oth = v("deposits_transferable"), v("deposits_other")
        rows.append({"iso3": iso, "year": yr, "deposit_share": round(dep / fin, 3),
                     "equity_share": round(shares / (fin - dep), 3) if fin > dep else None,
                     "overnight_share": round(tr / (tr + oth), 3) if tr + oth > 0 else None,
                     "source": "OECD financial balance sheets, households and NPISH" +
                     (" (Swiss data)" if iso == "LIE" else ""), "estimated": False})
    df = pd.DataFrame(rows)
    have = df.dropna(subset=["deposit_share"])
    eu = have[have["iso3"].map(lambda c: COUNTRIES[c][3] == "EU")]
    for c in ("deposit_share", "equity_share", "overnight_share"):
        miss = df[c].isna() if c in df else pd.Series(True, index=df.index)
        df.loc[miss, c] = round(float(eu[c].median()), 3)
    gap = df["source"].isna()
    df.loc[gap, "source"] = "EU median (no national balance sheet with deposits and funds)"
    df.loc[gap, "estimated"] = True
    df.loc[gap, "year"] = int(eu["year"].max()) if not eu.empty else None
    return df


def derive_market_index(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Yearly home-price and rent indices per country (current year = months so far), used
    to move curated price/rent levels from their survey year to the latest data."""
    h = long[long["measure"] == "housing"].dropna(subset=["year", "value"])
    rows = []
    for stat, idx in (("house_price_index", "home_price"), ("rent_index", "rent"),
                      ("median_existing_single_family_price", "home_price")):
        d = h[h["statistic"] == stat]
        for (iso, yr), g in d.groupby(["iso3", "year"]):
            sub = g[g["frequency"].isin(["M", "Q"])] if g["frequency"].isin(["M", "Q"]).any() \
                else g
            rows.append({"iso3": iso, "index": idx, "year": int(yr),
                         "value": float(sub["value"].mean()),
                         "n": int(len(sub)), "source": str(g["source"].iloc[0]),
                         "estimated": bool(g["estimated"].any()) or (
                             g["frequency"].iloc[0] == "M" and len(sub) < 12)})
    # fuel: HICP fuels and lubricants (CP0722) in Europe, EIA gasoline price (US)
    pl = long[(long["measure"] == "price_level") & (long["statistic"] == "hicp_index") &
              (long["category"].astype(str) == "CP0722")].dropna(subset=["year", "value"])
    for (iso, yr), g in pl.groupby(["iso3", "year"]):
        rows.append({"iso3": iso, "index": "fuel", "year": int(yr), "value": float(g["value"].mean()),
                     "n": int(len(g)), "source": "Eurostat HICP (fuels, CP0722)",
                     "estimated": False})
    gas = long[(long["statistic"] == "gasoline_price")].dropna(subset=["year", "value"])
    for (iso, yr), g in gas.groupby(["iso3", "year"]):
        rows.append({"iso3": iso, "index": "fuel", "year": int(yr), "value": float(g["value"].mean()),
                     "n": int(len(g)), "source": str(g["source"].iloc[0]) + " (gasoline)",
                     "estimated": len(g) < 50})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # one source per country and index: the most recent one (Eurostat stops for the UK in
    # 2019), Eurostat on a tie
    last = df.groupby(["iso3", "index", "source"])["year"].transform("max")
    cnt = df.groupby(["iso3", "index", "source"])["year"].transform("count")
    # newest first, then Eurostat, then the longest series (US: BIS index over NAR medians)
    df["rank"] = -last * 10_000 + df["source"].map(lambda x: 0 if "Eurostat" in x else
                                                     5_000) - cnt
    best = df.groupby(["iso3", "index"])["rank"].transform("min")
    return df[df["rank"] == best].drop(columns="rank").sort_values(["iso3", "index", "year"])


def derive_us_states(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    s = long[long["measure"] == "us_state"].dropna(subset=["value"])
    if s.empty:
        return pd.DataFrame()
    t = s.pivot_table(index="category", columns="statistic", values="value", aggfunc="last")
    yrs = s.groupby("statistic")["year"].max()
    t["acs_year"] = int(yrs.drop("rpp", errors="ignore").max())
    if "rpp" in yrs:
        t["rpp_year"] = int(yrs["rpp"])
    return t.reset_index().rename(columns={"category": "name"})


def derive_mortgage_longrun(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Average new-loan mortgage rate over the last 20 complete years (the rate a fixed
    period resets to), from the same series as the current-rate defaults."""
    pref = {"mortgage_rate_new_loans": 0, "mortgage_rate_10y_fixed_median": 1,
            "mortgage_rate_2y_fixed_75ltv": 1, "mortgage_rate_30y_fixed": 1}
    m = long[long["statistic"].isin(pref)].dropna(subset=["year", "value"])
    last = datetime.now().year - 1
    m = m[m["year"].between(last - 19, last)]
    rows = []
    for iso, g in m.groupby("iso3"):
        g = g[g["statistic"].map(pref) == g["statistic"].map(pref).min()]
        y = g.groupby("year")["value"].mean()
        rows.append({"iso3": iso, "rate_longrun": round(float(y.mean()), 2),
                     "years": int(len(y)), "from": int(y.index.min()), "to": int(y.index.max()),
                     "source": str(g["source"].iloc[0]), "series": str(g["statistic"].iloc[0])})
    return pd.DataFrame(rows)


# ---------- Country snapshot -----------------------------------------------
def derive_snapshot(long, ctx, budget, life, epp) -> pd.DataFrame:
    rows = []
    for iso3, (_, _, name, region) in COUNTRIES.items():
        r = {"iso3": iso3, "country": name, "region": region,
             "currency": LOCAL_CURRENCY.get(iso3, "EUR")}

        def last(measure, stat, **eq):
            s = ctx.mi(measure, iso3)  # pre-indexed: no full-table scan per call
            s = s[s["statistic"] == stat]
            for k, v in eq.items():
                s = s[s[k].astype(str).str.contains(v, case=False, regex=True, na=False)]
            if s.empty:
                return None, None
            s = s.sort_values(["year", "period"]).iloc[-1]
            return s["value"], s["period"]
        if not epp.empty:
            e = epp[(epp["iso3"] == iso3) & (epp["statistic"] == "median")]
            for stage in ("early", "mid"):
                s = e[e["career_stage"] == stage].sort_values("year")
                if not s.empty:
                    s = s[s["year"] == s["year"].max()]
                    r[f"median_ft_earnings_{stage}_intl$"] = s["annual_intl_dollars"].median()
                    r[f"median_ft_earnings_{stage}_year"] = int(s["year"].iloc[0])
                    r[f"median_ft_earnings_{stage}_estimated"] = bool(s["estimated"].any())
        v, p = last("household_income", "median", unit="^NAC",
                    household_type="two adults younger|^two adults$")
        if v is not None:
            r["silc_median_equivalised_2adults"], r["silc_year"] = v, p
            r["silc_couple_household_net_x1.5"] = v * 1.5
        v, p = last("unemployment_rate", "rate", age_band="15-24|15T24")
        r["youth_unemployment_rate"], r["youth_unemployment_year"] = v, p
        v, p = last("job_vacancy_rate", "rate")
        r["job_vacancy_rate"], r["job_vacancy_period"] = v, p
        v, p = last("saving_rate_net", "rate")
        r["household_net_saving_rate"], r["saving_year"] = v, p
        v, p = last("uncertainty", "wui_smoothed")
        r["wui_smoothed"] = v
        v, p = last("uncertainty", "political_stability")
        r["wgi_political_stability"] = v
        if not budget.empty:
            b = budget[budget["iso3"] == iso3]
            if not b.empty:
                r["couple_budget_monthly_local_excl_housing"] = b[~b["category"].isin(
                    HOUSING_TENURE_CATS)]["monthly_local"].sum()
                r["couple_budget_source"] = b["source"].iloc[0]
        if not life.empty:
            l = life[life["iso3"] == iso3]
            for item in ("home_price", "down_payment_amount", "mortgage_rate",
                         "mortgage_payment_25y", "tenant_total_housing_cost",
                         "median_gross_rent", "average_private_rent",
                         "median_of_centres_avg_rent_2br", "share_cannot_cover_unexpected_expense",
                         "unexpected_expense_size"):
                s = l[l["item"] == item]
                if not s.empty:
                    r[item] = s["value"].iloc[0]
        ppp, _ = ctx.wb("ppp_private_consumption", iso3, ctx.latest_year)
        r["ppp_lcu_per_intl$"] = ppp
        rows.append(r)
    return pd.DataFrame(rows)


# ==========================================================================
# Orchestration
# ==========================================================================
MODULES: dict[str, list[tuple[str, Callable]]] = {
    "income": [("Eurostat SES", earnings_europe_ses), ("ONS ASHE", earnings_uk_ashe),
               ("BLS CPS", earnings_us_cps), ("StatCan LFS wages", earnings_canada_lfs),
               ("EU-SILC by age", income_silc_by_age),
               ("EU-SILC by household", income_silc_by_household),
               ("EU-SILC by urbanisation", income_silc_by_urbanisation),
               ("EU-SILC percentiles", income_silc_distribution),
               ("OECD IDD", income_oecd_idd),
               ("OECD Taxing Wages", net_income_taxing_wages),
               ("Eurostat net earnings", net_income_eurostat)],
    "wage_history": [("OECD average wages", avg_wage_oecd),
                     ("Eurostat FTE salary", avg_salary_eurostat)],
    "mobility": [("Atlanta Fed WGT", wage_growth_us), ("EU-SILC transitions", income_transitions_eu),
                 ("StatCan income mobility", income_mobility_canada)],
    "job_market": [("Eurostat unemployment", unemployment_europe),
                   ("OECD unemployment", unemployment_oecd),
                   ("Eurostat monthly unemployment", unemployment_europe_monthly),
                   ("Eurostat unemployment by education", unemp_by_education_europe),
                   ("Eurostat labour-market slack", slack_europe),
                   ("Eurostat over-qualification", overqualification_europe),
                   ("OECD Education at a Glance", education_oecd),
                   ("OECD involuntary part-time", involuntary_pt_oecd),
                   ("StatCan supplementary rates", slack_canada),
                   ("ONS discouraged workers", slack_uk),
                   ("CPS microdata (US)", labour_us_cps), ("CPS careers (US)", career_us_cps),
                   ("Eurostat JVS", vacancies_europe),
                   ("JOLTS", vacancies_us), ("StatCan JVWS", vacancies_canada),
                   ("ONS vacancies", vacancies_uk)],
    "saving": [("OECD saving", saving_oecd), ("Eurostat saving", saving_eurostat)],
    "cost_of_living": [("World Bank PPP", price_levels_worldbank),
                       ("World Bank CPI", cpi_worldbank),
                       ("Eurostat PPP/PLI", price_levels_eurostat),
                       ("Eurostat HICP", hicp_eurostat), ("Eurostat HBS", spending_eu_hbs),
                       ("BLS CE", spending_us_ce), ("StatCan SHS", spending_canada_shs),
                       ("ONS Family Spending", spending_uk_family),
                       ("StatCan MBM", spending_canada_mbm),
                       ("EU-SILC housing costs", housing_costs_silc),
                       ("Eurostat HPI", house_price_index_eu), ("Census ACS", housing_us_acs),
                       ("Eurostat rent index", rent_index_eu), ("BIS house prices", house_prices_bis),
                       ("ACS + BEA by US state", us_states_api),
                       ("US housing & energy", housing_energy_us),
                       ("Canada housing & fuel", housing_canada),
                       ("BoE mortgage rate", housing_uk_boe),
                       ("ECB mortgage rates", mortgage_rates_ecb),
                       ("SSB/SNB mortgage rates", mortgage_rates_no_ch),
                       ("OECD interest rates", interest_rates_oecd),
                       ("Eurostat interest rates", interest_rates_eu),
                       ("ECB deposit rates", deposit_rates_ecb),
                       ("US CPI components", prices_us_cpi_parts),
                       ("US inflation outlook", inflation_outlook_us),
                       ("FDIC deposit rates", deposit_rates_us),
                       ("OECD household assets", household_assets_oecd),
                       ("Eurostat energy prices", energy_prices_eu),
                       ("World Bank health", health_worldbank)],
    "life_events": [("OECD childcare", childcare_oecd),
                    ("Eurostat mothers' work", family_work_europe),
                    ("Eurostat leaving home", leave_home_europe),
                    ("CPS families (US)", family_us_cps), ("StatCan families", family_canada),
                    ("HBS coverage (EU)", hbs_coverage_eu), ("CE coverage (US)", coverage_us),
                    ("OECD pensions", pensions_oecd),
                    ("OECD retirement ages", retirement_oecd),
                    ("OECD university costs", university_cost_oecd),
                    ("OECD job-loss replacement", job_loss_replacement_oecd),
                    ("EU-SILC emergencies", emergency_eu)],
    "uncertainty": [("WUI", uncertainty_wui), ("WGI", uncertainty_wgi)],
}
# derived tables need price levels; pulled automatically when other modules run
ALWAYS = [("World Bank PPP", price_levels_worldbank), ("World Bank CPI", cpi_worldbank)]


def to_annual(long: pd.DataFrame) -> pd.DataFrame:
    """Average sub-annual series to years. Annual (and multi-year) rows pass straight
    through; only M/Q/S rows are grouped, on factorised integer keys (much faster than
    a 16-column object groupby over the whole table)."""
    keys = ["iso3", "country", "region", "measure", "statistic", "age_band", "career_stage",
            "household_type", "area_type", "category", "year", "unit", "estimated", "source",
            "series", "comparability_note"]
    sub = long["frequency"].isin(["M", "Q", "S"])
    annual = long.loc[~sub, keys + ["value"]].assign(n_subperiods=1)
    s = long.loc[sub, keys + ["value"]]
    if s.empty:
        return annual
    codes = pd.MultiIndex.from_frame(s[keys].astype(str)).factorize()[0]
    g = s.groupby(codes)
    first = g[keys].first()
    first["value"] = g["value"].mean()
    first["n_subperiods"] = g.size()
    return pd.concat([annual, first.reset_index(drop=True)], ignore_index=True)


def write(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path.with_suffix(".csv"), index=False)
    try:
        df.to_parquet(path.with_suffix(".parquet"), index=False)
    except (ImportError, ValueError, TypeError):
        pass


def load_references(path: Path | None) -> pd.DataFrame:
    refs = pd.DataFrame(REFERENCE_VALUES)
    if path:
        user = pd.read_csv(path)
        key = ["iso3", "item"]
        refs = pd.concat([user, refs]).drop_duplicates(key, keep="first")
    return refs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="econ_data", type=Path)
    ap.add_argument("--cache", default=None, type=Path, help="cache folder (default <out>/.cache)")
    ap.add_argument("--ttl-days", default=30.0, type=float,
                    help="re-download cached files older than this (default 30)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--since", default=2005, type=int, help="first year (default 2005)")
    ap.add_argument("--only", nargs="+", choices=list(MODULES), help="subset of modules")
    ap.add_argument("--home-m2", default=100.0, type=float,
                    help="home size used for European price-per-m2 (default 100)")
    ap.add_argument("--reference", type=Path, default=None,
                    help="CSV overriding reference_values.csv rows (same columns)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--derive-only", action="store_true",
                    help="skip downloads; rebuild derived tables from econ_long.csv")
    ap.add_argument("--profile", action="store_true",
                    help="run under cProfile and print the 25 most expensive functions")
    a = ap.parse_args(argv)
    if a.profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()
        argv2 = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--profile"]
        rc = prof.runcall(main, argv2)
        pstats.Stats(prof).sort_stats("cumulative").print_stats(25)
        return rc
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    a.out.mkdir(parents=True, exist_ok=True)
    f = CachedFetcher(a.cache or a.out / ".cache", ttl_days=a.ttl_days, refresh=a.refresh)
    refs = load_references(a.reference)

    plan: list[tuple[str, str, Callable]] = []
    for mod in a.only or list(MODULES):
        plan += [(mod, n, fn) for n, fn in MODULES[mod]]
    for n, fn in ALWAYS:
        if not any(fn is p[2] for p in plan):
            plan.append(("cost_of_living", n, fn))

    frames, status = [], []
    if a.derive_only:  # rebuild derived tables from the last econ_long.csv, no network
        prev = pd.read_csv(a.out / "econ_long.csv", low_memory=False)
        prev = prev[~prev["source"].astype(str).str.endswith("(nowcast)")]
        prev["year"] = pd.to_numeric(prev["year"], errors="coerce").astype("Int64")
        for c in ("period", "age_band", "category", "household_type", "area_type", "unit"):
            prev[c] = prev[c].astype(str).replace({"nan": pd.NA, "<NA>": pd.NA})
        frames.append(prev)
        plan = []
    for mod, name, fn in plan:
        t0 = time.time()
        try:
            df = fn(f, a.since)
            frames.append(df)
            status.append((mod, name, "ok", len(df), ""))
            log.info("%-26s %7d rows  (%.1fs)", name, len(df), time.time() - t0)
        except Exception as e:  # keep going; report at the end
            status.append((mod, name, "FAILED", 0, f"{type(e).__name__}: {str(e)[:200]}"))
            log.error("%-26s FAILED: %s", name, e)

    if not frames:
        log.error("Nothing fetched.")
        return 1
    t_derive = time.time()
    long = pd.concat(frames, ignore_index=True)
    long = long[long["iso3"].isin(COUNTRIES) | (long["iso3"] == "EMU")]
    long = pd.concat([long, derive_ses_nowcast(long)], ignore_index=True)
    long["estimated"] = long["estimated"].fillna(False).astype(bool)
    long = long.reset_index(drop=True)

    timings: list[tuple[str, float]] = []

    def timed(name, fn):
        t0 = time.time()
        try:
            df = fn()
            status.append(("derived", name, "ok", len(df), ""))
        except Exception as e:
            df = pd.DataFrame()
            status.append(("derived", name, "FAILED", 0, f"{type(e).__name__}: {str(e)[:200]}"))
            log.exception("derived %s failed", name)
        timings.append((name, time.time() - t0))
        log.info("derived %-22s %7d rows  (%.1fs)", name, len(df), timings[-1][1])
        return df

    t0 = time.time()
    ctx = Ctx(long, refs)
    timings.append(("Ctx index build", time.time() - t0))
    derived = {}
    for name, fn in [("earnings_ppp", lambda: derive_earnings_ppp(long, ctx)),
                     ("real_wage_history", lambda: derive_real_wages(long, ctx)),
                     ("couple_budget", lambda: derive_couple_budget(long, ctx)),
                     ("life_events", lambda: derive_life_events(long, ctx, a.home_m2)),
                     ("mobility_summary", lambda: derive_mobility(long, ctx)),
                     ("expat_tax", lambda: derive_expat_tax(long, ctx)),
                     ("earnings_profile", lambda: derive_earnings_profile(long, ctx)),
                     ("tax_curve", lambda: derive_tax_curve(long, ctx)),
                     ("fx_ppp", lambda: derive_fx_ppp(ctx)),
                     ("context_series", lambda: derive_context(long, ctx)),
                     ("labour_market", lambda: derive_labour_market(long, ctx)),
                     ("education_pay", lambda: derive_education_pay(long, ctx)),
                     ("family", lambda: derive_family(long, ctx)),
                     ("budget_coverage", lambda: derive_budget_coverage(long, ctx)),
                     ("pensions", lambda: derive_pensions(long, ctx)),
                     ("market_index", lambda: derive_market_index(long, ctx)),
                     ("us_states", lambda: derive_us_states(long, ctx)),
                     ("mortgage_longrun", lambda: derive_mortgage_longrun(long, ctx)),
                     ("rates", lambda: derive_rates(long, ctx)),
                     ("university", lambda: derive_university(long, ctx)),
                     ("portfolio", lambda: derive_portfolio(long, ctx)),
                     ("price_outlook", lambda: derive_price_outlook(long, ctx)),
                     ("us_career", lambda: derive_us_career(long, ctx))]:
        derived[name] = timed(name, fn)
    derived["country_snapshot"] = timed("country_snapshot", lambda: derive_snapshot(
        long, ctx, derived["couple_budget"], derived["life_events"], derived["earnings_ppp"]))

    status_df = pd.DataFrame(status, columns=["module", "source", "status", "rows", "error"])
    missing = missing_countries(derived)
    if missing and not a.only:
        # Writing these tables would silently drop countries from the app (as a FRED outage
        # once dropped the United States). Keep whatever the last good build left in --out.
        pd.DataFrame(f.log_rows).to_csv(a.out / "fetch_log.csv", index=False)
        print("\nSummary")
        print(status_df.to_string(index=False))
        print_failed_requests(f.log_rows)
        log.error("BUILD FAILED: no earnings profile and/or tax curve for %s. The app would "
                  "not offer these countries, so nothing was written (a previous build in %s "
                  "is untouched). Check the failed sources above; FRED feeds US earnings "
                  "(set FRED_API_KEY to use its API).",
                  ", ".join(f"{COUNTRIES[c][2]} ({c})" for c in missing), a.out)
        return 1
    if missing:
        log.warning("--only build: no earnings/tax rows for %s", ", ".join(missing))

    t0 = time.time()
    stamp = a.out / ".data-version"
    stamp.unlink(missing_ok=True)  # an interrupted write leaves no stamp -> run.py rebuilds
    if not a.derive_only:  # unchanged in --derive-only mode (and ~1 min to write)
        write(long, a.out / "econ_long")
        write(to_annual(long), a.out / "econ_annual")
    refs.to_csv(a.out / "reference_values.csv", index=False)
    for name, df in derived.items():
        if not df.empty:
            write(df, a.out / name)
    pd.DataFrame(f.log_rows).to_csv(a.out / "fetch_log.csv", index=False)
    timings.append(("write CSVs", time.time() - t0))

    print("\nTimings (derived steps and writes)")
    for n, t in timings:
        print(f"  {n:<24} {t:7.1f}s")
    net = pd.DataFrame(f.log_rows)
    if not net.empty and "elapsed_s" in net:
        net["host"] = net["url"].map(lambda u: urlparse(u).netloc)
        by_host = net.groupby("host")["elapsed_s"].agg(["count", "sum", "max"]) \
            .sort_values("sum", ascending=False).head(8)
        print("\nNetwork time by host (s)")
        print(by_host.round(1).to_string())
    print(f"\nTotal derive+write: {time.time() - t_derive:.1f}s")

    print("\nSummary")
    print(status_df.to_string(index=False))
    print_failed_requests(f.log_rows)
    cov = long.groupby("measure")["iso3"].nunique()
    print(f"\nCountries covered per measure (of {len(COUNTRIES)}):")
    print(cov.to_string())
    if not a.only:
        stamp.write_text(DATA_VERSION + "\n")
    print(f"\nWrote {a.out.resolve()}")
    failed = status_df[status_df["status"] != "ok"]
    if not failed.empty:
        log.warning("%d source(s) failed (%s); fallbacks were used. Every country still has "
                    "earnings and tax data.", len(failed), ", ".join(failed["source"]))
        return 2
    return 0


def missing_countries(derived: dict[str, pd.DataFrame]) -> list[str]:
    """Countries the app would not offer: it needs earnings_profile and tax_curve rows."""
    have = None
    for name in ("earnings_profile", "tax_curve"):
        df = derived.get(name)
        got = set(df["iso3"]) if df is not None and "iso3" in df else set()
        have = got if have is None else have & got
    return sorted(set(COUNTRIES) - have)


def print_failed_requests(log_rows: list[dict]) -> None:
    """Requests that never got data from the network (timeouts, errors, skipped hosts)."""
    net = pd.DataFrame(log_rows)
    if net.empty:
        return
    ok = set(net.loc[net["how"].isin(["cache", "network"]), "url"])
    bad = net[~net["how"].isin(["cache", "network"]) & ~net["url"].isin(ok)] \
        .drop_duplicates("url", keep="last")
    if bad.empty:
        return
    bad = bad.assign(host=bad["url"].map(lambda u: urlparse(u).netloc))
    print(f"\nRequests that got no fresh data ({len(bad)}; stale cache or nothing used)")
    for (host, how), g in bad.groupby(["host", "how"]):
        labels = [str(x) for x in g["label"]]
        more = f" (+{len(labels) - 6} more)" if len(labels) > 6 else ""
        print(f"  {host:<28} {how:<12} {len(g):>3}  {', '.join(labels[:6])}{more}")


if __name__ == "__main__":
    sys.exit(main())

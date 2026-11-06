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
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode, urlparse

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
DATA_VERSION = "2"

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
    dict(iso3="USA", topic="rent", item="median_gross_rent", value=1487, unit="USD/month",
         as_of="2024", quality="A", source="Census ACS 2024 1-year, table B25064",
         url="https://data.census.gov/table/ACSDT1Y2024.B25064",
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
    return frame(d, measure="price_level", frequency="A", source="World Bank WDI",
                 comparability_note="Consumer price index, national definitions.")


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


def unemployment_oecd(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = oecd(f, "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M", UNE_DIMS,
              {"REF_AREA": ["USA", "CAN", "GBR"], "MEASURE": ["UNE_LF_M"],
               "UNIT_MEASURE": ["PT_LF_SUB"], "TRANSFORMATION": ["_Z"], "SEX": ["_T"],
               "AGE": ["Y15T24", "Y_GE25", "Y_GE15"], "ACTIVITY": ["_Z"], "FREQ": ["A"]},
              since, version="1.0")
    df["_rank"] = df["ADJUSTMENT"].map({"Y": 0, "N": 1}).fillna(2)
    df = df.sort_values("_rank").drop_duplicates(["REF_AREA", "AGE", "TIME_PERIOD"])
    out = pd.DataFrame({"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"],
                        "value": df["OBS_VALUE"], "age_band": df["AGE"],
                        "series": "DF_IALFS_UNE_M"})
    return frame(out, measure="unemployment_rate", statistic="rate", frequency="A",
                 unit="% of labour force", source="OECD IALFS", comparability_note=NOTES["une_oecd"])


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
    return frame(out, measure="saving_rate_net", statistic="rate", frequency="A",
                 unit="% of net disposable income", source="OECD NAAG",
                 comparability_note=NOTES["sav_net"])


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
              "CP044", "CP045", "CP05", "CP06", "CP07", "CP071", "CP072", "CP0722", "CP073",
              "CP08", "CP09", "CP10", "CP11", "CP12", "CP125"]


def hicp_eurostat(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    df = eurostat(f, "prc_hicp_aind", optional={"unit": ["INX_A_AVG"], "coicop": HICP_CODES},
                  since=since, geo=EU_GEOS)
    df = df[df["unit"] == "INX_A_AVG"] if "unit" in df else df
    out = pd.DataFrame({"iso3": df["iso3"], "period": df["time"], "value": df["value"],
                        "statistic": "hicp_index", "category": df["coicop"],
                        "unit": "index", "series": "prc_hicp_aind:" + df["coicop"]})
    return frame(out, measure="price_level", frequency="A", source="Eurostat HICP",
                 comparability_note="Harmonised index of consumer prices, annual average.")


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
    return frame(out, measure="housing", frequency="A", source="Eurostat HPI",
                 comparability_note="House price index, all dwellings, purchase prices.")


CENSUS = "https://api.census.gov/data/{y}/acs/acs1"


def housing_us_acs(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    # The Census API now answers key-less requests with an HTML "Missing Key" page
    # (HTTP 200), which used to be cached as data and then silently skipped.
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("Census API now requires a key: get one free at "
                         "https://api.census.gov/data/key_signup.html and set CENSUS_API_KEY. "
                         "US rent falls back to reference_values.csv (ACS 2024 median).")
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


FRED_HOUSING = {
    "HSFMEDUSM052N": ("median_existing_single_family_price", "USD"),
    "MORTGAGE30US": ("mortgage_rate_30y_fixed", "%"),
    "APU000072610": ("electricity_price", "USD/kWh"),
    "APU000072620": ("utility_gas_price", "USD/therm"),
    "GASREGW": ("gasoline_price", "USD/gallon"),
}


def housing_energy_us(f: CachedFetcher, since: int | None) -> pd.DataFrame:
    parts = []
    for sid, (stat, unit) in FRED_HOUSING.items():
        try:
            d = fred(f, sid)
        except FetchError as e:
            log.warning("FRED %s skipped: %s", sid, e)
            continue
        meas = "housing" if stat.startswith(("median", "mortgage")) else "energy_prices"
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
                                    "BoC posted rates exceed typical discounted rates.")


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
        wb = pl[pl["source"] == "World Bank WDI"]
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


def _budget_eu(long, ctx):
    hbs = long[long["measure"] == "household_spending_raw"]
    out = []
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
    return out


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
                   ("OECD unemployment", unemployment_oecd), ("Eurostat JVS", vacancies_europe),
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
                       ("US housing & energy", housing_energy_us),
                       ("Canada housing & fuel", housing_canada),
                       ("BoE mortgage rate", housing_uk_boe),
                       ("ECB mortgage rates", mortgage_rates_ecb),
                       ("SSB/SNB mortgage rates", mortgage_rates_no_ch),
                       ("Eurostat energy prices", energy_prices_eu),
                       ("World Bank health", health_worldbank)],
    "life_events": [("OECD childcare", childcare_oecd),
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
                     ("context_series", lambda: derive_context(long, ctx))]:
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

"""Countries, currencies and the comparability notes attached to every row."""

from __future__ import annotations

from pathlib import Path

# iso3 -> (iso2, Eurostat geo code, name, region)
COUNTRIES: dict[str, tuple[str, str, str, str]] = {
    "AUT": ("AT", "AT", "Austria", "EU"),
    "BEL": ("BE", "BE", "Belgium", "EU"),
    "BGR": ("BG", "BG", "Bulgaria", "EU"),
    "HRV": ("HR", "HR", "Croatia", "EU"),
    "CYP": ("CY", "CY", "Cyprus", "EU"),
    "CZE": ("CZ", "CZ", "Czechia", "EU"),
    "DNK": ("DK", "DK", "Denmark", "EU"),
    "EST": ("EE", "EE", "Estonia", "EU"),
    "FIN": ("FI", "FI", "Finland", "EU"),
    "FRA": ("FR", "FR", "France", "EU"),
    "DEU": ("DE", "DE", "Germany", "EU"),
    "GRC": ("GR", "EL", "Greece", "EU"),
    "HUN": ("HU", "HU", "Hungary", "EU"),
    "IRL": ("IE", "IE", "Ireland", "EU"),
    "ITA": ("IT", "IT", "Italy", "EU"),
    "LVA": ("LV", "LV", "Latvia", "EU"),
    "LTU": ("LT", "LT", "Lithuania", "EU"),
    "LUX": ("LU", "LU", "Luxembourg", "EU"),
    "MLT": ("MT", "MT", "Malta", "EU"),
    "NLD": ("NL", "NL", "Netherlands", "EU"),
    "POL": ("PL", "PL", "Poland", "EU"),
    "PRT": ("PT", "PT", "Portugal", "EU"),
    "ROU": ("RO", "RO", "Romania", "EU"),
    "SVK": ("SK", "SK", "Slovakia", "EU"),
    "SVN": ("SI", "SI", "Slovenia", "EU"),
    "ESP": ("ES", "ES", "Spain", "EU"),
    "SWE": ("SE", "SE", "Sweden", "EU"),
    "ISL": ("IS", "IS", "Iceland", "EFTA"),
    "LIE": ("LI", "LI", "Liechtenstein", "EFTA"),
    "NOR": ("NO", "NO", "Norway", "EFTA"),
    "CHE": ("CH", "CH", "Switzerland", "EFTA"),
    "GBR": ("GB", "UK", "United Kingdom", "UK"),
    "USA": ("US", "US", "United States", "North America"),
    "CAN": ("CA", "CA", "Canada", "North America"),
}


EUROPE = [k for k, v in COUNTRIES.items() if v[3] in ("EU", "EFTA", "UK")]


EUROSTAT_TO_ISO3 = {v[1]: k for k, v in COUNTRIES.items()}


ISO2_TO_ISO3 = {v[0]: k for k, v in COUNTRIES.items()}


ISO2_TO_ISO3["EL"] = "GRC"


ISO2_TO_ISO3["UK"] = "GBR"


OECD_MEMBERS = {
    "AUT",
    "BEL",
    "CZE",
    "DNK",
    "EST",
    "FIN",
    "FRA",
    "DEU",
    "GRC",
    "HUN",
    "IRL",
    "ITA",
    "LVA",
    "LTU",
    "LUX",
    "NLD",
    "POL",
    "PRT",
    "SVK",
    "SVN",
    "ESP",
    "SWE",
    "ISL",
    "NOR",
    "CHE",
    "GBR",
    "USA",
    "CAN",
}


LOCAL_CURRENCY = {
    "USA": "USD",
    "CAN": "CAD",
    "GBR": "GBP",
    "CHE": "CHF",
    "NOR": "NOK",
    "ISL": "ISK",
    "SWE": "SEK",
    "DNK": "DKK",
    "POL": "PLN",
    "CZE": "CZK",
    "HUN": "HUF",
    "ROU": "RON",
    "LIE": "CHF",
    # Bulgaria adopted the euro on 1 Jan 2026, but every historical series
    # (World Bank FX/PPP, Eurostat NAC) is in lev: values stay in BGN
    # (1 EUR = 1.95583 BGN, fixed).
    "BGR": "BGN",
}


ROOT = Path(__file__).resolve().parent.parent  # the repository

# the euro area in 2026 (Bulgaria joined on 1 January 2026)
EURO_AREA = {
    "AUT",
    "BEL",
    "BGR",
    "HRV",
    "CYP",
    "EST",
    "FIN",
    "FRA",
    "DEU",
    "GRC",
    "IRL",
    "ITA",
    "LVA",
    "LTU",
    "LUX",
    "MLT",
    "NLD",
    "PRT",
    "SVK",
    "SVN",
    "ESP",
}
# countries whose statistics are in euro (Bulgaria's are in lev: see LOCAL_CURRENCY)
EURO_DATA = EURO_AREA - {"BGR"}

USER_AGENT = "econ-dataset-builder/2.0 (+python-requests; personal research)"


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

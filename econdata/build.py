"""Build the dataset the simulation reads: python build_econ_dataset.py [options]

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
import logging
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import pandas as pd

from econdata.config import COUNTRIES
from econdata.derive.budget import (
    derive_budget_coverage,
    derive_couple_budget,
    derive_price_outlook,
)
from econdata.derive.context import Ctx
from econdata.derive.earnings import (
    derive_earnings_ppp,
    derive_earnings_profile,
    derive_education_pay,
    derive_mobility,
    derive_real_wages,
    derive_ses_nowcast,
    derive_us_career,
)
from econdata.derive.history import (
    derive_eu_history,
    derive_part_time,
    derive_us_history,
    hfcs_tables,
)
from econdata.derive.labour import derive_labour_market
from econdata.derive.life import (
    derive_family,
    derive_life_events,
    derive_pensions,
    derive_university,
)
from econdata.derive.macro import (
    derive_context,
    derive_fx_ppp,
    derive_market_index,
    derive_mortgage_longrun,
    derive_rates,
    derive_us_states,
)
from econdata.derive.snapshot import derive_snapshot
from econdata.derive.tax import derive_expat_tax, derive_tax_curve
from econdata.derive.wealth import derive_cohort, derive_portfolio, derive_start_wealth
from econdata.fetch import CachedFetcher
from econdata.references import REFERENCE_VALUES
from econdata.sources.cohorts import (
    cohort_eu_lfs,
    cohort_us_asec,
    cohort_us_scf,
    part_time_europe,
    scf_hfcs_tables,
    scf_households,
    vacancies_canada,
    vacancies_europe,
    vacancies_uk,
    vacancies_us,
)
from econdata.sources.energy_health import energy_prices_eu, health_worldbank
from econdata.sources.history import history_europe, history_us
from econdata.sources.housing import (
    house_price_index_eu,
    house_prices_bis,
    housing_canada,
    housing_costs_silc,
    housing_energy_us,
    housing_uk_boe,
    housing_us_acs,
    mortgage_rates_ecb,
    mortgage_rates_no_ch,
    rent_index_eu,
    us_states_api,
)
from econdata.sources.income import (
    avg_salary_eurostat,
    avg_wage_oecd,
    cpi_worldbank,
    earnings_canada_lfs,
    earnings_europe_ses,
    earnings_uk_ashe,
    earnings_us_cps,
    income_mobility_canada,
    income_oecd_idd,
    income_silc_by_age,
    income_silc_by_household,
    income_silc_by_urbanisation,
    income_silc_distribution,
    income_transitions_eu,
    net_income_eurostat,
    net_income_taxing_wages,
    wage_growth_us,
)
from econdata.sources.labour import (
    career_us_cps,
    education_oecd,
    involuntary_pt_oecd,
    labour_us_cps,
    overqualification_europe,
    slack_canada,
    slack_europe,
    slack_uk,
    unemp_by_education_europe,
    unemployment_europe,
    unemployment_europe_monthly,
    unemployment_oecd,
)
from econdata.sources.life import (
    childcare_oecd,
    coverage_us,
    deposit_rates_ecb,
    deposit_rates_us,
    emergency_eu,
    family_canada,
    family_us_cps,
    family_work_europe,
    hbs_coverage_eu,
    household_assets_oecd,
    interest_rates_eu,
    interest_rates_oecd,
    job_loss_replacement_oecd,
    leave_home_europe,
    pensions_oecd,
    retirement_oecd,
    university_cost_oecd,
)
from econdata.sources.prices import price_levels_eurostat, price_levels_worldbank
from econdata.sources.saving import household_assets_typical_ecb, saving_eurostat, saving_oecd
from econdata.sources.spending import (
    hicp_eurostat,
    household_cost_us,
    inflation_outlook_us,
    prices_us_cpi_parts,
    spending_canada_mbm,
    spending_canada_shs,
    spending_eu_hbs,
    spending_uk_family,
    spending_us_ce,
)
from econdata.sources.uncertainty import uncertainty_wgi, uncertainty_wui
from econdata.tidy import TIDY_COLS
from econdata.version import DATA_VERSION
from econsim.calibrate import write_levels

log = logging.getLogger("econ")


MODULES: dict[str, list[tuple[str, Callable]]] = {
    "income": [
        ("Eurostat SES", earnings_europe_ses),
        ("ONS ASHE", earnings_uk_ashe),
        ("BLS CPS", earnings_us_cps),
        ("StatCan LFS wages", earnings_canada_lfs),
        ("EU-SILC by age", income_silc_by_age),
        ("EU-SILC by household", income_silc_by_household),
        ("EU-SILC by urbanisation", income_silc_by_urbanisation),
        ("EU-SILC percentiles", income_silc_distribution),
        ("OECD IDD", income_oecd_idd),
        ("OECD Taxing Wages", net_income_taxing_wages),
        ("Eurostat net earnings", net_income_eurostat),
    ],
    "wage_history": [
        ("OECD average wages", avg_wage_oecd),
        ("Eurostat FTE salary", avg_salary_eurostat),
    ],
    "mobility": [
        ("Atlanta Fed WGT", wage_growth_us),
        ("EU-SILC transitions", income_transitions_eu),
        ("StatCan income mobility", income_mobility_canada),
    ],
    "job_market": [
        ("Eurostat unemployment", unemployment_europe),
        ("OECD unemployment", unemployment_oecd),
        ("Eurostat monthly unemployment", unemployment_europe_monthly),
        ("Eurostat unemployment by education", unemp_by_education_europe),
        ("Eurostat labour-market slack", slack_europe),
        ("Eurostat over-qualification", overqualification_europe),
        ("OECD Education at a Glance", education_oecd),
        ("OECD involuntary part-time", involuntary_pt_oecd),
        ("StatCan supplementary rates", slack_canada),
        ("ONS discouraged workers", slack_uk),
        ("CPS microdata (US)", labour_us_cps),
        ("CPS careers (US)", career_us_cps),
        ("CPS ASEC cohorts (US)", cohort_us_asec),
        ("SCF wealth by age (US)", cohort_us_scf),
        ("ECB typical household portfolios", household_assets_typical_ecb),
        ("US history (prices, rates, returns)", history_us),
        ("European stock returns (history)", history_europe),
        ("LFS unemployment cohorts (EU)", cohort_eu_lfs),
        ("LFS part-time by age and education (EU)", part_time_europe),
        ("Eurostat JVS", vacancies_europe),
        ("JOLTS", vacancies_us),
        ("StatCan JVWS", vacancies_canada),
        ("ONS vacancies", vacancies_uk),
    ],
    "saving": [("OECD saving", saving_oecd), ("Eurostat saving", saving_eurostat)],
    "cost_of_living": [
        ("World Bank PPP", price_levels_worldbank),
        ("World Bank CPI", cpi_worldbank),
        ("Eurostat PPP/PLI", price_levels_eurostat),
        ("Eurostat HICP", hicp_eurostat),
        ("Eurostat HBS", spending_eu_hbs),
        ("BLS CE", spending_us_ce),
        ("StatCan SHS", spending_canada_shs),
        ("ONS Family Spending", spending_uk_family),
        ("StatCan MBM", spending_canada_mbm),
        ("EU-SILC housing costs", housing_costs_silc),
        ("Eurostat HPI", house_price_index_eu),
        ("Census ACS", housing_us_acs),
        ("Eurostat rent index", rent_index_eu),
        ("BIS house prices", house_prices_bis),
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
        ("US household cost index", household_cost_us),
        ("FDIC deposit rates", deposit_rates_us),
        ("OECD household assets", household_assets_oecd),
        ("Eurostat energy prices", energy_prices_eu),
        ("World Bank health", health_worldbank),
    ],
    "life_events": [
        ("OECD childcare", childcare_oecd),
        ("Eurostat mothers' work", family_work_europe),
        ("Eurostat leaving home", leave_home_europe),
        ("CPS families (US)", family_us_cps),
        ("StatCan families", family_canada),
        ("HBS coverage (EU)", hbs_coverage_eu),
        ("CE coverage (US)", coverage_us),
        ("OECD pensions", pensions_oecd),
        ("OECD retirement ages", retirement_oecd),
        ("OECD university costs", university_cost_oecd),
        ("OECD job-loss replacement", job_loss_replacement_oecd),
        ("EU-SILC emergencies", emergency_eu),
    ],
    "uncertainty": [("WUI", uncertainty_wui), ("WGI", uncertainty_wgi)],
}


# derived tables need price levels; pulled automatically when other modules run
ALWAYS = [("World Bank PPP", price_levels_worldbank), ("World Bank CPI", cpi_worldbank)]


def to_annual(long: pd.DataFrame) -> pd.DataFrame:
    """Average sub-annual series to years. Annual (and multi-year) rows pass straight
    through; only M/Q/S rows are grouped, on factorised integer keys (much faster than
    a 16-column object groupby over the whole table)."""
    keys = [c for c in TIDY_COLS if c not in ("period", "frequency", "value", "method")]
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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="econ_data", type=Path)
    ap.add_argument("--cache", default=None, type=Path, help="cache folder (default <out>/.cache)")
    ap.add_argument(
        "--ttl-days",
        default=30.0,
        type=float,
        help="re-download cached files older than this (default 30)",
    )
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--since", default=2005, type=int, help="first year (default 2005)")
    ap.add_argument("--only", nargs="+", choices=list(MODULES), help="subset of modules")
    ap.add_argument(
        "--home-m2",
        default=100.0,
        type=float,
        help="home size used for European price-per-m2 (default 100)",
    )
    ap.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="CSV overriding reference_values.csv rows (same columns)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument(
        "--derive-only",
        action="store_true",
        help="skip downloads; rebuild derived tables from econ_long.csv",
    )
    ap.add_argument(
        "--profile",
        action="store_true",
        help="run under cProfile and print the 25 most expensive functions",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if a.profile:
        import cProfile
        import pstats

        prof = cProfile.Profile()
        argv2 = [x for x in (argv if argv is not None else sys.argv[1:]) if x != "--profile"]
        rc = prof.runcall(main, argv2)
        pstats.Stats(prof).sort_stats("cumulative").print_stats(25)
        return rc
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )

    a.out.mkdir(parents=True, exist_ok=True)
    f = CachedFetcher(a.cache or a.out / ".cache", ttl_days=a.ttl_days, refresh=a.refresh)
    refs = load_references(a.reference)
    status: list[tuple] = []
    if a.derive_only:  # rebuild derived tables from the last econ_long.csv, no network
        frames = [previous_long(a.out)]
    else:
        frames = fetch_sources(f, sources_to_run(a.only), a.since, status)
    if not frames:
        log.error("Nothing fetched.")
        return 1
    t_derive = time.time()
    long = assemble(frames)
    timings: list[tuple[str, float]] = []
    derived = derive_tables(long, refs, f, a.home_m2, status, timings)

    missing = missing_countries(derived)
    if missing and not a.only:
        # Writing these tables would silently drop countries from the app (as a FRED outage
        # once dropped the United States). Keep whatever the last good build left in --out.
        pd.DataFrame(f.log_rows).to_csv(a.out / "fetch_log.csv", index=False)
        print_summary(status, f.log_rows)
        log.error(
            "BUILD FAILED: no earnings profile and/or tax curve for %s. The app would "
            "not offer these countries, so nothing was written (a previous build in %s "
            "is untouched). Check the failed sources above; FRED feeds US earnings "
            "(set FRED_API_KEY to use its API).",
            ", ".join(f"{COUNTRIES[c][2]} ({c})" for c in missing),
            a.out,
        )
        return 1
    if missing:
        log.warning("--only build: no earnings/tax rows for %s", ", ".join(missing))

    stamp = a.out / ".data-version"
    stamp.unlink(missing_ok=True)  # an interrupted write leaves no stamp -> run.py rebuilds
    write_tables(a.out, long, refs, derived, f.log_rows, timings, a.derive_only)
    calibrate_levels(a.out, status, timings)

    print_timings(timings, f.log_rows, time.time() - t_derive)
    print_summary(status, f.log_rows)
    cov = long.groupby("measure")["iso3"].nunique()
    print(f"\nCountries covered per measure (of {len(COUNTRIES)}):")
    print(cov.to_string())
    if not a.only:
        stamp.write_text(DATA_VERSION + "\n")
    print(f"\nWrote {a.out.resolve()}")
    failed = [s for s in status if s[2] != "ok"]
    if failed:
        log.warning(
            "%d source(s) failed (%s); fallbacks were used. Every country still has "
            "earnings and tax data.",
            len(failed),
            ", ".join(s[1] for s in failed),
        )
        return 2
    return 0


def sources_to_run(only: list[str] | None) -> list[tuple[str, str, Callable]]:
    """(module, source name, fetch function) for the chosen modules, plus the price
    levels every derived table needs."""
    plan: list[tuple[str, str, Callable]] = []
    for mod in only or list(MODULES):
        plan += [(mod, n, fn) for n, fn in MODULES[mod]]
    for n, fn in ALWAYS:
        if not any(fn is p[2] for p in plan):
            plan.append(("cost_of_living", n, fn))
    return plan


def fetch_sources(
    f: CachedFetcher, plan: list[tuple[str, str, Callable]], since: int, status: list[tuple]
) -> list[pd.DataFrame]:
    """Run every source; one that fails is reported in `status` and the rest go on."""
    frames = []
    for mod, name, fn in plan:
        t0 = time.time()
        try:
            df = fn(f, since)
            frames.append(df)
            status.append((mod, name, "ok", len(df), ""))
            log.info("%-26s %7d rows  (%.1fs)", name, len(df), time.time() - t0)
        except Exception as e:  # keep going; report at the end
            status.append((mod, name, "FAILED", 0, f"{type(e).__name__}: {str(e)[:200]}"))
            log.error("%-26s FAILED: %s", name, e)
    return frames


def previous_long(out: Path) -> pd.DataFrame:
    """The tidy table of the last build (without its nowcast rows, which are re-derived)."""
    prev = pd.read_csv(out / "econ_long.csv", low_memory=False)
    prev = prev[~prev["source"].astype(str).str.endswith("(nowcast)")]
    prev["year"] = pd.to_numeric(prev["year"], errors="coerce").astype("Int64")
    for c in ("period", "age_band", "category", "household_type", "area_type", "unit"):
        prev[c] = prev[c].astype(str).replace({"nan": pd.NA, "<NA>": pd.NA})
    return prev


def assemble(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """All sources' rows in one tidy table (covered countries and the euro area), with the
    earnings nowcast added."""
    long = pd.concat(frames, ignore_index=True)
    long = long[long["iso3"].isin(COUNTRIES) | (long["iso3"] == "EMU")]
    long = pd.concat([long, derive_ses_nowcast(long)], ignore_index=True)
    long["estimated"] = long["estimated"].fillna(False).astype(bool)
    return long.reset_index(drop=True)


def derive_tables(
    long: pd.DataFrame,
    refs: pd.DataFrame,
    f: CachedFetcher,
    home_m2: float,
    status: list[tuple],
    timings: list[tuple[str, float]],
) -> dict[str, pd.DataFrame]:
    """Every derived table; one that fails is empty and reported in `status`."""

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
    steps = [
        ("earnings_ppp", lambda: derive_earnings_ppp(long, ctx)),
        ("real_wage_history", lambda: derive_real_wages(long, ctx)),
        ("couple_budget", lambda: derive_couple_budget(long, ctx)),
        ("life_events", lambda: derive_life_events(long, ctx, home_m2)),
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
        ("us_career", lambda: derive_us_career(long, ctx)),
        ("cohort", lambda: derive_cohort(long, ctx)),
        ("start_wealth", lambda: derive_start_wealth(long, ctx)),
        ("us_history", lambda: derive_us_history(long, ctx)),
        ("eu_history", lambda: derive_eu_history(long, ctx)),
        ("hfcs_tables", lambda: hfcs_tables(long)),
        ("scf_tables", lambda: scf_hfcs_tables(f)),
        ("part_time", lambda: derive_part_time(long, ctx)),
        ("scf_households", lambda: scf_households(f)),
    ]
    derived = {name: timed(name, fn) for name, fn in steps}
    derived["country_snapshot"] = timed(
        "country_snapshot",
        lambda: derive_snapshot(
            long, ctx, derived["couple_budget"], derived["life_events"], derived["earnings_ppp"]
        ),
    )
    return derived


def write_tables(
    out: Path,
    long: pd.DataFrame,
    refs: pd.DataFrame,
    derived: dict[str, pd.DataFrame],
    log_rows: list[dict],
    timings: list[tuple[str, float]],
    derive_only: bool,
) -> None:
    t0 = time.time()
    if not derive_only:  # unchanged in --derive-only mode (and ~1 min to write)
        write(long, out / "econ_long")
        write(to_annual(long), out / "econ_annual")
    refs.to_csv(out / "reference_values.csv", index=False)
    for name, df in derived.items():
        if not df.empty:
            write(df, out / name)
    pd.DataFrame(log_rows).to_csv(out / "fetch_log.csv", index=False)
    timings.append(("write CSVs", time.time() - t0))


def calibrate_levels(out: Path, status: list[tuple], timings: list[tuple[str, float]]) -> None:
    """Each country's spending level: the simulated population saves the national rate
    (needs the tables just written; econsim.calibrate)."""
    t0 = time.time()
    try:
        lv = write_levels(str(out))
        status.append(("derived", "spending_level", "ok", len(lv), ""))
    except Exception as e:  # noqa: BLE001 - the app falls back to level 1
        status.append(
            ("derived", "spending_level", "FAILED", 0, f"{type(e).__name__}: {str(e)[:200]}")
        )
        log.exception("spending level calibration failed")
    timings.append(("spending levels", time.time() - t0))


def print_timings(timings: list[tuple[str, float]], log_rows: list[dict], total: float) -> None:
    print("\nTimings (derived steps and writes)")
    for n, t in timings:
        print(f"  {n:<24} {t:7.1f}s")
    net = pd.DataFrame(log_rows)
    if not net.empty and "elapsed_s" in net:
        net["host"] = net["url"].map(lambda u: urlparse(u).netloc)
        by_host = (
            net.groupby("host")["elapsed_s"]
            .agg(["count", "sum", "max"])
            .sort_values("sum", ascending=False)
            .head(8)
        )
        print("\nNetwork time by host (s)")
        print(by_host.round(1).to_string())
    print(f"\nTotal derive+write: {total:.1f}s")


def print_summary(status: list[tuple], log_rows: list[dict]) -> None:
    print("\nSummary")
    print(
        pd.DataFrame(status, columns=["module", "source", "status", "rows", "error"]).to_string(
            index=False
        )
    )
    print_failed_requests(log_rows)


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
    bad = net[~net["how"].isin(["cache", "network"]) & ~net["url"].isin(ok)].drop_duplicates(
        "url", keep="last"
    )
    if bad.empty:
        return
    bad = bad.assign(host=bad["url"].map(lambda u: urlparse(u).netloc))
    print(f"\nRequests that got no fresh data ({len(bad)}; stale cache or nothing used)")
    for (host, how), g in bad.groupby(["host", "how"]):
        labels = [str(x) for x in g["label"]]
        more = f" (+{len(labels) - 6} more)" if len(labels) > 6 else ""
        print(f"  {host:<28} {how:<12} {len(g):>3}  {', '.join(labels[:6])}{more}")

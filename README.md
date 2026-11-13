# Economic Calculator and Planner

"What would the financial picture of our life look like in country X?" for EU27 +
EFTA + UK, the US and Canada: a couple's earnings versus living costs, monthly
savings, and savings over a lifetime, including the US citizenship-based tax layer.

```
python run.py                  # first time: venv + packages + data + tests, then starts the app
python run.py setup            # set up only;  `python run.py start` starts without the data/test steps
python run.py test             # engine tests + headless UI smoke tests
python run.py data [--refresh] # rebuild the data (30-day download cache; --refresh ignores it)
```

`run.py` uses only the standard library and works on macOS, Linux and Windows (use
`python3` on macOS/Linux or `py` on Windows if `python` isn't on PATH). Python 3.10+.

Settings (port, host, Census key, ...) are in `config.env`; `example.config.env` documents
them and is copied to `config.env` on first run. To host it on a network, set
`HOST=0.0.0.0` and `OPEN_BROWSER=false`; behind a reverse proxy, forward WebSockets
(Streamlit uses `/_stcore/stream`) and optionally set `BASE_URL_PATH`. Restart the app
after `python run.py data` so it loads the new tables.

### Docker (website mode)

`compose.yaml` runs the app plus a Cloudflare Tunnel, both restarting automatically
whenever Docker runs. Put `TUNNEL_TOKEN` in `config.env`, then:

```
docker compose up -d --build     # first start builds the data into a volume (~10-20 min)
docker compose logs -f econ      # watch progress
docker compose down              # stop (data volume kept; `down -v` deletes it)
```

The container runs `python run.py`, so setup behaves exactly as above. Inside the
container `HOST` and `PORT` (8501) come from `compose.yaml`; everything else comes from `config.env`. In the Cloudflare dashboard, the
tunnel's public hostname uses path in `config.env` and service `http://econ:8501`. To refresh the
data: `docker compose exec econ python run.py data`, then `docker compose restart econ`.

### Deploying (automatic updates with doco-cd)

On the machine that hosts the site, [doco-cd](https://github.com/kimdre/doco-cd) keeps the
stack on the latest stable release of the repo it follows (this one, or your fork). Publishing a GitHub release runs the tests
([`.github/workflows/release.yml`](.github/workflows/release.yml)) and, if they pass,
moves the `release` branch to it. doco-cd polls that branch and runs the equivalent of
`docker compose up -d --build`, so code, `Dockerfile` and `compose.yaml` changes all
apply. Plain commits and pre-releases deploy nothing. The data volume is kept, and
cloudflared is updated to its latest image at each deploy. The container runs the full
test suite on its first start with each new version. A release that bumps `DATA_VERSION` also rebuilds
the data on its first start (reusing cached downloads).

One doco-cd instance serves every project on the machine. Put this in a folder of its own
(e.g. `~/docker/doco-cd/`), outside any project:

```yaml
# compose.yaml
services:
  doco-cd:
    image: ghcr.io/kimdre/doco-cd:latest
    restart: unless-stopped
    environment:
      TZ: Etc/UTC                    # your time zone, for the log timestamps
      POLL_CONFIG: |
        - url: https://github.com/bmck039/economic-calculator.git   # or your fork
          reference: release
          interval: 15m
        # one entry per project
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - data:/data
      - ./secrets:/secrets:ro      # one env file per project, never in git
    healthcheck:
      start_period: 15s
      interval: 30s
      timeout: 5s
      retries: 3
volumes:
  data:
```

Copy this project's `config.env` to `secrets/econ-calculator.env` next to that file
([`.doco-cd.yml`](.doco-cd.yml) points the stack at it), then `docker compose up -d`.
doco-cd deploys the current release within a minute and then checks every 15 minutes.
`docker logs -f doco-cd-doco-cd-1` shows what it does. If a stack started by hand with
`docker compose up` is running, stop it first (`docker compose down`, which keeps the data).

Platform notes for the doco-cd host:

* **Linux:** nothing extra with Docker Engine; it starts at boot if the `docker` service
  is enabled (`sudo systemctl enable docker`).
* **macOS and Windows (Docker Desktop):** enable *Start Docker Desktop when you sign in*
  (Docker then runs only while you're signed in, so use automatic sign-in on a machine
  that should serve the site unattended). On macOS also enable *Settings → Advanced →
  Allow the default Docker socket to be used*. On Windows use the WSL 2 backend with Linux
  containers (the default); the `/var/run/docker.sock` mount works as written.

**To ship a change:** push it, then publish a GitHub release (e.g. tag `v0.2.0`).

Manual equivalent: `pip install -r requirements.txt`, `python build_econ_dataset.py`,
`streamlit run app.py`, `pytest -q`. Useful pipeline flags: `--derive-only` (rebuild the derived tables from the last
`econ_long.csv`, no network, ~2 min), `--profile` (cProfile top 25), `--refresh`,
`--reference my_refs.csv`. Set `CENSUS_API_KEY` (free) to get ACS rents and the US job-market figures (CPS microdata); without it the
US rent comes from `reference_values.csv` and US unemployment by degree from OECD tables.
Set `FRED_API_KEY` (free, [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html))
to read the US series (CPS earnings, JOLTS, CE spending, housing) through FRED's API;
without it they come from FRED's CSV endpoint.

The build exits 1, without touching the previous tables, if any country would end up
without earnings or tax data (the app could not offer it); it exits 2 when only some sources
failed and fell back to cached or reference values. A complete build stamps
`econ_data/.data-version`; `python run.py` rebuilds whenever that stamp is missing or older
than `DATA_VERSION` in `build_econ_dataset.py` (bump it when old tables must be rebuilt).
`FRED_SMOKE=1 pytest tests/test_fred.py` checks the live FRED endpoints.

## Layout

| Path | What |
|---|---|
| `build_econ_dataset.py` | Data pipeline: official sources -> tidy CSVs in `econ_data/` |
| `econsim/params/*.csv` | Curated, sourced rules and market levels (see below); ship with the code |
| `econsim/params.py` | Loads them |
| `econsim/model.py` | `Sourced` (value + provenance), `CountryData`, `Plan` |
| `econsim/data.py` | Builds `CountryData` from the derived CSVs + curated params (+ US state) |
| `econsim/tax.py` | Local tax curves, joint taxation, youth exemptions; US residents (federal, state, local, FICA); US expats (FEIE/FTC) |
| `econsim/engine.py` | Year-by-year simulation; `to_display()` for real/nominal and currency |
| `econsim/history.py`, `backtest.py`, `backtest_eu.py`, `calibrate.py` | What actually happened (US, Europe); backtests on SCF households and on the HFCS / SCF tables; spending levels fitted to national saving |
| `reference/hfcs_tables.csv`, `tools/hfcs_tables.py` | The ECB HFCS statistical tables (PDF only), compiled into one table, and the script that does it (`pdftotext`) |
| `app.py` | Streamlit + Plotly UI (no modelling code) |
| `tests/` | `test_engine.py` (synthetic country), `test_life.py` (retirement and life factors), `test_params.py` (curated rules), `test_invariants.py` (every country), `test_app.py` (AppTest) |

### Curated parameters (`econsim/params/`)

Things no statistics API publishes comparably. Every value has a source and a quality grade
(A statutory/official, B official but approximated, C estimate); the app's Sources tab lists them.

| File | Contents |
|---|---|
| `housing.csv` | New-lease market rents (~70 m², excl. utilities); existing-home prices (national median per m² × 85 m², or per home) |
| `lending.csv` | Minimum down payment (and for young buyers), payment-to-income / loan-to-income caps and stress rates, typical term, rate-fixation period and 2005-2025 average rate, transfer tax and closing costs, property tax, home insurance, mortgage-interest relief, borrower insurance |
| `household.csv` (+ `household_sources.csv`) | Cars (price, insurance, tax, fuel, km, maintenance, tolls, transit pass), health premiums and out-of-pocket costs, parental leave (months, replacement, caps), free pre-school and school ages, compulsory occupational pension contributions, pension replacement rate, employer match, investment tax drag, wealth tax, typical wedding cost, long-run real wage growth |
| `us_states.csv` | Health-premium and car-insurance factors and state paid family leave (curated). The ACS columns (recent-mover rents, home values, property taxes, full-time earnings) and BEA price parities are refreshed from the Census and FRED APIs by the pipeline; the CSV values are the fallback |
| `us_state_tax.csv` | 2026 state income tax brackets, deductions, exemptions (Tax Foundation), local income tax, state payroll taxes |

## Model in one paragraph

Every country runs the same model; only the data differ ("Same model in every country"
below). Each partner is, each year, full time in a job that needs a degree, full time in
another job, part time, or looking for work: today's mix for their age and degree at each
age they reach (or, from a situation they enter, moving back toward it as fast as people
really move), with pay growing as their generation's full-time pay does - today's age
brackets plus the part of their generation's gap that has lasted historically. A stated
current pay sets the starting level; economy-wide real wage growth and inflation come on
top. Taxes use the local average tax-rate curve with joint taxation where it exists (DE/LU/
PT/PL splitting, FR quotient familial), youth exemptions (PL, HU, HR, PT, GR) and compulsory
occupational pension contributions (CH, NL, DK, IS, LI); US residents get federal + state +
local tax, FICA, the child tax credit and the earned income tax credit, and Medicaid / CHIP
below 138% / 200% of the poverty line. Births trigger the country's parental leave;
afterwards the main parent works the country's typical employment rate × hours of mothers.
Living costs are the survey couple's basket raised to national-accounts household
consumption category by category, times the country's calibrated spending level, scaled by
(income after housing / the survey couple's)^0.8; rent, the home a buyer can afford
(^0.75) and car costs (^0.25) follow income before housing; children add per-person
spending (OECD-modified scale), childcare until free pre-school. Before retirement 4% of
savings above the emergency fund is spent each year (not by renters saving for a home). When money runs short, discretionary
spending is cut (up to half), then - beyond what a household can borrow unsecured, a
quarter of a year's income - all living costs (up to 30%). Buying waits until the down
payment, taxes and fees are saved (keeping 6 months' buffer) and the loan fits the
country's lending limits; owned homes follow the (repeat-sales) price index, which already
includes their ageing; US and Danish owners refinance when new loans cost a point less. Savings above a 6-month
emergency fund are held as TYPICAL households hold theirs (the bottom 70% by wealth: ECB
Distributional Wealth Accounts, US SCF): part in bank deposits at the rates banks pay, the
rest invested at today's 10-year yield + the share in shares × a 3.5% equity premium − fees
− the country's tax drag, the share in stocks falling to half by retirement. From the
retirement age for today's young workers, each partner gets a pension (career-average pay
relative to the average wage × the OECD replacement rate), indexed to prices; savings are
drawn down slowly until the longer-living partner's projected lifespan.

## Careers: today's job market, not full-time averages

Full-time median pay overstates what people starting out now earn: among US bachelor's
holders aged 20-24 who work or look for work (students excluded), only about half are in
full-time graduate-level jobs. Every partner, in every country, is therefore in one of four
situations - full time in a job that needs a degree, full time in another job, part time,
looking for work:

| Piece | United States | Other countries |
|---|---|---|
| Today's mix by age and degree | CPS basic monthly microdata, last 12 months | unemployment by age and degree (labour_market.csv), part-time share by age and education (Eurostat LFS `lfsa_epgaed`; under 25: the 25-49 share raised as in the US, where students are left out), graduates in lower-level jobs (LFS over-qualification) |
| Pay in each situation | CPS usual weekly earnings at the partner's percentile | the country's own full-time pay by age, percentile and degree, split with the US ratios of non-graduate and part-time to graduate full-time pay (so its full-time average is the country's) |
| How fast a stated situation moves back to the typical mix | the same CPS households a year apart; switches between graduate and other full-time jobs scaled to measured persistence (73% of graduates who start underemployed still are 10 years later: Strada / Burning Glass, *Talent Disrupted*, 2024) | the US moves (no country publishes matched panels) |
| Pay growth | the generation's full-time year-round pay (CPS ASEC, below) | the country's full-time pay by age × the generation gap (SES) |
| Job market ahead | "stays as now", or "keeps changing": the last year's change in the mix continues 5 years | stays as now |

Starting from the typical mix, the expected mix at each age is today's mix at that age:
the surveys show no lasting generation gaps in unemployment or full-time work (below). A
stated pay fades toward the chosen percentile (ranks correlate ~0.6 over 10 years,
Kopczuk-Saez-Song 2010).

## Generations: your cohort, not today's older brackets

Today's 35-year-olds are not a forecast of today's 33-year-olds in two years. If the
33-year-olds have been doing worse than the 35-year-olds did at the same age, survey after
survey, they will most likely still be behind at 35. `econsim/cohort.py` measures this for
every statistic it has a long series for:

* the level at each age in each survey year, relative to all 25-64-year-olds that year (so
  economy-wide growth and the business cycle drop out);
* each cohort's **gap** to the cohort h years older at the same ages, averaged over its last
  three surveys (a one-off bad year is not a lasting gap);
* how much of such gaps **lasted** h years later in past surveys (`phi`, least squares
  over every age and year where the outcome is known; only horizons where the gap and the
  outcome do not share survey respondents);
* the forecast: today's level at the older age + `phi` × the gap. In the first years
  everyone moves as people moving up today's age brackets; later, by as much of their
  generation's gap as history says lasts. "The whole gap lasts" and "none" are options.

| Statistic | Survey | Share of a gap still there after 5 / 10 years |
|---|---|---|
| Full-time year-round pay (US) | CPS ASEC, every year since 2003 | ~25% / ~45% |
| Household income (US) | CPS ASEC | ~23% / ~25% |
| Home ownership (US) | CPS ASEC; SCF | ~15% / ~22% |
| Financial assets (US) | SCF, every 3 years since 1989 | 35% after 9 years, ~70% after 15 |
| Unemployment, full-time share (US, Europe) | CPS ASEC; Eurostat LFS since 2000 | ~0: they follow age and the cycle, not the birth year |
| Pay (Europe) | Eurostat SES 2006-2022 by age band | measured gaps; persistence from the US estimate (four waves of 10-year bands cannot measure it) |

The backtest (`python -m econsim.validate`: estimated on surveys up to 2014 or 2016,
compared with what the cohorts then did) beats "you'll be where today's older bracket is"
in most cases, modestly - generation gaps are real but mostly small, except in wealth.

**Starting point by age.** Starting savings and retirement accounts default to the median
of couples whose head is that age, with that degree, renting or owning (Fed SCF 2025); in
the euro area, of households whose reference person is that age (ECB HFCS, latest wave:
2021; by age only, interpolated between its 10-year bands); elsewhere 0, enter yours. From the planned buying age, a household starts as an owner with
the median home and mortgage of owner couples that age, the 20-year average mortgage rate
and the term left implied by the balance.

`python -m econsim.validate` checks every starting age 20-64 for the property asked of the
forecast: pay in the first three years moves within ~1% of today's age brackets (median),
and the careers' unemployment agrees with the March supplement. Wealth and ownership are
not checked against today's older couples - another generation - but against what really
happened (next section).

## Backtest: start in the past, compare with what happened

### United States: the SCF's households

`python -m econsim.backtest` starts every couple in the Fed's Survey of Consumer Finances
with the head aged 25-60 in the 2004, 2007, ..., 2022 waves (about 10,000 households) from
their own ages, degree, children, wages, savings, retirement accounts, home, mortgage and
student debt, and runs them through what actually happened (`us_history.csv`, FRED, BLS
and the Kenneth French data library: the price of each cost category, rents, home prices,
the mortgage rate for new loans, stock and bond returns, deposit rates, unemployment), with
their own generation's wages (the median wages of couples of their birth years and degree
in each later wave). It then compares with the SAME birth years (5-year cohorts) and degree
in every later wave: median wealth (financial assets + home equity − student debt) and the
share who own - 359 cohort cells, never another generation.

That backtest found, and fixed, what a forward-looking check cannot see:

| Found | Fix |
|---|---|
| Wealth 1.9× too high after 3 years, 3× after 21 | spending: the survey basket raised to national-accounts totals in full (the US Consumer Expenditure Survey records 50-65% of what households spend on several categories) and a calibrated level; a wealth effect (4% of free savings a year, from Carroll, Otsuka & Slacalek 2011: 2-6 cents per dollar - the backtest cannot pin it down, as few couples hold much above the emergency fund) |
| Stocks: the model held 84% of investments in shares (the household sector, led by the wealthiest); typical families hold ~50-60% | portfolios of the bottom 70% by wealth (ECB DWA, SCF) |
| Low-income couples ran up six-figure debts | a borrowing limit (then living costs are cut), Medicaid / CHIP, the EITC, rent and cars that follow income |
| The SCF's public files state every wave in the latest survey's dollars (2025), not that year's | each wave converted back to its own year's dollars (CPI); an apparent "ageing" of owned homes (−0.3 to −2.5%/yr vs the price index) was this mistake - in nominal terms SCF owners' homes kept pace with the index (or gained, trading up), so homes now follow it |
| A price series missing its latest year (car insurance, 2025) silently broke the households that pay it | a missing year counts as the year before |

How strongly spending follows income (the budget elasticity) is fitted on both backtests
at once - this one and Europe's below - refitting every country's spending level to its
national saving rate at each value:

| Elasticity | US wealth bias / MAE | degree / no degree | Europe wealth bias / MAE |
|---|---|---|---|
| 0.6 | −0.54 / 0.84 | −0.61 / −0.48 | +0.17 / 0.91 |
| **0.8 (default)** | −0.36 / **0.78** | −0.60 / −0.11 | +0.28 / **0.89** |
| 1.0 | −0.27 / 0.78 | −0.59 / +0.07 | +0.41 / 0.91 |

Result with the defaults, all 359 cells:

| | Bias | Mean absolute error |
|---|---|---|
| Wealth (years of the cohort's median wages) | −0.36 | 0.78 (of which ~0.5 is sampling noise in the survey cells and the model's sample) |
| Home ownership (percentage points) | −0.8 | 5.3 |

Remaining: couples with a degree end with too little (−0.6 on average, −1.1 from 55; it
does not depend on the elasticity: high earners save more through retirement plans than
the model's employer contributions alone), so the bias grows with the horizon (−0.24 up to
6 years, −0.79 beyond 12); couples without a degree are on target (−0.11).

### Euro area: the HFCS tables

The euro area's wealth survey (ECB HFCS, waves 2010, 2014, 2017, 2021, 15-22 countries)
publishes no household records without an application, and its tables by age only as
PDFs. `tools/hfcs_tables.py` compiles all four waves' tables (net wealth, ownership, home
value, financial assets, debt and gross income, by 10-year age band of the reference
person, plus each country's fieldwork dates) into `reference/hfcs_tables.csv`; the build
converts non-euro countries to their currency. `python -m econsim.backtest_eu` then starts,
in every country and wave, where the survey found households aged 30, 40 and 50, a small
synthetic population of couples:

* five income levels around the median household income of that age; owners and renters
  in the survey's proportion (more owners at higher incomes), owners with the median home
  value and debt, financial assets scaled so the population's median net wealth is the
  survey's;
* renters start trying to buy at the ages at which the same survey's older households own
  more (start-wave data only), the rest rent for good;
* they live through their country's history (`eu_history.csv`: HICP and each budget line's
  index, average wages, house prices, rents, ECB rates on new mortgages, bond and money-
  market rates, deposit rates, unemployment, European stock returns in local currency),
  earning what their cohort's households earned in the later waves;
* and are compared with the same birth years in each later wave (net wealth without
  occupational pensions, as the HFCS measures it).

The same method on the SCF laid out as HFCS tables (`scf_tables.csv`) checks its own
error, as the US household backtest gives the answer it should reproduce: wealth −0.42
(households: −0.36), MAE 0.66; ownership −9.4 points (households: −0.8) - built from
medians, young renters hold too little to buy on time, so the table method itself reads
ownership ~9 points low.

Result, 19 countries, 276 cells (start waves 2010-2017, 3-11 years later):

| | Bias | Mean absolute error |
|---|---|---|
| Wealth (years of the cohort's median income) | +0.28 | 0.89 |
| Home ownership (percentage points) | −3.7 (within the method's own −9) | 5.7 |

The error does not grow with the horizon (+0.32 up to 4 years, +0.33 at 7-11). By country
(mean error, years of income): too much in Spain (+1.4: the 2012-14 house-price fall and
young Spaniards' weak incomes), Cyprus, Portugal (+1.0), Slovenia (+0.9); too little in
Italy (−0.8) and Germany (−0.7) - in Italy young households' median wealth triples between
30 and 40, from inheritances and family help the model does not have; most others within
±0.5.

The calibrated spending level beyond the survey basket is shown as its own line, "spending
surveys do not record" (interest on consumer debt, gifts and transfers, giving, irregular
big costs), as discretionary as the rest of the basket.

## Same model in every country

| | All countries | Where the data differ |
|---|---|---|
| Careers | four situations, the same rules | US: CPS microdata; elsewhere national LFS shares, US pay ratios and moves |
| Generation gaps | today's brackets + the part of the gap that lasts | US: CPS ASEC, SCF; Europe: SES earnings (persistence from the US), LFS unemployment |
| Spending | survey basket raised to national accounts, × the country's level, the same income, housing, car and wealth responses | each country's own surveys and accounts |
| Spending level (shown as "spending surveys do not record") | fitted so a population of couples (25-64, pay percentiles 25-75, with and without children) saves the national accounts' household net saving rate (OECD; else Eurostat gross less the median depreciation gap; at least 0%: national accounts that show households dissaving mostly miss informal income) | US 1.37, Germany 1.13, Italy 1.21, Spain 0.94, UK 0.93 (median 1.09) |
| Starting wealth by age | the median of households that age, owners and renters | US: SCF couples by degree; euro area: HFCS, all households by age; elsewhere enter yours |
| Backtests | the same model and behaviour, started in past surveys | US: SCF households; euro area: HFCS tables (checked on the SCF) |
| Savings | typical households' deposit and stock shares | ECB DWA (euro area), SCF (US); elsewhere the sector moved by the median gap |
| Homes | follow the price index and income, lending rules | refinancing only where loans can be repaid without penalty (US, DK) |
| Means-tested help | OECD tax-benefit curves outside the US | US: EITC, CTC, Medicaid / CHIP |

`python -m econsim.calibrate` prints each country's population saving rate against its
national accounts (`spending_level.csv`, refitted after every data build).

## What things cost: your own inflation

The CPI measures a national basket, and counts owners' housing as the rent their home
would fetch, not home prices or mortgage interest. In the US what households pay rose
~6%/yr over the last 5 years against ~4% for the CPI (`price_outlook.csv`: a household
cost index - CPI less shelter, rents, and home prices × mortgage rates averaged over 7
years, BLS weights - in the spirit of the UK's Household Costs Indices and Bolhuis,
Cramer, Schulz & Summers 2024). The CPI is compiled by the BLS, not by the Fed, which
targets the PCE index; the gap is what the CPI includes, not how it is measured.

The simulation does not add that gap on top: it prices the household's own costs directly -
rent at the market trend, home prices and mortgage rates, and every budget line at its
own price trend. Each run reports **your own inflation** (a chained index of the
household's own spending; a fixed-rate mortgage does not rise) next to the CPI, and "Today's
money" can be shown by either.

## Job market by age and degree

The Context tab and the unemployment cost use `econ_data/labour_market.csv`: for ages 20-24,
25-29, 30-34 and 25-64 and six education levels (not specified, below upper secondary, upper
secondary, associate's/short tertiary, bachelor's, master's+), on one definition everywhere:

* **Unemployment**: ILO headline rate. Europe: Eurostat `lfsa_urgaed` (tertiary split into
  degree levels with OECD EAG ratios at 25-34); US: computed from CPS basic monthly microdata
  (Census API, composite weights; the current year is year-to-date); Canada/UK: OECD EAG
  (25-34, 25-64) and the OECD 15-24 rate × the education gap.
* **Functional unemployment** (U-6-like): unemployed + part-timers who want and can work more
  hours + people who want a job and could start but have stopped looking, as % of the labour
  force plus that last group. Europe: Eurostat slack (`lfsi_sla_a`, `lfsa_sup_edu`) without
  "seeking but not available" (as in U-6); US: CPS (part-timers wanting full-time, marginally
  attached); Canada: OECD involuntary part-time + StatCan R6−R4; UK: OECD involuntary part-time
  + ONS discouraged workers (a narrow definition, so the UK is understated). For ages 20-24
  the extra slack is the 25-29 value × (20-24 / 25-29 unemployment), because 15-24 slack data
  are dominated by students.
* **Hours underemployment**: the part-time piece of the above.
* **Over-qualification** (degree holders): employed graduates in jobs that do not usually need
  a degree, ISCO 4-9 (Eurostat `lfsa_eoqgan`). US: occupations outside management, business,
  financial and professional groups, except the sales/office jobs ISCO puts in groups 2-3. Not
  available for Canada and the UK on a comparable basis.

Youth (15-24) headline rates differ across countries mostly because of who is in the youth
labour force: a full-time student looking for a part-time job is unemployed (about 60% of
unemployed 15-24-year-olds in Sweden, 30% in the US), and the rate divides by a small youth
labour force where few teenagers work.

## Calibration against real households

`econ_data` also carries the data used to keep the model honest (all from APIs):

| Table | What | Source |
|---|---|---|
| `budget_coverage.csv` | National-accounts household consumption ÷ survey spending, per category | Eurostat `nama_10_co3_p3`, `hbs_exp_t121`, `lfst_hhnhtych`; US: BEA PCE (NIPA 2.4.5) ÷ BLS CE per household (other non-EU: EU median). Default add-back: half the US gap (the CE implies married couples save a third of their income); none in Europe, whose spending was calibrated on Eurostat saving by age with raw surveys |
| `us_career.csv` | US situations, pay distributions, year-on-year moves and raises by age and degree | CPS basic monthly microdata (Census API), BLS CES |
| `price_outlook.csv` | Each budget line's price trend vs all prices over 10 years; US market inflation expectations; US household cost index vs CPI (12 months, 5 and 10 years) | Eurostat HICP by COICOP, BLS CPI components, FRED T5YIE / T5YIFR, CSUSHPISA, MORTGAGE30US |
| `cohort.csv` | Statistics by single year of age × survey year × degree with that year's all-ages reference, for following cohorts | Census CPS ASEC 2003-2026 (Census API), Fed SCF 1989-2025, Eurostat LFS `lfsa_urgaed` 2000-2025, SES 2006-2022 |
| `us_history.csv` | What happened, year by year: CPI and its components, home prices, rents, mortgage, bill and bond rates, deposit rates, stock returns, wages, unemployment | FRED, BLS public API, Kenneth French data library |
| `scf_households.csv` | Every SCF couple (head 22-64), every wave since 1989: the backtest's starting points | Fed SCF summary extracts |
| `part_time.csv` | Part-time share of employment by age band and education | Eurostat LFS `lfsa_epgaed` |
| `spending_level.csv` | Each country's calibrated spending level and the saving rate it reproduces | econsim.calibrate |
| `start_wealth.csv` | What US couples of each age and degree hold now: renters' and owners' financial assets and retirement accounts, home value, mortgage, student debt, ownership | Fed SCF 2025 summary extract |
| `family.csv` | Mothers' employment × hours vs women without children; average age of leaving home | Eurostat `lfst_hheredch`, `lfst_hhptechi`, `lfsa_ewhun2`, `yth_demo_030`; CPS microdata; StatCan 14-10-0120 |
| `pensions.csv` | Net replacement rates at 0.5/1/2 × average wage, future retirement age, life expectancy at 65 (now, 2065) | OECD Pensions at a Glance (`DF_PRR`, `DF_PAG`) |
| `market_index.csv` | Home-price, rent and fuel indices: curated levels are moved to the latest data | Eurostat HPI, HICP; BIS (FRED); NAR, BLS, EIA (FRED) |
| `mortgage_longrun.csv` | 20-year average new-loan mortgage rate | ECB MIR, SSB, SNB, BoE, Freddie Mac |
| `rates.csv` | 10-year yield, 3-month rate, deposit rates paid to households, mortgage rate now and after a fixed period, inflation target | OECD `DF_FINMARK`, Eurostat `irt_lt_mcby_m` / `irt_st_m`, ECB MIR, FDIC (FRED) |
| `portfolio.csv` | Households' deposits, bonds, shares, funds and life insurance as shares of free financial savings | OECD `DF_T720R_A` |
| `university.csv` | Household payments to tertiary institutions per student | OECD Education at a Glance |

The spending defaults (the survey basket raised to national accounts in full, income
elasticity 0.8, housing 0.75, cars 0.25, a wealth effect of 4% a year, each country's
level) come from the backtest and the national saving rates above. Survey-based saving
rates (Eurostat's experimental `icw_sr_*`, 30-50% in most countries) are not used: they
pair surveyed incomes with surveyed spending, which surveys under-record.

## The years ahead: today's conditions, not today's retirees

The forecast describes a couple starting out now, under today's markets and legislated rules,
not what people of each age look like today (their careers ran under different conditions).

| Input | Default | Source |
|---|---|---|
| Retirement age | Normal age for someone entering the labour market now (e.g. Denmark 74, Italy and Sweden 70, US 67), with legislated links to life expectancy | OECD Pensions at a Glance `DF_PAG` (FRPLF22) |
| Pension | Career-average pay ÷ average wage → net replacement rate at 0.5 / 1 / 2 × average wage (mandatory schemes, full career, legislated rules); indexed to prices | OECD `DF_PRR` (NPRR50/100/200) |
| Plan until | Women's life expectancy at 65 in the year each partner turns 65 (today → 2065 projection) + 2 years for the longer-living partner | OECD `DF_PAG` (UN World Population Prospects) |
| How savings are held | How TYPICAL households (the bottom 70% by wealth) split their free financial savings between deposits and investments, and investments between shares and bonds: e.g. deposits US 26% (household sector 16%), Germany 62%, Poland 94%; shares US 54% of investments (sector 84%). Or invest everything, or a custom split | ECB Distributional Wealth Accounts, Fed SCF 2025; elsewhere OECD `DF_T720R_A` moved by the median typical-vs-sector gap |
| Investment return | 10-year government yield (latest 3 months) + the households' share in shares × 3.5% equity premium − 0.2% fees; stocks fall to half by retirement | OECD `DF_FINMARK`, Eurostat `irt_lt_mcby_m` |
| Emergency fund / deposits | What banks pay households now: overnight deposits; deposits = overnight and fixed-term weighted by households' holdings. Elsewhere the 3-month rate × the median pass-through | ECB MIR (households), FDIC national rates via FRED |
| Rate after the fixed period | Today's mortgage rate + (10-year yield − 0.5 term premium − 3-month rate): the move in short rates the market expects | as above + ECB MIR, SSB, SNB, BoE, FRED |
| Inflation | US: what bond markets expect - 5-year breakeven for the next 5 years, then the 5-year-forward rate. Elsewhere the central-bank target | FRED T5YIE, T5YIFR; statutory |
| Costs | Each budget line's price trend relative to all prices over the last 10 years continues for 10 years (costs that already rise with wages - childcare, health premiums, cars - only take the part beyond); or all with prices | Eurostat HICP by COICOP; BLS CPI components |
| Wage growth | The last 20 years' real trend (default), or official long-run projections | OECD / Eurostat average wages; EC Ageing Report / OBR / CBO / PBO |
| House prices and rents | The last 20 years' real trend for 10 years, then with incomes (default); or with incomes throughout | Eurostat HPI and HICP rents, BIS (incl. US), FRED |
| University (optional) | Household payments to tertiary institutions per full-time student | OECD Education at a Glance `DF_UOE_FIN_INDIC_SOURCE_NATURE` |
| Student debt | Default: borrowers' average by degree (US, UK, Canada); amount, rate and term editable. Rate: US 6.52% (2026-27 federal undergraduate), England RPI (Plan 5, 9% above GBP 25,000), Canada 0% (federal), elsewhere the 10-year yield | `params/student_debt.csv`: College Board, Student Loans Company, StatCan NGS; FSA |

In retirement, spending follows the pension plus savings ÷ years left (as US required minimum
distributions, which most withdrawals track), or the pension only, leaving savings to heirs. When savings run out, living costs are cut, owners
borrow against the home, and the state's minimum income for older people covers what remains
(it is not run up as debt). Optional (off by default): family gifts and inheritances,
paying part of children's university, a lasting hourly-pay penalty for
the main parent (Kleven, Landais & Søgaard 2019) and years of paid long-term care.

## Known limitations

* Only the US has matched panels: elsewhere careers use the US pay ratios between
  situations and the US pace of moves, with each country's own mix. The CPS pools both sexes
  and parents with non-parents (the child-penalty module adjusts the main parent).
* Europe's backtest rests on the HFCS tables, not households: all households (singles too)
  by 10-year age band, and the couples rebuilt from medians (checked on the US: wealth as
  the household backtest, ownership ~9 points low). The UK, Nordic countries outside the
  HFCS, Switzerland and Canada have no wealth backtest.
* Workplace defined-contribution pensions outside the US (UK auto-enrolment, NL, CH) are
  not separate accounts: the OECD replacement rate covers mandatory schemes only.
* With realistic spending, many couples with children live pay cheque to pay cheque: then
  more rent means less other spending, not less wealth.
* Starting wealth by age: US (SCF, couples by degree) and euro area (HFCS 2021, all
  households by age only); elsewhere enter yours. Generation gaps for Europe rest on four
  SES waves in 10-year bands and borrow how long gaps last from the US.
* The US backtest is on couples: singles are not tested.
* Forward from today, couples who start with little saved rarely manage a down payment
  (e.g. two US graduates at 25 buy at 68 in the default plan; the US median first-time
  buyer is 40): today's prices and rates against young couples' saving - the housing
  squeeze people report. The backtests get ownership right for couples who started from
  their real savings (US) or the survey's (euro area).
* Labour-market rates are national: US states use the national rates. Pay by degree is a
  national average gap (field of study, which matters as much, is not modelled).
* Several rents, home prices, car costs and wedding costs are estimates (quality C); edit
  `econsim/params/*.csv` if you know better values.
* Switzerland, Iceland and Liechtenstein have no budget survey here: a neighbouring country's
  quantities at local prices (scaled to local incomes).
* Family help and inheritances are an input, not a forecast: in Southern Europe they are how
  many young people buy (in the HFCS backtest young Italians' wealth triples between 30 and
  40; the model's does not).
* Retirement: no survivor pensions or death of one partner before the horizon; pension-pot
  withdrawals are not taxed separately (the OECD net rates tax the mandatory pension only);
  basic/minimum pensions for people with short careers are only covered by the safety net.
* The savings mix is the whole household sector's, which leans toward wealthier savers: a
  typical family holds even more in the bank. Interest on deposits is not taxed here.
* Divorce, disability and health shocks are not modelled (use the job-loss scenario for an
  income shock).
* Not modelled: means-tested health subsidies (CH premium reductions, NL zorgtoeslag),
  Hungarian Babavaro loans, child benefits that vary with the child's age, part-time work after
  children (use the leave slider / career growth), US expat state tax, PFIC/NIIT.

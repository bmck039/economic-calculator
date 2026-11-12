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

In the US, each partner's career follows today's job market (see "US careers" below):
today's mix of full-time graduate-level jobs, other full-time jobs, part-time work and job
search for their age and degree, moved on by the year-on-year moves and raises people make
now (CPS microdata). Elsewhere, gross pay follows the country's cross-sectional age-earnings
profile at the chosen percentile, times the full-time pay of their degree relative to all
degrees at that age (OECD Education at a Glance, if a degree is chosen). Either way, a
stated current pay sets the starting level, and economy-wide real wage growth and inflation
come on top. Taxes use the
local average tax-rate curve with joint taxation where it exists (DE/LU/PT/PL splitting, FR
quotient familial), youth exemptions (PL, HU, HR, PT, GR) and compulsory occupational pension
contributions (CH, NL, DK, IS, LI); US residents get federal + state + local tax and FICA,
itemising mortgage interest and property tax when that wins. Births trigger the country's
parental leave (benefit instead of pay for the typical months); afterwards the main parent's
pay follows the country's typical employment rate × hours of mothers relative to women without
children, by the youngest child's age (switchable to full time). An expected unemployment cost
(headline rate for the partner's age band and degree × net pay × (1 − benefit replacement))
is deducted each year. Living costs are the
survey couple's budget (optionally raised toward national-accounts household consumption,
which surveys under-record), scaled by (income after housing / the survey
households' income after housing)^0.8 with both growing with average wages, and with vehicles
and health insurance replaced by explicit car and health modules; children add 20% of that
per-person spending each (33% from 14, OECD-modified scale) until the country's average age
of leaving home, plus childcare until free pre-school. Services (childcare, health, cars) rise
with wages. When money runs short, discretionary spending is cut (up to half) before any
borrowing; employer pension contributions go to a retirement account, not to the deposit. Renting uses new-lease market rents growing with real wages. The purchase
waits until the down payment, taxes and fees are saved (keeping 6 months' buffer) and the loan
fits the country's lending limits; fixed periods end at the rate today's market expects.
Savings above a 6-month emergency fund are held the way the country's households hold
theirs (OECD financial balance sheets): part in bank deposits at the rates banks actually pay
(ECB, FDIC), the rest invested at today's 10-year yield + the households' share in shares ×
a 3.5% equity premium − fees, minus the country's tax drag, with the share in stocks falling
to half by retirement. From the
retirement age for today's young workers, each partner gets a pension (career-average pay
relative to the average wage × the OECD replacement rate at that pay), indexed to prices, and
the couple draws savings down slowly until the longer-living partner's projected lifespan.

## US careers: today's job market, not full-time averages

Full-time median pay overstates what people starting out now earn: among US bachelor's
holders aged 20-24 who work or look for work (students excluded), only about half are in
full-time graduate-level jobs. The US model therefore tracks four situations per partner -
full time in a job that needs a degree (management, business, financial, professional, and
the sales/office jobs ISCO puts with them), full time in another job, part time (< 35 usual
hours), and looking for work (incl. discouraged workers) - all from the last 12 months of
CPS basic monthly microdata (Census API):

| Piece | How | Data |
|---|---|---|
| Starting point | Today's mix for the partner's age band and degree, or the situation they enter ("P1 now") | CPS composite weights, 5-year age bands |
| Pay in each situation | Usual weekly earnings × 52 / 12 at the partner's percentile (or where their stated pay sits); a stated pay fades toward the chosen percentile (ranks correlate ~0.6 over 10 years, Kopczuk-Saez-Song 2010) | CPS outgoing rotation groups |
| Moves between situations | The same households matched one year apart (months in sample 1-4 → 5-8), by age group and degree. Occupation is coded afresh each year, so switches between graduate and other jobs are scaled to measured persistence: 73% of graduates who start underemployed still are 10 years later vs ~21% of those who start in college-level jobs (Strada / Burning Glass, *Talent Disrupted*, 2024) | CPS matched panels |
| Raises | The same workers' median pay change over a year (staying in the same situation), by age and degree, beyond economy-wide wage growth (average hourly earnings) | CPS months 4 → 8, BLS CES |
| Job market ahead | "Stays as it is now" (today's moves continue) or "keeps changing": the change between the prior 12 months and the last 12 continues for 5 years | CPS |

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
| `price_outlook.csv` | Each budget line's price trend vs all prices over 10 years; US market inflation expectations | Eurostat HICP by COICOP, BLS CPI components, FRED T5YIE / T5YIFR |
| `family.csv` | Mothers' employment × hours vs women without children; average age of leaving home | Eurostat `lfst_hheredch`, `lfst_hhptechi`, `lfsa_ewhun2`, `yth_demo_030`; CPS microdata; StatCan 14-10-0120 |
| `pensions.csv` | Net replacement rates at 0.5/1/2 × average wage, future retirement age, life expectancy at 65 (now, 2065) | OECD Pensions at a Glance (`DF_PRR`, `DF_PAG`) |
| `market_index.csv` | Home-price, rent and fuel indices: curated levels are moved to the latest data | Eurostat HPI, HICP; BIS (FRED); NAR, BLS, EIA (FRED) |
| `mortgage_longrun.csv` | 20-year average new-loan mortgage rate | ECB MIR, SSB, SNB, BoE, Freddie Mac |
| `rates.csv` | 10-year yield, 3-month rate, deposit rates paid to households, mortgage rate now and after a fixed period, inflation target | OECD `DF_FINMARK`, Eurostat `irt_lt_mcby_m` / `irt_st_m`, ECB MIR, FDIC (FRED) |
| `portfolio.csv` | Households' deposits, bonds, shares, funds and life insurance as shares of free financial savings | OECD `DF_T720R_A` |
| `university.csv` | Household payments to tertiary institutions per student | OECD Education at a Glance |

The defaults (spending elasticity 0.8, no survey add-back, children at home until the
national leaving age, the child penalty, the budget constraint) were checked against Eurostat's median saving rate by
age (`icw_sr_01`), the share of couples who struggle to make ends meet or cannot face an
unexpected expense (`ilc_mdes09`, `ilc_mdes04`), the age of leaving home, and the Fed's SCF
net worth of married working couples by age and income.

## The years ahead: today's conditions, not today's retirees

The forecast describes a couple starting out now, under today's markets and legislated rules,
not what people of each age look like today (their careers ran under different conditions).

| Input | Default | Source |
|---|---|---|
| Retirement age | Normal age for someone entering the labour market now (e.g. Denmark 74, Italy and Sweden 70, US 67), with legislated links to life expectancy | OECD Pensions at a Glance `DF_PAG` (FRPLF22) |
| Pension | Career-average pay ÷ average wage → net replacement rate at 0.5 / 1 / 2 × average wage (mandatory schemes, full career, legislated rules); indexed to prices | OECD `DF_PRR` (NPRR50/100/200) |
| Plan until | Women's life expectancy at 65 in the year each partner turns 65 (today → 2065 projection) + 2 years for the longer-living partner | OECD `DF_PAG` (UN World Population Prospects) |
| How savings are held | Households' split of free financial savings: deposits vs bonds, life insurance, listed shares and funds (not pensions or own businesses), e.g. deposits US 16%, Germany 50%, Poland 82%. Or invest everything, or a custom split | OECD financial balance sheets `DF_T720R_A` (households + NPISH; Cyprus, Malta, Iceland: EU median) |
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

* Careers from today's job market are US-only (CPS); elsewhere pay follows full-time earnings by age with the unemployment rate for the age and degree. The CPS pools both sexes and parents with non-parents (the child-penalty module adjusts the main parent).
* Starting wealth and home ownership are inputs: starting at 35 with no savings is not a typical 35-year-old.
* Labour-market rates are national: US states use the national rates. Pay by degree is a
  national average gap (field of study, which matters as much, is not modelled).
* Several rents, home prices, car costs and wedding costs are estimates (quality C); edit
  `econsim/params/*.csv` if you know better values.
* Switzerland, Iceland and Liechtenstein have no budget survey here: a neighbouring country's
  quantities at local prices (scaled to local incomes).
* Family help is an input, not a forecast: in Southern and Eastern Europe it is how many
  young people buy (HFCS tables are PDF only).
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

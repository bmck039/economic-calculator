# Economic Calculator and Planner

**What would our financial life look like in another country?** This app simulates a
couple's working life and retirement, year by year, in any of 34 countries (EU27, EFTA,
the UK, the US and Canada) or any US state: what they earn, pay in tax and spend, what
they save, when they can buy a home, and what they retire with. It uses official
statistics throughout, and every default shows its source.

It is built to answer "how would the same life go here rather than there?" Every country
runs the same model, and only the data differ. The model is checked by starting real
households in past surveys, running them through what actually happened, and comparing
them with the same birth years later on.

## Features

- **Incomes that match today's job market.** Pay by age, percentile and degree. Each
  partner's career moves between full-time graduate jobs, other full-time jobs, part-time
  work and looking for work, at the pace people really move. Your generation's pay gap
  to older cohorts lasts as far as such gaps have lasted in the past.
- **Taxes for each country.** Local income tax and social contributions, joint taxation
  and youth exemptions. US residents get federal, state and local tax, FICA, the CTC and
  the EITC, plus Medicaid / CHIP. US citizens abroad get the FEIE / FTC layer.
- **Costs of living from household budget surveys**, raised to national-accounts totals
  and scaled with income. Children, childcare, cars, health insurance, student loans,
  emergencies and weddings are counted separately. Each cost follows its own recent price
  trend.
- **Housing.** Rent, or a purchase that waits until the down payment and fees are saved
  and the loan fits the country's lending rules, then the mortgage, refinancing and
  upkeep.
- **Life events:** marriage, children with parental leave and mothers' typical work
  pattern, job loss, family gifts, inheritance, university, long-term care.
- **Retirement.** The retirement age for people starting work today, a pension from
  career-average pay and the OECD replacement rate, and savings drawn down slowly.
- **Comparison.** The same plan in up to three places, with net worth, saving, wages and
  prices over time, and 🏠 where each household buys.
- **Your own inflation.** A cost index of the household's own basket, with your rent or
  mortgage, next to the CPI.
- **Shareable.** Settings are kept in your browser. "🔗 Share this plan" gives a link that
  carries every setting, and nothing is stored on the server.

## Quick start

```
python run.py                  # first time: venv + packages + data + tests, then starts the app
python run.py setup            # set up only;  `python run.py start` starts without the data/test steps
python run.py test             # engine tests + headless UI smoke tests
python run.py data [--refresh] # rebuild the data (30-day download cache; --refresh ignores it)
```

`run.py` uses only the standard library and works on macOS, Linux and Windows. Use
`python3` on macOS/Linux, or `py` on Windows, if `python` isn't on PATH. It needs
Python 3.10+. The first run downloads and builds the data, which takes about 10-20
minutes.

Settings (port, host, Census key, ...) are in `config.env`. `example.config.env`
documents them and is copied to `config.env` on the first run. To host the app on a
network, set `HOST=0.0.0.0` and `OPEN_BROWSER=false`. Behind a reverse proxy, forward
WebSockets (Streamlit uses `/_stcore/stream`) and optionally set `BASE_URL_PATH`. Restart
the app after `python run.py data` so it loads the new tables.

The manual equivalent is `pip install -r requirements.txt`, then
`python build_econ_dataset.py`, `streamlit run app.py` and `pytest -q`.

### Docker (website mode)

`compose.yaml` runs the app plus a Cloudflare Tunnel, both restarting automatically
whenever Docker runs. Put `TUNNEL_TOKEN` in `config.env`, then:

```
docker compose up -d --build     # first start builds the data into a volume (~10-20 min)
docker compose logs -f econ      # watch progress
docker compose down              # stop (data volume kept; `down -v` deletes it)
```

The container runs `python run.py`, so setup behaves exactly as above. Inside the
container `HOST` and `PORT` (8501) come from `compose.yaml`, and everything else comes
from `config.env`. In the Cloudflare dashboard, the tunnel's public hostname uses the
path in `config.env` and the service `http://econ:8501`. To refresh the data, run
`docker compose exec econ python run.py data`, then `docker compose restart econ`.

### Deploying (automatic updates with doco-cd)

On the machine that hosts the site, [doco-cd](https://github.com/kimdre/doco-cd) keeps the
stack on the latest stable release of the repo it follows (this one, or your fork).

Publishing a GitHub release runs the **full** test suite on GitHub, on the built data
([`.github/workflows/release.yml`](.github/workflows/release.yml)). The tables are kept
between runs for each `DATA_VERSION`, so only a new data version rebuilds them (~10-20
min, then ~20 min of tests). Only if the suite passes does the workflow move the
`release` branch to the new release. A release that fails never reaches the server: the
old version keeps running, and GitHub emails you (*Settings → Notifications → Actions*:
"Notify me for failed workflows").

The data build needs the Census key as a repository secret, set once with
`gh secret set CENSUS_API_KEY` (paste the key from `config.env`). You can also run the
suite by hand: *Actions → test and release → Run workflow*.

doco-cd polls the `release` branch and runs the equivalent of `docker compose up -d
--build`, so changes to the code, `Dockerfile` and `compose.yaml` all apply:

- Plain commits and pre-releases deploy nothing.
- The data volume is kept, and cloudflared is updated to its latest image at each deploy.
- The container starts serving at once (`RUN_TESTS=never` in `compose.yaml`), because
  the release was tested before it was deployed.
- A release that bumps `DATA_VERSION` rebuilds the data on its first start, reusing
  cached downloads. The site is down for those ~10-20 minutes.

doco-cd can also tell you about its own failures (a broken build or compose file) through
[Apprise](https://github.com/caronc/apprise-api). For email, set
`APPRISE_NOTIFY_URLS: mailto://you:app-password@gmail.com` and
`APPRISE_NOTIFY_LEVEL: failure` on the doco-cd service; see doco-cd's documentation for
your version.

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
([`.doco-cd.yml`](.doco-cd.yml) points the stack at it), then run `docker compose up -d`.
doco-cd deploys the current release within a minute and then checks every 15 minutes.
`docker logs -f doco-cd-doco-cd-1` shows what it does. If a stack started by hand with
`docker compose up` is running, stop it first with `docker compose down` (which keeps the
data).

Platform notes for the doco-cd host:

- **Linux:** nothing extra is needed with Docker Engine. It starts at boot if the `docker`
  service is enabled (`sudo systemctl enable docker`).
- **macOS and Windows (Docker Desktop):** enable *Start Docker Desktop when you sign in*.
  Docker then runs only while you're signed in, so use automatic sign-in on a machine that
  should serve the site unattended. On macOS, also enable *Settings → Advanced → Allow the
  default Docker socket to be used*. On Windows, use the WSL 2 backend with Linux
  containers (the default); the `/var/run/docker.sock` mount works as written.

**To ship a change:** push it, then publish a GitHub release (e.g. tag `v0.9.0`).

### Data build options

`python build_econ_dataset.py --help` lists every option. The useful ones:

- `--derive-only`: rebuild the derived tables from the last `econ_long.csv`, with no
  network (~2 min).
- `--refresh`: ignore the download cache.
- `--reference my_refs.csv`: override curated reference values.
- `--profile`: cProfile's top 25.

Two free API keys add data:

- `CENSUS_API_KEY` adds ACS rents and the US job-market figures (CPS microdata). Without
  it, the US rent comes from `reference_values.csv` and US unemployment by degree from
  OECD tables.
- `FRED_API_KEY` ([fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html))
  reads the US series through FRED's API. Without it, they come from FRED's CSV endpoint.

The build exits with:

- **1** if any country would end up without earnings or tax data (the app could not offer
  it). The previous tables are left untouched.
- **2** when only some sources failed and fell back to cached or reference values.
- **0** otherwise.

A complete build stamps `econ_data/.data-version`. `python run.py` rebuilds whenever that
stamp is missing or older than `DATA_VERSION` in `econdata/version.py`; bump it when old
tables must be rebuilt.

## How it works

```
official statistics ──► econdata/ ──► econ_data/*.csv ──► econsim/ ──► ui/ + app.py
(Eurostat, OECD, FRED,   download,     tidy tables:        the year-by-  the page: inputs,
 BLS, Census, StatCan,   cache, parse, earnings, taxes,    year model,   charts, tables,
 ONS, ECB, World Bank)   derive        budgets, history…   backtests     share links
                          + econsim/params/*.csv (curated rules: lending, cars, leave, …)
```

1. **Data.** `build_econ_dataset.py` (the `econdata` package) downloads every series once
   into a 30-day cache. It turns each one into tidy rows (country, measure, age band,
   category, year, value, unit, source, comparability note), then derives the tables the
   model reads. Things no statistics API publishes comparably (lending rules, car costs,
   parental leave, US state taxes, ...) are curated in `econsim/params/*.csv`, each value
   with a source and a quality grade.
2. **Country data.** `econsim.data.DataStore` builds one `CountryData` per country or US
   state. Every number in it is a `Sourced` value: value, unit, source, year, estimate
   flag.
3. **Plan.** The sidebar's choices become a `Plan` (`econsim.model`). `econsim.inputs.resolve`
   fills every input the plan leaves open with the country's default, with its source.
   The Sources tab lists them all.
4. **Simulation.** `econsim.engine.simulate` runs the household one year at a time, in
   nominal local currency (stages below). `to_display` converts the result to today's
   money, USD or international dollars.

### One simulated year

| Stage | What happens |
|---|---|
| Prices and returns | Prices, wages, rents and home prices move along their paths. Investments earn the 10-year yield plus the share in stocks × a 3.5% equity premium, minus fees and the tax drag. The share in stocks falls to half over the 15 years before retirement. |
| Pay | Each partner's gross pay: their career situation and pay level, the generation's lasting gap, economy-wide real wage growth. A stated current pay fades toward the chosen percentile (ranks correlate ~0.6 over 10 years). |
| Family | Births start the country's parental leave. Afterwards, the main parent works the country's typical employment rate × hours of mothers, until the youngest is 18. |
| Taxes | The local average tax-rate curve with joint taxation where it exists (DE/LU/PT/PL splitting, FR quotient familial), youth exemptions (PL, HU, HR, PT, GR), compulsory occupational pensions (CH, NL, DK, IS, LI). US: federal + state + local, FICA, CTC, EITC, Medicaid / CHIP. |
| Job loss and risk | A simulated job loss replaces pay with benefits at the OECD replacement rates. Otherwise, the expected cost of unemployment for each partner's age and degree is deducted. |
| Spending | The survey couple's basket, raised to national accounts category by category, × the country's calibrated spending level. It scales with income relative to the survey couple's: ^1.0 below it, ^0.4 above it. The home a buyer can afford (^0.75) and cars (^0.25) follow income too. Children add per-person spending (OECD-modified scale) and childcare until free pre-school. Before retirement, 4% of savings above the emergency fund is spent each year (not by renters saving for a home). |
| Renting | You stay in your home, and its rent rises with the rent index less the tenure discount (US 1.5% a year). Each year, the share of renting couples your age who move (US: 35% under 25, 19% at 30-34, 10% after 45; scaled by each country's renters' moves) take a new lease. It costs what a couple with your income (US ^0.4; each country's own) and children (+7% / +16% or the country's ratio) pays now. If the rent passes 60% of net income and a cheaper place is found, you move. The rent shown is the expected one, so it rises smoothly rather than at one guessed moving date. |
| Housing | Renters buy once the down payment, taxes and fees are saved (keeping 6 months' spending as a buffer) and the loan fits the country's limits. The mortgage follows the country's fixation and term. US and Danish owners refinance when new loans cost a point less. Owned homes follow the repeat-sales price index. |
| Shortfalls | If money runs short (savings used up), discretionary spending is cut first, by up to half. Then a little is borrowed (unsecured, up to a quarter of a year's income), then all living costs are cut, by up to 30%. With "Cut optional spending before using savings", the cuts start as soon as a working year's income falls short of the regular bills. Retired owners borrow against the home, and the state's minimum income for older people covers the rest. |
| Balances | The emergency fund (6 months) is kept in cash. Savings above it are split between deposits and investments as typical households (the bottom 70% by wealth) hold theirs, moving toward the whole household sector's mix as savings grow (halfway at a year's income). Employer pension contributions build a retirement account. |
| Retirement | From the retirement age for today's young workers, each partner gets a pension: career-average pay relative to the average wage × the OECD net replacement rate, indexed to prices. Savings are drawn down over the years left, until the longer-living partner's projected lifespan. |

The tunable behaviour (the spending elasticities, the wealth effect, borrowing limits, how
far spending is cut) is in one place: `econsim/assumptions.py`.

### Careers: today's job market, not full-time averages

Full-time median pay overstates what people starting out now earn. Among US bachelor's
holders aged 20-24 who work or look for work (students excluded), only about half are in
full-time graduate-level jobs. So every partner, in every country, is in one of four
situations each year: full time in a job that needs a degree, full time in another job,
part time, or looking for work.

| Piece | United States | Other countries |
|---|---|---|
| Today's mix by age and degree | CPS basic monthly microdata, last 12 months | Unemployment by age and degree (labour_market.csv); part-time share by age and education (Eurostat LFS `lfsa_epgaed`; under 25, the 25-49 share raised as in the US, where students are left out); graduates in lower-level jobs (LFS over-qualification) |
| Pay in each situation | CPS usual weekly earnings at the partner's percentile | The country's own full-time pay by age, percentile and degree, split with the US ratios of non-graduate and part-time to graduate full-time pay (so its full-time average is the country's) |
| How fast a stated situation moves back to the typical mix | The same CPS households a year apart. Switches between graduate and other full-time jobs are scaled to measured persistence: 73% of graduates who start underemployed still are 10 years later (Strada / Burning Glass, *Talent Disrupted*, 2024) | The US moves (no country publishes matched panels) |
| Pay growth | The generation's full-time year-round pay (CPS ASEC) | The country's full-time pay by age × the generation gap (SES) |
| Job market ahead | "Stays as now", or "keeps changing": the last year's change in the mix continues for 5 years | Stays as now |

Starting from the typical mix, the expected mix at each age is today's mix at that age.
The surveys show no lasting generation gaps in unemployment or full-time work.

### Generations: your cohort, not today's older brackets

Today's 35-year-olds are not a forecast of today's 33-year-olds in two years. If the
33-year-olds have been doing worse than the 35-year-olds did at the same age, survey after
survey, they will most likely still be behind at 35. `econsim/cohort.py` measures this for
every statistic it has a long series for:

- the level at each age in each survey year, relative to all 25-64-year-olds that year
  (so economy-wide growth and the business cycle drop out);
- each cohort's **gap** to the cohort h years older at the same ages, averaged over its
  last three surveys (a one-off bad year is not a lasting gap);
- how much of such gaps **lasted** h years later in past surveys (`phi`, least squares
  over every age and year where the outcome is known; only horizons where the gap and the
  outcome do not share survey respondents);
- the forecast: today's level at the older age + `phi` × the gap. In the first years
  everyone moves as people moving up today's age brackets; later, by as much of their
  generation's gap as history says lasts. "The whole gap lasts" and "none" are options.

| Statistic | Survey | Share of a gap still there after 5 / 10 years |
|---|---|---|
| Full-time year-round pay (US) | CPS ASEC, every year since 2003 | ~25% / ~45% |
| Household income (US) | CPS ASEC | ~23% / ~25% |
| Home ownership (US) | CPS ASEC; SCF | ~15% / ~22% |
| Financial assets (US) | SCF, every 3 years since 1989 | 35% after 9 years, ~70% after 15 |
| Unemployment, full-time share (US, Europe) | CPS ASEC; Eurostat LFS since 2000 | ~0: they follow age and the cycle, not the birth year |
| Pay (Europe) | Eurostat SES 2006-2022 by age band | Measured gaps; persistence from the US estimate (four waves of 10-year bands cannot measure it) |

`python -m econsim.validate` estimates these on surveys up to 2014 or 2016 and compares
them with what the cohorts then did. The estimates beat "you'll be where today's older
bracket is" in most cases, modestly: generation gaps are real but mostly small, except in
wealth.

The same command checks every starting age from 20 to 64. Pay in the first three years
moves within ~1% of today's age brackets (median), and the careers' unemployment agrees
with the March supplement.

**Starting point by age.** Starting savings and retirement accounts default to the median
of couples whose head is that age, with that degree, renting or owning (Fed SCF 2025). In
the euro area, they default to the median of households whose reference person is that
age (ECB HFCS 2021, by age only, interpolated between its 10-year bands). Elsewhere they
start at 0, so enter yours. From the planned buying age, a household starts as an owner
with the median home and mortgage of owner couples that age, the 20-year average mortgage
rate, and the term left implied by the balance.

### The years ahead: today's conditions, not today's retirees

The forecast describes a couple starting out now, under today's markets and legislated
rules. It does not describe what people of each age look like today, because their
careers ran under different conditions.

| Input | Default | Source |
|---|---|---|
| Retirement age | The normal age for someone entering the labour market now (e.g. Denmark 74, Italy and Sweden 70, US 67), with legislated links to life expectancy | OECD Pensions at a Glance `DF_PAG` (FRPLF22) |
| Pension | Career-average pay ÷ average wage → net replacement rate at 0.5 / 1 / 2 × the average wage (mandatory schemes, full career, legislated rules); indexed to prices | OECD `DF_PRR` (NPRR50/100/200) |
| Plan until | Women's life expectancy at 65 in the year each partner turns 65 (today → 2065 projection) + 2 years for the longer-living partner | OECD `DF_PAG` (UN World Population Prospects) |
| How savings are held | How typical households (the bottom 70% by wealth) split free financial savings between deposits and investments, and investments between shares and bonds (e.g. deposits US 26%, sector 16%; Germany 62%; Poland 94%). Or invest everything, or a custom split | ECB Distributional Wealth Accounts, Fed SCF 2025; elsewhere OECD `DF_T720R_A` moved by the median typical-vs-sector gap |
| Investment return | 10-year government yield (latest 3 months) + the households' share in shares × 3.5% equity premium − 0.2% fees | OECD `DF_FINMARK`, Eurostat `irt_lt_mcby_m` |
| Emergency fund / deposits | What banks pay households now: overnight deposits; deposits = overnight and fixed-term weighted by households' holdings. Elsewhere, the 3-month rate × the median pass-through | ECB MIR (households), FDIC national rates via FRED |
| Rate after the fixed period | Today's mortgage rate + (10-year yield − 0.5 term premium − 3-month rate): the move in short rates the market expects | As above, + ECB MIR, SSB, SNB, BoE, FRED |
| Inflation | US: what bond markets expect (the 5-year breakeven for the next 5 years, then the 5-year-forward rate). Elsewhere, the central-bank target | FRED T5YIE, T5YIFR; statutory |
| Costs | Each budget line's price trend relative to all prices over the last 10 years continues for 10 years. Costs that already rise with wages (childcare, health premiums, cars) take only the part beyond. Or all rise with prices | Eurostat HICP by COICOP; BLS CPI components |
| Wage growth | The last 20 years' real trend (default), or official long-run projections | OECD / Eurostat average wages; EC Ageing Report / OBR / CBO / PBO |
| House prices and rents | The last 20 years' real trend for 10 years, then with incomes (default); or with incomes throughout | Eurostat HPI and HICP rents, BIS (incl. US), FRED |
| University (optional) | Household payments to tertiary institutions per full-time student | OECD Education at a Glance `DF_UOE_FIN_INDIC_SOURCE_NATURE` |
| Student debt | Borrowers' average by degree (US, UK, Canada); amount, rate and term editable. Rate: US 6.52% (2026-27 federal undergraduate), England RPI (Plan 5, 9% above GBP 25,000), Canada 0% (federal), elsewhere the 10-year yield | `params/student_debt.csv`: College Board, Student Loans Company, StatCan NGS; FSA |

In retirement, spending follows the pension plus savings ÷ years left (as US required
minimum distributions, which most withdrawals track), or the pension only, leaving
savings to heirs. Optional (off by default): family gifts and inheritances, paying part of
children's university, a lasting hourly-pay penalty for the main parent (Kleven, Landais &
Søgaard 2019), and years of paid long-term care.

### What things cost: your own inflation

The CPI measures a national basket, and counts owners' housing as the rent their home
would fetch, not home prices or mortgage interest. In the US, what households pay rose
~6%/yr over the last 5 years, against ~4% for the CPI. That comes from
`price_outlook.csv`'s household cost index: the CPI less shelter, plus rents, plus home
prices × mortgage rates averaged over 7 years, with BLS weights. It is in the spirit of
the UK's Household Costs Indices and Bolhuis, Cramer, Schulz & Summers (2024).

The simulation does not add that gap on top. It prices the household's own costs
directly: rent at the market trend, home prices and mortgage rates, and every budget line
at its own price trend. Each run reports **your own inflation** next to the CPI (a chained
index of the household's own spending, in which a fixed-rate mortgage does not rise).
"Today's money" can be shown by either.

### Renting: staying put, and what a move costs

Renters mostly stay where they are, and a sitting tenant's rent rises more slowly than
new leases (the "tenure discount"). When they move, they take what a household with their
income and children rents now. The model follows that, with figures measured on US
microdata (`python -m tools.rent_research`, Census ACS PUMS 2013-2023 and CPS ASEC
2023-2025, married couples who rent, compared within the same area):

| | Measured | Model |
|---|---|---|
| Couples who move each year | 35% under 25, 28% at 25-29, 19% at 30-34, 16% at 35-39, ~10% after 40 (CPS ASEC) | The same, by partner 1's age; other countries scaled (below) |
| Why they move | Job 27%, a better or bigger home or area 26%, family 21%, a cheaper home 10% | A move takes a new lease at today's market for the household |
| Sitting tenants vs new leases | −7 to −8% after 2-4 years, −13 to −17% after 5-9 (same size of home, area and income) | Their rent rises 1.5%/yr less than the rent index (× how often the country's renters move: the index follows all tenancies, and leases reset on moving are what lift it) |
| Rent vs income (new leases) | 0.41-0.43 with permanent income (education × age as instruments; 0.2 on one year's income). Studies: renters 0.3-0.5 (Mayo 1981; Goodman & Kawai) | ^0.4 in the US (before v0.9.2: ^0.75, every year); other countries below |
| Children (new leases, same income) | +5-8% with one, +11-16% with two | +7% / +16% (US; a country's own EU-SILC ratios where published) |
| Unaffordable rent | | Above 60% of net income (HUD's "severe" burden is half of gross), the household moves somewhere cheaper |

**Every other country** (`econsim/params/renting.csv`, `python -m tools.rent_research
--write`):

- **Rent vs income.** The OECD Affordable Housing Database (HC1.2.A2) gives tenants'
  median rent burden by income quintile, computed from each country's household survey
  (EU-SILC; the ACS for the US). Eurostat (`ilc_di01`) gives each quintile's share of
  income, and the ACS gives the same for the US. Together they show how rent rises from
  the 2nd to the top quintile (2015-2024 pooled). That spread also reflects richer
  households living in dearer places (the US shows 0.56 on it, against 0.4 within the
  same area), so each country is scaled by the US: 0.4 × country ÷ US.
- **How often renters move.** The share of tenants at market rent who moved in the last
  five years (EU-SILC 2012, `ilc_hcmp05`; the US from the ACS 2013, 73%), turned into a
  yearly hazard relative to the US, scales the US rates by age.
- Countries with too few tenants in the upper quintiles take the European median (0.31);
  Canada takes the US values, and Liechtenstein Switzerland's moves.

Elasticity / moves vs the US:

Austria 0.29 / 0.40; Belgium 0.28 / 0.50; Bulgaria 0.31 / 0.30; Croatia 0.31 / 0.41; Cyprus 0.31 / 1.28; Czechia 0.31 / 0.17; Denmark 0.22 / 0.60; Estonia 0.31 / 0.80; Finland 0.27 / 0.75; France 0.47 / 0.56; Germany 0.36 / 0.33; Greece 0.29 / 0.32; Hungary 0.31 / 0.51; Ireland 0.56 / 0.68; Italy 0.28 / 0.20; Latvia 0.31 / 0.28; Lithuania 0.31 / 0.97; Luxembourg 0.31 / 0.51; Malta 0.31 / 0.43; Netherlands 0.26 / 0.30; Poland 0.52 / 0.48; Portugal 0.55 / 0.36; Romania 0.31 / 0.29; Slovakia 0.31 / 0.15; Slovenia 0.31 / 0.31; Spain 0.40 / 0.55; Sweden 0.22 / 0.67; Iceland 0.31 / 1.58; Liechtenstein 0.31 / 0.44; Norway 0.31 / 1.05; Switzerland 0.38 / 0.44; United Kingdom 0.36 / 1.12; Canada 0.40 / 1.00.

Rent follows income least where regulated or social renting is large (Sweden and Denmark
0.22, the Netherlands 0.26) and most in France, Poland, Ireland and Portugal (0.47-0.56).
Renters move least in Slovakia, Czechia and Italy (0.15-0.20 × the US) and as often as in
the US in the UK, Norway, Lithuania, Cyprus and Iceland. With each country's own values,
the euro-area backtest is the same as with the US values everywhere (+0.33 / 0.89).

The default rents are new leases already (US: ACS rents of recent movers; Europe: asking
rents), so a household starts at today's market and then falls behind it while it stays.
The rent shown is the expected one, averaged over the chance of moving each year, so it
rises smoothly instead of at one guessed moving date.

Checked on history with the same birth years (ACS 2013 → 2018 → 2023), rent against the
rent index grew by this share of each cohort's income growth over the 10 years. The model
was run through the same years:

| Aged in 2013 | Survey | Model | Before v0.9.2 |
|---|---|---|---|
| 22-26 | 0.62 | 0.50-0.52 | 1.31-1.33 |
| 25-29 | 0.60 | 0.60-0.62 | 1.42-1.43 |
| 30-34 | 0.38 | 0.33-0.39 | 0.80-0.83 |

(The survey cohorts lose their higher earners to buying, and 2021-23 new leases outran
the index, so the survey's ratio is if anything high.) The wealth backtests are unchanged
in the US and slightly better in the euro area; the euro area's ownership is 0.6 points
further off.

### Job market by age and degree

The Context tab and the unemployment cost use `econ_data/labour_market.csv`. It covers
ages 20-24, 25-29, 30-34 and 25-64 and six education levels (not specified, below upper
secondary, upper secondary, associate's/short tertiary, bachelor's, master's+), on one
definition everywhere:

- **Unemployment:** the ILO headline rate.
  - Europe: Eurostat `lfsa_urgaed` (tertiary split into degree levels with OECD EAG ratios
    at 25-34).
  - US: computed from CPS basic monthly microdata (Census API, composite weights; the
    current year is year-to-date). Every month of the last two years; before that, one
    month a quarter back to 2015 for the history charts.
  - Canada/UK: OECD EAG (25-34, 25-64) and the OECD 15-24 rate × the education gap.
- **Functional unemployment** (like the US U-6): the unemployed, plus part-timers who want
  and can work more hours, plus people who want a job and could start but have stopped
  looking. It is a percentage of the labour force plus that last group.
  - Europe: Eurostat slack (`lfsi_sla_a`, `lfsa_sup_edu`) without "seeking but not
    available" (as in U-6).
  - US: CPS (part-timers wanting full-time, marginally attached).
  - Canada: OECD involuntary part-time + StatCan R6−R4.
  - UK: OECD involuntary part-time + ONS discouraged workers. This is a narrow definition,
    so the UK is understated.
  - For ages 20-24, the extra slack is the 25-29 value × (20-24 / 25-29 unemployment),
    because 15-24 slack data are dominated by students.
  - Eurostat publishes slack by age only from 2021 (the survey reform). Earlier years
    take the 2021 value and move it with the all-ages slack, which goes back to 2010
    (marked as estimates).
- **Over-qualification** (degree holders): employed graduates in jobs that do not usually
  need a degree, ISCO 4-9 (Eurostat `lfsa_eoqgan`).
  - US: occupations outside the management, business, financial and professional groups,
    except the sales/office jobs that ISCO puts in groups 2-3.
  - Not available for Canada and the UK on a comparable basis.
- **Underemployment** (the ILO's two measurable kinds), as a percentage of the employed:
  people working fewer hours than they want (part-timers who want and can work more),
  plus, for graduates, those in a job that does not need their degree (over-qualified).
  - US: counted person by person in the CPS, so nobody is counted twice.
  - Elsewhere: the part-timers come from the slack data above. For graduates they are
    combined with over-qualification as if the two were independent, because the
    surveys publish no overlap.
  - Being paid less than one's skills would earn (the ILO's income-related kind) has no
    comparable measure and is left out.

Youth (15-24) headline rates differ across countries mostly because of who is in the
youth labour force. A full-time student looking for a part-time job counts as unemployed:
about 60% of unemployed 15-24-year-olds in Sweden are students, and 30% in the US. The
rate also divides by a small youth labour force where few teenagers work.

## Validation

The model is tuned only on history: real households are started in a past survey and run
through what actually happened, then compared with the **same birth years** in later
surveys, never with another generation. Forecasts from today are not tuned to look
plausible.

### United States: the SCF's households

`python -m econsim.backtest` starts every couple in the Fed's Survey of Consumer Finances
whose head was aged 25-60 in the 2004, 2007, ..., 2022 waves (about 10,000 households).
Each starts from its own ages, degree, children, wages, savings, retirement accounts,
home, mortgage and student debt.

They then live through what actually happened (`us_history.csv`, from FRED, BLS and the
Kenneth French data library): the price of each cost category, rents, home prices, the
mortgage rate for new loans, stock and bond returns, deposit rates and unemployment. They
earn their own generation's wages: the median wages of couples of their birth years and
degree in each later wave.

The results are compared with the same birth years (5-year cohorts) and degree in every
later wave, on two measures: median wealth (financial assets + home equity − student
debt) and the share who own. That makes 359 cohort cells, never another generation.

Result with the defaults:

| | Bias | Mean absolute error |
|---|---|---|
| Wealth (years of the cohort's median wages) | −0.08 (v0.9: −0.08) | 0.74 (0.74; ~0.5 of it is sampling noise in the survey cells and the model's sample) |
| Home ownership (percentage points) | −0.5 (−0.5) | 5.3 (5.3) |
| Spread: 25th / 75th percentile of wealth | +0.03 / −0.68 (+0.07 / −0.68) | |
| Bottom / top third by wages | −0.52 / +0.10 (−0.44 / +0.10) | |

By degree:

- Couples with a degree are −0.28 at the median, −0.34 for their top third by pay, and
  −1.4 for their top quarter of wealth.
- Couples without a degree are +0.11, with their top quarter on target.

The bias grows with the horizon: −0.02 up to 6 years, −0.46 beyond 12. The top of the
wealth distribution also reflects inheritances, own businesses and returns above the
market's, which the model does not have. The bottom third by wages holds about half a
year's wages too little in every variant. Couples who started young (head under 32) end
up with about the right wealth (−0.11) but own 8 points too rarely: they hold more in the
bank and less in a home than the survey's couples.

The backtest found, and fixed, problems that a forward-looking check cannot see:

| Found | Fix |
|---|---|
| Wealth 1.9× too high after 3 years, 3× after 21 | Spending: the survey basket raised to national-accounts totals in full (the US Consumer Expenditure Survey records 50-65% of what households spend on several categories), and a calibrated level. A wealth effect of 4% of free savings a year (Carroll, Otsuka & Slacalek 2011 find 2-6 cents per dollar; the backtest cannot pin it down, as few couples hold much above the emergency fund) |
| The model held 84% of investments in shares, as the household sector does (led by the wealthiest); typical families hold ~50-60% | Portfolios of the bottom 70% by wealth (ECB DWA, SCF) |
| Low-income couples ran up six-figure debts | A borrowing limit (beyond it, living costs are cut), Medicaid / CHIP, the EITC, and rent and cars that follow income |
| The SCF's public files state every wave in the latest survey's dollars (2025), not that year's | Each wave converted back to its own year's dollars (CPI). An apparent "ageing" of owned homes (−0.3 to −2.5%/yr vs the price index) was this mistake: in nominal terms SCF owners' homes kept pace with the index (or gained, trading up), so homes now follow it |
| A price series missing its latest year (car insurance, 2025) silently broke the households that pay it | A missing year counts as the year before |
| The spending levels were fitted wrongly (v0.9) | See below |

**How the spending levels are fitted.** Each country's level is fitted so that a
population of couples saves the national household saving rate, measured on what the
couples *plan* to spend. Until v0.8, the fit used what they ended up spending, after the
cuts the model makes when money runs short. Those cuts keep saving from falling much below
zero, so a level far too high still "fitted" wherever the national rate is low: every
couple's spending was cut instead.

The US level fell from 1.49 to 1.32. The US backtest, which never sees the national rate,
does best between 1.3 and 1.32:

| US spending level | 1.0 | 1.15 | 1.3 | 1.49 |
|---|---|---|---|---|
| Wealth bias | +0.54 | +0.21 | −0.05 | −0.27 |
| Mean absolute error | 0.87 | 0.75 | 0.74 | 0.77 |

Canada, Italy, the UK, Greece, Bulgaria, Romania and the Baltic states also fell. Where
the cuts never bound (Germany, France, the Netherlands, ...), nothing changed.

**How strongly spending follows income** (the budget elasticity) is fitted on both
backtests at once, this one and Europe's below, refitting every country's spending level
to its national saving rate at each value. With one elasticity for every income, the
model's households ended up too alike. The top quarter of each cohort, and the top third
by earnings, held far less than the survey's: a couple in the top 10% of pay saved almost
nothing.

So the backtest also compares the spread within each cell: the 25th and 75th percentiles
of wealth, and the median of the bottom and top third by wages. The elasticity may also
differ below and above the survey couple's income. In the table, wealth is in years of the
cohort's median wages, and "spread" is the mean absolute error of those four measures; the
runs used the v0.7 spending levels.

| Below / above | US median bias / MAE | Top quarter | Top third by pay | Spread | Degree / no degree | Europe bias / MAE |
|---|---|---|---|---|---|---|
| 0.8 / 0.8 (v0.6) | −0.36 / 0.78 | −1.36 | −0.79 | 1.31 | −0.60 / −0.11 | +0.28 / 0.89 |
| 0.6 / 0.6 | −0.54 / 0.84 | −1.37 | −0.65 | 1.41 | −0.61 / −0.48 | +0.17 / 0.91 |
| 0.8 / 0.4 | −0.41 / 0.77 | −1.22 | −0.48 | 1.30 | −0.59 / −0.25 | +0.24 / 0.89 |
| 1.0 / 0.8 | −0.28 / 0.79 | −1.30 | −0.76 | 1.29 | −0.58 / +0.04 | +0.39 / 0.91 |
| 1.0 / 0.4 | −0.29 / 0.77 | −1.12 | −0.42 | 1.27 | −0.56 / −0.03 | +0.35 / 0.90 |
| **1.0 / 0.4, portfolios that grow with savings (default)** | **−0.27 / 0.77** | **−0.98** | **−0.27** | **1.27** | −0.52 / −0.03 | +0.35 / 0.90 |

All nine combinations of 0.6 / 0.8 / 1.0 below and 0.4 / 0.6 / 0.8 above were run; 0.6
below is worse on every US measure. Below the middle, spending is proportional to income:
the saving rate does not rise with income there (Straub 2019 finds ~0.7 overall). Above
it, richer households save a larger share (Dynan, Skinner & Zeldes 2004).

Kept:

- **Portfolios that grow with savings.** Savings are held as typical households hold
  them, moving toward the whole household sector's mix as they grow (wealth-weighted, so
  led by the wealthiest: more shares). Halfway at 1 year's income fits better than at 3.

Tested on the same cells and not used, because they gave no clear gain:

- **A wealth effect that falls as savings grow relative to income** (Fagereng, Holm &
  Natvik 2021: wealthier households spend less of a windfall). It had no measurable effect
  (top quarter −1.06 vs −1.12), as few couples hold much above the emergency fund.
- **Renters saving for a home spend 30% / 50% less on extras until they buy** (Engelhardt
  1996). Young couples' ownership improved to −6 / −4 points instead of −8, but ownership
  overall went to +1 / +2.4 points, and wealth was no better. The backtest's renters all
  plan to buy at once, older ones too.
- **Keeping only 2 months' spending (or none) after buying**, instead of the emergency
  fund. Ownership went to −0.2 / +0.1 instead of −0.5, and young couples to −7.8 / −7.6:
  within noise.

### Euro area: the HFCS tables

The euro area's wealth survey (ECB HFCS, waves 2010, 2014, 2017 and 2021, in 15-22
countries) publishes no household records without an application, and its tables by age
only as PDFs. `tools/hfcs_tables.py` compiles all four waves' tables into
`reference/hfcs_tables.csv`: net wealth, ownership, home value, financial assets, debt and
gross income, by 10-year age band of the reference person, plus each country's fieldwork
dates. The build converts non-euro countries to their currency.

`python -m econsim.backtest_eu` then starts a small synthetic population of couples in
every country and wave, where the survey found households aged 30, 40 and 50:

- **Incomes and wealth.** Five income levels around the median household income of that
  age. Owners and renters come in the survey's proportion (more owners at higher incomes),
  owners with the median home value and debt. Financial assets are scaled so the
  population's median net wealth is the survey's.
- **Buying.** Renters start trying to buy at the ages at which the same survey's older
  households own more (start-wave data only); the rest rent for good.
- **History.** The couples live through their country's history (`eu_history.csv`: the
  HICP and each budget line's index, average wages, house prices, rents, ECB rates on new
  mortgages, bond and money-market rates, deposit rates, unemployment, and European stock
  returns in local currency). They earn what their cohort's households earned in the later
  waves.
- **Comparison.** They are compared with the same birth years in each later wave, on net
  wealth without occupational pensions (as the HFCS measures it).

The same method, run on the SCF laid out as HFCS tables (`scf_tables.csv`), checks its own
error, because the US household backtest gives the answer it should reproduce. At v0.6's
spending defaults:

- Wealth: −0.42 (households: −0.36), with a mean absolute error of 0.66.
- Ownership: −9.4 points (households: −0.8). Because the couples are built from medians,
  young renters hold too little to buy on time, so the table method itself reads
  ownership ~9 points low.

Result for 22 countries, 315 cells (start waves 2010-2017, 3-11 years later):

| | Bias | Mean absolute error |
|---|---|---|
| Wealth (years of the cohort's median income) | +0.33 (v0.9: +0.39) | 0.89 (0.91) |
| Home ownership (percentage points) | −3.9 (−3.3; within the method's own −9) | 5.8 (5.3) |

The error does not grow with the horizon. By country (mean error, in years of income):

- **Too much wealth:** Croatia (+2.0), Spain (+1.4: the 2012-14 house-price fall and young
  Spaniards' weak incomes), Cyprus, Estonia, Portugal and Slovenia (+1.0).
- **Too little:** Italy (−0.7) and Germany (−0.7). In Italy, young households' median
  wealth triples between 30 and 40, from inheritances and family help the model does not
  have.
- Most others are within ±0.5.

### Same model in every country

| | All countries | Where the data differ |
|---|---|---|
| Careers | Four situations, the same rules | US: CPS microdata; elsewhere national LFS shares, US pay ratios and moves |
| Generation gaps | Today's brackets + the part of the gap that lasts | US: CPS ASEC, SCF; Europe: SES earnings (persistence from the US), LFS unemployment |
| Spending | Survey basket raised to national accounts, × the country's level; the same income, housing, car and wealth responses | Each country's own surveys and accounts |
| Spending level (shown as "spending surveys do not record") | Fitted so a population of couples (25-64, pay percentiles 25-75, with and without children) saves the national accounts' household net saving rate on planned spending. The rate is the OECD's, else Eurostat's gross rate less the median depreciation gap, and at least 0% (national accounts that show households dissaving mostly miss informal income) | US 1.32, Germany 1.19, Italy 1.19, Spain 1.01, UK 0.93. Hungary and Switzerland are at the 0.7 floor: their national rates include workplace pension saving that the model does not hold as accounts |
| Starting wealth by age | The median of households that age, owners and renters | US: SCF couples by degree; euro area: HFCS, all households by age; elsewhere enter yours |
| Backtests | The same model and behaviour, started in past surveys | US: SCF households; euro area: HFCS tables (checked on the SCF) |
| Savings | Typical households' deposit and stock shares | ECB DWA (euro area), SCF (US); elsewhere the sector moved by the median gap |
| Homes | Follow the price index and income, and the lending rules | Refinancing only where loans can be repaid without penalty (US, DK) |
| Means-tested help | OECD tax-benefit curves outside the US | US: EITC, CTC, Medicaid / CHIP |

`python -m econsim.calibrate` prints each country's population saving rate against its
national accounts (`spending_level.csv`, refitted after every data build). The calibrated
spending beyond the survey basket is shown as its own line, "spending surveys do not
record": interest on consumer debt, gifts and transfers, giving, and irregular big costs.

## Data

Everything comes from official APIs, except what no API publishes comparably (curated in
`econsim/params/`, below). The main tables in `econ_data/`:

| Table | What | Source |
|---|---|---|
| `earnings_profile.csv`, `education_pay.csv` | Gross earnings by age and percentile; pay by degree | Eurostat SES (nowcast with national-accounts salaries), ONS ASHE, BLS CPS, StatCan LFS; OECD Education at a Glance |
| `tax_curve.csv` | Income tax and social contributions by multiple of the average wage | OECD Taxing Wages, Eurostat |
| `couple_budget.csv`, `budget_coverage.csv` | The survey couple's spending by category; national-accounts consumption ÷ survey spending per category | Eurostat HBS, BLS CE, StatCan SHS, ONS Family Spending; Eurostat `nama_10_co3_p3`, BEA PCE |
| `life_events.csv` | Home prices, down payments, transfer taxes, mortgage rates, rents, childcare, children's costs, emergencies | Eurostat, OECD, NAR, Census ACS, CMHC, reference values |
| `labour_market.csv`, `us_career.csv`, `part_time.csv` | Unemployment, slack, over-qualification by age and degree; US situations, pay, moves and raises; part-time shares | Eurostat LFS, CPS basic monthly microdata (Census API), OECD, StatCan, ONS |
| `cohort.csv` | Statistics by single year of age × survey year × degree, for following cohorts | Census CPS ASEC 2003-2026, Fed SCF 1989-2025, Eurostat LFS 2000-2025, SES 2006-2022 |
| `us_history.csv`, `eu_history.csv` | What happened year by year: prices and each cost category, wages, home prices, rents, rates, returns, unemployment | FRED, BLS, Kenneth French data library; Eurostat, ECB, OECD, BIS |
| `scf_households.csv`, `scf_tables.csv`, `hfcs_tables.csv` | The backtests' starting points and targets | Fed SCF summary extracts; ECB HFCS statistical tables |
| `start_wealth.csv` | What US couples of each age and degree hold now | Fed SCF 2025 |
| `family.csv` | Mothers' employment × hours vs women without children; average age of leaving home | Eurostat LFS, CPS microdata, StatCan |
| `pensions.csv` | Net replacement rates, future retirement ages, life expectancy at 65 | OECD Pensions at a Glance |
| `rates.csv`, `mortgage_longrun.csv`, `portfolio.csv` | Yields, short rates, deposit and mortgage rates; how households hold their savings | OECD, Eurostat, ECB MIR and DWA, FDIC, BoE, SSB, SNB, Freddie Mac |
| `price_outlook.csv`, `market_index.csv` | Each budget line's price trend; US inflation expectations and household cost index; home-price, rent and fuel indices | Eurostat HICP, BLS CPI, FRED, BIS, NAR, EIA |
| `spending_level.csv` | Each country's calibrated spending level and the saving rate it reproduces | `econsim.calibrate` |
| `econ_long.csv` | Every observation, tidy, with its source and comparability note | All of the above |

Survey-based saving rates (Eurostat's experimental `icw_sr_*`, 30-50% in most countries)
are not used: they pair surveyed incomes with surveyed spending, which surveys
under-record.

### Curated parameters (`econsim/params/`)

These are things no statistics API publishes comparably. Every value has a source and a
quality grade (A statutory/official, B official but approximated, C estimate), and the
app's Sources tab lists them. They ship with the code, so changing one needs no data
rebuild.

| File | Contents |
|---|---|
| `housing.csv` | New-lease market rents (~70 m², excl. utilities); existing-home prices (national median per m² × 85 m², or per home) |
| `lending.csv` | Minimum down payment (and for young buyers), payment-to-income / loan-to-income caps and stress rates, typical term, rate-fixation period and 2005-2025 average rate, transfer tax and closing costs, property tax, home insurance, mortgage-interest relief, borrower insurance |
| `household.csv` (+ `household_sources.csv`) | Cars (price, insurance, tax, fuel, km, maintenance, tolls, transit pass), health premiums and out-of-pocket costs, parental leave (months, replacement, caps), free pre-school and school ages, compulsory occupational pension contributions, pension replacement rate, employer match, investment tax drag, wealth tax, typical wedding cost, long-run real wage growth |
| `student_debt.csv` | Typical student debt by degree (US, UK, Canada) |
| `us_states.csv` | Health-premium and car-insurance factors and state paid family leave (curated). The ACS columns (recent-mover rents, home values, property taxes, full-time earnings) and BEA price parities are refreshed from the Census and FRED APIs by the build; the CSV values are the fallback |
| `us_state_tax.csv` | 2026 state income tax brackets, deductions and exemptions (Tax Foundation), local income tax, state payroll taxes |

## Project layout

| Path | What |
|---|---|
| `run.py` | Setup and start (standard library only): venv, packages, data, tests, the app |
| `build_econ_dataset.py` | Runs the dataset builder (`econdata.build`) |
| `econdata/build.py` | The builder's command line: every source, the derived tables, writing, the summary |
| `econdata/fetch.py`, `clients.py`, `tidy.py` | HTTP with a disk cache and retries; clients for Eurostat, OECD, FRED, World Bank and StatCan; the tidy row layout |
| `econdata/config.py`, `version.py`, `references.py` | Countries, currencies and comparability notes; `DATA_VERSION`; hand-curated reference values |
| `econdata/sources/` | One function per source, grouped by topic (income, labour, cohorts, history, saving, prices, spending, housing, energy and health, life events, uncertainty) |
| `econdata/derive/` | The derived tables, grouped by topic (earnings, budget, life events, tax, labour, wealth, macro, history, snapshot) |
| `econsim/model.py` | `Sourced` (value + provenance), `CountryData`, `Plan` |
| `econsim/data.py`, `params.py`, `params/*.csv` | Builds `CountryData` from the tables and the curated parameters (+ US state) |
| `econsim/inputs.py` | Every input of a run resolved with its source; starting wealth; the horizon |
| `econsim/engine.py` | `simulate()`: one household, year by year; `to_display()` for real/nominal and currency |
| `econsim/household.py`, `money.py`, `assumptions.py` | The parts of a year (prices, pay, leave, costs, loans, cuts, summary); money arithmetic; the model's calibrated behaviour and fixed rules |
| `econsim/tax.py`, `career.py`, `cohort.py` | Taxes (local, US residents, US citizens abroad); careers; generation gaps |
| `econsim/history.py`, `backtest.py`, `backtest_eu.py`, `calibrate.py`, `validate.py` | What actually happened; the backtests; the spending levels; the cohort checks |
| `app.py` | The Streamlit page: sidebar → plan → simulation → header and tabs |
| `ui/sidebar.py`, `plan.py`, `header.py` | The sidebar (one function per section), the `Plan` it describes, the headline numbers |
| `ui/tab_*.py` | One module per tab; each `render(page)` draws from the run's results |
| `ui/settings.py`, `names.py`, `share.py` | Settings kept in the browser; their fixed names and migration; the share link |
| `ui/charts.py`, `common.py` | Chart style (palette, layout, event markers) and labels for sourced values |
| `components/settings_store/` | A tiny Streamlit component (plain HTML/JS) that keeps the settings in the browser |
| `reference/hfcs_tables.csv`, `tools/hfcs_tables.py` | The ECB HFCS statistical tables (PDF only), compiled into one table, and the script that does it |
| `tools/golden.py`, `app_golden.py`, `build_golden.py` | Exact-results checks for refactoring (below) |
| `tools/rent_research.py`, `econsim/params/renting.csv` | The data behind the renting assumptions (US: Census ACS PUMS, CPS ASEC; every country: OECD, EU-SILC) and the table it writes |
| `tests/` | Engine (synthetic country), life events, curated rules, every country's invariants, careers, cohorts, backtests, the builder's parsers, settings, and the app (Streamlit AppTest) |

## Development

```
.venv/bin/pip install -r requirements-dev.txt     # ruff, vulture, pylint (in the venv only)
.venv/bin/python -m pytest -q                      # tests (UI and backtest tests need the data)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs the linter, the formatter check and the tests on every push. On a release, it
runs the full suite on the built data (above). `FRED_SMOKE=1 pytest tests/test_fred.py`
checks the live FRED endpoints.

**Changing code without changing results.** Record every result first, then check after
each step. All three checks must report everything identical:

```
.venv/bin/python -m tools.golden save         # ~200 plans: every country, US states, careers,
.venv/bin/python -m tools.app_golden save     #   owners, the backtests' history path; the app's pages
.venv/bin/python -m tools.build_golden save   # the whole data build, offline from the download cache
... edit ...
.venv/bin/python -m tools.golden check
.venv/bin/python -m tools.app_golden check
.venv/bin/python -m tools.build_golden check  # run on the same day as `save`
```

The recordings are kept next to the data (`econ_data/golden*/`), because they depend on it.
Save them again after a deliberate change to the model.

**Changing the model.** Judge a change of behaviour on the backtests
(`python -m econsim.backtest`, `python -m econsim.backtest_eu`), not on whether today's
forecast looks plausible. Then refit the spending levels (`python -m econsim.calibrate`,
or rebuild the data).

**Settings.** Every setting has a fixed name (`ui/names.py`), such as `w:p1_age` or
`k:rent@USA-CA` (per place). Those names are what the browser saves and what share links
carry, so they must not change. A renamed setting gets an entry in `migrate` instead:
version 1 links (v0.8) are migrated, and links made by a newer version than the one
opening them are refused rather than half-applied. Settings whose default depends on
others (e.g. the marriage age on partner 1's age) follow their default until you change
them.

## Known limitations

- **Careers outside the US.** Only the US has matched panels. Elsewhere, careers use the
  US pay ratios between situations and the US pace of moves, with each country's own mix.
  The CPS pools both sexes and parents with non-parents (the child-penalty module adjusts
  the main parent).
- **Europe's backtest** rests on the HFCS tables, not on households: all households
  (singles too) by 10-year age band, with the couples rebuilt from medians. On the US,
  this method reproduces wealth as the household backtest does, but reads ownership ~9
  points low. The UK, the Nordic countries outside the HFCS, Switzerland and Canada have no
  wealth backtest.
- **Workplace pensions.** Defined-contribution pensions outside the US (UK auto-enrolment,
  NL, CH) are not separate accounts: the OECD replacement rate covers mandatory schemes
  only.
- **Tight budgets.** With realistic spending, many couples with children live pay cheque
  to pay cheque. For them, more rent means less other spending, not less wealth.
- **Starting wealth by age** is available for the US (SCF, couples by degree) and the euro
  area (HFCS 2021, all households by age only); elsewhere, enter yours.
- **Generation gaps for Europe** rest on four SES waves in 10-year bands, and borrow from
  the US how long gaps last.
- **Singles.** The US backtest is on couples: singles are not tested.
- **Renting outside the US.** How rent follows income and how often renters move are
  measured for each country from published tables (OECD, EU-SILC), scaled by the US
  microdata, not from each country's own microdata. The moves are from 2012. The
  tenure discount is the US one, scaled by how often renters move. European rent
  factors for children are EU-SILC averages without an income control (some are noisy,
  e.g. Lithuania 1.9 for two children).
- **Young buyers.** Forward from today, couples who start with little saved rarely manage
  a down payment. For example, two US graduates at 25 buy at 68 in the default plan, while
  the US median first-time buyer is 40. This is today's prices and rates set against young
  couples' saving: the housing squeeze people report. It is a prediction of a method
  checked on history, not something tuned away. In the backtests, ownership comes out
  right for couples who started from their real savings (US) or the survey's (euro area),
  though couples who started young own 8 points too rarely.
- **Regional job markets.** Labour-market rates are national: US states use the national
  rates (pay follows each state's full-time earnings).
- **Field of study.** Pay by degree is a national average gap; field of study, which
  matters as much, is not modelled.
- **Estimates.** Several rents, home prices, car costs and wedding costs are estimates
  (quality C). Edit `econsim/params/*.csv` if you know better values.
- **No budget survey** here for Switzerland, Iceland and Liechtenstein: they use a
  neighbouring country's quantities at local prices, scaled to local incomes.
- **Hungary and Switzerland** sit at the spending level's floor (0.7). Their national
  saving rates include workplace pension saving the model does not hold as accounts.
- **Family help and inheritances** are an input, not a forecast. In Southern Europe they
  are how many young people buy: in the HFCS backtest, young Italians' wealth triples
  between 30 and 40, and the model's does not.
- **Retirement.** There are no survivor pensions, and neither partner dies before the
  horizon. Pension-pot withdrawals are not taxed separately (the OECD net rates tax the
  mandatory pension only). Basic/minimum pensions for people with short careers are
  covered only by the safety net.
- **Savings.** The savings mix starts as typical households' (bottom 70% by wealth) and
  moves toward the whole household sector's as savings grow. Interest on deposits is not
  taxed.
- **The top of each generation's wealth distribution** is still too low. In the US, the
  75th percentile is about a year's wages short, and nearly two for couples with degrees.
  Inheritances, own businesses and returns above the market's are not modelled. Europe's
  backtest checks medians only, because the HFCS tables give no spread by age.
- **Shocks.** Divorce, disability and health shocks are not modelled; use the job-loss
  scenario for an income shock.
- **Also not modelled:**
  - means-tested health subsidies (CH premium reductions, NL zorgtoeslag);
  - Hungarian Babaváró loans;
  - child benefits that vary with the child's age;
  - part-time work after children (use the leave slider or career growth);
  - US expat state tax;
  - PFIC and NIIT.

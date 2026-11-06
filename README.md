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
container `HOST`, `PORT` (8501), `OPEN_BROWSER` and `BASE_URL_PATH` (`econ`) are set in
`compose.yaml`; everything else comes from `config.env`. In the Cloudflare dashboard, the
tunnel's public hostname uses path `econ` and service `http://econ:8501`. To refresh the
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
`--reference my_refs.csv`. Set `CENSUS_API_KEY` (free) to get ACS rents; without it the
US rent comes from `reference_values.csv`.
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
| `econsim/model.py` | `Sourced` (value + provenance), `CountryData`, `Plan` |
| `econsim/data.py` | Builds `CountryData` from the derived CSVs (never reads `econ_long.csv`) |
| `econsim/tax.py` | Local tax curves; US federal tax, FEIE + housing exclusion vs FTC, ACTC, FICA |
| `econsim/engine.py` | Year-by-year simulation; `to_display()` for real/nominal and currency |
| `app.py` | Streamlit + Plotly UI (no modelling code) |
| `tests/` | `test_engine.py` (synthetic country), `test_app.py` (Streamlit AppTest) |

New derived tables for the simulator: `earnings_profile.csv` (annual gross by age band
and percentile), `tax_curve.csv` (income tax + employee SSC at multiples of the average
wage), `fx_ppp.csv`, `context_series.csv`.

## Model in one paragraph

Each partner's gross pay follows the country's cross-sectional age-earnings profile at
the chosen percentile (the career-growth slider raises it to a power), plus economy-wide
real wage growth and inflation. Each partner is taxed individually on the local average
tax-rate curve. Child benefits are the OECD net-income difference with and without
children. For US citizens abroad, the cheaper of FEIE + FTC and FTC-only is chosen each
year; with FTC, the child credit becomes refundable. Spending is the survey couple
budget (rent and mortgage handled separately), plus per-child spending, childcare, an
expected emergency cost, the wedding, and the house purchase. The purchase waits until
the deposit and costs can be paid without breaking the emergency fund, *and* the
payment is under the payment-to-income cap. A job loss uses OECD net replacement rates
month by month. Savings fill the emergency fund in cash first; the rest is invested.

## Known limitations

* Joint-taxation countries (DE, FR, PT, CH, LU, US) are slightly overstated for unequal couples.
* Pensions are not modelled. The default horizon is the last working year.
* Home price defaults for most of Europe are Deloitte **new-build, city-weighted**
  prices per m² × 100 m²; a suburban resale home is usually cheaper. Countries without
  Deloitte data use a price-to-income estimate (quality C).
* EU HBS budgets are from 2015/2020 and rolled forward with HICP. "Two adults" includes older couples.
* Switzerland, Iceland and Liechtenstein have no budget survey here. Their budgets are a
  neighbouring country's quantities at local prices (Eurostat category PPPs, estimate).
* US tax: no state tax for expats, no PFIC/NIIT, and excess FTC is not carried forward.

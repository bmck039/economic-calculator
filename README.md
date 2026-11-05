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
`compose.yaml`; everything else comes from `config.env`.

**Auto-update.** The container follows the latest *stable* GitHub release of
`UPDATE_REPO` (drafts and pre-releases are ignored), checking every `UPDATE_CHECK_HOURS`.
A new release is downloaded into the `econ-state` volume and set up there by its own
`run.py setup` (packages, a fresh data build from the shared download cache, the tests)
while the current version keeps serving. Only if all of that passes does the app switch,
with a few seconds of downtime. A failing release is retried at the next two checks
(a data source may just have timed out) and then skipped until a newer one is published.
If the tests fail right after a data build, the next start rebuilds the data (reusing
what was already downloaded) instead of failing the same way again. Each release also
rebuilds the data, so publishing one is also how to refresh it.

**Docker setup changes.** The container can't rebuild its own image, so changes to the
`Dockerfile`, `compose.yaml` or `.dockerignore` are applied on the host by
[docker-autoupdate](https://github.com/bmck039/docker-auto-update), vendored here as
`docker_autoupdate.py`. On the server, in a dedicated clone of this repo (on macOS
outside `~/Documents`, `~/Desktop` and `~/Downloads`), run once:

```
python3 docker_autoupdate.py install
```

Every 6 hours it checks for a new stable release. It checks the clone out at that tag
and, only if the release changed the Docker setup, runs `docker compose up -d --build`;
a failed rebuild is rolled back. Code-only releases are left to the in-container updater.

**To ship a change:** push it, then publish a GitHub release (e.g. tag `v1.2.0`).

Manual equivalent: `pip install -r requirements.txt`, `python build_econ_dataset.py`,
`streamlit run app.py`, `pytest -q`. Useful pipeline flags: `--derive-only` (rebuild the derived tables from the last
`econ_long.csv`, no network, ~2 min), `--profile` (cProfile top 25), `--refresh`,
`--reference my_refs.csv`. Set `CENSUS_API_KEY` (free) to get ACS rents; without it the
US rent comes from `reference_values.csv`.

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

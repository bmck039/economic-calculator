"""Clients for the statistical APIs (Eurostat, OECD, FRED, World Bank, StatCan)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests

from econdata.config import COUNTRIES, EUROPE, EUROSTAT_TO_ISO3
from econdata.fetch import CachedFetcher, FetchError, is_json, is_zip

log = logging.getLogger("econ")


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
    items = (
        values.items()
        if isinstance(values, dict)
        else ((i, v) for i, v in enumerate(values) if v is not None)
    )
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


def eurostat(
    f: CachedFetcher,
    dataset: str,
    required: dict[str, list[str]] | None = None,
    optional: dict[str, list[str]] | None = None,
    since: int | None = None,
    geo: list[str] | None = None,
) -> pd.DataFrame:
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
        df = parse_jsonstat(
            f.get(
                EUROSTAT_API + dataset,
                build({**required, **optional}, geo),
                label=f"eurostat:{dataset}",
                validate=is_json,
            )
        )
    except FetchError as e:
        if "HTTP 400" not in str(e):
            raise
        # Ask the dataset for its real structure (one tiny request), then keep only the
        # filters that exist. The country filter is never dropped: dropping it used to
        # download every country in the world.
        dims = eurostat_dims(f, dataset)
        bad_req = {k: v for k, v in required.items() if k not in dims or not set(v) & dims[k]}
        if bad_req:
            raise FetchError(
                f"Eurostat {dataset}: required filters {bad_req} not in "
                f"structure {{{', '.join(f'{k}: {sorted(v)[:8]}' for k, v in dims.items())}}}"
            )
        pruned = {k: [c for c in v if c in dims[k]] for k, v in optional.items() if k in dims}
        pruned = {k: v for k, v in pruned.items() if v}
        dropped = sorted(set(optional) - set(pruned))
        geos = [g for g in (geo or []) if g in dims.get("geo", set())] or None
        log.warning(
            "Eurostat %s rejected the query; dimensions are %s. Dropping filters %s "
            "and retrying (country filter kept)",
            dataset,
            list(dims),
            dropped,
        )
        df = parse_jsonstat(
            f.get(
                EUROSTAT_API + dataset,
                build({**required, **pruned}, geos),
                label=f"eurostat:{dataset}:pruned",
                validate=is_json,
            )
        )
    if "geo" in df:
        df = df.assign(iso3=df["geo"].map(EUROSTAT_TO_ISO3)).dropna(subset=["iso3"])
    return df


def eurostat_dims(f: CachedFetcher, dataset: str) -> dict[str, set[str]]:
    """Dimension -> set of codes, from a 1-period, 1-country query."""
    js = json.loads(
        f.get(
            EUROSTAT_API + dataset,
            [("format", "JSON"), ("lang", "EN"), ("lastTimePeriod", "1"), ("geo", "DE")],
            label=f"eurostat:{dataset}:structure",
            validate=is_json,
        )
    )
    dims = {}
    for d in js["id"]:
        cat = js["dimension"][d]["category"]
        dims[d] = set(cat.get("label", {}).keys()) | set(
            cat["index"] if isinstance(cat.get("index"), list) else cat.get("index", {})
        )
    dims["geo"] = set(EU_GEOS)  # the probe only had DE; geo codes are standard
    return dims


def dimcol(df: pd.DataFrame, *names: str) -> str:
    """First of `names` that is a column (Eurostat renames dimensions, e.g.
    indic_il -> statinfo, hhtyp -> hhcomp, quantile -> quant_inc)."""
    for n in names:
        if n in df.columns:
            return n
    raise FetchError(
        f"none of {names} in columns {[c for c in df.columns if not c.endswith('_label')]}"
    )


EU_GEOS = [COUNTRIES[c][1] for c in EUROPE]


OECD_API = "https://sdmx.oecd.org/public/rest/data/"


def sdmx_key(dims: list[str], filters: dict[str, list[str]]) -> str:
    return ".".join("+".join(filters.get(d, [])) for d in dims)


def oecd(
    f: CachedFetcher,
    flow: str,
    dims: list[str],
    filters: dict[str, list[str]],
    since: int | None,
    version: str = "latest",
) -> pd.DataFrame:
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


def oecd_rows(df: pd.DataFrame, cols: dict) -> pd.DataFrame:
    """An OECD SDMX table's country, period and value, plus `cols` (other tidy columns)."""
    return pd.DataFrame(
        {"iso3": df["REF_AREA"], "period": df["TIME_PERIOD"], "value": df["OBS_VALUE"], **cols}
    )


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
            raw = f.get(
                FRED_API,
                {"series_id": series_id, "api_key": key, "file_type": "json"},
                label=f"fred-api:{series_id}",
                validate=is_json,
                timeout=30,
            )
            return fred_api_frame(raw, series_id)
        except FetchError as e:
            if "api_key" in str(e):  # bad key: say so once, then use the CSV endpoint
                log.error("FRED_API_KEY was rejected (%s); using the CSV endpoint", str(e)[:200])
                f._host_fails[api_host] = f.host_fail_limit
            else:
                log.warning("FRED API %s failed (%s); trying the CSV endpoint", series_id, e)
    raw = f.get(
        FRED_CSV,
        {"id": series_id},
        label=f"fred:{series_id}",
        headers=FRED_CSV_HEADERS,
        timeout=FRED_CSV_TIMEOUT,
    )
    return fred_csv_frame(raw, series_id)


def fred_api_frame(raw: bytes, series_id: str) -> pd.DataFrame:
    obs = json.loads(raw).get("observations")
    if obs is None:
        raise FetchError(f"FRED API {series_id}: no observations in the response")
    df = (
        pd.DataFrame(obs, columns=["date", "value"])
        if obs
        else pd.DataFrame(columns=["date", "value"])
    )
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


def _annual(d: pd.DataFrame) -> pd.Series:
    """Monthly (or weekly / daily) FRED values -> calendar-year means; a year counts when
    at least 11 months are there (October 2025 was not collected: federal shutdown)."""
    x = (
        pd.Series(
            pd.to_numeric(d["value"], errors="coerce").to_numpy(), index=pd.to_datetime(d["date"])
        )
        .dropna()
        .resample("MS")
        .mean()
    )
    x = x.interpolate(limit=1, limit_area="inside")
    n = x.groupby(x.index.year).count()
    a = x.groupby(x.index.year).mean()
    return a[n >= 12]


def fred_annual_chained(
    f: CachedFetcher, series_id: str, annual: pd.Series
) -> tuple[pd.DataFrame, int, float]:
    """A monthly FRED series as calendar-year means ("mean", with "size" = months), the
    last complete year it shares with `annual` (year -> value), and the factor that chains
    the series to `annual` in that year."""
    m = fred(f, series_id)
    m["year"] = m["date"].str[:4].astype(int)
    a = m.groupby("year")["value"].agg(["mean", "size"])
    base = int(max(y for y in annual.index if y in a.index and a.loc[y, "size"] == 12))
    return a, base, annual.loc[base] / a.loc[base, "mean"]


WB_API = "https://api.worldbank.org/v2/country/{c}/indicator/{i}"


def worldbank(
    f: CachedFetcher,
    indicator: str,
    iso3: list[str],
    since: int | None,
    sources: tuple[str | None, ...] = (None,),
) -> pd.DataFrame:
    last = None
    for src in sources:
        params = {"format": "json", "per_page": "20000"}
        if since:
            params["date"] = f"{since}:{datetime.now().year}"
        if src:
            params["source"] = src
        url = WB_API.format(c=";".join(iso3), i=indicator)
        try:
            js = json.loads(
                f.get(url, params, label=f"worldbank:{indicator}:{src}", validate=is_json)
            )
        except FetchError as e:
            last = e
            continue
        if not isinstance(js, list) or len(js) < 2 or js[1] is None:
            last = FetchError(f"World Bank {indicator} source={src}: {str(js)[:200]}")
            continue
        rows = [
            {"iso3": r["countryiso3code"], "date": r["date"], "value": r["value"]}
            for r in js[1]
            if r.get("value") is not None
        ]
        if rows:
            return pd.DataFrame(rows)
        last = FetchError(f"World Bank {indicator}: no data")
    raise FetchError(str(last))


def worldbank_indicators(
    f: CachedFetcher,
    indicators: dict[str, tuple[str, str]],
    iso3: list[str],
    since: int | None,
    sources: tuple[str | None, ...] = (None,),
) -> list[pd.DataFrame]:
    """Several World Bank indicators ({code: (statistic, unit)}) as rows with period,
    statistic, unit and series; one that fails is skipped (with a warning)."""
    parts = []
    for ind, (stat, unit) in indicators.items():
        try:
            d = worldbank(f, ind, iso3, since, sources=sources)
        except FetchError as e:
            log.warning("WB %s skipped: %s", ind, e)
            continue
        parts.append(
            d.rename(columns={"date": "period"}).assign(statistic=stat, unit=unit, series=ind)
        )
    return parts


STATCAN_ZIP = "https://www150.statcan.gc.ca/n1/tbl/csv/{pid}-eng.zip"


STATCAN_STD = {
    "REF_DATE",
    "GEO",
    "DGUID",
    "UOM",
    "UOM_ID",
    "SCALAR_FACTOR",
    "SCALAR_ID",
    "VECTOR",
    "COORDINATE",
    "VALUE",
    "STATUS",
    "SYMBOL",
    "TERMINATED",
    "DECIMALS",
}


def statcan(
    f: CachedFetcher,
    pid: str,
    geo: str | list[str] | None = "Canada",
    match: dict[str, str | list[str] | None] | None = None,
    since: int | None = None,
) -> pd.DataFrame:
    """Download a full StatCan table and filter it.

    `match` maps a keyword found in a dimension column's name (case-insensitive)
    to a substring (or list of substrings) its value must contain; None keeps all."""
    raw = f.get(STATCAN_ZIP.format(pid=pid), label=f"statcan:{pid}", timeout=900, validate=is_zip)
    match = match or {}
    geos = [geo] if isinstance(geo, str) else geo
    # Parsing a multi-million-row CSV costs 1-2 minutes even from cache: keep the
    # filtered result, keyed by the zip's content hash and the filters.
    key = hashlib.sha256(
        raw[: 1 << 20]
        + str(len(raw)).encode()
        + json.dumps([geos, match, since], sort_keys=True, default=str).encode()
    ).hexdigest()[:24]
    filtered = f.cache_dir / f"statcan_{pid}_{key}.pkl"
    if filtered.exists() and not f.refresh:
        return pd.read_pickle(filtered)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(
            n for n in z.namelist() if n.lower().endswith(".csv") and "metadata" not in n.lower()
        )
        out = []
        with z.open(name) as fh:
            for chunk in pd.read_csv(fh, dtype=str, chunksize=200_000, encoding="utf-8-sig"):
                if geos is not None:
                    chunk = chunk[chunk["GEO"].isin(geos)]
                if since:
                    chunk = chunk[
                        pd.to_numeric(chunk["REF_DATE"].str[:4], errors="coerce") >= since
                    ]
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

"""FRED fetching, API-key redaction and the build's country-coverage check.

The live checks only run with FRED_SMOKE=1 (they need the network):
    FRED_SMOKE=1 pytest tests/test_fred.py
"""
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_econ_dataset as b  # noqa: E402

CSV = b"observation_date,JTSJOR\n2024-01-01,5.4\n2024-02-01,.\n2024-03-01,5.2\n"
API = json.dumps({"observations": [
    {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": d, "value": v}
    for d, v in [("2024-01-01", "5.4"), ("2024-02-01", "."), ("2024-03-01", "5.2")]]}).encode()


class FakeFetcher(b.CachedFetcher):
    """Records requests and answers from `responses` (url -> bytes or exception)."""

    def __init__(self, tmp_path, responses):
        super().__init__(tmp_path)
        self.responses, self.calls = responses, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {}), kw))
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r


def expected():
    return pd.DataFrame({"date": ["2024-01-01", "2024-03-01"], "value": [5.4, 5.2]})


def check(df):
    pd.testing.assert_frame_equal(df.reset_index(drop=True), expected())


def test_csv_endpoint_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    f = FakeFetcher(tmp_path, {b.FRED_CSV: CSV})
    check(b.fred(f, "JTSJOR"))
    (url, params, kw), = f.calls
    assert params == {"id": "JTSJOR"}
    # Regression guard: FRED's edge stalled the pipeline's own UA; keep requests' default.
    assert kw["headers"]["User-Agent"].startswith("python-requests/")
    assert kw["timeout"] <= 30


def test_api_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "abc123")
    f = FakeFetcher(tmp_path, {b.FRED_API: API})
    check(b.fred(f, "JTSJOR"))
    (url, params, _), = f.calls
    assert url == b.FRED_API and params["api_key"] == "abc123" and params["file_type"] == "json"


def test_api_failure_falls_back_to_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "abc123")
    f = FakeFetcher(tmp_path, {b.FRED_API: b.FetchError("timeout"), b.FRED_CSV: CSV})
    check(b.fred(f, "JTSJOR"))
    assert [c[0] for c in f.calls] == [b.FRED_API, b.FRED_CSV]


def test_rejected_key_disables_api_for_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "bad")
    err = b.FetchError("HTTP 400: Bad Request.  The value for variable api_key is not registered.")
    f = FakeFetcher(tmp_path, {b.FRED_API: err, b.FRED_CSV: CSV})
    b.fred(f, "A")
    b.fred(f, "B")
    assert [c[0] for c in f.calls] == [b.FRED_API, b.FRED_CSV, b.FRED_CSV]


def test_csv_not_a_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    f = FakeFetcher(tmp_path, {b.FRED_CSV: b"<html>error</html>"})
    with pytest.raises(b.FetchError):
        b.fred(f, "NOPE")


def test_redact():
    assert b.redact("https://x/obs?series_id=A&api_key=SECRET&file_type=json") == \
        "https://x/obs?series_id=A&api_key=***&file_type=json"
    assert b.redact("https://api.census.gov/data?get=NAME&for=us:1&key=SECRET") == \
        "https://api.census.gov/data?get=NAME&for=us:1&key=***"
    assert b.redact("https://x/?monkey=1") == "https://x/?monkey=1"


def test_missing_countries():
    every = pd.DataFrame({"iso3": list(b.COUNTRIES)})
    assert b.missing_countries({"earnings_profile": every, "tax_curve": every}) == []
    no_us = every[every["iso3"] != "USA"]
    assert b.missing_countries({"earnings_profile": no_us, "tax_curve": every}) == ["USA"]
    assert b.missing_countries({"earnings_profile": pd.DataFrame(), "tax_curve": every}) == \
        sorted(b.COUNTRIES)


# ------------------------------------------------------------------ live (opt-in)
live = pytest.mark.skipif(os.environ.get("FRED_SMOKE") != "1",
                          reason="set FRED_SMOKE=1 to hit the live FRED endpoints")


@live
@pytest.mark.parametrize("use_api", [False, True])
def test_live_fred(tmp_path, monkeypatch, use_api):
    if use_api and not os.environ.get("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY not set")
    if not use_api:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    f = b.CachedFetcher(tmp_path, refresh=True)
    df = b.fred(f, "LEU0252881500A")          # CPS median weekly earnings (US earnings)
    assert len(df) > 20 and df["value"].gt(0).all()
    assert all(r["how"] == "network" for r in f.log_rows), f.log_rows

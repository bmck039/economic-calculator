"""HTTP with a disk cache, polite per-host spacing, retries and stale fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode, urlparse

import requests

from econdata.config import USER_AGENT

log = logging.getLogger("econ")


class FetchError(RuntimeError):
    pass


@dataclass
class CachedFetcher:
    cache_dir: Path
    ttl_days: float = 30.0
    refresh: bool = False
    min_interval: dict[str, float] = field(
        default_factory=lambda: {
            "sdmx.oecd.org": 4.0,  # OECD rate-limits anonymous use hard
            "fred.stlouisfed.org": 1.0,
            "api.stlouisfed.org": 0.6,  # FRED API: 120 requests/minute per key
            "www150.statcan.gc.ca": 1.0,
            "www.ons.gov.uk": 2.5,  # ONS returns HTTP 429 below ~2s spacing
            "api.census.gov": 1.0,
            "www.bankofengland.co.uk": 2.0,
            "data-api.ecb.europa.eu": 1.0,
        }
    )
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

    def get(
        self,
        url: str,
        params=None,
        *,
        label: str = "",
        timeout: float = 60,
        headers: dict | None = None,
        validate: Callable[[bytes], bool] | None = None,
        method: str = "GET",
        json_body: dict | None = None,
    ) -> bytes:
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
            return self._fallback(
                body_p, label, full, FetchError(f"{host} skipped: failed {self.host_fail_limit}x")
            )
        last_err: Exception | None = None
        timeouts = 0
        for attempt in range(self.max_attempts):
            self._throttle(host)
            t0 = time.monotonic()
            try:
                r = self.session.request(
                    method, full, timeout=(15, timeout), headers=headers, json=json_body
                )
                el = round(time.monotonic() - t0, 2)
                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 5 * 2**attempt
                    )
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
                meta_p.write_text(
                    json.dumps(
                        {
                            "url": redact(full),
                            "fetched_ts": time.time(),
                            "fetched_utc": datetime.now(timezone.utc).isoformat(),
                            "bytes": len(r.content),
                            "label": label,
                        }
                    )
                )
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
                time.sleep(3 * 2**attempt)
        self._host_fails[host] = self._host_fails.get(host, 0) + 1
        return self._fallback(body_p, label, full, last_err)

    def _fallback(self, body_p: Path, label: str, full: str, err) -> bytes:
        if body_p.exists():  # stale cache beats nothing
            log.warning("Using STALE cache for %s (%s)", label or full, err)
            self._log(label, full, "stale-cache", error=str(err))
            return body_p.read_bytes()
        raise FetchError(f"{redact(full)}: {err}")

    def _log(self, label: str, url: str, how: str, **kw) -> None:
        self.log_rows.append(
            {
                "label": label,
                "url": redact(url),
                "how": how,
                "time_utc": datetime.now(timezone.utc).isoformat(),
                **kw,
            }
        )


def redact(url: str) -> str:
    """Hide API keys in URLs that end up in logs, fetch_log.csv and cache metadata."""
    return re.sub(r"([?&](?:api_key|key)=)[^&]+", r"\1***", url)


def is_json(b: bytes) -> bool:
    return b.lstrip()[:1] in (b"{", b"[")


def is_zip(b: bytes) -> bool:
    return b[:2] == b"PK"

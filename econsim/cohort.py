"""Cohorts: how the people now at each age compare with the people who were that age before
them, and how much of such a gap has lasted historically.

Today's 35-year-olds are not a forecast of today's 33-year-olds in two years: if the
33-year-olds have been doing worse than the 35-year-olds did at the same age, for several
years running, they will most likely still be behind at 35. The data say how much:

  r(a, y)    a statistic at age a in survey year y, relative to all 25-64-year-olds in y
             (log for money and rates, log-odds for shares, a ratio for wealth)
  gap_h      how the cohort now aged a compares with the cohort h years older at the same
             age, averaged over the cohort's last few surveys (so a one-off bad year is not
             taken for a lasting gap):
               gap_h(a, y) = mean_j [ r(a - j, y - j) - r(a - j, y - j - h) ]
  forecast   r(a + h, y + h) = r(a + h, y) + phi_h * gap_h(a, y)
             phi_h = 1: the gap lasts in full; 0: the cohort ends up where the older one
             is now (the cross-section)
  phi_h      estimated from history by least squares over every age and survey year where
             the outcome is known: target r(a + h, y + h) - r(a + h, y) on gap_h(a, y).

The adjustment phi_h * gap_h is added to today's cross-section at a + h: in the first years
everyone moves roughly as people moving up today's age brackets, except by gaps that have
lasted. Beyond the horizon the history can test, the last estimated phi and the longest
measurable gap are held.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# how each statistic is compared across ages and years
TRANSFORM = {"earn": "log", "earn_ftyr": "log", "hh_income": "log", "unemp": "logit",
             "ftyr_share": "logit", "own": "logit", "scf_own": "logit", "networth": "slog",
             "fin_assets": "slog", "earn_ses": "log"}
FIT_AGES = (20, 64)          # cohorts used to estimate phi
GAP_OBS = 3                  # surveys averaged into a cohort's gap
PHI_BOUNDS = (0.0, 1.0)
SLOG_FLOOR = 0.02            # wealth below 2% of the reference counts as 2%
# education keys of the simulator -> groups in the cohort tables (most specific first)
EDU_CHAIN = {"bachelor": ("bachelor", "degree", "tertiary", "all"),
             "master_plus": ("master_plus", "degree", "tertiary", "all"),
             "short_tertiary": ("short_tertiary", "no_degree", "tertiary", "all"),
             "upper_secondary": ("upper_secondary", "no_degree", "all"),
             "below_upper_secondary": ("below_upper_secondary", "no_degree", "all"),
             None: ("all",), "all": ("all",)}


def to_rel(v, ref, kind: str):
    v, ref = np.asarray(v, float), np.asarray(ref, float)
    if kind == "log":
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.log(v / ref)
    if kind == "logit":
        v, ref = np.clip(v, 1e-3, 1 - 1e-3), np.clip(ref, 1e-3, 1 - 1e-3)
        return np.log(v / (1 - v)) - np.log(ref / (1 - ref))
    if kind == "slog":
        return np.log(np.maximum(v, SLOG_FLOOR * ref) / ref)
    return v / ref


def from_rel(r, ref, kind: str):
    r, ref = np.asarray(r, float), np.asarray(ref, float)
    if kind == "log":
        return ref * np.exp(r)
    if kind == "logit":
        ref = np.clip(ref, 1e-3, 1 - 1e-3)
        z = r + np.log(ref / (1 - ref))
        return 1 / (1 + np.exp(-z))
    if kind == "slog":
        return ref * np.exp(r)
    return r * ref


def _shift(R: np.ndarray, di: int, dj: int) -> np.ndarray:
    """out[i, j] = R[i - di, j - dj] (NaN outside)."""
    out = np.full_like(R, np.nan)
    n, m = R.shape
    i0, i1 = max(di, 0), min(n + di, n)
    j0, j1 = max(dj, 0), min(m + dj, m)
    if i0 < i1 and j0 < j1:
        out[i0:i1, j0:j1] = R[i0 - di:i1 - di, j0 - dj:j1 - dj]
    return out


@dataclass
class Series:
    """One statistic for one group: r(a, y) on a regular grid of single years of age x
    survey years (one survey every `step` years; missing surveys are NaN rows)."""
    measure: str
    edu: str
    kind: str
    years: np.ndarray
    ages: np.ndarray
    R: np.ndarray                # (len(years), len(ages)) relative level
    level: np.ndarray            # the statistic itself
    ref: dict                    # year -> all-ages reference
    step: int = 1
    span: int = 1                # ages pooled into one value (window / band width):
    #                              at horizons below it, gap and outcome share people
    _gaps: dict = field(default_factory=dict, repr=False)

    def gaps(self, k: int) -> np.ndarray:
        """gap matrix for a horizon of k surveys (h = k x step years)."""
        if k not in self._gaps:
            parts = [_shift(self.R, m, m * self.step) - _shift(self.R, m + k, m * self.step)
                     for m in range(GAP_OBS)]
            with warnings.catch_warnings():        # all-NaN cells stay NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                self._gaps[k] = np.nanmean(np.stack(parts), axis=0)
        return self._gaps[k]

    def targets(self, k: int) -> np.ndarray:
        h = k * self.step
        return _shift(self.R, -k, -h) - _shift(self.R, 0, -h)

    def idx(self, a: int, y: int):
        if (y - self.years[0]) % self.step:
            return None
        i, j = (y - int(self.years[0])) // self.step, a - int(self.ages[0])
        if 0 <= i < len(self.years) and 0 <= j < len(self.ages):
            return i, j
        return None

    def r(self, a: int, y: int) -> float:
        ij = self.idx(a, y)
        return float(self.R[ij]) if ij else float("nan")

    def gap(self, a: int, y: int, h: int) -> float:
        """The cohort aged a in y vs the cohort h years older, at the same ages."""
        if h % self.step:
            return float("nan")
        ij = self.idx(a, y)
        return float(self.gaps(h // self.step)[ij]) if ij else float("nan")

    def lags(self) -> list[int]:
        span = int(self.years[-1] - self.years[0]) if len(self.years) else 0
        return [h for h in range(self.step, span + 1, self.step)]


@dataclass
class Fit:
    phi: float
    se: float
    n: int
    rmse_cohort: float      # in-sample check of the forecast...
    rmse_cross: float       # ...vs simply today's older bracket (phi = 0)
    rmse_full: float        # ...vs gaps lasting in full (phi = 1)


def fit_phi(series: list[Series], h: int) -> Optional[Fit]:
    """phi_h pooled over groups (education x countries) and ages 20-64."""
    xs, ts = [], []
    for s in series:
        if h % s.step or h < s.span:
            continue
        k = h // s.step
        G, T = s.gaps(k), s.targets(k)
        jm = (s.ages >= FIT_AGES[0]) & (s.ages <= FIT_AGES[1] - h)
        x, t = G[:, jm].ravel(), T[:, jm].ravel()
        ok = ~np.isnan(x) & ~np.isnan(t)
        xs.append(x[ok])
        ts.append(t[ok])
    if not xs:
        return None
    x, t = np.concatenate(xs), np.concatenate(ts)
    if len(x) < 30 or (x * x).sum() <= 0:
        return None
    phi = float((x * t).sum() / (x * x).sum())
    res = t - phi * x
    se = float(np.sqrt((res ** 2).sum() / max(len(x) - 1, 1) / (x * x).sum()))
    phi_c = float(np.clip(phi, *PHI_BOUNDS))
    return Fit(phi_c, se, len(x), float(np.sqrt(np.mean((t - phi_c * x) ** 2))),
               float(np.sqrt(np.mean(t ** 2))), float(np.sqrt(np.mean((t - x) ** 2))))


@dataclass
class CohortData:
    """All cohort series for one country, with phi pooled as the data allow."""
    series: dict = field(default_factory=dict)     # (measure, edu) -> Series
    fits: dict = field(default_factory=dict)       # measure -> {h: Fit}
    source: dict = field(default_factory=dict)     # measure -> text
    borrowed: dict = field(default_factory=dict)   # measure -> where its phi comes from

    @classmethod
    def from_table(cls, t: pd.DataFrame, fits: Optional[dict] = None,
                   max_year: Optional[int] = None,
                   borrow: Optional[dict] = None) -> Optional["CohortData"]:
        """t: rows measure, education, age, year, value, ref, span, source for one
        country. fits: {measure: {h: Fit}} estimated elsewhere (pooled), else on t.
        borrow: measure -> (label, {h: Fit}) for statistics whose surveys cannot measure
        persistence themselves (too few or too coarse)."""
        if t is None or t.empty:
            return None
        c = cls()
        c.series = build_series(t, max_year)
        fits = fit_all(t, max_year) if fits is None else fits
        for m in {k[0] for k in c.series}:
            f = fits.get(m) or {}
            if not f and borrow and m in borrow:
                c.borrowed[m], f = borrow[m]
            c.fits[m] = f
            src = t.loc[t["measure"] == m, "source"]
            c.source[m] = str(src.iloc[0]) if len(src) else ""
        return c

    def get(self, measure: str, edu: Optional[str]) -> Optional[Series]:
        for e in EDU_CHAIN.get(edu, (edu, "all")):
            s = self.series.get((measure, e))
            if s is not None:
                return s
        return None

    def latest_year(self, measure: str) -> Optional[int]:
        s = self.get(measure, "all")
        return int(s.years[-1]) if s is not None else None

    def phi(self, measure: str, h: int) -> float:
        """Share of a gap that lasts h years: estimated where the history measures it;
        below the first measured horizon it builds up linearly from 0, beyond the last
        one it is held."""
        fits = self.fits.get(measure, {})
        if not fits or h <= 0:
            return 0.0
        hs = sorted(fits)
        if h in fits:
            return fits[h].phi
        if h > hs[-1]:
            return fits[hs[-1]].phi
        lo = max([0] + [x for x in hs if x < h])
        hi = min(x for x in hs if x > h)
        p_lo = 0.0 if lo == 0 else fits[lo].phi
        return p_lo + (fits[hi].phi - p_lo) * (h - lo) / (hi - lo)

    def gap_now(self, measure: str, edu: Optional[str], a0: int, h: int) -> float:
        """The cohort now aged a0 vs the cohort h years older at the same ages (the
        longest measurable horizon up to h, between surveys interpolated)."""
        s = self.get(measure, edu)
        if s is None or h <= 0:
            return 0.0
        y = int(s.years[-1])
        lags = [x for x in s.lags() if s.gap(a0, y, x) == s.gap(a0, y, x)]
        if not lags:
            return 0.0
        if h >= lags[-1]:
            return s.gap(a0, y, lags[-1])
        if h in lags:
            return s.gap(a0, y, h)
        lo = max([0] + [x for x in lags if x < h])
        hi = min(x for x in lags if x > h)
        g_lo = 0.0 if lo == 0 else s.gap(a0, y, lo)
        return g_lo + (s.gap(a0, y, hi) - g_lo) * (h - lo) / (hi - lo)

    def adjustment(self, measure: str, edu: Optional[str], a0: int, h: int,
                   mode: str = "history") -> float:
        """phi_h x gap_h: how far the cohort now aged a0 will be from today's people
        aged a0 + h, when it gets there (relative units of the statistic).
        mode "full": the whole gap lasts (phi = 1); "off": none of it."""
        if mode == "off" or h <= 0:
            return 0.0
        g = self.gap_now(measure, edu, a0, h)
        if g != g:
            return 0.0
        return g if mode == "full" else self.phi(measure, h) * g

    def path(self, measure: str, edu: Optional[str], a0: int, years: int,
             mode: str = "history") -> Optional[np.ndarray]:
        """The statistic relative to the all-ages reference for h = 0..years-1: today's
        level at a0 + h plus the lasting gap. NaN beyond the surveyed ages."""
        s = self.get(measure, edu)
        if s is None:
            return None
        y = int(s.years[-1])
        return np.array([_interp_age(s, a0 + h, y) +
                         self.adjustment(measure, edu, a0, h, mode)
                         for h in range(years)])

    def level(self, measure: str, edu: Optional[str], a: float) -> float:
        """The statistic itself at age a in the latest survey."""
        s = self.get(measure, edu)
        if s is None:
            return float("nan")
        i = len(s.years) - 1
        row = s.level[i]
        ok = ~np.isnan(row)
        return float(np.interp(a, s.ages[ok], row[ok])) if ok.any() else float("nan")


def fit_all(t: pd.DataFrame, max_year: Optional[int] = None) -> dict:
    """{measure: {h: Fit}} pooled over every group (education, countries) in t."""
    ser = build_series(t, max_year)
    out = {}
    for m in {k[0] for k in ser}:
        ss = [s for k, s in ser.items() if k[0] == m]
        fits = {}
        for h in sorted({h for s in ss for h in s.lags()}):
            f = fit_phi(ss, h)
            if f is not None:
                fits[h] = f
        out[m] = fits
    return out


def _interp_age(s: Series, a: float, y: int) -> float:
    i = (y - int(s.years[0])) // s.step
    row = s.R[i]
    ok = ~np.isnan(row)
    if not ok.any() or a > s.ages[ok][-1] or a < s.ages[ok][0]:
        return float("nan")
    return float(np.interp(a, s.ages[ok], row[ok]))


def build_series(t: pd.DataFrame, max_year: Optional[int] = None) -> dict:
    """(measure, edu[, iso3]) -> Series on a regular grid. Age bands become single years
    by interpolating between band midpoints."""
    out = {}
    if max_year is not None:
        t = t[t["year"] <= max_year]
    multi = "iso3" in t and t["iso3"].nunique() > 1
    keys = ["measure", "education"] + (["iso3"] if multi else [])
    for k, g in t.groupby(keys):
        m, e = k[0], k[1]
        kind = TRANSFORM.get(m, "log")
        ys = np.array(sorted(g["year"].unique()), int)
        if len(ys) < 2:
            continue
        step = int(np.min(np.diff(ys)))
        years = np.arange(ys[0], ys[-1] + 1, step)
        a_all = g["age"].to_numpy(float)
        ages = np.arange(int(np.ceil(a_all.min())), int(np.floor(a_all.max())) + 1)
        R = np.full((len(years), len(ages)), np.nan)
        L = np.full_like(R, np.nan)
        ref = {}
        for y, gy in g.groupby("year"):
            if (y - years[0]) % step:
                continue
            i = (int(y) - int(years[0])) // step
            gy = gy.sort_values("age")
            rr = to_rel(gy["value"].to_numpy(float), gy["ref"].to_numpy(float), kind)
            ok = ~np.isnan(rr) & np.isfinite(rr)
            if ok.sum() < 2:
                continue
            a_ = gy["age"].to_numpy(float)[ok]
            inside = (ages >= a_.min()) & (ages <= a_.max())
            R[i, inside] = np.interp(ages[inside], a_, rr[ok])
            L[i, inside] = np.interp(ages[inside], a_, gy["value"].to_numpy(float)[ok])
            ref[int(y)] = float(gy["ref"].iloc[0])
        span = int(g["span"].iloc[0]) if "span" in g and g["span"].notna().any() else 1
        out[tuple(k) if multi else (m, e)] = Series(m, e, kind, years, ages, R, L, ref,
                                                   step, span)
    return out

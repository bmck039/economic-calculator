"""Careers from the current US job market (CPS microdata, last 12 months).

Each partner is, in any year, in one of four situations: full time in a graduate-level
job, full time in another job, part time, or unemployed (incl. discouraged). The model
starts from today's mix for the partner's age and degree (or the situation they enter),
moves it forward with the year-on-year moves people of that age and degree make NOW
(the same CPS households matched a year apart), and pays each situation what it pays now
at that age, at the partner's place in the pay distribution. Pay then grows with the
raises workers of each age get now (the same workers matched a year apart) beyond
economy-wide wage growth, plus the chosen economy-wide outlook.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

STATES = ("FT_grad", "FT_nongrad", "PT", "U")
WORK = STATES[:3]
STATE_LABEL = {"FT_grad": "full time, job needs a degree",
               "FT_nongrad": "full time, job doesn't need a degree",
               "PT": "part time", "U": "looking for work"}
BAND_MID = {"20-24": 22, "25-29": 27, "30-34": 32, "35-39": 37, "40-44": 42, "45-49": 47,
            "50-54": 52, "55-59": 57, "60-64": 62}
GROUPS = (("20-24", 20, 24), ("25-29", 25, 29), ("30-34", 30, 34), ("35-44", 35, 44),
          ("45-54", 45, 54), ("55-64", 55, 64))
EDU_CHAIN = {"bachelor": ("bachelor", "degree", "all"),
             "master_plus": ("master_plus", "degree", "all"),
             "short_tertiary": ("short_tertiary", "all"),
             "upper_secondary": ("upper_secondary", "no_degree", "all"),
             "below_upper_secondary": ("below_upper_secondary", "no_degree", "all"),
             None: ("all",), "all": ("all",)}
QS = (10, 25, 50, 75, 90)
PCT = {"p10": 10, "p25": 25, "median": 50, "p75": 75, "p90": 90}
# A pay level stated today says less about pay in 10 years: earnings ranks correlate ~0.6
# over a decade in US Social Security data (Kopczuk, Saez & Song 2010) => the distance
# from the median shrinks by 0.6 ** 0.1 per year
RANK_PERSISTENCE = 0.6 ** 0.1
TREND_YEARS = 5            # "keeps changing": how long the last year's change continues
MIN_N_TRANS = 100          # matched people needed in a row of moves (else a broader group)
# The CPS codes occupation afresh each year, so matched records show far more switching
# between graduate-level and other jobs than really happens. Persistence is calibrated to
# Strada & Burning Glass Institute, Talent Disrupted (2024): 73% of graduates who start
# underemployed still are 10 years later, vs ~21% (73% / 3.5) of those who start in
# college-level jobs => the two-state chain's 10-year memory 0.73 - 0.21 = 0.52, i.e.
# only ~6.3% of full-time workers switch between the two kinds of job each year.
JOB_TYPE_SWITCH = 1 - 0.52 ** 0.1


def _group(age: float) -> str:
    for g, lo, hi in GROUPS:
        if age <= hi:
            return g
    return GROUPS[-1][0]


def _sticky(T: np.ndarray) -> np.ndarray:
    """Shrink graduate <-> non-graduate switches to the measured persistence, keeping their
    ratio (so today's mix of the two kinds of job is what the chain tends to)."""
    T = T.copy()
    a_, b_ = T[1, 0], T[0, 1]
    if a_ + b_ > JOB_TYPE_SWITCH:
        k = JOB_TYPE_SWITCH / (a_ + b_)
        T[1, 1] += T[1, 0] * (1 - k)
        T[1, 0] *= k
        T[0, 0] += T[0, 1] * (1 - k)
        T[0, 1] *= k
    return T


@dataclass
class CareerData:
    """us_career.csv, indexed for lookups."""
    shares: dict = field(default_factory=dict)    # (window, band, edu) -> {state: p}
    earn: dict = field(default_factory=dict)      # (edu, state) -> {band: {q: $/month}}
    trans: dict = field(default_factory=dict)     # (window, group, edu, s0) -> ({s1: p}, n)
    growth: dict = field(default_factory=dict)    # (group, edu) -> % nominal, (value, n)
    agg_growth: Optional[float] = None            # economy-wide nominal wage growth, %
    cpi: Optional[float] = None
    window: str = ""
    year: Optional[int] = None
    source: str = "CPS microdata"
    measured_moves: bool = True     # moves from this country's own panel data

    @classmethod
    def from_table(cls, t: pd.DataFrame) -> Optional["CareerData"]:
        if t is None or t.empty:
            return None
        c = cls()
        cur = t[t["window"] == "current"]
        if not cur.empty:
            c.window = f"{cur['from'].dropna().iloc[0]} to {cur['to'].dropna().iloc[0]}" \
                if cur["from"].notna().any() else ""
            to = str(cur["to"].dropna().iloc[0]) if cur["to"].notna().any() else ""
            c.year = int(to[:4]) if to[:4].isdigit() else None
        for r in t.itertuples(index=False):
            k = r.kind
            if k == "share":
                c.shares.setdefault((r.window, r.age_band, r.education), {})[r.state] = r.value
            elif k == "earn" and r.window == "current":
                c.earn.setdefault((r.education, r.state), {}).setdefault(
                    r.age_band, {})[int(r.q)] = r.value
            elif k == "trans":
                d, n = c.trans.get((r.window, r.age_band, r.education, r.state), ({}, 0))
                d[r.to_state] = r.value
                c.trans[(r.window, r.age_band, r.education, r.state)] = (d, int(r.n or 0))
            elif k == "growth":
                c.growth[(r.age_band, r.education)] = (r.value, int(r.n or 0))
            elif k == "agg_growth":
                c.agg_growth = r.value
            elif k == "cpi":
                c.cpi = r.value
        return c

    # ------------------------------------------------------------ lookups
    def mix(self, age: float, edu: Optional[str], window: str = "current") -> dict:
        band = next((b for b, m in BAND_MID.items() if age <= m + 2), "60-64")
        for e in EDU_CHAIN.get(edu, ("all",)):
            s = self.shares.get((window, band, e))
            if s and sum(s.values()) > 0:
                tot = sum(s.get(x, 0.0) for x in STATES)
                return {x: s.get(x, 0.0) / tot for x in STATES}
        return {"FT_grad": 0.0, "FT_nongrad": 1.0, "PT": 0.0, "U": 0.0}

    def mix_at(self, age: float, edu: Optional[str], window: str = "current"):
        """Today's mix at an exact age: interpolated between age-band midpoints."""
        pts = []
        for b, mid in BAND_MID.items():
            for e in EDU_CHAIN.get(edu, ("all",)):
                sh = self.shares.get((window, b, e))
                if sh and sum(sh.values()) > 0:
                    v = np.array([sh.get(x, 0.0) for x in STATES], float)
                    pts.append((mid, v / v.sum()))
                    break
        if not pts:
            return None if window != "current" else \
                np.array([self.mix(age, edu)[s] for s in STATES])
        ages = [a for a, _ in pts]
        out = np.array([np.interp(age, ages, [v[i] for _, v in pts]) for i in range(4)])
        return out / out.sum()

    def pay(self, age: float, edu: Optional[str], state: str, q: float) -> Optional[float]:
        """Monthly pay now (survey-period dollars) in `state` at `age`, quantile q."""
        for e in EDU_CHAIN.get(edu, ("all",)):
            tab = self.earn.get((e, state))
            if not tab:
                continue
            pts = sorted((BAND_MID[b], v) for b, v in tab.items() if b in BAND_MID)
            if len(pts) < 2:
                continue
            ages = [a for a, _ in pts]
            by_q = [float(np.interp(q, QS, [v[x] for x in QS])) for _, v in pts]
            return float(np.interp(age, ages, by_q))
        return None

    def quantile_of(self, pay_month: float, age: float, edu: Optional[str],
                    state: str) -> float:
        """Where a monthly pay sits in today's distribution for that situation."""
        vals = [self.pay(age, edu, state, q) for q in QS]
        if any(v is None for v in vals):
            return 50.0
        return float(np.clip(np.interp(pay_month, vals, QS), 5, 95))

    def moves(self, age: float, edu: Optional[str], window: str = "current",
              trend_years: int = 0) -> np.ndarray:
        """Year-on-year moves between situations (rows sum to 1). trend_years > 0: the
        change from the prior 12 months to the last 12 continues for that many years."""
        T = self._moves_raw(age, edu, "current")
        if trend_years > 0:
            P = self._moves_raw(age, edu, "prior")
            T = np.clip(T + trend_years * (T - P), 0.0, None)
            T = T / T.sum(axis=1, keepdims=True)
        return _sticky(T)

    def _moves_raw(self, age: float, edu: Optional[str], window: str) -> np.ndarray:
        g = _group(age)
        T = np.zeros((4, 4))
        for i, s0 in enumerate(STATES):
            row = None
            for e in EDU_CHAIN.get(edu, ("all",)):
                d, n = self.trans.get((window, g, e, s0), ({}, 0))
                if n >= MIN_N_TRANS:
                    row = d
                    break
            if row is None:
                row = {s0: 1.0}
            v = np.array([row.get(s1, 0.0) for s1 in STATES])
            T[i] = v / v.sum() if v.sum() > 0 else np.eye(4)[i]
        return T

    def raise_beyond_economy(self, age: float, edu: Optional[str]) -> float:
        """Yearly pay growth of a continuing worker of this age, beyond economy-wide wage
        growth (%): the same people's median raise now / average wage growth now."""
        if self.agg_growth is None:
            return 0.0
        g = _group(age)
        chain = ("degree" if edu in ("bachelor", "master_plus") else
                 "no_degree" if edu in ("upper_secondary", "below_upper_secondary") else
                 edu or "all", "all")
        for e in chain:
            v = self.growth.get((g, e))
            if v and v[1] >= 150:
                return ((1 + v[0] / 100) / (1 + self.agg_growth / 100) - 1) * 100
        return 0.0


@dataclass
class CareerPath:
    """Expected situation mix, pay when working and unemployment rate, per year."""
    pay: list            # yearly gross when working (survey-period money, real terms)
    unemployment: list   # share of the year unemployed
    mix: list            # [{state: p}]
    quantile: float
    start_state: Optional[str]
    note: str = ""


def career_path(c: CareerData, age0: int, edu: Optional[str], years: int, *,
                percentile: str = "median", job: Optional[str] = None,
                pay_now: Optional[float] = None, window: str = "current",
                growth: Optional[np.ndarray] = None) -> CareerPath:
    """Expected career for someone aged age0 (pay per YEAR in the data's money, real terms;
    economy-wide wage growth and inflation are added by the engine).

    Situations: someone starting from the typical mix has, at each age, the mix of the
    people at that age today (the surveys show no lasting generation gaps in unemployment
    or full-time work: econsim.cohort). Someone who states their situation starts there,
    and the difference to the typical mix closes at the pace people really move
    (the same CPS households a year apart, underemployment made as sticky as measured).
    "Keeps changing": the last year's change in the mix continues for 5 years.

    growth: the typical full-time year-round pay of this person's cohort, relative to
    economy-wide wages (econsim.cohort: today's age brackets plus the gap that lasts).
    Pay within each situation grows so that full-time pay of someone with the typical mix
    follows it (moves between graduate and other full-time jobs are part of that path, so
    they are taken out). Without it: the raises workers of each age get now."""
    trend = window == "trend"

    def M(t: int) -> np.ndarray:
        m = c.mix_at(age0 + t, edu, "current")
        if trend:
            p = c.mix_at(age0 + t, edu, "prior")
            if p is not None:
                m = np.clip(m + min(t + 1, TREND_YEARS) * (m - p), 0.0, None)
                m = m / m.sum()
        return m

    mix = [M(t) for t in range(years)]
    pi0 = np.array([1.0 if s == job else 0.0 for s in STATES]) if job in STATES else mix[0]
    dev = pi0 - mix[0]
    q = q0 = float(PCT.get(percentile, 50))
    stated = bool(pay_now and job in WORK)
    if stated:
        q = q0 = c.quantile_of(pay_now, age0, edu, job)
    qd = float(PCT.get(percentile, 50))
    base_d = [c.pay(age0, edu, s, qd) or 0.0 for s in WORK]
    lifts = [1.0] * years
    if growth is not None and len(growth) and growth[0] == growth[0]:
        m0 = None
        for t in range(years):     # full-time pay (the growth is full-time year-round)
            p_d = mix[t]
            w_ = p_d[:2].sum()
            m = sum(p * b for p, b in zip(p_d[:2], base_d[:2])) / w_ if w_ > 0 else 0.0
            m0 = m if m0 is None else m0
            g = growth[min(t, len(growth) - 1)]
            g = g if g == g else growth[~np.isnan(growth)][-1]
            lifts[t] = (g / growth[0]) / (m / m0) if m > 0 and m0 else 1.0
    else:
        lift = 1.0
        for t in range(years):
            lifts[t] = lift
            lift *= 1 + c.raise_beyond_economy(age0 + t, edu) / 100
    pays, unemp, mixes = [], [], []
    for t in range(years):
        pi = np.clip(mix[t] + dev, 0.0, None)
        pi = pi / pi.sum() if pi.sum() > 0 else mix[t]
        work = pi[:3].sum()
        # levels: today's pay for that situation at the START age, then raised as the
        # cohort's pay grows (not to what older cohorts earn today)
        if stated:       # today's stated pay fades toward the chosen percentile
            q = qd + (q0 - qd) * RANK_PERSISTENCE ** t
        base = [c.pay(age0, edu, s, q) or 0.0 for s in WORK]
        pm = sum(p * b for p, b in zip(pi[:3], base)) / work if work > 0 else 0.0
        pays.append(pm * 12 * lifts[t])
        unemp.append(float(pi[3]))
        mixes.append(dict(zip(STATES, map(float, pi))))
        dev = dev @ c.moves(age0 + t, edu)       # the difference closes as people move
    return CareerPath(pays, unemp, mixes, q0, job if job in STATES else None)


# ---------------------------------------------------------------- other countries
PT_EDU = {"bachelor": "tertiary", "master_plus": "tertiary", "short_tertiary": "tertiary",
          "upper_secondary": "upper_secondary",
          "below_upper_secondary": "below_upper_secondary", "all": "all"}
EDU_KEYS = ("bachelor", "master_plus", "short_tertiary", "upper_secondary",
            "below_upper_secondary", "all")
PT_BAND = {"20-24": "25-49", "25-29": "25-49", "30-34": "25-49", "35-39": "25-49",
           "40-44": "25-49", "45-49": "25-49", "50-54": "50-64", "55-59": "50-64",
           "60-64": "50-64"}


def from_country(cd, us: CareerData, pt: pd.DataFrame, pt_all: pd.DataFrame) -> CareerData:
    """The same four situations for a country without matched panel data, from its own
    statistics: unemployment by age and degree, the share working part time (Eurostat
    LFS; under 25 the 25-49 share raised as in the US, where students are left out),
    graduates in jobs below their level (LFS over-qualification), and pay from the
    country's own earnings by age, percentile and degree. Borrowed from the US CPS, where
    no country measures them: pay in non-graduate and part-time jobs relative to
    full-time graduate jobs, and how fast people move between situations."""
    from .engine import edu_pay_factor, interp_profile, unemployment_rate_for
    c = CareerData()
    c.trans, c.growth, c.agg_growth = us.trans, {}, None
    c.year = int(str(cd.earnings_src.year)[:4]) if str(cd.earnings_src.year)[:4].isdigit() \
        else us.year
    c.source = "national statistics (unemployment, part time, over-qualification: " \
        "Eurostat LFS / national LFS); pay ratios and moves from the US CPS"
    c.window = f"{c.year}"
    c.measured_moves = False

    def pt_share(band: str, edu: str) -> float:
        e, b = PT_EDU.get(edu, "all"), PT_BAND[band]
        for tab in (pt, pt_all):
            if tab is None or tab.empty:
                continue
            r = tab[(tab["age_band"] == b) & (tab["education"] == e)]
            if not r.empty:
                v = float(r["value"].median()) / 100
                if band == "20-24":           # young: as much more as in the US
                    yu, pr = us.mix_at(22, edu), us.mix_at(37, edu)
                    if yu is not None and pr is not None and pr[:3].sum() > 0:
                        k = (yu[2] / yu[:3].sum()) / max(pr[2] / pr[:3].sum(), 1e-3)
                        v = min(v * k, 0.9)
                return v
        return 0.2

    for band, mid in BAND_MID.items():
        for edu in EDU_KEYS:
            e_ = None if edu == "all" else edu
            u, _ = unemployment_rate_for(cd, mid, e_)
            u = (u if u is not None else cd.unemployment_rate.value) / 100
            p = pt_share(band, edu)
            us_mix = us.mix_at(mid, edu)
            if edu in ("bachelor", "master_plus", "short_tertiary"):
                oq = cd.labour.get(("overqualification", age_band_lm(mid), edu)) \
                    if cd.labour else None
                oq = oq.value / 100 if oq is not None else \
                    (us_mix[1] / us_mix[:2].sum() if us_mix is not None else 0.3)
            else:      # share of full-time jobs below graduate level: as in the US
                oq = us_mix[1] / us_mix[:2].sum() if us_mix is not None else 0.8
            ft = (1 - u) * (1 - p)
            c.shares[("current", band, edu)] = {"FT_grad": ft * (1 - oq),
                                                "FT_nongrad": ft * oq,
                                                "PT": (1 - u) * p, "U": u}
            # pay: the country's full-time pay at that age, split by situation with the
            # US ratios (so full-time pay on average is the country's)
            rho = _ratio(us, mid, edu, "FT_nongrad", "FT_grad", 0.7)
            kap = _ratio(us, mid, edu, "PT", "FT_nongrad", 0.5)
            for q, pk in zip(QS, ("p10", "p25", "median", "p75", "p90")):
                prof = cd.earnings.get(pk) or cd.earnings.get("median")
                base = interp_profile(prof, mid) / 12 * edu_pay_factor(cd, e_, mid)
                if pk not in cd.earnings:        # log-normal from the median
                    z = {"p10": -1.2816, "p25": -0.6745, "p75": 0.6745, "p90": 1.2816}[pk]
                    base *= float(np.exp(0.45 * z))
                grad = base / ((1 - oq) + oq * rho)
                for st, v in (("FT_grad", grad), ("FT_nongrad", grad * rho),
                              ("PT", grad * rho * kap)):
                    c.earn.setdefault((edu, st), {}).setdefault(band, {})[q] = v
    return c


def age_band_lm(age: float) -> str:
    return "20-24" if age < 25 else "25-29" if age < 30 else "30-34" if age < 35 \
        else "25-64"


def _ratio(us: CareerData, age: float, edu: str, a: str, b: str, default: float) -> float:
    x, y = us.pay(age, edu, a, 50), us.pay(age, edu, b, 50)
    return float(x / y) if x and y else default

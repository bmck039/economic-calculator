"""What actually happened, year by year: the environment for backtesting the simulation.

A History replaces the simulation's forecasts with realised values from a start year:
consumer prices (and each cost category's own price), economy-wide real wages, home prices,
rents, the mortgage rate for new loans, market returns, deposit rates and unemployment.
Backtests (econsim.backtest) start households in a past survey year and compare what the
model then predicts with what the same birth years held in later surveys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# US budget lines (simulation keys) with a CPI series in us_history.csv ("cpi:<key>"; any
# such series is used, in Europe the HICP index of each budget line)
CPI_KEYS = ("groceries", "restaurants_hotels", "alcohol_tobacco", "clothing",
            "furnishings_household", "household_operations", "utilities_phone",
            "health_out_of_pocket", "insurance_health", "recreation", "education",
            "transport_services", "personal_misc", "vehicle_purchase", "vehicle_running",
            "insurance_vehicle", "childcare", "rent")
BOND_DURATION = 7.0        # a 10-year bond fund's return ~ yield + duration x fall in yield


@dataclass
class History:
    year0: int
    years: list                      # calendar years covered, from year0
    infl: list                       # CPI change year t -> t + 1
    wage_real: list                  # economy-wide real wage change t -> t + 1
    home_real: list                  # real home-price change t -> t + 1
    rent_real: list                  # real rent change t -> t + 1
    mortgage_rate: list              # % for new 30-year loans in year t
    equity: list                     # % nominal total return in year t
    bond: list                       # % nominal return of 10-year bonds in year t
    cash: list                       # % on money-market / bills in year t
    deposit: list                    # % banks paid on savings deposits in year t
    unemp_factor: list               # national unemployment in t vs the data year
    rel: dict = field(default_factory=dict)   # key -> price relative to the CPI, by t
    #                                           (1 at year0)
    rel_level: dict = field(default_factory=dict)  # key -> (price / CPI) in year0
    #                                                vs the data year (levels back then)
    wage_level: float = 1.0          # real wages in year0 vs the data year
    home_level: float = 1.0          # real home prices in year0 vs the data year
    rent_level: float = 1.0          # real rents in year0 vs the data year
    cpi_level: float = 1.0           # CPI in year0 vs the data year

    def n(self) -> int:
        return len(self.infl)

    def at(self, name: str, t: int, default=None):
        v = getattr(self, name)
        return v[t] if 0 <= t < len(v) else default


def _series(h: pd.DataFrame, name: str) -> pd.Series:
    s = h[h["series"] == name]
    return pd.Series(s["value"].to_numpy(float), index=s["year"].to_numpy(int))


def from_table(h: pd.DataFrame, year0: int, data_year: int,
               end: Optional[int] = None, iso3: Optional[str] = None) -> History:
    """us_history.csv / eu_history.csv -> History from year0 to `end` (default: the last
    complete year). data_year: the year the country's levels (budget, rents, prices) are
    from. iso3: the country, for tables with several (eu_history.csv)."""
    if iso3 is not None and "iso3" in h:
        h = h[h["iso3"] == iso3]
    cpi = _series(h, "cpi")
    end = int(cpi.index.max()) if end is None else min(end, int(cpi.index.max()))
    ys = list(range(year0, end + 1))
    wage = _series(h, "wage") / cpi
    hpi, rent = _series(h, "hpi") / cpi, _series(h, "rent") / cpi
    m30, y10 = _series(h, "mortgage30"), _series(h, "y10")
    eq, bill = _series(h, "equity_return"), _series(h, "tbill_return")
    tb3 = _series(h, "tbill3m")
    dep = _series(h, "deposit_savings")
    un = _series(h, "unemployment")
    # savings deposits before FDIC's series (2021-): the share of the bill rate they paid
    both = [y for y in dep.index if y in tb3.index and tb3[y] > 0.5]
    pass_through = float(np.median([dep[y] / tb3[y] for y in both])) if both else 0.3

    def ch(s: pd.Series, y: int) -> float:
        return float(s[y + 1] / s[y] - 1) if y in s.index and y + 1 in s.index else np.nan

    def lev(s: pd.Series) -> float:
        s = s.dropna()
        if s.empty or year0 not in s.index:
            return 1.0
        d = data_year if data_year in s.index else int(s.index.max())
        v = float(s[year0] / s[d])
        return v if v == v and v > 0 else 1.0

    pairs = ys[:-1]
    H = History(
        year0=year0, years=ys,
        infl=[ch(cpi, y) for y in pairs],
        wage_real=[ch(wage, y) for y in pairs],
        home_real=[ch(hpi, y) for y in pairs],
        rent_real=[ch(rent, y) for y in pairs],
        mortgage_rate=[float(m30.get(y, np.nan)) for y in ys],
        equity=[float(eq.get(y, np.nan)) for y in ys],
        bond=[float(y10[y - 1] + BOND_DURATION * (y10[y - 1] - y10[y]))
              if y in y10.index and y - 1 in y10.index else np.nan for y in ys],
        cash=[float(bill.get(y, np.nan)) for y in ys],
        deposit=[float(dep[y]) if y in dep.index else float(tb3.get(y, np.nan)) *
                 pass_through for y in ys],
        unemp_factor=[float(un[y] / un[data_year if data_year in un.index else
                                     int(un.index.max())]) if y in un.index else 1.0
                      for y in ys],
        wage_level=lev(wage), home_level=lev(hpi), rent_level=lev(rent),
        cpi_level=lev(cpi))
    keys = sorted({x[4:] for x in h["series"].unique() if str(x).startswith("cpi:")})
    for k in keys:
        s = _series(h, f"cpi:{k}")
        if s.empty or year0 not in s.index:
            continue
        r = (s / cpi).dropna()
        if year0 not in r.index:
            continue
        rel = [float(r[y] / r[year0]) if y in r.index else np.nan for y in ys]
        for i in range(1, len(rel)):             # a missing year: as the year before
            if rel[i] != rel[i]:
                rel[i] = rel[i - 1]
        H.rel[k] = rel
        H.rel_level[k] = lev(r)
    return H

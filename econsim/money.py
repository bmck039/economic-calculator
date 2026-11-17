"""Money arithmetic: rolling values between years, annuities, interpolation."""

from __future__ import annotations

import numpy as np


def interp_profile(points: list[tuple[float, float]], age: float) -> float:
    xs = [a for a, _ in points]
    ys = [v for _, v in points]
    return float(np.interp(age, xs, ys))


def nrr_for_month(nrr: dict[int, float], month: int) -> float:
    """Household net replacement rate (fraction) in the m-th month of unemployment,
    from OECD TaxBEN values at 2, 6, 12 and 24 months."""
    for m in sorted(nrr):
        if month <= m:
            return nrr[m] / 100
    return nrr[max(nrr)] / 100 if nrr else 0.0


def nan0(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x else x


def interp_nrr(tab: dict, rel: float) -> float:
    """Net replacement rate (%) at `rel` x the average wage: linear in log(earnings)
    between the OECD's 0.5 / 1 / 2 x AW points, flat outside them."""
    pts = sorted(tab.items())
    if rel <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if rel <= x1:
            w = (np.log(rel) - np.log(x0)) / (np.log(x1) - np.log(x0))
            return y0 + (y1 - y0) * w
    return pts[-1][1]


def roll(
    value: float, from_year, to_year: int, inflation: float, cpi: dict | None = None
) -> float:
    """Move a money value from its data year to the start year: actual CPI where it is
    known, `inflation` after that."""
    try:
        fy = int(str(from_year)[:4])
    except (TypeError, ValueError):
        return value
    f = 1.0
    if cpi and to_year < fy and to_year in cpi:  # back in time: actual CPI
        top = min(fy, max(cpi))
        return value * cpi[to_year] / cpi[top] / (1 + inflation / 100) ** (fy - top)
    if cpi and fy in cpi:
        last = min(max(cpi), to_year)
        if last > fy:
            f = cpi[last] / cpi[fy]
            fy = last
    return value * f * (1 + inflation / 100) ** max(to_year - fy, 0)


def annuity(principal: float, rate: float, years: float) -> float:
    """Yearly payment (rate as a fraction)."""
    if years <= 0:
        return principal * rate
    if rate <= 0:
        return principal / years
    r = rate / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n) * 12


def annuity_pv(payment_year: float, rate: float, years: float) -> float:
    """Loan supported by a yearly payment."""
    if payment_year <= 0:
        return 0.0
    if years <= 0:
        return payment_year / rate if rate > 0 else float("inf")
    if rate <= 0:
        return payment_year * years
    r = rate / 12
    n = years * 12
    return payment_year / 12 * (1 - (1 + r) ** -n) / r

"""Tax calculations.

Local tax: average income-tax and employee social-security rates for a single
person at several multiples of the national average wage (OECD / Eurostat tax-benefit
models), interpolated linearly in income and extrapolated above the top point with
the published marginal rate. Each partner is taxed individually (joint-filing /
income-splitting countries such as Germany or France will be slightly overstated for
unequal couples).

US citizens abroad: federal income tax still applies. We compute the two standard
routes and keep the cheaper one each year:
  A) Foreign Earned Income Exclusion (FEIE, per spouse) + foreign housing exclusion,
     with the "stacking" rule, plus a Foreign Tax Credit (FTC) for local income tax on
     the non-excluded part. The refundable child tax credit is not allowed with FEIE.
  B) FTC only: US tax on all income minus a credit for local income tax (excess
     credits are not carried forward here - conservative).
Local social security is not creditable; with a totalization agreement it replaces
US Social Security. A local employer does not withhold US FICA; a US employer in a
country without an agreement does (option `us_employer`).
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import TaxPoint


def local_rates(gross: float, aw: float, points: list[TaxPoint],
                marginal_top: float | None) -> tuple[float, float]:
    """(income tax, employee SSC) in currency for one earner's annual gross."""
    if gross <= 0 or not points:
        return 0.0, 0.0
    pts = sorted(points, key=lambda p: p.aw_multiple)
    x = gross / aw
    if x <= pts[0].aw_multiple:
        p = pts[0]
        # below the lowest modelled wage, scale the average rate down linearly to 0
        # at zero income (allowances make low incomes pay proportionally less)
        k = x / p.aw_multiple
        return gross * p.income_tax_rate * k, gross * p.ssc_rate * (0.5 + 0.5 * k)
    if x >= pts[-1].aw_multiple:
        p = pts[-1]
        g_top = p.aw_multiple * aw
        it, ssc = g_top * p.income_tax_rate, g_top * p.ssc_rate
        if marginal_top is not None:
            # NPMTR includes employee SSC; SSC is often capped, so put all of the
            # extra into income tax and keep SSC at its top-point level
            it += max(marginal_top - p.ssc_rate * 0.5, 0) * (gross - g_top)
            ssc += p.ssc_rate * 0.5 * (gross - g_top)
        else:
            it, ssc = gross * p.income_tax_rate, gross * p.ssc_rate
        return it, ssc
    for lo, hi in zip(pts, pts[1:]):
        if lo.aw_multiple <= x <= hi.aw_multiple:
            w = (x - lo.aw_multiple) / (hi.aw_multiple - lo.aw_multiple)
            itr = lo.income_tax_rate + w * (hi.income_tax_rate - lo.income_tax_rate)
            ssr = lo.ssc_rate + w * (hi.ssc_rate - lo.ssc_rate)
            return gross * itr, gross * ssr
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- US federal
@dataclass
class USParams:
    brackets_mfj: list[tuple[float, float]]     # (rate, lower threshold)
    brackets_single: list[tuple[float, float]]
    std_mfj: float
    std_single: float
    ctc: float
    ctc_refundable: float
    feie: float
    housing_base: float
    housing_limit: float
    fica_rate: float
    ss_wage_base: float

    def indexed(self, f: float) -> "USParams":
        """All dollar amounts scaled by an inflation factor f (IRS indexes yearly)."""
        return USParams([(r, t * f) for r, t in self.brackets_mfj],
                        [(r, t * f) for r, t in self.brackets_single],
                        self.std_mfj * f, self.std_single * f, self.ctc, self.ctc_refundable,
                        self.feie * f, self.housing_base * f, self.housing_limit * f,
                        self.fica_rate, self.ss_wage_base * f)


def parse_brackets(note: str) -> list[tuple[float, float]]:
    """'10:0;12:24800;...' -> [(0.10, 0), (0.12, 24800), ...]"""
    out = []
    for part in str(note).split(";"):
        r, t = part.split(":")
        out.append((float(r) / 100, float(t)))
    return sorted(out, key=lambda x: x[1])


def bracket_tax(taxable: float, brackets: list[tuple[float, float]]) -> float:
    if taxable <= 0:
        return 0.0
    tax = 0.0
    for i, (rate, lo) in enumerate(brackets):
        hi = brackets[i + 1][1] if i + 1 < len(brackets) else float("inf")
        if taxable > lo:
            tax += (min(taxable, hi) - lo) * rate
    return tax


@dataclass
class USResult:
    residual: float          # USD owed to the IRS after credits (negative = refund)
    method: str              # "FEIE+FTC" or "FTC"
    feie_residual: float
    ftc_residual: float
    us_tax_before_credits: float


def us_expat_tax(earned: list[float], foreign_income_tax: list[float], married: bool,
                 n_children: int, housing_cost: float, p: USParams) -> USResult:
    """All amounts in USD for one year. `earned`/`foreign_income_tax` per spouse."""
    earned = [max(e, 0.0) for e in earned]
    total = sum(earned)
    if total <= 0:
        return USResult(0.0, "none", 0.0, 0.0, 0.0)
    units = [(earned, foreign_income_tax)] if married else \
        [([e], [t]) for e, t in zip(earned, foreign_income_tax)]
    feie_tot = ftc_tot = before_tot = 0.0
    for k, (inc, ft) in enumerate(units):
        brackets = p.brackets_mfj if married else p.brackets_single
        std = p.std_mfj if married else p.std_single
        kids = n_children if (married or k == 0) else 0
        agi = sum(inc)
        if agi <= 0:
            continue
        ctc = p.ctc * kids
        # ---- B) FTC only. Order matters: the FTC is applied first (Schedule 8812's
        # credit-limit worksheet subtracts it), the child credit only uses what is left,
        # and the unused part is refundable (ACTC) up to the per-child cap. So in a
        # high-tax country the whole child credit becomes a refund.
        t_all = bracket_tax(agi - std, brackets)
        before_tot += t_all
        ftc_b = min(sum(ft), t_all)
        t_b = t_all - ftc_b
        credit_nonref = min(ctc, t_b)
        refundable = min(p.ctc_refundable * kids, ctc - credit_nonref,
                         max(0.15 * (agi - 2500), 0))
        res_b = t_b - credit_nonref - refundable
        # ---- A) FEIE (+ housing exclusion on the first earner) + FTC on the rest
        excl = [min(e, p.feie) for e in inc]
        housing = 0.0
        if housing_cost > p.housing_base and k == 0:
            housing = min(housing_cost - p.housing_base, p.housing_limit - p.housing_base)
            housing = min(housing, max(inc[0] - excl[0], 0))
        excluded = sum(excl) + housing
        taxable_rest = max(agi - excluded - std, 0)
        # stacking rule: rates as if the excluded income were still there
        t_a = bracket_tax(taxable_rest + excluded, brackets) - bracket_tax(excluded, brackets) \
            if taxable_rest > 0 else 0.0
        share_taxed = (agi - excluded) / agi if agi else 0
        ftc_a = min(sum(ft) * share_taxed, t_a)
        t_a -= ftc_a
        res_a = t_a - min(ctc, t_a)   # no refundable ACTC when claiming the FEIE
        feie_tot += res_a
        ftc_tot += res_b
    if feie_tot <= ftc_tot:
        return USResult(feie_tot, "FEIE+FTC", feie_tot, ftc_tot, before_tot)
    return USResult(ftc_tot, "FTC", feie_tot, ftc_tot, before_tot)


def us_fica(earned_usd: float, p: USParams) -> float:
    """Employee FICA (6.2% up to the wage base + 1.45% Medicare)."""
    return min(earned_usd, p.ss_wage_base) * 0.062 + earned_usd * 0.0145

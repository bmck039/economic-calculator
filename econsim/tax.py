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
    eitc_scale: float = 1.0          # EITC dollar amounts vs 2026 (indexed like the rest)

    def indexed(self, f: float) -> "USParams":
        """All dollar amounts scaled by an inflation factor f (IRS indexes yearly)."""
        return USParams([(r, t * f) for r, t in self.brackets_mfj],
                        [(r, t * f) for r, t in self.brackets_single],
                        self.std_mfj * f, self.std_single * f, self.ctc, self.ctc_refundable,
                        self.feie * f, self.housing_base * f, self.housing_limit * f,
                        self.fica_rate, self.ss_wage_base * f, self.eitc_scale * f)


# Earned income tax credit, tax year 2026 (IRS Rev. Proc. 2025-32 s.4.06; rates: IRC s.32):
# children 0 / 1 / 2 / 3+ -> phase-in rate, maximum credit, phase-out rate; the phase-out
# starts at 18,140 (no children) / 31,160 (children) for joint filers, 7,280 less for
# others (the completed phase-outs 26,820 / 58,863 / 65,899 / 70,244 follow)
EITC = ((0.0765, 664.0, 0.0765), (0.34, 4427.0, 0.1598), (0.40, 7316.0, 0.2106),
        (0.45, 8231.0, 0.2106))
EITC_START_MFJ = (18_140.0, 31_160.0)
EITC_MFJ_EXTRA = 7_280.0


def eitc(earned: float, kids: int, married: bool, scale: float = 1.0) -> float:
    rin, mx, rout = EITC[min(max(kids, 0), 3)]
    start = (EITC_START_MFJ[1 if kids else 0] - (0 if married else EITC_MFJ_EXTRA)) * scale
    c = min(rin * earned, mx * scale)
    return max(c - rout * max(earned - start, 0.0), 0.0)


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


# ---------------------------------------------------------------- local refinements
# Income-splitting countries: a married couple pays twice the tax on half the joint
# income (Germany Ehegattensplitting, Luxembourg class 2, Portugal/Poland joint
# filing). Applied to the income-tax part of the OECD single-person curve.
SPLITTING = {"DEU", "LUX", "PRT", "POL"}
FRA_HALF_PART_CAP = 1807.0   # quotient familial: max advantage per child half-part (2026)


def income_tax_only(gross: float, aw: float, points: list[TaxPoint],
                    marginal_top: float | None) -> float:
    return local_rates(gross, aw, points, marginal_top)[0]


def household_income_tax(iso3: str, gross: list[float], married: bool, n_children: int,
                         aw: float, points: list[TaxPoint],
                         marginal_top: float | None) -> list[float]:
    """Income tax per earner, after joint-taxation rules (split back pro rata)."""
    indiv = [income_tax_only(g, aw, points, marginal_top) for g in gross]
    tot = sum(gross)
    if not married or tot <= 0:
        return indiv
    if iso3 in SPLITTING:
        joint = 2 * income_tax_only(tot / 2, aw, points, marginal_top)
    elif iso3 == "FRA":
        parts = 2 + 0.5 * min(n_children, 2) + 1.0 * max(n_children - 2, 0)
        couple = 2 * income_tax_only(tot / 2, aw, points, marginal_top)
        joint = parts * income_tax_only(tot / parts, aw, points, marginal_top)
        joint = max(joint, couple - FRA_HALF_PART_CAP * (parts - 2) * 2)
    else:
        return indiv
    joint = min(joint, sum(indiv))
    return [joint * g / tot for g in gross]


def uk_sdlt_first_time(price: float) -> float:
    """England stamp duty land tax for a first-time buyer (from April 2025)."""
    if price <= 500_000:
        return 0.05 * max(price - 300_000, 0)
    bands = [(125_000, 0.0), (250_000, 0.02), (925_000, 0.05), (1_500_000, 0.10),
             (float("inf"), 0.12)]
    tax, lo = 0.0, 0.0
    for hi, r in bands:
        if price > lo:
            tax += (min(price, hi) - lo) * r
        lo = hi
    return tax


# ---------------------------------------------------------------- US residents
SALT_CAP = 40_000.0          # 2025-2029 (OBBBA); phased down above USD 500k MAGI


@dataclass
class USResidentTax:
    federal: float
    state: float
    local: float
    fica: float
    state_payroll: float

    @property
    def income_tax(self) -> float:
        return self.federal + self.state + self.local

    @property
    def ssc(self) -> float:
        return self.fica + self.state_payroll


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def state_income_tax(wages: float, married: bool, n_dep: int, st: dict | None) -> float:
    if not st or not str(st.get("brackets_single", "")).strip():
        return 0.0
    from .params import parse_rate_brackets
    br = parse_rate_brackets(st["brackets_mfj" if married else "brackets_single"])
    std = _num(st["std_mfj" if married else "std_single"])
    ex = _num(st["exempt_mfj" if married else "exempt_single"]) + n_dep * _num(st["exempt_dep"])
    tax = bracket_tax(max(wages - std - ex, 0), br)
    tax -= _num(st["credit_mfj" if married else "credit_single"]) + n_dep * _num(
        st["credit_dep"])
    return max(tax, 0.0)


def us_resident_tax(wages: list[float], married: bool, n_ctc: int, n_dep: int,
                    p: USParams, st: dict | None, mortgage_interest: float = 0.0,
                    property_tax: float = 0.0) -> USResidentTax:
    """Federal + state + local income tax and employee payroll taxes for a US-resident
    couple (USD/year). Itemises when mortgage interest + SALT beat the standard
    deduction; child tax credit with the refundable part; earned income tax credit."""
    wages = [max(w, 0.0) for w in wages]
    fica = sum(us_fica(w, p) for w in wages)
    payroll = sum(wages) * _num(st.get("payroll_pct")) / 100 if st else 0.0
    local_rate = _num(st.get("local_rate")) / 100 if st else 0.0
    units = [(wages, n_ctc, n_dep)] if married else \
        [([wages[0]], n_ctc, n_dep), ([wages[1]], 0, 0)]
    fed = s_tax = loc = 0.0
    for inc, kids, deps in units:
        agi = sum(inc)
        if agi <= 0:
            continue
        share = agi / max(sum(wages), 1e-9)
        st_t = state_income_tax(agi, married, deps, st)
        lo_t = agi * local_rate
        salt = min(st_t + lo_t + property_tax * share, SALT_CAP if married else SALT_CAP)
        itemized = salt + mortgage_interest * share
        std = p.std_mfj if married else p.std_single
        brackets = p.brackets_mfj if married else p.brackets_single
        t = bracket_tax(max(agi - max(std, itemized), 0), brackets)
        ctc = p.ctc * kids
        phase = 400_000 if married else 200_000
        ctc = max(ctc - 0.05 * max(agi - phase, 0), 0)
        nonref = min(ctc, t)
        refund = min(p.ctc_refundable * kids, ctc - nonref, max(0.15 * (agi - 2500), 0))
        fed += t - nonref - refund - eitc(agi, deps, married, p.eitc_scale)
        s_tax += st_t
        loc += lo_t
    return USResidentTax(fed, s_tax, loc, fica, payroll)


def youth_tax_factor(iso3: str, age: int, gross: float, aw: float, years_working: int,
                     mother_of_two: bool = False) -> float:
    """Share of the income tax still due after age-based exemptions (2026 rules):
    PL PIT-0 under 26 (to PLN 85,528); HU under 25 (to the average wage) and mothers of
    two under 40 (2026); HR under 25 exempt, 25-30 half; PT IRS Jovem (<=35, first 10
    working years: 100/75/50/25% of income up to ~EUR 28,700 exempt); GR 0% on the first
    EUR 20k up to 25 and 9% (vs 22%) on the 10-20k band at 26-30."""
    if gross <= 0:
        return 1.0
    if iso3 == "POL" and age < 26:
        return 1 - min(1.0, 85_528 / gross)
    if iso3 == "HUN":
        if mother_of_two and age < 40:
            return 0.0
        if age < 25:
            return 1 - min(1.0, aw / gross)
    if iso3 == "HRV":
        return 0.0 if age < 25 else 0.5 if age <= 30 else 1.0
    if iso3 == "PRT" and age <= 35 and years_working < 10:
        pct = 1.0 if years_working < 1 else 0.75 if years_working < 4 else \
            0.5 if years_working < 7 else 0.25
        return 1 - pct * min(1.0, 28_737 / gross)
    if iso3 == "GRC":
        if age <= 25:
            return 1 - min(1.0, 20_000 / gross)
        if age <= 30:
            return 0.8
    return 1.0

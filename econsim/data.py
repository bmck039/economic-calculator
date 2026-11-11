"""Build CountryData (with provenance) from the pipeline's derived CSVs.

Only the compact derived tables are read (never econ_long.csv), so the UI starts
in a second or two.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from . import params as P
from .model import CountryData, Sourced, TaxPoint
from .tax import USParams, parse_brackets

DATA_DIR = Path(__file__).resolve().parent.parent / "econ_data"

# budget categories that are tenure-specific (handled by the rent/own switch) or
# totals - excluded from the baseline couple budget
TENURE = {"rent_actual", "rent_imputed_owners", "owner_costs_mortgage_tax_maint",
          "other_items_incl_mortgage_interest", "housing_net_fuel_power", "TOTAL"}
UTILITY_CATS = {"energy_home", "water_refuse"}
COUNTRY_NAMES: dict[str, str] = {}


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


class DataStore:
    def __init__(self, data_dir: Path | str = DATA_DIR):
        self.dir = Path(data_dir)
        rd = lambda n: pd.read_csv(self.dir / f"{n}.csv") if (self.dir / f"{n}.csv").exists() \
            else pd.DataFrame()  # noqa: E731
        self.budget = rd("couple_budget")
        self.life = rd("life_events")
        self.earn = rd("earnings_profile")
        self.taxc = rd("tax_curve")
        self.fx = rd("fx_ppp")
        self.ctx = rd("context_series")
        self.refs = rd("reference_values")
        self.rwh = rd("real_wage_history")
        self.mob = rd("mobility_summary")
        self.expat = rd("expat_tax")
        self.snap = rd("country_snapshot")
        self.labour = rd("labour_market")
        self.edu_pay = rd("education_pay")
        if not self.snap.empty:
            COUNTRY_NAMES.update(dict(zip(self.snap["iso3"], self.snap["country"])))

    # ------------------------------------------------------------ helpers
    def countries(self) -> list[tuple[str, str]]:
        """Countries with at least earnings + tax data, sorted by name."""
        ok = set(self.earn["iso3"]) & set(self.taxc["iso3"]) if not self.earn.empty else set()
        return sorted(((c, COUNTRY_NAMES.get(c, c)) for c in ok), key=lambda x: x[1])

    def ref(self, iso3: str, item: str):
        r = self.refs[(self.refs["iso3"] == iso3) & (self.refs["item"] == item)]
        return None if r.empty else r.iloc[0]

    def life_item(self, iso3: str, item: str, contains: bool = False) -> Sourced | None:
        l = self.life[self.life["iso3"] == iso3]
        l = l[l["item"].astype(str).str.contains(item, regex=False)] if contains else \
            l[l["item"] == item]
        if l.empty:
            return None
        r = l.iloc[0]
        q = str(r.get("quality", "") or "")
        txt = str(r.get("source", "")) + " " + str(r.get("method", ""))
        est = q == "C" or "ESTIMATE" in txt or txt.lower().startswith("assumption")
        return Sourced(float(r["value"]), str(r["unit"]), str(r["source"]),
                       None if _nan(r["year"]) else str(r["year"])[:7], est, q,
                       str(r.get("method", "") or ""))

    def fx_latest(self, iso3: str, col: str):
        d = self.fx[(self.fx["iso3"] == iso3) & self.fx[col].notna()].sort_values("year")
        if d.empty:
            return None, None
        return float(d[col].iloc[-1]), int(d["year"].iloc[-1])

    def context(self, iso3: str, indicator: str) -> pd.DataFrame:
        if self.ctx.empty:
            return self.ctx
        return self.ctx[(self.ctx["iso3"] == iso3) & (self.ctx["indicator"] == indicator)] \
            .sort_values("year")

    # ------------------------------------------------------------ US params
    def us_params(self) -> USParams:
        def v(item, default):
            r = self.ref("USA", item)
            return float(r["value"]) if r is not None and not _nan(r["value"]) else default

        def br(item, default):
            r = self.ref("USA", item)
            return parse_brackets(r["note"]) if r is not None else parse_brackets(default)
        feie = v("feie_max_exclusion", 132900)
        return USParams(
            brackets_mfj=br("brackets_mfj", "10:0;12:24800;22:100800;24:211400;32:403550;"
                                            "35:512450;37:768700"),
            brackets_single=br("brackets_single", "10:0;12:12400;22:50400;24:105700;"
                                                  "32:201775;35:256225;37:640600"),
            std_mfj=v("std_deduction_mfj", 32200), std_single=v("std_deduction_single", 16100),
            ctc=v("child_tax_credit", 2200), ctc_refundable=v("child_tax_credit_refundable", 1700),
            feie=feie, housing_base=v("foreign_housing_base_amount", 0.16 * feie),
            housing_limit=v("foreign_housing_limit_default", 0.30 * feie),
            fica_rate=v("fica_employee_rate", 7.65) / 100, ss_wage_base=v("ss_wage_base", 184500))

    # ------------------------------------------------------------ country
    @lru_cache(maxsize=128)
    def country(self, iso3: str, state: str | None = None) -> CountryData:
        cd = self._country(iso3)
        cd = self._apply_params(cd)
        if iso3 == "USA" and state:
            cd = self._apply_state(cd, state)
        return cd

    # ------------------------------------------------------------ curated parameters
    def _cpi(self, iso3: str) -> dict[int, float]:
        d = self.fx[(self.fx["iso3"] == iso3) & self.fx["cpi_index"].notna()]
        return {int(y): float(v) for y, v in zip(d["year"], d["cpi_index"])}

    def _budget_ref_income(self, iso3: str, cd: CountryData) -> Sourced | None:
        """Net income of the households behind the survey budget, in the budget's price
        year (local currency/year): the budget is scaled to the simulated couple's income
        relative to this (spending rises less than one-for-one with income)."""
        fixed = {  # (value, year, source, quality)
            "USA": (104117, 2023, "BLS CE 2023: income after taxes, married couple only "
                    "(CXUINCAFTTXLB0603M)", "A"),
            "CAN": (110000, 2023, "ESTIMATE: Canadian Income Survey after-tax income of "
                    "couples without children (mean, ~1.15x the median)", "C"),
            "GBR": (52000, 2024, "ESTIMATE: ONS Family Spending / HBAI mean disposable income "
                    "of non-retired two-adult households", "C"),
        }
        proxy = {"CHE": "AUT", "LIE": "AUT", "ISL": "NOR"}
        b = self.budget[self.budget["iso3"] == iso3]
        price_year = int(str(b["price_year"].iloc[0])[:4]) if not b.empty else cd.data_year
        cpi = cd.cpi
        if iso3 in fixed:
            v, y, src, q = fixed[iso3]
            f = cpi.get(price_year, cpi.get(max(cpi), 1)) / cpi.get(y, 1) if cpi and y in cpi \
                else 1.0
            return Sourced(v * f, f"{cd.currency}/year", src, y, q == "C", q,
                           f"rolled to {price_year} prices with CPI")
        src_iso = proxy.get(iso3, iso3)
        sb = self.budget[self.budget["iso3"] == src_iso]
        if sb.empty:
            return None
        survey = int(str(sb["survey_year"].iloc[0])[:4])
        l = self.life[(self.life["iso3"] == src_iso) &
                      (self.life["item"] == "silc_median_equivalised_A2_lt65")]
        if l.empty:
            return None
        l = l.assign(y=pd.to_numeric(l["year"], errors="coerce"))
        r = l.iloc[(l["y"] - (survey + 1)).abs().argsort().iloc[0]]
        y = int(r["y"])
        cpi_s = self._cpi(src_iso)
        roll = cpi_s.get(price_year, cpi_s.get(max(cpi_s), 1)) / cpi_s.get(y - 1, 1) \
            if cpi_s else 1.0
        v = float(r["value"]) * 1.5 * 1.12 * roll
        legacy = {"HRV": (2023, 7.5345)}          # NAC before the euro changeover
        if src_iso in legacy and y < legacy[src_iso][0]:
            v /= legacy[src_iso][1]
        note = (f"SILC {y} median equivalised net income of two adults <65 (income year {y-1})"
                f" x1.5 (couple) x1.12 (mean/median), x{roll:.3f} CPI to {price_year}")
        if src_iso != iso3:  # proxy budget: same conversion as the budget itself
            ppp_s, _ = self.fx_latest(src_iso, "lcu_per_intl_dollar")
            ppp, _ = self.fx_latest(iso3, "lcu_per_intl_dollar")
            if iso3 == "LIE":
                ppp, _ = self.fx_latest("CHE", "lcu_per_intl_dollar")
            if ppp_s and ppp:
                v *= ppp / ppp_s
                note += f"; proxy budget ({src_iso}) converted at PPP"
        return Sourced(v, f"{cd.currency}/year", "Eurostat EU-SILC ilc_di04", y, True, "B",
                       note)

    def _apply_params(self, cd: CountryData) -> CountryData:
        from dataclasses import replace
        iso3 = cd.iso3
        hh, lend = P.household(iso3), P.lending(iso3)
        notes = list(cd.notes)
        upd: dict = {"hh": hh, "lend": lend, "cpi": self._cpi("CHE" if iso3 == "LIE"
                                                              else iso3)}
        cd = replace(cd, cpi=upd["cpi"])
        rent = P.housing_item(iso3, "rent_new_lease")
        if rent is not None:
            upd["rent"] = rent.with_value(rent.value, note=(
                "NEW-lease market rent (~70 m2, 2 bedrooms, excl. utilities). Previous "
                f"default (all sitting tenants, EU-SILC): {cd.rent.value:,.0f}"))
        hp = P.housing_item(iso3, "home_price")
        if hp is None:
            m2 = P.housing_item(iso3, "home_price_m2")
            size = P.housing_item(iso3, "home_m2")
            if m2 is not None:
                hp = m2.with_value(m2.value * size.value, unit=cd.currency,
                                   note=f"{m2.value:,.0f}/m2 x {size.value:.0f} m2 "
                                   "(existing family flat)")
        if hp is not None:
            upd["home_price"] = hp
        if lend:
            src = str(lend.get("source", ""))
            q = str(lend.get("quality", "B"))
            upd["down_payment_pct"] = Sourced(lend["down_pct"], "%", src, "2026", q == "C", q,
                                              "minimum down payment for a first home; "
                                              f"{lend['down_pct_young']:g}% up to age "
                                              f"{lend['young_max_age']:g}"
                                              if lend.get("young_max_age") else "")
            upd["transfer_tax_pct"] = Sourced(lend["transfer_pct"], "%", src, "2026",
                                              q == "C", q)
        w = P.household_sourced(iso3, "wedding_cost", cd.currency)
        if w is not None:
            upd["wedding_cost"] = w
        cc = hh.get("childcare_override", float("nan"))
        if cc == cc:
            upd["childcare_per_child"] = Sourced(cc, f"{cd.currency}/year per child",
                                                 P._hh_sources()["childcare_override"][0],
                                                 "2025", True, "C",
                                                 f"OECD TaxBEN value was "
                                                 f"{cd.childcare_per_child.value:,.0f}")
        upd["emergency_expense_size"] = Sourced(
            0.0, cd.currency, "stress test only: the survey budgets already include "
            "irregular costs (repairs, dental, replacements)", None, False, "",
            f"size of one unexpected expense (EU-SILC/Fed SHED): "
            f"{cd.emergency_expense_size.value:,.0f}")
        rwg = hh.get("real_wage_growth", float("nan"))
        if rwg == rwg:
            upd["real_wage_growth_hist"] = cd.real_wage_growth_hist.with_value(
                rwg, source=P._hh_sources()["real_wage_growth"][0], estimated=True,
                quality="C", note=f"history: {cd.real_wage_growth_hist.value:.2f}%/yr "
                f"({cd.real_wage_growth_hist.source})")
        snap = self.snap[self.snap["iso3"] == iso3] if not self.snap.empty else self.snap
        if not snap.empty and "youth_unempl" in snap:
            upd["unemp_youth"] = float(snap["youth_unempl"].iloc[0])
        upd["unemp_total"] = cd.unemployment_rate.value
        upd.update(self._labour(iso3))
        upd["notes"] = notes
        cd = replace(cd, **upd)
        cd = replace(cd, budget_ref_income=self._budget_ref_income(iso3, cd))
        cd = replace(cd, budget_ref_housing=self._budget_ref_housing(iso3, cd))
        return cd

    def labour_history(self, iso3: str, indicator: str, band: str, edu: str) -> pd.DataFrame:
        """Yearly values of one labour_market cell (for charts)."""
        L = self.labour
        if L.empty:
            return L
        return L[(L["iso3"] == iso3) & (L["indicator"] == indicator) & (L["age_band"] == band)
                 & (L["education"] == edu)].sort_values("year")

    def _labour(self, iso3: str) -> dict:
        out: dict = {"labour": {}, "edu_pay": {}, "edu_pay_src": None}
        L = self.labour
        if not L.empty:
            last = L[L["iso3"] == iso3].sort_values("year").groupby(
                ["indicator", "age_band", "education"]).tail(1)
            for r in last.itertuples(index=False):
                q = str(r.quality) if not _nan(r.quality) else ""
                out["labour"][(r.indicator, r.age_band, r.education)] = Sourced(
                    float(r.value), str(r.unit), str(r.source), int(r.year),
                    bool(r.estimated), q, "" if _nan(r.method) else str(r.method))
        E = self.edu_pay
        if not E.empty:
            e = E[E["iso3"] == iso3]
            out["edu_pay"] = {(r.education, r.age_band): float(r.factor)
                              for r in e.itertuples(index=False)}
            if not e.empty:
                r = e.iloc[0]
                out["edu_pay_src"] = Sourced(
                    float("nan"), "x all-levels pay", str(r["source"]),
                    None if _nan(r["year"]) else int(r["year"]), bool(e["estimated"].any()),
                    "B", "" if _nan(r["method"]) else str(r["method"]))
        return out

    def _budget_ref_housing(self, iso3: str, cd: CountryData) -> Sourced | None:
        """Monthly housing costs (excl. utilities) of the survey households, same price
        year as the budget: EU-SILC mean total housing cost of two-adult households (all
        tenures) minus the utilities that sit in the budget; CE/SHS/FS shelter lines for
        the US, Canada and the UK."""
        b = self.budget[self.budget["iso3"] == iso3]
        if b.empty:
            return None
        price_year = int(str(b["price_year"].iloc[0])[:4])
        lines = b.groupby("category")["monthly_local"].sum()
        if iso3 in ("USA", "CAN", "GBR"):
            v = float(lines.get("rent_actual", 0) + lines.get("owner_costs_mortgage_tax_maint", 0)
                      + 0.5 * lines.get("other_items_incl_mortgage_interest", 0))
            return Sourced(v, f"{cd.currency}/month", "survey shelter spending (rent + owner "
                           "costs incl. mortgage interest)", price_year, True, "B")
        proxy = {"CHE": "AUT", "LIE": "AUT", "ISL": "NOR"}
        src = proxy.get(iso3, iso3)
        sb = self.budget[self.budget["iso3"] == src]
        survey = int(str(sb["survey_year"].iloc[0])[:4])
        l = self.life[(self.life["iso3"] == src) & (self.life["item"] == "silc_A2_mean_housing_cost")]
        if l.empty:
            return None
        l = l.assign(y=pd.to_numeric(l["year"], errors="coerce"))
        r = l.iloc[(l["y"] - (survey + 1)).abs().argsort().iloc[0]]
        cpi_s = self._cpi(src)
        y = int(r["y"])
        roll = cpi_s.get(price_year, cpi_s.get(max(cpi_s), 1)) / cpi_s.get(y, 1) if cpi_s else 1
        v = float(r["value"]) * roll
        if src != iso3:
            ppp_s, _ = self.fx_latest(src, "lcu_per_intl_dollar")
            ppp, _ = self.fx_latest("CHE" if iso3 == "LIE" else iso3, "lcu_per_intl_dollar")
            if ppp_s and ppp:
                v *= ppp / ppp_s
        util = sum(x.value for k, x in cd.budget.items() if k in UTILITY_CATS)
        return Sourced(max(v - util, 0.0), f"{cd.currency}/month", "Eurostat EU-SILC ilc_mded03 "
                       "(two adults, all tenures)", y, True, "B",
                       f"{v:,.0f} incl. utilities minus {util:,.0f} utilities in the budget")

    def _apply_state(self, cd: CountryData, state: str) -> CountryData:
        """US state: earnings, rents, home prices, prices, property tax and premiums."""
        from dataclasses import replace
        S = P.us_states()
        if state not in S.index:
            return cd
        s, us = S.loc[state], S.loc["US"]
        earn_f = (s["earn_ft_male"] + s["earn_ft_female"]) / (us["earn_ft_male"] +
                                                              us["earn_ft_female"])
        price_f = (100 + 0.35 * (s["rpp"] - 100)) / 100   # non-housing price level
        earnings = {k: [(a, v * earn_f) for a, v in pts] for k, pts in cd.earnings.items()}
        budget = {k: v.with_value(v.value * price_f, note=f"x{price_f:.3f} state prices "
                                  f"(BEA RPP {s['rpp']:.1f}, non-rent share)")
                  for k, v in cd.budget.items()}
        util = 0.75 * cd.budget["utilities_phone"].value * price_f \
            if "utilities_phone" in cd.budget else 200.0
        rent = Sourced(max(s["rent_recent_gross"] - util, 0.5 * s["rent_recent_gross"]),
                       "USD/month", "Census ACS 2025 B25113: median gross rent, moved in 2024 "
                       "or later", "2025", False, "A",
                       f"gross {s['rent_recent_gross']:,.0f} minus utilities {util:,.0f}")
        nar = 415000 / us["home_value"]
        hp = Sourced(s["home_value"] * nar, "USD", "Census ACS 2025 B25077 median home value "
                     f"x {nar:.3f} (NAR 2025 median sale price / ACS national median)",
                     "2025", False, "B")
        lend = dict(cd.lend)
        lend["property_tax_pct"] = round(100 * s["re_taxes"] / s["home_value"], 3)
        hh = dict(cd.hh)
        for k in ("health_premium_adult", "health_premium_young", "health_premium_child"):
            hh[k] = hh[k] * s["health_contrib_factor"]
        hh["car_insurance"] *= s["car_ins_factor"]
        if s["pfl_weeks"] > 0:
            m = s["pfl_weeks"] / 4.33
            hh["leave1_months"], hh["leave1_rr"] = max(m, hh["leave1_months"]), s["pfl_rr"]
            hh["partner_leave_months"] = max(hh["partner_leave_months"], min(m, 2.8))
            hh["partner_leave_rr"] = s["pfl_rr"]
        cc = cd.childcare_per_child
        ccf = (s["rpp"] / 100) ** 1.5
        st_tax = P.us_state_tax().loc[state].to_dict() if state in P.us_state_tax().index \
            else None
        notes = list(cd.notes) + [f"US state: {s['name']}. Earnings x{earn_f:.3f} (ACS 2025 "
                                  "median full-time earnings vs US), prices x"
                                  f"{price_f:.3f}, property tax {lend['property_tax_pct']:.2f}%"
                                  " of value (ACS)."]
        return replace(cd, name=f"{cd.name} – {s['name']}", earnings=earnings, budget=budget,
                       rent=rent, home_price=hp, lend=lend, hh=hh, notes=notes, state=state,
                       state_tax=st_tax, state_info=s.to_dict(),
                       childcare_per_child=cc.with_value(cc.value * ccf, note=(
                           f"national x{ccf:.2f} (state price level^1.5: childcare is "
                           "labour-intensive)")),
                       budget_ref_income=cd.budget_ref_income.with_value(
                           cd.budget_ref_income.value * earn_f)
                       if cd.budget_ref_income else None)

    def _country(self, iso3: str) -> CountryData:
        notes: list[str] = []
        name = COUNTRY_NAMES.get(iso3, iso3)
        # -- earnings
        e = self.earn[(self.earn["iso3"] == iso3) & self.earn["age_mid"].notna()]
        narrow = e[e["age_width"] <= 15]
        if narrow["age_band"].nunique() >= 3:
            e = narrow
        earnings = {}
        for stat, g in e.groupby("statistic"):
            pts = sorted((float(a), float(v)) for a, v in zip(g["age_mid"], g["annual_gross_local"]))
            earnings[stat] = pts
        currency = str(e["currency"].iloc[0]) if not e.empty else "EUR"
        est = bool(e["estimated"].any()) if not e.empty else True
        earnings_src = Sourced(float("nan"), f"{currency}/year", str(e["source"].iloc[0])
                               if not e.empty else "", int(e["year"].max()) if not e.empty else None,
                               est, "B" if est else "A",
                               "; ".join(sorted(set(e["method"].dropna().astype(str))))[:300])
        # -- tax
        t = self.taxc[self.taxc["iso3"] == iso3].sort_values("aw_multiple")
        pts = [TaxPoint(r.aw_multiple, r.income_tax_rate, r.employee_ssc_rate)
               for r in t.itertuples()]
        aw = float(t["avg_wage_local"].iloc[0]) if not t.empty else float("nan")
        tyear = int(t["year"].max()) if not t.empty else None
        tax_src = Sourced(float("nan"), "", str(t["source"].iloc[0]) if not t.empty else "",
                          tyear, bool(t["estimated"].any()) if not t.empty else True,
                          str(t["quality"].iloc[0]) if not t.empty else "C")
        mtr = t["marginal_rate_top"].dropna()
        kid = t["child_net_benefit_per_child_frac_aw"].dropna()
        child_ben = Sourced(float(kid.iloc[0]) * aw if not kid.empty else 0.0,
                            f"{currency}/year per child",
                            "OECD Taxing Wages: net income of couple (100%+67% AW) with 2 "
                            "children minus without, / 2" if not kid.empty else
                            "not available (0)", tyear, kid.empty, "A" if not kid.empty else "C")
        # -- budget
        b = self.budget[(self.budget["iso3"] == iso3) & ~self.budget["category"].isin(TENURE)]
        budget = {}
        for cat, g in b.groupby("category"):
            r = g.iloc[0]
            budget[cat] = Sourced(float(g["monthly_local"].sum()), f"{currency}/month",
                                  str(r["source"]), str(r["price_year"])[:7],
                                  bool(g["estimated"].any()),
                                  "C" if "ESTIMATE" in str(r["method"]) else "B",
                                  str(r["method"])[:200])
        if not budget:
            notes.append("No household budget survey: budget is empty - enter one manually.")
        util = sum(v.value for k, v in budget.items() if k in UTILITY_CATS)
        if iso3 == "USA":
            util += 0.75 * budget["utilities_phone"].value if "utilities_phone" in budget else 0
        # -- rent (monthly, couple, EXCLUDING utilities: they are in the budget)
        rent = None
        for item, incl_util in (("average_private_rent", False),
                                ("median_of_centres_avg_rent_2br", False),
                                ("median_gross_rent", True),
                                ("tenant_total_housing_cost", True)):
            s = self.life_item(iso3, item)
            if s is not None:
                if incl_util and util:
                    v = max(s.value - util, 0.5 * s.value)
                    rent = s.with_value(v, estimated=True, note=(
                        f"{s.note}; utilities in the budget ({util:,.0f}/month) subtracted"))
                else:
                    rent = s
                break
        if rent is None and iso3 == "LIE":
            ch = self.country("CHE")
            rent = ch.rent.with_value(ch.rent.value, estimated=True, quality="C",
                                      source="ESTIMATE: Swiss rent used for Liechtenstein; "
                                      + ch.rent.source)
        if rent is None:
            rent = Sourced(0.0, f"{currency}/month", "missing - enter rent", None, True, "C")
            notes.append("No rent data: enter a monthly rent.")
        rf1 = self.life_item(iso3, "rent_factor_one_child")
        rf2 = self.life_item(iso3, "rent_factor_two_children")
        rent_f = [rf1.value if rf1 else 1.15, rf2.value if rf2 else 1.30]
        rent_f_src = rf1 or Sourced(1.15, "ratio", "assumption: +15% / +30% rent with 1 / 2 "
                                    "children (bigger home)", None, True, "C")
        # -- house
        hp = self.life_item(iso3, "home_price") or Sourced(0.0, currency, "missing", None, True, "C")
        if "Deloitte" in hp.source:
            hp = hp.with_value(hp.value, note="NEW dwellings, city-weighted: a suburban resale "
                               "home is usually cheaper - adjust. " + hp.note)
        dp = self.life_item(iso3, "down_payment_pct") or Sourced(20.0, "%", "assumption", None,
                                                                 True, "C")
        tt = self.life_item(iso3, "transfer_tax_pct")
        if tt is None:
            tt = {"GBR": Sourced(0.0, "%", "Stamp duty: first-time-buyer relief (0% up to "
                                 "GBP 300k, 5% to 500k) - England/NI", "2026", False, "B"),
                  "USA": Sourced(0.5, "%", "assumption: state/local transfer taxes ~0-2%",
                                 None, True, "C"),
                  "CAN": Sourced(1.5, "%", "assumption: provincial land transfer tax "
                                 "(~1-2%, Ontario/BC higher)", None, True, "C")}.get(
                iso3, Sourced(2.0, "%", "assumption: no data (typical 1-4%)", None, True, "C"))
        mr = self.life_item(iso3, "mortgage_rate") or Sourced(4.0, "%", "assumption: no data",
                                                              None, True, "C")
        wed = self.life_item(iso3, "average_cost") or Sourced(0.0, currency, "missing", None,
                                                              True, "C")
        if wed.source.startswith("derived"):
            wed = wed.with_value(wed.value, estimated=True)
        # -- children
        cs = self.life_item(iso3, "extra_spending_per_child")
        if cs is None:
            tot = sum(v.value for v in budget.values())
            cs = Sourced(0.15 * tot, f"{currency}/month", "assumption: +15% of the couple "
                         "budget per child (no survey split)", None, True, "C")
        cc = self._childcare(iso3, aw, currency, tyear)
        # -- emergencies
        es = self.life_item(iso3, "unexpected_expense_size") or Sourced(
            0.0, currency, "missing", None, True, "C")
        if es.unit.startswith("EUR") and currency != "EUR":
            r_l, _ = self.fx_latest(iso3, "lcu_per_usd")
            r_e, _ = self.fx_latest("EMU", "lcu_per_usd")
            if r_l and r_e:
                es = es.with_value(es.value * r_l / r_e, unit=currency)
        if iso3 == "LIE":  # no own data: borrow Switzerland's (same currency, similar costs)
            ch = self.country("CHE")

            def swiss(s: Sourced, other: Sourced) -> Sourced:
                if s.value or not other.value:
                    return s
                return other.with_value(other.value, estimated=True, quality="C",
                                        source="ESTIMATE: Swiss value used for Liechtenstein; "
                                        + other.source)
            hp, wed = swiss(hp, ch.home_price), swiss(wed, ch.wedding_cost)
            cc, es = swiss(cc, ch.childcare_per_child), swiss(es, ch.emergency_expense_size)
        nrr = self._nrr(iso3)
        un = self.context(iso3, "unemployment_rate")
        unemp = Sourced(float(un["value"].iloc[-1]), "%", str(un["source"].iloc[-1]),
                        int(un["year"].iloc[-1])) if not un.empty else Sourced(float("nan"))
        inf = self.context(iso3, "inflation_cpi")
        inf10 = inf[inf["year"] > inf["year"].max() - 10] if not inf.empty else inf
        infl = Sourced(float(inf10["value"].mean()) if not inf10.empty else 2.0, "%/yr",
                       "World Bank CPI, 10-year average", f"{int(inf10['year'].min())}-"
                       f"{int(inf10['year'].max())}" if not inf10.empty else None)
        rwg = self._real_wage_growth(iso3)
        fxv, fxy = self.fx_latest(iso3, "lcu_per_usd")
        pppv, pppy = self.fx_latest(iso3, "lcu_per_intl_dollar")
        if iso3 == "LIE":
            fxv, fxy = self.fx_latest("CHE", "lcu_per_usd")
            pppv, pppy = pppv or self.fx_latest("CHE", "lcu_per_intl_dollar")[0], pppy
        if iso3 == "BGR":
            notes.append("Bulgaria adopted the euro on 1 Jan 2026; amounts are in lev (BGN) "
                         "because all historical statistics are. 1 EUR = 1.95583 BGN.")
        tot_ref = self.ref(iso3, "us_totalization_agreement")
        data_year = fxy or 2025
        return CountryData(
            iso3=iso3, name=name, currency=currency, data_year=data_year, earnings=earnings,
            earnings_src=earnings_src, tax_points=pts,
            avg_wage=Sourced(aw, f"{currency}/year", tax_src.source, tyear),
            marginal_rate_top=float(mtr.iloc[0]) if not mtr.empty else None, tax_src=tax_src,
            child_benefit_per_child=child_ben, budget=budget, rent=rent,
            rent_factor_per_child=rent_f, rent_factor_src=rent_f_src, home_price=hp,
            down_payment_pct=dp, transfer_tax_pct=tt, mortgage_rate=mr, wedding_cost=wed,
            child_extra_spending=cs, childcare_per_child=cc, emergency_expense_size=es, nrr=nrr,
            unemployment_rate=unemp, inflation_hist=infl, real_wage_growth_hist=rwg,
            lcu_per_usd=Sourced(fxv or 1.0, f"{currency} per USD", "World Bank PA.NUS.FCRF", fxy),
            lcu_per_intl=Sourced(pppv or (fxv or 1.0), f"{currency} per intl $",
                                 "World Bank PA.NUS.PRVT.PP (private consumption PPP)", pppy),
            totalization=bool(tot_ref is not None or iso3 == "USA"), notes=notes)

    def _childcare(self, iso3, aw, currency, year) -> Sourced:
        """OECD net childcare cost for a couple (100%+67% AW) with two children aged 2-3
        in full-time centre care, per child per year."""
        l = self.life[(self.life["iso3"] == iso3) &
                      self.life["item"].astype(str).str.startswith("childcare_net_cost")]
        if l.empty:
            return Sourced(0.0, f"{currency}/year", "missing - enter childcare cost", None,
                           True, "C")
        # prefer an amount in national currency; else a % of AW / of net income
        for unit_rx, conv, how in (
                (r"^XDC$|NAC|national currency", lambda v: v, "national currency"),
                (r"PT_AW|average wage|_AW", lambda v: v / 100 * aw, "% of average wage x AW"),
                (r"PT_INC_NET|net.*income|PT_NHI", lambda v: v / 100 * aw * 1.67 * 0.75,
                 "% of net household income x approx. net income (1.67 AW x 0.75)")):
            s = l[l["unit"].astype(str).str.contains(unit_rx, regex=True, case=False)]
            if not s.empty:  # OECD XDC values are ANNUAL, for both children
                r = s.sort_values("value").iloc[len(s) // 2]   # median across variants
                v = conv(float(r["value"]))
                per_year = v * 12 if "month" in str(r["unit"]).lower() else v
                return Sourced(per_year / 2, f"{currency}/year per child", "OECD TaxBEN net "
                               "childcare cost (2 children, full-time centre care)",
                               r["year"], how != "national currency", "A",
                               f"{r['item']} [{r['unit']}] -> {how}; / 2 children")
        return Sourced(0.0, f"{currency}/year", "childcare unit not understood", None, True, "C")

    def _nrr(self, iso3) -> dict[int, Sourced]:
        l = self.life[(self.life["iso3"] == iso3) &
                      self.life["item"].astype(str).str.startswith("net_replacement_rate")]
        out = {}
        for r in l.itertuples():
            m = re.search(r"_m(\d+) \((C_C0)[^)]*\)", str(r.item))
            if not m:
                continue
            months = int(m.group(1))
            # several spouse-income variants: keep the 'spouse earns' variant (higher NRR
            # because the spouse's pay continues) if present, else whatever exists
            prev = out.get(months)
            if prev is None or "AW67" in str(r.item):
                out[months] = Sourced(float(r.value), "% of net income", "OECD TaxBEN NRR",
                                      r.year, False, "A", str(r.item))
        return out

    def _real_wage_growth(self, iso3) -> Sourced:
        if self.rwh.empty or "real_cagr_pct_last_10y" not in self.rwh:
            return Sourced(0.5, "%/yr", "assumption", None, True, "C")
        r = self.rwh[(self.rwh["iso3"] == iso3) & self.rwh["real_cagr_pct_last_10y"].notna()]
        for meas, src in (("avg_wage", "OECD average wages"), ("avg_wage", None),
                          ("earnings_by_age", None), ("household_income", None)):
            s = r[r["measure"] == meas]
            if src:
                s = s[s["source"] == src]
            if not s.empty:
                x = s.iloc[0]
                return Sourced(float(x["real_cagr_pct_last_10y"]), "%/yr",
                               f"{x['source']} ({x['measure']}, MEAN wage)" if meas == "avg_wage"
                               else str(x["source"]), int(x["year"]), False, "A",
                               "real CAGR over the last 10 years")
        return Sourced(0.5, "%/yr", "assumption: no wage history", None, True, "C")

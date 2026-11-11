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
    @lru_cache(maxsize=64)
    def country(self, iso3: str) -> CountryData:
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

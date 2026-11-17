"""Derived cost of living: the couple's budget, its coverage and price trends."""

from __future__ import annotations

import re

import pandas as pd

from econdata.config import COUNTRIES, LOCAL_CURRENCY
from econdata.derive.context import Ctx
from econdata.sources.spending import (
    COICOP_CATS,
    HH_COUPLE,
    HH_EXCLUDE,
    HICP_CODES,
    LEVEL2,
    US_CPI_PARTS,
)

# ---------- Couple budget ------------------------------------------------
BUDGET_LABELS = {
    "groceries": "Groceries",
    "alcohol_tobacco": "Alcohol & tobacco",
    "clothing": "Clothing",
    "rent_actual": "Rent (survey avg over ALL households - see housing rows)",
    "home_maintenance": "Home maintenance",
    "water_refuse": "Water & refuse",
    "energy_home": "Electricity, gas, heating",
    "furnishings_household": "Furnishings & household",
    "health_out_of_pocket": "Healthcare out of pocket",
    "vehicle_purchase": "Vehicle purchase",
    "fuel_vehicle": "Vehicle fuel",
    "vehicle_maintenance": "Vehicle maintenance & parts",
    "vehicle_other": "Other vehicle services (parking, tolls)",
    "transport_services": "Public transport & other transport services",
    "phone_internet": "Phone & internet",
    "recreation": "Recreation & culture",
    "education": "Education",
    "restaurants_hotels": "Restaurants & hotels",
    "insurance_home": "Home insurance",
    "insurance_health": "Health insurance (private)",
    "insurance_vehicle": "Vehicle insurance",
    "insurance_other": "Other insurance",
    "personal_misc": "Personal care & misc",
    "council_tax": "Council tax",
    "health_insurance_mandatory": "Mandatory health insurance premiums",
    "household_operations": "Household operations (incl. phone, services)",
    "utilities_phone": "Utilities & phone",
    "vehicle_all": "Private transport (all)",
    "transport_all": "Transport (all)",
    "housing_net_fuel_power": "Housing (net), fuel & power",
    "vehicle_running": "Vehicle running costs (fuel, insurance, repairs)",
}


CE_TO_CAT = {
    "FOODHOME": "groceries",
    "FOODAWAY": "restaurants_hotels",
    "ALCBEVG": "alcohol_tobacco",
    "RNTDWELL": "rent_actual",
    "OWNDWELL": "owner_costs_mortgage_tax_maint",
    "UTILS": "utilities_phone",
    "HHOPER": "household_operations",
    "HKPGSUPP": "furnishings_household",
    "HHFURNSH": "furnishings_household",
    "APPAREL": "clothing",
    "VEHPURCH": "vehicle_purchase",
    "GASOIL": "fuel_vehicle",
    "VEHINSUR": "insurance_vehicle",
    "PUBTRANS": "transport_services",
    "HLTHINSR": "insurance_health",
    "ENTRTAIN": "recreation",
    "PERSCARE": "personal_misc",
    "EDUCATN": "education",
    "MISC": "personal_misc",
}


# Rent/owner costs come from the housing rows (rent vs mortgage is the user's switch),
# so they are excluded from the baseline total. UK CP04 mixes rent and energy (noted).
HOUSING_TENURE_CATS = [
    "rent_actual",
    "rent_imputed_owners",
    "owner_costs_mortgage_tax_maint",
    "other_items_incl_mortgage_interest",
    "housing_net_fuel_power",
    "TOTAL",
]


def derive_couple_budget(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    eu = pd.DataFrame(_budget_eu(long, ctx))
    uk = pd.DataFrame(_budget_uk(long, ctx))
    if not uk.empty and not eu.empty:  # national survey (FYE2025) beats HBS (2015/2020)
        eu = eu[eu["iso3"] != "GBR"]
    df = pd.concat(
        [eu, uk, pd.DataFrame(_budget_us(long, ctx)), pd.DataFrame(_budget_ca(long, ctx))],
        ignore_index=True,
    )
    if df.empty:
        return df
    df = pd.concat([df, pd.DataFrame(_budget_proxy(df, ctx))], ignore_index=True)
    df = pd.concat([df, pd.DataFrame(_mandatory_health(df, ctx))], ignore_index=True)
    df["category_label"] = df["category"].map(BUDGET_LABELS).fillna(df["category"])
    return df


def _match_hh(series: pd.Series, patterns: list[str]) -> str | None:
    s = series.astype(str)
    for pat in patterns:
        m = s[
            s.str.contains(pat, case=False, regex=True)
            & ~s.str.contains(HH_EXCLUDE, case=False, regex=True)
        ]
        if not m.empty:
            return m.iloc[0]
    return None


def _per_household(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Prefer per-household units; if only per-adult-equivalent exists, return it with
    the modified-OECD factor for a couple (1.5) so values become per couple household."""
    u = df["unit"].astype(str)
    hh = df[u.str.contains("HH", case=False)]
    if not hh.empty:
        return hh, 1.0
    ae = df[u.str.contains("AE", case=False)]
    if not ae.empty:
        return ae, 1.5
    return df, 1.0


# Non-rent parts of COICOP 04 (housing, water, electricity, gas): maintenance, water and
# refuse, energy. Rents (CP041 actual, CP042 imputed) are covered by the rent / purchase.
CP04_NON_RENT = ("CP043", "CP044", "CP045")


def _budget_eu(long, ctx):
    hbs = long[long["measure"] == "household_spending_raw"]
    out = []
    cp04_split: dict[str, list[float]] = {c: [] for c in CP04_NON_RENT}  # share of CP04
    for iso3, g in hbs.groupby("iso3"):
        tot_all = g[g["statistic"] == "total_by_hhtype"]
        if tot_all.empty:
            continue
        year = int(tot_all["year"].max())
        tot, ae_factor = _per_household(tot_all[tot_all["year"] == year])
        couple_label = _match_hh(tot["household_type"], HH_COUPLE)
        if couple_label is None:
            continue
        total_pps = tot[tot["household_type"] == couple_label]["value"].iloc[0] * ae_factor
        nat_tot = tot[tot["household_type"].astype(str).str.contains("^total", case=False)]
        # --- towns & suburbs factor on the total ---------------------------------
        urb, _ = _per_household(g[(g["statistic"] == "total_by_urb") & (g["year"] == year)])
        deg2 = urb[urb["area_type"].astype(str).str.contains("towns and suburbs", case=False)]
        allu = urb[urb["area_type"].astype(str).str.contains("^total|all areas", case=False)]
        denom = (
            allu["value"].iloc[0]
            if not allu.empty
            else (
                nat_tot["value"].iloc[0] / (1.5 if ae_factor == 1.5 else 1)
                if not nat_tot.empty and ae_factor == 1.0
                else None
            )
        )
        urb_factor = deg2["value"].iloc[0] / denom if (not deg2.empty and denom) else 1.0
        # --- level-2 structure of the couple type, tilted towards towns & suburbs --
        sh = g[(g["statistic"] == "share_by_hhtype") & (g["year"] == year)]
        shares = {
            r.category: r.value / 1000
            for r in sh[sh["household_type"] == couple_label].itertuples()
            if r.category in LEVEL2
        }
        national = {
            r.category: r.value / 1000
            for r in sh[
                sh["household_type"].astype(str).str.contains("^total", case=False)
            ].itertuples()
            if r.category in LEVEL2
        }
        lv = g[(g["statistic"] == "level_by_coicop") & (g["year"] == year)]
        lv, _ = _per_household(lv) if not lv.empty else (lv, 1.0)
        lvl = {r.category: r.value for r in lv.itertuples()}
        if lvl.get("CP04") and all(lvl.get(c) for c in CP04_NON_RENT):
            for c in CP04_NON_RENT:
                cp04_split[c].append(lvl[c] / lvl["CP04"])
        if not national and lvl.get("CP00"):
            national = {c: lvl[c] / lvl["CP00"] for c in LEVEL2 if c in lvl}
        if not shares:
            shares = dict(national)
        if not shares:
            continue
        su = g[(g["statistic"] == "share_by_urb") & (g["year"] == year)]
        su2 = {
            r.category: r.value / 1000
            for r in su[
                su["area_type"].astype(str).str.contains("towns and suburbs", case=False)
            ].itertuples()
        }
        adj = {
            c: v * (su2[c] / national[c] if su2.get(c) and national.get(c) else 1.0)
            for c, v in shares.items()
        }
        s = sum(adj.values()) or 1.0
        adj = {c: v / s for c, v in adj.items()}
        # --- split into budget categories using national detailed structure -------
        cat_share: dict[str, tuple[float, str]] = {}
        for parent in LEVEL2:
            if parent not in adj:
                continue
            kids = [c for c in COICOP_CATS if c.startswith(parent) and len(c) > 4]
            used = 0.0
            if lvl.get(parent):
                for c in kids:
                    if c not in lvl:
                        continue
                    sub = lvl[c]
                    if c == "CP125":  # 'other insurance' = CP125 minus listed subtypes
                        sub = max(
                            lvl[c] - sum(lvl.get(k, 0) for k in ("CP1252", "CP1253", "CP1254")), 0
                        )
                    sh_c = adj[parent] * sub / lvl[parent]
                    cat_share[c] = (sh_c, COICOP_CATS[c])
                    used += sh_c
            if parent in COICOP_CATS:  # remainder (or all of it) to the parent category
                cat_share[parent] = (max(adj[parent] - used, 0), COICOP_CATS[parent])
            elif used == 0:
                cat_share[parent] = (adj[parent], parent.lower() + "_unsplit")
        nac = ctx.nac_per_pps(iso3, year)
        if not nac:
            continue
        couple_pps = total_pps * urb_factor
        for code, (share, cat) in cat_share.items():
            annual_local = couple_pps * share * nac
            factor, py, used_idx = ctx.hicp_factor(iso3, code, year)
            annual_local *= factor
            intl, _ = ctx.intl(annual_local, iso3, py)
            out.append(
                {
                    "iso3": iso3,
                    "country": COUNTRIES[iso3][2],
                    "category": cat,
                    "coicop": code,
                    "monthly_local": annual_local / 12,
                    "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                    "monthly_intl_dollars": intl / 12 if intl else None,
                    "survey_year": year,
                    "price_year": py,
                    "household_type": couple_label,
                    "area_type": "towns and suburbs" if urb_factor != 1.0 else "all areas",
                    "method": f"HBS couple total x share; urb factor {urb_factor:.3f}; "
                    f"HICP {used_idx or 'none'} x{factor:.3f}"
                    + ("; per-adult-equivalent x1.5" if ae_factor != 1 else ""),
                    "source": "Eurostat HBS",
                    "estimated": True,
                }
            )
    return _split_unsplit_cp04(out, cp04_split)


def _split_unsplit_cp04(out: list[dict], cp04_split: dict[str, list[float]]) -> list[dict]:
    """A country without HBS detail for COICOP 04 got the whole group as 'cp04_unsplit',
    and that group is mostly actual + imputed RENT, which the rent / house purchase already
    pays. Keep only its utilities and maintenance, using the median structure of the
    countries that publish the detail."""
    med = {c: float(pd.Series(v).median()) for c, v in cp04_split.items() if v}
    res = []
    for r in out:
        if r["category"] != "cp04_unsplit":
            res.append(r)
            continue
        if len(med) < len(CP04_NON_RENT):
            continue  # no structure to borrow: drop it rather than double-count rent
        for c in CP04_NON_RENT:
            k = dict(r)
            k.update(
                category=COICOP_CATS[c],
                coicop=c,
                monthly_local=r["monthly_local"] * med[c],
                monthly_intl_dollars=(r["monthly_intl_dollars"] or 0) * med[c] or None,
                method=r["method"] + f"; no CP04 detail: x{med[c]:.3f} = median {c} share "
                "of CP04 in countries with detail",
            )
            res.append(k)
    return res


def _budget_us(long, ctx):
    ce = long[
        (long["source"] == "BLS CE (via FRED)") & (long["household_type"] == "married couple only")
    ]
    if ce.empty:
        return []
    year = int(ce["year"].max())
    ce = ce[ce["year"] == year]
    out = []
    v = ce.set_index("category")["value"]
    # Residual categories: FRED only carries the CE aggregates for these groups
    derived = []
    if {"TRANS", "VEHPURCH", "PUBTRANS"} <= set(v.index):
        derived.append(
            (
                "CE:TRANS-VEHPURCH-PUBTRANS",
                "vehicle_running",
                v["TRANS"] - v["VEHPURCH"] - v["PUBTRANS"],
            )
        )
    if {"HEALTH", "HLTHINSR"} <= set(v.index):
        derived.append(("CE:HEALTH-HLTHINSR", "health_out_of_pocket", v["HEALTH"] - v["HLTHINSR"]))
    for code, cat, val in derived:
        intl, _ = ctx.intl(val, "USA", year)
        out.append(
            {
                "iso3": "USA",
                "country": "United States",
                "category": cat,
                "coicop": code,
                "monthly_local": val / 12,
                "currency": "USD",
                "monthly_intl_dollars": intl / 12 if intl else None,
                "survey_year": year,
                "price_year": year,
                "household_type": "married couple only",
                "area_type": "all areas",
                "method": f"CE mean, residual {code.split(':')[1]} / 12",
                "source": "BLS CE",
                "estimated": True,
            }
        )
    for r in ce.itertuples():
        cat = CE_TO_CAT.get(r.category)
        if cat is None:
            continue
        intl, _ = ctx.intl(r.value, "USA", year)
        out.append(
            {
                "iso3": "USA",
                "country": "United States",
                "category": cat,
                "coicop": r.category,
                "monthly_local": r.value / 12,
                "currency": "USD",
                "monthly_intl_dollars": intl / 12 if intl else None,
                "survey_year": year,
                "price_year": year,
                "household_type": "married couple only",
                "area_type": "all areas",
                "method": "CE mean annual spend / 12",
                "source": "BLS CE",
                "estimated": False,
            }
        )
    return out


# Anchored to ONE level of the SHS hierarchy: unanchored matching ("rent" also hit
# "Rented living quarters", "Parking at rented ...") double-counted sub-items.
SHS_MAP = [
    (r"^food purchased from stores$", "groceries"),
    (r"^food purchased from restaurants$", "restaurants_hotels"),
    (r"^rent$", "rent_actual"),
    (r"^owned living quarters$", "owner_costs_mortgage_tax_maint"),
    (r"^water, fuel and electricity for principal accommodation$", "energy_home"),
    (r"^other accommodation$", "restaurants_hotels"),
    (r"^household operations$", "household_operations"),
    (r"^household furnishings and equipment$", "furnishings_household"),
    (r"^clothing and accessories$", "clothing"),
    (r"^private transportation$", "vehicle_all"),
    (r"^public transportation$", "transport_services"),
    (r"^health care$", "health_out_of_pocket"),
    (r"^personal care$", "personal_misc"),
    (r"^recreation$", "recreation"),
    (r"^education$", "education"),
    (r"^reading materials", "recreation"),
    (r"^games of chance", "recreation"),
    (r"^tobacco products", "alcohol_tobacco"),
    (r"^miscellaneous expenditures$", "personal_misc"),
    (r"^total current consumption$", "TOTAL"),
]


SHS_COUPLE = r"^couples? without children"


SHS_KIDS = r"^couples? with children"


def _shs_by_cat(long, pattern):
    shs = long[
        (long["source"] == "StatCan SHS")
        & long["household_type"]
        .astype(str)
        .str.contains(pattern, case=False, na=False, regex=True)
    ]
    if shs.empty:
        return None, {}
    year = int(shs["year"].max())
    agg: dict[str, float] = {}
    for r in shs[shs["year"] == year].itertuples():
        cat = next(
            (c for pat, c in SHS_MAP if re.search(pat, str(r.category).strip(), re.I)), None
        )
        if cat:
            agg[cat] = agg.get(cat, 0.0) + r.value
    return year, agg


def _budget_ca(long, ctx):
    year, agg = _shs_by_cat(long, SHS_COUPLE)
    out = []
    for cat, v in agg.items():
        intl, _ = ctx.intl(v, "CAN", year)
        out.append(
            {
                "iso3": "CAN",
                "country": "Canada",
                "category": cat,
                "coicop": "SHS",
                "monthly_local": v / 12,
                "currency": "CAD",
                "monthly_intl_dollars": intl / 12 if intl else None,
                "survey_year": year,
                "price_year": year,
                "household_type": "Couples without children",
                "area_type": "all areas",
                "method": "SHS mean / 12",
                "source": "StatCan SHS",
                "estimated": False,
            }
        )
    return out


UK_FS_CATS = {
    "CP01": "groceries",
    "CP02": "alcohol_tobacco",
    "CP03": "clothing",
    "CP041": "rent_actual",
    "CP043": "home_maintenance",
    "CP044": "water_refuse",
    "CP045": "energy_home",
    "CP05": "furnishings_household",
    "CP06": "health_out_of_pocket",
    "CP07": "transport_all",
    "CP08": "phone_internet",
    "CP09": "recreation",
    "CP10": "education",
    "CP11": "restaurants_hotels",
    "CP12": "personal_misc",
    "CP13": "other_items_incl_mortgage_interest",
    "TOTAL": "TOTAL",
}


def _budget_uk(long, ctx):
    fs = long[
        (long["source"].astype(str).str.startswith("ONS Family Spending"))
        & (long["household_type"] == "two adults")
    ]
    out = []
    has_split = (fs["category"] == "CP045").any()
    for r in fs.itertuples():
        cat = UK_FS_CATS.get(r.category)
        if r.category == "CP04" and not has_split:
            cat = "housing_net_fuel_power"
        if cat is None:
            continue
        monthly = r.value * 52 / 12
        f, py, _ = ctx.hicp_factor("GBR", "CP00", int(r.year))  # UK HICP ends 2020 -> ~1
        intl, _ = ctx.intl(monthly * 12, "GBR", int(r.year))
        out.append(
            {
                "iso3": "GBR",
                "country": "United Kingdom",
                "category": cat,
                "coicop": r.category,
                "monthly_local": monthly,
                "currency": "GBP",
                "monthly_intl_dollars": intl / 12 if intl else None,
                "survey_year": int(r.year),
                "price_year": int(r.year),
                "household_type": "two adults (non-retired)",
                "area_type": "all areas",
                "method": "weekly x 52/12"
                + (f"; {r.method}" if isinstance(r.method, str) else ""),
                "source": r.source,
                "estimated": bool(r.estimated),
            }
        )
    ct = ctx.ref("GBR", "council_tax_annual")
    if out and ct is not None:
        out.append(
            {
                "iso3": "GBR",
                "country": "United Kingdom",
                "category": "council_tax",
                "coicop": "ref",
                "monthly_local": ct["value"] / 12,
                "currency": "GBP",
                "monthly_intl_dollars": None,
                "survey_year": ct["as_of"],
                "price_year": ct["as_of"],
                "household_type": "Band D",
                "area_type": "England average",
                "method": "annual / 12",
                "source": ct["source"],
                "estimated": False,
            }
        )
    return out


# Countries with no household budget survey in the data: same real consumption as a
# neighbour, re-priced with Eurostat category PPPs. ESTIMATE (quality C).
BUDGET_PROXY = {"CHE": "AUT", "LIE": "AUT", "ISL": "NOR"}


def _budget_proxy(budget: pd.DataFrame, ctx: Ctx) -> list[dict]:
    out = []
    have = set(budget["iso3"]) if not budget.empty else set()
    for iso3, nb in BUDGET_PROXY.items():
        if iso3 in have or nb not in have:
            continue
        src = budget[budget["iso3"] == nb]
        for r in src.to_dict("records"):
            year = int(r["survey_year"]) if str(r["survey_year"]).isdigit() else ctx.latest_year
            code = str(r["coicop"])[:4] if str(r["coicop"]).startswith("CP") else "A01"
            p_nb, p_x = ctx.ppp_category(nb, code, year), ctx.ppp_category(iso3, code, year)
            if iso3 == "LIE" and p_x is None:
                p_x = ctx.ppp_category("CHE", code, year)
            if not p_nb or not p_x:
                continue
            local = r["monthly_local"] / p_nb * p_x
            intl, _ = ctx.intl(local * 12, iso3, ctx.latest_year)
            out.append(
                {
                    **r,
                    "iso3": iso3,
                    "country": COUNTRIES[iso3][2],
                    "monthly_local": local,
                    "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                    "monthly_intl_dollars": intl / 12 if intl else None,
                    "method": f"ESTIMATE: {nb} couple quantities x {iso3}/{nb} PPP "
                    f"for {code} ({p_x / p_nb:.2f})",
                    "source": f"proxy: {r['source']} ({nb}) + Eurostat PPP",
                    "estimated": True,
                }
            )
    return out


def _mandatory_health(budget: pd.DataFrame, ctx: Ctx) -> list[dict]:
    out = []
    for _, r in ctx.refs[ctx.refs["item"] == "mandatory_health_premium_adult_monthly"].iterrows():
        if budget.empty or r["iso3"] not in set(budget["iso3"]):
            continue
        # Some surveys (e.g. Dutch HBS) already record the basic premium under health
        # insurance: only add the reference premium when the survey line is clearly
        # missing it (Swiss/Liechtenstein proxy budgets come from Austria).
        hi = budget[(budget["iso3"] == r["iso3"]) & (budget["category"] == "insurance_health")][
            "monthly_local"
        ].sum()
        if hi >= 0.5 * 2 * r["value"] and not str(
            budget[budget["iso3"] == r["iso3"]]["source"].iloc[0]
        ).startswith("proxy"):
            continue
        out.append(
            {
                "iso3": r["iso3"],
                "country": COUNTRIES[r["iso3"]][2],
                "category": "health_insurance_mandatory",
                "coicop": "ref",
                "monthly_local": 2 * r["value"],
                "currency": str(r["unit"]).split("/")[0],
                "monthly_intl_dollars": None,
                "survey_year": r["as_of"],
                "price_year": r["as_of"],
                "household_type": "2 adults",
                "area_type": "national average",
                "method": "2 x adult premium",
                "source": r["source"],
                "estimated": r["quality"] == "C",
            }
        )
    return out


COVERAGE_CATS = {  # budget category -> COICOP division whose HBS/NA ratio applies
    "groceries": "CP01",
    "alcohol_tobacco": "CP02",
    "clothing": "CP03",
    "furnishings_household": "CP05",
    "transport_services": "CP07",
    "phone_internet": "CP08",
    "recreation": "CP09",
    "education": "CP10",
    "restaurants_hotels": "CP11",
}


COVERAGE_CAP = {"CP11": 1.5}  # domestic concept: tourists' restaurant/hotel spending


def derive_budget_coverage(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """National-accounts household consumption per household / HBS mean per household, by
    COICOP division and HBS wave (2010, 2015, 2020), averaged. Rents (own model), health
    (own module) and CP12 (financial services, insurance, social protection: conceptual
    differences) are not corrected. Clipped to [1, 2] (restaurants & hotels 1.5). The
    engine applies ratio ** survey_correction (default 0.5). Non-EU countries: EU median."""
    h = long[long["measure"] == "hbs_coverage"].dropna(subset=["year", "value"])
    if h.empty:
        return pd.DataFrame()
    hb = h[h.statistic == "hbs_eur_per_household"].set_index(["iso3", "year", "category"])["value"]
    na = h[h.statistic == "na_meur"].set_index(["iso3", "year", "category"])["value"]
    nh = h[h.statistic == "households_ths"].set_index(["iso3", "year"])["value"]
    rows = []
    for (iso, yr, cp), v in hb.items():
        if (iso, yr, cp) in na.index and (iso, yr) in nh.index and v > 0:
            rows.append(
                {
                    "iso3": iso,
                    "year": int(yr),
                    "coicop": cp,
                    "ratio": na[(iso, yr, cp)] * 1e6 / (nh[(iso, yr)] * 1e3) / v,
                }
            )
    r = pd.DataFrame(rows)
    if r.empty:
        return r
    r = r.groupby(["iso3", "coicop"], as_index=False).agg(
        ratio=("ratio", "mean"), year=("year", "max")
    )
    out = []
    med = (
        r[r["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", "", ""))[3] == "EU")]
        .groupby("coicop")["ratio"]
        .median()
    )
    for iso in COUNTRIES:
        for cat, cp in COVERAGE_CATS.items():
            m = r[(r.iso3 == ("CHE" if iso == "LIE" else iso)) & (r.coicop == cp)]
            est = m.empty
            raw = float(med.get(cp, 1.0)) if est else float(m["ratio"].iloc[0])
            out.append(
                {
                    "iso3": iso,
                    "category": cat,
                    "coicop": cp,
                    "raw_ratio": round(raw, 3),
                    "ratio": round(min(max(raw, 1.0), COVERAGE_CAP.get(cp, 2.0)), 3),
                    "year": None if est else int(m["year"].iloc[0]),
                    "estimated": est,
                    "source": (
                        "BEA personal consumption / BLS Consumer Expenditure Survey, per household"
                        if iso == "USA" and not est
                        else "Eurostat national accounts (nama_10_co3_p3) / HBS "
                        "(hbs_exp_t121) per household"
                        + (" - EU median (no survey comparison for this country)" if est else "")
                    ),
                }
            )
    return pd.DataFrame(out)


PRICE_TREND_YEARS = 10


def hicp_budget_codes() -> dict[str, str]:
    """Budget line -> the HICP (COICOP) price index that tracks it."""
    code_for = {}
    for code, key in COICOP_CATS.items():  # most specific code wins
        if code in HICP_CODES and len(code) >= len(code_for.get(key, "")):
            code_for[key] = code
    code_for.update(
        {
            "childcare": "CP124",
            "phone_internet": "CP08",
            "vehicle_running": "CP0723",
            "rent": "CP041",
        }
    )
    return code_for


def derive_price_outlook(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Current price trends: how much faster (or slower) each budget line's prices rose than
    prices in general over the last 10 complete years (HICP by COICOP in Europe, CPI
    components in the US), and US market inflation expectations."""
    rows = []
    pl = long[long["measure"] == "price_level"].dropna(subset=["year", "value"])
    hicp = pl[pl["statistic"] == "hicp_index"]
    code_for = hicp_budget_codes()
    sources = []
    for iso, g in hicp.groupby("iso3"):
        sources.append(
            (
                iso,
                g.pivot_table(index="year", columns="category", values="value"),
                "CP00",
                code_for,
                "Eurostat HICP",
            )
        )
    us = pl[(pl["statistic"] == "cpi_component") & (pl["iso3"] == "USA")]
    if not us.empty:
        sources.append(
            (
                "USA",
                us.pivot_table(index="year", columns="category", values="value"),
                "all",
                {k: k for k in US_CPI_PARTS if k != "all"},
                "BLS CPI (FRED)",
            )
        )
    for iso, t, all_code, cmap, src in sources:
        if all_code not in t:
            continue
        t = t.dropna(subset=[all_code])
        if t.empty:
            continue
        y1 = int(t.index.max())
        y0 = y1 - PRICE_TREND_YEARS
        if y0 not in t.index:
            continue
        base = (t.loc[y1, all_code] / t.loc[y0, all_code]) ** (1 / PRICE_TREND_YEARS)
        for key, code in cmap.items():
            if code not in t or pd.isna(t.loc[y0, code]) or pd.isna(t.loc[y1, code]):
                continue
            g = (t.loc[y1, code] / t.loc[y0, code]) ** (1 / PRICE_TREND_YEARS)
            rows.append(
                {
                    "iso3": iso,
                    "item": f"rel:{key}",
                    "value": round(100 * (g / base - 1), 3),
                    "period": f"{y0}-{y1}",
                    "source": f"{src} {code} vs all items",
                }
            )
    io = long[long["measure"] == "inflation_outlook"].dropna(subset=["value"])
    for stat, item in (("breakeven_5y", "inflation_5y"), ("forward_5y5y", "inflation_5y5y")):
        s_ = io[io["statistic"] == stat].sort_values("period").tail(3)
        if not s_.empty:
            rows.append(
                {
                    "iso3": "USA",
                    "item": item,
                    "value": round(float(s_["value"].mean()), 3),
                    "period": str(s_["period"].iloc[-1]),
                    "source": f"{s_['series'].iloc[-1]} (last 3 months)",
                }
            )
    hc = long[(long["measure"] == "price_level") & (long["statistic"] == "household_cost")].dropna(
        subset=["value"]
    )
    for r in hc.itertuples(index=False):  # items hci_12m, cpi_5y, ...
        rows.append(
            {
                "iso3": r.iso3,
                "item": r.category,
                "value": round(float(r.value), 3),
                "period": str(r.period),
                "source": str(r.method),
            }
        )
    return pd.DataFrame(rows)

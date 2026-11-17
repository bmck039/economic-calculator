"""Derived tax tables: the tax curve and US citizens abroad."""

from __future__ import annotations

import re

import pandas as pd

from econdata.config import COUNTRIES, LOCAL_CURRENCY
from econdata.derive.context import Ctx
from econdata.tidy import latest


# ---------- Expat tax comparison ------------------------------------------
def derive_expat_tax(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    tw = long[(long["source"] == "OECD Taxing Wages")]
    out = []
    if not tw.empty:
        # average personal tax rate measures: pick codes that look like average tax/wedge
        keep = tw[tw["statistic"].astype(str).str.contains("AV_|NPAV|TW", regex=True)]
        keep = latest(keep, ["iso3", "statistic", "household_type"])
        us = keep[keep["iso3"] == "USA"].set_index(["statistic", "household_type"])["value"]
        for r in keep.itertuples():
            usv = us.get((r.statistic, r.household_type))
            out.append(
                {
                    "iso3": r.iso3,
                    "country": COUNTRIES.get(r.iso3, ("", "", ""))[2],
                    "measure": r.statistic,
                    "household_type": r.household_type,
                    "year": r.year,
                    "local_value": r.value,
                    "usa_value": usv,
                    "local_minus_usa": (r.value - usv) if usv is not None else None,
                    "flag": (
                        "local burden below US - residual US tax possible "
                        "(FEIE/FTC may not fully offset)"
                    )
                    if usv is not None and r.value < usv
                    else "",
                }
            )
    df = pd.DataFrame(out)
    refs = ctx.refs[ctx.refs["topic"] == "expat_tax"].copy()
    tot = refs[refs["item"] == "us_totalization_agreement"]["iso3"].tolist()
    if not df.empty:
        df["us_totalization_agreement"] = df["iso3"].isin(tot)
    return df


ECASE_RX = re.compile(r"P1_NCH_AW(\d+)$")


def derive_tax_curve(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Average income tax and employee social-security rates for a SINGLE person
    without children at several multiples of the national average wage (AW), plus
    the marginal rate at the top point (for extrapolation) and the net cash value of
    two children for a couple. Eurostat net earnings (6 points, 50-167% AW) first;
    OECD Taxing Wages (67/100/167% AW) for countries Eurostat lacks."""
    rows = []
    ne = ctx.measure("net_income_tax_model")
    eu = ne[ne["source"] == "Eurostat net earnings"]
    tw = ne[ne["source"] == "OECD Taxing Wages"]
    for iso3 in COUNTRIES:
        g = eu[(eu["iso3"] == iso3)]
        pts = []
        if not g.empty:
            nac = g[g["unit"].astype(str).str.startswith("NAC")]
            g = nac if not nac.empty else g
            y = int(g["year"].max())
            g = g[g["year"] == y]
            for case, gc in g.groupby("series"):
                m = ECASE_RX.search(str(case))
                if not m:
                    continue
                v = gc.set_index("statistic")["value"]
                if not v.get("GRS"):
                    continue
                grs = ctx.to_local(v["GRS"], str(gc["unit"].iloc[0]), iso3, y)
                pts.append(
                    {
                        "aw_multiple": int(m.group(1)) / 100,
                        "gross_local": grs,
                        "income_tax_rate": v.get("TAX", 0) / v["GRS"],
                        "employee_ssc_rate": v.get("SOC", 0) / v["GRS"],
                        "year": y,
                        "source": "Eurostat net earnings (OECD-EC model)",
                    }
                )
        t = tw[(tw["iso3"] == iso3) & tw["household_type"].astype(str).str.startswith("S_C0")]
        stale = bool(pts) and not t.empty and pts[0]["year"] < int(t["year"].max()) - 1
        if len(pts) < 3 or stale:  # e.g. UK: Eurostat stops in 2019, OECD has 2025
            if not t.empty:
                y = int(t["year"].max())
                t = t[t["year"] == y]
                pts = []
                for hh, gt in t.groupby("household_type"):
                    lvl = re.search(r"AW(\d+)", hh)
                    v = {(r.statistic, r.unit): r.value for r in gt.itertuples()}
                    gross = v.get(("GEBT", "XDC"))
                    if not lvl or gross is None:
                        continue
                    pts.append(
                        {
                            "aw_multiple": int(lvl.group(1)) / 100,
                            "gross_local": gross,
                            "income_tax_rate": v.get(("AV_ITR", "PT_WG_EARN_G"), 0) / 100,
                            "employee_ssc_rate": v.get(("AV_R_EMPEE_SSC", "PT_WG_EARN_G"), 0)
                            / 100,
                            "year": y,
                            "source": "OECD Taxing Wages",
                        }
                    )
        if not pts:
            continue
        aw = (
            next((p["gross_local"] for p in pts if p["aw_multiple"] == 1.0), None)
            or pts[0]["gross_local"] / pts[0]["aw_multiple"]
        )
        # marginal net personal tax rate at 167% AW (OECD) for extrapolation
        t = tw[
            (tw["iso3"] == iso3)
            & (tw["statistic"] == "NPMTR_PE")
            & tw["household_type"].astype(str).str.startswith("S_C0|AW167")
        ]
        mtr = float(t.sort_values("year")["value"].iloc[-1]) / 100 if not t.empty else None
        # value of two children for a two-earner couple (100% + 67% AW): net income
        # with 2 kids minus without, as a share of AW (cash benefits + tax credits)
        c2 = tw[(tw["iso3"] == iso3) & (tw["statistic"] == "NIAT") & (tw["unit"] == "XDC")]
        kid = None
        if not c2.empty:
            yy = int(c2["year"].max())
            c2 = c2[c2["year"] == yy].set_index("household_type")["value"]
            a, b = c2.get("C_C2|AW100+AW67"), c2.get("C_C0|AW100+AW67")
            if a is not None and b is not None:
                kid = (a - b) / 2 / aw
        if kid is None:  # Eurostat: family allowances, couple 100%+67% with 2 children
            fam = eu[
                (eu["iso3"] == iso3)
                & (eu["statistic"] == "FAM")
                & eu["series"].astype(str).str.endswith("CPL_CH2_AW100_67")
            ]
            if not fam.empty:
                fam = fam.sort_values("year").iloc[-1]
                kid = ctx.to_local(fam["value"], fam["unit"], iso3, int(fam["year"])) / 2 / aw
        for p in sorted(pts, key=lambda p: p["aw_multiple"]):
            rows.append(
                {
                    "iso3": iso3,
                    **p,
                    "avg_wage_local": aw,
                    "total_rate": p["income_tax_rate"] + p["employee_ssc_rate"],
                    "marginal_rate_top": mtr,
                    "child_net_benefit_per_child_frac_aw": kid,
                    "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                    "estimated": False,
                    "quality": "A",
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty and "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        lie = df[df["iso3"] == "CHE"].copy()
        lie["iso3"], lie["estimated"], lie["quality"] = "LIE", True, "C"
        lie["source"] = "ESTIMATE: Swiss tax curve used for Liechtenstein"
        df = pd.concat([df, lie], ignore_index=True)
    return df

"""Derived earnings tables: nowcasts, PPP, real wages, profiles, pay by
degree, mobility, US careers."""

from __future__ import annotations

import math
import re

import pandas as pd

from econdata.config import COUNTRIES, EUROPE, LOCAL_CURRENCY, NOTES
from econdata.derive.context import PER_YEAR, Ctx
from econdata.derive.labour import _LM
from econdata.tidy import TIDY_COLS

# Bulgaria joined 1 Jan 2026; World Bank may still report BGN for earlier years, so
# it is handled through the exchange-rate ratio rather than assumed to be 1.


# ---------- SES nowcast ---------------------------------------------------
def derive_ses_nowcast(long: pd.DataFrame) -> pd.DataFrame:
    ses = long[(long["source"] == "Eurostat SES") & (long["unit"] == "EUR/month")]
    if ses.empty:
        return pd.DataFrame(columns=TIDY_COLS)
    nama = long[(long["series"] == "nama_10_fte") & (long["unit"] == "EUR/year")]
    g = {(r.iso3, int(r.year)): r.value for r in nama.itertuples()}
    avw = long[(long["measure"] == "avg_wage") & (long["source"] == "OECD average wages")]
    avw = avw[avw["unit"].astype(str).str.match(r"^[A-Z]{3}\|V$", na=False)]
    g_oecd = {(r.iso3, int(r.year)): r.value for r in avw.itertuples()}
    out = []
    for iso3, grp in ses.groupby("iso3"):
        base_year = int(grp["year"].max())
        base = grp[grp["year"] == base_year]
        years = sorted({y for (c, y) in g if c == iso3 and y > base_year})
        src = "nama_10_fte (EUR)"
        table = g
        if not years:
            years = sorted({y for (c, y) in g_oecd if c == iso3 and y > base_year})
            src, table = "OECD average wage (national currency, current)", g_oecd
        if not years or (iso3, base_year) not in table:
            continue
        for y in years:
            factor = table[(iso3, y)] / table[(iso3, base_year)]
            rows = base.copy()
            rows["value"] = rows["value"] * factor
            rows["period"] = str(y)
            rows["year"] = y
            rows["estimated"] = True
            rows["method"] = f"SES {base_year} x growth of {src} {base_year}->{y} ({factor:.3f})"
            rows["source"] = "Eurostat SES (nowcast)"
            rows["comparability_note"] = NOTES["ses_nowcast"]
            out.append(rows)
    return pd.concat(out)[TIDY_COLS] if out else pd.DataFrame(columns=TIDY_COLS)


# ---------- Earnings in international $ -----------------------------------
def derive_earnings_ppp(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    e = long[
        (long["measure"] == "earnings_by_age") & ~long["unit"].astype(str).str.startswith("PPS")
    ]
    out = []
    for r in e.itertuples():
        y = int(r.year)
        cur, per = str(r.unit).split("/")
        local = ctx.to_local(r.value * PER_YEAR[per], r.unit, r.iso3, y)
        if local is None:
            continue
        intl, py = ctx.intl(local, r.iso3, y)
        if intl is None:
            continue
        out.append(
            {
                **{c: getattr(r, c) for c in TIDY_COLS},
                "annual_local": local,
                "ppp_year_used": py,
                "annual_intl_dollars": intl,
            }
        )
    return pd.DataFrame(out)


# ---------- Real wage history (stagnation) --------------------------------
def derive_real_wages(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Nominal -> real (latest-year prices) with CPI, indexed to the first year, CAGR.
    Vectorised: the old per-group loop over ~8k groups took most of a minute."""
    u = long["unit"].astype(str)
    sel = long[
        (
            (long["measure"] == "earnings_by_age")
            & (long["statistic"] == "median")
            & ~u.str.startswith("PPS")
        )
        | (
            (long["measure"] == "household_income")
            & (long["statistic"] == "median")
            & u.str.startswith(("NAC", "XDC", "EUR"))
        )
        |
        # OECD units are ISO codes: 'USD|V' = current prices (|Q = constant,
        # USD_PPP = converted); Eurostat 'NAC/year' or 'EUR/year'
        ((long["measure"] == "avg_wage") & u.str.match(r"^([A-Z]{3}\|V|NAC/|EUR/)", na=False))
    ]
    keys = ["iso3", "measure", "source", "age_band", "household_type", "area_type", "unit"]
    d = sel[keys + ["year", "value", "estimated"]].dropna(subset=["year"]).copy()
    for k in keys:  # pandas 3: astype(str) keeps <NA>, and groupby would DROP those rows
        d[k] = d[k].fillna("").astype(str)
    d["year"] = d["year"].astype(int)
    d = d.sort_values(keys + ["year"]).drop_duplicates(keys + ["year"], keep="last")
    d = d[d["iso3"] != "_"]
    eur = d["unit"].str.split("/").str[0].str.split(" ").str[0] == "EUR"
    fx = [ctx.lcu_per_eur(c, y) if e else 1.0 for c, y, e in zip(d["iso3"], d["year"], eur)]
    d["local"] = d["value"] * pd.to_numeric(pd.Series(fx, index=d.index), errors="coerce")
    d["cpi"] = [ctx.cpi(c, y) for c, y in zip(d["iso3"], d["year"])]
    ref = {c: ctx.wb("cpi_index", c, ctx.latest_year) for c in d["iso3"].unique()}
    d["cpi_ref"] = d["iso3"].map(lambda c: ref[c][0])
    d["price_base_year"] = d["iso3"].map(lambda c: ref[c][1])
    d = d.dropna(subset=["local", "cpi", "cpi_ref"])
    d["real_local"] = d["local"] * d["cpi_ref"] / d["cpi"]
    ppp = {c: ctx.intl(1.0, c, ref[c][1])[0] for c in d["iso3"].unique() if ref[c][1]}
    d["real_intl_dollars"] = d["real_local"] * pd.to_numeric(d["iso3"].map(ppp), errors="coerce")
    g = d.groupby(keys, sort=False)
    d["_n"] = g["year"].transform("size")
    d = d[d["_n"] >= 2]
    g = d.groupby(keys, sort=False)
    first_real, first_year = g["real_local"].transform("first"), g["year"].transform("first")
    d["index_first_year_100"] = d["real_local"] / first_real * 100
    last = g.tail(1).copy()
    span = last["year"] - first_year.loc[last.index]
    last["real_cagr_pct_since_first_year"] = (
        (last["real_local"] / first_real.loc[last.index]) ** (1 / span.where(span > 0)) - 1
    ) * 100
    # 10-year CAGR: latest point vs the last point at least 10 years earlier
    cagr10 = {}
    for idx, r in last.iterrows():
        k = tuple(r[keys])
        h = d.loc[g.groups[k]]
        old = h[h["year"] <= r["year"] - 10]
        if not old.empty:
            o = old.iloc[-1]
            cagr10[idx] = (
                (r["real_local"] / o["real_local"]) ** (1 / (r["year"] - o["year"])) - 1
            ) * 100
    last["real_cagr_pct_last_10y"] = pd.Series(cagr10)
    d = d.join(last[["real_cagr_pct_since_first_year", "real_cagr_pct_last_10y"]])
    out = d.rename(columns={"unit": "unit_nominal", "value": "nominal"})
    out["country"] = out["iso3"].map(lambda c: COUNTRIES.get(c, ("", "", c))[2])
    cols = [
        "iso3",
        "country",
        "measure",
        "source",
        "age_band",
        "household_type",
        "area_type",
        "unit_nominal",
        "year",
        "nominal",
        "price_base_year",
        "real_local",
        "real_intl_dollars",
        "index_first_year_100",
        "estimated",
        "real_cagr_pct_since_first_year",
        "real_cagr_pct_last_10y",
    ]
    return out[cols].reset_index(drop=True)


# ---------- Mobility summary -------------------------------------------------
def derive_mobility(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    out = []
    e = long[
        (long["measure"] == "earnings_by_age")
        & (long["statistic"] == "median")
        & ~long["estimated"].fillna(False).astype(bool)
    ]
    # (1) cross-sectional age-earnings profile: each band / earliest band, same year
    for (iso3, src, unit, y), g in e.groupby(["iso3", "source", "unit", "year"]):
        g = g[g["career_stage"].isin(["early", "mid", "late"])]
        if g["career_stage"].nunique() < 2:
            continue
        early = g[g["career_stage"] == "early"]["value"].max()
        for r in g.itertuples():
            out.append(
                {
                    "iso3": iso3,
                    "metric": "age_profile_vs_youngest",
                    "detail": f"{r.age_band} / youngest band",
                    "year": y,
                    "value": r.value / early if early else None,
                    "source": src,
                    "note": "cross-section: today's older workers vs today's young",
                }
            )
    # (2) pseudo-cohort real growth across SES waves: <30 in wave t -> 30-39 in t+8
    ses = e[(e["source"] == "Eurostat SES") & (e["unit"] == "EUR/month")]
    for iso3, g in ses.groupby("iso3"):
        for y in sorted(g["year"].unique()):
            a = g[(g["year"] == y) & (g["age_band"] == "Y_LT30")]
            b = g[(g["year"] == y + 8) & (g["age_band"] == "Y30-39")]
            if a.empty or b.empty:
                continue
            va = ctx.to_local(a["value"].iloc[0], "EUR", iso3, int(y))
            vb = ctx.to_local(b["value"].iloc[0], "EUR", iso3, int(y) + 8)
            c0, c1 = ctx.cpi(iso3, int(y)), ctx.cpi(iso3, int(y) + 8)
            if va and vb and c0 and c1:
                g_real = (vb / c1) / (va / c0)
                out.append(
                    {
                        "iso3": iso3,
                        "metric": "pseudo_cohort_real_growth_8y",
                        "detail": f"median <30 in {y} -> median 30-39 in {y + 8}",
                        "year": int(y) + 8,
                        "value": g_real,
                        "source": "Eurostat SES + World Bank CPI",
                        "note": "x-fold real growth of a cohort's median; "
                        f"annualised {(g_real ** (1 / 8) - 1) * 100:.1f}%/yr",
                    }
                )
    # (3) Atlanta Fed: stayer vs switcher premium (annual means)
    w = long[long["measure"] == "wage_growth_individual"]
    if not w.empty:
        wa = w.groupby(["statistic", "year"])["value"].mean().unstack(0)
        for y, r in wa.iterrows():
            for col in wa.columns:
                out.append(
                    {
                        "iso3": "USA",
                        "metric": "wage_growth_same_person",
                        "detail": col,
                        "year": int(y),
                        "value": r[col],
                        "source": "Atlanta Fed WGT",
                        "note": "% per year, median",
                    }
                )
    # (4) EU-SILC: share moving up >= 1 pay decile per year
    t = long[long["measure"] == "income_transitions"]
    t = t[t["category"].astype(str).str.contains("total", case=False, na=False)]
    for (iso3, stat), g in t.groupby(["iso3", "statistic"]):
        r = g.sort_values("year").iloc[-1]
        out.append(
            {
                "iso3": iso3,
                "metric": "pay_decile_transition",
                "detail": stat,
                "year": int(r["year"]),
                "value": r["value"],
                "source": r["source"],
                "note": "% of employees, year to year",
            }
        )
    return pd.DataFrame(out)


# ---------- Simulation inputs ------------------------------------------------
# Compact tables the simulator/UI read, so it never has to load econ_long.
Z = {"p10": -1.2816, "p25": -0.6745, "p75": 0.6745, "p90": 1.2816}


EARN_PREF = {"GBR": "ONS ASHE", "USA": "BLS CPS (via FRED)", "CAN": "StatCan LFS"}


def age_span(band: str) -> tuple[float, float] | None:
    """(low, high) working ages covered by an earnings band; None for all-ages totals.
    Open-ended bands are capped at 64; '<30' starts at 20 (full-time workers)."""
    b = str(band).strip()
    if b.upper() == "TOTAL" or b.lower() == "total":
        return None
    if (
        m := re.fullmatch(r"Y(\d{2})-(\d{2})", b)
        or re.fullmatch(r"(\d{2})-(\d{2})", b)
        or re.fullmatch(r"(\d{2}) to (\d{2}) years", b)
    ):
        return float(m.group(1)), float(m.group(2))
    if m := re.fullmatch(r"Y_LT(\d{2})", b):
        return 20.0, float(m.group(1)) - 1
    if (
        m := re.fullmatch(r"Y_GE(\d{2})", b)
        or re.fullmatch(r"(\d{2})\+", b)
        or re.fullmatch(r"(\d{2}) years and over", b)
    ):
        lo = float(m.group(1))
        return None if lo <= 16 else (lo, max(lo + 4, 64.0))
    return None


def age_mid(band: str) -> float | None:
    s = age_span(band)
    return None if s is None else (s[0] + s[1]) / 2


def derive_earnings_profile(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Annual gross full-time earnings by age band and percentile (p10..p90), local
    currency, latest year, one source per country. Missing percentiles are ESTIMATED:
    SES has p10/p90 -> p25/p75 by log-normal interpolation; CPS has p25/p75 only for
    all ages -> that ratio applied to each age median; LFS (Canada) has medians only ->
    US ratios. LIE has no survey -> Swiss profile (estimate)."""
    e = ctx.measure("earnings_by_age")
    e = e[~e["unit"].astype(str).str.startswith("PPS")]
    out = []
    for iso3 in COUNTRIES:
        g = e[e["iso3"] == iso3]
        pref = EARN_PREF.get(iso3)
        if pref:
            g = g[g["source"] == pref]
        else:
            g = g[g["source"].astype(str).str.startswith("Eurostat SES")]
        if g.empty:
            continue
        y = int(g["year"].max())
        full = g
        g = g[g["year"] == y]
        src = g["source"].iloc[0]
        est_all = bool(g["estimated"].any())
        if not pref and g["age_band"].map(age_span).notna().sum() == 0:
            # SES 2022 (and its nowcast) has only the TOTAL for most countries in
            # earn_ses_monthly: take the AGE SHAPE (band / total, per statistic) from the
            # latest wave with >= 4 age bands and apply it to the latest level.
            ses = full[full["source"] == "Eurostat SES"]

            def n_bands(w):
                b = ses[ses["year"] == w]["age_band"]
                return b[b.map(age_span).notna()].nunique()

            waves = [w for w in sorted(ses["year"].unique(), reverse=True) if n_bands(w) >= 4]
            if waves:
                w = int(waves[0])
                sw = ses[ses["year"] == w]
                tot_w = sw[sw["age_band"] == "TOTAL"].set_index(["statistic", "unit"])["value"]
                tot_y = g[g["age_band"] == "TOTAL"].set_index(["statistic", "unit"])["value"]
                rows = []
                for r in sw[sw["age_band"] != "TOTAL"].itertuples():
                    k = (r.statistic, r.unit)
                    if k in tot_w.index and k in tot_y.index and tot_w[k]:
                        rows.append(
                            {
                                **{c: getattr(r, c) for c in g.columns if hasattr(r, c)},
                                "value": r.value / tot_w[k] * tot_y[k],
                                "year": y,
                                "estimated": True,
                            }
                        )
                if rows:
                    g = pd.concat([g, pd.DataFrame(rows)], ignore_index=True)
                    src = f"{src}; age shape from SES {w}"
                    est_all = True
        for band, gb in g.groupby("age_band"):
            st = {r.statistic: r for r in gb.itertuples()}
            if "median" not in st:
                continue
            unit = str(st["median"].unit)
            cur, per = unit.split("/")
            conv = lambda v: ctx.to_local(v * PER_YEAR[per], unit, iso3, y)  # noqa: E731
            med = conv(st["median"].value)
            if med is None:
                continue
            vals = {"median": (med, est_all, "")}
            for p in ("p10", "p25", "p75", "p90"):
                if p in st:
                    vals[p] = (conv(st[p].value), est_all, "")
            if "p25" not in vals and "p10" in vals:
                sig = (math.log(med) - math.log(vals["p10"][0])) / -Z["p10"]
                vals["p25"] = (
                    med * math.exp(Z["p25"] * sig),
                    True,
                    "log-normal interpolation from p10/median",
                )
            if "p75" not in vals and "p90" in vals:
                sig = (math.log(vals["p90"][0]) - math.log(med)) / Z["p90"]
                vals["p75"] = (
                    med * math.exp(Z["p75"] * sig),
                    True,
                    "log-normal interpolation from median/p90",
                )
            bf = _ses_bonus_factor(ctx, iso3) if not pref else {}
            for p, (v, est, how) in vals.items():
                if p in bf or "median" in bf:
                    k = bf.get(p, bf.get("median"))
                    v = v * k[0]
                    how = (
                        how + "; " if how else f"{per}ly x {PER_YEAR[per]}; "
                    ) + f"x {k[0]:.3f} annual/(Oct monthly x 12) bonus factor, SES {k[1]}"
                span = age_span(band)
                out.append(
                    {
                        "iso3": iso3,
                        "age_band": band,
                        "age_mid": age_mid(band),
                        "age_width": None if span is None else span[1] - span[0],
                        "statistic": p,
                        "annual_gross_local": v,
                        "currency": LOCAL_CURRENCY.get(iso3, "EUR"),
                        "year": y,
                        "source": src,
                        "estimated": est,
                        "method": how
                        or f"{per}ly x {PER_YEAR[per]}" + (" (SES nowcast)" if est_all else ""),
                        "quality": "C" if est and how else ("B" if est else "A"),
                    }
                )
    df = pd.DataFrame(out)
    if df.empty:
        return df
    # fill p25/p75 where only medians exist, using an all-ages ratio
    ratio_src = {}
    for iso3, g in df.groupby("iso3"):
        tot = g[g["age_mid"].isna()].set_index("statistic")["annual_gross_local"]
        if {"p25", "median", "p75"} <= set(tot.index):
            ratio_src[iso3] = (tot["p25"] / tot["median"], tot["p75"] / tot["median"])
    extra = []
    for iso3, g in df.groupby("iso3"):
        r = ratio_src.get(iso3) or ratio_src.get("USA")
        if r is None:
            continue
        donor = iso3 if iso3 in ratio_src else "USA"
        for band, gb in g.groupby("age_band"):
            have = set(gb["statistic"])
            med = gb[gb["statistic"] == "median"].iloc[0]
            for p, k in (("p25", 0), ("p75", 1)):
                if p not in have:
                    extra.append(
                        {
                            **med.to_dict(),
                            "statistic": p,
                            "annual_gross_local": med["annual_gross_local"] * r[k],
                            "estimated": True,
                            "quality": "C",
                            "method": f"ESTIMATE: age median x all-ages {p}/median "
                            f"ratio of {donor} ({r[k]:.2f})",
                        }
                    )
    df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    if "LIE" not in set(df["iso3"]) and "CHE" in set(df["iso3"]):
        lie = df[df["iso3"] == "CHE"].copy()
        lie["iso3"], lie["estimated"], lie["quality"] = "LIE", True, "C"
        lie["method"] = "ESTIMATE: Swiss earnings profile used for Liechtenstein"
        lie["currency"] = "CHF"
        df = pd.concat([df, lie], ignore_index=True)
    df["country"] = df["iso3"].map(lambda c: COUNTRIES[c][2])
    return df


def _ses_bonus_factor(ctx: Ctx, iso3: str) -> dict[str, tuple[float, int]]:
    """statistic -> (annual / (monthly x 12), wave) for all ages, latest common wave."""
    a = ctx.mi("earnings_annual_ses", iso3)
    m = ctx.mi("earnings_by_age", iso3)
    m = m[
        (m["source"] == "Eurostat SES") & (m["unit"] == "EUR/month") & (m["age_band"] == "TOTAL")
    ]
    a = a[a["age_band"] == "TOTAL"]
    out = {}
    for stat in ("median", "p10", "p90"):
        av = {int(r.year): r.value for r in a[a["statistic"] == stat].itertuples()}
        mv = {int(r.year): r.value for r in m[m["statistic"] == stat].itertuples()}
        common = sorted(set(av) & set(mv))
        if common:
            w = common[-1]
            k = av[w] / (mv[w] * 12)
            if 0.9 < k < 1.6:  # guard against unit / coverage mismatches
                out[stat] = (k, w)
    if "median" in out:  # interpolated percentiles use the neighbouring factor
        out.setdefault(
            "p25", ((out["median"][0] + out.get("p10", out["median"])[0]) / 2, out["median"][1])
        )
        out.setdefault(
            "p75", ((out["median"][0] + out.get("p90", out["median"])[0]) / 2, out["median"][1])
        )
    return out


PAY_BANDS = ["25-34", "35-44", "45-54", "55-64"]


PAY_LEVELS = [
    "below_upper_secondary",
    "upper_secondary",
    "short_tertiary",
    "bachelor",
    "master_plus",
]


def derive_education_pay(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Full-time earnings by education relative to all education levels, by age band:
    OECD EAG relative earnings (upper secondary = 100), averaged over the latest 3 years,
    divided by their employment-weighted mean (attainment share x employment rate)."""
    lm = _LM(long)
    rows = []
    for iso in COUNTRIES:
        for band in PAY_BANDS:
            rel, w = {}, {}
            for e in PAY_LEVELS + ["tertiary"]:
                ys = lm.years(iso, "DF_LSO_EARN_REL_UPPER", "index_upper_secondary_100", band, e)
                if not ys:
                    continue
                vals = [
                    lm.get(iso, "DF_LSO_EARN_REL_UPPER", "index_upper_secondary_100", band, e, y)[
                        0
                    ]
                    for y in ys[-3:]
                ]
                rel[e] = (sum(vals) / len(vals), ys[-1])
                sh = lm.get(iso, "DF_LSO_NEAC_DISTR_EA", "share", band, e)[0]
                em = lm.get(iso, "DF_LSO_NEAC_EMP", "rate", band, e)[0]
                if sh is not None and em is not None:
                    w[e] = sh * em / 100
            for e in ("short_tertiary", "bachelor", "master_plus"):
                if e not in rel and "tertiary" in rel:
                    rel[e] = rel["tertiary"]
            lv = [e for e in PAY_LEVELS if e in rel and e in w]
            if len(lv) < 3 or sum(w[e] for e in lv) <= 0:
                continue
            mean = sum(rel[e][0] * w[e] for e in lv) / sum(w[e] for e in lv)
            for e, (v, y) in rel.items():
                rows.append(
                    {
                        "iso3": iso,
                        "education": e,
                        "age_band": band,
                        "factor": round(v / mean, 3),
                        "year": y,
                        "source": "OECD Education at a Glance (relative earnings, "
                        "full-time full-year)",
                        "estimated": False,
                        "method": "3-year average / employment-weighted mean of all levels",
                    }
                )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    eu = df[df["iso3"].isin([c for c in EUROPE if c != "GBR"])]
    med = eu.groupby(["education", "age_band"], as_index=False)["factor"].median()
    extra = []
    for iso in COUNTRIES:
        if iso in set(df["iso3"]):
            continue
        base = df[df["iso3"] == "CHE"] if iso == "LIE" else med
        note = (
            "proxy: Switzerland"
            if iso == "LIE"
            else "ESTIMATE: median of European countries (no OECD data)"
        )
        for r in base.itertuples(index=False):
            extra.append(
                {
                    "iso3": iso,
                    "education": r.education,
                    "age_band": r.age_band,
                    "factor": r.factor,
                    "year": getattr(r, "year", None),
                    "source": "OECD Education at a Glance",
                    "estimated": True,
                    "method": note,
                }
            )
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True)


def derive_us_career(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """us_career.csv: one row per number - kind (share / earn / trans / growth / cpi),
    window (current = last 12 months, prior = the 12 before), age band, education,
    situation (and the situation a year later, for moves), pay quantile."""
    c = long[long["measure"] == "us_career"].dropna(subset=["value"])
    rows = []
    for r in c.itertuples(index=False):
        stat = str(r.statistic)
        window = "current"
        if stat.startswith("prior:"):
            window, stat = "prior", stat[6:]
        name, _, st = stat.partition("|")
        kind, q, s0, s1 = name, None, st or None, None
        if name.startswith("earn_q"):
            kind, q = "earn", int(name[6:])
        elif name == "trans":
            s0, s1 = st.split(">")
        elif name == "pay_growth_median":
            kind = "growth"
        elif name == "cpi_yoy":
            kind = "cpi"
        elif name == "agg_wage_yoy":
            kind = "agg_growth"
        m = re.search(r"n=(\d+); (\S+)\.\.(\S+)", str(r.method))
        rows.append(
            {
                "kind": kind,
                "window": window,
                "age_band": r.age_band,
                "education": r.category,
                "state": s0,
                "to_state": s1,
                "q": q,
                "value": float(r.value),
                "n": int(m.group(1)) if m else None,
                "from": m.group(2) if m else None,
                "to": m.group(3) if m else None,
            }
        )
    return pd.DataFrame(rows)

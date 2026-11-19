"""Where the renting assumptions come from (econsim.assumptions, "renting"): US microdata.

    python -m tools.rent_research          (needs CENSUS_API_KEY; downloads ~150 MB once)

- How often renting couples move, by age, and why: CPS ASEC 2023-2025 (married renters
  who lived in another home a year earlier; the main reason they give).
- What sitting tenants pay vs a new lease (the tenure discount and the new-lease premium),
  how rent rises with income when people move and with children: Census ACS PUMS 2013,
  2018, 2023, married couples who rent, within the same area (PUMA fixed effects).
  Income on one year is noisy, so the permanent-income elasticity uses the householder's
  education x age as instruments.
- The same birth years followed 2013 -> 2018 -> 2023: their median rent against the rent
  index (CPI rent of primary residence) and their median income, as a check of the rule
  on history (compare with the model run through the same years).

Every country (written to econsim/params/renting.csv, `--write`):

- How rent follows income: tenants' median rent burden by income quintile (OECD
  Affordable Housing Database HC1.2.A2, from EU-SILC, the ACS, ...; 2015-2024 pooled) and
  each quintile's share of income (Eurostat ilc_di01; the US from the ACS, computed the
  same way) give how rent rises across the quintiles. That also picks up richer
  households living in dearer places, so each country is scaled by the US: elasticity =
  US elasticity x country / US on this measure.
- How often renters move: the share of tenants at market rent who moved in the last five
  years (EU-SILC 2012, ilc_hcmp05; the US from the ACS 2013 the same way), as a yearly
  hazard relative to the US: the US moves by age are scaled by it.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from econdata.clients import eurostat  # noqa: E402
from econdata.config import COUNTRIES, EUROSTAT_TO_ISO3  # noqa: E402
from econdata.fetch import CachedFetcher, is_zip  # noqa: E402

PUMS = "https://api.census.gov/data/{y}/acs/acs1/pums"
ASEC = "https://api.census.gov/data/{y}/cps/asec/mar"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
OECD_HC12 = (
    "https://webfs.oecd.org/els-com/Affordable_Housing_Database/"
    "HC1-2-Housing-costs-over-income.xlsx"
)
OUT = ROOT / "econsim" / "params" / "renting.csv"
US_ELASTICITY = 0.4  # econsim.assumptions.RENT_ELASTICITY: the US, on permanent income
QUINTILES = ["2nd quintile", "3rd quintile", "4th quintile", "top quintile"]  # bottom:
#   housing benefits and subsidised rents
OECD_NAMES = {"Slovak Republic": "SVK", "Türkiye": "TUR", "United States": "USA"}
AGES = [17, 24, 29, 34, 39, 44, 54, 64, 79]
REASONS = {  # CPS NXTRES -> group
    "family": (1, 2, 3, 4),
    "job": (5, 6, 7, 9),
    "better or bigger home / area": (10, 11, 12),
    "cheaper home / eviction": (13, 14),
}


def census(f: CachedFetcher, url: str, params: dict, label: str) -> pd.DataFrame:
    p = dict(params, key=os.environ["CENSUS_API_KEY"])
    js = json.loads(f.get(url, p, label=label, timeout=300))
    d = pd.DataFrame(js[1:], columns=js[0]).loc[:, lambda x: ~x.columns.duplicated()]
    for c in d.columns:
        if c not in ("PUMA", "state"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def pums(f: CachedFetcher, y: int, cols: str, tenure: int, **pred) -> pd.DataFrame:
    d = census(
        f,
        PUMS.format(y=y),
        {"get": cols, "TEN": tenure, "SPORDER": 1, "for": "state:*", **pred},
        f"acs-pums:{y}:{tenure}:{cols}",
    )
    d["area"] = d["state"] + "-" + d["PUMA"]
    d["inc"] = d["HINCP"] * d["ADJINC"] / (1e6 if d["ADJINC"].max() > 100 else 1)
    return d[(d["HHT"] == 1) & (d["inc"] > 5000)].copy()  # married couples


def _demean(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    w, g = df["WGTP"].astype(float), df["area"]
    sw = w.groupby(g).transform("sum")
    return pd.DataFrame(
        {c: df[c] - (df[c] * w).groupby(g).transform("sum") / sw for c in cols}, index=df.index
    )


def fe_ols(df: pd.DataFrame, y: str, xs: list[str]) -> dict[str, float]:
    """Weighted OLS within areas."""
    d, sw = _demean(df, [y] + xs), np.sqrt(df["WGTP"].values)
    b = np.linalg.lstsq(d[xs].values * sw[:, None], d[y].values * sw, rcond=None)[0]
    return dict(zip(xs, b))


def fe_iv(df: pd.DataFrame, y: str, x: str, zs: list[str], ctrl: list[str]) -> float:
    """2SLS within areas: the coefficient of x instrumented by zs."""
    d, sw = _demean(df, [y, x] + zs + ctrl), np.sqrt(df["WGTP"].values)
    Z = d[zs + ctrl].values
    xhat = Z @ np.linalg.lstsq(Z * sw[:, None], d[x].values * sw, rcond=None)[0]
    X = np.column_stack([xhat, d[ctrl].values])
    return float(np.linalg.lstsq(X * sw[:, None], d[y].values * sw, rcond=None)[0][0])


def wmed(x: pd.Series, w: pd.Series) -> float:
    o = np.argsort(x.values)
    c = np.cumsum(w.values[o])
    return float(x.values[o][np.searchsorted(c, c[-1] / 2)])


def moves(f: CachedFetcher) -> None:
    d = pd.concat(
        census(
            f,
            ASEC.format(y=y),
            {"get": "A_AGE,A_MARITL,H_TENURE,MIGSAME,NXTRES,A_EXPRRP,MARSUPWT", "A_AGE": "18:79"},
            f"asec-moves:{y}",
        )
        for y in (2023, 2024, 2025)
    )
    h = d[d.A_EXPRRP.isin([1, 2]) & d.A_MARITL.isin([1, 2]) & d.MIGSAME.isin([1, 2])]
    h = h[h.H_TENURE == 2].assign(moved=lambda x: x.MIGSAME == 2)
    print("\nMarried renters who moved in the last year (CPS ASEC 2023-2025), by age:")
    for band, g in h.groupby(pd.cut(h.A_AGE, AGES), observed=True):
        print(f"  {band}: {np.average(g.moved, weights=g.MARSUPWT):.1%} (n={len(g)})")
    m = h[h.moved]
    tot = m.MARSUPWT.sum()
    print("Their main reason:")
    for k, codes in REASONS.items():
        print(f"  {k}: {m[m.NXTRES.isin(codes)].MARSUPWT.sum() / tot:.0%}")


def rents(f: CachedFetcher) -> dict[int, pd.DataFrame]:
    out = {}
    print("\nMarried renters, within the same area (ACS PUMS):")
    for y in (2013, 2018, 2023):
        c = pums(f, y, "GRNTP,HINCP,MV,AGEP,NOC,HHT,BDSP,WGTP,ADJINC,PUMA", 3)
        c = c[c.GRNTP > 0]
        out[y] = c
        c["lr"], c["li"] = np.log(c.GRNTP), np.log(c.inc)
        mv = [f"mv{k}" for k in range(2, 8)]
        for k in range(2, 8):
            c[f"mv{k}"] = (c.MV == k).astype(float)
        beds = [f"b{b}" for b in (0, 1, 3, 4, 5)]
        for b in range(6):
            c[f"b{b}"] = (c.BDSP.clip(0, 5) == b).astype(float)
        c["k1"], c["k2"] = (c.NOC == 1).astype(float), (c.NOC >= 2).astype(float)
        c["age"], c["age2"] = c.AGEP, c.AGEP**2 / 100
        b = fe_ols(c, "lr", mv + ["li", "k1", "k2", "age", "age2"] + beds)
        eff = sum(c[m] * b[m] for m in mv)
        prem = np.exp(-np.average(eff, weights=c.WGTP))
        new = c[c.MV == 1]
        e = fe_ols(new, "lr", ["li", "k1", "k2", "age", "age2"])
        print(
            f"  {y}: same home 2-4 / 5-9 / 10-19 years {b['mv3']:+.3f} / {b['mv4']:+.3f} / "
            f"{b['mv5']:+.3f} (log, same bedrooms); new lease / all renters {prem:.3f}; "
            f"movers: income {e['li']:.2f}, 1 child {e['k1']:+.3f}, 2+ {e['k2']:+.3f}"
        )
    return out


def permanent_income(f: CachedFetcher) -> None:
    print("\nPermanent-income elasticity, couples who moved in the last year (education x age):")
    for y in (2018, 2023):
        for kind, col, ten in (("rent", "GRNTP", 3), ("home value", "VALP", 2)):
            d = pums(f, y, f"{col},HINCP,MV,AGEP,NOC,HHT,SCHL,WGTP,ADJINC,PUMA", ten, MV=1)
            d = d[(d[col] > 0) & d.AGEP.between(22, 64)].copy()
            d["ly"], d["li"] = np.log(d[col]), np.log(d.inc)
            d["k1"], d["k2"] = (d.NOC == 1).astype(float), (d.NOC >= 2).astype(float)
            e = pd.cut(d.SCHL, [0, 15, 17, 19, 20, 21, 22, 24], labels=False)
            ages = [(22, 29), (30, 39), (40, 49), (50, 64)]
            for k, (lo, hi) in enumerate(ages):
                d[f"a{k}"] = d.AGEP.between(lo, hi).astype(float)
            zs = []
            for k in range(1, 7):
                for a in (0, 1, 2, 3):
                    d[f"e{k}a{a}"] = ((e == k) & (d[f"a{a}"] == 1)).astype(float)
                    zs.append(f"e{k}a{a}")
            ctrl = ["k1", "k2", "a1", "a2", "a3"]
            iv = fe_iv(d, "ly", "li", zs, ctrl)
            ols = fe_ols(d, "ly", ["li"] + ctrl)["li"]
            print(f"  {y} {kind}: {iv:.2f} (one year's income: {ols:.2f}; n={len(d)})")


def cohorts(f: CachedFetcher, d: dict[int, pd.DataFrame]) -> None:
    def annual(series: str) -> pd.Series:
        s = pd.read_csv(io.BytesIO(f.get(FRED, {"id": series}, label=f"fred:{series}")))
        s.columns = ["d", "v"]
        return s.groupby(s.d.str[:4].astype(int)).v.mean()

    rent, cpi = annual("CUUR0000SEHA"), annual("CPIAUCNS")
    print("\nThe same birth years, 2013 -> 2018 -> 2023 (married renters' medians):")
    for a0 in (22, 25, 30, 35):
        lr, li = [], []
        for k, y in enumerate((2013, 2018, 2023)):
            g = d[y][d[y].AGEP.between(a0 + 5 * k, a0 + 5 * k + 4)]
            lr.append(np.log(wmed(g.GRNTP, g.WGTP) / rent[y]))
            li.append(np.log(wmed(g.inc, g.WGTP) / cpi[y]))
        dr, di = np.diff(lr), np.diff(li)
        print(
            f"  aged {a0}-{a0 + 4} in 2013: rent vs the rent index {dr.round(3)}, real income "
            f"{di.round(3)}; over 10 years {dr.sum() / di.sum():.2f} of income growth"
        )


def oecd_rent_burden(f: CachedFetcher) -> pd.DataFrame:
    """Tenants' median rent / disposable income by income quintile: iso3, year, q, burden."""
    raw = f.get(OECD_HC12, label="oecd:ahd:hc12", validate=is_zip, timeout=120)
    d = pd.read_excel(io.BytesIO(raw), sheet_name="HC12_A2", header=None)
    years, qs = d.iloc[4].ffill(), d.iloc[5]
    names = {v[2]: k for k, v in COUNTRIES.items()} | OECD_NAMES
    rows, iso = [], None
    for i in range(6, len(d)):
        lab = d.iloc[i, 0]
        if not isinstance(lab, str):
            continue
        if lab.startswith("Rent"):
            for j in range(1, d.shape[1]):
                if iso and pd.notna(d.iloc[i, j]) and isinstance(qs[j], str):
                    rows.append((iso, int(years[j]), qs[j], float(d.iloc[i, j])))
        elif not lab.startswith("Owner"):
            iso = names.get(lab.strip())
    return pd.DataFrame(rows, columns=["iso3", "year", "q", "burden"])


def quintile_shares(f: CachedFetcher) -> dict[str, np.ndarray]:
    """Shares of equivalised income of quintiles 2-5 (Eurostat; the US from the ACS)."""
    d = eurostat(
        f,
        "ilc_di01",
        {"statinfo": ["SHARE"], "unit": ["EUR"], "quant_inc": ["QU2", "QU3", "QU4", "QU5"]},
        since=2015,
    )
    d["iso3"] = d["geo"].map(EUROSTAT_TO_ISO3)
    t = d.dropna(subset=["iso3", "value"]).pivot_table(
        index="iso3", columns="quant_inc", values="value", aggfunc="mean"
    )
    out = {i: r[["QU2", "QU3", "QU4", "QU5"]].values for i, r in t.iterrows()}
    a = census(
        f,
        PUMS.format(y=2023),
        {"get": "HINCP,NP,WGTP", "SPORDER": 1, "for": "state:*"},
        "acs-pums:2023:all-incomes",
    )
    a = a[(a.NP > 0) & a.HINCP.notna()]
    e = (a.HINCP / np.sqrt(a.NP)).values.astype(float)
    w = (a.WGTP * a.NP).values.astype(float)  # persons, as Eurostat
    o = np.argsort(e)
    cw = np.cumsum(w[o]) / w.sum()
    inc, q = (e * w)[o], np.minimum((cw * 5).astype(int), 4)
    out["USA"] = np.array([inc[q == k].sum() / inc.sum() * 100 for k in range(1, 5)])
    return out


def income_slopes(f: CachedFetcher) -> pd.Series:
    """How rent rises across income quintiles 2-5: 1 + d log(rent burden) / d log income."""
    b = oecd_rent_burden(f)
    b = b[b.year.between(2015, 2024) & b.q.isin(QUINTILES)]
    shares = quintile_shares(f)
    out = {}
    for iso, g in b.groupby("iso3"):
        if iso not in shares:
            continue
        lb = g.groupby("q").burden.apply(lambda x: np.log(x).mean()).reindex(QUINTILES)
        ok = lb.notna().values & ~np.isnan(shares[iso])
        if ok.sum() >= 3:  # quintiles with >= 100 tenants (OECD)
            out[iso] = 1 + np.polyfit(np.log(shares[iso][ok]), lb.values[ok], 1)[0]
    return pd.Series(out)


def moved_5y(f: CachedFetcher) -> pd.Series:
    """% of tenants at market rent who moved in the last five years (EU-SILC 2012; the
    US: renters paying rent, ACS 2013, moved in less than five years)."""
    d = eurostat(f, "ilc_hcmp05", {"tenure": ["RENT_MKT"], "deg_urb": ["TOTAL"]})
    d["iso3"] = d["geo"].map(EUROSTAT_TO_ISO3)
    s = d.dropna(subset=["iso3", "value"]).groupby("iso3")["value"].mean()
    a = census(
        f, PUMS.format(y=2013), {"get": "GRNTP,MV,WGTP", "TEN": 3, "for": "state:*"}, "acs:mv13"
    )
    a = a[a.GRNTP > 0]
    s["USA"] = 100 * np.average(a.MV <= 3, weights=a.WGTP)
    return s


def write_params(f: CachedFetcher) -> pd.DataFrame:
    sl, mv = income_slopes(f), moved_5y(f)
    el = (US_ELASTICITY * sl / sl["USA"]).round(2)
    hz = -np.log(1 - mv / 100)  # yearly hazard x 5
    scale = (hz / hz["USA"]).round(2)
    med = float(el.drop("USA").median())
    rows = []
    for iso in COUNTRIES:
        src_e = f"OECD AHD HC1.2.A2 x Eurostat ilc_di01, {sl.get(iso, np.nan):.2f} vs US"
        e = el.get(iso)
        if (e is None or np.isnan(e)) and COUNTRIES[iso][3] not in ("EU", "EFTA", "UK"):
            e, src_e = US_ELASTICITY, "no comparable income shares: as the US"
        elif e is None or np.isnan(e):
            e, src_e = round(med, 2), "median of the countries with data (too few tenants)"
        src_e += f" {sl['USA']:.2f}"
        m = scale.get(iso if iso != "LIE" else "CHE")
        src_m = f"EU-SILC 2012 ilc_hcmp05: {mv.get(iso if iso != 'LIE' else 'CHE', 0):.0f}%"
        if m is None or np.isnan(m):
            m, src_m = 1.0, "no data: as the US"
        src_m += f" moved in 5 years vs US {mv['USA']:.0f}% (ACS 2013)"
        rows.append(
            {
                "iso3": iso,
                "rent_elasticity": US_ELASTICITY if iso == "USA" else e,
                "mover_scale": 1.0 if iso == "USA" else m,
                "quality": "B",
                "source": "ACS PUMS (tools/rent_research.py)"
                if iso == "USA"
                else f"rent vs income: {src_e}; moves: {src_m}",
            }
        )
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    print(f"\nWrote {OUT.relative_to(ROOT)}:")
    print(t.drop(columns="source").to_string(index=False))
    return t


def main() -> int:
    if not os.environ.get("CENSUS_API_KEY"):
        print("needs CENSUS_API_KEY (free: https://api.census.gov/data/key_signup.html)")
        return 2
    f = CachedFetcher(ROOT / "econ_data" / ".cache", ttl_days=3650)
    if "--write" in sys.argv:
        write_params(f)
        return 0
    moves(f)
    d = rents(f)
    permanent_income(f)
    cohorts(f, d)
    return 0


if __name__ == "__main__":
    sys.exit(main())

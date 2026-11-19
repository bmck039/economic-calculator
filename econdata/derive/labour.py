"""Derived job-market table by age and degree."""

from __future__ import annotations

import pandas as pd

from econdata.config import COUNTRIES
from econdata.derive.context import Ctx
from econdata.sources.labour import PC_ELF

# ---------- Job market by age and education ----------------------------------------
LM_BANDS = ["20-24", "25-29", "30-34", "25-64"]


LM_EDU = [
    "all",
    "below_upper_secondary",
    "upper_secondary",
    "short_tertiary",
    "bachelor",
    "master_plus",
]


TERTIARY_SUB = {"short_tertiary", "bachelor", "master_plus"}


LM_SERIES = {
    "lfsa_urgaed",
    "lfsi_sla_a",
    "lfsa_sup_edu",
    "lfsa_eoqgan",
    "DF_LSO_NEAC_UNEMP",
    "DF_INVPT_I",
    "14-10-0077-01",
    "CPS basic monthly",
    "DF_IALFS_UNE_M",
    "DF_LSO_NEAC_EMP",
    "DF_LSO_NEAC_DISTR_EA",
    "DF_LSO_EARN_REL_UPPER",
    "ONS LF8B",
}


# Functional unemployment = unemployed + part-timers who want and can work more hours +
# people who want a job and could start but are not searching (U-6-like). People who are
# searching but cannot start (Eurostat SEEK_NAVL: mostly students) are left out, as in U-6.
SLACK_EXTRA = ["uemp_pt", "nseek_avl"]


class _LM:
    """(iso3, series, statistic, age, education, unit) -> {year: value} lookups."""

    def __init__(self, long: pd.DataFrame):
        d = long[long["series"].isin(LM_SERIES)].dropna(subset=["year", "value"])
        self.s: dict[tuple, dict[str, dict[int, float]]] = {}
        self.meta: dict[tuple, tuple[bool, str]] = {}
        txt = lambda x, d="all": d if pd.isna(x) or str(x) in ("", "nan") else str(x)  # noqa
        for r in d.itertuples(index=False):
            k = (r.iso3, r.series, txt(r.statistic), txt(r.age_band), txt(r.category))
            self.s.setdefault(k, {}).setdefault(txt(r.unit, ""), {})[int(r.year)] = float(r.value)
            self.meta[k + (int(r.year),)] = (
                bool(r.estimated),
                "" if pd.isna(r.method) else str(r.method),
            )

    def get(self, iso, series, stat, age, edu="all", year=None, back=0, unit=None):
        for u, v in self.s.get((iso, series, stat, age, edu), {}).items():
            if unit is not None and u != unit:
                continue
            ys = [y for y in v if year is None or year - back <= y <= year]
            if ys:
                y = max(ys)
                return v[y], y
        return None, None

    def years(self, iso, series, stat, age, edu="all"):
        ys = set()
        for v in self.s.get((iso, series, stat, age, edu), {}).values():
            ys |= set(v)
        return sorted(ys)


def _ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _sup_edu(lm: _LM, src: str, edu: str, Y: int) -> list[float] | None:
    """[part-timers wanting more hours, available but not seeking] for ages 25-74 and an
    education level, % of its extended labour force (Eurostat publishes the components as
    counts only; extended labour force = slack count / slack %)."""
    ths = [
        lm.get(src, "lfsa_sup_edu", s, "25-74", edu, Y, unit="thousand persons")[0]
        for s in SLACK_EXTRA + ["slack"]
    ]
    pc = lm.get(src, "lfsa_sup_edu", "slack", "25-74", edu, Y, unit=PC_ELF)[0]
    if ths[-1] is None or not pc or all(t is None for t in ths[:-1]):
        return None
    elf = ths[-1] / pc * 100
    return [100 * (t or 0.0) / elf for t in ths[:-1]]


def _band_slack(lm: _LM, src: str, band: str, Y: int) -> tuple[dict | None, str]:
    """Eurostat slack beyond unemployment for an age band, % of its extended labour force:
    {'uemp_pt': part-timers wanting more hours, 'nseek_avl': available, not seeking}."""
    if band == "25-29":  # 25-29 = (15-29) - (15-24), in persons
        parts = {}
        for a in ("15-29", "15-24"):
            ths = [
                lm.get(src, "lfsi_sla_a", s, a, "all", Y, unit="thousand persons")[0]
                for s in SLACK_EXTRA + ["une"]
            ]
            pc = lm.get(src, "lfsi_sla_a", "une", a, "all", Y, unit=PC_ELF)[0]
            if ths[0] is None or ths[-1] is None or not pc:
                parts = None
                break
            parts[a] = ([t or 0.0 for t in ths], ths[-1] / pc * 100)
        if parts and parts["15-29"][1] > parts["15-24"][1]:
            elf = parts["15-29"][1] - parts["15-24"][1]
            x = {
                s: 100 * (parts["15-29"][0][i] - parts["15-24"][0][i]) / elf
                for i, s in enumerate(SLACK_EXTRA)
            }
            return x, "ages 25-29 (15-29 minus 15-24)"
    x = {s: lm.get(src, "lfsi_sla_a", s, "25-54", "all", Y, unit=PC_ELF)[0] for s in SLACK_EXTRA}
    if x["uemp_pt"] is None and x["nseek_avl"] is None:
        return None, ""
    return x, "ages 25-54"


def _band_slack_backcast(lm: _LM, src: str, band: str, Y: int) -> tuple[dict | None, str]:
    """Slack by age before Eurostat published it (2021): the earliest published year,
    moved with the all-ages (15-74) slack, which goes back to 2010."""
    first = [y for y in lm.years(src, "lfsi_sla_a", "uemp_pt", "25-54") if y > Y]
    if not first:
        return None, ""
    Yr = min(first)
    x, xn = _band_slack(lm, src, band, Yr)
    if x is None:
        return None, ""
    out = {}
    for s_ in SLACK_EXTRA:
        now = lm.get(src, "lfsi_sla_a", s_, "15-74", "all", Y, unit=PC_ELF)[0]
        ref = lm.get(src, "lfsi_sla_a", s_, "15-74", "all", Yr, unit=PC_ELF)[0]
        if now is None or not ref:
            return None, ""
        out[s_] = (x[s_] or 0.0) * now / ref
    return out, f"{xn} in {Yr}, moved with all-ages slack (no age split before {Yr})"


def _lm_europe(lm: _LM, iso: str, src: str, Y: int, band: str, edu: str) -> dict:
    """One cell for a Eurostat country (src = the country whose data is used). Returns
    {'unemployment': (v, est, note), 'extra': (slack beyond unemployment, of which hours,
    est, note), 'overqualification': (...)}; 20-24 slack is filled in afterwards."""
    out, notes, est = {}, [], src != iso
    if src != iso:
        notes.append(f"proxy: {src} data")
    e = edu if edu in ("all", "below_upper_secondary", "upper_secondary") else "tertiary"
    u, _ = lm.get(src, "lfsa_urgaed", "rate", band, e, Y)
    if u is None:  # small sample: band rate x the education gap at 25-64
        r = _ratio(
            lm.get(src, "lfsa_urgaed", "rate", "25-64", e, Y)[0],
            lm.get(src, "lfsa_urgaed", "rate", "25-64", "all", Y)[0],
        )
        ua, _ = lm.get(src, "lfsa_urgaed", "rate", band, "all", Y)
        if r is None or ua is None:
            return {}
        u, est = ua * r, True
        notes.append(f"{band} rate x {e.replace('_', ' ')} gap at 25-64 (cell not published)")
    if edu in TERTIARY_SUB:
        r = _ratio(
            lm.get(src, "DF_LSO_NEAC_UNEMP", "rate", "25-34", edu, Y, back=4)[0],
            lm.get(src, "DF_LSO_NEAC_UNEMP", "rate", "25-34", "tertiary", Y, back=4)[0],
        )
        if r is not None:
            u, est = u * r, True
            notes.append(f"x{r:.2f} OECD EAG ({edu.replace('_', ' ')} vs all tertiary, 25-34)")
        else:
            notes.append("all tertiary (no split by degree)")
    out["unemployment"] = (u, est, "; ".join(notes))
    if band != "20-24":
        x, xn = _band_slack(lm, src, band, Y)
        if x is None:  # before the 2021 survey reform: no slack by age
            x, xn = _band_slack_backcast(lm, src, band, Y)
            est = est or x is not None
        if x is not None:
            x = {k: max(v or 0.0, 0.0) for k, v in x.items()}
            r_all = r_pt = 1.0
            if e != "all":
                xe, x0 = _sup_edu(lm, src, e, Y), _sup_edu(lm, src, "all", Y)
                if xe and x0:
                    r_all = _ratio(sum(xe), sum(x0)) or 1.0
                    r_pt = _ratio(xe[0], x0[0]) or r_all
                    xn += f", x{r_all:.2f} {e.replace('_', ' ')} gap at 25-74"
                else:
                    xn += " (no education split)"
            out["extra"] = (
                sum(x.values()) * r_all,
                x["uemp_pt"] * r_pt,
                est or e != "all",
                f"Eurostat slack ({xn})",
            )
    if e == "tertiary":
        oa = {"20-24": "20-34", "25-29": "25-34", "30-34": "25-34", "25-64": "20-64"}[band]
        o, _ = lm.get(src, "lfsa_eoqgan", "rate", oa, "tertiary", Y)
        if o is not None:
            out["overqualification"] = (o, src != iso, f"Eurostat, ages {oa}, all tertiary")
    return out


def _lm_us(lm: _LM, Y: int, band: str, edu: str) -> dict:
    out = {}

    def g(stat, b=band, e=edu):
        return lm.get("USA", "CPS basic monthly", stat, b, e, Y)[0]

    n = g("n_lf") or 0
    est, method = lm.meta.get(
        ("USA", "CPS basic monthly", "unemployment", band, edu, Y), (False, "")
    )
    thin = n < 150 and edu != "all"

    def thin_fix(stat):  # band rate x education gap at 25-64
        r = _ratio(g(stat, "25-64", edu), g(stat, "25-64", "all"))
        b = g(stat, band, "all")
        return b * r if r is not None and b is not None else None

    u = thin_fix("unemployment") if thin else g("unemployment")
    note = method + (
        f"; {band} rate x education gap at 25-64 (only {int(n)} sample members)" if thin else ""
    )
    if u is None:
        return out
    out["unemployment"] = (u, est or thin, note)
    if band != "20-24":
        x = thin_fix("slack_extra") if thin else g("slack_extra")
        h = thin_fix("hours_underemployment") if thin else g("hours_underemployment")
        if x is not None and h is not None:
            out["extra"] = (
                x,
                h,
                est or thin,
                "CPS: part-timers wanting full-time + marginally attached",
            )
    if edu in TERTIARY_SUB:
        o = g("overqualification", "25-34") if thin else g("overqualification")
        if o is not None:
            out["overqualification"] = (o, est or thin, method + ("; ages 25-34" if thin else ""))
    ue = thin_fix("underemployment") if thin else g("underemployment")
    te = thin_fix("time_underemployment") if thin else g("time_underemployment")
    if ue is not None and te is not None and ue == ue:
        out["under"] = (ue, te, est or thin, note)
    return out


def _lm_ca_uk(lm: _LM, iso: str, Y: int, band: str, edu: str) -> dict:
    """Canada / UK: OECD EAG by education (25-34, 25-64) and OECD youth unemployment;
    slack = OECD involuntary part-time + StatCan R6-R4 (Canada) / ONS discouraged (UK)."""
    out, notes, est = {}, [], False
    eag = lambda a, e: lm.get(iso, "DF_LSO_NEAC_UNEMP", "rate", a, e, Y, back=2)[0]  # noqa
    a = "25-64" if band == "25-64" else "25-34"
    u = eag(a, edu)
    if edu != "all" and eag("25-34", edu) is None:
        return {}  # level not published: derive_labour_market uses the nearest level
    r_edu = (_ratio(eag("25-34", edu), eag("25-34", "all")) or 1.0) if edu != "all" else 1.0
    if band == "20-24":
        y, _ = lm.get(iso, "DF_IALFS_UNE_M", "rate", "Y15T24", "all", Y)
        if y is None:
            return {}
        u, est = y * r_edu, True
        notes.append(
            "OECD 15-24 rate"
            + (f" x{r_edu:.2f} {edu.replace('_', ' ')} gap at 25-34" if edu != "all" else "")
        )
    elif band in ("25-29", "30-34"):
        notes.append("OECD EAG ages 25-34")
    if u is None:
        return {}
    out["unemployment"] = (u, est, "; ".join(notes))
    if band != "20-24":
        ipt, _ = lm.get(iso, "DF_INVPT_I", "rate", "25-54", "all", Y, back=2)
        if iso == "CAN":
            ca = {"25-29": "25-44", "30-34": "25-44", "25-64": "25-54"}[band]
            r6, _ = lm.get(iso, "14-10-0077-01", "r6", ca, "all", Y)
            r4, _ = lm.get(iso, "14-10-0077-01", "r4", ca, "all", Y)
            disc = r6 - r4 if r6 is not None and r4 is not None else None
            dn = f"StatCan R6-R4 discouraged + waiting ({ca})"
        elif iso == "GBR":
            disc, _ = lm.get("GBR", "ONS LF8B", "discouraged", "16-64", "all", Y, back=2)
            dn = "ONS discouraged workers 16-64 (narrow UK definition)"
        else:
            disc, dn = None, ""
        if ipt is not None and disc is not None:
            out["extra"] = (
                (ipt + disc) * r_edu,
                ipt * r_edu,
                True,
                f"OECD involuntary part-time 25-54 + {dn}"
                + (f", x{r_edu:.2f} education gap" if edu != "all" else ""),
            )
    return out


def _underemployment(c: dict, edu: str) -> dict:
    """Underemployed (ILO): employed people working fewer hours than they want, or -
    graduates - in a job that does not need their degree, % of the employed. US: counted
    person by person in the CPS. Elsewhere: the part-timers wanting more hours, moved from
    the extended labour force to the employed, and for graduates combined with
    over-qualification as if the two were independent (the surveys publish no overlap)."""
    if "under" in c:
        ue, te, est, note = c["under"]
        return {
            "underemployment": (ue, est, note),
            "time_underemployment": (te, est, note),
        }
    if "unemployment" not in c or "extra" not in c:
        return {}
    u, ue, _ = c["unemployment"]
    x, h, xe, xn = c["extra"]
    emp = (1 - max(x - h, 0.0) / 100) * (1 - u / 100)  # employed / extended labour force
    if emp <= 0:
        return {}
    t = h / emp
    out = {"time_underemployment": (t, xe, xn + "; as % of the employed")}
    if edu in TERTIARY_SUB:
        if "overqualification" not in c:
            return out
        o, oe, on = c["overqualification"]
        out["underemployment"] = (
            t + o - t * o / 100,
            True,
            f"part-timers wanting more hours ({xn}) + graduates in non-graduate jobs "
            f"({on}), overlap assumed independent",
        )
    elif edu == "all":  # over-qualification is published for graduates only
        out["underemployment"] = (
            t,
            True,
            xn + "; part-timers wanting more hours only (graduates' over-qualification "
            "not included)",
        )
    else:
        out["underemployment"] = (t, xe or ue, xn + "; part-timers wanting more hours")
    return out


def derive_labour_market(long: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    """Unemployment, functional unemployment (U-6-like slack), hours underemployment,
    over-qualification and underemployment (either) by age band and education, on one
    definition for every country.

    functional = unemployment + 'extra' slack (part-timers wanting more hours + people who
    want work and could start but are not searching). For ages 20-24 the extra slack is
    the 25-29 extra x (20-24 / 25-29 unemployment): 15-24 slack data are dominated by
    students (who want part-time jobs or can't start), the group this comparison avoids."""
    lm = _LM(long)
    rows = []
    srcs = {
        "EU": "Eurostat LFS (lfsa_urgaed, lfsi_sla_a, lfsa_sup_edu, lfsa_eoqgan) + OECD EAG",
        "USA": "CPS microdata (Census API), computed",
        "CAN": "OECD EAG + OECD involuntary part-time + StatCan 14-10-0077",
        "GBR": "OECD EAG + OECD involuntary part-time + ONS LF8B",
    }
    srcs["USA_OECD"] = "OECD EAG (no CENSUS_API_KEY: no CPS microdata)"
    units = {
        "unemployment": "% of labour force",
        "functional": PC_ELF,
        "hours_underemployment": PC_ELF,
        "overqualification": "% of employed graduates in non-graduate jobs",
        "time_underemployment": "% of employed",
        "underemployment": "% of employed",
    }
    for iso in COUNTRIES:
        if iso == "USA" and lm.years("USA", "CPS basic monthly", "unemployment", "25-29"):
            years = lm.years("USA", "CPS basic monthly", "unemployment", "25-29", "all")
            cell = lambda Y, b, e: _lm_us(lm, Y, b, e)  # noqa: E731
            src = srcs["USA"]
        elif iso in ("CAN", "GBR", "USA"):  # USA without CPS microdata: OECD, no slack
            years = [
                y for y in lm.years(iso, "DF_LSO_NEAC_UNEMP", "rate", "25-34", "all") if y >= 2015
            ]
            cell = lambda Y, b, e, i=iso: _lm_ca_uk(lm, i, Y, b, e)  # noqa: E731
            src = srcs["USA_OECD" if iso == "USA" else iso]
        else:
            s = "CHE" if iso == "LIE" else iso
            years = [y for y in lm.years(s, "lfsa_urgaed", "rate", "25-29", "all") if y >= 2015]
            cell = lambda Y, b, e, i=iso, s=s: _lm_europe(lm, i, s, Y, b, e)  # noqa: E731
            src = srcs["EU"]
        for Y in years:
            cells = {(b, e): cell(Y, b, e) for b in LM_BANDS for e in LM_EDU}
            u25 = (cells[("25-29", "all")].get("unemployment") or (None,))[0]
            u20 = (cells[("20-24", "all")].get("unemployment") or (None,))[0]
            k = _ratio(u20, u25)
            for (b, e), c in cells.items():  # degree level not published: nearest level
                if e in TERTIARY_SUB and "unemployment" not in c:
                    for alt in ("bachelor", "short_tertiary", "master_plus"):
                        if "unemployment" in cells[(b, alt)]:
                            cells[(b, e)] = c = {
                                k2: v[:-2]
                                + (
                                    True,
                                    v[-1] + f"; {alt.replace('_', ' ')} "
                                    "rate (this degree level not published)",
                                )
                                for k2, v in cells[(b, alt)].items()
                            }
                            break
            for (b, e), c in cells.items():
                if b == "20-24" and k is not None and "extra" in cells[("25-29", e)]:
                    x, h, est, note = cells[("25-29", e)]["extra"]
                    c["extra"] = (
                        x * k,
                        h * k,
                        True,
                        note + f"; scaled x{k:.2f} (20-24 vs "
                        "25-29 unemployment): 15-24 slack data are mostly students",
                    )
                out = {}
                if "unemployment" in c:
                    out["unemployment"] = c["unemployment"]
                    if "extra" in c:
                        u, ue, un = c["unemployment"]
                        x, h, xe, xn = c["extra"]
                        out["functional"] = (
                            u + x,
                            ue or xe,
                            "; ".join(t for t in (un, "+ " + xn) if t),
                        )
                        out["hours_underemployment"] = (h, xe, xn)
                if "overqualification" in c:
                    out["overqualification"] = c["overqualification"]
                out.update(_underemployment(c, e))
                for ind, (v, est, note) in out.items():
                    if v is None or v != v:
                        continue
                    rows.append(
                        {
                            "iso3": iso,
                            "indicator": ind,
                            "age_band": b,
                            "education": e,
                            "year": Y,
                            "value": round(v, 2),
                            "unit": units[ind],
                            "source": src,
                            "method": note,
                            "estimated": bool(est),
                            "quality": "A" if not est and ind == "unemployment" else "B",
                        }
                    )
    return pd.DataFrame(rows)

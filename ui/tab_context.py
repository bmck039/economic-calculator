"""Context: how the places compare beyond the plan - jobs, pay by degree, prices, safety
and more."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from econsim.model import TERTIARY, age_band
from ui.charts import PALETTE, base_layout
from ui.common import EDU_SHORT


def render(page) -> None:
    ctx_iso = list(dict.fromkeys([page.iso] + [page.PLACES[n][0] for n in page.cmp_names]))
    who = [(int(page.a1), page.d1 or "all"), (int(page.a2), page.d2 or "all")]
    st.subheader("Job market for your age and degree")
    st.caption(
        "**Functional unemployment** counts everyone who wants a job and can't get "
        "one: the unemployed + part-timers who want full-time hours + people who want "
        "work but have stopped looking (Eurostat 'labour-market slack', the "
        "international version of the US U-6). **Headline** is the official ILO rate. "
        "Rates are for people of the partner's age band and highest degree (e.g. a "
        "bachelor's at 23 = 20-24-year-old graduates, i.e. recent graduates). "
        "Same definitions in every country; the US is computed from Census/BLS CPS "
        "microdata, Europe from Eurostat's labour-force survey, Canada and the UK "
        "from OECD + StatCan."
    )

    for col, (i, (age, edu)) in zip(st.columns(2), enumerate(who)):
        _partner_job_market(col, i, age, edu, page.cd)
    a_, e_ = who[0]
    c1, c2 = st.columns(2)
    _lm_chart(
        page,
        ctx_iso,
        "functional",
        f"Functional unemployment — ages {age_band(a_)}, "
        f"{EDU_SHORT[e_].lower()} (dotted: headline)",
        a_,
        e_,
        "unemployment",
        c1,
    )
    if e_ in TERTIARY:
        _lm_chart(
            page,
            ctx_iso,
            "overqualification",
            f"Graduates in non-graduate jobs — ages {age_band(a_)}",
            a_,
            e_,
            None,
            c2,
        )
    else:
        _lm_chart(
            page,
            ctx_iso,
            "hours_underemployment",
            f"Underemployed part-timers — ages {age_band(a_)}, {EDU_SHORT[e_].lower()}",
            a_,
            e_,
            None,
            c2,
        )
    with st.expander("Why youth unemployment looks so different across countries"):
        st.markdown(
            "All countries use the same ILO definition (no work, looked in the last 4 weeks, "
            "can start within 2 weeks), but who is in the 15-24 labour force differs:\n\n"
            "* **Students.** A full-time student looking for a part-time or summer job counts "
            "as unemployed. In Sweden about 60% of unemployed 15-24-year-olds are full-time "
            "students (SCB: 102,000 of 170,000 in 2025); in the US about 30% (CPS 2026).\n"
            "* **Rate vs share of all young people.** The rate divides by the youth labour "
            "force, not all young people. As a share of ALL 15-24-year-olds, ~14% of Swedes "
            "and ~5% of Americans are unemployed (2.7x); counting only those who are not "
            "students, ~5.5% vs ~3.4% (1.6x). Sweden's youth market is weaker, but far less "
            "than 24% vs 9% suggests.\n"
            "* **Apprenticeships.** In Germany, Austria and Switzerland apprentices are "
            "employed, which lowers youth rates.\n\n"
            "That is why this section uses ages 20-24 / 25-29 / 30-34 and the partner's "
            "degree: it compares people like you rather than all teenagers."
        )

    _real_wages(page, ctx_iso)
    c1, c2 = st.columns(2)
    for k, (indicator, title, ytitle) in enumerate(
        [
            ("unemployment_rate", "Unemployment rate", "% of labour force"),
            ("job_vacancy_rate", "Job vacancy rate", "%"),
            ("saving_rate_net", "Household net saving rate", "% of disposable income"),
            ("world_uncertainty_index", "World Uncertainty Index", "index"),
            ("inflation_cpi", "Inflation (CPI)", "% per year"),
            ("political_stability", "Political stability (WGI)", "-2.5 … 2.5"),
        ]
    ):
        _ctx_chart(page, ctx_iso, indicator, title, ytitle, (c1, c2)[k % 2])


def _pct(s):
    return "—" if s is None else f"{s.value:.1f}%" + (" ⚠️" if s.estimated else "")


def _lm_help(s, what):
    if s is None:
        return f"No comparable {what} data for this group."
    return (
        f"{s.source}, {s.year}"
        + (" — ESTIMATE" if s.estimated else "")
        + (f". {s.note}" if s.note else "")
    )


def _partner_job_market(col, i: int, age: int, edu: str, cd) -> None:
    """One partner's unemployment, underemployment and over-qualification now."""
    band = age_band(age)
    lab = cd.labour or {}
    f_ = lab.get(("functional", band, edu))
    u_ = lab.get(("unemployment", band, edu))
    h_ = lab.get(("hours_underemployment", band, edu))
    o_ = lab.get(("overqualification", band, edu)) if edu in TERTIARY else None
    col.markdown(
        f"**Partner {i + 1}** — ages {band}, {EDU_SHORT[edu].lower()}"
        + (f" · {u_.year}" if u_ is not None else "")
    )
    m1, m2 = col.columns(2)
    m1.metric("Functional unemployment", _pct(f_), help=_lm_help(f_, "slack"))
    m2.metric("Headline unemployment", _pct(u_), help=_lm_help(u_, "unemployment"))
    m3, m4 = col.columns(2)
    m3.metric("Underemployed (want more hours)", _pct(h_), help=_lm_help(h_, "hours"))
    m4.metric(
        "Graduates in non-graduate jobs",
        _pct(o_) if edu in TERTIARY else "n/a",
        help=(
            _lm_help(o_, "over-qualification")
            + " Over-qualification: employed graduates in jobs that do not "
            "usually need a degree (clerical, sales, service, trades, "
            "operators). US: outside management/business/financial/"
            "professional occupations (approximation of the EU's ISCO 4-9)."
        )
        if edu in TERTIARY
        else "Applies to degree holders.",
    )


def _lm_chart(page, ctx_iso, indicator, title, age, edu, dashed=None, col=None):
    """One labour-market indicator over time, for each place."""
    ds, names = page.ds, page.names
    band, fig, srcs = age_band(age), go.Figure(), set()
    for i, c in enumerate(ctx_iso):
        for ind, dash in [(indicator, None)] + ([(dashed, "dot")] if dashed else []):
            d = ds.labour_history(c, ind, band, edu)
            if d.empty:
                continue
            srcs.add(str(d["source"].iloc[-1]))
            nm = names[c] + ("" if dash is None else " (headline)")
            fig.add_scatter(
                x=d["year"],
                y=d["value"],
                name=nm,
                mode="lines+markers",
                line=dict(color=PALETTE[i], width=2, dash=dash),
                marker=dict(size=5),
                showlegend=dash is None or i == 0,
                hovertemplate=nm + ": %{y:.1f}%<extra></extra>",
            )
    lay = base_layout(fig, title, "%")
    lay.update_layout(height=340, xaxis_title=None)
    (col or st).plotly_chart(lay, width="stretch")
    (col or st).caption(
        ("Sources: " + "; ".join(sorted(srcs)))
        if srcs
        else "No data for this age band and degree."
    )


def _ctx_chart(page, ctx_iso, indicator, title, ytitle, col=None):
    """One context series over time, for each place."""
    ds, names = page.ds, page.names
    fig = go.Figure()
    srcs = set()
    for i, c in enumerate(ctx_iso):
        d = ds.context(c, indicator)
        d = d[d["year"] >= 2005]
        if d.empty:
            continue
        srcs |= set(d["source"].astype(str))
        fig.add_scatter(
            x=d["year"],
            y=d["value"],
            name=names[c],
            mode="lines+markers",
            line=dict(color=PALETTE[i], width=2),
            marker=dict(size=5),
            hovertemplate=names[c] + ": %{y:.1f}<extra></extra>",
        )
    lay = base_layout(fig, title, ytitle)
    lay.update_layout(height=320, xaxis_title=None)
    (col or st).plotly_chart(lay, width="stretch")
    (col or st).caption("Source: " + ", ".join(sorted(srcs)) if srcs else "No data.")


def _real_wages(page, ctx_iso) -> None:
    """Real average wages, indexed (wage stagnation)."""
    ds, names = page.ds, page.names
    fig = go.Figure()
    srcs = set()
    for i, c in enumerate(ctx_iso):
        r = ds.rwh[
            (ds.rwh["iso3"] == c)
            & (ds.rwh["measure"] == "avg_wage")
            & (ds.rwh["source"] == "OECD average wages")
        ]
        if r.empty:
            r = ds.rwh[(ds.rwh["iso3"] == c) & (ds.rwh["measure"] == "avg_wage")]
        if r.empty:
            continue
        r = r[r["unit_nominal"] == r["unit_nominal"].iloc[0]].sort_values("year")
        srcs.add(str(r["source"].iloc[0]))
        fig.add_scatter(
            x=r["year"],
            y=r["index_first_year_100"],
            name=names[c],
            mode="lines",
            line=dict(color=PALETTE[i], width=2),
            hovertemplate=names[c] + ": %{y:.0f}<extra></extra>",
        )
    lay = base_layout(fig, "Real average wage (first year = 100) — wage stagnation", "index")
    lay.update_layout(height=340, xaxis_title="Year")
    st.plotly_chart(lay, width="stretch")
    st.caption(
        "MEAN wages per full-time equivalent, deflated by CPI. Source: " + ", ".join(sorted(srcs))
    )

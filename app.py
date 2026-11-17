"""Economic Calculator and Planner - Streamlit UI.

    .venv/bin/streamlit run app.py

All modelling lives in econsim/ (engine, tax, data); the page is drawn by ui/: the sidebar
collects the inputs, ui.plan turns them into a Plan, and each tab draws from the result.
Every default shows its source, year and estimate flag; the 'Sources' tab lists every
number used.
"""

from __future__ import annotations

from functools import partial

import streamlit as st

from econsim.data import DataStore
from econsim.engine import simulate, to_display
from ui import (
    header,
    settings,
    sidebar,
    tab_budget,
    tab_compare,
    tab_context,
    tab_income,
    tab_savings,
    tab_sources,
    tab_taxes,
)
from ui.plan import build_plan, country_with_overrides

st.set_page_config(page_title="Life Abroad Planner", layout="wide", page_icon="🌍")


@st.cache_resource
def store() -> DataStore:
    return DataStore()


ds = store()
countries = ds.countries()
if not countries:
    st.error("No data found in econ_data/. Run `python build_econ_dataset.py` first.")
    st.stop()

v = sidebar.render(ds, countries)
# what the header and the tabs draw from (ui/tab_*.py): the choices and the result
v.us = ds.us_params()
v.build_plan = partial(build_plan, v)
v.country_with_overrides = partial(country_with_overrides, v)
v.plan = v.build_plan(v.iso, True)
cd_run = v.country_with_overrides(v.cd)
v.res = simulate(v.plan, cd_run, v.us)
v.disp = to_display(v.res, cd_run, v.real, v.cur, v.defl)
v.CUR = {"local": v.cd.currency, "USD": "USD", "intl$": "intl $"}[v.cur]
v.money_note = ("today's money" if v.real else "nominal") + f", {v.CUR}"

header.render(v)
TABS = {
    "💶 Monthly budget": tab_budget,
    "📈 Savings": tab_savings,
    "💼 Income": tab_income,
    "🧾 Taxes": tab_taxes,
    "🌐 Compare": tab_compare,
    "🧭 Context": tab_context,
    "📚 Sources": tab_sources,
}
for tab, module in zip(st.tabs(list(TABS)), TABS.values()):
    with tab:
        module.render(v)

settings.finish(v.share_box)

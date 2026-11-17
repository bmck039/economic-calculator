"""Settings names, links and the migration of older saved settings (no data needed)."""

from __future__ import annotations

import textwrap

from ui.names import VERSION, global_key, migrate, place_key
from ui.share import decode_share, encode_share


def test_keys():
    assert global_key("p1_age") == "w:p1_age"
    assert place_key("rent", "NLD", None) == "k:rent@NLD"
    assert place_key("rent", "USA", "CA") == "k:rent@USA-CA"


def test_link_round_trip():
    s = {"w:p1_age": 31, "w:compare_with": ["Germany"], "k:rent@USA-NY": 2100.0}
    assert decode_share(encode_share(s)) == {"v": VERSION, "s": s}
    assert decode_share("not a link") is None


def test_version_1_names_are_migrated():
    v1 = {
        "w:cname": "United States",
        "w:state": "NY",
        "w:a1": 31,
        "w:m_age31": 33,
        "w:m_age23": 40,
        "w:k1None": 1,
        "k:rent_USA_NY": 2100.0,
        "k:hpbachelorNone35_USA_NY": 450000.0,
        "k:own_USA_NY": 1.2,
        "k:own31NoneNone_USA_NY": True,
        "k:ret_DEU_None": 66,
        "k:ret0.6_DEU_None": 5.1,
        "k:starting_savings31NoneFalse_DEU_None": 9000.0,
        "k:depr_DEU_None": 1.5,
        "w:unknown_old": 1,
    }
    assert migrate(1, v1) == {
        "w:country": "United States",
        "w:us_state": "NY",
        "w:p1_age": 31,
        "w:marriage_age": 33,
        "k:rent@USA-NY": 2100.0,
        "k:home_price@USA-NY": 450000.0,
        "k:owner_costs_pct@USA-NY": 1.2,
        "k:own_home@USA-NY": True,
        "k:retirement_age@DEU": 66,
        "k:investment_return@DEU": 5.1,
        "k:starting_savings@DEU": 9000.0,
        "k:deposit_return@DEU": 1.5,
    }
    assert migrate(VERSION, {"w:p1_age": 40}) == {"w:p1_age": 40}


def test_following_settings(tmp_path):
    """Until changed, a setting follows its default; once changed it keeps the value, and
    a following setting still at its default is not saved (so it goes on following)."""
    from streamlit.testing.v1 import AppTest

    script = tmp_path / "app.py"
    script.write_text(
        textwrap.dedent("""
        import streamlit as st
        from ui.settings import _persisted, follow
        a1 = st.number_input("a1", 18, 70, 23, key="w:p1_age")
        m = st.number_input("m", 18, 80, max(int(a1), 28),
                            key=follow("w:marriage_age", max(int(a1), 28)))
        st.write(f"{m}|{sorted(_persisted())}")
    """)
    )
    at = AppTest.from_file(str(script)).run()
    assert at.markdown[0].value == "28|['w:p1_age']"
    at.number_input(key="w:p1_age").set_value(31).run()
    assert at.markdown[0].value == "31|['w:p1_age']"
    at.number_input(key="w:marriage_age").set_value(35).run()
    at.number_input(key="w:p1_age").set_value(33).run()
    assert at.markdown[0].value == "35|['w:marriage_age', 'w:p1_age']"

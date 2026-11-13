"""Headless smoke test of the Streamlit UI (needs econ_data/ built)."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")
HAVE_DATA = (Path(APP).parent / "econ_data" / "earnings_profile.csv").exists()


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize("country", ["Netherlands", "United States", "Switzerland", "Portugal",
                                     "United Kingdom", "Canada", "Liechtenstein"])
def test_app_runs_for_country(country):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    at.sidebar.selectbox[0].set_value(country).run()
    assert not at.exception, at.exception
    assert any(country in t.value for t in at.title)
    assert len(at.metric) == 5 + 8          # headline row + job market (2 partners x 4)


def _w(at, kind, label):
    return next(w for w in getattr(at.sidebar, kind) if w.label.startswith(label))


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_toggles():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _w(at, "radio", "Tenure").set_value("Rent forever").run()
    _w(at, "toggle", "Show in today").set_value(False).run()
    _w(at, "radio", "Currency").set_value("USD").run()
    _w(at, "checkbox", "Simulate a job loss").set_value(True).run()
    _w(at, "selectbox", "Child 1").set_value("none").run()
    _w(at, "radio", "Main parental leave").set_value("Partner 1").run()
    _w(at, "number_input", "Cars owned").set_value(0).run()
    _w(at, "checkbox", "Use the country's lending rules").set_value(False).run()
    assert not at.exception, at.exception


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_us_state():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].set_value("United States").run()
    _w(at, "selectbox", "US state").set_value("CA").run()
    assert not at.exception, at.exception
    assert any("California" in t.value for t in at.title)


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize("country", ["Sweden", "United States", "United Kingdom", "Canada"])
def test_app_degrees(country):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].set_value(country).run()
    _w(at, "selectbox", "P1 highest degree").set_value("bachelor").run()
    _w(at, "selectbox", "P2 highest degree").set_value("upper_secondary").run()
    assert not at.exception, at.exception
    m = {x.label: x.value for x in at.metric}
    if (Path(APP).parent / "econ_data" / "labour_market.csv").exists():
        assert m["Functional unemployment"] not in ("—", None)
    _w(at, "checkbox", "Pay follows the degree").set_value(False).run()
    assert not at.exception, at.exception


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize("country", ["Germany", "United States", "Iceland"])
def test_app_outlook_and_life_factors(country):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].set_value(country).run()
    _w(at, "radio", "House prices & rents").set_value("Recent trend (10 yrs)").run()
    _w(at, "radio", "After the fixed period").set_value("20-year average").run()
    _w(at, "number_input", "Gift from family").set_value(30000.0).run()
    _w(at, "number_input", "Inheritance (").set_value(100000.0).run()
    _w(at, "slider", "Parents pay this share").set_value(50).run()
    _w(at, "slider", "Main parent's lasting pay").set_value(7).run()
    _w(at, "number_input", "Years of paid long-term care").set_value(3).run()
    _w(at, "radio", "In retirement we spend").set_value("Only the pension").run()
    _w(at, "radio", "How we hold our savings").set_value("Custom").run()
    _w(at, "slider", "% kept in bank deposits").set_value(30).run()
    _w(at, "slider", "% of investments in shares").set_value(80).run()
    _w(at, "number_input", "Student debt at the start").set_value(40000.0).run()
    _w(at, "number_input", "Interest %/yr").set_value(5.0).run()
    _w(at, "number_input", "Repaid over").set_value(20).run()
    _w(at, "radio", "How we hold our savings").set_value("Invest everything").run()
    if country == "United States":
        _w(at, "selectbox", "P1 now").set_value("PT").run()
        _w(at, "number_input", "P1 gross pay now").set_value(1200.0).run()
        _w(at, "selectbox", "P2 now").set_value("FT_nongrad").run()
        _w(at, "number_input", "P2 gross pay now").set_value(3000.0).run()
        _w(at, "radio", "Job market ahead").set_value(
            "Keeps changing as in the last year (5 more years)").run()
        _w(at, "radio", "Costs ahead").set_value("All rise with prices").run()
        _w(at, "checkbox", "Careers follow today's job market").set_value(False).run()
    assert not at.exception, at.exception
    assert any(m.label.startswith("Net worth at retirement") for m in at.metric)


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize("country", ["United States", "Germany"])
def test_app_generations_wealth_and_own_inflation(country):
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    at.sidebar.selectbox[0].set_value(country).run()
    _w(at, "selectbox", "P1 now").set_value("PT").run()
    _w(at, "radio", "How far our generation").set_value("full").run()
    _w(at, "radio", "Today's money means").set_value("own").run()
    _w(at, "number_input", "Partner 1 age").set_value(45).run()
    _w(at, "checkbox", "We already own our home").set_value(True).run()
    assert not at.exception

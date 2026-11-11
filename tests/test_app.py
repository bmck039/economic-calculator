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

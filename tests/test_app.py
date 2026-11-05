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
    assert len(at.metric) == 5


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_toggles():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.radio[0].set_value("Rent forever").run()      # tenure
    at.sidebar.toggle[0].set_value(False).run()               # nominal
    at.sidebar.radio[1].set_value("USD").run()                # currency
    at.sidebar.checkbox[1].set_value(True).run()              # job loss
    at.sidebar.selectbox[3].set_value("none").run()           # child 1 none
    assert not at.exception, at.exception

"""Headless smoke test of the Streamlit UI (needs econ_data/ built)."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")
HAVE_DATA = (Path(APP).parent / "econ_data" / "earnings_profile.csv").exists()


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize(
    "country",
    [
        "Netherlands",
        "United States",
        "Switzerland",
        "Portugal",
        "United Kingdom",
        "Canada",
        "Liechtenstein",
    ],
)
def test_app_runs_for_country(country):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    at.sidebar.selectbox[0].set_value(country).run()
    assert not at.exception, at.exception
    assert any(country in t.value for t in at.title)
    assert len(at.metric) == 5 + 8  # headline row + job market (2 partners x 4)


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
            "Keeps changing as in the last year (5 more years)"
        ).run()
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


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_compares_us_states():
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    at.sidebar.selectbox[0].set_value("United States").run()
    ms = _w(at, "multiselect", "Compare with")
    assert "United States – California" in ms.options and "United States" not in ms.options
    ms.set_value(["United States – California", "United States – Mississippi"]).run()
    assert not at.exception, at.exception
    t = next(d.value for d in at.dataframe if "country" in d.value.columns)
    assert list(t["country"]) == [
        "United States",
        "United States – California",
        "United States – Mississippi",
    ]
    inc = t.set_index("country")["net income /mo"]
    # state pay, taxes and prices differ (careers follow state earnings too)
    assert (
        inc["United States – California"]
        > inc["United States"]
        > inc["United States – Mississippi"]
    )
    # choosing a state as the main place removes it from the comparison
    _w(at, "selectbox", "US state").set_value("CA").run()
    assert not at.exception, at.exception
    assert "United States – California" not in _w(at, "multiselect", "Compare with").value


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_restores_saved_settings():
    """What the browser saved (localStorage) is put back into the widgets."""
    at = AppTest.from_file(APP, default_timeout=90)
    for k, v in {
        "w:country": "United States",
        "w:us_state": "NY",
        "w:p1_age": 31,
        "w:p2_age": 33,
        "w:compare_with": ["United States – Ohio"],
        "_restored": True,
    }.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    assert any("New York" in t.value for t in at.title)
    assert _w(at, "number_input", "Partner 1 age").value == 31
    assert _w(at, "multiselect", "Compare with").value == ["United States – Ohio"]
    # every setting has a key that can be saved
    import ast

    tree = ast.parse(Path(APP).read_text())
    widgets = {
        "selectbox",
        "radio",
        "multiselect",
        "slider",
        "number_input",
        "checkbox",
        "toggle",
        "text_input",
        "select_slider",
    }
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in widgets
        ):
            assert any(k.arg == "key" for k in n.keywords), n.lineno
    # reset: back to the defaults
    _w(at, "button", "↺ Reset").click().run()
    assert not at.exception, at.exception
    assert _w(at, "number_input", "Partner 1 age").value == 23


def _share_code(version: int, settings: dict) -> str:
    """The link format: compact JSON {"v": version, "s": settings}, zlib, URL-safe base64."""
    import base64
    import json
    import zlib

    raw = json.dumps({"v": version, "s": settings}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
@pytest.mark.parametrize(
    "version,settings",
    [
        (
            2,
            {
                "w:country": "United States",
                "w:us_state": "NY",
                "w:p1_age": 31,
                "w:compare_with": ["Germany"],
                "w:marriage_age": 33,
            },
        ),
        (
            1,
            {
                "w:cname": "United States",
                "w:state": "NY",
                "w:a1": 31,  # a v0.8 link
                "w:cmp_names": ["Germany"],
                "w:m_age31": 33,
                "w:m_age23": 40,
            },
        ),
    ],
)
def test_app_opens_a_shared_link(version, settings):
    """?plan=... loads every setting in it, and the page offers a link for its own."""
    at = AppTest.from_file(APP, default_timeout=90)
    at.query_params["plan"] = _share_code(version, settings)
    at.run()
    assert not at.exception, at.exception
    assert any("New York" in t.value for t in at.title)
    assert _w(at, "number_input", "Partner 1 age").value == 31
    assert _w(at, "multiselect", "Compare with").value == ["Germany"]
    assert _w(at, "number_input", "Marriage at P1 age").value == 33
    assert "plan" not in at.query_params  # later edits are the viewer's own
    assert any("shared plan" in i.value for i in at.sidebar.info)
    # the page's own link carries the same settings
    import base64
    import json
    import zlib

    link = at.code[0].value
    code = link.split("?plan=")[1]
    s = json.loads(zlib.decompress(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))))
    assert s["v"] == 2 and s["s"]["w:p1_age"] == 31 and s["s"]["w:us_state"] == "NY"
    assert s["s"]["w:marriage_age"] == 33
    assert len(link) < 4000  # comfortably within URL limits


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_app_rejects_a_broken_link():
    at = AppTest.from_file(APP, default_timeout=90)
    at.query_params["plan"] = _share_code(2, {"w:p1_age": 31})[:-12]  # cut short
    at.run()
    assert not at.exception, at.exception
    assert any("could not be read" in w.value for w in at.sidebar.warning)
    assert _w(at, "number_input", "Partner 1 age").value == 23


@pytest.mark.skipif(not HAVE_DATA, reason="run build_econ_dataset.py first")
def test_compare_tab_charts_wages_prices_and_house_purchases():
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["w:compare_with"] = ["Germany", "Spain"]
    at.run()
    assert not at.exception, at.exception
    import plotly.io as pio

    figs = [pio.from_json(e.proto.spec) for e in at.get("plotly_chart")]
    titles = {f.layout.title.text: f for f in figs if f.layout.title.text}
    assert "Wages over time (household, before tax)" in titles
    assert "Consumer prices over time" in titles
    prices = [t for t in titles["Consumer prices over time"].data if t.name]
    import base64

    import numpy as np

    def arr(v):  # plotly 6 sends arrays as {"dtype", "bdata"}
        return (
            np.frombuffer(base64.b64decode(v["bdata"]), v["dtype"])
            if isinstance(v, dict)
            else np.asarray(v)
        )

    assert len(prices) == 3 and all(abs(arr(t.y)[0] - 100) < 1e-6 for t in prices)
    houses = [
        t
        for t in titles["Net worth over life"].data
        if t.text is not None and "🏠" in list(t.text)
    ]
    assert houses, "no house-purchase markers"

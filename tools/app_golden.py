"""Exact-output check for refactoring the app: render the page with several settings and
compare every title, text, metric, table and chart with what was recorded.

    python -m tools.app_golden save | check

Kept next to the data (econ_data/golden_app/), like tools.golden.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ["LIFEPLAN_NO_BROWSER_STORE"] = "1"
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PRESETS = {
    "default": {},
    "us_state": {
        "w:country": "United States",
        "w:us_state": "CA",
        "w:compare_with": ["United States – Mississippi", "Germany"],
    },
    "germany_family": {
        "w:country": "Germany",
        "w:p1_age": 27,
        "w:p2_age": 29,
        "w:job_loss": True,
        "w:compare_with": ["France", "Spain"],
    },
    "uk_owner": {
        "w:country": "United Kingdom",
        "w:p1_age": 45,
        "w:p2_age": 44,
        "w:cut_before_savings": True,
    },
    "us_abroad": {"w:country": "Ireland", "w:compare_with": ["United States"]},
}


def _elements(at) -> dict:
    out = {}
    for kind in (
        "title",
        "header",
        "subheader",
        "markdown",
        "caption",
        "info",
        "warning",
        "success",
        "error",
        "code",
    ):
        out[kind] = [str(e.value) for e in getattr(at, kind)]
    for side in ("sidebar",):
        out["sidebar_text"] = [str(e.value) for e in getattr(at, side).markdown]
    out["metric"] = [(m.label, m.value, m.delta) for m in at.metric]
    out["dataframe"] = [e.value.astype(str).to_dict("split") for e in at.dataframe]
    out["plotly"] = [json.loads(e.proto.spec) for e in at.get("plotly_chart")]
    out["widgets"] = (
        {
            k: str(at.session_state[k])
            for k in at.session_state
            if isinstance(k, str) and k.startswith(("w:", "k:"))
        }
        if hasattr(at.session_state, "__iter__")
        else {}
    )
    return out


def render(preset: dict) -> dict:
    at = AppTest.from_file(APP, default_timeout=300)
    for k, v in preset.items():
        at.session_state[k] = v
    at.run()
    if at.exception:
        return {"exception": [str(e.value) for e in at.exception]}
    return _elements(at)


def main() -> int:
    from econsim.data import DataStore

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["save", "check"])
    a = ap.parse_args()
    out = Path(DataStore().dir) / "golden_app"
    out.mkdir(exist_ok=True)
    bad = 0
    for name, preset in PRESETS.items():
        got = json.loads(json.dumps(render(preset), default=str))
        f = out / f"{name}.json"
        if a.action == "save":
            f.write_text(json.dumps(got))
            continue
        old = json.loads(f.read_text())
        if old != got:
            bad += 1
            keys = sorted(k for k in set(old) | set(got) if old.get(k) != got.get(k))
            print(f"{name}: differs in {keys}")
            for k in keys:
                o, g = old.get(k), got.get(k)
                if isinstance(o, list) and isinstance(g, list):
                    for i, (x, y) in enumerate(zip(o, g)):
                        if x != y:
                            print(f"  {k}[{i}]: {str(x)[:200]}\n   -> {str(y)[:200]}")
                            break
                    if len(o) != len(g):
                        print(f"  {k}: {len(o)} vs {len(g)} items")
    print("saved" if a.action == "save" else f"{len(PRESETS) - bad} of {len(PRESETS)} identical")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

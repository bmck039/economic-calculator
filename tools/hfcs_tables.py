"""Compile the ECB's HFCS statistical tables (PDF only) into reference/hfcs_tables.csv.

The Household Finance and Consumption Survey publishes country tables by age of the
household's reference person only as PDFs (the microdata need an application). This
script downloads the four waves' tables, extracts their text with poppler's `pdftotext
-layout`, and parses every "breakdowns" table into long rows:

    wave, iso3, table, indicator, group, row, value, se

plus the fieldwork periods (table "REF": the survey's reference year for assets per
country). Values are as published: EUR thousands (nominal, survey year) or % of
households. The CSV is committed so the data build needs neither the PDFs nor poppler.

    python tools/hfcs_tables.py [--pdf-dir DIR]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://www.ecb.europa.eu/home/pdf/research/hfcn/"
WAVES = {   # wave -> file (the ECB's file names do not follow the waves)
    2010: "HFCS_Statistical_Tables_Wave2010.pdf",
    2014: "HFCS_Statistical_Tables_Wave1.pdf",
    2017: "HFCS_Statistical_Tables_Wave_2017_July2023.pdf",
    2021: "HFCS_Statistical_Tables_Wave_2021_June_2026.pdf",
}
# breakdown tables kept: id -> indicator
TABLES = {
    "A3": "networth_median",
    "B3": "own_hmr_pct",
    "B5": "realestate_median",
    "C4": "financial_median",
    "E5": "has_debt_pct",
    "E6": "debt_median",
    "I1": "income_median",
}
ISO2 = {"BE": "BEL", "CZ": "CZE", "DE": "DEU", "EE": "EST", "IE": "IRL", "GR": "GRC",
        "ES": "ESP", "FR": "FRA", "HR": "HRV", "IT": "ITA", "CY": "CYP", "LV": "LVA",
        "LT": "LTU", "LU": "LUX", "HU": "HUN", "MT": "MLT", "NL": "NLD", "AT": "AUT",
        "PL": "POL", "PT": "PRT", "SI": "SVN", "SK": "SVK", "FI": "FIN", "EA": "EA"}
COUNTRY_NAMES = {"Belgium": "BEL", "Czech Republic": "CZE", "Czechia": "CZE",
                 "Germany": "DEU", "Estonia": "EST", "Ireland": "IRL", "Greece": "GRC",
                 "Spain": "ESP", "France": "FRA", "Croatia": "HRV", "Italy": "ITA",
                 "Cyprus": "CYP", "Latvia": "LVA", "Lithuania": "LTU", "Luxembourg": "LUX",
                 "Hungary": "HUN", "Malta": "MLT", "Netherlands": "NLD", "Austria": "AUT",
                 "Poland": "POL", "Portugal": "PRT", "Slovenia": "SVN", "Slovakia": "SVK",
                 "Finland": "FIN"}
VAL = re.compile(r"^(-?\d[\d,]*\.?\d*|M|N|<0\.1|-)$")
TITLE = re.compile(r"^\s*Table ([A-Z]\d+) (.+?)\s*$")


def text_pages(pdf: Path) -> list[str]:
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], check=True,
                         capture_output=True, text=True).stdout
    return out.split("\f")


def _num(tok: str):
    if tok in ("M", "N", "-"):
        return None
    if tok == "<0.1":
        return 0.0
    return float(tok.replace(",", ""))


def parse_tables(pages: list[str], wave: int) -> list[dict]:
    rows, table, cols, group = [], None, None, None
    pending_val = None
    for page in pages:
        for line in page.splitlines():
            if "...." in line:          # table of contents
                continue
            m = TITLE.match(line)
            if m:
                table, cols, group, pending_val = m.group(1), None, None, None
                continue
            if table not in TABLES:
                continue
            toks = line.split()
            if not toks:
                continue
            codes = [t for t in toks if t in ISO2 and t != "EA"]
            if len(codes) >= 4 and len(codes) >= len(toks) - 2:      # column header
                cols = (["EA"] if "area" in toks else []) + codes
                continue
            if cols is None:
                continue
            s = line.strip()
            if s.startswith("("):       # standard errors of the row above
                if pending_val is not None:
                    ses = [t.strip("()") for t in s.split()]
                    for r, se in zip(pending_val, ses):
                        try:
                            r["se"] = float(se)
                        except ValueError:
                            pass
                pending_val = None
                continue
            line2 = line.replace("< 0.1", "<0.1")
            toks = line2.split()
            vals = []
            while toks and VAL.match(toks[-1]) and len(vals) < len(cols):
                vals.insert(0, toks.pop())
            if len(vals) != len(cols):
                pending_val = None
                continue
            label = line2[: len(line2.rstrip()) - len(" ".join(vals))]
            # the label part: "Group   row" or "   row" (group carried on)
            left = line2[: line2.find(vals[0]) if vals else len(line2)]
            parts = [p for p in re.split(r"\s{2,}", left.strip()) if p]
            if not parts:
                continue
            indent = len(left) - len(left.lstrip())
            if len(parts) >= 2:
                group, row = parts[0], " ".join(parts[1:])
            elif indent < 8 and group is None:
                group, row = parts[0], parts[0]
            elif indent < 8:
                group, row = parts[0], parts[0]
            else:
                row = parts[0]
            new = []
            for c, v in zip(cols, vals):
                new.append({"wave": wave, "iso3": ISO2[c], "table": table,
                            "indicator": TABLES[table], "group": group, "row": row,
                            "value": _num(v), "se": None})
            rows += new
            pending_val = new
            del label
    return rows


def parse_reference(pages: list[str], wave: int) -> list[dict]:
    """Fieldwork periods: the year assets refer to (fieldwork midpoint)."""
    rows = []
    on = False
    for page in pages:
        for line in page.splitlines():
            if re.match(r"^\s*Reference periods\s*$", line):
                on = True
                continue
            if not on:
                continue
            m = re.match(r"^\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?)\s+(\d{2})/(\d{4})\s*[–-]?\s*"
                         r"(\d{2})/(\d{4})(.*)$", line)
            if m and m.group(1) in COUNTRY_NAMES:
                a = int(m.group(3)) + (int(m.group(2)) - 0.5) / 12
                b = int(m.group(5)) + (int(m.group(4)) - 0.5) / 12
                mid = (a + b) / 2
                # income: a stated calendar year (its middle), else the 12 months before
                # the interview / the last calendar year
                rest = re.split(r"\s{2,}", m.group(6).strip())
                inc = rest[-1] if rest else ""
                yrs = re.findall(r"\b(\d{4})\b", inc)
                if yrs and "/" not in inc:
                    iy = int(yrs[-1]) + 0.5
                elif "calendar" in inc.lower():
                    iy = int(mid) - 0.5
                else:
                    iy = mid - 0.5
                iso = COUNTRY_NAMES[m.group(1)]
                for ind, v in (("fieldwork_mid", mid), ("income_year_mid", iy)):
                    rows.append({"wave": wave, "iso3": iso, "table": "REF", "indicator": ind,
                                 "group": "", "row": "", "value": round(v, 2), "se": None})
        if on and rows:
            break
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", default=None)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1]
                                         / "reference" / "hfcs_tables.csv"))
    a = ap.parse_args()
    d = Path(a.pdf_dir) if a.pdf_dir else Path.cwd() / "hfcs_pdf"
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for wave, f in WAVES.items():
        p = d / f
        if not p.exists():
            print("downloading", f, file=sys.stderr)
            urllib.request.urlretrieve(BASE + f, p)
        pages = text_pages(p)
        t = parse_tables(pages, wave)
        r = parse_reference(pages, wave)
        print(wave, len(t), "values,", len(r), "reference periods", file=sys.stderr)
        rows += t + r
    df = pd.DataFrame(rows)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out, index=False)
    print("wrote", a.out, len(df), file=sys.stderr)


if __name__ == "__main__":
    main()

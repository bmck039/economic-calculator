"""Lookups over the tidy table shared by the derived tables."""

from __future__ import annotations

import pandas as pd

from econdata.config import EURO_DATA, EUROPE

PER_YEAR = {"month": 12, "week": 52, "year": 1}


class Ctx:
    """Lookup helpers over the long table."""

    def __init__(self, long: pd.DataFrame, refs: pd.DataFrame):
        # Everything is indexed ONCE here. The old helpers re-filtered the full
        # (3M-row) table on every call, thousands of times: that was most of the hour.
        self.long = long
        self.refs = refs
        self.by_measure: dict[str, pd.DataFrame] = {
            m: g for m, g in long.groupby("measure", sort=False)
        }
        self.by_measure_iso: dict[tuple[str, str], pd.DataFrame] = {}
        for m, g in self.by_measure.items():
            for iso3, gi in g.groupby("iso3", sort=False):
                self.by_measure_iso[(m, iso3)] = gi
        pl = self.measure("price_level")
        wb = pl[pl["source"].astype(str).str.startswith("World Bank WDI")]
        self._wb = {
            s: {(r.iso3, int(r.year)): r.value for r in g.itertuples() if pd.notna(r.year)}
            for s, g in wb.groupby("statistic")
        }
        for s in ("ppp_private_consumption", "exchange_rate", "cpi_index", "price_level_ratio"):
            self._wb.setdefault(s, {})
        self.latest_year = max([y for (_, y) in self._wb["cpi_index"]] or [2024])
        # HICP sub-indices: (iso3, coicop) -> {year: index}
        h = pl[pl["statistic"] == "hicp_index"]
        self._hicp: dict[tuple[str, str], dict[int, float]] = {}
        for r in h.itertuples():
            if pd.notna(r.year):
                self._hicp.setdefault((r.iso3, r.category), {})[int(r.year)] = r.value
        # Eurostat PPP (NAC per PPS): iso3 -> {year: value}, household consumption
        pp = pl[pl["statistic"] == "ppp_eu27_2020"]
        self._nac_pps: dict[str, dict[int, float]] = {}
        self._ppp_cat: dict[tuple[str, str], dict[int, float]] = {}
        for pat in ("actual individual consumption", "household final consumption"):
            s = pp[pp["category"].astype(str).str.contains(pat, case=False, na=False)]
            for r in s.itertuples():
                self._nac_pps.setdefault(r.iso3, {})[int(r.year)] = r.value
        for r in pp.itertuples():
            code = str(r.series).split(":")[-1]
            self._ppp_cat.setdefault((r.iso3, code), {})[int(r.year)] = r.value

    def measure(self, m: str) -> pd.DataFrame:
        return self.by_measure.get(m, self.long.iloc[0:0])

    def mi(self, m: str, iso3: str) -> pd.DataFrame:
        return self.by_measure_iso.get((m, iso3), self.long.iloc[0:0])

    def hicp_factor(self, iso3: str, code: str, from_year: int):
        """Price change from `from_year` to the latest year for COICOP `code`, falling
        back to its parents and finally the all-items index (CP00)."""
        for c in [code[:k] for k in range(len(code), 3, -1)] + ["CP00"]:
            s = self._hicp.get((iso3, c))
            if not s or from_year not in s:
                continue
            ly = max(s)
            return s[ly] / s[from_year], ly, c
        return 1.0, from_year, None

    def ppp_category(self, iso3: str, coicop2: str, year: int):
        """NAC per PPS for a COICOP division (CP01..CP12 -> A0101..A0112)."""
        code = "A01" + coicop2[2:4] if coicop2.startswith("CP") and len(coicop2) >= 4 else "A01"
        for c in (code, "A01"):
            s = self._ppp_cat.get((iso3, c))
            if s:
                y = year if year in s else max(s)
                return s[y]
        return None

    def wb(self, stat, iso3, year, back=6, ahead=2):
        t = self._wb.get(stat, {})
        for y in range(int(year), int(year) - back - 1, -1):
            if (iso3, y) in t:
                return t[(iso3, y)], y
        for y in range(int(year) + 1, int(year) + ahead + 1):  # small look-ahead
            if (iso3, y) in t:
                return t[(iso3, y)], y
        return None, None

    def cpi(self, iso3, year):
        v, _ = self.wb("cpi_index", iso3, year, back=0, ahead=0)
        return v

    def nac_per_pps(self, iso3, year):
        """Eurostat PPP (national currency per PPS) for household consumption."""
        s = self._nac_pps.get(iso3)
        if not s:
            return None
        return s[year] if year in s else s[max(s)]

    def lcu_per_eur(self, iso3, year):
        """Local currency per EUR, from World Bank LCU/USD ratios (euro members -> ~1)."""
        if iso3 in EURO_DATA:
            return 1.0
        c, _ = self.wb("exchange_rate", iso3, year)
        e, _ = self.wb("exchange_rate", "EMU", year)
        return c / e if c and e else None

    def to_local(self, value, unit, iso3, year):
        """Convert 'EUR...' units to local currency; pass through local units."""
        cur = str(unit).split("/")[0].split(" ")[0]
        if cur == "EUR":
            r = self.lcu_per_eur(iso3, year)
            return value * r if r else None
        return value

    def intl(self, value_local, iso3, year):
        p, py = self.wb("ppp_private_consumption", iso3, year)
        return (value_local / p, py) if p else (None, None)

    def ref(self, iso3, item):
        r = self.refs[(self.refs["iso3"] == iso3) & (self.refs["item"] == item)]
        if r.empty and iso3 in EUROPE:
            r = self.refs[(self.refs["iso3"] == "_EUROPE_DEFAULT") & (self.refs["item"] == item)]
        return None if r.empty else r.iloc[0]

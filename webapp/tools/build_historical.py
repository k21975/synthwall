#!/usr/bin/env python3
"""
build_historical.py — consolidate the Tesla-app CSV exports into historical.json
for the web app to append pre-live (Feb–May 2026) daily energy.

Source: ../../Tesla Data Import/PW{1,2}-{02..05}.csv  (one month per file, per
Powerwall). Columns: Date time, Home, Vehicle, From Powerwall, Solar, From Grid,
To Grid  (all kWh; Date time is local ISO-8601 with offset).

Output: webapp/historical.json  ->  { system: [ {ts, home, solar, from_grid,
to_grid, from_pw, ev}, ... ] }  where panel_a=PW1, panel_b=PW2, combined=sum.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "Tesla Data Import"
OUT = Path(__file__).resolve().parents[1] / "historical.json"

FIELD_MAP = {
    "Home (kWh)": "home",
    "Vehicle (kWh)": "ev",
    "From Powerwall (kWh)": "from_pw",
    "Solar Energy (kWh)": "solar",
    "From Grid (kWh)": "from_grid",
    "To Grid (kWh)": "to_grid",
}
FIELDS = ("home", "solar", "from_grid", "to_grid", "from_pw", "ev")


def read_pw(prefix):
    """Return {ts_ms: {field: kWh}} for one Powerwall across its month files."""
    days = {}
    for f in sorted(SRC.glob(f"{prefix}-*.csv")):
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                dt = datetime.fromisoformat(row["Date time"])
                ts = int(dt.timestamp() * 1000)  # local midnight -> UTC epoch ms
                rec = days.setdefault(ts, {k: 0.0 for k in FIELDS})
                for col, key in FIELD_MAP.items():
                    rec[key] += float(row.get(col) or 0.0)
    return days


def to_rows(days):
    return [
        {"ts": ts, **{k: round(v, 2) for k, v in rec.items()}}
        for ts, rec in sorted(days.items())
    ]


def main():
    pw1, pw2 = read_pw("PW1"), read_pw("PW2")
    combined = {}
    for ts in set(pw1) | set(pw2):
        rec = {k: 0.0 for k in FIELDS}
        for src in (pw1.get(ts), pw2.get(ts)):
            if src:
                for k in FIELDS:
                    rec[k] += src[k]
        combined[ts] = rec

    data = {"combined": to_rows(combined), "panel_a": to_rows(pw1), "panel_b": to_rows(pw2)}
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    n = len(data["combined"])
    span = (
        datetime.utcfromtimestamp(data["combined"][0]["ts"] / 1000).date(),
        datetime.utcfromtimestamp(data["combined"][-1]["ts"] / 1000).date(),
    ) if n else (None, None)
    print(f"wrote {OUT} — {n} combined days, span {span[0]}..{span[1]}")


if __name__ == "__main__":
    main()

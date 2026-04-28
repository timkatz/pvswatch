#!/usr/bin/env python3
"""Build fixtures/history_seed.sqlite from a real solar_history.db.

Strips PII (panel inverter serials, absolute timestamps) and trims to a
configurable window. Timestamps are normalized so the most-recent row has
ts = 0 and older rows are negative — the runtime seed adds time.time()
so MAX(ts) maps to "now" at seed time, regardless of when the fixture
was captured. Run from repo root:

    scp root@<host>:/path/to/solar_history.db /tmp/src.db
    python3 scripts/build_history_fixture.py /tmp/src.db
    rm /tmp/src.db
    git diff fixtures/history_seed.sqlite

Output goes to fixtures/history_seed.sqlite (overwrites). Re-run whenever
the production DB has more days of data you want reflected in tests.
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "history_seed.sqlite",
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source_db", help="Path to a real solar_history.db (read-only).")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output fixture path.")
    ap.add_argument("--max-days", type=float, default=30.0,
                    help="Trim to the most recent N days of data (default: 30).")
    args = ap.parse_args()

    if not os.path.exists(args.source_db):
        print(f"Source DB not found: {args.source_db}", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(f"file:{args.source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    # Establish window: most-recent row → most-recent row minus max-days
    max_ts = src.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    if max_ts is None:
        print("Source DB has no readings.", file=sys.stderr)
        sys.exit(1)
    cutoff = max_ts - args.max_days * 86400

    # Stable, sorted serial → MOCKINV### mapping.
    real_serials = [r[0] for r in src.execute(
        "SELECT DISTINCT serial FROM panel_readings WHERE serial IS NOT NULL ORDER BY serial"
    ).fetchall()]
    serial_map = {s: f"MOCKINV{i:03d}" for i, s in enumerate(real_serials, start=1)}
    print(f"Anonymizing {len(serial_map)} panel serials → MOCKINV001..{len(serial_map):03d}")

    if os.path.exists(args.out):
        os.remove(args.out)
    dst = sqlite3.connect(args.out)
    # Match the runtime schema exactly (proxy.py _init_db). Keeping these in
    # sync is mandatory — if you ALTER TABLE in proxy.py, mirror it here.
    dst.executescript("""
        CREATE TABLE readings (
            ts REAL PRIMARY KEY,
            production_kw REAL, consumption_kw REAL, net_kw REAL, lifetime_kwh REAL,
            sys_v REAL, l1_v REAL, l2_v REAL, freq_hz REAL,
            pf_production REAL, pf_consumption REAL,
            num_panels INTEGER, panels_working INTEGER, panels_error INTEGER,
            battery_kw REAL, battery_soc REAL, backup_min REAL,
            battery_lifetime_kwh REAL, home_lifetime_kwh REAL, grid_lifetime_kwh REAL
        );
        CREATE TABLE panel_readings (
            ts REAL, serial TEXT, panel_model TEXT, state TEXT, watts REAL,
            v_dc REAL, i_dc REAL, v_ac REAL, temp_c REAL, lifetime_kwh REAL,
            PRIMARY KEY (ts, serial)
        );
    """)

    # Normalize timestamps relative to max_ts: newest row → 0, older → negative.
    # Removes "this fixture was captured at <date>" as a side-channel.
    n_readings = 0
    for row in src.execute("SELECT * FROM readings WHERE ts >= ?", (cutoff,)):
        d = list(row)
        d[0] = d[0] - max_ts
        dst.execute(
            "INSERT INTO readings VALUES (" + ",".join("?" * 20) + ")",
            tuple(d),
        )
        n_readings += 1

    n_panel = 0
    for row in src.execute("SELECT * FROM panel_readings WHERE ts >= ?", (cutoff,)):
        d = dict(row)
        d["ts"] = d["ts"] - max_ts
        d["serial"] = serial_map.get(d["serial"], d["serial"])
        dst.execute(
            "INSERT INTO panel_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d["ts"], d["serial"], d["panel_model"], d["state"], d["watts"],
             d["v_dc"], d["i_dc"], d["v_ac"], d["temp_c"], d["lifetime_kwh"]),
        )
        n_panel += 1

    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()

    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f"Wrote {args.out}")
    print(f"  readings={n_readings}, panel_readings={n_panel}, size={size_mb:.2f} MB")


if __name__ == "__main__":
    main()

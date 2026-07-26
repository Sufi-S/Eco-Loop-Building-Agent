"""
parse_output.py - Parse EnergyPlus eplusout.sql into a structured list of timestep dicts.

Why .sql and not .csv?
    The reference IDF (`RefBldgSmallOfficeNew2004_Chicago.idf`) has
        Output:SQLite, Simple;
    which causes EnergyPlus to write all per-time-series data to `eplusout.sql`
    instead of `eplusout.csv`. Trying to read `eplusout.csv` returns zero rows.
    The .sql file is the canonical output for this IDF.

What gets extracted:
    - timestamp: built from the Time table (Year/Month/Day/Hour)
    - zones: dict[zone_name] -> {"temperature": °C, "humidity": None}
        * Relative humidity is NOT requested in this IDF, so it is always None.
          (See note in recommended next steps below.)
    - total_power_kw: facility electricity demand (J/h) converted to kW
    - extra: a small bag of other useful variables for the agent
        (outdoor temp, fans/cooling/heating electricity, etc.) — same per-timestep

Returns a list of timestep dicts, ordered by TimeIndex.
"""

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# SQL queries — built once, reused
# ---------------------------------------------------------------------------
# Pull every reported variable/meter we care about, joined with its time row.
# We bind dictionary indices dynamically so we don't pull the whole table.
_BASE_QUERY = """
SELECT
    t.TimeIndex,
    t.Year, t.Month, t.Day, t.Hour, t.Minute,
    d.ReportDataDictionaryIndex,
    d.Name,
    d.Units,
    r.Value
FROM Time t
JOIN ReportData r ON r.TimeIndex = t.TimeIndex
JOIN reportDataDictionary d ON r.ReportDataDictionaryIndex = d.ReportDataDictionaryIndex
"""


def _load_dictionary(con):
    """Return {name: [dict_index, ...]} for every hourly row in the dictionary."""
    cur = con.cursor()
    rows = cur.execute(
        "SELECT ReportDataDictionaryIndex, Name, Units "
        "FROM reportDataDictionary "
        "WHERE ReportingFrequency = 'Hourly'"
    ).fetchall()
    by_name = {}
    for idx, name, units in rows:
        by_name.setdefault(name, []).append((idx, units))
    return by_name


def _load_zone_order(con):
    """Return zone names in EnergyPlus' internal ZoneIndex order.

    The `*` request for zone-scoped variables (e.g. `*,Zone Mean Air Temperature`)
    produces one row per zone in the same order as the `Zones` table.
    """
    cur = con.cursor()
    return [r[0] for r in cur.execute(
        "SELECT ZoneName FROM Zones ORDER BY ZoneIndex"
    ).fetchall()]


def _resolve_indices(by_name, target_name, expected_count, label):
    """Look up a variable name and verify it has the expected number of variants.

    Some variables (zone-scoped, key=$ZoneName) appear N times in the dictionary
    — once per zone. Others (facility-level) appear once. We assert the count
    so silent mismatches don't slip through.
    """
    entries = by_name.get(target_name, [])
    if not entries:
        print(f"[parse_output] WARNING: '{target_name}' ({label}) not found in dictionary.")
        return []
    if len(entries) != expected_count:
        print(f"[parse_output] NOTE: '{target_name}' has {len(entries)} entries "
              f"(expected {expected_count}). Using what's available.")
    return [idx for idx, _units in entries]


def parse_eplus_output(output_dir):
    """
    Read eplusout.sql from output_dir and extract per-timestep data.

    Args:
        output_dir (str or Path): Directory containing eplusout.sql.

    Returns:
        list of dict: Each dict corresponds to one timestep, with keys:
            "timestamp"        : ISO format datetime string
            "zones"            : dict mapping zone name -> {"temperature": °C, "humidity": %}
            "total_power_kw"   : facility electricity demand (kW)
            "extra"            : dict of other useful variables (outdoor temp, fans, etc.)
    """
    output_dir = Path(output_dir)
    sql_path = output_dir / "eplusout.sql"

    if not sql_path.exists():
        raise FileNotFoundError(f"eplusout.sql not found in {output_dir}")

    con = sqlite3.connect(str(sql_path))
    try:
        return _parse(con)
    finally:
        con.close()


def _parse(con):
    by_name = _load_dictionary(con)
    zones = _load_zone_order(con)
    n_zones = len(zones)

    # Resolve dictionary indices for the variables we want.
    temp_idx = _resolve_indices(by_name, "Zone Mean Air Temperature", n_zones, "zone temperatures")
    hum_idx = _resolve_indices(by_name, "Zone Air Relative Humidity", n_zones, "zone humidity")
    # Power: facility electricity demand (hourly integrated, in Joules)
    power_idx = _resolve_indices(by_name, "Electricity:Facility", 1, "facility electricity")
    power_idx = power_idx[0] if power_idx else None

    # Extras — useful for the agent: weather, sub-meters
    extra_names = [
        "Site Outdoor Air Drybulb Temperature",
        "Site Outdoor Air Humidity Ratio",
        "Site Outdoor Air Relative Humidity",
        "Fans:Electricity",
        "Cooling:Electricity",
        "Heating:Electricity",
        "InteriorLights:Electricity",
        "InteriorEquipment:Electricity",
        "NaturalGas:Facility",
    ]
    extra_idx = {name: _resolve_indices(by_name, name, 1, name) for name in extra_names}
    extra_idx = {k: v[0] for k, v in extra_idx.items() if v}

    # ---- Pull ALL data in one query, then sort/group in Python ----
    # This is faster than one query per timestep.
    cur = con.cursor()
    rows = cur.execute(_BASE_QUERY).fetchall()

    # Group by TimeIndex
    by_time = {}
    for time_idx, year, month, day, hour, minute, dict_idx, name, units, value in rows:
        bucket = by_time.setdefault(time_idx, {
            "ts": (year, month, day, hour, minute),
            "values": {},
        })
        bucket["values"][dict_idx] = value

    # Build the result list
    # ---------------------------------------------------------------------------
    # Timestamp generation
    #
    # EnergyPlus design-day runs have two tricky properties:
    #
    #   1. The Year column is 0 for every row (placeholder). We map it to a
    #      fixed wall-clock year for ISO-8601 output.
    #   2. hour=24 is written for the END of each design day instead of rolling
    #      to (day+1, hour=0). We roll it over.
    #
    # For multi-design-day runs (this IDF uses Winter + Summer), EnergyPlus
    # RESTARTS the day counter for each design day, e.g. Winter is Jan 21 and
    # Summer is also reported as starting on "Jan 21" (with year=0 and a
    # different month). Naively stamping both with year=2026 produces
    # timestamps like "2026-01-21..." and "2026-07-21..." which are 6 months
    # apart on a continuous time axis — that breaks any line chart.
    #
    # To get a contiguous 48-hour window we drop monthly rollup rows
    # first, then build timestamps as a simple linear sequence: row 0 is
    # the first design day's hour 1, and each subsequent row is exactly
    # one hour later (regardless of which "season" it represents). The
    # wall-clock year/month/day is only correct for the FIRST design day;
    # rows 24+ are stamped on subsequent days of the same month for
    # plotting convenience.
    # ---------------------------------------------------------------------------
    sorted_indices = sorted(by_time.keys())
    timestamps = []  # built in lock-step with sorted_indices

    # First pass: roll over hour=24 and stamp year=0 -> 2026 for every row.
    # We also drop monthly rollup rows here so they don't trip the boundary
    # detection below. Rollup rows in this IDF look like (year=0, day=31,
    # hour=24, minute=0) — EnergyPlus writes them for monthly aggregation
    # and they don't carry hourly zone data (the downstream filter would
    # drop them later, but we want them gone NOW so the (year, month)
    # boundary detection below fires exactly once, at the real design-day
    # transition).
    raw_tuples = []
    raw_time_indices = []  # SQL TimeIndex for each raw_tuple, parallel list
    for i, time_idx in enumerate(sorted_indices):
        bucket = by_time[time_idx]
        y, mo, d, h, mi = bucket["ts"]
        # Skip monthly rollup rows: (year=0, day=31, hour=24, minute=0)
        if y == 0 and d == 31 and h == 24 and mi == 0:
            continue
        if y == 0:
            y = 2026
        if h == 24:
            h = 0
            d += 1
            # Cheap month rollover for design days we care about (max 31 days)
            if d > 31:
                d = 1
                mo += 1
        raw_tuples.append((y, mo, d, h, mi))
        raw_time_indices.append(time_idx)

    # Second pass: build ISO timestamps. Each row is exactly 1 hour after
    # the previous row, so wall-clock is simply `start_time + i hours`.
    # The start time is the FIRST design day's date (any month/day works as
    # long as it's consistent — the two design days are different "seasons"
    # but we want a single contiguous 48-hour timeline for plotting).
    if raw_tuples:
        anchor_y, anchor_mo, anchor_d, anchor_h, _ = raw_tuples[0]
        # Anchor at (date, hour=0) so the very first row's hour `anchor_h`
        # is correctly offset. We then add `i` hours per row.
        from datetime import datetime, timedelta
        anchor_dt = datetime(anchor_y, anchor_mo, anchor_d, 0, 0)
        for i, _ in enumerate(raw_tuples):
            dt = anchor_dt + timedelta(hours=i + anchor_h)
            timestamps.append(dt.strftime("%Y-%m-%dT%H:%M:%S"))
    else:
        timestamps = []

    # Third pass: actually build the result rows
    # NOTE: zip against raw_time_indices (NOT sorted_indices) because we
    # dropped rollup rows in the first pass. sorted_indices still contains
    # them, so zip(sorted_indices, timestamps) would lose the last hourly
    # row of the second design day.
    results = []
    for time_idx, timestamp in zip(raw_time_indices, timestamps):
        bucket = by_time[time_idx]
        _, _, _, _, _ = bucket["ts"]
        values = bucket["values"]

        # --- per-zone temperatures + humidity ---
        zone_data = {}
        for zone_i, zone_name in enumerate(zones):
            t = None
            h = None
            if zone_i < len(temp_idx):
                raw = values.get(temp_idx[zone_i])
                try:
                    t = float(raw)
                except (TypeError, ValueError):
                    t = None
            if zone_i < len(hum_idx):
                raw = values.get(hum_idx[zone_i])
                try:
                    h = float(raw)
                except (TypeError, ValueError):
                    h = None
            zone_data[zone_name] = {
                "temperature": t,
                "humidity": h,  # None if not requested in IDF
            }

        # --- total power (J/h -> kW) ---
        total_power_kw = None
        if power_idx is not None:
            try:
                total_power_kw = float(values[power_idx]) / 3600.0
            except (TypeError, ValueError, KeyError):
                total_power_kw = None

        # --- extras (also convert J/h meters to kW where units are 'J') ---
        extra = {}
        for name, idx in extra_idx.items():
            v = values.get(idx)
            try:
                v = float(v)
            except (TypeError, ValueError):
                extra[name] = None
                continue
            # If the unit is Joules (hourly integrated), convert to kW
            units = by_name.get(name, [[None, ""]])[0][1]
            if units == "J":
                v = v / 3600.0
            extra[name] = v

        results.append({
            "timestamp": timestamp,
            "zones": zone_data,
            "total_power_kw": total_power_kw,
            "extra": extra,
        })

    # All remaining rows are hourly design-day rows (rollups were filtered
    # out in the first pass, so the downstream "drop rows without zone
    # temperature" filter is now redundant — but we keep it as a defensive
    # backstop in case a future IDF has a different rollup shape).
    hourly = [
        r for r in results
        if any(info.get("temperature") is not None for info in r["zones"].values())
    ]
    return hourly if hourly else results


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    output_dir = project_root / "energyplus" / "output"

    try:
        data = parse_eplus_output(output_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Parsed {len(data)} timesteps.\n")
    # Show first 3 timesteps
    for i, ts_data in enumerate(data[:3]):
        print(f"Timestep {i+1}:")
        print(json.dumps(ts_data, indent=2, default=str))
        print("-" * 40)

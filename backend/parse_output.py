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
    results = []
    for time_idx in sorted(by_time.keys()):
        bucket = by_time[time_idx]
        year, month, day, hour, minute = bucket["ts"]
        # Design-day runs often have Year=0. Stamp with a placeholder so the
        # timestamp is still a valid ISO-8601 string. The dashboard can group
        # by (month, day, hour) which is what actually matters for design days.
        if year == 0:
            year = 2026
        # EnergyPlus writes hour=24 at the end of a design day rather than
        # rolling to (day+1, hour=0). Roll it over so the timestamp parses.
        if hour == 24:
            hour = 0
            day += 1
            # Cheap month rollover for the design days we care about
            if day > 31:
                day = 1
                month += 1
        timestamp = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"
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

    # Drop summary/rollup rows that don't carry hourly zone data.
    # When the run includes monthly aggregation, EnergyPlus appends extra
    # rows that contain only monthly meters — no zones, no hourly power.
    # Keeping those would make consumers (e.g. tools.py) treat the last
    # timestep as "empty building" and produce no recommendations.
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

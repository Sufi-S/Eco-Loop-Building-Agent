"""
loop.py - Main orchestration for the closed-loop optimization.

Performs:
- Baseline simulation (unmodified IDF) and saves results to sample_results/before.csv
- Inspects IDF thermostats to understand structure (currently the only action in __main__)
- (Placeholder) Modify IDF based on agent recommendations and run optimized simulation
- Compares energy consumption and prints savings
"""

import shutil
import re
from pathlib import Path
import pandas as pd

# Import project modules
from run_simulation import run_simulation
from parse_output import parse_eplus_output
from agent import BuildingAgent

# ---------- PATHS ----------
PROJECT_ROOT = Path(__file__).parent.parent
ENERGYPLUS_DIR = PROJECT_ROOT / "energyplus"
SAMPLE_RESULTS_DIR = PROJECT_ROOT / "sample_results"

IDF_PATH = ENERGYPLUS_DIR / "RefBldgSmallOfficeNew2004_Chicago.idf"
EPW_PATH = ENERGYPLUS_DIR / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
BASELINE_OUTPUT_DIR = ENERGYPLUS_DIR / "output_baseline"
OPTIMIZED_OUTPUT_DIR = ENERGYPLUS_DIR / "output_optimized"
MODIFIED_IDF_PATH = ENERGYPLUS_DIR / "optimized_building.idf"

# Ensure sample_results exists
SAMPLE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------- HELPER: INSPECT IDF THERMOSTATS ----------
def inspect_idf_thermostats(idf_path: Path) -> None:
    """
    Scan the IDF file for thermostat-related objects and print their structure.

    Searches for:
      - ZoneControl:Thermostat
      - ThermostatSetpoint:DualSetpoint
      - Schedule:Compact (often used for setpoint schedules)
      - Schedule:Constant
      - Schedule:Day:Interval, etc.

    Prints object names and the schedule references they use.
    """
    if not idf_path.exists():
        print(f"ERROR: IDF file not found at {idf_path}")
        return

    with open(idf_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split into objects separated by semicolons (EnergyPlus IDF format)
    # Objects are like: ObjectType, ... ; (with semicolon at end)
    # We'll use a simple regex to find each object block ending with ;
    # But careful: semicolons can appear in comments. We'll ignore comments for simplicity.
    # We'll split by ; and then for each chunk, if it starts with recognized type, we print.

    # Remove comments (lines starting with !)
    lines = content.splitlines()
    clean_lines = []
    for line in lines:
        # Remove everything after ! (comment)
        if '!' in line:
            line = line[:line.index('!')]
        if line.strip():
            clean_lines.append(line)
    clean_content = '\n'.join(clean_lines)

    # Now split by semicolon (object boundaries)
    raw_objects = clean_content.split(';')
    # Remove empty
    raw_objects = [obj.strip() for obj in raw_objects if obj.strip()]

    # Patterns to match
    thermostat_patterns = [
        r'^\s*ZoneControl:Thermostat\s*,\s*([^,]+)',
        r'^\s*ThermostatSetpoint:DualSetpoint\s*,\s*([^,]+)',
        r'^\s*Schedule:Compact\s*,\s*([^,]+)',
        r'^\s*Schedule:Constant\s*,\s*([^,]+)',
        r'^\s*Schedule:Day:Interval\s*,\s*([^,]+)',
    ]
    # Also look for Schedule:Year, Schedule:Week, etc.

    print("=" * 60)
    print(f"INSPECTING THERMOSTAT OBJECTS IN: {idf_path}")
    print("=" * 60)

    found_count = 0
    for obj in raw_objects:
        # Check each pattern
        for pattern in thermostat_patterns:
            match = re.match(pattern, obj, re.IGNORECASE)
            if match:
                found_count += 1
                obj_type = pattern.split(',')[0].strip()  # e.g., "ZoneControl:Thermostat"
                obj_name = match.group(1).strip()
                print(f"\n--- {obj_type} ---")
                print(f"Name: {obj_name}")
                # Print the full object content (first few lines)
                lines_obj = obj.split(',')
                # Print each field with line numbers
                for i, field in enumerate(lines_obj):
                    print(f"  Field {i}: {field.strip()}")
                break  # only one type per object

    if found_count == 0:
        print("No thermostat-related objects found (or unrecognized pattern).")
        print("Please check the IDF manually to identify the correct object names.")
    else:
        print(f"\nFound {found_count} thermostat-related objects.")

    # Also list all Zone objects to see which zones exist
    print("\n--- ZONE OBJECTS ---")
    zone_pattern = r'^\s*Zone\s*,\s*([^,]+)'
    for obj in raw_objects:
        match = re.match(zone_pattern, obj, re.IGNORECASE)
        if match:
            zone_name = match.group(1).strip()
            print(f"Zone: {zone_name}")

    print("=" * 60)


# ---------- BASELINE RUN ----------
def run_baseline() -> Path:
    """
    Run the original IDF and save simplified results to sample_results/before.csv.

    Returns:
        Path to the saved CSV file.
    """
    print("--- Running baseline simulation ---")
    success = run_simulation(IDF_PATH, EPW_PATH, BASELINE_OUTPUT_DIR)
    if not success:
        raise RuntimeError("Baseline simulation failed.")

    # Parse the output
    data = parse_eplus_output(BASELINE_OUTPUT_DIR)
    if not data:
        raise ValueError("No timestep data from baseline output.")

    # Convert to DataFrame
    rows = []
    for ts in data:
        row = {
            "timestamp": ts["timestamp"],
            "total_power_kw": ts.get("total_power_kw")
        }
        # Add zone temperatures if desired (optional)
        for zone, info in ts.get("zones", {}).items():
            row[f"temp_{zone}"] = info.get("temperature")
            row[f"hum_{zone}"] = info.get("humidity")
        rows.append(row)

    df = pd.DataFrame(rows)
    out_path = SAMPLE_RESULTS_DIR / "before.csv"
    df.to_csv(out_path, index=False)
    print(f"Baseline results saved to {out_path}")
    return out_path


# ---------- MODIFICATION LOGIC (UPDATED) ----------
def modify_idf_based_on_decisions(
    decisions: dict,
    low_priority_zones: list,
    original_idf: Path,
    output_idf: Path
) -> None:
    """
    Apply agent's decisions to the IDF by adjusting global heating/cooling setpoint schedules.

    The IDF uses two shared schedules:
      - CLGSETP_SCH (cooling setpoint): occupied value 24.0°C, unoccupied 26.7°C
      - HTGSETP_SCH (heating setpoint): occupied value 21.0°C, unoccupied 15.6°C

    This function aggregates per-zone decisions into one global action:
      - If any occupied zone requests "cool" -> raise cooling setpoint +1.0°C (24.0 -> 25.0)
      - If any occupied zone requests "heat" -> lower heating setpoint -1.0°C (21.0 -> 20.0)
      - If only "reduce_conditioning" appears (and no cool/heat) -> widen deadband:
          cooling 24.0 -> 24.5, heating 21.0 -> 20.5
      - If both cool and heat appear -> majority wins (count zones)
      - Else no changes

    Args:
        decisions: dict from agent.decide()["recommendations"] mapping zone -> {action, reasoning}
        low_priority_zones: list of zones considered unoccupied (from agent result)
        original_idf: path to the baseline IDF
        output_idf: path where the modified IDF will be written
    """
    print("\n--- Modifying IDF based on agent decisions ---")

    # ---- Step 1: Aggregate decisions ----
    # Count actions per zone, but only for occupied zones (not in low_priority_zones)
    action_counts = {"cool": 0, "heat": 0, "maintain": 0, "reduce_conditioning": 0}
    occupied_zones = [z for z in decisions.keys() if z not in low_priority_zones]

    for zone in occupied_zones:
        action = decisions[zone].get("action", "maintain")
        if action in action_counts:
            action_counts[action] += 1
        else:
            action_counts["maintain"] += 1

    # Determine global action
    global_action = None
    adjust_cooling = None  # new setpoint value for cooling (or None)
    adjust_heating = None  # new setpoint value for heating (or None)

    # If both cool and heat exist, pick the one with more occupied zones
    if action_counts["cool"] > 0 and action_counts["heat"] > 0:
        if action_counts["cool"] >= action_counts["heat"]:
            global_action = "cool"
        else:
            global_action = "heat"
    elif action_counts["cool"] > 0:
        global_action = "cool"
    elif action_counts["heat"] > 0:
        global_action = "heat"
    elif action_counts["reduce_conditioning"] > 0:
        global_action = "widen_deadband"
    else:
        global_action = "maintain"

    # Compute new setpoints
    original_cooling = 24.0
    original_heating = 21.0
    new_cooling = original_cooling
    new_heating = original_heating

    if global_action == "cool":
        new_cooling = original_cooling + 1.0
        print(f"Global action: Cooling required for {action_counts['cool']} occupied zone(s).")
    elif global_action == "heat":
        new_heating = original_heating - 1.0
        print(f"Global action: Heating required for {action_counts['heat']} occupied zone(s).")
    elif global_action == "widen_deadband":
        new_cooling = original_cooling + 0.5
        new_heating = original_heating - 0.5
        print("Global action: Widen deadband (reduce conditioning) for unoccupied zones.")
    else:
        print("No changes needed (all zones maintain).")
        # Still copy file unchanged
        shutil.copy2(original_idf, output_idf)
        return

    # ---- Step 2: Read and modify the IDF text ----
    with open(original_idf, 'r', encoding='utf-8') as f:
        content = f.read()

    # Helper to replace numbers inside a Schedule:Compact object
    def replace_in_schedule(obj_name: str, search_val: float, replace_val: float) -> str:
        # Find the object block starting with "Schedule:Compact, <obj_name>" and ending with ";"
        # Using regex with DOTALL to match across lines
        pattern = re.compile(
            r'(Schedule:Compact\s*,\s*' + re.escape(obj_name) + r'.*?)(?=;)',
            re.IGNORECASE | re.DOTALL
        )
        match = pattern.search(content)
        if not match:
            print(f"WARNING: Schedule {obj_name} not found. No changes made.")
            return content

        obj_block = match.group(1)
        # Replace all occurrences of the exact search value (as float string like "24.0")
        # We'll replace both "24.0" and "24" etc. But we know the values are "24.0" and "21.0".
        # We'll replace the exact string representation with the new value as a string with one decimal.
        old_str = f"{search_val:.1f}"
        new_str = f"{replace_val:.1f}"
        new_obj_block = obj_block.replace(old_str, new_str)
        # Replace the old block with the new one in content
        return content.replace(obj_block, new_obj_block, 1)

    # Apply cooling adjustment if changed
    if new_cooling != original_cooling:
        content = replace_in_schedule("CLGSETP_SCH", original_cooling, new_cooling)
        print(f"  Cooling setpoint: {original_cooling:.1f}°C -> {new_cooling:.1f}°C")

    # Apply heating adjustment if changed
    if new_heating != original_heating:
        content = replace_in_schedule("HTGSETP_SCH", original_heating, new_heating)
        print(f"  Heating setpoint: {original_heating:.1f}°C -> {new_heating:.1f}°C")

    # Write modified IDF
    with open(output_idf, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"Modified IDF written to {output_idf}")


# ---------- OPTIMIZED RUN ----------
def run_optimized(agent_result: dict) -> Path:
    """
    Create modified IDF based on agent decisions, run simulation, and save results.

    Args:
        agent_result: full dict from agent.decide() containing recommendations and low_priority_zones

    Returns:
        Path to the saved CSV (after.csv).
    """
    print("--- Running optimized simulation ---")
    # Modify the IDF
    modify_idf_based_on_decisions(
        decisions=agent_result["recommendations"],
        low_priority_zones=agent_result["low_priority_zones"],
        original_idf=IDF_PATH,
        output_idf=MODIFIED_IDF_PATH
    )

    # Run the modified IDF
    success = run_simulation(MODIFIED_IDF_PATH, EPW_PATH, OPTIMIZED_OUTPUT_DIR)
    if not success:
        raise RuntimeError("Optimized simulation failed.")

    # Parse results
    data = parse_eplus_output(OPTIMIZED_OUTPUT_DIR)
    if not data:
        raise ValueError("No timestep data from optimized output.")

    # Convert to DataFrame (same structure as before)
    rows = []
    for ts in data:
        row = {
            "timestamp": ts["timestamp"],
            "total_power_kw": ts.get("total_power_kw")
        }
        for zone, info in ts.get("zones", {}).items():
            row[f"temp_{zone}"] = info.get("temperature")
            row[f"hum_{zone}"] = info.get("humidity")
        rows.append(row)

    df = pd.DataFrame(rows)
    out_path = SAMPLE_RESULTS_DIR / "after.csv"
    df.to_csv(out_path, index=False)
    print(f"Optimized results saved to {out_path}")
    return out_path


# ---------- ENERGY COMPARISON ----------
def compare_energy(before_csv: Path, after_csv: Path) -> None:
    """
    Load before and after CSVs, sum total_power_kw, and print savings.
    """
    df_before = pd.read_csv(before_csv)
    df_after = pd.read_csv(after_csv)

    total_before = df_before["total_power_kw"].sum()
    total_after = df_after["total_power_kw"].sum()

    savings = total_before - total_after
    savings_pct = (savings / total_before) * 100 if total_before != 0 else 0

    print("\n=== ENERGY COMPARISON ===")
    print(f"Baseline total energy: {total_before:.2f} kWh (assuming kW * timestep count)")
    print(f"Optimized total energy: {total_after:.2f} kWh")
    print(f"Energy savings: {savings:.2f} kWh ({savings_pct:.1f}%)")
    print("=========================\n")


# ---------- MAIN ORCHESTRATION ----------
def run_full_loop(agent_mode: str = "mock"):
    """
    Run the entire closed loop:
    1. Baseline simulation
    2. Agent decision (mock or bedrock)
    3. Modified IDF creation and simulation
    4. Comparison
    """
    # Step 1: Baseline
    before_csv = run_baseline()

    # Step 2: Agent decision
    agent = BuildingAgent(mode=agent_mode)
    decision_result = agent.decide()
    print("\nAgent decision summary:")
    print(f"Mode: {decision_result['agent_mode']}")
    print(f"Explanation: {decision_result['agent_explanation']}")

    # Step 3: Optimized simulation
    after_csv = run_optimized(decision_result)

    # Step 4: Compare
    compare_energy(before_csv, after_csv)


# ---------- MAIN ----------
if __name__ == "__main__":
    # For initial inspection, we could still run this to verify structure
    # But now we are ready to run the full loop. We'll still run the inspector
    # once to confirm, then run the loop.
    inspect_idf_thermostats(IDF_PATH)

    # Now run the full optimization with mock agent
    run_full_loop(agent_mode="mock")   # change to "bedrock" later
"""
run_simulation.py - Execute EnergyPlus simulation from Python.

Hardcodes EnergyPlus executable path (configurable at top).
Uses subprocess to run: energyplus.exe -w <epw> -d <output_dir> <idf>
Checks return code and verifies expected output files.
"""

import subprocess
import sys
from pathlib import Path

# ---------- CONFIGURATION ----------
ENERGYPLUS_EXE = Path(r"C:\EnergyPlusV26-1-0\energyplus.exe")
# -----------------------------------

def run_simulation(idf_path, epw_path, output_dir):
    """
    Run EnergyPlus simulation.

    Args:
        idf_path (str or Path): Path to the IDF file.
        epw_path (str or Path): Path to the EPW weather file.
        output_dir (str or Path): Directory where EnergyPlus writes outputs.

    Returns:
        bool: True if simulation succeeded and expected outputs exist, else False.
    """
    # Convert to Path objects for easier handling
    idf_path = Path(idf_path)
    epw_path = Path(epw_path)
    output_dir = Path(output_dir)

    # Validate existence of executable and input files
    if not ENERGYPLUS_EXE.exists():
        print(f"ERROR: EnergyPlus executable not found at {ENERGYPLUS_EXE}")
        return False
    if not idf_path.exists():
        print(f"ERROR: IDF file not found at {idf_path}")
        return False
    if not epw_path.exists():
        print(f"ERROR: EPW file not found at {epw_path}")
        return False

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build command
    cmd = [
        str(ENERGYPLUS_EXE),
        "-w", str(epw_path),
        "-d", str(output_dir),
        str(idf_path)
    ]

    print(f"Running EnergyPlus: {' '.join(cmd)}")
    try:
        # Run the simulation and capture output
        result = subprocess.run(
            cmd,
            cwd=str(output_dir),        # EnergyPlus writes auxiliary files to cwd by default
            capture_output=True,
            text=True,
            timeout=600                 # 10 minute timeout (adjust as needed)
        )
    except subprocess.TimeoutExpired:
        print("ERROR: Simulation timed out after 600 seconds.")
        return False
    except Exception as e:
        print(f"ERROR: Failed to run EnergyPlus: {e}")
        return False

    # Check return code
    if result.returncode != 0:
        print(f"ERROR: EnergyPlus returned non-zero exit code {result.returncode}")
        # Print stderr for diagnostics
        if result.stderr:
            print("stderr:")
            print(result.stderr[:500])   # show first 500 chars
        return False

    print("EnergyPlus finished successfully.")

    # Verify expected output files exist
    csv_path = output_dir / "eplusout.csv"
    sql_path = output_dir / "eplusout.sql"
    err_path = output_dir / "eplusout.err"

    # Success criteria:
    #   1. eplusout.err must exist (every successful EnergyPlus run writes it).
    #   2. At least one of eplusout.csv or eplusout.sql must exist.
    #      Some IDFs (incl. the project's reference model) use Output:SQLite,
    #      which writes the time series to .sql instead of .csv.
    if not err_path.exists():
        print(f"ERROR: Missing eplusout.err — simulation likely failed.")
        return False

    if not csv_path.exists() and not sql_path.exists():
        print(f"ERROR: Neither eplusout.csv nor eplusout.sql found in {output_dir}.")
        return False

    if csv_path.exists():
        print(f"Output files verified: {csv_path.name}, {err_path.name}")
    else:
        # .sql-only output — completely fine when the IDF uses Output:SQLite.
        print(f"Output file: {sql_path.name} (SQLite-only output, no .csv).")
    return True


if __name__ == "__main__":
    # Example usage with relative paths (assuming script is in backend/)
    # Adjust if you run from different directory.
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    idf = project_root / "energyplus" / "RefBldgSmallOfficeNew2004_Chicago.idf"
    epw = project_root / "energyplus" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
    out = project_root / "energyplus" / "output"

    success = run_simulation(idf, epw, out)
    sys.exit(0 if success else 1)

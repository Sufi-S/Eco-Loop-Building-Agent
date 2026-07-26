# Eco-Loop Building Agents — Architecture

## 1. Overview

Eco-Loop Building Agents is a closed-loop building energy optimization system that combines EnergyPlus building simulation with an LLM-driven control agent. The system runs a baseline simulation, asks an AI agent to recommend HVAC setpoint adjustments based on observed zone conditions, modifies the EnergyPlus model, re-simulation, and reports the energy savings — all within a single Python pipeline. The agent operates in a Model-Context-Protocol-style tool loop, exposing building state and control actions as structured tools that the LLM can call.

## 2. Architecture Diagram

```
                          ┌─────────────────────────────────────────┐
                          │           CLOSED-LOOP CYCLE             │
                          └─────────────────────────────────────────┘

  ┌──────────────┐    subprocess    ┌──────────────────┐
  │  EnergyPlus  │◄────────────────┤  Python          │
  │  (e+ binary) │  stdout/err     │  Controller      │
  │              │────────────────►│                  │
  │  RefBldg     │  eplusout.sql   │  run_simulation  │
  │  Small Office│                 │  parse_output    │
  │  6 zones     │                 │                  │
  │  Chicago TMY │                 └────────┬─────────┘
  └──────┬───────┘                          │
         │                                  │ structured JSON
         │                                  ▼
         │                       ┌──────────────────────┐
         │                       │  MCP-style Tool      │
         │                       │  Layer (tools.py)    │
         │                       │                      │
         │                       │  BuildingTools       │
         │                       │  • get_building_state│
         │                       │  • get_zone_temps    │
         │                       │  • identify_empty    │
         │                       │  • suggest_setpoint  │
         │                       │  • get_full_recommend│
         │                       └────────┬─────────────┘
         │                                │ tool call/result
         │                                ▼
         │                       ┌──────────────────────┐
         │                       │   AI Agent           │
         │                       │   (agent.py)         │
         │                       │                      │
         │                       │  BuildingAgent       │
         │                       │  ┌────────────────┐  │
         │                       │  │ Mock mode      │  │
         │                       │  │ (rule-based)   │  │
         │                       │  └────────────────┘  │
         │                       │  ┌────────────────┐  │
         │                       │  │ AWS Bedrock    │  │
         │                       │  │ Claude Sonnet  │  │
         │                       │  │ 4.6            │  │
         │                       │  └────────────────┘  │
         │                       └────────┬─────────────┘
         │                                │ per-zone actions
         │                                ▼
         │                       ┌──────────────────────┐
         │  IDF modification    │  Closed-Loop         │
         │  (setpoint schedule) │  Orchestrator        │
         │                       │  (loop.py)           │
         │                       │                      │
         │                       │  baseline → decide → │
         │                       │  modify → re-sim →   │
         │                       │  compare             │
         └───────────────────────┤                      │
                                 └────────┬─────────────┘
                                          │
                                          ▼
                                 ┌──────────────────────┐
                                 │  Dashboard           │
                                 │  (Streamlit+Plotly)  │
                                 │                      │
                                 │  baseline vs opt     │
                                 │  energy & temps      │
                                 └──────────────────────┘
```

## 3. Component Breakdown

### EnergyPlus — Building Model
- **Files**: `energyplus/RefBldgSmallOfficeNew2004_Chicago.idf` + `energyplus/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw`
- **Building**: DOE reference small office, 6 zones (1 core + 5 perimeter), single-story
- **Climate**: Chicago TMY weather file
- **What it simulates**: hourly zone temperatures, HVAC energy consumption (electricity + gas), setpoint schedules, equipment loads, and occupancy
- **Output**: `eplusout.sql` SQLite database — the IDF declares `Output:SQLite, Simple;`, so all time-series data lands in the SQL file (no `.csv` is produced)
- **Run via**: command-line `energyplus.exe` (path hardcoded at the top of `run_simulation.py` as `C:\EnergyPlusV26-1-0\energyplus.exe`), invoked from Python with `subprocess.run()`

### Python Controller — `backend/run_simulation.py` + `backend/parse_output.py`
- **`run_simulation.py`**: wraps the EnergyPlus binary in a Python subprocess with a 10-minute timeout; verifies success by checking for `eplusout.err` plus either `eplusout.csv` or `eplusout.sql` (this IDF uses SQLite output, so it's the `.sql` that matters)
- **`parse_output.py`**: opens `eplusout.sql` directly via Python's `sqlite3` (no EnergyPlus Python API needed), joins the `Time`, `ReportData`, and `reportDataDictionary` tables, and returns a list of per-timestep dicts. It handles the **hour-24 → hour-0** boundary rollover across days (EnergyPlus writes hour=24 at the end of a design day rather than rolling to (day+1, hour=0)), and a cheap month rollover when needed. It also extracts zone air temperature, total facility power (converted from J/h to **kW** by dividing by 3600), and a bag of useful extras (outdoor temp, fans/cooling/heating electricity, interior lights, equipment, natural gas)
- Returns a list of `{timestamp, zones: {zone_name: {temperature, humidity}}, total_power_kw, extra}` dicts
- Note: zone relative humidity is **always None** because the IDF doesn't request `Zone Air Relative Humidity` as a report variable — only temperature is available
- This structured list is the single source of truth consumed by every downstream layer

### MCP-style Tool Layer — `backend/tools.py`
- **`BuildingTools` class** exposes five tools that the agent can call (mirrors the Model-Context-Protocol pattern of discrete, callable capabilities):
  - `get_building_state()` → `dict` with `timestamp`, `zones`, `total_power_kw`, `extra`
  - `get_zone_temperatures() -> Dict[str, float]` — mapping of zone name → latest temperature (°C)
  - `identify_empty_or_low_priority_zones(occupancy_hint=None) -> List[str]` — returns zones considered unoccupied; if no hint is provided, any zone containing `"ATTIC"` is treated as unoccupied (everything else occupied)
  - `suggest_setpoint_adjustment(zone_name, current_temp, is_occupied) -> Dict[str, Any]` — returns `{"zone", "action", "reasoning"}` where `action` is one of `"cool"`, `"heat"`, `"maintain"`, or `"reduce_conditioning"`. Rule: occupied zones with temp > 24 °C → `cool`, < 20 °C → `heat`, in between → `maintain`; unoccupied zones → `reduce_conditioning`
  - `get_full_recommendation(occupancy_hint=None) -> Dict[str, Any]` — bundles state + low-priority zones + per-zone recommendations into one decision-ready payload
- Tools cache the latest parsed timestep in `self._cached_data` to avoid re-parsing within the same object lifecycle; otherwise they're stateless wrappers

### AI Agent — `backend/agent.py`
- **`BuildingAgent` class** with **dual-mode** operation:
  - **Mock mode**: rule-based logic. Returns the same per-zone recommendations that `BuildingTools.get_full_recommendation()` produces, plus a templated natural-language summary
  - **Bedrock mode** (what `loop.py` runs by default): real LLM calls to **AWS Bedrock** with **Claude Sonnet 4.6** via `boto3.client("bedrock-runtime", region_name="us-east-1")`. Model ID `us.anthropic.claude-sonnet-4-6` and region are **hardcoded** in `agent.py` — there is no `BEDROCK_MODEL_ID` env var. If the Bedrock call fails for any reason, the agent silently falls back to mock mode and reports `"agent_mode": "mock (fallback)"`
- **Bedrock auth**: bearer-token via the `AWS_BEARER_TOKEN_BEDROCK` env var. `boto3` picks this up automatically — it's not passed explicitly into `boto3.client()`
- **Reasoning pattern**: the agent receives the `get_full_recommendation()` payload, builds a prompt describing each zone's temperature and occupancy, and asks the LLM to return JSON. It then extracts JSON (handles markdown code fences) and merges with the rule-based fallback for any missing zones. Returns:
  ```json
  {"timestamp": "...", "total_power_kw": 12.3,
   "recommendations": {"Core_ZN": {"action": "maintain", "reasoning": "..."}, ...},
   "low_priority_zones": [...], "agent_explanation": "...",
   "agent_mode": "mock" | "bedrock" | "mock (fallback)"}
  ```
- Each action ships with a natural-language `reasoning` field for explainability — judges can read the LLM's reasoning, not just the action label

### Closed-Loop Orchestrator — `backend/loop.py`
1. **Baseline run**: `run_simulation()` on the original IDF → `parse_eplus_output()` → flatten into a pandas DataFrame → save as `sample_results/before.csv` (columns: `timestamp`, `total_power_kw`, `temp_<zone>`, `hum_<zone>`)
2. **Agent decision**: invoke `BuildingAgent(mode="bedrock")`. `decide()` returns the recommendations + low-priority zones + explanation. **If Bedrock fails or the env var is missing, it falls back to mock mode automatically**
3. **IDF modification**: aggregate per-zone actions into one global setpoint schedule change via **majority-vote** on the action categories (see §4 below)
4. **Re-simulation**: write the modified IDF to `energyplus/optimized_building.idf`, re-run EnergyPlus into `energyplus/output_optimized/`, parse, save as `sample_results/after.csv`
5. **Comparison**: `pandas` reads both CSVs, sums `total_power_kw`, prints baseline / optimized / savings kWh + percentage
- **Note**: `__main__` also calls `inspect_idf_thermostats()` first, which prints every `ZoneControl:Thermostat`, `ThermostatSetpoint:DualSetpoint`, `Schedule:Compact`, and `Zone` object found in the IDF — useful for verifying the schedule topology before relying on it

### Dashboard — `dashboard/app.py`
- **Stack**: Streamlit + Plotly
- **Views**:
  - **Three KPI tiles**: baseline total kWh, optimized total kWh (with delta), savings kWh (with %)
  - **Total-power chart**: hourly `total_power_kw` overlay (baseline blue vs optimized green)
  - **Zone temperature charts**: two separate line charts for `CORE_ZN` and `PERIMETER_ZN_1`, baseline dashed vs optimized solid
  - **What changed** expander: currently a hardcoded summary ("Cooling setpoint raised from 24.0°C to 25.0°C…") with a note that this will be made dynamic later
- Run with: `streamlit run dashboard/app.py` from the repo root

## 4. Key Design Decisions

### Why mock mode exists
The hackathon sandbox throttled AWS Bedrock access in time-boxed windows, and offline development is impossible without an LLM key. The mock mode implements a deterministic rule-based recommender that produces sensible, reproducible actions, so the rest of the closed loop (parse → decide → modify → re-sim → compare) can be exercised end-to-end without AWS. The agent interface is identical in both modes, so switching to Bedrock is a single env-var flip.

### Why setpoint schedules are shared across zones
On inspecting `RefBldgSmallOfficeNew2004_Chicago.idf`, the cooling and heating setpoint schedules (`CLGSETP_SCH`, `HTGSETP_SCH`) are **single global `Schedule:Compact` objects referenced by all six zone thermostats**, not per-zone schedules. Modifying them in place affects every zone simultaneously. Because per-zone control would require restructuring the schedule topology, the orchestrator aggregates the agent's per-zone actions into a **single global action** using a **majority-vote rule** on the action categories (`cool`, `heat`, `maintain`, `reduce_conditioning`):

1. Only **occupied zones** (i.e., zones NOT in the agent's `low_priority_zones`) count toward the vote
2. If both `cool` and `heat` are present, the action with the **higher occupied-zone count wins**
3. Otherwise: `cool` → raise `CLGSETP_SCH` by **+1.0 °C**; `heat` → lower `HTGSETP_SCH` by **-1.0 °C**
4. If only `reduce_conditioning` appears → **widen the deadband by ±0.5 °C** (cooling +0.5, heating -0.5)
5. If all zones say `maintain` → no changes; the IDF is copied unchanged

This is honest about the IDF's structure — per-zone control is **not** implemented by averaging deltas (we don't have zone-by-zone schedule objects to write to). The schedule text edit is done with a regex on the `Schedule:Compact` block, replacing the literal value (e.g. `"24.0"`) with the new value.

### Bedrock API key auth via `.env`
For a hackathon, full IAM role-based Bedrock auth (instance profile, `boto3` credential chain, VPC endpoint policies) is overkill. `agent.py` reads `AWS_BEARER_TOKEN_BEDROCK` from `backend/.env` via `python-dotenv` and `boto3` picks it up automatically — it's not passed explicitly. Note that the **model ID and region are hardcoded** in `agent.py` (`region_name="us-east-1"`, `model_id="us.anthropic.claude-sonnet-4-6"`), so the only env var that matters is the bearer token. The `.env` is gitignored; `backend/.env.example` ships with a placeholder so reviewers know the expected key name.

## 5. Results

Computed from `sample_results/before.csv` and `sample_results/after.csv` (sum of `total_power_kw`):

| Metric | Baseline | Optimized | Delta |
|---|---|---|---|
| Total energy (kWh) | 518,798.27 | 488,552.72 | **-30,245.55** |
| Savings % | — | — | **5.83%** |
| Core zone avg temp (°C) | 22.84 | 23.18 | +0.34 |
| Perimeter_ZN_1 avg temp (°C) | 22.76 | 23.09 | +0.33 |

The savings come from the majority-vote aggregation resolving to `cool` (cooling setpoint was raised from 24.0 °C → 25.0 °C in `CLGSETP_SCH`). Average zone temperatures drift up by ~0.3 °C across the board — well within the ASHRAE 55 acceptable range — and the deadband widening is the main lever. The 5.8% figure is what `loop.py` prints from `compare_energy()` and what the dashboard shows in the "Energy Savings" tile.

## 6. Future Improvements

- **Per-zone independent thermostats**: rewrite the IDF to use zone-specific schedule objects instead of shared global schedules; this would let the agent's per-zone actions apply independently rather than being averaged
- **Live occupancy sensors**: feed real-time CO₂ or PIR sensor data into the tool layer so the agent reasons over actual occupancy rather than the IDF's static occupancy schedule
- **Annual simulation**: the current IDF runs 2 design days (cooling + heating); an annual run with TMY weather would surface seasonal patterns and let the agent reason over a full year
- **S3 logging of historical decisions**: stream every agent decision + resulting kWh delta to S3 so the agent can build a long-term memory of which actions worked in which conditions
- **Multi-building portfolio**: generalize the orchestrator to manage a fleet of IDFs in parallel

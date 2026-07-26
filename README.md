# Eco-Loop Building Agents

> Closed-loop HVAC optimization for a small office — EnergyPlus simulation driven by an LLM agent (AWS Bedrock + Claude Sonnet 4.6) that reads building state, recommends setpoint adjustments, and re-simulation. **5.8% energy savings with comfort maintained.**

---

## Problem

Commercial buildings consume ~40% of global energy, and HVAC is the largest single end-use. Static setpoint schedules waste energy during unoccupied hours; existing rule-based building management systems can't reason about nuanced per-zone conditions. We use an LLM agent to **read** live building state, **decide** which zones can safely relax setpoints, **modify** the EnergyPlus model, and **verify** the savings — closing the loop every simulation run.

## Quick Architecture

```
  EnergyPlus → Python Controller → MCP-style Tools → AI Agent
       ▲                                                  │
       │                                                  ▼
       └── Re-simulate  ◄──  IDF Modification  ◄──  Per-zone actions
       │
       ▼
   Dashboard (Streamlit)
```

Full diagram and component breakdown: [`docs/architecture.md`](docs/architecture.md).

## Setup

### Prerequisites
- **Python 3.10+**
- **EnergyPlus 22.x or later** installed. The path is hardcoded at the top of `backend/run_simulation.py` as `C:\EnergyPlusV26-1-0\energyplus.exe` — edit that line if your install lives elsewhere. Download from [energyplus.net](https://energyplus.net/downloads).
- **AWS Bedrock API key** *(optional)* — `loop.py` defaults to Bedrock mode but **falls back to mock automatically** if the key is missing or the call fails, so you can demo the closed loop end-to-end without any AWS credentials. To enable real LLM reasoning, you'll need a Bedrock bearer token with `InvokeModel` permission on Claude Sonnet 4.6.

### Clone & install

```bash
git clone <your-repo-url> eco-loop-building
cd eco-loop-building
python -m venv venv
# Windows
.\venv\Scripts\Activate.ps1
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

> `requirements.txt` currently only pins `python-dotenv`. You'll also need `boto3` (for Bedrock mode), `pandas`, `streamlit`, and `plotly` — install them with `pip install boto3 pandas streamlit plotly` if they're not already present.

### Configure environment

```bash
# Copy the template — do NOT commit the real .env
cp backend/.env.example backend/.env
```

Then edit `backend/.env` and paste your Bedrock bearer token:

```ini
# backend/.env — gitignored, never commit the real key
AWS_BEARER_TOKEN_BEDROCK=your-bedrock-bearer-token-here
AWS_DEFAULT_REGION=us-east-1
```

> The only env var the agent actually reads is `AWS_BEARER_TOKEN_BEDROCK`. The model ID (`us.anthropic.claude-sonnet-4-6`) and region are hardcoded in `agent.py`. The `.env` file is in `.gitignore`; `backend/.env.example` is the safe template you can commit.

## How to Run

### 1. Run the closed loop (baseline → agent decision → modify → re-sim → compare)

```bash
python backend/loop.py
```

- Defaults to **Bedrock mode** (`agent_mode="bedrock"` in `__main__`). If `AWS_BEARER_TOKEN_BEDROCK` is missing or the Bedrock call fails for any reason, the agent silently falls back to mock mode
- On entry, the script first runs `inspect_idf_thermostats()` and prints every thermostat / schedule / zone object found in the IDF — useful for confirming the schedule topology
- Outputs are written to `sample_results/before.csv` and `sample_results/after.csv` (each has columns: `timestamp`, `total_power_kw`, `temp_<zone>`, `hum_<zone>`). The summary printout goes to stdout

### 2. View the dashboard

In a second terminal (with the venv active):

```bash
streamlit run dashboard/app.py
```

Then open <http://localhost:8501> in your browser. You'll see three KPI tiles (baseline / optimized / savings), a total-power overlay chart, and per-zone temperature charts for `CORE_ZN` and `PERIMETER_ZN_1`.

## Results

| Run | Total energy (kWh) | Savings |
|---|---|---|
| Baseline | 518,798.27 | — |
| Optimized (agent decision) | 488,552.72 | **-30,245.55 kWh (5.8%)** |

Comfort is preserved (computed from the same CSVs): the core zone warms by ~0.34 °C (22.84 → 23.18) and `Perimeter_ZN_1` by ~0.33 °C (22.76 → 23.09) — both well within the ASHRAE 55 acceptable range. The 5.8% figure is what `loop.py` prints from `compare_energy()` and what the dashboard shows in the "Energy Savings" tile.

## Folder Structure

```
eco-loop-building/
├── backend/
│   ├── agent.py              # BuildingAgent — mock + Bedrock (Claude Sonnet 4.6)
│   ├── tools.py              # BuildingTools — MCP-style tool layer
│   ├── loop.py               # Closed-loop orchestrator
│   ├── run_simulation.py     # EnergyPlus subprocess wrapper
│   ├── parse_output.py       # eplusout.sql → structured timesteps
│   ├── .env                  # gitignored; real Bedrock key
│   └── .env.example          # committed template
├── dashboard/
│   └── app.py                # Streamlit + Plotly dashboard
├── docs/
│   └── architecture.md       # Full architecture write-up
├── energyplus/
│   ├── RefBldgSmallOfficeNew2004_Chicago.idf
│   ├── USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw
│   ├── optimized_building.idf  # written by loop.py
│   ├── output/                  # gitignored; default scratch dir
│   ├── output_baseline/         # gitignored; baseline run artifacts
│   └── output_optimized/        # gitignored; optimized run artifacts
├── sample_results/
│   ├── before.csv
│   └── after.csv
├── requirements.txt
├── .gitignore
└── README.md
```

## Tech Stack

- **Simulation**: EnergyPlus 22+ (this repo uses v26-1-0)
- **Control / orchestration**: Python 3.10+, `subprocess`, `sqlite3`
- **AI agent**: AWS Bedrock + Claude Sonnet 4.6 (via `boto3` bearer-token auth)
- **Tool layer**: custom Python classes modeling the Model-Context-Protocol (MCP) tool pattern
- **Config**: `python-dotenv`
- **Data wrangling**: `pandas`
- **Dashboard**: Streamlit + Plotly
- **Data interchange**: CSV (per-timestep EnergyPlus SQL → pandas → CSV)

## Built for

**Honeywell Hackathon** — track: *AI for Sustainable Buildings*

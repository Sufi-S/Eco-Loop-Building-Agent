# Project Context: Eco-Loop Building Agents (Honeywell Hackathon)

## What we're building
An AI-powered autonomous building energy management system with a closed feedback loop:

EnergyPlus (simulator) → Python Controller (parses output) → MCP-style Tool Layer
→ AI Agent (Bedrock LLM, decides HVAC/lighting setpoints) → writes new setpoints back
into EnergyPlus → re-simulate → repeat. Plus a dashboard comparing baseline vs
AI-optimized energy usage and comfort (PMV).

## Environment
- OS: Windows
- EnergyPlus installed at: C:\EnergyPlusV26-1-0
- EnergyPlus executable: C:\EnergyPlusV26-1-0\energyplus.exe
- Building model: RefBldgSmallOfficeNew2004_Chicago.idf (copied into project's energyplus/ folder)
- Weather file: Chicago .epw (copied into project's energyplus/ folder)
- Python venv set up with: pandas, streamlit, boto3
- Project root: C:\Users\azams\eco-loop-building

## Folder structure
```
eco-loop-building/
├── backend/       (all Python: run_simulation.py, parse_output.py, tools.py, agent.py, loop.py)
├── energyplus/    (.idf + .epw files, EnergyPlus writes outputs here too)
├── dashboard/     (Streamlit app)
├── docs/          (architecture doc)
├── sample_results/ (before.csv, after.csv, graphs)
```

## Constraints
- Solo developer, ~11 hour hackathon window, no shortcuts on core idea.
- AWS Sandbox available but resets after 1 day — core logic must work locally first,
  AWS/Bedrock wired in only near the end, kept minimal and isolated.
- Must NOT skip any of: EnergyPlus, Python controller, MCP-style tool layer,
  LLM agent decision-making, closed loop, dashboard.

## Deliverables needed at the end
- GitHub repo (all source code)
- .idf building model file(s)
- Energy savings dashboard (screenshots/live)
- Architecture document
- 3-minute demo video
- Presentation (PPT)

## Current stage
EnergyPlus installed and verified. Building folder structure now.
Next: Python script to run EnergyPlus from code, then parse its output into JSON.

"""
app.py - Streamlit dashboard for Eco-Loop Building Agents.

Reads before.csv and after.csv from sample_results/ and displays:
- Key energy metrics (baseline, optimized, savings)
- Interactive line chart of total power over time (baseline vs optimized)
- Zone temperature comparison for Core_ZN and Perimeter_ZN_1
- Explanation of the setpoint change applied
- Footer describing the closed-loop system
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path

# ---------- CONFIG ----------
# Hardcoded change summary (will be made dynamic later)
CHANGE_SUMMARY = (
    "Cooling setpoint raised from 24.0°C to 25.0°C during occupied hours "
    "(weekdays 6am‑10pm, Saturdays 6am‑6pm)."
)

# Paths to CSV files
PROJECT_ROOT = Path(__file__).parent.parent
SAMPLE_DIR = PROJECT_ROOT / "sample_results"
BEFORE_CSV = SAMPLE_DIR / "before.csv"
AFTER_CSV = SAMPLE_DIR / "after.csv"

# ---------- PAGE CONFIG ----------
st.set_page_config(
    page_title="Eco‑Loop – AI Energy Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ---------- LOAD DATA ----------
@st.cache_data
def load_data():
    """Load before and after CSV files, handle missing files."""
    df_before = None
    df_after = None

    if BEFORE_CSV.exists():
        df_before = pd.read_csv(BEFORE_CSV)
        # Ensure timestamp is datetime
        if "timestamp" in df_before.columns:
            df_before["timestamp"] = pd.to_datetime(df_before["timestamp"])
    else:
        st.warning("Baseline file (before.csv) not found.")

    if AFTER_CSV.exists():
        df_after = pd.read_csv(AFTER_CSV)
        if "timestamp" in df_after.columns:
            df_after["timestamp"] = pd.to_datetime(df_after["timestamp"])
    else:
        st.warning("Optimized file (after.csv) not found.")

    return df_before, df_after

df_before, df_after = load_data()

# ---------- METRICS COMPUTATION ----------
def compute_metrics(df_before, df_after):
    """Compute total energy and savings."""
    total_before = df_before["total_power_kw"].sum() if df_before is not None else 0
    total_after = df_after["total_power_kw"].sum() if df_after is not None else 0
    savings_kwh = total_before - total_after
    savings_pct = (savings_kwh / total_before * 100) if total_before > 0 else 0
    return total_before, total_after, savings_kwh, savings_pct

total_before, total_after, savings_kwh, savings_pct = compute_metrics(df_before, df_after)

# ---------- HEADER ----------
st.title("🌿 Eco‑Loop Building Agents — AI‑Optimized Energy Dashboard")
st.markdown(
    "A closed‑loop system that uses EnergyPlus simulation, a Python controller, "
    "and an AI agent (Bedrock LLM) to adjust setpoints and reduce energy consumption "
    "while maintaining occupant comfort."
)
st.divider()

# ---------- TOP METRICS ROW ----------
col1, col2, col3 = st.columns(3)
with col1:
    st.metric(
        label="Baseline Total Energy",
        value=f"{total_before:,.2f} kWh",
        delta=None
    )
with col2:
    st.metric(
        label="Optimized Total Energy",
        value=f"{total_after:,.2f} kWh",
        delta=f"{(total_after - total_before):+,.2f} kWh"
    )
with col3:
    st.metric(
        label="Energy Savings",
        value=f"{savings_kwh:,.2f} kWh",
        delta=f"{savings_pct:.1f}%",
        delta_color="normal" if savings_kwh >= 0 else "off"  # green if savings positive
    )

st.divider()

# ---------- LINE CHART: TOTAL POWER OVER TIME ----------
st.subheader("Total Electricity Demand Over Time")

if df_before is not None and df_after is not None:
    # Create a combined dataframe for plotting, aligning by timestamp if needed.
    # We'll use Plotly to overlay two traces.
    fig = go.Figure()

    # Add baseline trace
    fig.add_trace(go.Scatter(
        x=df_before["timestamp"],
        y=df_before["total_power_kw"],
        mode="lines",
        name="Baseline",
        line=dict(color="blue", width=2)
    ))

    # Add optimized trace
    fig.add_trace(go.Scatter(
        x=df_after["timestamp"],
        y=df_after["total_power_kw"],
        mode="lines",
        name="Optimized",
        line=dict(color="green", width=2)
    ))

    fig.update_layout(
        xaxis_title="Time",
        yaxis_title="Total Power (kW)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=30, b=30),
        height=400
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Not enough data to display power chart.")

st.divider()

# ---------- ZONE TEMPERATURE COMPARISON ----------
st.subheader("Zone Temperature Comparison (Baseline vs Optimized)")

# Select two representative zones: Core_ZN and Perimeter_ZN_1
zone_cols = ["temp_CORE_ZN", "temp_PERIMETER_ZN_1"]
zone_labels = ["Core Zone", "Perimeter Zone 1"]

if df_before is not None and df_after is not None:
    # Check if these columns exist
    missing = []
    for col in zone_cols:
        if col not in df_before.columns and col not in df_after.columns:
            missing.append(col)
    if missing:
        st.warning(f"Some zone columns missing: {missing}. Skipping temperature charts.")
    else:
        # Create a figure with two subplots (or two separate charts)
        for zone_col, label in zip(zone_cols, zone_labels):
            fig = go.Figure()
            # Baseline
            if zone_col in df_before.columns:
                fig.add_trace(go.Scatter(
                    x=df_before["timestamp"],
                    y=df_before[zone_col],
                    mode="lines",
                    name=f"Baseline - {label}",
                    line=dict(color="blue", width=2, dash="dash")
                ))
            # Optimized
            if zone_col in df_after.columns:
                fig.add_trace(go.Scatter(
                    x=df_after["timestamp"],
                    y=df_after[zone_col],
                    mode="lines",
                    name=f"Optimized - {label}",
                    line=dict(color="green", width=2)
                ))

            fig.update_layout(
                title=f"{label} Temperature",
                xaxis_title="Time",
                yaxis_title="Temperature (°C)",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=10, r=10, t=40, b=30),
                height=300
            )
            st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Temperature data not available.")

st.divider()

# ---------- WHAT CHANGED (EXPANDER) ----------
with st.expander("📋 What changed in this optimization run?", expanded=True):
    st.markdown(CHANGE_SUMMARY)
    st.caption(
        "This summary is currently hard‑coded. In future versions, it will be "
        "automatically generated from the actual setpoint changes applied to the IDF."
    )

st.divider()

# ---------- FOOTER ----------
st.markdown(
    """
    **Eco‑Loop Building Agents** — *Closed‑loop AI optimization pipeline*
    EnergyPlus → Python Parser → MCP‑style Tools → AI Agent (Bedrock LLM) → IDF Modification → Re‑simulation
    Built for the Honeywell Hackathon.
    """
)
st.caption("Dashboard powered by Streamlit and Plotly.")
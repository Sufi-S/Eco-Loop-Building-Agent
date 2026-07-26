"""
tools.py - MCP-style tools for building state inspection and rule-based recommendations.

Provides a BuildingTools class that wraps the parsed EnergyPlus output and offers
methods to query the latest state, identify low-priority zones, and suggest
setpoint adjustments. This serves as a placeholder that will later be replaced
or wrapped by an LLM agent.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any

# Import the parser from the sibling module
from parse_output import parse_eplus_output

# Default output directory (adjust if needed)
DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "energyplus" / "output"


class BuildingTools:
    """
    MCP-style tool set for building energy management.

    Each method includes a docstring with name, description, and input/output
    schemas (informal but clear).
    """

    def __init__(self, output_dir: Optional[Path] = None):
        """
        Initialize the tools with a path to EnergyPlus output directory.

        Args:
            output_dir: Path to directory containing eplusout.csv.
                        Defaults to ../energyplus/output relative to this file.
        """
        if output_dir is None:
            self.output_dir = DEFAULT_OUTPUT_DIR
        else:
            self.output_dir = Path(output_dir)

        # Cache the latest state to avoid repeated parsing
        self._cached_data = None
        self._cached_timestamp = None

    def _get_latest_state(self) -> Dict[str, Any]:
        """
        Parse the output CSV and return the last timestep's data.
        Caches the result to avoid re-parsing within the same object lifecycle.
        """
        # If we already have cached data, return it
        if self._cached_data is not None:
            return self._cached_data

        # Parse the full list of timesteps
        all_timesteps = parse_eplus_output(self.output_dir)
        if not all_timesteps:
            raise ValueError("No timestep data found in output.")
        latest = all_timesteps[-1]
        # Cache it
        self._cached_data = latest
        return latest

    # ----------------------------------------------------------------------
    # Tool 1: get_building_state
    # ----------------------------------------------------------------------
    def get_building_state(self) -> Dict[str, Any]:
        """
        Name: get_building_state
        Description: Returns the latest full building state, including zone
                     temperatures, humidities, total power, and outdoor conditions.
        Input schema: None (no arguments)
        Output schema: dict with keys:
            - timestamp (str)
            - zones (dict: zone_name -> {"temperature": float, "humidity": float})
            - total_power_kw (float)
            - outdoor (dict: {"temperature": float, "humidity": float, ...}) [optional]
            - (other sub-meter data as present)
        """
        return self._get_latest_state()

    # ----------------------------------------------------------------------
    # Tool 2: get_zone_temperatures
    # ----------------------------------------------------------------------
    def get_zone_temperatures(self) -> Dict[str, float]:
        """
        Name: get_zone_temperatures
        Description: Returns a mapping of zone names to their latest air temperature.
        Input schema: None
        Output schema: dict of {zone_name: temperature_in_celsius}
        """
        state = self._get_latest_state()
        zones = state.get("zones", {})
        return {zone: info["temperature"] for zone, info in zones.items()
                if info.get("temperature") is not None}

    # ----------------------------------------------------------------------
    # Tool 3: identify_empty_or_low_priority_zones
    # ----------------------------------------------------------------------
    def identify_empty_or_low_priority_zones(
        self, occupancy_hint: Optional[Dict[str, bool]] = None
    ) -> List[str]:
        """
        Name: identify_empty_or_low_priority_zones
        Description: Returns a list of zone names considered low priority for
                     conditioning based on occupancy hints.
        Input schema: occupancy_hint (optional dict: zone_name -> bool)
                      If not provided, ATTIC is assumed unoccupied, others occupied.
        Output schema: list of zone names (strings)
        """
        state = self._get_latest_state()
        zones = state.get("zones", {})

        # If no hint provided, build default: ATTIC unoccupied, everything else occupied
        if occupancy_hint is None:
            occupancy_hint = {}
            for zone in zones.keys():
                # Treat any zone containing "ATTIC" as unoccupied (case-insensitive)
                if "attic" in zone.lower():
                    occupancy_hint[zone] = False
                else:
                    occupancy_hint[zone] = True
        else:
            # For zones not in the hint, default to occupied (conservative)
            for zone in zones.keys():
                if zone not in occupancy_hint:
                    occupancy_hint[zone] = True

        # Return zones that are unoccupied (low priority)
        return [zone for zone, occupied in occupancy_hint.items()
                if zone in zones and not occupied]

    # ----------------------------------------------------------------------
    # Tool 4: suggest_setpoint_adjustment
    # ----------------------------------------------------------------------
    def suggest_setpoint_adjustment(
        self, zone_name: str, current_temp: float, is_occupied: bool
    ) -> Dict[str, Any]:
        """
        Name: suggest_setpoint_adjustment
        Description: Given a zone and its current temperature, suggests a simple
                     rule-based action (cool, heat, maintain, or reduce conditioning).
        Input schema:
            - zone_name (str)
            - current_temp (float, in Celsius)
            - is_occupied (bool)
        Output schema: dict with keys:
            - zone (str)
            - action (str): "cool", "heat", "maintain", or "reduce_conditioning"
            - reasoning (str): explanation
        """
        if not is_occupied:
            # Unoccupied: suggest reducing conditioning (e.g., raise cooling setpoint, lower heating)
            action = "reduce_conditioning"
            reasoning = f"Zone {zone_name} is unoccupied; conditioning can be relaxed."
        else:
            # Occupied: comfort bounds
            if current_temp > 24.0:
                action = "cool"
                reasoning = f"Temperature {current_temp:.1f}°C > 24°C; suggest cooling."
            elif current_temp < 20.0:
                action = "heat"
                reasoning = f"Temperature {current_temp:.1f}°C < 20°C; suggest heating."
            else:
                action = "maintain"
                reasoning = f"Temperature {current_temp:.1f}°C within comfort range (20-24°C); maintain setpoints."

        return {
            "zone": zone_name,
            "action": action,
            "reasoning": reasoning
        }

    # ----------------------------------------------------------------------
    # Tool 5: get_full_recommendation
    # ----------------------------------------------------------------------
    def get_full_recommendation(
        self, occupancy_hint: Optional[Dict[str, bool]] = None
    ) -> Dict[str, Any]:
        """
        Name: get_full_recommendation
        Description: Orchestrates the tools to produce a full recommendation
                     for all zones based on latest state and occupancy hints.
        Input schema: occupancy_hint (optional dict)
        Output schema: dict with keys:
            - timestamp (str)
            - total_power_kw (float)
            - recommendations (dict: zone_name -> recommendation dict)
            - low_priority_zones (list)
        """
        state = self._get_latest_state()
        zones = state.get("zones", {})
        total_power = state.get("total_power_kw")

        # Identify low-priority zones
        low_priority = self.identify_empty_or_low_priority_zones(occupancy_hint)

        # For each zone, get temperature and determine occupancy
        recommendations = {}
        for zone, info in zones.items():
            temp = info.get("temperature")
            if temp is None:
                continue  # skip if missing data

            # Determine if occupied: use occupancy_hint if provided, else default
            if occupancy_hint is not None:
                is_occupied = occupancy_hint.get(zone, True)  # default to occupied
            else:
                # If no hint, treat "attic" as unoccupied, others occupied
                is_occupied = "attic" not in zone.lower()

            rec = self.suggest_setpoint_adjustment(zone, temp, is_occupied)
            recommendations[zone] = rec

        return {
            "timestamp": state["timestamp"],
            "total_power_kw": total_power,
            "recommendations": recommendations,
            "low_priority_zones": low_priority
        }


# ----------------------------------------------------------------------
# Standalone test: run get_full_recommendation on default output.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Instantiate the tools (uses default output dir)
    tools = BuildingTools()

    try:
        # Get full recommendations with default occupancy (attic unoccupied)
        result = tools.get_full_recommendation()
        print("Full Recommendation:")
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(f"Error: {e}")
"""
agent.py - AI decision-making layer for building energy management.

Supports two modes:
- MOCK: offline rule-based reasoning using BuildingTools.get_full_recommendation()
- BEDROCK: calls Anthropic Claude Sonnet 4.6 via AWS Bedrock to reason and output
           JSON decisions per zone. Falls back to mock on any failure.
"""

import json
import re
from pathlib import Path
from typing import Optional, Dict, Any

# Attempt to import boto3; if not available, we'll still work in mock mode
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    boto3 = None

# Import our tools
from tools import BuildingTools


class BuildingAgent:
    """
    Agent that uses BuildingTools to inspect building state and produce recommendations.
    Supports mock (rule-based) or Bedrock (LLM) reasoning.
    """

    def __init__(self, mode: str = "mock", output_dir: Optional[Path] = None):
        """
        Initialize the agent.

        Args:
            mode: "mock" or "bedrock". Defaults to "mock".
            output_dir: Path to EnergyPlus output directory. If None, uses default.
        """
        if mode not in ("mock", "bedrock"):
            raise ValueError("mode must be 'mock' or 'bedrock'")
        self.mode = mode
        self.tools = BuildingTools(output_dir)

        # For Bedrock, we'll cache the client if needed
        self.bedrock_client = None

    def _get_bedrock_client(self):
        """Lazy initialization of Bedrock runtime client.

        Uses AWS Bedrock API key authentication via the AWS_BEARER_TOKEN_BEDROCK
        environment variable. boto3 picks this up automatically for bedrock-runtime
        calls; it does not need to be passed explicitly into boto3.client().
        """
        if not BOTO3_AVAILABLE:
            raise ImportError("boto3 is not installed")
        if self.bedrock_client is None:
            import os
            api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            if not api_key:
                raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK environment variable not set")
            try:
                self.bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name="us-east-1"
                )
            except Exception as e:
                raise RuntimeError(f"Failed to create Bedrock client: {e}")
        return self.bedrock_client

    def _call_bedrock(self, prompt: str) -> str:
        """
        Send a prompt to Claude Sonnet 4.6 via Bedrock and return the response text.

        Args:
            prompt: The user prompt.

        Returns:
            The assistant's response as a string.

        Raises:
            Exception if the API call fails.
        """
        client = self._get_bedrock_client()
        model_id = "us.anthropic.claude-sonnet-4-6"

        # Claude messages format
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1500,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}]
        }

        try:
            response = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body).encode("utf-8")
            )
            response_body = json.loads(response["body"].read().decode("utf-8"))
            # Claude 3 response has "content" list with "text" field
            if "content" in response_body and len(response_body["content"]) > 0:
                return response_body["content"][0]["text"]
            else:
                raise ValueError("Unexpected response format from Bedrock")
        except Exception as e:
            # Re-raise with context
            raise RuntimeError(f"Bedrock invocation failed: {e}") from e

    def _extract_json_from_response(self, text: str) -> dict:
        """
        Extract a JSON object from the model's response, handling markdown code fences
        and extra text.

        Args:
            text: The raw assistant response.

        Returns:
            Parsed JSON dict.

        Raises:
            ValueError if no valid JSON is found.
        """
        # Try to find a JSON block inside markdown code fences (```json ... ```)
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            # Otherwise, try to find any JSON object in the text (first occurrence of { ... })
            match = re.search(r"(\{.*\})", text, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                raise ValueError("No JSON object found in the response")

        # Parse the JSON
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}\nExtracted string: {json_str}")

    def _generate_mock_explanation(self, recommendations: dict, total_power: float) -> str:
        """
        Create a natural-language explanation from the rule-based recommendations.

        Args:
            recommendations: dict from tools.get_full_recommendation()["recommendations"]
            total_power: total power in kW

        Returns:
            A human-readable summary string.
        """
        lines = []
        lines.append("I've analyzed the current building state using rule-based logic.")
        if total_power is not None:
            lines.append(f"Current total electricity demand: {total_power:.2f} kW.")

        # Group actions for summary
        action_counts = {}
        for zone, rec in recommendations.items():
            action = rec["action"]
            action_counts[action] = action_counts.get(action, 0) + 1

        if action_counts:
            summary_parts = []
            for action, count in action_counts.items():
                if count > 0:
                    summary_parts.append(f"{count} zone(s) suggested for '{action}'")
            if summary_parts:
                lines.append("Summary: " + "; ".join(summary_parts) + ".")
            else:
                lines.append("No actions recommended.")

        # List specific recommendations for each zone (only if not too many)
        if len(recommendations) <= 10:
            lines.append("Detailed zone-level reasoning:")
            for zone, rec in recommendations.items():
                lines.append(f"  - {zone}: {rec['reasoning']}")
        else:
            lines.append("Too many zones; see the structured output for details.")

        return " ".join(lines)

    def decide(self, occupancy_hint: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        """
        Produce a decision for the building.

        Args:
            occupancy_hint: Optional dict mapping zone name to occupied (bool).
                            If None, default logic (ATTC unoccupied) is used.

        Returns:
            A dict with keys:
                - timestamp (str)
                - total_power_kw (float)
                - recommendations (dict: zone -> {action, reasoning})
                - low_priority_zones (list)
                - agent_explanation (str): natural language summary
                - agent_mode (str): "mock" or "bedrock"
        """
        # Always get the baseline rule-based recommendations to have a fallback
        # and to have structured data even if Bedrock fails.
        rule_based_result = self.tools.get_full_recommendation(occupancy_hint)
        rule_recommendations = rule_based_result["recommendations"]
        total_power = rule_based_result["total_power_kw"]
        timestamp = rule_based_result["timestamp"]
        low_priority = rule_based_result["low_priority_zones"]

        # If mode is mock, just use the rule-based result and generate explanation
        if self.mode == "mock":
            explanation = self._generate_mock_explanation(
                rule_recommendations, total_power
            )
            return {
                "timestamp": timestamp,
                "total_power_kw": total_power,
                "recommendations": rule_recommendations,
                "low_priority_zones": low_priority,
                "agent_explanation": explanation,
                "agent_mode": "mock"
            }

        # --- BEDROCK MODE ---
        # Build a prompt that describes the state and available tools
        # Get detailed state from tools
        state = self.tools.get_building_state()
        zones_info = state.get("zones", {})
        # Prepare a readable summary of zone temperatures and occupancy hints
        zone_lines = []
        for zone, info in zones_info.items():
            temp = info.get("temperature")
            if temp is None:
                temp_str = "unknown"
            else:
                temp_str = f"{temp:.1f}°C"
            # Determine occupancy (use hint if provided)
            if occupancy_hint is not None:
                occupied = occupancy_hint.get(zone, True)
            else:
                occupied = "attic" not in zone.lower()
            occ_str = "occupied" if occupied else "unoccupied"
            zone_lines.append(f"- {zone}: {temp_str}, {occ_str}")

        prompt = f"""You are an AI building energy manager. You have access to the following tools:
1. get_zone_temperatures() -> returns current temperatures per zone.
2. identify_empty_or_low_priority_zones(occupancy_hint) -> returns list of low-priority zones.
3. suggest_setpoint_adjustment(zone, temp, is_occupied) -> returns action: 'cool', 'heat', 'maintain', or 'reduce_conditioning'.

Current building state (latest timestep):
- Total power: {total_power:.2f} kW (if available)
- Zone data:
{chr(10).join(zone_lines)}

Based on this information, reason step by step about which zones need action. For each zone, decide one of the following actions: "cool", "heat", "maintain", or "reduce_conditioning". Provide a short reasoning for each.

Output your final decision as a JSON object with the following structure:
{{
  "recommendations": {{
    "ZoneName": {{
      "action": "cool/heat/maintain/reduce_conditioning",
      "reasoning": "your reasoning here"
    }},
    ...
  }}
}}
Only output the JSON, no other text.
"""

        try:
            # Attempt Bedrock call
            response_text = self._call_bedrock(prompt)
            # Extract JSON
            parsed = self._extract_json_from_response(response_text)
            # Ensure it has "recommendations" key
            if "recommendations" not in parsed:
                raise ValueError("JSON does not contain 'recommendations' key")

            # Use the model's recommendations, but ensure all zones are present
            model_recommendations = parsed["recommendations"]
            # Merge with rule-based for any missing zones (shouldn't happen, but safe)
            final_recommendations = {}
            for zone in zones_info.keys():
                if zone in model_recommendations:
                    final_recommendations[zone] = model_recommendations[zone]
                else:
                    # fallback to rule-based for missing zone
                    final_recommendations[zone] = rule_recommendations.get(zone, {
                        "action": "maintain",
                        "reasoning": "No model decision; using default."
                    })

            # Generate explanation from the model's reasoning (use the first few)
            explanation_lines = ["I've reasoned about the building state using LLM."]
            if total_power is not None:
                explanation_lines.append(f"Current total power: {total_power:.2f} kW.")
            explanation_lines.append("Per-zone decisions:")
            for zone, rec in final_recommendations.items():
                action = rec.get("action", "unknown")
                reasoning = rec.get("reasoning", "No reasoning provided.")
                explanation_lines.append(f"- {zone}: {action} ({reasoning})")

            explanation = " ".join(explanation_lines)

            return {
                "timestamp": timestamp,
                "total_power_kw": total_power,
                "recommendations": final_recommendations,
                "low_priority_zones": low_priority,
                "agent_explanation": explanation,
                "agent_mode": "bedrock"
            }

        except Exception as e:
            # Fall back to mock mode on any failure
            print(f"WARNING: Bedrock mode failed: {e}. Falling back to MOCK mode.")
            explanation = self._generate_mock_explanation(
                rule_recommendations, total_power
            )
            return {
                "timestamp": timestamp,
                "total_power_kw": total_power,
                "recommendations": rule_recommendations,
                "low_priority_zones": low_priority,
                "agent_explanation": explanation + " (fallback from failed Bedrock)",
                "agent_mode": "mock (fallback)"
            }


# ----------------------------------------------------------------------
# Standalone test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Test mock mode
    agent_mock = BuildingAgent(mode="mock")
    result_mock = agent_mock.decide()
    print("MOCK MODE DECISION:")
    print(json.dumps(result_mock, indent=2, default=str))

    # Uncomment to test Bedrock (if credentials available)
    # agent_bedrock = BuildingAgent(mode="bedrock")
    # result_bedrock = agent_bedrock.decide()
    # print("\nBEDROCK MODE DECISION:")
    # print(json.dumps(result_bedrock, indent=2, default=str))
"""Google ADK conversational entry point."""

from __future__ import annotations

import os

from google.adk.agents import Agent

from cloud_journey.tools import (
    approve_journey,
    continue_journey,
    get_journey_status,
    reject_journey,
    start_journey,
)

INSTRUCTION = """
You are the Cloud Journey conversational interface. All business state is durable
in PostgreSQL and all state changes must be made with the registered tools.

Rules:
- Call a tool for every request to start, continue, approve, reject, show status,
  or show history. Never infer, cache, or invent a Journey state.
- Reuse a Journey ID returned earlier in this conversation when the user says
  "the journey". Ask for an ID only if none is available.
- Never claim success when a tool returns ok=false. Clearly show its status code,
  error, and message.
- Display the tool's state_path or transitions vertically with arrows, then show
  the current state and Journey ID.
- Explain that WAITING_FOR_APPROVAL is a human approval boundary.
- Provisioning in this PoC is simulated; never imply real resources were created.
- Do not place state-transition rules in your own reasoning. The state machine is
  the authority and its response is final.
"""

root_agent = Agent(
    name="cloud_journey_agent",
    model=os.getenv("JOURNEY_AGENT_MODEL", "gemini-2.5-flash"),
    description="A durable Cloud Journey state-machine assistant.",
    instruction=INSTRUCTION,
    tools=[
        start_journey,
        continue_journey,
        approve_journey,
        reject_journey,
        get_journey_status,
    ],
)

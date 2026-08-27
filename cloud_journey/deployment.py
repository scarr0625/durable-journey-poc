"""Agent Runtime deployment object.

This module is packaged with the agent. Agent Runtime supplies managed sessions;
Journey business state continues to come only from Cloud SQL.
"""

from vertexai import agent_engines

from cloud_journey.agent import root_agent

app = agent_engines.AdkApp(agent=root_agent)

__all__ = ["app"]

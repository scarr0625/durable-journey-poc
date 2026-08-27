"""Small SDK smoke test for a deployed Agent Runtime resource."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env.gcp")


async def main() -> None:
    import vertexai

    project_id = os.environ["GCP_PROJECT_ID"]
    location = os.environ["GCP_LOCATION"]
    resource_name = os.environ["AGENT_RESOURCE_NAME"]
    client = vertexai.Client(project=project_id, location=location)
    remote_agent = client.agent_engines.get(name=resource_name)
    async for event in remote_agent.async_stream_query(
        user_id=os.getenv("AGENT_TEST_USER", "cloud-compass-poc-user"),
        message=os.getenv(
            "AGENT_TEST_MESSAGE",
            "Explain what Cloud Compass can help me with before I start a Journey.",
        ),
    ):
        print(event)


if __name__ == "__main__":
    asyncio.run(main())

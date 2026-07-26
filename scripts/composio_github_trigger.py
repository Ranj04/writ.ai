"""One-off live wiring of a Composio GITHUB_COMMIT_EVENT trigger for this repo.

Run with: PYTHONPATH=backend python3 scripts/composio_github_trigger.py

This makes real Composio API calls (trigger creation + a live subscription).
It is intentionally outside the fixture-driven demo flow described in AGENTS.md.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from composio import Composio  # noqa: E402

REPO_OWNER = "Eman-Gon"
REPO_NAME = "DragBack"
COMPOSIO_USER_ID = "default"


def main() -> None:
    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        sys.exit("COMPOSIO_API_KEY is not set in the environment/.env")

    composio = Composio(api_key=api_key)

    trigger_type = composio.triggers.get_type("GITHUB_COMMIT_EVENT")
    print(f"Trigger config schema: {trigger_type.config}")

    trigger = composio.triggers.create(
        slug="GITHUB_COMMIT_EVENT",
        user_id=COMPOSIO_USER_ID,
        trigger_config={"owner": REPO_OWNER, "repo": REPO_NAME},
    )
    print(f"Trigger created: {trigger.trigger_id}")

    subscription = composio.triggers.subscribe()

    @subscription.handle(trigger_id=trigger.trigger_id)
    def handle_event(data: object) -> None:
        print(f"Event received: {data}")

    print(f"Listening for commits on {REPO_OWNER}/{REPO_NAME}... (Ctrl+C to stop)")
    subscription.wait_forever()


if __name__ == "__main__":
    main()

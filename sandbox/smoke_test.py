"""Smoke test: run Claude Code inside the sandbox as a fake OWUI user.

Run from the `sandbox/` directory after `docker compose up -d`:

    OPEN_TERMINAL_API_KEY=$(grep OPEN_TERMINAL_API_KEY .env | cut -d= -f2) \
    ANTHROPIC_API_KEY=... \
    python -m smoke_test
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from open_terminal_client import OpenTerminalClient
from claude_runner import ClaudeRunConfig, run_claude


async def main() -> int:
    base_url = os.environ.get("OPEN_TERMINAL_URL", "http://localhost:8000")
    api_key = os.environ["OPEN_TERMINAL_API_KEY"]
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")

    if not (anthropic_key or oauth_token):
        print("Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN", file=sys.stderr)
        return 2

    cfg = ClaudeRunConfig(
        model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5"),
        api_key=anthropic_key,
        oauth_token=oauth_token,
        workdir="~/chat-smoke",
        max_turns=5,
    )

    prompt = (
        "Create a file called hello.py that prints 'hi from sandbox' and "
        "then run it. Report the output."
    )

    async with OpenTerminalClient(base_url, api_key) as client:
        async for event in run_claude(client, user_id="smoke", prompt=prompt, cfg=cfg):
            etype = event.get("type", "?")
            if etype == "_stderr":
                print(f"[stderr] {event['text']}", end="", file=sys.stderr)
            elif etype == "_exit":
                print(f"\n[exit code: {event['code']}]")
            elif etype == "_raw":
                print(f"[raw] {event['text']}")
            else:
                # Compact dump so you can eyeball the event stream.
                print(json.dumps(event)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

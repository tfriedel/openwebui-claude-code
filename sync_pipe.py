"""Push the local claude_agent_pipe_sandboxed.py to a running OpenWebUI.

Usage (typical, from this directory):
    set -a; source /home/thomas/projects/helm_assistant/.env; set +a
    python3 sync_pipe.py

Behaviour:
    1. Reads OPENWEBUI_URL (default http://localhost:${OPENWEBUI_PORT:-13000})
       and OPENWEBUI_API_KEY from the environment. The key never enters this
       script's logs or output.
    2. GETs /api/v1/functions/ to find the sandboxed pipe's id. Match
       priority: explicit `--id`, then exact id == SANDBOXED_PIPE_ID,
       then a unique function whose content starts with the pipe's
       `title: Claude Code (Sandboxed)` marker.
    3. POSTs /api/v1/functions/id/{id}/update with the current file
       contents, preserving the function's existing `name` / `meta`.

    Existing valves and the `is_active` flag are untouched — the update
    endpoint only rewrites `content` + `meta.manifest` + `name`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SANDBOXED_PIPE_ID = "claude-code-sandboxed"
SANDBOXED_PIPE_MARKER = "title: Claude Code (Sandboxed)"
PIPE_FILE = Path(__file__).parent / "claude_agent_pipe_sandboxed.py"


def api(method: str, url: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} {method} {url}\n  → {detail[:500]}")


def find_function_id(base: str, key: str, explicit_id: str | None) -> tuple[str, dict]:
    """Return (id, current_function_record)."""
    functions = api("GET", f"{base}/api/v1/functions/", key)
    by_id = {f["id"]: f for f in functions}

    if explicit_id:
        if explicit_id not in by_id:
            raise SystemExit(
                f"No function with id={explicit_id!r}. "
                f"Available: {sorted(by_id)}"
            )
        return explicit_id, by_id[explicit_id]

    if SANDBOXED_PIPE_ID in by_id:
        return SANDBOXED_PIPE_ID, by_id[SANDBOXED_PIPE_ID]

    # Fall back: find functions whose content contains the title marker.
    # The list endpoint doesn't include `content`, so fetch each candidate
    # individually. Keep the search narrow — only unloaded pipes need it.
    matches: list[tuple[str, dict]] = []
    for fid, meta in by_id.items():
        if meta.get("type") != "pipe":
            continue
        detail = api("GET", f"{base}/api/v1/functions/id/{fid}", key)
        if SANDBOXED_PIPE_MARKER in (detail.get("content") or ""):
            matches.append((fid, detail))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(
            f"No function matches id={SANDBOXED_PIPE_ID!r} and no pipe "
            f"contains '{SANDBOXED_PIPE_MARKER}'. "
            f"Create it once in the OWUI UI, or pass --id <existing_id>."
        )
    raise SystemExit(
        f"Multiple pipes contain '{SANDBOXED_PIPE_MARKER}': "
        f"{[m[0] for m in matches]}. Pass --id explicitly."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="Override function id (default: auto-detect).")
    ap.add_argument(
        "--url",
        default=os.environ.get(
            "OPENWEBUI_URL",
            f"http://localhost:{os.environ.get('OPENWEBUI_PORT', '13000')}",
        ),
        help="OpenWebUI base URL.",
    )
    args = ap.parse_args()

    key = os.environ.get("OPENWEBUI_API_KEY")
    if not key:
        print(
            "OPENWEBUI_API_KEY not set. Source the helm_assistant .env first:\n"
            "  set -a; source /home/thomas/projects/helm_assistant/.env; set +a",
            file=sys.stderr,
        )
        return 2

    if not PIPE_FILE.exists():
        print(f"Pipe file not found: {PIPE_FILE}", file=sys.stderr)
        return 2

    content = PIPE_FILE.read_text(encoding="utf-8")
    # Pull the pipe's own name from its docstring frontmatter so we don't
    # overwrite whatever the user titled it. Fall back to a sensible default.
    m = re.search(r"^title:\s*(.+)$", content, re.M)
    title = m.group(1).strip() if m else "Claude Code (Sandboxed)"

    base = args.url.rstrip("/")
    fid, current = find_function_id(base, key, args.id)

    form = {
        "id": fid,
        "name": current.get("name") or title,
        "content": content,
        "meta": current.get("meta") or {"description": title, "manifest": {}},
    }
    api("POST", f"{base}/api/v1/functions/id/{fid}/update", key, form)
    print(f"✓ Synced {PIPE_FILE.name} → {base} (id={fid}, {len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

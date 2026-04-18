# Sandboxed Claude Code via open-terminal

Runs the Claude Code agent inside [open-webui/open-terminal](https://github.com/open-webui/open-terminal) instead of directly in the Open WebUI backend process. Each Open WebUI user gets a dedicated Linux account (via `OPEN_TERMINAL_MULTI_USER=true`), so files, processes, and commands are isolated by standard Unix permissions.

> **Threat model:** small, trusted groups. One shared kernel, no hard multi-tenant boundaries. Good enough to stop accidental cross-user damage and to keep the agent out of the Open WebUI host's filesystem. Not a substitute for microVMs if you're exposing this to untrusted users.

## Components

| File | Purpose |
| --- | --- |
| `Dockerfile` | Extends `ghcr.io/open-webui/open-terminal` with `@anthropic-ai/claude-code` preinstalled. |
| `docker-compose.yml` | Runs the sandbox on `:8000` with multi-user mode and a named volume for `/home`. |
| `open_terminal_client.py` | Async HTTP client: `start()`, `stream_output()`, `read_file()`, `write_file()`. |
| `claude_runner.py` | Builds + invokes the `claude --output-format stream-json` command in the user's account and yields parsed events. |

## Why this architecture

The existing `claude_agent_pipe.py` uses the Claude Agent SDK in-process — which means Bash/Read/Write tool calls hit the Open WebUI host's filesystem with whatever permissions that process has. That's fine for a solo dev setup, dangerous in any shared deployment.

The Agent SDK has no "run on a remote host" hook: it always spawns `claude` locally. So instead of using the SDK, we invoke the `claude` CLI directly with `--output-format stream-json` inside the sandbox. The event stream that comes back is identical to what the SDK surfaces (system/assistant/user/result messages), just newline-delimited JSON — so the existing pipe's renderer can stay largely unchanged.

## Bringing up the sandbox

```bash
cd sandbox
echo "OPEN_TERMINAL_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build
curl -s http://localhost:8000/health
```

Quick smoke test, impersonating two different OWUI users to verify isolation:

```bash
API_KEY=$(grep OPEN_TERMINAL_API_KEY .env | cut -d= -f2)

# User A writes a secret
curl -s http://localhost:8000/execute \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hunter2 > ~/secret.txt && ls -la ~"}'

# User B tries to read it (should fail — different /home/owui_bob)
curl -s http://localhost:8000/execute \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-User-Id: bob" \
  -H "Content-Type: application/json" \
  -d '{"command": "cat /home/owui_alice/secret.txt || echo DENIED"}'
```

## Wiring into the pipe

In `claude_agent_pipe.py`, replace the SDK-backed code path with a call into `claude_runner.run_claude()`, passing:

- `user_id = __user__["id"]` — Open WebUI injects the user object into `pipe()`; its stable `id` becomes the sandbox account prefix.
- `cfg.resume_session_id` — look up the `chat_id → claude_session_id` map the same way the existing pipe does. The first event from the stream (`type: "system"`, `subtype: "init"`) carries the new session id; stash it.
- `cfg.workdir = f"~/chat-{chat_id}"` — per-chat subdirectory inside the user's home. Carries artifacts across turns without leaking between chats.

Event shape differences from the SDK:

| SDK type | stream-json equivalent |
| --- | --- |
| `AssistantMessage.content[ToolUseBlock]` | `{"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}` |
| `UserMessage.content[ToolResultBlock]` | `{"type":"user","message":{"content":[{"type":"tool_result",...}]}}` |
| `ResultMessage` | `{"type":"result","subtype":"success","total_cost_usd":...}` |
| `SystemMessage(subtype="init")` | `{"type":"system","subtype":"init","session_id":"..."}` |

The renderer's `_tool_preview` / `_tool_input_block` helpers already work on the `tool_use.input` dict, so they port over directly.

## Open questions / next steps

- **Artifacts:** the existing pipe scans `cwd` post-run for generated PDFs/CSVs/images and uploads them as OWUI artifacts. In the sandbox version, swap the filesystem scan for `client.list_files(user, workdir)` + `client.read_file()`.
- **Image context:** when the user attaches images in the chat, `client.write_file()` them into the workdir before invoking claude, then reference by path in the prompt.
- **Session resume:** store `claude_session_id` per `chat_id` in-process (same as the current pipe). Needs testing that `claude --resume` works cleanly across separate `POST /execute` calls — each call is a fresh process, but claude persists session state to `~/.claude/` inside the user's home.
- **Cold start:** first request per user spawns `useradd`; measure and decide whether to pre-warm on Open WebUI login.

## Operations

### Pinning the Claude Code CLI version

The Dockerfile pins via an ARG. Two clean builds produce identical `claude --version`:

```sh
docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.120 open-terminal
```

Bump the default in the Dockerfile when you want the repo to track a new version.

### Disk cleanup

Nothing is auto-deleted. `/opt/cleanup.sh` is installed in the image for explicit runs:

```sh
# Dry-run (safe): see what would be deleted, nothing touched.
docker compose exec \
  -e CHAT_TTL_DAYS=30 -e SESSION_TTL_DAYS=90 -e CLEANUP_DRY_RUN=true \
  open-terminal /opt/cleanup.sh

# Execute:
docker compose exec \
  -e CHAT_TTL_DAYS=30 -e SESSION_TTL_DAYS=90 -e CLEANUP_DRY_RUN=false \
  open-terminal /opt/cleanup.sh
```

Schedule nightly via host cron if desired. Defaults to dry-run to prevent surprise deletions.

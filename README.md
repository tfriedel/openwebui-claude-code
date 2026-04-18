# OpenWebUI Claude Code Pipe

Run [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)'s agent loop from inside [Open WebUI](https://github.com/open-webui/open-webui) chats, via the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

This is an Open WebUI **Pipe** that exposes Claude Code as a selectable model. Each chat gets its own isolated workspace directory; agent turns within the same chat resume the same Claude Code session, so context (files, prior tool calls) carries forward.

## Features

- **Full Claude Code agent loop** — Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch (configurable allowlist)
- **Per-chat workspaces** — each `chat_id` gets a sandboxed working directory that persists across turns
- **Dual auth** — bring your own Anthropic **API key** (pay-per-token) *or* a **Claude Pro/Max OAuth token** (bills against your subscription)
- **Fast path for trivial messages** — when no tools are needed, bypasses the SDK and hits the Messages API directly (~300 ms instead of multi-second CLI cold start)
- **Streaming UI** — tool calls render inline with previews; generated images/PDFs/CSVs surface as artifacts in the chat
- **Configurable valves** — model, permission mode, tool allowlist, max turns, workspace root

## Requirements

- Open WebUI (any recent version with the Pipes/Functions framework)
- Python deps (auto-installed by Open WebUI from the file header):
  - `claude-agent-sdk>=0.1.60`
  - `anthropic>=0.40.0`
- The `claude` CLI must be available on the host running Open WebUI's Python backend (the SDK shells out to it). Install via `npm install -g @anthropic-ai/claude-code`.

## Installation

1. In Open WebUI, go to **Workspace → Functions → +** (or **Admin Panel → Functions**).
2. Paste the contents of [`claude_agent_pipe.py`](./claude_agent_pipe.py) into the editor.
3. Save and enable the function.
4. Open the function's **Valves** and configure auth (one of):
   - `ANTHROPIC_API_KEY` — standard pay-per-token billing
   - `CLAUDE_CODE_OAUTH_TOKEN` — generate on a machine with a browser via `claude setup-token`; bills against your Pro/Max/Team subscription
5. A new model named **Claude Code** will appear in the model picker.

## Configuration (Valves)

| Valve | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(env)* | Anthropic API key. Falls back to the backend's env var. |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(empty)* | Claude subscription OAuth token. Takes priority over the API key when set. |
| `MODEL` | `claude-haiku-4-5` | Claude model ID (e.g. `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`). |
| `PERMISSION_MODE` | `bypassPermissions` | `default`, `acceptEdits`, `bypassPermissions`, `plan`, or `dontAsk`. |
| `ALLOWED_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch` | Comma-separated tools auto-approved without prompting. |
| `WORKDIR_ROOT` | `/tmp/claude-agent-pipe` | Root directory for per-chat workspaces. |
| `MAX_TURNS` | `30` | Max agent turns per user message. `0` disables the cap. |

## Auth notes

When both auth methods are present, the OAuth token wins and the API key is unset before invoking the SDK so it can't override.

Per Anthropic's terms: a Claude subscription is for personal use — **don't re-offer subscription auth to other end users** through a shared Open WebUI deployment. For multi-user setups, use API keys.

## License

MIT

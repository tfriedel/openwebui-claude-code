"""Run Claude Code inside an open-terminal sandbox.

Replaces the Claude Agent SDK's in-process invocation with a remote one: we
shell the `claude` CLI inside a per-user Linux account on the open-terminal
container, and consume its stream-json output.

Why stream-json over the SDK: the SDK runs `claude` in the *local* process;
there's no hook for "spawn it on a remote machine." Shelling `claude` with
`--output-format stream-json` gives us the same event stream (assistant
messages, tool_use, tool_result, result) that the SDK surfaces, just as
newline-delimited JSON we can forward straight to the pipe's UI renderer.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from open_terminal_client import OpenTerminalClient, ProcessHandle


@dataclass
class ClaudeRunConfig:
    model: str = "claude-haiku-4-5"
    permission_mode: str = "bypassPermissions"
    allowed_tools: tuple[str, ...] = (
        "Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    )
    max_turns: int = 30
    # Persisted across turns: `claude --resume <session_id>` picks up
    # conversation state. We store the id per OWUI chat on the pipe side.
    resume_session_id: Optional[str] = None
    # OAuth token or API key. Passed via env so it never hits argv.
    oauth_token: Optional[str] = None
    api_key: Optional[str] = None
    # Working dir inside the user's home. The pipe should stick to one
    # directory per OWUI chat so artifacts persist across turns.
    workdir: str = "~/workspace"


def _build_env(cfg: ClaudeRunConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if cfg.oauth_token:
        # OAuth takes priority — matches the existing pipe's contract.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = cfg.oauth_token
    elif cfg.api_key:
        env["ANTHROPIC_API_KEY"] = cfg.api_key
    return env


def _env_prefix(env: dict[str, str]) -> str:
    """Render env assignments as a safely-quoted shell prefix.
    shlex.quote guarantees no injection via the values."""
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())


def _build_claude_command(prompt: str, cfg: ClaudeRunConfig) -> str:
    parts = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--model", shlex.quote(cfg.model),
        "--permission-mode", shlex.quote(cfg.permission_mode),
        "--allowed-tools", shlex.quote(",".join(cfg.allowed_tools)),
    ]
    if cfg.max_turns and cfg.max_turns > 0:
        parts += ["--max-turns", str(cfg.max_turns)]
    if cfg.resume_session_id:
        parts += ["--resume", shlex.quote(cfg.resume_session_id)]
    parts.append(shlex.quote(prompt))
    return " ".join(parts)


async def run_claude(
    client: OpenTerminalClient,
    user_id: str,
    prompt: str,
    cfg: ClaudeRunConfig,
    *,
    session_id: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Yield parsed stream-json events from Claude Code running in the sandbox.

    Each event is a dict with a `type` key: 'system', 'assistant', 'user'
    (tool results), 'result'. The pipe's renderer already understands these
    shapes from the SDK path — they're the same schema, just transport-wrapped.
    """
    env = _build_env(cfg)
    # Ensure workspace exists, then run claude. A single compound command
    # keeps it one process the pipe can cancel with one DELETE.
    mkdir = f"mkdir -p {shlex.quote(cfg.workdir)}"
    cd = f"cd {shlex.quote(cfg.workdir)}"
    prefix = _env_prefix(env)
    cmd = _build_claude_command(prompt, cfg)
    full = f"{mkdir} && {cd} && {prefix} {cmd}"

    handle: ProcessHandle = await client.start(
        user_id=user_id,
        command=full,
        session_id=session_id,
    )

    # open-terminal merges stdout/stderr via PTY, so auth warnings printed
    # by `claude` on stderr will interleave with stream-json on stdout.
    # Non-JSON lines surface as synthetic _raw events.
    buf = ""
    async for stream, chunk in client.stream_output(handle, session_id=session_id):
        if stream == "exit":
            if buf.strip():
                try:
                    yield json.loads(buf)
                except json.JSONDecodeError:
                    yield {"type": "_raw", "text": buf.strip()}
                buf = ""
            yield {"type": "_exit", "code": int(chunk)}
            return
        buf += chunk
        # stream-json is newline-delimited; one JSON object per line.
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Claude CLI prints warnings before the stream starts;
                # expose them rather than swallow silently.
                yield {"type": "_raw", "text": line}

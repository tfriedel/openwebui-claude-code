"""
title: Claude Code (Sandboxed)
description: Run Claude Code inside an open-webui/open-terminal sandbox, one
  Linux account per OpenWebUI user. Isolates the agent's file/process reach
  from the Open WebUI backend host.
author: Thomas Friedel
version: 0.1
license: MIT
requirements: httpx>=0.27
"""

# Why this pipe exists
# --------------------
# The sibling `claude_agent_pipe.py` runs Claude Code's agent loop *in-process*
# via the Claude Agent SDK. That means Bash/Read/Write/Edit calls hit the
# Open WebUI backend's filesystem with whatever UID that process has — fine
# for a solo dev setup, dangerous in any shared deployment.
#
# This variant shells the `claude` CLI inside a per-user Linux account on an
# open-webui/open-terminal container. Isolation is by standard Unix
# permissions (shared kernel). Ship this when you have multiple users who
# you'd rather keep from stomping on each other's workspaces.
#
# Not yet ported from the SDK pipe:
#   - Knowledge-base MCP tool (Workspace Model KB attachments). The SDK pipe
#     registers an in-process MCP tool that calls OpenWebUI's retrieval layer
#     directly. In the sandbox the `claude` CLI can't reach Python functions;
#     we'd need to expose the KB as an HTTP MCP server from the OWUI backend
#     and `claude mcp add` it inside the container. Left as a follow-up.
#   - Partial-message token streaming for thinking blocks. stream-json emits
#     full `assistant` events, which is fine for text but slightly chunkier
#     UX than the SDK's `include_partial_messages`. Can be upgraded later
#     via `claude --verbose` stream_event passthrough.

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf", ".csv", ".tsv", ".txt", ".md", ".json", ".yaml", ".yml",
    ".html", ".xml", ".xlsx", ".docx", ".pptx", ".zip",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# OpenWebUI calls pipe() fresh for each chat turn. Same pattern as the SDK
# pipe — in-process map of chat_id → claude session_id so `claude --resume`
# picks up prior turns' state.
_chat_sessions: Dict[str, str] = {}


# ============================================================================
# open-terminal client
# ============================================================================

_USER_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
_CHAT_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_user_id(raw: str) -> str:
    """Match open-terminal's server-side sanitation so we can predict the
    Linux username it provisions (useful for logging)."""
    return _USER_ID_SAFE.sub("", raw)[:32] or "anon"


def _safe_chat_id(raw: str) -> str:
    """Strip everything except alphanumerics, dash, and underscore. OWUI's
    chat_ids are UUIDs so this is a no-op in practice, but it lets us embed
    the id directly in shell commands without shlex.quote — which is
    necessary because shlex.quote wraps in single quotes and single quotes
    suppress the `~` and `$HOME` expansion our workdir templates rely on."""
    return _CHAT_ID_SAFE.sub("", raw) or "default"


@dataclass
class _ProcessHandle:
    process_id: str
    user_id: str


class OpenTerminalError(RuntimeError):
    pass


class OpenTerminalClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self._base, headers=self._headers, timeout=self._timeout
        )
        return self

    async def __aexit__(self, *_exc):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _uh(self, user_id: str, session_id: Optional[str] = None) -> dict:
        h = {"X-User-Id": _sanitize_user_id(user_id)}
        if session_id:
            h["X-Session-Id"] = session_id
        return h

    async def start(
        self,
        user_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> _ProcessHandle:
        assert self._client is not None
        payload: dict = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        r = await self._client.post(
            "/execute", json=payload, headers=self._uh(user_id, session_id)
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"POST /execute {r.status_code}: {r.text}")
        # open-terminal returns {id, command, status, output, next_offset, ...}.
        # `id` here is the process identifier (not related to X-User-Id).
        return _ProcessHandle(process_id=r.json()["id"], user_id=user_id)

    async def stream_output(
        self,
        handle: _ProcessHandle,
        *,
        session_id: Optional[str] = None,
        wait: float = 10.0,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield (stream, chunk) tuples until the process exits.

        Uses the long-poll pattern: `GET /execute/{id}/status?wait=N&offset=K`
        returns as soon as new output appears or the process exits, up to N
        seconds. `offset` is advanced by the cumulative number of entries
        we've already consumed (from the server's `next_offset`).

        Stream values: 'output' for merged stdout/stderr (open-terminal runs
        commands through a PTY so both streams are combined), and 'exit' as
        a synthetic terminator carrying the exit code.
        """
        assert self._client is not None
        headers = self._uh(handle.user_id, session_id)
        offset = 0
        # Per-request timeout must exceed `wait`, or httpx aborts while the
        # server is still happily long-polling.
        read_timeout = wait + 10.0
        while True:
            r = await self._client.get(
                f"/execute/{handle.process_id}/status",
                params={"wait": wait, "offset": offset},
                headers=headers,
                timeout=read_timeout,
            )
            if r.status_code >= 400:
                raise OpenTerminalError(f"status: {r.status_code} {r.text}")
            data = r.json()
            for entry in data.get("output") or []:
                # All entries currently arrive with type='output'. Strip the
                # PTY's CR so downstream splitting on '\n' works cleanly.
                text = (entry.get("data") or "").replace("\r\n", "\n").replace("\r", "\n")
                if text:
                    yield "output", text
            offset = data.get("next_offset", offset)
            if data.get("status") != "running":
                yield "exit", str(data.get("exit_code") or 0)
                return

    async def kill(self, handle: _ProcessHandle, *, force: bool = False) -> None:
        assert self._client is not None
        try:
            await self._client.delete(
                f"/execute/{handle.process_id}",
                params={"force": str(force).lower()},
                headers=self._uh(handle.user_id),
            )
        except Exception:
            pass  # best-effort cleanup

    async def list_files(self, user_id: str, path: str) -> List[dict]:
        assert self._client is not None
        r = await self._client.get(
            "/files/list", params={"path": path}, headers=self._uh(user_id)
        )
        if r.status_code >= 400:
            return []
        return r.json().get("entries", [])

    async def read_file(self, user_id: str, path: str) -> bytes:
        """Download a file's raw bytes. Uses `/files/view` (not `/files/read`)
        — /read returns a JSON envelope `{path, content}` for text and 415
        for binary, so it's unusable for PNG/PDF/ZIP artifacts."""
        assert self._client is not None
        r = await self._client.get(
            "/files/view", params={"path": path}, headers=self._uh(user_id)
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"view {path}: {r.status_code}")
        return r.content

    async def ensure_dir(self, user_id: str, path: str) -> None:
        """Create a directory in the user's home if missing. `path` must be
        pre-sanitized (alphanumeric + `/._-` + `~` / `$HOME`): it's embedded
        unquoted so bash can expand `~` and variables. We can't shlex.quote
        here — single quotes suppress the very expansion we need."""
        handle = await self.start(user_id, f"mkdir -p {path}")
        async for _ in self.stream_output(handle):
            pass


# ============================================================================
# claude CLI invocation + stream-json parsing
# ============================================================================


@dataclass
class _ClaudeRunConfig:
    model: str
    permission_mode: str
    allowed_tools: List[str]
    max_turns: int
    resume_session_id: Optional[str]
    # URL of the credential-injecting proxy the sandbox should route Claude API
    # traffic through. Real ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN live on
    # the proxy container only; the sandbox never sees them.
    anthropic_base_url: str
    workdir: str
    system_prompt_append: Optional[str]


# Baked into the system prompt on every turn so Claude knows the sandbox's
# quirks without the user having to phrase requests carefully. Kept short —
# it's prepended to whatever the Workspace Model's system prompt says.
_SANDBOX_ENV_NOTE = (
    "You are running in a headless Linux sandbox (no display, no audio). "
    "For any plot or chart, ALWAYS save it to a file in the current working "
    "directory with an explicit extension (e.g. matplotlib → "
    "plt.savefig('name.png'); pandas/plotly → .to_html or kaleido png). "
    "plt.show() is a no-op here. The host automatically inlines any new "
    "PNG/PDF/CSV/HTML/… files from the working directory into the chat "
    "after each turn, so the user will see saved files without you needing "
    "to read them back."
)


def _claude_command(prompt: str, cfg: _ClaudeRunConfig) -> str:
    env_parts: List[str] = []
    # Route all Anthropic API calls through the credential-injecting proxy.
    # Claude Code honours ANTHROPIC_BASE_URL; the proxy adds the real
    # credential before forwarding to api.anthropic.com. No token in env or
    # argv → `env`, `printenv`, `cat /proc/self/environ` reveal nothing
    # Anthropic-shaped from inside the sandbox.
    env_parts.append(f"ANTHROPIC_BASE_URL={shlex.quote(cfg.anthropic_base_url)}")
    # The CLI refuses to talk to a non-anthropic.com base URL unless it also
    # sees a non-empty auth variable. ANTHROPIC_AUTH_TOKEN is the docs'
    # recommended variable for gateway/proxy setups — the CLI sends it as
    # `Authorization: Bearer ...`, which the proxy then unconditionally
    # overwrites with the real credential. The placeholder value is never
    # seen by Anthropic.
    env_parts.append("ANTHROPIC_AUTH_TOKEN=proxied")
    # `claude --dangerously-skip-permissions` still refuses root unless told
    # it's sandboxed. open-terminal runs processes as the per-user UID so this
    # rarely matters, but setting it is harmless.
    env_parts.append("IS_SANDBOX=1")
    # Force matplotlib's non-interactive backend. Without this, any stray
    # `plt.show()` can trigger a DISPLAY-not-found exception that aborts the
    # script before savefig runs.
    env_parts.append("MPLBACKEND=Agg")

    cli_parts = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--verbose",  # required for stream-json
        "--model", shlex.quote(cfg.model),
        "--permission-mode", shlex.quote(cfg.permission_mode),
        "--allowed-tools", shlex.quote(",".join(cfg.allowed_tools)),
    ]
    if cfg.max_turns and cfg.max_turns > 0:
        cli_parts += ["--max-turns", str(cfg.max_turns)]
    if cfg.resume_session_id:
        cli_parts += ["--resume", shlex.quote(cfg.resume_session_id)]

    # Always prepend the sandbox environment note; merge in whatever the
    # Workspace Model configured. One --append-system-prompt flag carries both.
    system_append = _SANDBOX_ENV_NOTE
    if cfg.system_prompt_append:
        system_append = f"{_SANDBOX_ENV_NOTE}\n\n{cfg.system_prompt_append}"
    cli_parts += ["--append-system-prompt", shlex.quote(system_append)]

    cli_parts.append(shlex.quote(prompt))

    return " ".join(env_parts + cli_parts)


async def _stream_claude_events(
    client: OpenTerminalClient,
    user_id: str,
    prompt: str,
    cfg: _ClaudeRunConfig,
    *,
    session_id: Optional[str],
) -> AsyncIterator[dict]:
    """Start claude in the sandbox and yield parsed stream-json events.
    Synthetic event types (underscore-prefixed) surface transport-level
    signals the caller may want (stderr diagnostics, exit code)."""
    await client.ensure_dir(user_id, cfg.workdir)
    cmd = _claude_command(prompt, cfg)
    # workdir is unquoted on purpose — see ensure_dir docstring. cfg.workdir
    # has already been rendered through _safe_chat_id so it's shell-safe.
    full = f"cd {cfg.workdir} && {cmd}"
    handle = await client.start(user_id, full, session_id=session_id)

    # open-terminal merges stdout+stderr through a PTY, so anything the CLI
    # prints on stderr (auth warnings, config notices) will interleave with
    # the stream-json lines on stdout. We parse every line as JSON; those
    # that fail become synthetic _raw events so the caller can log them
    # without disrupting the agent loop.
    buf = ""
    try:
        async for stream, chunk in client.stream_output(handle, session_id=session_id):
            if stream == "exit":
                if buf.strip():
                    # Flush any trailing unterminated line.
                    try:
                        yield json.loads(buf)
                    except json.JSONDecodeError:
                        yield {"type": "_raw", "text": buf.strip()}
                    buf = ""
                yield {"type": "_exit", "code": int(chunk)}
                return
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "_raw", "text": line}
    except asyncio.CancelledError:
        await client.kill(handle, force=True)
        raise


# ============================================================================
# Rendering helpers (kept in sync with claude_agent_pipe.py)
# ============================================================================

_TOOL_PREVIEW_FIELDS = {
    "Bash": "command", "Read": "file_path", "Write": "file_path",
    "Edit": "file_path", "Glob": "pattern", "Grep": "pattern",
    "WebSearch": "query", "WebFetch": "url", "Task": "description",
}
_FENCE_LANG_PER_TOOL = {
    "Bash": "bash", "Glob": "text", "Grep": "text",
    "WebSearch": "text", "WebFetch": "text",
}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    if key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated + (" …" if "\n" in raw and not truncated.endswith("…") else "")


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    if not tool_input:
        return "```\n(no input)\n```"
    primary = _TOOL_PREVIEW_FIELDS.get(name)
    if primary and primary in tool_input:
        lang = _FENCE_LANG_PER_TOOL.get(name, "text")
        parts = [f"```{lang}\n{tool_input[primary]}\n```"]
        others = {k: v for k, v in tool_input.items() if k != primary}
        if others:
            parts.append(
                f"```json\n{json.dumps(others, indent=2, ensure_ascii=False)}\n```"
            )
        return "\n\n".join(parts)
    return f"```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"


def _format_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append(text if isinstance(text, str) else repr(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


# ============================================================================
# body / metadata extraction (ported verbatim from claude_agent_pipe.py)
# ============================================================================


def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


# ============================================================================
# Artifact upload (walks the sandbox workspace, uploads new files to OWUI)
# ============================================================================


async def _snapshot_workspace(
    client: OpenTerminalClient, user_id: str, workdir: str
) -> Dict[str, int]:
    """Take a name→mtime_ns snapshot of artifact-like files in the workspace.
    We compare before/after to detect what the agent produced this turn."""
    # Use find with -printf to get path + mtime in one call, avoiding N+1
    # HTTP hits against /files/list for every subdirectory.
    # workdir unquoted so `~` / `$HOME` expand; see OpenTerminalClient.ensure_dir.
    cmd = (
        f"find {workdir} -type f "
        rf"\( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' "
        rf"-o -iname '*.gif' -o -iname '*.svg' -o -iname '*.webp' "
        rf"-o -iname '*.pdf' -o -iname '*.csv' -o -iname '*.tsv' "
        rf"-o -iname '*.txt' -o -iname '*.md' -o -iname '*.json' "
        rf"-o -iname '*.yaml' -o -iname '*.yml' -o -iname '*.html' "
        rf"-o -iname '*.xml' -o -iname '*.xlsx' -o -iname '*.docx' "
        rf"-o -iname '*.pptx' -o -iname '*.zip' \) "
        rf"-printf '%T@\t%p\n' 2>/dev/null"
    )
    handle = await client.start(user_id, cmd)
    out = ""
    async for stream, chunk in client.stream_output(handle):
        if stream == "output":
            out += chunk
    snapshot: Dict[str, int] = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        mtime_str, path = line.split("\t", 1)
        try:
            snapshot[path] = int(float(mtime_str) * 1e9)
        except ValueError:
            continue
    return snapshot


async def _inline_new_artifacts(
    client: OpenTerminalClient,
    user_id: str,
    workdir: str,
    before: Dict[str, int],
    owui_user_id: Optional[str],
) -> List[str]:
    """Detect files created/modified since `before`, download them from the
    sandbox, and register them as OpenWebUI artifacts. See the SDK pipe for
    why we serve via /api/v1/files/{id}/content instead of data URIs."""
    if not owui_user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]

    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        log.warning("artifact inline: OWUI file store unavailable: %s", exc)
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    after = await _snapshot_workspace(client, user_id, workdir)
    new_paths = [p for p, m in after.items() if before.get(p) != m]
    new_paths.sort()
    log.info(
        "artifact inline: workdir=%s before=%d after=%d new=%d paths=%s",
        workdir, len(before), len(after), len(new_paths), new_paths[:10],
    )

    chunks: List[str] = []
    for remote_path in new_paths:
        name = remote_path.rsplit("/", 1)[-1]
        ext = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
        if ext not in _ARTIFACT_EXTENSIONS:
            continue

        try:
            content = await client.read_file(user_id, remote_path)
        except Exception as exc:
            chunks.append(f"\n\n_(Couldn't fetch {name}: {exc})_\n")
            continue

        size = len(content)
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {name}: {size // 1024 // 1024} MiB exceeds "
                f"{_MAX_ARTIFACT_BYTES // 1024 // 1024} MiB limit.)_\n"
            )
            continue

        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )
        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{name}"

        # Write through a temp buffer OWUI's Storage layer can consume.
        # It expects a file-like with .read(); BytesIO fits.
        import io
        try:
            stored, storage_path = Storage.upload_file(
                io.BytesIO(content),
                storage_filename,
                {"OpenWebUI-User-Id": owui_user_id, "OpenWebUI-File-Id": file_id},
            )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", remote_path)
            chunks.append(f"\n\n_(Failed to save {name}: {exc})_\n")
            continue

        try:
            Files.insert_new_file(
                owui_user_id,
                FileForm(
                    id=file_id,
                    filename=name,
                    path=storage_path,
                    data={},
                    meta={"name": name, "content_type": mime, "size": len(stored)},
                ),
            )
        except Exception as exc:
            log.exception("Artifact DB row failed: %s", remote_path)
            chunks.append(f"\n\n_(Saved but not linkable: {name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{name}](/api/v1/files/{file_id}/content)\n")
        else:
            kib = size // 1024
            chunks.append(
                f"\n\n📎 [{name}](/api/v1/files/{file_id}/content) · {kib} KiB\n"
            )
    return chunks


# ============================================================================
# Pipe
# ============================================================================


class Pipe:
    class Valves(BaseModel):
        OPEN_TERMINAL_URL: str = Field(
            default="http://open-terminal:8000",
            description=(
                "Base URL of the open-terminal sandbox. Use the docker-compose "
                "service name when both run in the same network."
            ),
        )
        OPEN_TERMINAL_API_KEY: str = Field(
            default="",
            description=(
                "Shared secret matching OPEN_TERMINAL_API_KEY on the sandbox "
                "container. Authenticates this pipe as a privileged caller; "
                "the end user is then conveyed via the X-User-Id header."
            ),
        )
        ANTHROPIC_BASE_URL: str = Field(
            default="http://anthropic-proxy:8081",
            description=(
                "Credential-injecting proxy the sandbox routes Anthropic "
                "traffic through. The real ANTHROPIC_API_KEY / "
                "CLAUDE_CODE_OAUTH_TOKEN live on the proxy container only — "
                "the sandbox never sees them, so `env` inside the agent "
                "reveals nothing Anthropic-shaped. Use the docker-compose "
                "service DNS name when both run in the same network."
            ),
        )
        MODEL: str = Field(default="claude-haiku-4-5")
        PERMISSION_MODE: str = Field(default="bypassPermissions")
        ALLOWED_TOOLS: str = Field(
            default="Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch"
        )
        MAX_TURNS: int = Field(default=30)
        WORKDIR_TEMPLATE: str = Field(
            default="~/chat-{chat_id}",
            description=(
                "Per-chat workspace path inside the user's sandbox home. "
                "{chat_id} is substituted. Artifacts persist across turns."
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        return [{"id": "claude-code-sandboxed", "name": "Claude Code (Sandboxed)"}]

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[str, None]:
        if not self.valves.OPEN_TERMINAL_API_KEY:
            yield "_Sandbox not configured: set the OPEN_TERMINAL_API_KEY valve._"
            return

        prompt = _extract_latest_user_prompt(body)
        if not prompt:
            yield "_No user message to send to Claude Code._"
            return

        chat_id = __chat_id__ or "default"
        owui_user_id = (__user__ or {}).get("id") or "anon"
        # The sandbox's X-User-Id drives Linux account provisioning. Tie it
        # to OWUI's stable user.id (not email/name — those can change).
        sandbox_user = owui_user_id
        # Render the workdir through _safe_chat_id so it's safe to embed
        # unquoted in shell commands. Unquoted is required for `~` and
        # `$HOME` in the template to expand.
        workdir = self.valves.WORKDIR_TEMPLATE.format(
            chat_id=_safe_chat_id(chat_id)
        )

        allowed_tools = [
            t.strip() for t in self.valves.ALLOWED_TOOLS.split(",") if t.strip()
        ]
        cfg = _ClaudeRunConfig(
            model=self.valves.MODEL,
            permission_mode=self.valves.PERMISSION_MODE,
            allowed_tools=allowed_tools,
            max_turns=self.valves.MAX_TURNS,
            resume_session_id=_chat_sessions.get(chat_id),
            anthropic_base_url=self.valves.ANTHROPIC_BASE_URL,
            workdir=workdir,
            system_prompt_append=_extract_system_prompt(body),
        )

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        active_tools: Dict[str, Dict[str, Any]] = {}
        heartbeat_task: Optional[asyncio.Task] = None

        async def _heartbeat() -> None:
            try:
                while active_tools:
                    await asyncio.sleep(2)
                    if not active_tools:
                        return
                    oldest = min(active_tools.values(), key=lambda t: t["started"])
                    elapsed = int(time.monotonic() - oldest["started"])
                    count = len(active_tools)
                    label = (
                        oldest["label"]
                        if count == 1
                        else f"{count} tools · longest {oldest['label']}"
                    )
                    await emit_status(f"⏳ {label} · running {elapsed}s…")
            except asyncio.CancelledError:
                pass

        def _ensure_heartbeat() -> None:
            nonlocal heartbeat_task
            if heartbeat_task is None or heartbeat_task.done():
                heartbeat_task = asyncio.create_task(_heartbeat())

        await emit_status(f"Starting Claude Code in sandbox ({_sanitize_user_id(sandbox_user)})…")

        try:
            async with OpenTerminalClient(
                self.valves.OPEN_TERMINAL_URL,
                self.valves.OPEN_TERMINAL_API_KEY,
            ) as client:
                await client.ensure_dir(sandbox_user, workdir)
                before_snapshot = await _snapshot_workspace(
                    client, sandbox_user, workdir
                )

                async for event in _stream_claude_events(
                    client, sandbox_user, prompt, cfg, session_id=chat_id
                ):
                    async for out in _handle_event(
                        event,
                        chat_id=chat_id,
                        active_tools=active_tools,
                        ensure_heartbeat=_ensure_heartbeat,
                        emit_status=emit_status,
                    ):
                        yield out

                await emit_status("Done.", done=True)
                for chunk in await _inline_new_artifacts(
                    client, sandbox_user, workdir, before_snapshot, owui_user_id
                ):
                    yield chunk

        except Exception as exc:
            log.exception("Sandboxed Claude Code pipe failed")
            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**Claude Code error:** `{type(exc).__name__}: {exc}`\n"
        finally:
            active_tools.clear()
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass


async def _handle_event(
    event: dict,
    *,
    chat_id: str,
    active_tools: Dict[str, Dict[str, Any]],
    ensure_heartbeat: Callable[[], None],
    emit_status: Callable[..., Any],
) -> AsyncGenerator[str, None]:
    """Translate one stream-json event into UI output.

    Event shapes (from `claude --output-format stream-json --verbose`):
      system    — {type:"system", subtype:"init", session_id:"..."}
      assistant — {type:"assistant", message:{content:[{type:"text"|"tool_use"|"thinking",...}]}}
      user      — {type:"user", message:{content:[{type:"tool_result", tool_use_id, content, is_error}]}}
      result    — {type:"result", subtype:"success"|..., total_cost_usd, duration_ms}
      _stderr/_exit/_raw — synthetic transport-layer signals from our runner.
    """
    etype = event.get("type")

    if etype == "system":
        if event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                _chat_sessions[chat_id] = sid
        return

    if etype == "assistant":
        content = ((event.get("message") or {}).get("content")) or []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                yield block.get("text", "")
            elif btype == "thinking":
                text = (block.get("thinking") or "").strip()
                if text:
                    await emit_status("💭 Thinking…")
                    yield (
                        "\n\n<details>\n<summary>💭 Thinking</summary>\n\n"
                        f"{text}\n\n</details>\n\n"
                    )
            elif btype == "tool_use":
                name = block.get("name", "tool")
                tool_input = block.get("input") or {}
                tool_id = block.get("id", "")
                preview = _tool_preview(name, tool_input)
                label = f"{name}: {preview}" if preview else name
                await emit_status(f"🔧 {label}")
                active_tools[tool_id] = {"label": label, "started": time.monotonic()}
                ensure_heartbeat()
                summary_text = f"🔧 {name}" + (f" · {preview}" if preview else "")
                body_block = _tool_input_block(name, tool_input)
                yield (
                    "\n\n<details>\n"
                    f"<summary>{summary_text}</summary>\n\n"
                    f"{body_block}\n\n</details>\n\n"
                )
        return

    if etype == "user":
        content = ((event.get("message") or {}).get("content")) or []
        for block in content:
            if block.get("type") != "tool_result":
                continue
            active_tools.pop(block.get("tool_use_id", ""), None)
            if block.get("is_error"):
                err = _format_tool_result(block.get("content"))[:800]
                yield (
                    "\n\n<details>\n<summary>"
                    "<sub>⚙️ tool hiccup (retrying)</sub>"
                    "</summary>\n\n"
                    f"```\n{err}\n```\n\n</details>\n\n"
                )
        return

    if etype == "result":
        subtype = event.get("subtype")
        if subtype and subtype != "success":
            yield f"\n\n_Agent stopped: {subtype}_\n"
        cost = event.get("total_cost_usd")
        dur = event.get("duration_ms")
        if cost is not None and dur is not None:
            yield f"\n\n_Cost: ${float(cost):.4f} · {dur}ms_\n"
        return

    if etype == "_stderr":
        # claude CLI prints auth/config warnings on stderr. Log but don't surface
        # to the chat unless they're fatal — the _exit event will carry that.
        log.debug("claude stderr: %s", event.get("text", "").strip())
        return

    if etype == "_exit":
        code = event.get("code", 0)
        if code != 0:
            yield f"\n\n_Sandbox process exited with code {code}._\n"
        return

    if etype == "_raw":
        log.debug("unparsed line from claude: %s", event.get("text", ""))
        return

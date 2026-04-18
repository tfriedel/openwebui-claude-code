"""Async client for the open-webui/open-terminal HTTP API.

Scoped to what the Claude Code pipe needs: run a command as a specific
end-user (via X-User-Id), poll its streaming output, and pull back any
artifact files the agent produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

_USER_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_user_id(raw: str) -> str:
    """Mirror open-terminal's own sanitation so we can predict the Linux
    username it will provision. The server sanitizes server-side too; doing
    it here lets callers log/reference the resolved identity."""
    return _USER_ID_SAFE.sub("", raw)[:32] or "anon"


@dataclass
class ProcessHandle:
    process_id: str
    user_id: str


class OpenTerminalError(RuntimeError):
    pass


class OpenTerminalClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
    ):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *_exc):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _user_headers(self, user_id: str, session_id: Optional[str] = None) -> dict:
        h = {"X-User-Id": sanitize_user_id(user_id)}
        if session_id:
            h["X-Session-Id"] = session_id
        return h

    async def start(
        self,
        user_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        stdin: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ProcessHandle:
        """Start a command in the user's account. Returns immediately with a
        process handle; use `stream_output` to consume stdout/stderr."""
        assert self._client is not None, "use as async context manager"
        payload: dict = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if stdin is not None:
            payload["stdin"] = stdin
        r = await self._client.post(
            "/execute",
            json=payload,
            headers=self._user_headers(user_id, session_id),
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"POST /execute {r.status_code}: {r.text}")
        return ProcessHandle(process_id=r.json()["id"], user_id=user_id)

    async def stream_output(
        self,
        handle: ProcessHandle,
        *,
        session_id: Optional[str] = None,
        wait: float = 10.0,
    ) -> AsyncIterator[tuple[str, str]]:
        """Long-poll status with `wait=N&offset=K` to yield new output
        entries as they arrive. Yields ('output', text) for each chunk and
        a final ('exit', exit_code_str). Stdout and stderr are merged by
        the server's PTY — we strip the resulting CRLFs."""
        assert self._client is not None
        headers = self._user_headers(handle.user_id, session_id)
        offset = 0
        read_timeout = wait + 10.0
        while True:
            r = await self._client.get(
                f"/execute/{handle.process_id}/status",
                params={"wait": wait, "offset": offset},
                headers=headers,
                timeout=read_timeout,
            )
            if r.status_code >= 400:
                raise OpenTerminalError(
                    f"status {handle.process_id}: {r.status_code} {r.text}"
                )
            data = r.json()
            for entry in data.get("output") or []:
                text = (entry.get("data") or "").replace("\r\n", "\n").replace("\r", "\n")
                if text:
                    yield "output", text
            offset = data.get("next_offset", offset)
            if data.get("status") != "running":
                yield "exit", str(data.get("exit_code") or 0)
                return

    async def kill(self, handle: ProcessHandle, *, force: bool = False) -> None:
        assert self._client is not None
        await self._client.delete(
            f"/execute/{handle.process_id}",
            params={"force": str(force).lower()},
            headers=self._user_headers(handle.user_id),
        )

    async def send_stdin(self, handle: ProcessHandle, data: str) -> None:
        assert self._client is not None
        r = await self._client.post(
            f"/execute/{handle.process_id}/input",
            json={"input": data},
            headers=self._user_headers(handle.user_id),
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"stdin: {r.status_code} {r.text}")

    # ---- file ops (used to pull artifacts Claude produces in $HOME) ----

    async def read_file(self, user_id: str, path: str) -> bytes:
        assert self._client is not None
        r = await self._client.get(
            "/files/read",
            params={"path": path},
            headers=self._user_headers(user_id),
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"read {path}: {r.status_code}")
        return r.content

    async def list_files(self, user_id: str, path: str = "~") -> list[dict]:
        assert self._client is not None
        r = await self._client.get(
            "/files/list",
            params={"path": path},
            headers=self._user_headers(user_id),
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"list {path}: {r.status_code}")
        return r.json().get("entries", [])

    async def write_file(self, user_id: str, path: str, content: bytes | str) -> None:
        assert self._client is not None
        if isinstance(content, str):
            content = content.encode("utf-8")
        r = await self._client.post(
            "/files/write",
            params={"path": path},
            content=content,
            headers=self._user_headers(user_id),
        )
        if r.status_code >= 400:
            raise OpenTerminalError(f"write {path}: {r.status_code} {r.text}")

"""
title: Claude Code
description: Run Claude Code's agent loop from inside OpenWebUI chats via the Claude Agent SDK.
author: Thomas Friedel
version: 0.1
license: MIT
requirements: claude-agent-sdk>=0.1.60, anthropic>=0.40.0
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".xml",
    ".xlsx",
    ".docx",
    ".pptx",
    ".zip",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
# Safety cap to avoid uploading runaway files. Uploaded artifacts are served
# via OpenWebUI's file endpoint, so they don't bloat the chat history even
# when large — this is only a "don't accidentally ship a DVD ISO" guard.
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

log = logging.getLogger(__name__)

# OpenWebUI calls pipe() fresh for each chat turn. We keep a chat_id -> session_id
# map in-process so follow-up turns resume the same Claude Code session.
_chat_sessions: Dict[str, str] = {}


_TOOL_PREVIEW_FIELDS = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Task": "description",
}


def _tool_preview(name: str, tool_input: Dict[str, Any]) -> str:
    key = _TOOL_PREVIEW_FIELDS.get(name)
    if key and key in tool_input:
        raw = str(tool_input[key])
    elif tool_input:
        raw = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(tool_input.items())[:2])
    else:
        return ""
    # Collapse to one line — multi-line values break inline-code spans and leak
    # "# python comments" into markdown as H1 headings.
    first = raw.split("\n", 1)[0]
    truncated = first if len(first) <= 120 else first[:117] + "…"
    return truncated + (" …" if "\n" in raw and not truncated.endswith("…") else "")


_FENCE_LANG_PER_TOOL = {
    "Bash": "bash",
    "Glob": "text",
    "Grep": "text",
    "WebSearch": "text",
    "WebFetch": "text",
}


def _tool_input_block(name: str, tool_input: Dict[str, Any]) -> str:
    """Full tool invocation as fenced code block(s). If the tool has a known
    primary field (Bash→command, Read→file_path, …), render that with the
    right syntax highlight and append any other fields as a small JSON block.
    Tools with no known primary render as a single JSON block.
    """
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


def _iter_artifact_files(scan_dirs: List[Path]) -> "list[Path]":
    """Yield image/document artifacts from each scan dir. Workdir is searched
    recursively; other dirs (typically /tmp) are searched non-recursively to
    avoid picking up unrelated files under nested system caches."""
    seen: List[Path] = []
    for idx, root in enumerate(scan_dirs):
        if not root.exists():
            continue
        iterator = root.rglob("*") if idx == 0 else root.iterdir()
        for path in iterator:
            if path.is_file() and path.suffix.lower() in _ARTIFACT_EXTENSIONS:
                seen.append(path)
    return seen


def _snapshot_artifacts(scan_dirs: List[Path]) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in _iter_artifact_files(scan_dirs):
        try:
            snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return snapshot


def _inline_new_artifacts(
    scan_dirs: List[Path],
    before: Dict[str, int],
    user_id: Optional[str],
) -> List[str]:
    """Upload artifacts new or modified since `before` to OpenWebUI's file
    store, and return markdown referencing the served URLs.

    Why not base64 data URIs: large blobs (multi-MB PDFs) encoded as
    `data:application/pdf;base64,…` in a markdown link cause browsers to spam
    the address bar and stall when clicked. They'd also persist in chat
    history, bloating the DB on every turn.

    URL shape: `/api/v1/files/{id}/content` for every artifact.
      - Images: loaded by the markdown `<img>` tag → display inline.
      - PDFs: the route emits `Content-Disposition: inline` → browser opens
        them in its native PDF viewer (new tab).
      - Everything else: the route falls back to `attachment`, so clicking
        triggers a download (fine for CSV/XLSX/ZIP — they have no sensible
        inline view anyway).
    Deliberately avoids `/content/{filename}`, which hard-codes `attachment`
    for every type and so forces a download even for PDFs.
    """
    if not user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]
    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    chunks: List[str] = []
    for path in sorted(_iter_artifact_files(scan_dirs)):
        try:
            mtime = path.stat().st_mtime_ns
            size = path.stat().st_size
        except OSError:
            continue
        if before.get(str(path)) == mtime:
            continue  # untouched
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: {size // 1024 // 1024} MiB exceeds {_MAX_ARTIFACT_BYTES // 1024 // 1024} MiB limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(path.name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )

        file_id = str(uuid.uuid4())
        storage_filename = f"{file_id}_{path.name}"
        try:
            with path.open("rb") as handle:
                contents, storage_path = Storage.upload_file(
                    handle,
                    storage_filename,
                    {
                        "OpenWebUI-User-Id": user_id,
                        "OpenWebUI-File-Id": file_id,
                    },
                )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", path)
            chunks.append(f"\n\n_(Failed to save {path.name}: {exc})_\n")
            continue

        try:
            Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id,
                    filename=path.name,
                    path=storage_path,
                    data={},
                    meta={
                        "name": path.name,
                        "content_type": mime,
                        "size": len(contents),
                    },
                ),
            )
        except Exception as exc:
            log.exception("Artifact DB row failed: %s", path)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{path.name}](/api/v1/files/{file_id}/content)\n")
        else:
            kib = size // 1024
            chunks.append(
                f"\n\n📎 [{path.name}](/api/v1/files/{file_id}/content) · {kib} KiB\n"
            )
    return chunks


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    """Collect `role=system` content from body.messages. OpenWebUI merges the
    Workspace Model's configured system prompt into messages[0] before the
    pipe is called (payload.py:apply_system_prompt_to_body)."""
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


def _knowledge_collections(
    metadata: Optional[Dict[str, Any]],
    files: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Extract attached knowledge collections.

    Priority order:
    1. `metadata["model"]["info"]["meta"]["knowledge"]` — the authoritative
       Workspace Model config, populated regardless of the `function_calling`
       gate. This is the only source that works when the Workspace Model
       sets `function_calling=native` (which disables OpenWebUI's auto-RAG).
    2. `files` (__files__) — populated by the middleware's auto-RAG branch
       when `function_calling != "native"`. Useful for non-native chats or
       as a fallback.
    """
    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(coll: Any, name: Any) -> None:
        if not coll:
            return
        cid = str(coll)
        if cid in seen:
            return
        seen.add(cid)
        out.append({"id": cid, "name": str(name or cid)})

    def _consume(item: Dict[str, Any]) -> None:
        """Mirror OpenWebUI's vector-collection naming
        (retrieval/utils.py:get_sources_from_items)."""
        name = item.get("name")
        # Old-style: explicit collection_name(s). Legacy KBs with multiple colls.
        if item.get("collection_name"):
            _add(item["collection_name"], name)
            return
        if item.get("collection_names"):
            for coll in item["collection_names"]:
                _add(coll, name)
            return
        # Modern: collection name derived from the knowledge row's id.
        item_type = item.get("type")
        if item_type == "collection" and item.get("id"):
            _add(item["id"], name)
        elif item_type == "file" and item.get("id"):
            # legacy single-file entries skip the prefix; modern ones prepend "file-".
            coll = item["id"] if item.get("legacy") else f"file-{item['id']}"
            _add(coll, name)

    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if isinstance(item, dict):
            _consume(item)

    for f in files or []:
        if isinstance(f, dict):
            _consume(f)

    return out


def _knowledge_row_ids(metadata: Optional[Dict[str, Any]]) -> List[str]:
    """Return knowledge-table row ids for the attached Workspace-Model KBs.
    These are the IDs used to look up files via Knowledges.get_files_by_id().
    Only populated for `type: "collection"` entries — single-file entries
    aren't exposed to list/read/grep (search still works for those)."""
    ids: List[str] = []
    model_knowledge = ((metadata or {}).get("model") or {}).get("info", {}).get(
        "meta", {}
    ).get("knowledge") or []
    for item in model_knowledge:
        if (
            isinstance(item, dict)
            and item.get("type") == "collection"
            and item.get("id")
        ):
            ids.append(str(item["id"]))
    return ids


def _build_kb_mcp_server(
    knowledge: List[Dict[str, str]],
    knowledge_row_ids: Optional[List[str]] = None,
    user_dict: Optional[Dict[str, Any]] = None,
    event_emitter: Optional[Callable] = None,
):
    """Return (mcp_config, tool_names) for a knowledge-base search tool Claude
    can invoke, or (None, []) if no KBs are attached.

    Result formatting follows OpenWebUI's native RAG shape (<source id=N ...>
    tags + citation event), so Claude's replies render with inline [N]
    citations and populate the sources side-panel. Access control is
    enforced implicitly: the closure captures only the collection_names
    that OpenWebUI's middleware already filtered by the user's grants.
    """
    if not knowledge:
        return None, []

    collection_names = [k["id"] for k in knowledge]
    display = ", ".join(k["name"] for k in knowledge)

    @tool(
        "search_knowledge",
        (
            f"Search the attached knowledge base(s): {display}. "
            "Call whenever you need internal facts, prior guidance, or "
            "documented product/process details. Reformulate and search "
            "multiple times if the first query misses."
        ),
        {"query": str, "top_k": int},
    )
    async def _search(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from open_webui.main import app
            from open_webui.models.users import Users
            from open_webui.retrieval.utils import query_collection
        except Exception as exc:
            return {
                "content": [
                    {"type": "text", "text": f"Knowledge search unavailable: {exc}"}
                ]
            }

        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "content": [
                    {"type": "text", "text": "Empty query — nothing to search."}
                ]
            }
        # Default to OpenWebUI's configured RAG_TOP_K so the tool respects the
        # admin's retrieval setting. Fall back to 5 if unreadable.
        try:
            default_top_k = int(app.state.config.RAG_TOP_K.value)
        except Exception:
            default_top_k = 5
        top_k = int(args.get("top_k") or default_top_k)

        # Resolve a proper UserModel so embedding_function can attribute
        # usage / rate-limits per user (some backends require it).
        user_obj = None
        user_id = (user_dict or {}).get("id")
        if user_id:
            try:
                user_obj = Users.get_user_by_id(user_id)
            except Exception:
                pass

        async def _embed(queries, prefix=None):
            return await app.state.EMBEDDING_FUNCTION(
                queries, prefix=prefix, user=user_obj
            )

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔎 Searching KB: {query[:80]}",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        try:
            results = await query_collection(
                collection_names=collection_names,
                queries=[query],
                embedding_function=_embed,
                k=top_k,
            )
        except Exception as exc:
            log.exception("KB search failed")
            return {"content": [{"type": "text", "text": f"Search failed: {exc}"}]}

        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []

        if not docs:
            return {
                "content": [
                    {"type": "text", "text": f"No passages found for: {query!r}"}
                ]
            }

        # Surface citations in OpenWebUI's sources side-panel.
        # Emit one event per document so each source gets its own filename label
        # (a single event with a shared source.name collapses all sources into
        # one label in the UI).
        if event_emitter:
            dist_iter = dists or [None] * len(metas)
            for doc, meta, dist in zip(docs, metas, dist_iter):
                meta = meta or {}
                source_name = (
                    meta.get("name")
                    or meta.get("source")
                    or meta.get("title")
                    or "unknown"
                )
                try:
                    await event_emitter(
                        {
                            "type": "citation",
                            "data": {
                                "document": [doc],
                                "metadata": [
                                    {
                                        "source": source_name,
                                        "file_id": meta.get("file_id", ""),
                                        "relevance_score": (
                                            round(float(dist), 3)
                                            if dist is not None
                                            else None
                                        ),
                                    }
                                ],
                                "source": {"name": source_name},
                            },
                        }
                    )
                except Exception:
                    pass

        # XML <source> tags = OpenWebUI's native RAG format → renders as [N] citations.
        parts = [f"Found {len(docs)} passage(s) for {query!r}:\n"]
        for i, (doc, meta) in enumerate(zip(docs, metas), 1):
            meta = meta or {}
            name = (
                meta.get("source") or meta.get("name") or meta.get("title") or "unknown"
            )
            parts.append(f'<source id="{i}" name="{name}">{doc}</source>')
        parts.append(
            "\nCite these using [1], [2], … in your response. Do not include the XML tags themselves."
        )
        return {"content": [{"type": "text", "text": "\n\n".join(parts)}]}

    # ---------- Agentic helpers: list / read / grep ---------------------------
    # Scoped to `knowledge_row_ids` (type=collection entries only). Single-file
    # KBs (type=file) are searchable but not listable/readable by these tools.
    kb_ids: List[str] = list(knowledge_row_ids or [])

    async def _iter_scoped_files():
        from open_webui.models.knowledge import Knowledges

        for kid in kb_ids:
            try:
                files = Knowledges.get_files_by_id(kid) or []
            except Exception:
                continue
            for f in files:
                yield f

    async def _allowed_file_ids() -> Set[str]:
        return {f.id async for f in _iter_scoped_files()}

    @tool(
        "list_knowledge_documents",
        (
            f"List every document in the attached knowledge base(s): {display}. "
            "Returns file_id, filename, and size for each. Use before "
            "read_knowledge_document or grep_knowledge when you need to know "
            "what's there."
        ),
        {},
    )
    async def _list_docs(args: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
        if not kb_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "No enumerable knowledge collections attached.",
                    }
                ]
            }
        lines = [f"Documents in {display}:"]
        count = 0
        async for f in _iter_scoped_files():
            content = (f.data or {}).get("content", "") or ""
            lines.append(
                f"- file_id={f.id} · {f.filename} · {len(content) // 1024} KiB · {len(content)} chars"
            )
            count += 1
        if count == 0:
            lines.append("(no files found)")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "read_knowledge_document",
        (
            "Read the full content of a knowledge document (or a character "
            "range of it) by file_id. Use to zoom into a doc that "
            "search_knowledge found, or read it top-to-bottom if small. "
            "Omit start_char/end_char to read the whole file. "
            "Each call caps at 40 000 chars; page using start_char/end_char "
            "if the file is larger."
        ),
        {"file_id": str, "start_char": int, "end_char": int},
    )
    async def _read_doc(args: Dict[str, Any]) -> Dict[str, Any]:
        from open_webui.models.files import Files

        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return {"content": [{"type": "text", "text": "file_id is required."}]}
        allowed = await _allowed_file_ids()
        if file_id not in allowed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"file_id {file_id!r} is not in the attached knowledge base(s).",
                    }
                ]
            }

        try:
            file_obj = Files.get_file_by_id(file_id)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Lookup failed: {exc}"}]}
        if file_obj is None:
            return {"content": [{"type": "text", "text": "File not found."}]}

        content = (file_obj.data or {}).get("content", "") or ""
        total = len(content)
        start = max(0, int(args.get("start_char") or 0))
        raw_end = args.get("end_char")
        end = total if raw_end in (None, 0) else min(total, max(start, int(raw_end)))
        # Hard cap per call to avoid flooding the context window.
        MAX_CHARS = 40_000
        if end - start > MAX_CHARS:
            end = start + MAX_CHARS
        slice_ = content[start:end]
        header = (
            f"# {file_obj.filename}\n"
            f"_chars {start}..{end} of {total}"
            f"{' (truncated — call again with a higher start_char to continue)' if end < total else ''}_\n\n"
        )
        return {"content": [{"type": "text", "text": header + slice_}]}

    @tool(
        "grep_knowledge",
        (
            "Regex/substring search across knowledge documents. Runs against "
            "pre-extracted plain text in the database (no PDF re-parsing), "
            "so it's fast. Use for exact keywords, product codes, "
            "acronyms where vector search struggles.\n\n"
            "- `pattern` (required): regex or literal string\n"
            "- `file_id` (optional): if set, grep only that file; omit or "
            "leave empty to grep the whole knowledge base\n"
            "- `case_insensitive` (default true)\n"
            "- `max_matches` (default 30)\n\n"
            "Returns each hit with 80 chars of surrounding context, the "
            "source filename, and the char offset — use that offset with "
            "read_knowledge_document to fetch more context."
        ),
        {
            "pattern": str,
            "file_id": str,
            "case_insensitive": bool,
            "max_matches": int,
        },
    )
    async def _grep(args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return {"content": [{"type": "text", "text": "pattern is required."}]}
        flags = re.IGNORECASE if args.get("case_insensitive", True) else 0
        max_matches = int(args.get("max_matches") or 30)
        file_id_filter = str(args.get("file_id") or "").strip() or None
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"content": [{"type": "text", "text": f"Invalid regex: {exc}"}]}

        if file_id_filter:
            allowed = await _allowed_file_ids()
            if file_id_filter not in allowed:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"file_id {file_id_filter!r} is not in the attached knowledge base(s).",
                        }
                    ]
                }

        hits: List[str] = []
        files_scanned = 0
        async for f in _iter_scoped_files():
            if file_id_filter and f.id != file_id_filter:
                continue
            files_scanned += 1
            content = (f.data or {}).get("content", "") or ""
            for m in compiled.finditer(content):
                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(content), m.end() + 80)
                ctx = content[ctx_start:ctx_end].replace("\n", " ")
                hits.append(
                    f"- **{f.filename}** (file_id={f.id}) @ char {m.start()}:\n  …{ctx}…"
                )
                if len(hits) >= max_matches:
                    break
            if len(hits) >= max_matches:
                break

        scope = (
            f"1 file ({file_id_filter})"
            if file_id_filter
            else f"{files_scanned} file(s)"
        )
        if not hits:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"No matches for /{pattern}/ across {scope}.",
                    }
                ]
            }
        header = (
            f"Found {len(hits)} match(es) for /{pattern}/ across {scope}"
            f"{' (capped at max_matches)' if len(hits) >= max_matches else ''}:\n"
        )
        return {"content": [{"type": "text", "text": header + "\n".join(hits)}]}

    tools_list = [_search]
    tool_names = ["mcp__helm-kb__search_knowledge"]
    tools_by_name: Dict[str, Any] = {"search_knowledge": _search}
    if kb_ids:
        tools_list.extend([_list_docs, _read_doc, _grep])
        tool_names.extend(
            [
                "mcp__helm-kb__list_knowledge_documents",
                "mcp__helm-kb__read_knowledge_document",
                "mcp__helm-kb__grep_knowledge",
            ]
        )
        tools_by_name.update(
            {
                "list_knowledge_documents": _list_docs,
                "read_knowledge_document": _read_doc,
                "grep_knowledge": _grep,
            }
        )

    server = create_sdk_mcp_server("helm-kb", "0.1", tools=tools_list)
    return server, tool_names, tools_by_name


def _anthropic_kb_tool_defs(
    knowledge: List[Dict[str, str]], has_kb_ids: bool
) -> List[Dict[str, Any]]:
    """JSON-Schema tool definitions for the Anthropic Messages API. Kept
    in sync with `_build_kb_mcp_server`'s tools (same names & input fields)
    so Claude sees an identical toolbox regardless of which path is active."""
    if not knowledge:
        return []
    display = ", ".join(k["name"] for k in knowledge)
    defs: List[Dict[str, Any]] = [
        {
            "name": "search_knowledge",
            "description": (
                f"Search the attached knowledge base(s): {display}. "
                "Call whenever you need internal facts, prior guidance, or "
                "documented product/process details. Reformulate and search "
                "multiple times if the first query misses."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        }
    ]
    if has_kb_ids:
        defs.extend(
            [
                {
                    "name": "list_knowledge_documents",
                    "description": (
                        f"List every document in {display}. "
                        "Returns file_id, filename, and size for each."
                    ),
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "read_knowledge_document",
                    "description": (
                        "Read full content (or a character range) of a "
                        "knowledge document by file_id. Omit start_char / "
                        "end_char to read the whole file. Caps at 40 000 "
                        "chars per call — page using start_char to continue."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string"},
                            "start_char": {"type": "integer"},
                            "end_char": {"type": "integer"},
                        },
                        "required": ["file_id"],
                    },
                },
                {
                    "name": "grep_knowledge",
                    "description": (
                        "Regex/substring search across knowledge documents. "
                        "Fast (runs on pre-extracted text in the DB). Use for "
                        "exact keywords, product codes, acronyms where vector "
                        "search struggles."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "file_id": {"type": "string"},
                            "case_insensitive": {"type": "boolean"},
                            "max_matches": {"type": "integer"},
                        },
                        "required": ["pattern"],
                    },
                },
            ]
        )
    return defs


async def _dispatch_kb_tool(
    name: str,
    args: Dict[str, Any],
    tools_by_name: Dict[str, Any],
) -> str:
    """Call a KB tool by name and unwrap its MCP-format result into plain
    text suitable for returning as an Anthropic tool_result content block."""
    sdk_tool = tools_by_name.get(name)
    if sdk_tool is None:
        return f"Unknown tool: {name}"
    try:
        result = await sdk_tool.handler(args or {})
    except Exception as exc:
        log.exception("KB tool %s failed", name)
        return f"Tool {name} failed: {exc}"
    content = result.get("content") or []
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return first.get("text", "")
    return ""


# ---------------------------------------------------------------------------
# Fast-path gate: decide whether a turn needs the full Claude Code agent loop
# (CLI + MCP + tool-use deliberation, ~3–5 s overhead) or can ride the cheap
# Messages-API path (~300 ms – 2 s). Same model on both sides — the split is
# about mode, not model.
# ---------------------------------------------------------------------------

_AGENT_PATTERN = re.compile(
    r"\b("
    r"plot|chart|graph|pdf|"
    r"create\s+(a\s+)?file|save\s+(to\s+|as\s+)?(a\s+)?file|"
    r"generate\s+(a\s+)?(pdf|file|chart|plot|image|report)|"
    r"run\s+(this\s+)?(code|script|command|bash|python)|"
    r"execute\s+(code|script|this|the)|"
    r"download|fetch\s+(from|url|the\s+url)|"
    r"analyze\s+(the\s+|this\s+)?(file|doc|document|csv|spreadsheet|data)|"
    r"read\s+(the\s+|this\s+)?(file|doc|document)|"
    r"write\s+(to\s+)?(a\s+)?file|edit\s+\S+\.\w+|"
    r"make\s+(a\s+|me\s+a\s+)?(plot|chart|pdf|graph|visualization|viz)"
    r")\b",
    re.IGNORECASE,
)

# Mentioning a file extension is a strong "the user has / wants a file" signal.
_FILE_EXT_PATTERN = re.compile(
    r"\.(pdf|csv|tsv|xlsx|xls|docx|pptx|png|jpe?g|svg|html?|json|md|ipynb|zip|tar\.gz)\b",
    re.IGNORECASE,
)

_MODE_PREFIXES = ("/agent", "/fast")


def _strip_mode_prefix(prompt: str) -> str:
    stripped = prompt.lstrip()
    for tag in _MODE_PREFIXES:
        if stripped.startswith(tag):
            return stripped[len(tag) :].lstrip()
    return prompt


def _needs_agent(prompt: str, files: Optional[List[Any]]) -> bool:
    """Route-per-turn heuristic. `/agent` / `/fast` prefixes are explicit
    overrides. Attachments force agent mode (the model should be able to
    read them). Otherwise: look for keywords and file-extension mentions."""
    if not prompt:
        return False
    stripped = prompt.lstrip()
    if stripped.startswith("/agent"):
        return True
    if stripped.startswith("/fast"):
        return False
    if files:
        return True
    if _AGENT_PATTERN.search(stripped):
        return True
    if _FILE_EXT_PATTERN.search(stripped):
        return True
    return False


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


class Pipe:
    class Valves(BaseModel):
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Anthropic API key (pay-per-token). Leave empty to use the subscription OAuth token or inherit from the backend env.",
        )
        CLAUDE_CODE_OAUTH_TOKEN: str = Field(
            default="",
            description=(
                "Long-lived Claude Pro/Max/Team OAuth token generated by "
                "`claude setup-token` on a machine with a browser. When set, "
                "bills against your subscription (not the API). Takes priority "
                "over ANTHROPIC_API_KEY — the key gets unset so it can't "
                "override. Anthropic's terms: use your own subscription only, "
                "don't re-offer subscription auth to end users."
            ),
        )
        MODEL: str = Field(
            default="claude-haiku-4-5",
            description="Claude model ID (e.g. claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7).",
        )
        PERMISSION_MODE: str = Field(
            default="bypassPermissions",
            description='Permission mode: "default", "acceptEdits", "bypassPermissions", "plan", or "dontAsk".',
        )
        ALLOWED_TOOLS: str = Field(
            default="Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch",
            description="Comma-separated tools auto-approved without prompting.",
        )
        WORKDIR_ROOT: str = Field(
            default="/tmp/claude-agent-pipe",
            description="Root directory for per-chat workspaces. One subdir per chat_id.",
        )
        MAX_TURNS: int = Field(
            default=30,
            description="Maximum agent turns per user message. 0 disables the cap.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        return [{"id": "claude-code", "name": "Claude Code"}]

    async def _run_fast(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """Dispatcher. Pick the cheapest available fast path:
        1. API key available → direct Messages API (~300 ms – 2 s).
        2. OAuth token only → "lite agent": ClaudeSDKClient with no tools,
           no MCP, plain system prompt (~2–3 s; CLI cold-start is
           unavoidable because Anthropic's Messages API rejects OAuth tokens
           — the subscription only works via the Claude Code backend).
        """
        has_api_key = bool(
            self.valves.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
        )
        if has_api_key:
            async for chunk in self._run_messages_api(
                body, user_dict, metadata, files, event_emitter
            ):
                yield chunk
        else:
            async for chunk in self._run_lite_agent(
                body, user_dict, metadata, files, event_emitter
            ):
                yield chunk

    async def _run_messages_api(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """Direct Anthropic Messages-API streaming with optional agentic KB
        tool use. No CLI cold start, no Claude Code persona. If a Workspace
        Model has a knowledge base attached, Claude gets the same KB tools
        as the full agent (search / list / read / grep) and can reformulate
        queries in a native tool-use loop."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            yield "_Fast path needs the `anthropic` package — add it to the Function requirements._"
            return

        if self.valves.ANTHROPIC_API_KEY:
            client = AsyncAnthropic(api_key=self.valves.ANTHROPIC_API_KEY)
        else:
            client = AsyncAnthropic()

        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)
        system = "\n\n".join(p for p in system_parts if p.strip()) or None

        # Build KB tools if a workspace knowledge base is attached. Both the
        # MCP server's tool handlers and the Anthropic tool defs come from
        # the same underlying closures (via _build_kb_mcp_server's 3rd
        # return), so Claude sees an identical toolbox in either fast or
        # agent mode.
        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        _, _, kb_tools_by_name = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )
        tool_defs = _anthropic_kb_tool_defs(knowledge, bool(kb_row_ids))

        # Conversation: user/assistant only. Strip any `/agent` or `/fast`
        # prefix from user turns so the model doesn't see it as content.
        messages: List[Dict[str, Any]] = []
        for msg in body.get("messages") or []:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
            if content:
                messages.append({"role": role, "content": content})

        if not messages:
            return

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {"description": "⚡ fast mode", "done": False},
                    }
                )
            except Exception:
                pass

        # Agentic tool-use loop. Stream text as it arrives; if Claude stops
        # with stop_reason="tool_use", execute the tool(s), append the
        # tool_result content blocks, and loop. Cap iterations so a runaway
        # loop can't pin the event loop.
        MAX_TOOL_ROUNDS = 10
        for _round in range(MAX_TOOL_ROUNDS + 1):
            kwargs: Dict[str, Any] = {
                "model": self.valves.MODEL,
                "max_tokens": 4096,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tool_defs:
                kwargs["tools"] = tool_defs

            try:
                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        yield text
                    final = await stream.get_final_message()
            except Exception as exc:
                log.exception("Fast path failed")
                yield (
                    f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"
                )
                return

            if final.stop_reason != "tool_use":
                return  # end_turn — we're done

            # Serialise the assistant message (incl. tool_use blocks) back
            # into the conversation, then resolve each tool call.
            assistant_content: List[Dict[str, Any]] = []
            for block in final.content:
                bt = block.type
                if bt == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif bt == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: List[Dict[str, Any]] = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                preview = _tool_preview(block.name, block.input or {})
                if event_emitter:
                    try:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"🔧 {block.name}"
                                    + (f": {preview}" if preview else ""),
                                    "done": False,
                                },
                            }
                        )
                    except Exception:
                        pass
                # Render a compact tool-use note so the user can see what
                # Claude searched for.
                summary = f"🔧 {block.name}" + (f" · {preview}" if preview else "")
                yield (
                    "\n\n<details>\n"
                    f"<summary>{summary}</summary>\n\n"
                    f"{_tool_input_block(block.name, block.input or {})}\n\n"
                    "</details>\n\n"
                )
                text = await _dispatch_kb_tool(
                    block.name, block.input or {}, kb_tools_by_name
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        yield "\n\n_(Fast-path tool loop cap reached — switch to `/agent` for deeper research.)_\n"

    async def _run_lite_agent(
        self,
        body: Dict[str, Any],
        user_dict: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
        files: Optional[List[Dict[str, Any]]],
        event_emitter: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        """OAuth-compatible fast path: ClaudeSDKClient with KB tools only
        (no Bash / Read / Write / Web / files). Pays the CLI cold-start
        (~1–2 s) but skips the big agent persona. When a workspace knowledge
        base is attached, Claude can agentically search / list / read / grep
        it — query reformulation works here too."""
        prompt = _strip_mode_prefix(_extract_latest_user_prompt(body))
        if not prompt:
            return

        # Workspace-Model system + prior conversation (ClaudeSDKClient.query
        # takes a single string, so we pack history into the system).
        system_parts: List[str] = []
        ws_system = _extract_system_prompt(body)
        if ws_system:
            system_parts.append(ws_system)

        history_lines: List[str] = []
        messages = body.get("messages") or []
        user_turns_remaining = sum(1 for m in messages if m.get("role") == "user")
        consumed = 0
        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = _strip_mode_prefix(content)
                consumed += 1
                if consumed == user_turns_remaining:
                    continue  # skip the latest — it's the query itself
            if content.strip():
                history_lines.append(f"{role.capitalize()}: {content.strip()}")
        if history_lines:
            system_parts.append(
                "Prior conversation (for context):\n\n" + "\n\n".join(history_lines)
            )

        # KB tools via the existing MCP server. No Bash/Read/Write/etc.
        knowledge = _knowledge_collections(metadata, files)
        kb_row_ids = _knowledge_row_ids(metadata)
        kb_server, kb_tool_names, _kb_dict = _build_kb_mcp_server(
            knowledge,
            knowledge_row_ids=kb_row_ids,
            user_dict=user_dict,
            event_emitter=event_emitter,
        )

        if kb_tool_names:
            system_parts.append(
                "You have read-only knowledge-base tools ("
                + ", ".join(t.rsplit("__", 1)[-1] for t in kb_tool_names)
                + "). Use them when the user asks about facts that might be "
                "in the knowledge base. Reformulate and search multiple times "
                "if the first query misses."
            )
        else:
            system_parts.append("Respond concisely and directly.")
        system_text = "\n\n".join(p for p in system_parts if p.strip())

        options_kwargs: Dict[str, Any] = {
            "model": self.valves.MODEL,
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": kb_tool_names,
            "setting_sources": [],
            "system_prompt": system_text,
            "include_partial_messages": True,
        }
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}
        options = ClaudeAgentOptions(**options_kwargs)

        if event_emitter:
            try:
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "description": "⚡ fast mode (OAuth)",
                            "done": False,
                        },
                    }
                )
            except Exception:
                pass

        thinking_buffers: Dict[int, str] = {}
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                yield delta.get("text", "")
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                text = thinking_buffers.pop(idx).strip()
                                if text:
                                    yield (
                                        "\n\n<details>\n"
                                        "<summary>💭 Thinking</summary>\n\n"
                                        f"{text}\n\n"
                                        "</details>\n\n"
                                    )
                    elif isinstance(message, AssistantMessage):
                        # Tool-use rendering (KB tools only here).
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                summary = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                yield (
                                    "\n\n<details>\n"
                                    f"<summary>{summary}</summary>\n\n"
                                    f"{_tool_input_block(block.name, block.input)}\n\n"
                                    "</details>\n\n"
                                )
                    elif isinstance(message, UserMessage):
                        # Surface tool errors (quietly) so the user isn't
                        # confused by Claude retrying silently.
                        content = message.content
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, ToolResultBlock)
                                    and block.is_error
                                ):
                                    err_text = _format_tool_result(block.content)[:400]
                                    yield (
                                        "\n\n<details>\n<summary>"
                                        "<sub>⚙️ tool hiccup</sub></summary>\n\n"
                                        f"```\n{err_text}\n```\n\n"
                                        "</details>\n\n"
                                    )
                    elif isinstance(message, ResultMessage):
                        return
        except Exception as exc:
            log.exception("Lite-agent fast path failed")
            yield f"\n\n**Fast-path error:** `{type(exc).__name__}: {exc}`\n"

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        # Auth selection:
        #   1. If CLAUDE_CODE_OAUTH_TOKEN valve is set → use subscription.
        #      Remove any ANTHROPIC_API_KEY from env because per Claude Code's
        #      precedence order, the API key outranks the OAuth token (docs:
        #      code.claude.com/docs/en/authentication#authentication-precedence)
        #      and would otherwise silently win.
        #   2. Else if ANTHROPIC_API_KEY valve is set → use API.
        #   3. Else → whatever the backend environment already provides.
        if self.valves.CLAUDE_CODE_OAUTH_TOKEN:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.valves.CLAUDE_CODE_OAUTH_TOKEN
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif self.valves.ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = self.valves.ANTHROPIC_API_KEY
        # claude CLI refuses --dangerously-skip-permissions under root unless
        # told it's inside a sandbox. OpenWebUI's backend runs as UID 0.
        os.environ.setdefault("IS_SANDBOX", "1")

        prompt = _extract_latest_user_prompt(body)
        if not prompt:
            yield "_No user message to send to Claude Code._"
            return

        # Fast path disabled — always run the full agent loop.
        prompt = _strip_mode_prefix(prompt)

        chat_id = __chat_id__ or "default"
        workdir = Path(self.valves.WORKDIR_ROOT) / chat_id
        workdir.mkdir(parents=True, exist_ok=True)

        allowed_tools = [
            t.strip() for t in self.valves.ALLOWED_TOOLS.split(",") if t.strip()
        ]
        resume_id = _chat_sessions.get(chat_id)

        # Knowledge base attached via Workspace Model → expose as an MCP tool
        # Claude can call agentically. OpenWebUI's middleware already added one
        # entry per attached KB to files/__files__.
        kb_server, kb_tool_names, _ = _build_kb_mcp_server(
            _knowledge_collections(__metadata__, __files__),
            knowledge_row_ids=_knowledge_row_ids(__metadata__),
            user_dict=__user__,
            event_emitter=__event_emitter__,
        )
        allowed_tools = allowed_tools + kb_tool_names

        options_kwargs: Dict[str, Any] = {
            "cwd": str(workdir),
            "model": self.valves.MODEL,
            "permission_mode": self.valves.PERMISSION_MODE,
            "allowed_tools": allowed_tools,
            # Don't load the host's ~/.claude/ or project .claude/ — OpenWebUI chats
            # should run with an empty baseline, not inherit the backend user's config.
            "setting_sources": [],
            # Stream token-level deltas so long answers type out instead of
            # appearing as one chunk when the block finishes.
            "include_partial_messages": True,
        }
        if resume_id:
            options_kwargs["resume"] = resume_id
        if self.valves.MAX_TURNS:
            options_kwargs["max_turns"] = self.valves.MAX_TURNS
        if kb_server is not None:
            options_kwargs["mcp_servers"] = {"helm-kb": kb_server}

        # Extend Claude Code's default agent-loop system prompt with whatever
        # the Workspace Model configured. `append` keeps the agentic prompt
        # intact while adding domain persona/rules on top.
        system_prompt = _extract_system_prompt(body)
        if system_prompt:
            options_kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            }

        options = ClaudeAgentOptions(**options_kwargs)

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        await emit_status("Starting Claude Code…")
        # Claude often saves generated files to /tmp from habit (absolute paths
        # in matplotlib/PIL examples), even though cwd is the chat workdir.
        # Scan both so we don't miss the image.
        scan_dirs = [workdir, Path("/tmp")]
        artifact_snapshot = _snapshot_artifacts(scan_dirs)

        # Buffer thinking deltas and emit the <details>…</details> wrapper as
        # one atomic chunk at content_block_stop. Streaming the opener+content
        # token-by-token is unreliable: CommonMark's HTML block terminates at
        # blank lines, so thinking text with paragraph breaks strands the
        # opening <details><summary> as literal text in some renderers. Reset
        # at each message_start (indices restart per assistant message).
        thinking_buffers: Dict[int, str] = {}

        # Heartbeat: when a tool starts, emit a status update every 5s showing
        # elapsed time so the user sees that long-running commands (e.g. a 30s
        # Bash) aren't stuck. Keyed by tool_use_id; completed tools removed on
        # the matching ToolResultBlock.
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
                    log.debug("heartbeat tick: %s · %ss", label, elapsed)
                    await emit_status(f"⏳ {label} · running {elapsed}s…")
            except asyncio.CancelledError:
                pass

        def _ensure_heartbeat() -> None:
            nonlocal heartbeat_task
            if heartbeat_task is None or heartbeat_task.done():
                heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            session_id = message.data.get("session_id")
                            if session_id:
                                _chat_sessions[chat_id] = session_id
                        continue

                    if isinstance(message, StreamEvent):
                        ev = message.event or {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            thinking_buffers.clear()
                        elif etype == "content_block_start":
                            block = ev.get("content_block") or {}
                            if block.get("type") == "thinking":
                                thinking_buffers[ev.get("index", 0)] = ""
                                await emit_status("💭 Thinking…")
                        elif etype == "content_block_delta":
                            delta = ev.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta":
                                yield delta.get("text", "")
                            elif dt == "thinking_delta":
                                idx = ev.get("index", 0)
                                if idx in thinking_buffers:
                                    thinking_buffers[idx] += delta.get("thinking", "")
                            # signature_delta / input_json_delta: ignore. Tool input
                            # is rendered once fully from AssistantMessage below.
                        elif etype == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in thinking_buffers:
                                text = thinking_buffers.pop(idx).strip()
                                if text:
                                    yield (
                                        "\n\n<details>\n<summary>💭 Thinking</summary>\n\n"
                                        f"{text}\n\n"
                                        "</details>\n\n"
                                    )
                        continue

                    if isinstance(message, AssistantMessage):
                        # Text + thinking already streamed via StreamEvent. Only
                        # emit tool-use previews here (we need the completed
                        # input dict, which StreamEvent only has as partial JSON).
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                preview = _tool_preview(block.name, block.input)
                                label = (
                                    f"{block.name}: {preview}"
                                    if preview
                                    else block.name
                                )
                                await emit_status(f"🔧 {label}")
                                active_tools[block.id] = {
                                    "label": label,
                                    "started": time.monotonic(),
                                }
                                _ensure_heartbeat()
                                # Render as a collapsed <details>: summary is
                                # plain text (OpenWebUI's sanitizer strips
                                # inline HTML like <strong>/<code> inside
                                # <summary> and renders the tags as literal
                                # text); expanding reveals the full tool
                                # input as a language-tagged fenced code block.
                                # Don't html.escape here — OpenWebUI escapes
                                # <summary> content itself, so pre-escaping
                                # would double-encode ("&lt;" → "&amp;lt;").
                                summary_text = f"🔧 {block.name}" + (
                                    f" · {preview}" if preview else ""
                                )
                                body = _tool_input_block(block.name, block.input)
                                yield (
                                    "\n\n<details>\n"
                                    f"<summary>{summary_text}</summary>\n\n"
                                    f"{body}\n\n"
                                    "</details>\n\n"
                                )
                        continue

                    if isinstance(message, UserMessage):
                        content = message.content
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                active_tools.pop(block.tool_use_id, None)
                                if block.is_error:
                                    # Tool errors are usually transient — Claude
                                    # retries and recovers. Render as a quiet,
                                    # collapsed detail so the red icon / big
                                    # traceback doesn't alarm users.
                                    err_text = _format_tool_result(block.content)[:800]
                                    yield (
                                        "\n\n<details>\n<summary>"
                                        "<sub>⚙️ tool hiccup (retrying)</sub>"
                                        "</summary>\n\n"
                                        f"```\n{err_text}\n```\n\n"
                                        "</details>\n\n"
                                    )
                        continue

                    if isinstance(message, ResultMessage):
                        await emit_status("Done.", done=True)
                        for chunk in _inline_new_artifacts(
                            scan_dirs,
                            artifact_snapshot,
                            (__user__ or {}).get("id"),
                        ):
                            yield chunk
                        if message.subtype != "success":
                            yield f"\n\n_Agent stopped: {message.subtype}_\n"
                        if message.total_cost_usd is not None:
                            yield f"\n\n_Cost: ${message.total_cost_usd:.4f} · {message.duration_ms}ms_\n"
                        return

        except Exception as exc:
            log.exception("Claude Agent SDK pipe failed")
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

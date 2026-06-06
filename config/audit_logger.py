"""
config/audit_logger.py
----------------------
Structured JSON audit logger for MCP tool calls.

Every tool invocation is recorded as a single JSON object on its own line
in ``logs/audit.log``. The file is separate from ``logs/app.log`` (debug)
so audit trails are never mixed with noisy debug output.

Each audit record contains:
    timestamp   — ISO-8601 UTC time the tool call completed
    tool        — MCP tool name
    status      — "ok" or "error"
    elapsed_ms  — wall-clock time in milliseconds
    arguments   — sanitised dict of tool call arguments
    result      — short summary extracted from the tool's JSON response
    error       — error message when status == "error" (omitted otherwise)

Usage (inside server.py)::

    from config.audit_logger import audit_tool_call

    async def my_tool(collection_name: str, ...) -> str:
        t0 = time.perf_counter()
        result_json = ...  # actual work
        await audit_tool_call(
            tool="my_tool",
            arguments={"collection_name": collection_name, ...},
            result_json=result_json,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
        return result_json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ── Audit-log directory (same /logs folder as app.log) ───────────────────────
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

# ── Dedicated logger — no propagation so nothing leaks into app.log ──────────
_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False  # do NOT forward to the root logger / app.log

_audit_handler = RotatingFileHandler(
    _LOG_DIR / "audit.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=5,               # keep audit.log + audit.log.1 … .5
    encoding="utf-8",
)
# Plain format — the message IS the JSON line; no extra prefix needed
_audit_handler.setFormatter(logging.Formatter("%(message)s"))
_audit_logger.addHandler(_audit_handler)

# ── Destructive tool names — flagged in the audit record ─────────────────────
_DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "drop_database",
        "drop_collection",
        "delete_one",
        "delete_many",
        "drop_index",
        "bulk_write",
    }
)

# ── Sensitive argument keys — values replaced with "***" in the audit log ────
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "token", "secret", "key", "api_key", "auth"}
)


def _sanitise_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *arguments* with sensitive values masked.

    Args:
        arguments: Raw keyword arguments dict from a tool call.

    Returns:
        New dict with values for sensitive keys replaced by ``"***"``.
    """
    return {
        k: ("***" if k.lower() in _SENSITIVE_KEYS else v)
        for k, v in arguments.items()
    }


def _summarise_result(result_json: str, status: str) -> Any:
    """Extract a compact summary from a tool's JSON result string.

    For successful calls, returns key metrics (counts, ids, names) without
    embedding the full document payload. For errors, returns the error message.

    Args:
        result_json: Raw JSON string returned by the tool function.
        status:      ``"ok"`` or ``"error"``.

    Returns:
        A JSON-serialisable summary (dict, list slice, or plain value).
    """
    try:
        parsed = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        # Not valid JSON — store a truncated string
        text = str(result_json)
        return text[:200] + "…" if len(text) > 200 else text

    if isinstance(parsed, dict):
        if status == "error":
            return {"error": parsed.get("error", result_json[:200])}

        # Pull out the most useful scalar / small fields; drop large arrays
        summary: dict[str, Any] = {}
        for key, val in parsed.items():
            if isinstance(val, list):
                summary[key] = f"[{len(val)} items]"
            elif isinstance(val, dict) and len(str(val)) > 200:
                summary[key] = "{…}"
            else:
                summary[key] = val
        return summary

    if isinstance(parsed, list):
        return f"[{len(parsed)} documents]"

    return parsed


async def audit_tool_call(
    *,
    tool: str,
    arguments: dict[str, Any],
    result_json: str,
    elapsed_ms: float,
) -> None:
    """Write one structured JSON audit record for a completed tool call.

    This coroutine is safe to ``await`` from any async context. The actual
    I/O is synchronous (the ``logging`` module handles it) but wrapping it
    as a coroutine keeps call-sites consistent with the async tool pattern.

    Args:
        tool:        MCP tool name (e.g. ``"insert_one"``).
        arguments:   Dict of arguments passed to the tool.
        result_json: Raw JSON string the tool returned.
        elapsed_ms:  Wall-clock duration of the tool call in milliseconds.
    """
    # Determine status from the result payload
    try:
        parsed = json.loads(result_json)
        status = "error" if (isinstance(parsed, dict) and "error" in parsed) else "ok"
    except (json.JSONDecodeError, TypeError):
        status = "ok"

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "destructive": tool in _DESTRUCTIVE_TOOLS,
        "arguments": _sanitise_arguments(arguments),
        "result": _summarise_result(result_json, status),
    }

    if status == "error":
        try:
            err_obj = json.loads(result_json)
            record["error"] = err_obj.get("error", result_json[:300])
        except (json.JSONDecodeError, TypeError):
            record["error"] = str(result_json)[:300]

    _audit_logger.info(json.dumps(record, ensure_ascii=False))

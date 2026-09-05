"""
edgedash/query/ask.py

Two-call natural language query pipeline (steering rules 40–46).
Call 1: ROUTE — Select a deterministic tool from TOOLS registry and parse parameters.
Call 2: PHRASE — Turn raw returned rows into grounded prose without adding outside context.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from edgedash import llm, storage
from edgedash.config import Config, load_config
from edgedash.query.tools import TOOLS, ToolSpec


@dataclass(frozen=True)
class Answer:
    text: str
    rows: list[dict[str, Any]]
    tool_used: str | None
    params: dict[str, Any]
    summary: str
    answerable: bool


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

ROUTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tool", "params", "confidence"],
    "properties": {
        "tool": {
            "type": ["string", "null"],
            "description": "The exact name of the selected tool from the registry, or null if no tool matches.",
        },
        "params": {
            "type": "object",
            "description": "Key-value dictionary of parameters to pass to the tool.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "low"],
            "description": "Confidence level in the tool selection.",
        },
    },
}

PHRASING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {
        "answer": {
            "type": "string",
            "description": "The 2-3 sentence grounded prose answer.",
        },
    },
}


# ---------------------------------------------------------------------------
# Prompt Formatters
# ---------------------------------------------------------------------------

def _format_tools_registry() -> str:
    """Format all registered tools into clean text for the router model."""
    parts: list[str] = []
    for name, spec in TOOLS.items():
        props = spec.parameters.get("properties", {})
        required = spec.parameters.get("required", [])
        param_lines = []
        for p_name, p_spec in props.items():
            req_str = ", required" if p_name in required else f", default {p_spec.get('default', 'none')}"
            p_desc = p_spec.get("description", "")
            param_lines.append(f"    - {p_name} ({p_spec.get('type', 'any')}{req_str}): {p_desc}")
        param_block = "\n".join(param_lines) if param_lines else "    None."
        parts.append(f"- {name}: {spec.description}\n  Parameters:\n{param_block}")
    return "\n\n".join(parts)


def _build_routing_prompt(question: str) -> str:
    registry_text = _format_tools_registry()
    return f"""\
You are a query router for EdgeDash job and career intelligence.
Your task is to select the single best query tool from the registry below to answer the user's question, extracting any parameters.

AVAILABLE TOOLS:
{registry_text}

ROUTING RULES:
1. Select exactly ONE tool name from the registry above if and only if it directly answers the user's question.
2. Extract the appropriate parameter values based on the tool's parameter specification.
3. If no tool in the registry directly answers the question, or if the question asks for information not covered by any tool, you MUST set "tool" to null and "params" to {{}}.
4. Rule 45: NEVER guess, NEVER choose the "closest" tool if it does not fit, and NEVER attempt to answer general knowledge or out-of-scope questions.
5. NEVER compose SQL or assume database tables exist. You can only dispatch to the registered tools.
6. Set "confidence" to "high" if the user's intent clearly maps to a tool; otherwise "low".

USER QUESTION:
{question}
"""


def _build_phrasing_prompt(question: str, summary: str, rows: list[dict[str, Any]]) -> str:
    rows_json = json.dumps(rows, indent=2, default=str)
    return f"""\
You are phrasing the answer to a user's question about job market data based strictly on the verified query results below.

USER QUESTION:
{question}

DATA SUMMARY:
{summary}

QUERY RESULT ROWS (JSON):
{rows_json}

RULES (Steering Rule 43):
1. Write a concise, natural response (2-3 sentences).
2. You MUST use ONLY the exact numbers, names, and facts present in the rows and summary above.
3. NEVER estimate, extrapolate, calculate unstated figures, or add outside assumptions.
4. If the result rows are empty, state clearly that the verified database does not contain an answer for that query.
5. Reference the scope of what was analyzed based on the DATA SUMMARY.
"""


def _build_unanswerable_text() -> str:
    """Plain English fallback listing available tools when no tool matches (Rule 45)."""
    tool_lines = [
        f"• **{name}**: {spec.description}"
        for name, spec in TOOLS.items()
    ]
    return (
        "I cannot answer this question because it does not match any of the available query tools. "
        "Here are the questions and data you can ask about:\n\n"
        + "\n".join(tool_lines)
    )


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def ask(
    question: str,
    *,
    config: Config | None = None,
) -> Answer:
    """
    Two-call natural language query pipeline (rules 40–46).
    1. Route (LLM call 1) -> select tool and params
    2. Execute (deterministic read-only function)
    3. Phrase (LLM call 2) -> format rows into grounded answer
    """
    t_start = time.monotonic()
    cfg = config or load_config()
    storage.init_db(cfg.db_path)

    q = question.strip() if question else ""
    if not q:
        return Answer(
            text="Please enter a question to ask your job market data.",
            rows=[],
            tool_used=None,
            params={},
            summary="Empty question.",
            answerable=False,
        )

    # 1. ROUTE (Rule 42 — LLM call 1)
    route_prompt = _build_routing_prompt(q)
    try:
        route_resp = llm.complete_json(route_prompt, ROUTING_SCHEMA, config=cfg)
    except Exception as exc:
        duration_ms = round((time.monotonic() - t_start) * 1000.0, 1)
        storage.log_query(cfg.db_path, q, None, {}, False, duration_ms)
        return Answer(
            text=f"Routing failed due to a model error: {exc}",
            rows=[],
            tool_used=None,
            params={},
            summary="Routing error.",
            answerable=False,
        )

    tool_name = route_resp.get("tool")
    params = route_resp.get("params") or {}

    # Rule 45: If tool is null or not found, return fixed message without phrasing model call
    if not tool_name or tool_name not in TOOLS:
        duration_ms = round((time.monotonic() - t_start) * 1000.0, 1)
        storage.log_query(cfg.db_path, q, tool_name, params, False, duration_ms)
        return Answer(
            text=_build_unanswerable_text(),
            rows=[],
            tool_used=None,
            params=params,
            summary="No matching query tool found.",
            answerable=False,
        )

    # 2. EXECUTE (Rule 41 — parameterised read-only function with clamping)
    tool_spec: ToolSpec = TOOLS[tool_name]
    try:
        # Pass params safely without eval/getattr outside registry
        result = tool_spec.func(**params, config=cfg)
        rows: list[dict[str, Any]] = result.get("rows", [])
        summary: str = result.get("summary", "")
    except Exception as exc:
        duration_ms = round((time.monotonic() - t_start) * 1000.0, 1)
        storage.log_query(cfg.db_path, q, tool_name, params, False, duration_ms)
        return Answer(
            text=f"Query execution failed: {exc}",
            rows=[],
            tool_used=tool_name,
            params=params,
            summary="Execution error.",
            answerable=False,
        )

    # 3. PHRASE (Rule 42, 43 — LLM call 2)
    if not rows:
        phrased_text = f"The query was executed ({summary}), but returned 0 rows in the verified database."
    else:
        phrase_prompt = _build_phrasing_prompt(q, summary, rows)
        try:
            phrase_resp = llm.complete_json(phrase_prompt, PHRASING_SCHEMA, config=cfg)
            phrased_text = phrase_resp.get("answer", summary)
        except Exception:
            # Fallback to deterministic summary if phrasing call fails
            phrased_text = summary

    duration_ms = round((time.monotonic() - t_start) * 1000.0, 1)
    storage.log_query(cfg.db_path, q, tool_name, params, True, duration_ms)

    return Answer(
        text=phrased_text,
        rows=rows,
        tool_used=tool_name,
        params=params,
        summary=summary,
        answerable=True,
    )

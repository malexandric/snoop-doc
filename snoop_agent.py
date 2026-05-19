"""
Snoop Agent — the core Claude-powered question-answering engine,
extracted from app.py with all Streamlit dependencies removed.

This module is used by:
  - slack_bot.py  (Slack @snoop mentions)
  - Any future integration (Discord, API server, CLI, etc.)

The Streamlit app (app.py) runs its own equivalent loop with richer
real-time UI — this module handles the headless path.

Usage
-----
    from snoop_agent import run_question, load_settings

    settings = load_settings()
    result = run_question(
        question="What was our MRR in Q4 2024?",
        api_key=settings["api_key"],
    )
    print(result.text)
    for df in result.tables:
        print(df)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import anthropic
import pandas as pd
import plotly.graph_objects as go

import context_pointers
import memory
import tools


# ---------------------------------------------------------------------------
# Paths + constants (mirror the ones in app.py)
# ---------------------------------------------------------------------------
SETTINGS_PATH = Path.home() / ".snoop-doc" / "settings.json"
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
CONTEXT_DIR = Path(__file__).parent / "data" / "context"
CONTEXT_TABLES_DIR = CONTEXT_DIR / "tables"

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.0
MAX_ITERATIONS = 25  # hard cap to prevent runaway loops in headless mode

ANTHROPIC_WEB_FETCH_TOOL: dict = {"type": "web_fetch_20250910", "name": "web_fetch"}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class AgentResult:
    """Everything the agent produced for one question."""
    text: str = ""
    tables: list[pd.DataFrame] = field(default_factory=list)
    figures: list[go.Figure] = field(default_factory=list)
    confidence: str = ""    # extracted from <snoop-confidence> block if present
    iterations: int = 0
    duration: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    """Read persisted settings from ~/.snoop-doc/settings.json.

    Returns a dict with at minimum 'api_key' and 'model'. Falls back to
    environment variables when a key isn't in the file.
    """
    raw: dict = {}
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "api_key": (
            raw.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        ),
        "model": raw.get("model", DEFAULT_MODEL),
        "slack_bot_token": (
            raw.get("slack_bot_token") or os.environ.get("SLACK_BOT_TOKEN", "")
        ),
        "slack_app_token": (
            raw.get("slack_app_token") or os.environ.get("SLACK_APP_TOKEN", "")
        ),
    }


# ---------------------------------------------------------------------------
# System prompt assembly (mirrors app.py, no Streamlit)
# ---------------------------------------------------------------------------
def _load_context_docs(table_scope: list[str] | None) -> str:
    """Build the context-docs section of the system prompt.

    Identical logic to app.py's _load_context_docs — kept in sync manually.
    General docs always loaded; table-related docs filtered by scope.
    """
    parts: list[str] = []

    if CONTEXT_DIR.exists():
        general_files = sorted(
            p for p in CONTEXT_DIR.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".md"
            and p.name.lower() != "readme.md"
        )
        for path in general_files:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            body = context_pointers.strip_frontmatter(raw)
            parts.append(f"### From `{path.name}`\n\n{body}")

    if CONTEXT_TABLES_DIR.exists():
        discovered = tools.discover_tables()
        for var_name, csv_path in discovered.items():
            if table_scope is not None and var_name not in table_scope:
                continue
            doc_path = CONTEXT_TABLES_DIR / f"{csv_path.stem}.md"
            if not doc_path.exists():
                continue
            try:
                parts.append(
                    f"### From `tables/{doc_path.name}` (for `{csv_path.name}`)\n\n"
                    f"{doc_path.read_text(encoding='utf-8')}"
                )
            except OSError:
                continue

    if not parts:
        return ""
    return "## Company & data context\n\n" + "\n\n".join(parts)


def build_system_prompt(table_scope: list[str] | None = None) -> str:
    """Assemble the full system prompt from disk files + context docs."""
    if SYSTEM_PROMPT_PATH.exists():
        base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    else:
        base = "You are Snoop Doc, a helpful financial analyst assistant."

    parts = [base]
    context = _load_context_docs(table_scope)
    if context:
        parts.append(f"---\n\n{context}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool assembly
# ---------------------------------------------------------------------------
def _build_tools(scope: list[str] | None) -> list[dict]:
    """Assemble the tools list for one API call (run_python + save_memory + web_fetch)."""
    tool_list = list(tools.build_tools(selected_tables=scope))
    tool_list.append(memory.MEMORY_TOOL)
    tool_list.append(ANTHROPIC_WEB_FETCH_TOOL)
    return tool_list


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------
_CONFIDENCE_RE = re.compile(
    r"<snoop-confidence>(.*?)</snoop-confidence>", re.DOTALL | re.IGNORECASE
)


def extract_confidence(text: str) -> tuple[str, str]:
    """Return (clean_text, confidence_text).

    Strips <snoop-confidence>...</snoop-confidence> from text and returns
    its contents separately so callers can format it differently.
    """
    match = _CONFIDENCE_RE.search(text)
    if not match:
        return text.strip(), ""
    confidence = match.group(1).strip()
    clean = _CONFIDENCE_RE.sub("", text).strip()
    return clean, confidence


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------
def run_question(
    question: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    table_scope: list[str] | None = None,
    conversation_history: list[dict] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    on_progress: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run the Snoop agent on `question` and return structured results.

    Parameters
    ----------
    question            The user's question text.
    api_key             Anthropic API key.
    model               Model ID (default: claude-sonnet-4-5).
    table_scope         Restrict to these CSV variable names (None = all).
    conversation_history  Prior turns as [{"role": .., "content": ..}, ...].
                          Pass for multi-turn Slack threads.
    max_tokens          Max output tokens per API call.
    temperature         Sampling temperature.
    on_progress         Optional callback(msg: str) for log messages.
                        Useful for streaming progress to Slack status updates.

    Returns
    -------
    AgentResult with text, tables, figures, confidence, timing, and any error.
    """
    def _log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    result = AgentResult()
    start = time.time()

    if not api_key:
        result.error = "No Anthropic API key configured. Set it in Snoop's Settings or via ANTHROPIC_API_KEY."
        return result

    client = anthropic.Anthropic(api_key=api_key)

    # Build message history: prior turns + this question
    messages: list[dict] = list(conversation_history or [])
    messages.append({"role": "user", "content": question})

    system_blocks = [
        {
            "type": "text",
            "text": build_system_prompt(table_scope),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    sandbox = tools.build_initial_namespace(selected_tables=table_scope)
    agent_tools = _build_tools(table_scope)

    # Accumulators — collect output across all iterations
    all_text_blocks: list[str] = []
    tables: list[pd.DataFrame] = []
    figures: list[go.Figure] = []

    iteration = 0
    while iteration < MAX_ITERATIONS:
        iteration += 1
        _log(f"Iteration {iteration} — calling Claude…")

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_blocks,
                tools=agent_tools,
                messages=_strip_messages(messages),
            )
        except anthropic.AuthenticationError:
            result.error = "Authentication failed — check the Anthropic API key in Snoop's Settings."
            result.duration = time.time() - start
            return result
        except anthropic.APIError as e:
            result.error = f"Anthropic API error: {e}"
            result.duration = time.time() - start
            return result

        # Convert response blocks to plain dicts (same pattern as app.py)
        assistant_content: list[dict] = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            else:
                try:
                    assistant_content.append(block.model_dump())
                except AttributeError:
                    assistant_content.append(
                        {k: getattr(block, k) for k in dir(block)
                         if not k.startswith("_") and not callable(getattr(block, k))}
                    )

        messages.append({"role": "assistant", "content": assistant_content})

        # Collect text blocks from this iteration
        for block in assistant_content:
            if block.get("type") == "text":
                t = block.get("text", "").strip()
                if t:
                    all_text_blocks.append(t)

        # Execute tool calls
        tool_results: list[dict] = []
        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue

            tool_name = block.get("name", "")
            block_id = block["id"]

            if tool_name == "save_memory":
                inp = block.get("input", {}) or {}
                try:
                    memory.save_memory(
                        content=inp.get("content", ""),
                        category=inp.get("category", "fact"),
                        rationale=inp.get("rationale", ""),
                    )
                    result_text = "Memory entry saved."
                    _log(f"Saved to memory: '{inp.get('content', '')[:60]}'")
                except Exception as e:  # noqa: BLE001
                    result_text = f"Error saving memory: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": result_text,
                })
                continue

            # run_python
            code = block.get("input", {}).get("code", "")
            _log(f"Running Python ({code.count(chr(10)) + 1} lines)…")
            exec_result = tools.execute(code, sandbox)

            if exec_result.fig is not None:
                figures.append(exec_result.fig)
            if exec_result.table is not None:
                tables.append(exec_result.table)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block_id,
                "content": exec_result.to_tool_result_text(),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            break

    result.duration = time.time() - start
    result.iterations = iteration
    result.tables = tables
    result.figures = figures

    # Combine text blocks and extract confidence
    full_text = "\n\n".join(all_text_blocks)
    result.text, result.confidence = extract_confidence(full_text)

    return result


def _strip_messages(messages: list[dict]) -> list[dict]:
    """Keep only 'role' and 'content' keys — the API rejects extras."""
    return [
        {k: v for k, v in m.items() if k in ("role", "content")}
        for m in messages
    ]

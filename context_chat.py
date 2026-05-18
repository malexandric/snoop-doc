"""
AI-assisted context-doc editor.

A modal chat dialog that helps the user write context documents. Two modes:

  - **table**: editing a CSV's doc-related context (lives in
    `data/context/tables/<csv_stem>.md`). The assistant sees the CSV's
    data fingerprint, can run pandas against it to verify claims, and is
    pushed to cover loading recipe + business semantics + aggregation
    traps. Opened from the Data view's per-file Context button.

  - **general**: editing a top-level general context doc (company info,
    glossary, accounting conventions; lives directly in `data/context/`).
    The assistant has no run_python tool — these docs are reference text,
    not data analysis. Opened from the top-level Context view.

In both modes the assistant has read-only access to all other general
docs so it can match terminology and flag conflicts. Chat history is *not*
persisted across opens; each session starts fresh. Manual edits made in
the doc preview are sent along with each AI turn so they're never silently
overwritten.

The model used is `st.session_state.utility_model` — defaults to Haiku,
which is plenty for this focused workflow.
"""

from __future__ import annotations

from pathlib import Path

import anthropic
import streamlit as st

import data_fingerprint
import tools


CONTEXT_DIR = Path(__file__).parent / "data" / "context"
CONTEXT_TABLES_DIR = CONTEXT_DIR / "tables"

# Max tool iterations *within one chat turn*. Tighter than the main agent
# because the context-editing flow is narrower — if the model is looping
# inside one turn it's stuck.
MAX_TOOL_ITERATIONS_PER_TURN = 15

# Mode constants — used as the source of truth for which kind of doc the
# dialog is editing. Stored under `_ctx_chat_mode` in session_state.
MODE_TABLE = "table"
MODE_GENERAL = "general"


# ---------------------------------------------------------------------------
# Tool definitions — both modes get update_context_doc; only table mode
# additionally gets run_python (general docs don't analyse data)
# ---------------------------------------------------------------------------
_UPDATE_DOC_TOOL = {
    "name": "update_context_doc",
    "description": (
        "Replace the entire context doc with new content. Pass the "
        "FULL doc as `new_content` — not a diff or patch. Briefly "
        "describe what changed in `rationale`. The user sees the "
        "updated doc immediately in the editor."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "new_content": {
                "type": "string",
                "description": "Complete new doc content (markdown).",
            },
            "rationale": {
                "type": "string",
                "description": "One-sentence explanation of what changed and why.",
            },
        },
        "required": ["new_content", "rationale"],
    },
}

_RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Inspect the CSV data deeper. Same sandbox as the main chat — "
        "pd, np, the table loaded under its variable name and also "
        "aliased as `df`. Use this when the data fingerprint isn't "
        "enough to verify a claim you'd put in the doc."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute. Use print() to surface results.",
            },
        },
        "required": ["code"],
    },
}

TABLE_CHAT_TOOLS = [_RUN_PYTHON_TOOL, _UPDATE_DOC_TOOL]
GENERAL_CHAT_TOOLS = [_UPDATE_DOC_TOOL]


# ---------------------------------------------------------------------------
# State helpers — open / clear the dialog
# ---------------------------------------------------------------------------
def open_for_existing(csv_name: str) -> None:
    """Open the chat to edit an existing CSV's doc-related context."""
    ctx_path = _table_ctx_path(csv_name)
    existing = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else ""
    _init_state(MODE_TABLE, csv_name, existing)


def open_for_new_upload(csv_name: str) -> None:
    """Open after a fresh CSV upload — empty doc, AI is asked to introduce
    itself and start the conversation."""
    _init_state(MODE_TABLE, csv_name, "")
    st.session_state._ctx_chat_pending_message = (
        f"I just uploaded `{csv_name}`. Take a look at the data fingerprint, "
        "introduce yourself briefly, and ask the questions you need to build "
        "a precise context doc. Create a first draft as soon as you've got "
        "enough to start, then refine with me."
    )


def open_for_general_existing(md_name: str) -> None:
    """Open the chat to edit an existing general context doc."""
    ctx_path = _general_ctx_path(md_name)
    existing = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else ""
    _init_state(MODE_GENERAL, md_name, existing)


def open_for_general_new(md_name: str) -> None:
    """Open the chat to create a new general context doc. Kicks the
    conversation off so the user can describe what the doc should cover."""
    _init_state(MODE_GENERAL, md_name, "")
    st.session_state._ctx_chat_pending_message = (
        f"I'm starting a new general context doc called `{md_name}`. "
        "Introduce yourself briefly, look at any other general context docs "
        "already in the project to see what's covered, and ask me what this "
        "new doc should be about. Once I've described it, draft a first "
        "version and we'll refine together."
    )


def _init_state(mode: str, target: str, doc_content: str) -> None:
    st.session_state._ctx_chat_mode = mode
    st.session_state._ctx_chat_target = target
    st.session_state._ctx_chat_doc = doc_content
    st.session_state._ctx_chat_messages = []
    st.session_state._ctx_doc_widget_version = 0


def _clear_state() -> None:
    """Wipe all chat-dialog state. Called on Save or Cancel."""
    for k in (
        "_ctx_chat_mode",
        "_ctx_chat_target",
        "_ctx_chat_doc",
        "_ctx_chat_messages",
        "_ctx_chat_pending_message",
        "_ctx_doc_widget_version",
    ):
        st.session_state.pop(k, None)


def _table_ctx_path(csv_name: str) -> Path:
    return CONTEXT_TABLES_DIR / f"{Path(csv_name).stem}.md"


def _general_ctx_path(md_name: str) -> Path:
    # Accept either "company.md" or "company" — always append .md if missing.
    stem = Path(md_name).stem
    return CONTEXT_DIR / f"{stem}.md"


def _current_doc_path() -> Path | None:
    """Where the in-flight doc will be saved on disk. Returns None if the
    dialog isn't active (no mode set)."""
    mode = st.session_state.get("_ctx_chat_mode")
    target = st.session_state.get("_ctx_chat_target")
    if not mode or not target:
        return None
    if mode == MODE_TABLE:
        return _table_ctx_path(target)
    return _general_ctx_path(target)


# ---------------------------------------------------------------------------
# Public render entry point — call from the page top-level
# ---------------------------------------------------------------------------
def maybe_render() -> None:
    """If the chat is active (state set), open the dialog. Idempotent."""
    if st.session_state.get("_ctx_chat_mode"):
        _show_chat_dialog()


# ---------------------------------------------------------------------------
# Dialog UI
# ---------------------------------------------------------------------------
@st.dialog("AI context editor", width="large")
def _show_chat_dialog() -> None:
    mode = st.session_state.get("_ctx_chat_mode")
    target = st.session_state.get("_ctx_chat_target")
    if not mode or not target:
        return

    ctx_path = _current_doc_path()
    is_new = ctx_path is None or not ctx_path.exists()
    rel_path = (
        ctx_path.relative_to(Path(__file__).parent) if ctx_path else Path(target)
    )
    if mode == MODE_TABLE:
        st.caption(
            f"{'Creating' if is_new else 'Editing'} context for `{target}` "
            f"→ `{rel_path}`"
        )
    else:
        st.caption(
            f"{'Creating' if is_new else 'Editing'} general context doc "
            f"`{target}` → `{rel_path}`"
        )

    # Doc preview / editor. The counter-based widget key lets us refresh
    # the widget's *displayed* value when the AI calls `update_context_doc`
    # — without it, Streamlit would keep showing the user's stale local
    # input. Bumping the counter each AI update gives us a clean swap.
    counter = st.session_state.get("_ctx_doc_widget_version", 0)
    edited = st.text_area(
        "Doc preview (you can also edit manually)",
        value=st.session_state.get("_ctx_chat_doc", ""),
        height=300,
        key=f"_ctx_doc_textarea_v{counter}",
    )
    # Persist manual edits to the canonical state so the next AI call
    # sees them.
    if edited != st.session_state.get("_ctx_chat_doc", ""):
        st.session_state._ctx_chat_doc = edited

    # Run any queued bootstrap message *after* the doc widget renders so
    # its session_state key is established and we don't fight Streamlit's
    # widget lifecycle when the AI replaces the doc on the same run.
    pending = st.session_state.pop("_ctx_chat_pending_message", None)
    if pending:
        with st.spinner("Thinking…"):
            _run_chat_turn(pending)
        st.rerun()

    st.divider()
    st.markdown("**AI assistant**")
    _render_chat_history()

    if prompt := st.chat_input("Ask or answer the assistant…"):
        with st.spinner("Thinking…"):
            _run_chat_turn(prompt)
        st.rerun()

    st.divider()
    save_col, cancel_col = st.columns(2)
    with save_col:
        if st.button(
            "Save context",
            type="primary",
            icon=":material/save:",
            key="_ctx_chat_save",
            use_container_width=True,
        ):
            if ctx_path is None:
                st.error("Internal error: no save path set.")
                return
            try:
                ctx_path.parent.mkdir(parents=True, exist_ok=True)
                ctx_path.write_text(
                    st.session_state.get("_ctx_chat_doc", ""),
                    encoding="utf-8",
                )
            except OSError as e:
                st.error(f"Could not save: {e}")
                return
            _clear_state()
            st.rerun()

    with cancel_col:
        if st.button(
            "Cancel",
            key="_ctx_chat_cancel",
            use_container_width=True,
        ):
            _clear_state()
            st.rerun()


def _render_chat_history() -> None:
    """Render the chat messages. Tool calls show as small captions /
    collapsed expanders; tool results are skipped (they're plumbing)."""
    history = st.session_state.get("_ctx_chat_messages", [])
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user" and isinstance(content, str):
            with st.chat_message("user"):
                st.markdown(content)
            continue

        if role == "assistant" and isinstance(content, list):
            with st.chat_message("assistant"):
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            st.markdown(text)
                    elif btype == "tool_use":
                        name = block.get("name", "")
                        if name == "update_context_doc":
                            rationale = block.get("input", {}).get("rationale", "")
                            st.caption(f"✎ Updated doc — {rationale}")
                        elif name == "run_python":
                            with st.expander("Ran code", expanded=False, icon=":material/code:"):
                                code = block.get("input", {}).get("code", "")
                                st.code(code, language="python")
        # role="user" with a tool_result list — skip rendering, it's plumbing.


# ---------------------------------------------------------------------------
# General-context loading — for both modes to stay consistent with what's
# already documented in the project
# ---------------------------------------------------------------------------
def _load_general_docs(exclude_filename: str | None = None) -> str:
    """Concatenate every general context doc (`data/context/*.md` root,
    excluding README) into one markdown chunk. Pass `exclude_filename`
    when the doc being edited is itself a general doc, so we don't
    feed it back to itself.

    Doc-related context docs (under `tables/`) are intentionally not
    included — those are CSV-specific and irrelevant when working on a
    different file or on a general doc.
    """
    if not CONTEXT_DIR.exists():
        return ""
    exclude_path = (
        (CONTEXT_DIR / f"{Path(exclude_filename).stem}.md")
        if exclude_filename
        else None
    )
    parts: list[str] = []
    for path in sorted(CONTEXT_DIR.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if path.name.lower() == "readme.md":
            continue
        if exclude_path is not None and path == exclude_path:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### From `{path.name}`\n\n{content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# System prompts — one per mode
# ---------------------------------------------------------------------------
def _table_system_prompt(csv_name: str, fingerprint_text: str, current_doc: str) -> str:
    var_name = tools.sanitize_name(csv_name) or "df"
    # Table-mode chat sees all general docs, never excluded — they're a
    # different category. No exclude_filename here.
    general_context = _load_general_docs()
    return f"""You help users write a precise, concise context document for a CSV data file.

Your goal: make the doc helpful enough for a downstream AI agent (a different conversation) to query this CSV correctly. Brevity is a feature — the doc is loaded into that other agent's system prompt on every turn, so every line should be actionable.

**File being documented:** `{csv_name}`
**Sandbox variable name:** `{var_name}` (also aliased as `df` when this is the only loaded table)

## How you should behave

- **Ask focused questions** to fill gaps in the doc. Don't ask things you can already infer from the data fingerprint.
- **Use `run_python`** to verify anything you're unsure of before claiming it. The fingerprint covers the whole file but lists only sampled rows directly — `run_python` lets you check specific patterns. **State persists across run_python calls within this turn** — variables, helpers, and DataFrame mutations from one call are visible to the next.
- **Always make progress.** After inspecting the data, either ask the user a clarifying question OR call `update_context_doc` with concrete improvements — don't just stop. Even a partial update is useful.
- **Use `update_context_doc`** to write or update the doc as you learn things. Always pass the full content (the tool replaces the doc wholesale). The user sees your updates immediately.
- **Keep the doc short.** Aim for under 600 words. No filler, no TODO bullets — if you don't know something, ask or leave it out.
- **Never invent business meaning.** Ask the user for what each branch / metric / column means. If they don't know, leave it out — silence beats guessing.
- **Match exact strings.** Use column names exactly as they appear in the data, including case and spacing. Prefer anchoring row references to label content (e.g. "the row whose first cell is `'Total Expenses'`") over row indices, which drift if the file is re-saved.
- **Show, don't describe.** For common queries the file can answer, include a one-line code example. `"Q1 revenue: df.loc[df.iloc[:, 0] == 'Revenue', q1_cols].sum().sum()"` is more useful than "aggregate revenue by quarter".
- **Acknowledge when done.** When the doc is solid and you've no more questions, say so — the user can save when they're ready.
- **Keep chat replies short.** Save the prose for the doc itself.

## Three sections every context doc must include

If you don't have the info to write any of these, **ask the user before finalising the doc**. A doc without these is half-done.

1. **Loading recipe.** Show exactly how to read the file: the right `pd.read_csv` invocation (`header`, `encoding`, `sep`, `skiprows`) plus any post-load cleanup the downstream agent should do (parse currency strings, drop section-header rows, strip whitespace from column names). Verify your recipe runs cleanly with `run_python` before claiming it works. Without this section the downstream agent has to re-invent loading on every turn.

2. **Business semantics for grouped columns.** When the file groups columns by some category (departments, branches, regions, products, channels), don't assume each column maps one-to-one to a distinct business unit. Sometimes several columns belong to one business entity (a parent company's sub-channels); sometimes one column is an aggregate of others. **Ask the user explicitly** about the column-to-business-unit relationships before documenting them. Getting this wrong silently breaks every downstream answer.

3. **Aggregation traps.** Find and call out every place naive aggregation would double-count or pick up garbage: TOTAL or SUBTOTAL columns mixed with line items, TOTAL rows mixed with data rows, section-header rows like "EXPENSES" that have no values, grand-total rows, anything pre-aggregated. For each trap, add an explicit "**Don't double-count:** ..." note with the right way to aggregate. Aggregation mistakes are the single largest source of wrong answers in finance data.

## Data fingerprint

{fingerprint_text}

## Current doc content

```
{current_doc}
```
""" + (f"""

## General context docs already in this project

These are the project's company-wide context files — they define departments, business units, metric formulas, fiscal calendar, currency conventions. **Use them.**

- When a general doc already defines a term (e.g. "Masteriyo is our LMS plugin"), use the SAME wording in your doc rather than redefining or paraphrasing. Don't ask the user to re-explain things that are already documented.
- If you notice a contradiction between these docs and what you see in the data, flag it to the user — don't silently disagree.
- Don't duplicate content. If `company.md` already covers fiscal calendar and currency, the per-CSV doc doesn't need its own section on those.

{general_context}
""" if general_context else "")


def _general_system_prompt(md_name: str, current_doc: str) -> str:
    other_general = _load_general_docs(exclude_filename=md_name)
    return f"""You help users write a clear, concise GENERAL context document for the Snoop Doc app.

General context docs describe company-wide facts that the downstream AI assistant needs across every conversation: org structure, brand or product portfolio, fiscal calendar, currency conventions, glossary of internal terms, accounting principles. They are loaded into the main agent's system prompt on every turn, so brevity matters — every line should be actionable.

**File being documented:** `{md_name}`

## How you should behave

- **Ask focused questions** to fill gaps. Don't ask the user to re-explain things already covered in another general doc.
- **Use `update_context_doc`** to draft or refine the doc as you learn things. Always pass the full content (the tool replaces the doc wholesale). The user sees your updates immediately.
- **Keep the doc short.** Aim for under 600 words. Prefer headings + bullets to paragraphs.
- **Never invent facts.** If the user doesn't know something, leave it out — silence beats guessing.
- **Watch for conflicts and duplication with the other general docs.** If something is already documented elsewhere, point that out — don't restate it. If you spot a contradiction between what the user is telling you and what an existing general doc says, flag it before writing anything.
- **Acknowledge when done.** When the doc is solid and you've no more questions, say so — the user can save when they're ready.
- **Keep chat replies short.** Save the prose for the doc itself.

## Current doc content

```
{current_doc}
```
""" + (f"""

## Other general context docs already in this project

These are the project's other general context files. Use them to stay consistent and avoid duplication.

{other_general}
""" if other_general else """

## Other general context docs already in this project

(This is the first general context doc — there are no others yet.)
""")


# ---------------------------------------------------------------------------
# Agent loop — process one chat turn (user message in, replies + edits out)
# ---------------------------------------------------------------------------
def _run_chat_turn(user_text: str) -> None:
    """Process one user message: append, call the model, run any tools,
    repeat until end_turn. Updates `_ctx_chat_messages` + `_ctx_chat_doc`.
    Branches by mode set in session state.
    """
    mode = st.session_state.get("_ctx_chat_mode")
    target = st.session_state.get("_ctx_chat_target")
    if not mode or not target:
        return

    api_key = st.session_state.get("api_key", "")
    if not api_key:
        st.error("Add your Anthropic API key in Settings to use the AI context editor.")
        return

    history = st.session_state.setdefault("_ctx_chat_messages", [])
    history.append({"role": "user", "content": user_text})

    if mode == MODE_TABLE:
        system_prompt, sandbox, chat_tools = _build_table_turn(target)
    else:
        system_prompt = _general_system_prompt(
            target, st.session_state.get("_ctx_chat_doc", "")
        )
        sandbox = None
        chat_tools = GENERAL_CHAT_TOOLS

    client = anthropic.Anthropic(api_key=api_key)
    model = st.session_state.get("utility_model", "claude-haiku-4-5")

    for _ in range(MAX_TOOL_ITERATIONS_PER_TURN):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=chat_tools,
                messages=history,
            )
        except anthropic.AuthenticationError:
            st.error("Authentication failed. Check your API key in Settings.")
            history.pop()  # don't poison retries with the unsent user msg
            return
        except anthropic.APIError as e:
            st.error(f"API error: {e}")
            history.pop()
            return

        # Materialise the assistant turn for history + UI rendering.
        assistant_content: list[dict] = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        # If the model returned literally nothing usable, that's a dead
        # end. Don't silently swallow it — surface it so the user knows
        # the conversation stalled rather than thinking the app broke.
        if not assistant_content:
            history.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "_(The model returned an empty response "
                                f"(stop_reason: `{response.stop_reason}`). "
                                "Try a more specific follow-up question, or "
                                "switch the **Utility model** in Settings "
                                "to something stronger like Sonnet.)_"
                            ),
                        }
                    ],
                }
            )
            return

        history.append({"role": "assistant", "content": assistant_content})

        # Execute every tool call in the response, collect results.
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "update_context_doc":
                new_doc = block.input.get("new_content", "")
                rationale = block.input.get("rationale", "")
                st.session_state._ctx_chat_doc = new_doc
                # Force a fresh text_area widget on next render so the
                # displayed value matches the new doc.
                st.session_state._ctx_doc_widget_version = (
                    st.session_state.get("_ctx_doc_widget_version", 0) + 1
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Doc updated. ({rationale})",
                    }
                )

            elif block.name == "run_python":
                code = block.input.get("code", "")
                if sandbox is None:
                    # General mode: run_python isn't exposed in the tools
                    # list, so the model shouldn't be calling it. If it
                    # does (older context, hallucination), refuse cleanly.
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": (
                                "Error: run_python is not available when "
                                "editing a general context doc — there's "
                                "no data file to analyse. Ask the user "
                                "for the information instead."
                            ),
                        }
                    )
                    continue
                # Sandbox is built ONCE per chat turn (outside this loop)
                # so state persists across tool calls within the turn.
                result = tools.execute(code, sandbox)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.to_tool_result_text(),
                    }
                )

            else:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {block.name}",
                    }
                )

        if tool_results:
            history.append({"role": "user", "content": tool_results})

        if response.stop_reason == "end_turn":
            return
        if not tool_results:
            # Stop_reason isn't end_turn but no tool calls fired —
            # surface it so the user can see the conversation stalled.
            history.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"_(Model stopped with `{response.stop_reason}` "
                                "but didn't call a tool or finish the turn. "
                                "Try following up to nudge it along.)_"
                            ),
                        }
                    ],
                }
            )
            return
    else:
        # for-else: the for loop completed without `return` — i.e. we hit
        # MAX_TOOL_ITERATIONS_PER_TURN without an end_turn. Tell the user.
        history.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "_(Hit the per-turn tool-call cap "
                            f"({MAX_TOOL_ITERATIONS_PER_TURN}) without "
                            "finishing. You can ask the model to wrap up.)_"
                        ),
                    }
                ],
            }
        )


def _build_table_turn(csv_name: str) -> tuple[str, dict, list[dict]]:
    """Build the system prompt + sandbox + tools list for a table-mode turn.

    Splits the fingerprint-and-sandbox setup out of `_run_chat_turn` so
    that function stays mode-agnostic at the top.
    """
    var_name = tools.sanitize_name(csv_name) or "df"
    csv_path = tools.TABLES_DIR / csv_name

    df = None
    load_error: str | None = None
    if not csv_path.exists():
        load_error = f"file not found at `{csv_path}`"
    else:
        try:
            loaded = tools._load_tables(selected=[var_name])
            df = loaded.get(var_name)
            if df is None:
                load_error = (
                    "pd.read_csv failed with all the common encodings "
                    "(utf-8, utf-8-sig, latin-1, cp1252). The file may be "
                    "malformed, use a non-standard delimiter, have a "
                    "multi-row header, or include binary content."
                )
        except Exception as e:  # noqa: BLE001
            load_error = f"unexpected error: {e}"

    if df is not None:
        fp = data_fingerprint.fingerprint(df)
    else:
        fp = (
            f"⚠ **Could not auto-load `{csv_name}`** — {load_error}\n\n"
            f"The file is at `{csv_path}` and is available in the sandbox as "
            f"the variable `csv_path` (a string). Use `run_python` to "
            f"diagnose: read the first few hundred bytes raw, identify the "
            f"encoding / delimiter / header layout, then try `pd.read_csv` "
            f"with the right parameters. Once you've got it loading, "
            f"document that loading approach explicitly in the context doc "
            f"so the main agent knows how to read it later. Example "
            f"starting point:\n\n"
            f"```python\n"
            f"with open(csv_path, 'rb') as f:\n"
            f"    raw = f.read(500)\n"
            f"print(raw)  # look for BOM, weird bytes, separator characters\n"
            f"```"
        )

    current_doc = st.session_state.get("_ctx_chat_doc", "")
    system_prompt = _table_system_prompt(csv_name, fp, current_doc)

    # Build the run_python sandbox ONCE per chat turn — matches the main
    # agent's pattern. Variables, helper functions, and mutations the
    # model defines in one tool call persist for the next call within the
    # same turn. The `csv_path` global lets it recover from auto-load
    # failures with non-standard encodings/delimiters.
    sandbox = tools.build_initial_namespace(
        selected_tables=[var_name],
        extra_globals={"csv_path": str(csv_path)},
        skip_demo_fallback=True,
    )

    return system_prompt, sandbox, TABLE_CHAT_TOOLS

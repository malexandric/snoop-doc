"""
Agent-managed persistent memory.

A small system that lets the agent save facts learned during a chat to
`data/context/memory.md`, where they're then auto-loaded into every
future conversation's system prompt (memory.md lives in the general
context-doc directory, so it gets picked up by `_load_context_docs()`
just like company.md / data_sources.md / etc.).

Design principles
-----------------
- **Visible** — every save shows up as a small caption in the chat
  ("✎ Saved to memory: ..."), same pattern as the context-doc editor.
  Users see what got saved and can edit / delete via the Context view.
- **Explicit triggers** — the agent saves only when the user expresses
  durable intent ("remember", "next time", "always", correction that
  generalises, defining a new term). The system prompt lays this out;
  this module just provides the mechanism.
- **Conservative** — saving wrong is worse than not saving at all, so
  the system prompt errs on the side of "skip it" when unclear.
- **Always-loaded** — memory.md is a general context doc, picked up
  automatically by the existing context-loader. No special integration.

File format
-----------
A chronological log grouped by date. Each entry is a bullet with a
category tag, the content, and a one-line *Why:* explaining when the
entry should apply. The *Why* makes future-you (and future-Snoop) able
to judge whether an entry is still load-bearing.

```
# Snoop's memory

Persistent facts the agent has learned from prior conversations. Edit
freely or delete entries that are wrong / stale.

---

## 2026-05-16

- **[fact]** Fiscal year runs July–June.
  *Why:* Company convention; affects every period-based query.

- **[preference]** Always show MRR in USD even when subs are in EUR.
  *Why:* User's reporting context is USD.
```
"""

from __future__ import annotations

from datetime import date
from pathlib import Path


CONTEXT_DIR = Path(__file__).parent / "data" / "context"
MEMORY_PATH = CONTEXT_DIR / "memory.md"

# Tool definition handed to Claude (Anthropic native format). The
# description is the agent's instruction set — it's where the WHEN-to-call
# logic lives, so be specific. Keep it tight; long descriptions cost
# prompt tokens every turn.
MEMORY_TOOL = {
    "name": "save_memory",
    "description": (
        "Save a durable fact, preference, or correction to the project's "
        "persistent memory file (`data/context/memory.md`). The memory is "
        "auto-loaded into every future conversation, so use it for things "
        "that should outlive the current chat — and ONLY those things.\n\n"
        "**Use this tool when:**\n"
        "- The user says variants of 'remember', 'from now on', 'always', "
        "'next time', 'don't ever...'\n"
        "- The user corrects you on something that would apply again "
        "(e.g. 'PR retainer isn't part of marketing spend' — a rule, "
        "not a one-off observation)\n"
        "- The user defines a term you didn't know (e.g. 'Hub is what "
        "we call our LMS plugin internally')\n\n"
        "**Do NOT use this tool for:**\n"
        "- Today's numbers or any value derivable from the data (the "
        "data is authoritative, save its meaning not its values)\n"
        "- Question-specific context ('the user asked about Q3') — "
        "useless next time\n"
        "- Vague impressions like 'the user likes detail' — untargeted\n"
        "- Anything already covered in the existing memory entries "
        "(check the memory section of your context first; update an "
        "existing entry conceptually rather than adding a duplicate)\n\n"
        "Categorise each save: `fact` (something true about the "
        "company/data), `preference` (how the user wants you to behave), "
        "or `correction` (a rule the user gave that overrides defaults).\n\n"
        "Saves are visible to the user — they'll see what you saved and "
        "can edit it. Better to save too little than too much; the user "
        "can always tell you to remember something they think you missed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The fact / preference / correction itself, in one "
                    "sentence. Phrase it so it'd make sense to a future "
                    "version of you who's never seen this conversation."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["fact", "preference", "correction"],
                "description": "Which bucket this entry belongs to.",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "One-line *Why:* — when does this entry apply, and "
                    "why does it matter? Helps future-you judge whether "
                    "the entry is still load-bearing."
                ),
            },
        },
        "required": ["content", "category", "rationale"],
    },
}


_HEADER_TEMPLATE = """# Snoop's memory

Persistent facts the agent has learned from prior conversations. Edit
freely or delete entries that are wrong / stale.

The agent reads this file on every turn (it's loaded as a general
context doc) and may append new entries via its `save_memory` tool.
Manual edits via the Context view are also fine — same format.

---
"""


def ensure_memory_file() -> Path:
    """Create `memory.md` with a header comment if it doesn't exist yet.
    Idempotent — safe to call on every tool invocation."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text(_HEADER_TEMPLATE, encoding="utf-8")
    return MEMORY_PATH


def save_memory(content: str, category: str, rationale: str) -> str:
    """Append a memory entry to `memory.md` under today's date heading.

    Returns the entry as it was written (for display in the chat caption).
    Same-day saves are grouped under one date heading; a save on a new
    day adds a fresh heading.
    """
    content = content.strip()
    rationale = rationale.strip()
    category = category.strip().lower()
    if category not in ("fact", "preference", "correction"):
        category = "fact"

    ensure_memory_file()
    today = date.today().isoformat()
    today_heading = f"## {today}"

    existing = MEMORY_PATH.read_text(encoding="utf-8")

    entry = (
        f"- **[{category}]** {content}\n"
        f"  *Why:* {rationale}\n"
    )

    if today_heading in existing:
        # Already have entries for today — append to that section. We
        # find the last entry under today's heading by looking for the
        # next `## ` heading or end of file.
        idx = existing.index(today_heading) + len(today_heading)
        # Find where today's section ends (next "## " or EOF).
        rest = existing[idx:]
        next_heading_pos = rest.find("\n## ")
        if next_heading_pos == -1:
            # Today is the last section — append to end of file.
            new_content = existing.rstrip() + "\n\n" + entry
        else:
            # Insert before the next section heading.
            insert_at = idx + next_heading_pos
            new_content = existing[:insert_at].rstrip() + "\n\n" + entry + "\n" + existing[insert_at:].lstrip("\n")
    else:
        # First entry today — add a new date heading.
        new_content = existing.rstrip() + f"\n\n{today_heading}\n\n{entry}"

    MEMORY_PATH.write_text(new_content, encoding="utf-8")
    return entry

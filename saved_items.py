"""
Saved Items: storage + UI for charts, tables, reports, and conversations
the user keeps from their chat sessions.

Storage layout (project-relative — everyone using this checkout shares the
library; access control is a TODO):

    saved/
        charts/{id}.json            ← Plotly figure JSON
        charts/{id}.meta.json       ← {id, name, type, created}
        tables/{id}.csv
        tables/{id}.meta.json
        reports/{id}.html
        reports/{id}.meta.json
        conversations/{id}.json     ← messages + serialized figures + tables
        conversations/{id}.meta.json
        _trash/<type>/...           ← soft-deleted entries live here

No versioning yet. Saves create a new entry by default; toggling "Replace
existing" in the save dialog overwrites a same-named entry instead.
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st
import streamlit.components.v1 as components

import reports
from dialogs import confirm_or_run
import theme


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------
SAVED_DIR = Path(__file__).parent / "saved"
TRASH_DIR = SAVED_DIR / "_trash"
TABLES_SOURCE_DIR = Path(__file__).parent / "data" / "tables"

ITEM_TYPES = ("charts", "tables", "reports", "conversations")
EXT_BY_TYPE = {
    "charts": "json",
    "tables": "csv",
    "reports": "html",
    "conversations": "json",
}


def _ensure_dirs() -> None:
    for t in ITEM_TYPES:
        (SAVED_DIR / t).mkdir(parents=True, exist_ok=True)
        (TRASH_DIR / t).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Listing + lookup
# ---------------------------------------------------------------------------
def list_items(item_type: str, *, in_trash: bool = False) -> list[dict]:
    """Return every entry of `item_type`, sorted most-recent first.

    Each returned dict is the meta.json contents plus `_content_path` for
    quick access. `in_trash=True` lists the trash folder instead.
    """
    _ensure_dirs()
    parent = (TRASH_DIR if in_trash else SAVED_DIR) / item_type
    items: list[dict] = []
    for meta_path in parent.glob("*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entry_id = meta_path.stem.replace(".meta", "")
        data["id"] = data.get("id", entry_id)
        data["_content_path"] = parent / f"{entry_id}.{EXT_BY_TYPE[item_type]}"
        data["_meta_path"] = meta_path
        items.append(data)
    items.sort(key=lambda x: x.get("created", ""), reverse=True)
    return items


def find_by_name(item_type: str, name: str) -> dict | None:
    """Return the entry with the given name (case-insensitive), or None."""
    target = name.strip().lower()
    for item in list_items(item_type):
        if item.get("name", "").strip().lower() == target:
            return item
    return None


# ---------------------------------------------------------------------------
# Save (charts, tables)
# ---------------------------------------------------------------------------
def _write_entry(item_type: str, name: str, write_content) -> str:
    """Common save pipeline. `write_content(content_path)` writes the asset
    file at `content_path`; this function handles the id, the meta.json,
    and the directory plumbing."""
    _ensure_dirs()
    entry_id = uuid.uuid4().hex[:12]
    content_path = SAVED_DIR / item_type / f"{entry_id}.{EXT_BY_TYPE[item_type]}"
    meta_path = SAVED_DIR / item_type / f"{entry_id}.meta.json"
    write_content(content_path)
    meta = {
        "id": entry_id,
        "name": name,
        "type": item_type,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return entry_id


def save_chart(name: str, fig: go.Figure, replace_existing: bool = False) -> str:
    """Save a Plotly figure to `saved/charts/`. Returns the entry id."""
    if replace_existing:
        existing = find_by_name("charts", name)
        if existing:
            delete_item("charts", existing["id"])

    def _write(path: Path) -> None:
        path.write_text(pio.to_json(fig), encoding="utf-8")

    return _write_entry("charts", name.strip(), _write)


def save_table(name: str, df: pd.DataFrame, replace_existing: bool = False) -> str:
    """Save a DataFrame to `saved/tables/` as CSV. Returns the entry id."""
    if replace_existing:
        existing = find_by_name("tables", name)
        if existing:
            delete_item("tables", existing["id"])

    def _write(path: Path) -> None:
        df.to_csv(path, index=False)

    return _write_entry("tables", name.strip(), _write)


def save_conversation(
    name: str,
    messages: list[dict],
    figures: dict[str, go.Figure],
    tables: dict[str, pd.DataFrame],
    replace_existing: bool = False,
) -> str:
    """Save the entire chat thread (messages + every figure/table the
    agent produced) to `saved/conversations/`. Returns the entry id.

    The figures/tables stored are scoped to those actually referenced
    by tool_use_ids appearing in the message stream — orphaned
    artefacts from prior turns that were never wired into a tool_result
    aren't carried over.

    Serialisation:
      - figures → Plotly JSON via `pio.to_json` (round-trips perfectly).
      - tables  → pandas JSON, `orient="split"` (preserves column order
        and is dtype-tolerant on round-trip).
      - messages → already JSON-friendly dicts.
    """
    if replace_existing:
        existing = find_by_name("conversations", name)
        if existing:
            delete_item("conversations", existing["id"])

    # Walk messages and pick out every tool_use_id we see in tool_result
    # blocks; those are the artefacts the conversation actually
    # references, so they're the ones worth saving.
    referenced_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tuid = block.get("tool_use_id")
                    if tuid:
                        referenced_ids.add(tuid)

    figures_serialised = {
        tuid: pio.to_json(fig)
        for tuid, fig in figures.items()
        if tuid in referenced_ids
    }
    tables_serialised = {
        tuid: df.to_json(orient="split")
        for tuid, df in tables.items()
        if tuid in referenced_ids
    }

    payload = {
        "messages": messages,
        "figures": figures_serialised,
        "tables": tables_serialised,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }

    def _write(path: Path) -> None:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return _write_entry("conversations", name.strip(), _write)


def save_report(name: str, html_str: str, replace_existing: bool = False) -> str:
    """Save a pre-rendered HTML report to `saved/reports/`. The HTML
    is already self-contained (Plotly.js inlined, tables HTML-encoded);
    this function just stores it. Returns the entry id."""
    if replace_existing:
        existing = find_by_name("reports", name)
        if existing:
            delete_item("reports", existing["id"])

    def _write(path: Path) -> None:
        path.write_text(html_str, encoding="utf-8")

    return _write_entry("reports", name.strip(), _write)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def load_chart(entry_id: str) -> go.Figure | None:
    path = SAVED_DIR / "charts" / f"{entry_id}.json"
    if not path.exists():
        return None
    try:
        return pio.from_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load_table(entry_id: str) -> pd.DataFrame | None:
    path = SAVED_DIR / "tables" / f"{entry_id}.csv"
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return None


def load_conversation(entry_id: str) -> dict | None:
    """Return `{messages, figures, tables, saved_at}` with figures /
    tables already deserialised back to Plotly Figures / DataFrames.
    None if the entry is missing or unreadable.
    """
    path = SAVED_DIR / "conversations" / f"{entry_id}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    figures: dict[str, go.Figure] = {}
    for tuid, fig_json in raw.get("figures", {}).items():
        try:
            figures[tuid] = pio.from_json(fig_json)
        except Exception:  # noqa: BLE001 — skip any single broken fig
            continue

    tables: dict[str, pd.DataFrame] = {}
    for tuid, tbl_json in raw.get("tables", {}).items():
        try:
            # pd.read_json on a string is deprecated in 2.2+; wrap in StringIO.
            tables[tuid] = pd.read_json(io.StringIO(tbl_json), orient="split")
        except Exception:  # noqa: BLE001
            continue

    return {
        "messages": raw.get("messages", []),
        "figures": figures,
        "tables": tables,
        "saved_at": raw.get("saved_at"),
    }


def resume_conversation(entry_id: str) -> bool:
    """Load the saved conversation into `st.session_state` so the user
    can continue chatting. Mutates session state for messages, figures,
    tables. Caller is responsible for switching the view + rerun.
    Returns True on success, False if the entry couldn't be loaded.
    """
    payload = load_conversation(entry_id)
    if payload is None:
        return False
    st.session_state.messages = payload["messages"]
    st.session_state.figures = payload["figures"]
    st.session_state.tables = payload["tables"]
    # Stamp the meta so the list shows "Resumed N min ago".
    meta_path = SAVED_DIR / "conversations" / f"{entry_id}.meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["last_resumed"] = datetime.now().isoformat(timespec="seconds")
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    return True


def load_report(entry_id: str) -> str | None:
    """Return the raw HTML of a saved report, or None if missing."""
    path = SAVED_DIR / "reports" / f"{entry_id}.html"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Delete / restore / rename
# ---------------------------------------------------------------------------
def _move(src_dir: Path, dst_dir: Path, item_type: str, entry_id: str) -> None:
    """Move both the content file and meta file between dirs."""
    ext = EXT_BY_TYPE[item_type]
    for suffix in (f"{entry_id}.{ext}", f"{entry_id}.meta.json"):
        src = src_dir / item_type / suffix
        dst = dst_dir / item_type / suffix
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))


def delete_item(item_type: str, entry_id: str) -> None:
    """Soft-delete: move to `saved/_trash/<type>/`."""
    _ensure_dirs()
    # Stamp the meta with deleted-at before moving so the trash list can
    # show when it was deleted.
    meta_path = SAVED_DIR / item_type / f"{entry_id}.meta.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["deleted"] = datetime.now().isoformat(timespec="seconds")
            meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    _move(SAVED_DIR, TRASH_DIR, item_type, entry_id)


def restore_item(item_type: str, entry_id: str) -> None:
    """Move from `_trash/` back to `saved/<type>/`. Clears `deleted` field."""
    _ensure_dirs()
    meta_path = TRASH_DIR / item_type / f"{entry_id}.meta.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data.pop("deleted", None)
            meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    _move(TRASH_DIR, SAVED_DIR, item_type, entry_id)


def permanently_delete(item_type: str, entry_id: str) -> None:
    """Wipe a trash entry forever — no recovery."""
    for suffix in (f"{entry_id}.{EXT_BY_TYPE[item_type]}", f"{entry_id}.meta.json"):
        p = TRASH_DIR / item_type / suffix
        if p.exists():
            p.unlink()


def rename_item(item_type: str, entry_id: str, new_name: str) -> bool:
    """Rename an entry. Returns False on name collision with a *different* entry."""
    new_name = new_name.strip()
    if not new_name:
        return False
    meta_path = SAVED_DIR / item_type / f"{entry_id}.meta.json"
    if not meta_path.exists():
        return False
    existing = find_by_name(item_type, new_name)
    if existing and existing["id"] != entry_id:
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["name"] = new_name
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except (json.JSONDecodeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Promote a saved table into the source-data directory
# ---------------------------------------------------------------------------
def promote_to_source_data(entry_id: str, csv_filename: str) -> tuple[bool, str]:
    """Copy a saved table into `data/tables/`. Returns (ok, message)."""
    src = SAVED_DIR / "tables" / f"{entry_id}.csv"
    if not src.exists():
        return False, "Saved table file not found."
    TABLES_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    name = csv_filename.strip()
    if not name:
        return False, "Filename can't be empty."
    if not name.lower().endswith(".csv"):
        name += ".csv"
    target = TABLES_SOURCE_DIR / name
    if target.exists():
        return False, f"`{name}` already exists in data/tables/."
    shutil.copy(str(src), str(target))
    return True, f"Copied to `data/tables/{name}`."


# ---------------------------------------------------------------------------
# Save dialogs
# ---------------------------------------------------------------------------
def _default_name(prefix: str) -> str:
    return f"{prefix} — {datetime.now().strftime('%b %d, %H:%M')}"


@st.dialog("Save chart")
def show_save_chart_dialog(fig_id: str) -> None:
    fig = st.session_state.get("figures", {}).get(fig_id)
    if fig is None:
        st.error("Chart not found in session.")
        return

    st.plotly_chart(fig, use_container_width=True, key=f"_save_dlg_chart_{fig_id}")

    name = st.text_input(
        "Name",
        value=_default_name("Chart"),
        key=f"_save_chart_name_{fig_id}",
    )

    existing = find_by_name("charts", name) if name.strip() else None
    replace = False
    if existing:
        st.warning(f"An entry named '{existing['name']}' already exists.")
        replace = st.toggle(
            "Replace existing entry",
            key=f"_save_chart_replace_{fig_id}",
        )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Save",
            type="primary",
            icon=":material/save:",
            key=f"_save_chart_btn_{fig_id}",
            use_container_width=True,
        ):
            clean = name.strip()
            if not clean:
                st.error("Name can't be empty.")
                return
            if existing and not replace:
                st.error("Name in use. Toggle 'Replace existing' or pick another name.")
                return
            save_chart(clean, fig, replace_existing=replace)
            st.rerun()
    with col_b:
        if st.button(
            "Cancel",
            key=f"_cancel_save_chart_{fig_id}",
            use_container_width=True,
        ):
            st.rerun()


@st.dialog("Save table")
def show_save_table_dialog(table_id: str) -> None:
    df = st.session_state.get("tables", {}).get(table_id)
    if df is None:
        st.error("Table not found in session.")
        return

    st.dataframe(df.head(50), use_container_width=True)
    if len(df) > 50:
        st.caption(f"Showing first 50 of {len(df):,} rows.")

    name = st.text_input(
        "Name",
        value=_default_name("Table"),
        key=f"_save_table_name_{table_id}",
    )

    existing = find_by_name("tables", name) if name.strip() else None
    replace = False
    if existing:
        st.warning(f"An entry named '{existing['name']}' already exists.")
        replace = st.toggle(
            "Replace existing entry",
            key=f"_save_table_replace_{table_id}",
        )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Save",
            type="primary",
            icon=":material/save:",
            key=f"_save_table_btn_{table_id}",
            use_container_width=True,
        ):
            clean = name.strip()
            if not clean:
                st.error("Name can't be empty.")
                return
            if existing and not replace:
                st.error("Name in use. Toggle 'Replace existing' or pick another name.")
                return
            save_table(clean, df, replace_existing=replace)
            st.rerun()
    with col_b:
        if st.button(
            "Cancel",
            key=f"_cancel_save_table_{table_id}",
            use_container_width=True,
        ):
            st.rerun()


@st.dialog("Save conversation", width="medium")
def show_save_conversation_dialog() -> None:
    """Save the active chat thread under a name. Triggered by the
    'Save chat' button in the sidebar — reads `st.session_state.messages`,
    `.figures`, `.tables` directly.
    """
    messages = st.session_state.get("messages", [])
    figures = st.session_state.get("figures", {})
    tables = st.session_state.get("tables", {})

    n_user = sum(
        1 for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    )
    n_assistant_blocks = 0
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            n_assistant_blocks += sum(
                1 for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            )

    st.caption(
        f"Saves the full thread — every user question, every assistant "
        f"response, plus the charts and tables produced along the way."
    )

    parts = []
    if n_user:
        parts.append(f"{n_user} question{'s' if n_user != 1 else ''}")
    if n_assistant_blocks:
        parts.append(f"{n_assistant_blocks} assistant response block(s)")
    if figures:
        parts.append(f"{len(figures)} chart{'s' if len(figures) != 1 else ''}")
    if tables:
        parts.append(f"{len(tables)} table{'s' if len(tables) != 1 else ''}")
    if parts:
        st.markdown(f"**Contents:** {' · '.join(parts)}")

    # Default name = first user question, trimmed. Falls back to a
    # timestamp when there isn't one.
    first_q = next(
        (m["content"] for m in messages
         if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    suggested = (first_q[:80] + ("…" if len(first_q) > 80 else "")) or _default_name("Chat")

    name = st.text_input("Name", value=suggested, key="_save_convo_name")

    existing = find_by_name("conversations", name) if name.strip() else None
    replace = False
    if existing:
        st.warning(f"A conversation named '{existing['name']}' already exists.")
        replace = st.toggle("Replace existing entry", key="_save_convo_replace")

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Save conversation",
            type="primary",
            icon=":material/save:",
            key="_save_convo_btn",
            use_container_width=True,
        ):
            clean = name.strip()
            if not clean:
                st.error("Name can't be empty.")
                return
            if existing and not replace:
                st.error("Name in use. Toggle 'Replace existing' or pick another.")
                return
            if not messages:
                st.error("No conversation to save.")
                return
            save_conversation(clean, messages, figures, tables, replace_existing=replace)
            st.toast(f"Saved conversation '{clean}'.", icon=":material/check_circle:")
            st.rerun()
    with col_b:
        if st.button(
            "Cancel",
            key="_cancel_save_convo",
            use_container_width=True,
        ):
            st.rerun()


@st.dialog("Save as report", width="large")
def show_save_report_dialog(question: str, assistant_blocks: list[dict]) -> None:
    """Render an exchange to HTML and stash it under `saved/reports/`.

    The dialog shows a name input (defaulted from the question), a
    preview note about what's getting packaged (how many text blocks /
    charts / tables), and the standard Save / Cancel pair. The actual
    HTML render happens at Save time — no point building a 3 MB string
    if the user cancels.
    """
    # Quick summary of what's about to be packaged so the user knows
    # they're saving the right exchange.
    n_text = sum(1 for b in assistant_blocks if b.get("type") == "text" and b.get("text", "").strip())
    fig_ids = {b.get("tool_use_id") for b in assistant_blocks
               if b.get("type") == "tool_result" and b.get("tool_use_id") in st.session_state.get("figures", {})}
    table_ids = {b.get("tool_use_id") for b in assistant_blocks
                 if b.get("type") == "tool_result" and b.get("tool_use_id") in st.session_state.get("tables", {})}

    st.caption("Packaging this exchange as a self-contained HTML file.")
    summary_bits = []
    if n_text:
        summary_bits.append(f"{n_text} text block{'s' if n_text != 1 else ''}")
    if fig_ids:
        summary_bits.append(f"{len(fig_ids)} chart{'s' if len(fig_ids) != 1 else ''}")
    if table_ids:
        summary_bits.append(f"{len(table_ids)} table{'s' if len(table_ids) != 1 else ''}")
    if summary_bits:
        st.markdown(f"**Contents:** {' · '.join(summary_bits)}")

    suggested = (question[:80] + ("…" if len(question) > 80 else "")) or _default_name("Report")
    name = st.text_input(
        "Name",
        value=suggested,
        key="_save_report_name",
    )

    existing = find_by_name("reports", name) if name.strip() else None
    replace = False
    if existing:
        st.warning(f"A report named '{existing['name']}' already exists.")
        replace = st.toggle(
            "Replace existing entry",
            key="_save_report_replace",
        )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Save report",
            type="primary",
            icon=":material/save:",
            key="_save_report_btn",
            use_container_width=True,
        ):
            clean = name.strip()
            if not clean:
                st.error("Name can't be empty.")
                return
            if existing and not replace:
                st.error("Name in use. Toggle 'Replace existing' or pick another name.")
                return
            html_str = reports.render_exchange_to_html(
                question=question,
                assistant_blocks=assistant_blocks,
                figures=st.session_state.get("figures", {}),
                tables=st.session_state.get("tables", {}),
                report_name=clean,
            )
            save_report(clean, html_str, replace_existing=replace)
            st.toast(f"Saved report '{clean}'.", icon=":material/check_circle:")
            st.rerun()
    with col_b:
        if st.button(
            "Cancel",
            key="_cancel_save_report",
            use_container_width=True,
        ):
            st.rerun()


@st.dialog("Use as source data")
def show_promote_table_dialog(entry_id: str, default_name: str) -> None:
    """Copy a saved table into data/tables/ so the agent can use it."""
    filename = st.text_input(
        "Filename in data/tables/",
        value=default_name,
        key=f"_promote_filename_{entry_id}",
        help="Will be saved as a CSV. Pick a name without spaces if possible.",
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Copy",
            type="primary",
            icon=":material/content_copy:",
            use_container_width=True,
        ):
            ok, msg = promote_to_source_data(entry_id, filename)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
    with col_b:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------
def render() -> None:
    """The Saved Items view, rendered when the sidebar selects it."""
    st.subheader("Saved Items")
    st.caption(
        "Charts, tables, and reports you've saved from the chat. "
        "Sorted most-recent first."
    )

    sub_chats, sub_charts, sub_tables, sub_reports, sub_trash = st.tabs(
        ["Conversations", "Charts", "Tables", "Reports", "Trash"]
    )

    with sub_chats:
        _render_items_of_type("conversations")

    with sub_charts:
        _render_items_of_type("charts")

    with sub_tables:
        _render_items_of_type("tables")

    with sub_reports:
        _render_items_of_type("reports")

    with sub_trash:
        _render_trash()


_CARDS_PER_ROW = 3


def _render_items_of_type(item_type: str) -> None:
    """Render saved items of one type as a 3-column card grid. Each card
    has a preview thumbnail at top, name + date in the middle, and
    compact action icons at the bottom. View / Rename go through modal
    dialogs (`show_view_item_dialog`, `show_rename_item_dialog`) — fitting
    inline editors into a card is cramped and the modal scales better
    for the actual content."""
    items = list_items(item_type)
    if not items:
        st.info(f"No saved {item_type} yet. Save one from a chat message.")
        return

    # 3-column grid. Streamlit's columns don't reflow on narrow viewports,
    # so this is fixed-width on desktop. Iterate in chunks of N.
    for chunk_start in range(0, len(items), _CARDS_PER_ROW):
        chunk = items[chunk_start:chunk_start + _CARDS_PER_ROW]
        cols = st.columns(_CARDS_PER_ROW)
        for col, item in zip(cols, chunk):
            with col:
                _render_card(item_type, item)


def _render_card(item_type: str, item: dict) -> None:
    eid = item["id"]
    with st.container(border=True):
        _render_card_preview(item_type, item)
        st.markdown(f"**{item['name']}**")
        st.caption(_friendly_date(item.get("created")))

        # Action row — narrow columns so the icon buttons read as icons.
        # Conversations + tables + reports each have a 4th action (Resume,
        # Use-as-data, Download respectively); other types have just 3.
        n_actions = 4 if item_type in ("tables", "reports", "conversations") else 3
        action_cols = st.columns(n_actions)

        with action_cols[0]:
            if st.button(
                "",
                icon=":material/visibility:",
                help="View",
                key=f"_view_btn_{eid}",
                use_container_width=True,
            ):
                show_view_item_dialog(item_type, eid)
        with action_cols[1]:
            if st.button(
                "",
                icon=":material/edit:",
                help="Rename",
                key=f"_rename_btn_{eid}",
                use_container_width=True,
            ):
                show_rename_item_dialog(item_type, eid)
        with action_cols[2]:
            if st.button(
                "",
                icon=":material/delete:",
                help="Move to trash",
                key=f"_del_btn_{eid}",
                use_container_width=True,
            ):
                def _do_delete(t=item_type, x=eid):
                    delete_item(t, x)

                confirm_or_run(
                    f"Move '{item['name']}' to Trash?",
                    "Yes, move to trash",
                    _do_delete,
                )

        if item_type == "tables":
            with action_cols[3]:
                if st.button(
                    "",
                    icon=":material/move_to_inbox:",
                    help="Use as source data (copy to data/tables/)",
                    key=f"_promote_btn_{eid}",
                    use_container_width=True,
                ):
                    default = item["name"].lower().replace(" ", "_") + ".csv"
                    show_promote_table_dialog(eid, default)
        elif item_type == "reports":
            with action_cols[3]:
                report_path = item["_content_path"]
                if report_path.exists():
                    st.download_button(
                        "",
                        data=report_path.read_bytes(),
                        file_name=f"{item['name']}.html",
                        mime="text/html",
                        icon=":material/download:",
                        help="Download the self-contained HTML file.",
                        key=f"_download_report_{eid}",
                        use_container_width=True,
                    )
        elif item_type == "conversations":
            with action_cols[3]:
                if st.button(
                    "",
                    icon=":material/play_arrow:",
                    help="Resume — load this conversation back into chat and continue",
                    key=f"_resume_btn_{eid}",
                    use_container_width=True,
                ):
                    _trigger_resume(eid)


def _render_card_preview(item_type: str, item: dict) -> None:
    """Top of each card — a compact preview of the content."""
    eid = item["id"]
    if item_type == "charts":
        fig = load_chart(eid)
        if fig is None:
            st.caption("(chart unavailable)")
            return
        # Compact thumbnail: smaller margins, fixed height, no toolbar.
        # `update_layout` alone is enough — no need to call
        # `full_figure_for_development()`, which would spin up a headless
        # Chrome via kaleido for development-time defaults computation.
        fig.update_layout(
            margin=dict(l=10, r=10, t=30, b=10),
            height=200,
            showlegend=False,
        )
        st.plotly_chart(
            fig,
            use_container_width=True,
            key=f"_thumb_chart_{eid}",
            config={"displayModeBar": False, "staticPlot": True},
        )
    elif item_type == "tables":
        df = load_table(eid)
        if df is None:
            st.caption("(table unavailable)")
            return
        # First few rows, no index, capped columns so the card doesn't
        # blow horizontally on a wide table.
        preview_df = df.iloc[:3, :4]
        st.dataframe(preview_df, use_container_width=True, hide_index=True, height=140)
        st.caption(f"{len(df):,} rows × {len(df.columns)} cols")
    elif item_type == "reports":
        # HTML reports don't preview cleanly in a small thumbnail — use
        # a clean placeholder block instead.
        st.markdown(
            "<div style='height:140px; display:flex; align-items:center; "
            "justify-content:center; background:#F6F9FC; border-radius:6px; "
            "color:#697386; font-size:2rem;'>"
            "📄"
            "</div>",
            unsafe_allow_html=True,
        )
    elif item_type == "conversations":
        # Preview shows the first user question + a turn count, framed
        # in a quote-style block so it reads as "what was this about".
        payload = load_conversation(eid)
        if payload is None:
            st.caption("(conversation unavailable)")
            return
        messages = payload["messages"]
        first_q = next(
            (m["content"] for m in messages
             if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "(no questions yet)",
        )
        n_turns = sum(
            1 for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        first_q_short = first_q[:140] + ("…" if len(first_q) > 140 else "")
        st.markdown(
            f"<div style='height:140px; padding:14px 16px; "
            f"background:#F6F9FC; border-left:3px solid #635BFF; "
            f"border-radius:4px; color:#424770; font-size:0.85rem; "
            f"line-height:1.45; overflow:hidden;'>"
            f"<div style='font-style:italic;'>{first_q_short}</div>"
            f"<div style='margin-top:8px; color:#697386; font-size:0.75rem;'>"
            f"{n_turns} turn{'s' if n_turns != 1 else ''}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _friendly_date(iso: str | None) -> str:
    if not iso:
        return "Saved (unknown date)"
    # ISO comes through as "2026-05-16T13:45:00" — strip the time for
    # the card caption (full timestamp shows in the view dialog).
    return f"Saved {iso.split('T')[0]}"


# ---------------------------------------------------------------------------
# View / Rename modals — replace the old inline expand-toggle + inline-edit
# patterns. Cards are too small to render the full content inline, and modals
# scale to whatever the item needs.
# ---------------------------------------------------------------------------
def _trigger_resume(entry_id: str) -> None:
    """Resume button handler — confirms if there's an active chat to
    overwrite, then loads the saved conversation and routes to chat view.
    """
    has_active_chat = bool(st.session_state.get("messages"))

    def _do_resume(eid=entry_id) -> None:
        if not resume_conversation(eid):
            st.toast("Could not load conversation.", icon=":material/error:")
            return
        # Switch to the chat view so the resumed messages render. The
        # app.py module owns the view constant string; mirror it here.
        st.session_state.view = "Ask the Snoop"
        st.toast("Conversation resumed.", icon=":material/play_circle:")

    if has_active_chat:
        confirm_or_run(
            "Resuming will replace your current chat. Continue?",
            "Yes, resume",
            _do_resume,
        )
    else:
        _do_resume()
        st.rerun()


@st.dialog("View", width="large")
def show_view_item_dialog(item_type: str, eid: str) -> None:
    item = next((i for i in list_items(item_type) if i["id"] == eid), None)
    if item is None:
        st.error("Item not found.")
        return
    st.markdown(f"**{item['name']}**")
    st.caption(f"Saved {item.get('created', '?')}")
    st.divider()

    if item_type == "charts":
        fig = load_chart(eid)
        if fig is None:
            st.error("Could not load chart.")
        else:
            st.plotly_chart(fig, use_container_width=True, key=f"_view_dlg_chart_{eid}")
    elif item_type == "tables":
        df = load_table(eid)
        if df is None:
            st.error("Could not load table.")
        else:
            st.caption(f"{len(df):,} rows × {len(df.columns)} columns")
            st.dataframe(df, use_container_width=True)
    elif item_type == "reports":
        html_str = load_report(eid)
        if html_str is None:
            st.error("Could not load report.")
        else:
            components.html(html_str, height=600, scrolling=True)
    elif item_type == "conversations":
        payload = load_conversation(eid)
        if payload is None:
            st.error("Could not load conversation.")
            return
        _render_conversation_readonly(payload)


def _render_conversation_readonly(payload: dict) -> None:
    """Render a saved conversation's messages in a read-only preview.
    Lighter than the live chat — no copy / save-as-report buttons, no
    iteration logic. Just the user / assistant bubbles with text, charts,
    and tables inline."""
    messages = payload["messages"]
    figures = payload["figures"]
    tables = payload["tables"]

    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            with st.chat_message("user"):
                st.markdown(msg["content"])
            i += 1
            with st.chat_message("assistant"):
                while i < len(messages) and not (
                    messages[i].get("role") == "user"
                    and isinstance(messages[i].get("content"), str)
                ):
                    blocks = messages[i].get("content")
                    if isinstance(blocks, list):
                        for block in blocks:
                            btype = block.get("type")
                            if btype == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    st.markdown(text)
                            elif btype == "tool_result":
                                tuid = block.get("tool_use_id", "")
                                fig = figures.get(tuid)
                                if fig is not None:
                                    st.plotly_chart(
                                        fig, use_container_width=True,
                                        key=f"_view_convo_fig_{tuid}_{i}",
                                    )
                                df = tables.get(tuid)
                                if df is not None:
                                    st.dataframe(df, use_container_width=True)
                    i += 1
        else:
            i += 1


@st.dialog("Rename")
def show_rename_item_dialog(item_type: str, eid: str) -> None:
    item = next((i for i in list_items(item_type) if i["id"] == eid), None)
    if item is None:
        st.error("Item not found.")
        return
    new_name = st.text_input(
        "Name",
        value=item["name"],
        key=f"_rename_input_dlg_{eid}",
    )
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "Save",
            type="primary",
            icon=":material/save:",
            key=f"_rename_save_dlg_{eid}",
            use_container_width=True,
        ):
            ok = rename_item(item_type, eid, new_name)
            if ok:
                st.rerun()
            else:
                st.error("Name already in use or invalid.")
    with col_b:
        if st.button(
            "Cancel",
            key=f"_rename_cancel_dlg_{eid}",
            use_container_width=True,
        ):
            st.rerun()


def _render_trash() -> None:
    has_any = False
    for t in ITEM_TYPES:
        trash_items = list_items(t, in_trash=True)
        if not trash_items:
            continue
        has_any = True
        st.markdown(f"#### {t.title()}")
        for item in trash_items:
            with st.container(border=True):
                col_name, col_restore, col_purge = st.columns([4, 1, 1])
                with col_name:
                    st.markdown(f"**{item['name']}**")
                    st.caption(f"Deleted {item.get('deleted', '?')}")
                with col_restore:
                    if st.button(
                        "Restore",
                        icon=":material/restore:",
                        key=f"_restore_{t}_{item['id']}",
                        use_container_width=True,
                    ):
                        restore_item(t, item["id"])
                        st.rerun()
                with col_purge:
                    if st.button(
                        "Delete",
                        icon=":material/delete_forever:",
                        key=f"_purge_{t}_{item['id']}",
                        use_container_width=True,
                    ):
                        def _do_purge(tt=t, eid=item["id"]):
                            permanently_delete(tt, eid)

                        confirm_or_run(
                            f"Permanently delete '{item['name']}'? This can't be undone.",
                            "Yes, delete forever",
                            _do_purge,
                        )
    if not has_any:
        st.info("Trash is empty.")

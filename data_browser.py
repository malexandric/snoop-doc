"""
Data browser tab — view and edit files in data/tables/ and data/context/.

Lets users browse the CSVs and markdown explainer docs that live alongside
the app, edit them in-place, upload new ones, delete, and rename. Saves
write back to disk directly so the AI sees the changes on its next tool
call once real-data loading is wired in.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import context_chat
import context_pointers
import doc_import
import gsheets
import stripe_sync
import theme
import tools
from dialogs import confirm_or_run


DATA_DIR = Path(__file__).parent / "data"
TABLES_DIR = DATA_DIR / "tables"
CONTEXT_DIR = DATA_DIR / "context"
# Doc-related context lives in this subfolder. Each filename matches its CSV
# stem (e.g. `pnl_2026.csv` ↔ `tables/pnl_2026.md`). General context docs
# (company info, glossary, etc.) live in CONTEXT_DIR root and are managed
# from the top-level Context view, not here.
CONTEXT_TABLES_DIR = CONTEXT_DIR / "tables"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def render() -> None:
    # The AI context-chat dialog lives in its own module. Calling it here
    # opens / keeps-open the dialog whenever the relevant session_state is
    # set (either from clicking a Context button or after a fresh upload).
    context_chat.maybe_render()
    # The context-management dialog (set a schema-doc pointer, edit the
    # individual context doc, etc.) is owned by this module. Same pattern
    # as `context_chat.maybe_render`: opens whenever a session_state flag
    # is set, persists across reruns inside the dialog.
    _maybe_render_context_dialog()
    # Rename dialog — same single-flag pattern. Opens whenever the row's
    # rename button is clicked; cleared on save/cancel.
    _maybe_render_rename_dialog()

    st.subheader("Data")
    st.caption(
        "Browse and edit the financial data files in `data/tables/`. "
        "Use the context button on each file to edit its explainer doc; "
        "company-wide context docs live in the **Context** view."
    )

    _render_tables()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _list_files(directory: Path, suffix: str) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == suffix
    )


def _rel(path: Path) -> str:
    """Render a path relative to the project root for friendlier display."""
    try:
        return str(path.relative_to(Path(__file__).parent))
    except ValueError:
        return str(path)


def _context_path_for_csv(csv_filename: str) -> Path:
    """Return the path to the matching context doc for a CSV.

    `pnl_2026.csv` → `data/context/tables/pnl_2026.md`. The doc may not
    exist yet. Doc-related context lives under `tables/` so we can cleanly
    separate it from the general (always-loaded) context docs in the root.
    """
    return CONTEXT_TABLES_DIR / f"{Path(csv_filename).stem}.md"


def _suggest_csv_filename(original: str) -> str:
    """Suggest a cleaner default filename for an uploaded CSV.

    Doesn't replace the user's choice — just gives a sensible default in
    the upload dialog. Falls back to the original if cleaning produces an
    empty result.
    """
    stem = tools.sanitize_name(original)
    if not stem or stem == "df":
        return original
    return f"{stem}.csv"


def _render_folder_readme(directory: Path) -> None:
    """If a README.md exists in this folder, show it as a collapsed expander."""
    readme_path = directory / "README.md"
    if not readme_path.exists():
        return
    try:
        content = readme_path.read_text(encoding="utf-8")
    except OSError:
        return
    with st.expander(
        f"README — conventions for `{_rel(directory)}/`",
        expanded=False,
        icon=theme.ICON["readme"],
    ):
        st.markdown(content)


_STALENESS_ICON = {
    "fresh": ":material/cloud_done:",
    "stale": ":material/cloud_sync:",
    "old": ":material/cloud_alert:",
    "never": ":material/cloud_off:",
}


@st.cache_data(show_spinner=False)
def _read_csv_bytes(path_str: str, mtime_ns: int) -> bytes:
    """Cache file contents per session, keyed by mtime. Backs the
    per-row Download button: without caching we'd re-read every CSV
    visible on screen on every Streamlit rerun, which is bad when the
    Stripe tables are hundreds of MB. Cache invalidates automatically
    when a file is rewritten (different mtime_ns)."""
    return Path(path_str).read_bytes()


@st.cache_data(show_spinner=False)
def _build_data_zip(_signature: tuple) -> bytes:
    """Pack every CSV under `data/tables/` into an in-memory zip.

    `_signature` (passed in by the caller as a tuple of
    `(filename, mtime_ns)` for every CSV) is what Streamlit's cache
    keys on — so the zip only rebuilds when a CSV is added, removed,
    or rewritten. Without this we'd repack every CSV on every Streamlit
    rerun, which is heavy when the Stripe tables are large.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for csv_path in _list_files(TABLES_DIR, ".csv"):
            zf.write(csv_path, arcname=csv_path.name)
    return buf.getvalue()


def _data_zip_signature() -> tuple:
    """Tuple of `(filename, mtime_ns)` for every CSV — cheap to compute
    on each render, used as the cache key for `_build_data_zip`."""
    return tuple(
        (p.name, p.stat().st_mtime_ns) for p in _list_files(TABLES_DIR, ".csv")
    )


def _render_gsheets_sync_caption(entry: gsheets.SyncEntry) -> None:
    label = gsheets.staleness_label(entry)
    band = gsheets.staleness_band(entry)
    target = f"`{entry.sheet_title}` › `{entry.tab_name_at_sync}`"
    if entry.last_sync_error:
        st.caption(f":material/cloud_alert: {target} — **sync error:** {entry.last_sync_error}")
    else:
        band_word = {"fresh": "fresh", "stale": "stale", "old": "very stale", "never": ""}[band]
        suffix = f" · {band_word}" if band_word else ""
        st.caption(f"{_STALENESS_ICON[band]} from {target} · synced {label}{suffix}")


def _render_context_status_caption(pointer_docs: list[str], has_individual: bool) -> None:
    """A small one-line summary of the file's context state. Renders
    nothing when neither a pointer nor an individual doc exists — the
    Context button's bare-page icon already signals "set this up".
    """
    if not pointer_docs and not has_individual:
        return
    parts: list[str] = []
    if pointer_docs:
        parts.append(f"schema: `{', '.join(pointer_docs)}`")
    if has_individual:
        parts.append("individual context")
    st.caption(":material/description: " + " · ".join(parts))


def _render_stripe_sync_caption(object_key: str) -> None:
    label = stripe_sync.staleness_label(object_key)
    band = stripe_sync.staleness_band(object_key)
    status = stripe_sync.get_object_status(object_key) or {}
    pretty = stripe_sync.SPECS[object_key].label
    if status.get("last_sync_error"):
        st.caption(f":material/cloud_alert: from Stripe ({pretty}) — **sync error:** {status['last_sync_error']}")
    else:
        band_word = {"fresh": "fresh", "stale": "stale", "old": "very stale", "never": ""}[band]
        suffix = f" · {band_word}" if band_word else ""
        st.caption(f"{_STALENESS_ICON[band]} from Stripe ({pretty}) · synced {label}{suffix}")


def _do_gsheets_sync_one(entry: gsheets.SyncEntry) -> None:
    """Sync one Google Sheets entry inside a spinner + surface via toast."""
    with st.spinner(f"Syncing `{entry.target_filename}`…"):
        updated = gsheets.sync_entry(entry)
    if updated.last_sync_error:
        st.toast(f"Sync failed: {updated.last_sync_error}", icon=":material/sync_problem:")
    else:
        st.toast(f"Synced `{updated.target_filename}`.", icon=":material/cloud_done:")


def _do_stripe_sync_one(object_key: str) -> None:
    """Sync one Stripe object type with a live-updating status panel
    (these can take a while for large accounts)."""
    api_key = st.session_state.get("stripe_api_key", "")
    if not api_key:
        st.toast("Set a Stripe API key in Settings first.", icon=":material/key:")
        return
    pretty = stripe_sync.SPECS[object_key].label
    status = st.status(f"Syncing Stripe {pretty}…", expanded=True)

    def cb(n: int) -> None:
        status.update(label=f"Syncing Stripe {pretty}… {n:,} fetched so far")

    result = stripe_sync.sync_object(object_key, api_key, on_progress=cb)
    if result.get("ok"):
        status.update(
            label=f"Synced {result['rows']:,} {pretty.lower()}.",
            state="complete",
            expanded=False,
        )
        st.toast(f"Synced {result['rows']:,} {pretty.lower()}.", icon=":material/cloud_done:")
    else:
        status.update(
            label=f"Sync failed: {result.get('error')}",
            state="error",
            expanded=True,
        )


def _render_file_list_with_actions(
    files: list[Path],
    selection_key: str,
    key_prefix: str,
    *,
    show_context_action: bool = False,
) -> str | None:
    """Render a list of files as bordered rows with View + Delete actions
    (Context for Tables, plus Sync when a file is registered as a Google
    Sheets sync).

    Returns the currently-open filename, or None if nothing is open. No file
    is selected by default — the user clicks View to open one. View on the
    currently-open file closes it.

    Renaming isn't surfaced in this UI — pick a clean name at upload time
    (the upload dialog asks) or rename on the filesystem.
    """
    file_names = [f.name for f in files]

    current = st.session_state.get(selection_key)
    if current and current not in file_names:
        st.session_state.pop(selection_key, None)
        current = None

    # Pre-load both sync registries once per render so we don't re-read
    # the JSON for every row.
    gs_by_filename = {e.target_filename: e for e in gsheets.load_registry()}
    # Schema-doc pointer map: which general context doc(s) cover each
    # CSV. Built once per render and consulted in the loop below to
    # render a small caption beneath the filename.
    pointer_map = context_pointers.build_pointer_map() if show_context_action else {}

    for f in files:
        is_active = current == f.name
        gs_entry = gs_by_filename.get(f.name)
        stripe_key = stripe_sync.is_stripe_synced_file(f.name)
        is_synced = gs_entry is not None or stripe_key is not None
        has_individual_ctx = _context_path_for_csv(f.name).exists() if show_context_action else False
        pointer_docs = pointer_map.get(f.name, [])

        with st.container(border=True):
            if show_context_action:
                # 7 columns when context is shown: name + 6 action buttons
                # in this order: Sync / View / Download / Context / Rename
                # / Delete. Sync sits first because it's the most likely
                # action on a synced file; Download is grouped next to
                # View since both are "get the data" actions. Narrow
                # action columns so the icon buttons read as icons.
                # Sync + Rename columns render empty placeholders for
                # rows where the action doesn't apply so the layout
                # doesn't jitter between rows.
                name_col, sync_col, view_col, download_col, ctx_col, rename_col, del_col = st.columns(
                    [8, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7]
                )
            else:
                name_col, view_col, del_col = st.columns([8, 0.7, 0.7])
                ctx_col = sync_col = rename_col = download_col = None

            with name_col:
                st.markdown(f"**{f.name}**")
                if gs_entry is not None:
                    _render_gsheets_sync_caption(gs_entry)
                elif stripe_key is not None:
                    _render_stripe_sync_caption(stripe_key)
                if show_context_action:
                    _render_context_status_caption(pointer_docs, has_individual_ctx)

            if sync_col is not None:
                with sync_col:
                    if gs_entry is not None:
                        if st.button(
                            "",
                            icon=":material/sync:",
                            help="Sync from Google Sheets now",
                            key=f"{key_prefix}_sync_{f.name}",
                            use_container_width=True,
                        ):
                            _do_gsheets_sync_one(gs_entry)
                            st.rerun()
                    elif stripe_key is not None:
                        if st.button(
                            "",
                            icon=":material/sync:",
                            help="Sync from Stripe now",
                            key=f"{key_prefix}_sync_{f.name}",
                            use_container_width=True,
                        ):
                            _do_stripe_sync_one(stripe_key)
                            st.rerun()
                    else:
                        # Empty placeholder keeps the column widths stable
                        # whether or not a row has a sync entry.
                        st.write("")

            with view_col:
                if st.button(
                    "",
                    icon=":material/visibility_off:" if is_active else ":material/visibility:",
                    help="Close" if is_active else "View/Edit",
                    key=f"{key_prefix}_view_{f.name}",
                    use_container_width=True,
                    type="primary" if is_active else "secondary",
                ):
                    if is_active:
                        st.session_state.pop(selection_key, None)
                    else:
                        st.session_state[selection_key] = f.name
                    st.rerun()

            if download_col is not None:
                with download_col:
                    # Cached read so we don't re-load every visible CSV on
                    # every rerun. The mtime_ns argument keys the cache so
                    # a rewritten file invalidates automatically.
                    st.download_button(
                        "",
                        data=_read_csv_bytes(str(f), f.stat().st_mtime_ns),
                        file_name=f.name,
                        mime="text/csv",
                        icon=":material/download:",
                        help="Download CSV",
                        key=f"{key_prefix}_download_{f.name}",
                        use_container_width=True,
                    )

            if ctx_col is not None:
                with ctx_col:
                    has_any_context = bool(pointer_docs) or has_individual_ctx
                    if st.button(
                        "",
                        icon=":material/description:" if has_any_context else ":material/note_add:",
                        help="Edit context",
                        key=f"{key_prefix}_ctx_{f.name}",
                        use_container_width=True,
                    ):
                        st.session_state["_context_dialog_csv"] = f.name
                        st.rerun()

            if rename_col is not None:
                with rename_col:
                    # Stripe-synced filenames are canonical (stripe_invoices.csv etc.);
                    # renaming would just leave the renamed file stranded since the
                    # next sync writes the canonical name again. Show a disabled-
                    # ish placeholder for those rows so the column lines up.
                    if stripe_key is not None:
                        st.write("")
                    elif st.button(
                        "",
                        icon=":material/text_fields:",
                        help="Rename",
                        key=f"{key_prefix}_rename_{f.name}",
                        use_container_width=True,
                    ):
                        st.session_state["_rename_dialog_csv"] = f.name
                        st.rerun()

            with del_col:
                if st.button(
                    "",
                    icon=":material/delete:",
                    help="Delete",
                    key=f"{key_prefix}_del_{f.name}",
                    use_container_width=True,
                ):
                    def _do_delete(path=f, sk=selection_key):
                        try:
                            path.unlink()
                            # If the open file just got deleted, close it.
                            if st.session_state.get(sk) == path.name:
                                st.session_state.pop(sk, None)
                            # Drop any registry entry pointing at this file
                            # so it doesn't reappear as a phantom "synced
                            # but missing" row on the next render. Both
                            # sync sources have a remove-by-filename hook.
                            gsheets.remove_entry_by_filename(path.name)
                            # Stripe registry uses object keys; map filename
                            # back if applicable.
                            stripe_key_to_drop = stripe_sync.is_stripe_synced_file(path.name)
                            if stripe_key_to_drop:
                                reg = stripe_sync.load_registry()
                                reg.get("objects", {}).pop(stripe_key_to_drop, None)
                                stripe_sync.save_registry(reg)
                        except OSError as e:
                            st.error(f"Could not delete: {e}")

                    confirm_or_run(
                        f"Delete `{f.name}`? This can't be undone.",
                        "Yes, delete",
                        _do_delete,
                    )

    return st.session_state.get(selection_key)


# ---------------------------------------------------------------------------
# File upload flow — CSV and Excel, with rename + optional context creation
# ---------------------------------------------------------------------------
CSV_UPLOADER_KEY = "_tables_uploader"

_EXCEL_EXTS = {".xlsx", ".xls"}


def _render_file_upload() -> None:
    """Unified uploader for CSV and Excel files.

    Detects new uploads and routes to the appropriate setup dialog:
      - .csv → rename dialog + optional AI context creation
      - .xlsx / .xls → sheet-picker dialog → convert each sheet to a CSV

    Uses a (name, size) signature to avoid re-opening the dialog on
    every rerun while the uploader still holds the file reference.
    """
    uploaded = st.file_uploader(
        "Drop a CSV or Excel file here",
        type=["csv", "xlsx", "xls"],
        key=CSV_UPLOADER_KEY,
        label_visibility="collapsed",
    )
    if uploaded is None:
        return

    sig = (uploaded.name, uploaded.size)
    ext = Path(uploaded.name).suffix.lower()
    pending = st.session_state.get("_csv_upload_pending")
    handled = st.session_state.get("_csv_upload_handled_sig")

    if pending is None and handled != sig:
        st.session_state._csv_upload_pending = {
            "data": uploaded.getvalue(),
            "original_name": uploaded.name,
            "sig": sig,
            "ext": ext,
        }

    if "_csv_upload_pending" in st.session_state:
        pending_ext = st.session_state._csv_upload_pending.get("ext", ".csv")
        if pending_ext in _EXCEL_EXTS:
            _show_excel_upload_setup_dialog()
        else:
            _show_csv_upload_setup_dialog()


def _clear_csv_upload_state(sig) -> None:
    """Drop everything related to the in-flight upload + remember its sig
    so the same file doesn't re-trigger the dialog when the uploader still
    has the file in its state."""
    st.session_state._csv_upload_handled_sig = sig
    for key in (
        "_csv_upload_pending",
        "_csv_setup_filename",
        "_csv_setup_create_ctx",
        "_csv_setup_template",
        "_excel_sheets_cache",
        CSV_UPLOADER_KEY,
    ):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Excel import dialog — sheet picker + per-sheet filename inputs
# ---------------------------------------------------------------------------
@st.dialog("Import Excel workbook", width="large")
def _show_excel_upload_setup_dialog() -> None:
    """Parse the uploaded workbook, let the user pick which sheets to
    import and what CSV name each gets, then write the CSVs to disk."""
    pending = st.session_state.get("_csv_upload_pending")
    if not pending:
        return

    original = pending["original_name"]
    st.caption(f"Importing: `{original}`")

    # Parse the workbook once and cache the sheets in session state so the
    # dialog doesn't re-read the bytes on every widget interaction.
    if "_excel_sheets_cache" not in st.session_state:
        try:
            with st.spinner("Reading workbook…"):
                sheets = doc_import.read_excel_sheets(pending["data"])
            st.session_state._excel_sheets_cache = sheets
        except (RuntimeError, ValueError) as e:
            st.error(str(e))
            if st.button("Close", key="_excel_err_close"):
                _clear_csv_upload_state(pending["sig"])
                st.rerun()
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"Unexpected error reading workbook: {e}")
            if st.button("Close", key="_excel_err_close2"):
                _clear_csv_upload_state(pending["sig"])
                st.rerun()
            return

    sheets: dict = st.session_state._excel_sheets_cache
    if not sheets:
        st.warning("This workbook contains no sheets.")
        if st.button("Close", key="_excel_no_sheets_close"):
            _clear_csv_upload_state(pending["sig"])
            st.rerun()
        return

    st.markdown(
        f"Found **{len(sheets)} sheet{'s' if len(sheets) != 1 else ''}**. "
        "Choose which to import and what to name each CSV."
    )

    workbook_stem = tools.sanitize_name(Path(original).stem) or "sheet"

    # Collect per-sheet config from widgets.
    sheet_configs: dict[str, dict] = {}
    for sheet_name, df in sheets.items():
        with st.container(border=True):
            toggle_col, info_col = st.columns([4, 6])
            with toggle_col:
                enabled = st.toggle(
                    f"**{sheet_name}**",
                    value=True,
                    key=f"_excel_toggle_{sheet_name}",
                )
            with info_col:
                if len(df) == 0:
                    st.caption("empty sheet")
                else:
                    st.caption(f"{len(df):,} rows × {len(df.columns)} columns")

            if enabled:
                suggested = tools.sanitize_name(sheet_name) or workbook_stem
                filename = st.text_input(
                    "Save as",
                    value=f"{suggested}.csv",
                    key=f"_excel_filename_{sheet_name}",
                    help="Will be saved into `data/tables/`.",
                )
                sheet_configs[sheet_name] = {"df": df, "filename": filename.strip()}

    if not any(sheet_configs.values()):
        st.info("Enable at least one sheet to import.")

    use_ai = st.toggle(
        "Use AI to build context docs",
        value=True,
        key="_excel_use_ai",
        help=(
            "After saving, opens the AI context-chat for the first imported "
            "sheet to help you document its columns and business meaning."
        ),
    )

    st.markdown("---")
    save_col, cancel_col = st.columns(2)

    with save_col:
        if st.button(
            "Import",
            type="primary",
            icon=":material/cloud_download:",
            key="_excel_import_btn",
            use_container_width=True,
        ):
            enabled_sheets = {k: v for k, v in sheet_configs.items() if v}
            if not enabled_sheets:
                st.error("Enable at least one sheet to import.")
                return

            # Validate + normalise filenames before writing anything.
            resolved: list[tuple[str, str, pd.DataFrame]] = []  # (sheet_name, csv_name, df)
            seen_names: set[str] = set()
            for sheet_name, cfg in enabled_sheets.items():
                csv_name = cfg["filename"]
                if not csv_name:
                    st.error(f"Filename for sheet '{sheet_name}' is empty.")
                    return
                if not csv_name.lower().endswith(".csv"):
                    csv_name += ".csv"
                if csv_name in seen_names:
                    st.error(f"Two sheets share the filename `{csv_name}`. Use distinct names.")
                    return
                if (TABLES_DIR / csv_name).exists():
                    st.error(
                        f"`{csv_name}` already exists in `data/tables/`. "
                        "Choose a different name or delete the existing file first."
                    )
                    return
                seen_names.add(csv_name)
                resolved.append((sheet_name, csv_name, cfg["df"]))

            # Write CSVs to disk.
            TABLES_DIR.mkdir(parents=True, exist_ok=True)
            first_csv: str | None = None
            for _sheet_name, csv_name, df in resolved:
                try:
                    (TABLES_DIR / csv_name).write_text(
                        df.to_csv(index=False), encoding="utf-8"
                    )
                    if first_csv is None:
                        first_csv = csv_name
                except OSError as e:
                    st.error(f"Could not save `{csv_name}`: {e}")
                    return

            sig = pending["sig"]
            _clear_csv_upload_state(sig)

            if use_ai and first_csv:
                context_chat.open_for_new_upload(first_csv)

            n = len(resolved)
            st.toast(
                f"Imported {n} sheet{'s' if n != 1 else ''} as CSV{'s' if n != 1 else ''}.",
                icon=":material/check_circle:",
            )
            st.rerun()

    with cancel_col:
        if st.button("Cancel", key="_excel_cancel_btn", use_container_width=True):
            _clear_csv_upload_state(pending["sig"])
            st.rerun()


@st.dialog("Set up new data file", width="medium")
def _show_csv_upload_setup_dialog() -> None:
    pending = st.session_state.get("_csv_upload_pending")
    if not pending:
        return

    original = pending["original_name"]
    st.caption(f"Uploading: `{original}`")

    # Suggest a cleaner filename. The user can override.
    if "_csv_setup_filename" not in st.session_state:
        st.session_state._csv_setup_filename = _suggest_csv_filename(original)
    new_name = st.text_input(
        "Save as",
        key="_csv_setup_filename",
        help="Pick a friendlier name than the export's default. Will be saved into `data/tables/`.",
    )

    if "_csv_setup_use_ai" not in st.session_state:
        st.session_state._csv_setup_use_ai = True
    use_ai = st.toggle(
        "Use AI to build the context doc",
        key="_csv_setup_use_ai",
        help=(
            "Opens an AI chat after upload that scans the file and asks "
            "you targeted questions to build a precise context doc. "
            "Recommended. Turn off if you'd rather skip context for now "
            "(you can always add it later via the Context button)."
        ),
    )

    st.markdown("---")
    save_col, cancel_col = st.columns(2)
    with save_col:
        if st.button(
            "Save",
            type="primary",
            icon=":material/save:",
            use_container_width=True,
            key="_csv_setup_save",
        ):
            clean = new_name.strip()
            if not clean:
                st.error("Filename can't be empty.")
                return
            if not clean.lower().endswith(".csv"):
                clean += ".csv"
            target = TABLES_DIR / clean
            if target.exists():
                st.error(f"`{clean}` already exists in `data/tables/`.")
                return
            try:
                TABLES_DIR.mkdir(parents=True, exist_ok=True)
                target.write_bytes(pending["data"])
            except OSError as e:
                st.error(f"Could not save CSV: {e}")
                return

            sig = pending["sig"]
            _clear_csv_upload_state(sig)

            if use_ai:
                # Queue up the AI context-chat dialog. The next rerun
                # picks it up via `context_chat.maybe_render()`.
                context_chat.open_for_new_upload(clean)

            st.rerun()

    with cancel_col:
        if st.button(
            "Cancel",
            use_container_width=True,
            key="_csv_setup_cancel",
        ):
            _clear_csv_upload_state(pending["sig"])
            st.rerun()


# ---------------------------------------------------------------------------
# Action row — three side-by-side buttons that toggle a full-width section
# below. Replaces three stacked expanders so the user sees every way of
# getting data in at a glance, and the active section gets the full page
# width to render its form (the Stripe per-object list especially needs it).
# ---------------------------------------------------------------------------
_DATA_ACTION_KEY = "_data_action"  # values: None | "upload" | "gsheets" | "stripe"


def _render_data_action_row() -> None:
    active = st.session_state.get(_DATA_ACTION_KEY)

    def _toggle(target: str) -> None:
        current = st.session_state.get(_DATA_ACTION_KEY)
        st.session_state[_DATA_ACTION_KEY] = None if current == target else target

    # Upload — full-width row of its own. It's the most-frequent action,
    # and visually separating it from "Integrations" signals "this is the
    # manual path; below are the connected sources."
    if st.button(
        "Upload a data file",
        icon=":material/upload_file:",
        type="primary" if active == "upload" else "secondary",
        use_container_width=True,
        key="_data_action_upload",
        help="Upload a CSV or Excel workbook (.xlsx / .xls).",
    ):
        _toggle("upload")
        st.rerun()

    # Integrations — 3-column row. Two integration toggles (Google
    # Sheets, Stripe) and a "Sync all" meta-action that re-pulls every
    # registered sync source at once. Sync all is conditional: only
    # renders when there's at least one thing to sync; otherwise the
    # slot stays empty so the row layout doesn't jitter.
    st.markdown("**Integrations**")
    int_col1, int_col2, int_col3 = st.columns(3)
    with int_col1:
        if st.button(
            "Sync from Google Sheets",
            icon=":material/cloud_sync:",
            type="primary" if active == "gsheets" else "secondary",
            use_container_width=True,
            key="_data_action_gsheets",
        ):
            _toggle("gsheets")
            st.rerun()
    with int_col2:
        if st.button(
            "Sync from Stripe",
            icon=":material/credit_card:",
            type="primary" if active == "stripe" else "secondary",
            use_container_width=True,
            key="_data_action_stripe",
        ):
            _toggle("stripe")
            st.rerun()
    with int_col3:
        gs_registry_for_sync = gsheets.load_registry()
        stripe_objects_for_sync = stripe_sync.load_registry().get("objects", {})
        stripe_synced_for_sync = [
            k for k in stripe_objects_for_sync
            if k not in stripe_sync.SIDE_EFFECT_OBJECTS
            and k in stripe_sync.SPECS
        ]
        has_anything_to_sync = bool(gs_registry_for_sync) or bool(stripe_synced_for_sync)
        if has_anything_to_sync:
            help_parts = []
            if gs_registry_for_sync:
                help_parts.append(f"{len(gs_registry_for_sync)} Google Sheet(s)")
            if stripe_synced_for_sync:
                help_parts.append(f"{len(stripe_synced_for_sync)} Stripe object type(s)")
            if st.button(
                "Sync all",
                icon=":material/sync:",
                key="_tables_sync_all",
                use_container_width=True,
                help="Re-pull every registered sync — " + " + ".join(help_parts) + ".",
            ):
                _do_sync_all_sources()
                st.rerun()
        else:
            # Empty placeholder keeps the row stable when no sync sources
            # are registered yet.
            st.write("")

    if active == "upload":
        with st.container(border=True):
            _render_file_upload()
    elif active == "gsheets":
        with st.container(border=True):
            _render_google_sync_section()
    elif active == "stripe":
        with st.container(border=True):
            _render_stripe_sync_section()


# ---------------------------------------------------------------------------
# Tables (CSVs)
# ---------------------------------------------------------------------------
def _render_tables() -> None:
    _render_folder_readme(TABLES_DIR)

    _render_data_action_row()

    # Visual breathing room between the Integrations section and the
    # file list — without this they sit too close together.
    st.markdown("<div style='height: 30px;'></div>", unsafe_allow_html=True)

    files = _list_files(TABLES_DIR, ".csv")

    if not files:
        st.info(
            f"No CSV files in `{_rel(TABLES_DIR)}/` yet. "
            "Drop a CSV in there (or upload above) and it'll appear here."
        )
        return

    # File count on its own marker line, then Export-all as a full-width
    # button on its own line. Sync all lives in the Integrations row
    # above (alongside the per-integration toggles), since it's a meta-
    # action over the integration set.
    from datetime import date

    st.markdown(f"**{len(files)} file{'s' if len(files) != 1 else ''}**")
    st.download_button(
        "Export all",
        data=_build_data_zip(_data_zip_signature()),
        file_name=f"snoop-data-{date.today().isoformat()}.zip",
        mime="application/zip",
        icon=":material/download:",
        key="_tables_export_all",
        use_container_width=True,
        help="Download every CSV in `data/tables/` as a single zip.",
    )

    chosen_name = _render_file_list_with_actions(
        files=files,
        selection_key="tables_browser_choice",
        key_prefix="tables",
        show_context_action=True,
    )

    if not chosen_name:
        # Nothing open — no editor below the list.
        return

    file_path = TABLES_DIR / chosen_name
    st.divider()

    try:
        df = pd.read_csv(file_path)
    except Exception as e:  # noqa: BLE001 — surface whatever read_csv complained about
        st.error(f"Could not read `{chosen_name}`: {e}")
        return

    st.caption(f"`{_rel(file_path)}` · {len(df):,} rows × {len(df.columns)} columns")

    # Synced files are read-only here — edits would be silently nuked on
    # the next sync. Send the user back to the source of truth instead.
    gs_entry = gsheets.find_entry_by_filename(chosen_name)
    stripe_key = stripe_sync.is_stripe_synced_file(chosen_name)
    if gs_entry is not None:
        st.info(
            f"This file syncs from Google Sheets ({gs_entry.sheet_title} → "
            f"{gs_entry.tab_name_at_sync}). It's read-only here — edit at "
            "the source, then click **Sync now** in the file list to refresh.",
            icon=":material/lock:",
        )
        st.dataframe(df, use_container_width=True)
        return
    if stripe_key is not None:
        pretty = stripe_sync.SPECS[stripe_key].label
        st.info(
            f"This file syncs from Stripe ({pretty}). It's read-only here — "
            "Stripe is the source of truth. Click **Sync now** in the file "
            "list to refresh.",
            icon=":material/lock:",
        )
        st.dataframe(df, use_container_width=True)
        return

    edited_df = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        key=f"editor_csv_{chosen_name}",
    )

    save_col, info_col = st.columns([1, 3])
    with save_col:
        if st.button(
            "Save changes",
            icon=theme.ICON["save"],
            key=f"save_csv_{chosen_name}",
            use_container_width=True,
        ):
            try:
                edited_df.to_csv(file_path, index=False)
                st.success(f"Saved `{chosen_name}`.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not save: {e}")
    with info_col:
        st.caption(
            "Use the `+` row at the bottom of the table to add rows. "
            "Right-click a row number to delete."
        )


def _do_sync_all_sources() -> None:
    """Run a sync across every registered source (Google Sheets + Stripe).
    Each source has its own progress UI — gsheets a spinner, Stripe a
    live-updating status panel — and surfaces its own result toast so
    failures stay attributable. Stripe runs second because it can be
    much slower; gsheets toast lands first."""

    # --- Google Sheets ---
    gs_entries = gsheets.load_registry()
    if gs_entries:
        with st.spinner(f"Syncing {len(gs_entries)} Google Sheet(s)…"):
            try:
                gs_results = gsheets.sync_all()
            except Exception as e:  # noqa: BLE001
                st.toast(f"Google Sheets sync failed: {e}", icon=":material/sync_problem:")
                gs_results = []
        gs_failed = [e for e in gs_results if e.last_sync_error]
        if gs_failed:
            st.toast(
                f"Google Sheets: synced {len(gs_results) - len(gs_failed)} of "
                f"{len(gs_results)}; {len(gs_failed)} had errors.",
                icon=":material/sync_problem:",
            )
        elif gs_results:
            st.toast(
                f"Google Sheets: synced {len(gs_results)} sheet(s).",
                icon=":material/cloud_done:",
            )

    # --- Stripe ---
    stripe_objects = stripe_sync.load_registry().get("objects", {})
    stripe_real_keys = [
        k for k in stripe_objects if k not in stripe_sync.SIDE_EFFECT_OBJECTS
    ]
    if stripe_real_keys:
        api_key = st.session_state.get("stripe_api_key", "")
        if not api_key:
            st.toast(
                "Stripe API key missing — skipped Stripe portion of sync.",
                icon=":material/key:",
            )
        else:
            # Re-use the existing Stripe sync-all helper which has its
            # own status panel (live fetch counts during long pulls).
            _do_stripe_sync_all()


# ---------------------------------------------------------------------------
# Google Sheets sync — import flow inside an expander
# ---------------------------------------------------------------------------
_GS_URL_KEY = "_gs_import_url"
_GS_WORKBOOK_KEY = "_gs_import_workbook"
_GS_TAB_GID_KEY = "_gs_import_tab_gid"
_GS_FILENAME_KEY = "_gs_import_filename"
_GS_USE_AI_KEY = "_gs_import_use_ai"


def _clear_gs_import_state() -> None:
    for k in (
        _GS_URL_KEY,
        _GS_WORKBOOK_KEY,
        _GS_TAB_GID_KEY,
        _GS_FILENAME_KEY,
        _GS_USE_AI_KEY,
    ):
        st.session_state.pop(k, None)


def _render_google_sync_section() -> None:
    """Two-step import flow rendered inline:
      1. Paste a Google Sheets URL → "Look up tabs" fetches metadata.
      2. Pick a tab from the workbook + a target filename + the AI
         context-doc toggle → "Import" pulls the data and creates the CSV.

    Refuses to run without a Google connection — points the user at
    Settings to wire one up.
    """
    if not gsheets.is_configured():
        st.info(
            "Google Sheets sync isn't set up yet. See README for the "
            "one-time OAuth client setup, then connect in Settings.",
            icon=":material/build:",
        )
        return

    if not gsheets.is_connected():
        st.info(
            "Not connected to Google. Open **Settings** and click "
            "**Connect Google** to authorise.",
            icon=":material/link_off:",
        )
        return

    workbook = st.session_state.get(_GS_WORKBOOK_KEY)

    if workbook is None:
        # Step 1: URL → metadata.
        url = st.text_input(
            "Google Sheets URL",
            placeholder="https://docs.google.com/spreadsheets/d/…",
            key=_GS_URL_KEY,
            help=(
                "Paste the URL of any Google Sheet you have access to. "
                "Snoop will fetch its list of tabs so you can pick which "
                "one to import."
            ),
        )
        if st.button(
            "Look up tabs",
            icon=":material/search:",
            key="_gs_lookup",
            type="primary",
        ):
            sheet_id = gsheets.extract_sheet_id(url or "")
            if not sheet_id:
                st.error("That doesn't look like a Google Sheets URL or ID.")
                return
            try:
                with st.spinner("Fetching workbook metadata…"):
                    wb = gsheets.fetch_workbook_metadata(sheet_id)
            except gsheets.AccessDeniedError as e:
                st.error(str(e))
                return
            except gsheets.NotConnectedError as e:
                st.error(str(e))
                return
            except gsheets.GSheetsError as e:
                st.error(str(e))
                return
            except Exception as e:  # noqa: BLE001
                st.error(f"Unexpected error: {e}")
                return
            if not wb.tabs:
                st.error("This workbook has no tabs.")
                return
            st.session_state[_GS_WORKBOOK_KEY] = wb
            st.rerun()
        return

    # Step 2: pick a tab + filename + AI toggle + Import.
    st.markdown(f"**`{workbook.title}`** — pick one tab to import.")

    tab_labels = [
        f"{t.title}  ({t.rows:,} rows × {t.cols} cols)"
        for t in workbook.tabs
    ]
    default_idx = 0
    chosen_label_idx = st.radio(
        "Tab",
        options=list(range(len(workbook.tabs))),
        format_func=lambda i: tab_labels[i],
        index=default_idx,
        key=_GS_TAB_GID_KEY,
    )
    chosen_tab = workbook.tabs[chosen_label_idx]

    # Suggested CSV filename: sanitised tab name.
    suggested = tools.sanitize_name(chosen_tab.title) or "imported"
    if not suggested.endswith(".csv"):
        suggested += ".csv"
    # Initialise the input only if empty / tab changed.
    prev_filename = st.session_state.get(_GS_FILENAME_KEY)
    if not prev_filename or st.session_state.get("_gs_last_tab_idx") != chosen_label_idx:
        st.session_state[_GS_FILENAME_KEY] = suggested
        st.session_state["_gs_last_tab_idx"] = chosen_label_idx

    new_name = st.text_input(
        "Save as",
        key=_GS_FILENAME_KEY,
        help="Will be saved into `data/tables/`.",
    )

    use_ai = st.toggle(
        "Use AI to build the context doc",
        value=st.session_state.get(_GS_USE_AI_KEY, True),
        key=_GS_USE_AI_KEY,
        help=(
            "Opens an AI chat after import that scans the data and asks "
            "you targeted questions to build a precise context doc. "
            "Recommended for new sheets; turn off if you already have "
            "a context doc for this filename or want to add it later."
        ),
    )

    import_col, cancel_col = st.columns(2)
    with import_col:
        if st.button(
            "Import",
            type="primary",
            icon=":material/cloud_download:",
            key="_gs_import_go",
            use_container_width=True,
        ):
            _do_gs_import(workbook, chosen_tab, (new_name or "").strip(), use_ai)
    with cancel_col:
        if st.button(
            "Cancel",
            key="_gs_import_cancel",
            use_container_width=True,
        ):
            _clear_gs_import_state()
            st.rerun()


# ---------------------------------------------------------------------------
# Stripe sync — per-object list with sync buttons inside an expander
# ---------------------------------------------------------------------------
def _render_stripe_sync_section() -> None:
    """List every Stripe object type with its current sync status and a
    per-object sync button. Plus a Sync-all button at the top. Refuses
    to run without an API key — points the user at Settings."""
    api_key = st.session_state.get("stripe_api_key", "")
    if not api_key:
        st.info(
            "Stripe sync isn't configured yet. Open **Settings → "
            "Integrations → Stripe** to add a restricted API key, then "
            "come back here to sync.",
            icon=":material/key:",
        )
        return

    registry = stripe_sync.load_registry().get("objects", {})
    never_synced = not registry
    if never_synced:
        st.warning(
            "**First sync warning:** for an account with ~25k subscriptions "
            "and ~250k invoices, the initial pull takes **10–25 minutes**. "
            "Subsequent syncs are incremental (~30 seconds). You can sync "
            "objects one at a time below if you'd rather start small "
            "(products + prices are tiny, customers next, then the big "
            "ones: invoices and charges).",
            icon=":material/schedule:",
        )

    # Sync-all button at the top + a "Force full re-sync" companion that
    # nulls out the incremental cursors first. Use the second when the
    # registry's timestamps no longer reflect what's upstream — e.g.
    # right after swapping the API key to a different Stripe account.
    sync_col, force_col, _spacer = st.columns([2, 2, 5])
    with sync_col:
        if st.button(
            "Sync all Stripe objects",
            icon=":material/sync:",
            key="_stripe_sync_all",
            type="primary",
            use_container_width=True,
        ):
            _do_stripe_sync_all()
            st.rerun()
    with force_col:
        if st.button(
            "Force full re-sync",
            icon=":material/restart_alt:",
            key="_stripe_force_full",
            use_container_width=True,
            help=(
                "Clears the 'last synced at' timestamps for every "
                "incremental object (customers, invoices, payment "
                "intents, charges, refunds, disputes, payouts) and "
                "then re-syncs everything from the start. Use this "
                "when historical data appears to be missing — for "
                "example, after switching the API key to a different "
                "Stripe account. Slower than a regular sync."
            ),
        ):
            confirm_or_run(
                "Force a full re-sync of every Stripe object from "
                "the beginning? This will pull all historical data "
                "again — slow on large accounts.",
                "Yes, force full re-sync",
                _do_stripe_sync_all_full,
            )

    st.markdown("---")

    # Per-object list. Skip side-effect objects (subscription_items,
    # invoice_line_items) — they're synced as a side effect of their
    # parent's pull, so a separate button would be confusing.
    for key in stripe_sync.SYNC_ORDER:
        if key in stripe_sync.SIDE_EFFECT_OBJECTS:
            continue
        spec = stripe_sync.SPECS[key]
        status = registry.get(key) or {}
        row_count = status.get("row_count")
        last_err = status.get("last_sync_error")
        band = stripe_sync.staleness_band(key)
        label = stripe_sync.staleness_label(key)

        with st.container(border=True):
            name_col, sync_col = st.columns([8, 1])
            with name_col:
                st.markdown(f"**{spec.label}**")
                if last_err:
                    st.caption(f":material/cloud_alert: **sync error:** {last_err}")
                elif row_count is not None:
                    band_word = {"fresh": "fresh", "stale": "stale", "old": "very stale", "never": ""}[band]
                    suffix = f" · {band_word}" if band_word else ""
                    st.caption(
                        f"{_STALENESS_ICON[band]} `{stripe_sync.filename_for(key)}` · "
                        f"{row_count:,} rows · synced {label}{suffix}"
                    )
                else:
                    st.caption(f"{_STALENESS_ICON['never']} `{stripe_sync.filename_for(key)}` · never synced")
            with sync_col:
                if st.button(
                    "Sync",
                    icon=":material/sync:",
                    key=f"_stripe_sync_{key}",
                    use_container_width=True,
                ):
                    _do_stripe_sync_one(key)
                    st.rerun()

    # Footnote: explain the side-effect CSVs so users don't think they're
    # missing from the per-object list.
    if any(stripe_sync.get_object_status(k) for k in ("subscriptions", "invoices")):
        st.caption(
            ":material/info: `stripe_subscription_items.csv` and "
            "`stripe_invoice_line_items.csv` are generated automatically "
            "from Subscriptions and Invoices syncs — they share one fetch "
            "each, no separate button."
        )


def _do_stripe_sync_all() -> None:
    """Run sync_all (incremental) with a live-updating status panel."""
    _do_stripe_sync_all_impl(full=False)


def _do_stripe_sync_all_full() -> None:
    """Reset every incremental cursor and re-pull every object from
    scratch. Use after swapping API keys or whenever the local
    timestamps are stale vs upstream."""
    _do_stripe_sync_all_impl(full=True)


def _do_stripe_sync_all_impl(full: bool) -> None:
    api_key = st.session_state.get("stripe_api_key", "")
    if not api_key:
        st.toast("Set a Stripe API key in Settings first.", icon=":material/key:")
        return
    label = "Force-syncing all Stripe objects from scratch…" if full else "Syncing all Stripe objects…"
    status = st.status(label, expanded=True)
    state_holder = {"current": ""}

    def cb(object_key: str, n: int) -> None:
        pretty = stripe_sync.SPECS[object_key].label
        if state_holder["current"] != object_key:
            state_holder["current"] = object_key
            status.write(f"**{pretty}**")
        status.update(label=f"Syncing Stripe {pretty}… {n:,} fetched")

    if full:
        status.write("Clearing incremental cursors…")
        results = stripe_sync.sync_all_full(api_key, on_progress=cb)
    else:
        results = stripe_sync.sync_all(api_key, on_progress=cb)

    fail = [(k, r) for k, r in results if not r.get("ok")]
    succ = [(k, r) for k, r in results if r.get("ok")]
    total_rows = sum(r["rows"] for _, r in succ)

    if fail:
        status.update(
            label=(
                f"Synced {len(succ)} of {len(results)} object types "
                f"({total_rows:,} rows total). {len(fail)} failed — see below."
            ),
            state="error",
            expanded=True,
        )
        for key, result in fail:
            pretty = stripe_sync.SPECS[key].label
            status.write(f"  ↳ {pretty}: **{result.get('error')}**")
    else:
        status.update(
            label=f"Synced all {len(results)} object types ({total_rows:,} rows total).",
            state="complete",
            expanded=False,
        )


def _maybe_render_context_dialog() -> None:
    """Open the context-management dialog if a CSV has been queued for it.

    Single-flag pattern (`_context_dialog_csv` in session_state): the
    Context button sets the filename + reruns; the dialog reads it back.
    Cleared inside the dialog's Save/Cancel/launch-AI handlers so it
    doesn't reopen on the next rerun.
    """
    csv_name = st.session_state.get("_context_dialog_csv")
    if not csv_name:
        return
    # If the file no longer exists (deleted while dialog was queued),
    # silently drop the flag — opening a dialog about a missing file
    # would confuse the user more than the missing dialog will.
    if not (TABLES_DIR / csv_name).exists():
        st.session_state.pop("_context_dialog_csv", None)
        return
    _show_context_dialog(csv_name)


@st.dialog("Context", width="medium")
def _show_context_dialog(csv_name: str) -> None:
    """Dialog for managing a single CSV's context.

    Two independent sections:
      1. **Schema doc pointer** — pick one of the general context docs
         to claim coverage of this CSV (or `None` to clear). Writes
         into the chosen doc's `covers:` frontmatter list via
         `context_pointers.set_pointer`.
      2. **Individual context doc** — open the AI editor (existing
         flow) for `data/context/tables/<csv_stem>.md`, or just delete
         the existing doc.

    Each section commits independently so the user can set a pointer
    without touching the individual doc, or vice versa.
    """
    st.markdown(f"**`{csv_name}`**")
    st.caption(
        "Choose where this file's schema and business meaning are "
        "documented. The agent reads the schema doc to learn the file's "
        "columns and conventions, so it doesn't have to guess from the "
        "raw column names."
    )

    # --- Section 1: schema-doc pointer --------------------------------
    st.markdown("### Schema doc")
    general_docs = context_pointers.general_doc_names()
    current_pointers = context_pointers.docs_for_csv(csv_name)
    # The UI enforces one pointer per CSV. If somebody hand-edited the
    # docs to point this CSV at two group docs, surface that as the
    # current selection by preferring the first one and warning.
    current_choice = current_pointers[0] if current_pointers else None
    if len(current_pointers) > 1:
        st.warning(
            f"This file is currently listed in multiple group docs: "
            f"{', '.join(current_pointers)}. Saving below will move it to "
            "a single doc.",
            icon=":material/warning:",
        )

    if not general_docs:
        st.info(
            "No general context docs exist yet. Create one from the "
            "**Context** view first, then come back to point this file at it.",
            icon=":material/lightbulb:",
        )
    else:
        options = ["(none)"] + general_docs
        try:
            default_idx = options.index(current_choice) if current_choice else 0
        except ValueError:
            default_idx = 0
        picked = st.selectbox(
            "This file's schema is documented in:",
            options=options,
            index=default_idx,
            key=f"_ctx_dialog_pointer_{csv_name}",
            help=(
                "Pick the general context doc that explains this file's "
                "columns and meaning. The agent reads it on every "
                "conversation, so a single doc can cover many CSVs at "
                "once (e.g. one `pnl.md` describing every year of P&L)."
            ),
        )
        if st.button(
            "Save schema pointer",
            type="primary",
            icon=":material/save:",
            key=f"_ctx_dialog_save_pointer_{csv_name}",
            use_container_width=True,
        ):
            chosen = None if picked == "(none)" else picked
            try:
                context_pointers.set_pointer(csv_name, chosen)
                if chosen:
                    st.toast(f"Pointed `{csv_name}` at `{chosen}`.", icon=":material/done:")
                else:
                    st.toast(f"Cleared schema pointer for `{csv_name}`.", icon=":material/done:")
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not update pointer: {e}")
                return
            st.session_state.pop("_context_dialog_csv", None)
            st.rerun()

    st.markdown("---")

    # --- Section 2: individual context doc ----------------------------
    st.markdown("### Individual context")
    indiv_path = _context_path_for_csv(csv_name)
    indiv_exists = indiv_path.exists()
    if indiv_exists:
        st.caption(
            f"`{_rel(indiv_path)}` — a doc specific to this file. "
            "Use this for quirks that don't belong in the shared schema doc."
        )
    else:
        st.caption(
            "No individual doc yet. Most files don't need one — the "
            "schema doc above usually covers everything. Add one only "
            "for file-specific quirks (a one-off rebrand, a known bad "
            "row range, etc.)."
        )

    indiv_col1, indiv_col2 = st.columns(2)
    with indiv_col1:
        ai_label = "Edit individual doc with AI" if indiv_exists else "Create individual doc with AI"
        if st.button(
            ai_label,
            icon=":material/auto_awesome:",
            key=f"_ctx_dialog_edit_indiv_{csv_name}",
            use_container_width=True,
        ):
            st.session_state.pop("_context_dialog_csv", None)
            context_chat.open_for_existing(csv_name)
            st.rerun()
    with indiv_col2:
        if indiv_exists:
            if st.button(
                "Delete individual doc",
                icon=":material/delete:",
                key=f"_ctx_dialog_del_indiv_{csv_name}",
                use_container_width=True,
            ):
                def _do_delete(p=indiv_path):
                    try:
                        p.unlink()
                        st.toast(f"Deleted `{p.name}`.", icon=":material/done:")
                    except OSError as e:
                        st.error(f"Could not delete: {e}")

                confirm_or_run(
                    f"Delete `{indiv_path.name}`? The schema pointer above is "
                    "unaffected.",
                    "Yes, delete",
                    _do_delete,
                )

    st.markdown("---")
    if st.button(
        "Close",
        key=f"_ctx_dialog_close_{csv_name}",
        use_container_width=True,
    ):
        st.session_state.pop("_context_dialog_csv", None)
        st.rerun()


def _maybe_render_rename_dialog() -> None:
    """Open the rename dialog if a CSV has been queued for it. Same
    session_state-flag pattern as the context dialog: row button sets
    `_rename_dialog_csv`, dialog clears it on Save/Cancel.
    """
    csv_name = st.session_state.get("_rename_dialog_csv")
    if not csv_name:
        return
    if not (TABLES_DIR / csv_name).exists():
        st.session_state.pop("_rename_dialog_csv", None)
        return
    if stripe_sync.is_stripe_synced_file(csv_name):
        # Defensive — the button is hidden for Stripe rows, but if the
        # flag got set some other way, refuse rather than break the sync.
        st.session_state.pop("_rename_dialog_csv", None)
        return
    _show_rename_dialog(csv_name)


@st.dialog("Rename file", width="small")
def _show_rename_dialog(csv_name: str) -> None:
    """Rename a CSV + all the things that reference it by filename:
      - the matching per-file context doc (data/context/tables/<stem>.md)
      - schema-doc pointer entries in any general doc's `covers:` list
      - the Google Sheets sync registry's `target_filename`, if applicable
      - the currently-open file selection in session state, so the editor
        doesn't snap shut on the renamed file

    Stripe-synced files don't reach this dialog (canonical filenames).
    """
    st.markdown(f"Renaming **`{csv_name}`**")

    gs_entry = gsheets.find_entry_by_filename(csv_name)
    if gs_entry is not None:
        st.caption(
            f":material/cloud_sync: This file syncs from Google Sheets "
            f"(`{gs_entry.sheet_title}` › `{gs_entry.tab_name_at_sync}`). "
            "The sync registry will be updated so future syncs write to "
            "the new name."
        )

    state_key = f"_rename_dialog_input_{csv_name}"
    if state_key not in st.session_state:
        st.session_state[state_key] = csv_name
    new_name = st.text_input(
        "New filename",
        key=state_key,
        help="`.csv` extension added automatically if missing.",
    )

    save_col, cancel_col = st.columns(2)
    with save_col:
        if st.button(
            "Save",
            type="primary",
            icon=":material/save:",
            key=f"_rename_dialog_save_{csv_name}",
            use_container_width=True,
        ):
            _do_rename(csv_name, new_name, gs_entry, state_key)
    with cancel_col:
        if st.button(
            "Cancel",
            key=f"_rename_dialog_cancel_{csv_name}",
            use_container_width=True,
        ):
            st.session_state.pop("_rename_dialog_csv", None)
            st.session_state.pop(state_key, None)
            st.rerun()


def _do_rename(
    old_name: str,
    new_name_raw: str,
    gs_entry: gsheets.SyncEntry | None,
    state_key: str,
) -> None:
    """Apply a rename across CSV + context doc + pointers + sync registry.
    Surfaces validation errors inline in the dialog; on success closes
    the dialog with a toast.
    """
    new_name = (new_name_raw or "").strip()
    if not new_name:
        st.error("Filename can't be empty.")
        return
    if not new_name.lower().endswith(".csv"):
        new_name += ".csv"
    if new_name == old_name:
        # No-op, just close the dialog cleanly.
        st.session_state.pop("_rename_dialog_csv", None)
        st.session_state.pop(state_key, None)
        st.rerun()
        return

    old_path = TABLES_DIR / old_name
    new_path = TABLES_DIR / new_name
    if new_path.exists():
        st.error(f"`{new_name}` already exists in `data/tables/`.")
        return

    # 1. Rename the CSV.
    try:
        old_path.rename(new_path)
    except OSError as e:
        st.error(f"Could not rename CSV: {e}")
        return

    # 2. Rename the matching per-file context doc, if any. Best-effort —
    # a failure here doesn't unwind the CSV rename; we surface a warning
    # instead so the user can clean up by hand if needed.
    old_ctx = _context_path_for_csv(old_name)
    new_ctx = _context_path_for_csv(new_name)
    if old_ctx.exists():
        try:
            old_ctx.rename(new_ctx)
        except OSError as e:
            st.warning(f"Renamed CSV but couldn't rename context doc: {e}")

    # 3. Update schema-doc pointer entries in any general doc that
    # references the old filename in its `covers:` list.
    try:
        context_pointers.rename_csv(old_name, new_name)
    except Exception as e:  # noqa: BLE001
        st.warning(f"Renamed CSV but couldn't update context pointers: {e}")

    # 4. Update the Google Sheets registry's target_filename so future
    # syncs write to the new name. SyncEntry is a dataclass — mutate +
    # upsert by id.
    if gs_entry is not None:
        try:
            gs_entry.target_filename = new_name
            gsheets.upsert_entry(gs_entry)
        except Exception as e:  # noqa: BLE001
            st.warning(f"Renamed CSV but couldn't update Sheets sync registry: {e}")

    # 5. If the editor was open on this file, move the selection to the
    # new filename so it doesn't snap shut.
    for sk in ("tables_browser_choice",):
        if st.session_state.get(sk) == old_name:
            st.session_state[sk] = new_name

    st.session_state.pop("_rename_dialog_csv", None)
    st.session_state.pop(state_key, None)
    st.toast(f"Renamed to `{new_name}`.", icon=":material/done:")
    st.rerun()


def _do_gs_import(
    workbook: gsheets.WorkbookInfo,
    tab: gsheets.TabInfo,
    target_filename: str,
    use_ai: bool,
) -> None:
    """Finalise a Google Sheets import: validate filename, pull data,
    register the sync, optionally open the AI context editor."""
    import uuid as _uuid

    clean = target_filename
    if not clean:
        st.error("Filename can't be empty.")
        return
    if not clean.lower().endswith(".csv"):
        clean += ".csv"
    target_path = TABLES_DIR / clean
    if target_path.exists():
        st.error(
            f"`{clean}` already exists in `data/tables/`. Pick a different "
            "filename or delete the existing file first."
        )
        return

    with st.spinner(f"Pulling `{tab.title}` from Google Sheets…"):
        result = gsheets.sync_tab_to_csv(workbook.sheet_id, tab.title, clean)

    if not result.get("ok"):
        st.error(f"Import failed: {result.get('error')}")
        return

    entry = gsheets.SyncEntry(
        id=str(_uuid.uuid4()),
        sheet_id=workbook.sheet_id,
        sheet_title=workbook.title,
        gid=tab.gid,
        tab_name_at_sync=tab.title,
        target_filename=clean,
        last_synced_at=gsheets._now_iso(),
        last_sync_error=None,
    )
    gsheets.upsert_entry(entry)

    _clear_gs_import_state()

    if use_ai:
        # Re-use the existing post-upload context flow. The kickoff
        # message says "I just uploaded" — the model adapts fine; doesn't
        # warrant a parallel synced-specific entry point right now.
        context_chat.open_for_new_upload(clean)

    st.toast(f"Imported `{clean}` ({result.get('rows', 0)} rows).", icon=":material/cloud_done:")
    st.rerun()



"""
Context browser — top-level view for general context docs.

General context docs (`data/context/*.md` at the root) describe
company-wide facts that get loaded into the main chat's system prompt on
every turn: org structure, product portfolio, fiscal calendar, currency,
glossary, accounting conventions. They are separate from doc-related
context (which lives in `data/context/tables/` next to each CSV and is
managed from the Data view).

This module renders:
  - The folder README (collapsible).
  - A "Create new general doc" flow that asks for a filename, then opens
    the AI context-chat to draft it.
  - An "Upload a file" flow for dropping in a ready-made .md doc.
  - The list of existing general docs with View / Edit (AI) / Download /
    Delete actions, plus an "Export all" ZIP button above the list.

Doc-related docs are intentionally NOT listed here — they're accessed
via the per-CSV Context button in the Data view.
"""

import io
import zipfile
from pathlib import Path

import streamlit as st

import context_chat
import doc_import
import theme
from dialogs import confirm_or_run


DATA_DIR = Path(__file__).parent / "data"
CONTEXT_DIR = DATA_DIR / "context"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def render() -> None:
    # The AI context-chat dialog can be opened from here too; keep it
    # mounted at the top of the view so it survives reruns triggered by
    # button clicks below.
    context_chat.maybe_render()

    st.subheader("Context")
    st.caption(
        "General context docs describe company-wide facts — org structure, "
        "product portfolio, fiscal calendar, glossary — that Snoop reads on "
        "every conversation. Per-file context for a specific CSV lives in "
        "the **Data** view."
    )

    _render_folder_readme()
    _render_new_doc()
    _render_upload_doc()

    files = _list_general_docs()
    if not files:
        st.info(
            f"No general context `.md` files in `{_rel(CONTEXT_DIR)}/` yet "
            "(besides the README). Create one with the button above."
        )
        return

    # Toolbar row: doc count on the left, Export all on the right.
    count_col, export_col = st.columns([6, 2])
    with count_col:
        st.markdown(f"**{len(files)} doc{'s' if len(files) != 1 else ''}**")
    with export_col:
        st.download_button(
            "Export all",
            data=_build_zip(files),
            file_name="context_docs.zip",
            mime="application/zip",
            icon=":material/download:",
            key="ctx_export_all",
            use_container_width=True,
            help="Download all general context docs as a ZIP archive.",
        )

    chosen_name = _render_file_list(files)

    if not chosen_name:
        return

    _render_doc_view(CONTEXT_DIR / chosen_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path(__file__).parent))
    except ValueError:
        return str(path)


def _list_general_docs() -> list[Path]:
    """List general docs (root `.md` files, README excluded). The
    `tables/` subdirectory is skipped — those are doc-related, not
    general."""
    if not CONTEXT_DIR.exists():
        return []
    return sorted(
        p for p in CONTEXT_DIR.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".md"
        and p.name.lower() != "readme.md"
    )


def _build_zip(files: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return buf.getvalue()


def _render_folder_readme() -> None:
    readme_path = CONTEXT_DIR / "README.md"
    if not readme_path.exists():
        return
    try:
        content = readme_path.read_text(encoding="utf-8")
    except OSError:
        return
    with st.expander(
        f"README — conventions for `{_rel(CONTEXT_DIR)}/`",
        expanded=False,
        icon=theme.ICON["readme"],
    ):
        st.markdown(content)


def _render_new_doc() -> None:
    """Form for creating a new general doc. Asks for the filename, then
    hands off to the AI context-chat which prompts the user for the
    doc's purpose and drafts a first version."""
    with st.expander("Create a new general context doc", icon=":material/note_add:"):
        with st.form("_new_general_doc_form", clear_on_submit=True):
            name = st.text_input(
                "Filename",
                placeholder="e.g. company, glossary, accounting_conventions",
                help=(
                    "Lowercase, no spaces. `.md` extension is added automatically."
                ),
            )
            submitted = st.form_submit_button(
                "Create with AI",
                type="primary",
                icon=":material/auto_awesome:",
            )

        if submitted:
            clean = name.strip().lower().replace(" ", "_")
            if not clean:
                st.error("Please enter a filename.")
                return
            if not clean.endswith(".md"):
                clean += ".md"
            target = CONTEXT_DIR / clean
            if target.exists():
                st.error(
                    f"`{clean}` already exists. Open it via the list below "
                    "to edit, or pick a different name."
                )
                return
            context_chat.open_for_general_new(clean)
            st.rerun()


def _render_upload_doc() -> None:
    """Import a document as a general context doc.

    Accepts:
      .md   — saved as-is.
      .pdf  — text extracted with pypdf, saved as .md.
      .docx — text extracted with python-docx, saved as .md.
      .txt  — decoded and saved as .md.

    For non-markdown formats the user can preview the extracted text,
    rename the target file, and save it to `data/context/`.
    """
    with st.expander(
        "Import a context doc (.md, .pdf, .docx, .txt)",
        icon=":material/upload_file:",
    ):
        st.caption(
            "Drop in a Markdown file you've written, or import a PDF, "
            "Word doc, or plain-text file — Snoop extracts the text and "
            "saves it as a context doc the AI reads on every conversation."
        )
        uploaded = st.file_uploader(
            "Choose a file",
            type=["md", "pdf", "docx", "txt"],
            key="ctx_upload_file",
            label_visibility="collapsed",
        )

        if not uploaded:
            return

        ext = Path(uploaded.name).suffix.lower()
        file_bytes = uploaded.getvalue()

        if ext == ".md":
            _render_md_upload(uploaded.name, file_bytes)
        else:
            _render_doc_text_import(uploaded.name, ext, file_bytes)


def _render_md_upload(filename: str, file_bytes: bytes) -> None:
    """Existing flow: save a .md file directly."""
    if filename.lower() == "readme.md":
        st.error("`README.md` is reserved — choose a different filename.")
        return
    overwrite = st.checkbox(
        "Overwrite if a file with this name already exists",
        key="ctx_upload_overwrite",
    )
    target = CONTEXT_DIR / filename
    conflict = target.exists() and not overwrite
    if conflict:
        st.warning(f"`{filename}` already exists. Enable **Overwrite** to replace it.")
    if st.button(
        f"Save `{filename}`",
        icon=theme.ICON["save"],
        key="ctx_upload_save",
        type="primary",
        disabled=conflict,
    ):
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(file_bytes)
            st.toast(f"Uploaded `{filename}`.", icon=":material/check_circle:")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not save: {e}")


def _render_doc_text_import(filename: str, ext: str, file_bytes: bytes) -> None:
    """Extract text from a PDF / DOCX / TXT and save as a context doc."""
    # Extract text — do it once and cache in session_state keyed by filename.
    cache_key = f"_ctx_import_text_{filename}"
    if cache_key not in st.session_state:
        try:
            with st.spinner(f"Extracting text from `{filename}`…"):
                if ext == ".pdf":
                    text = doc_import.extract_pdf_text(file_bytes)
                elif ext == ".docx":
                    text = doc_import.extract_docx_text(file_bytes)
                else:  # .txt
                    text = doc_import.extract_txt_text(file_bytes)
            st.session_state[cache_key] = text
        except (RuntimeError, ValueError) as e:
            st.error(str(e))
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not extract text: {e}")
            return

    text: str = st.session_state[cache_key]
    if not text.strip():
        st.warning("No text could be extracted from this document.")
        return

    st.success(f"Extracted {len(text):,} characters from `{filename}`.")
    with st.expander("Preview extracted text", expanded=False):
        preview = text[:3000] + ("…" if len(text) > 3000 else "")
        st.text(preview)

    stem = Path(filename).stem.lower().replace(" ", "_")
    default_md_name = f"{stem}.md"

    save_name = st.text_input(
        "Save as",
        value=default_md_name,
        key="ctx_doc_import_name",
        help="Saved into `data/context/` as a Markdown file.",
    )
    overwrite = st.checkbox(
        "Overwrite if a file with this name already exists",
        key="ctx_doc_import_overwrite",
    )

    clean_name = (save_name or "").strip()
    if clean_name and not clean_name.endswith(".md"):
        clean_name += ".md"

    conflict = bool(clean_name) and (CONTEXT_DIR / clean_name).exists() and not overwrite
    if conflict:
        st.warning(f"`{clean_name}` already exists. Enable **Overwrite** to replace it.")

    if st.button(
        "Save as context doc",
        icon=theme.ICON["save"],
        key="ctx_doc_import_save",
        type="primary",
        disabled=not clean_name or conflict,
    ):
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            (CONTEXT_DIR / clean_name).write_text(text, encoding="utf-8")
            # Drop the cached text so a new upload of the same filename starts fresh.
            st.session_state.pop(cache_key, None)
            st.toast(
                f"Saved `{clean_name}` ({len(text):,} chars).",
                icon=":material/check_circle:",
            )
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not save: {e}")


def _render_file_list(files: list[Path]) -> str | None:
    """Bordered rows with View / Edit (AI) / Download / Delete actions.
    Returns the currently-open filename, or None."""
    selection_key = "context_browser_choice"
    file_names = [f.name for f in files]
    current = st.session_state.get(selection_key)
    if current and current not in file_names:
        st.session_state.pop(selection_key, None)
        current = None

    for f in files:
        is_active = current == f.name
        with st.container(border=True):
            name_col, view_col, edit_col, dl_col, del_col = st.columns(
                [8, 0.7, 0.7, 0.7, 0.7]
            )

            with name_col:
                st.markdown(f"**{f.name}**")

            with view_col:
                if st.button(
                    "",
                    icon=":material/visibility_off:" if is_active else ":material/visibility:",
                    help="Close" if is_active else "View",
                    key=f"ctx_view_{f.name}",
                    use_container_width=True,
                    type="primary" if is_active else "secondary",
                ):
                    if is_active:
                        st.session_state.pop(selection_key, None)
                    else:
                        st.session_state[selection_key] = f.name
                    st.rerun()

            with edit_col:
                if st.button(
                    "",
                    icon=":material/auto_awesome:",
                    help="Edit with AI",
                    key=f"ctx_edit_{f.name}",
                    use_container_width=True,
                ):
                    context_chat.open_for_general_existing(f.name)
                    st.rerun()

            with dl_col:
                try:
                    file_bytes = f.read_bytes()
                except OSError:
                    file_bytes = b""
                st.download_button(
                    "",
                    data=file_bytes,
                    file_name=f.name,
                    mime="text/markdown",
                    icon=":material/download:",
                    help="Download",
                    key=f"ctx_dl_{f.name}",
                    use_container_width=True,
                )

            with del_col:
                if st.button(
                    "",
                    icon=":material/delete:",
                    help="Delete",
                    key=f"ctx_del_{f.name}",
                    use_container_width=True,
                ):
                    def _do_delete(path=f):
                        try:
                            path.unlink()
                            if st.session_state.get(selection_key) == path.name:
                                st.session_state.pop(selection_key, None)
                        except OSError as e:
                            st.error(f"Could not delete: {e}")

                    confirm_or_run(
                        f"Delete `{f.name}`? This can't be undone.",
                        "Yes, delete",
                        _do_delete,
                    )

    return st.session_state.get(selection_key)


def _render_doc_view(path: Path) -> None:
    """Markdown viewer + plain-text editor for a single general doc. The
    AI-assisted flow lives behind the Edit button; this is the manual
    fallback / read-only view for users who'd rather edit directly.
    """
    st.divider()
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read `{path.name}`: {e}")
        return

    st.caption(f"`{_rel(path)}` · {len(content):,} characters")

    mode = st.radio(
        "Mode",
        options=["View", "Edit"],
        horizontal=True,
        key=f"ctx_mode_{path.name}",
    )

    if mode == "View":
        st.markdown(content)
        return

    edited = st.text_area(
        "Markdown",
        value=content,
        height=500,
        key=f"ctx_editor_{path.name}",
        label_visibility="collapsed",
    )

    save_col, _ = st.columns([1, 3])
    with save_col:
        if st.button(
            "Save changes",
            icon=theme.ICON["save"],
            key=f"ctx_save_{path.name}",
            use_container_width=True,
        ):
            try:
                path.write_text(edited, encoding="utf-8")
                st.success(f"Saved `{path.name}`.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not save: {e}")

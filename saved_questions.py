"""
Saved Questions view: list, add, edit, delete the user's pinned prompts.

Storage lives in `~/.snoop-doc/saved_questions.json` and is managed by the
top-level `app.py` (it owns the load/save functions and the session_state
entry). This module just renders the UI and pushes changes back via the
caller-supplied `persist` callable.
"""

import uuid

import streamlit as st

import theme
from dialogs import confirm_or_run


def render(persist) -> None:
    """Render the Saved Questions management page.

    Parameters
    ----------
    persist : callable
        No-arg function that writes `st.session_state.saved_questions` to disk.
        Called after every mutation so the list survives across sessions.
    """
    st.subheader("Saved Questions")
    st.caption(
        "Pin questions you ask often. They show up as a dropdown below the "
        "chat input — one click to re-run."
    )

    questions: list[dict] = st.session_state.get("saved_questions", [])

    # ---- Add form -------------------------------------------------------
    with st.expander("Add a new saved question", icon=":material/add:", expanded=not questions):
        new_text = st.text_area(
            "Question",
            key="_new_saved_q_text",
            placeholder="e.g. Show me revenue by branch for the last quarter",
            height=80,
        )
        if st.button(
            "Add",
            type="primary",
            icon=":material/add:",
            key="_new_saved_q_add",
        ):
            text = (new_text or "").strip()
            if text:
                questions.append({"id": str(uuid.uuid4()), "text": text})
                st.session_state.saved_questions = questions
                persist()
                # Streamlit doesn't allow writing to a widget's own key after
                # the widget was created, so we reset on the NEXT run via a
                # counter-based key in the widget definition above (kept
                # simple here: pop forces a fresh widget instance).
                st.session_state.pop("_new_saved_q_text", None)
                st.rerun()
            else:
                st.warning("Question can't be empty.")

    if not questions:
        return

    st.divider()

    # ---- Existing list --------------------------------------------------
    for i, q in enumerate(questions):
        qid = q["id"]
        editing = bool(st.session_state.get(f"_editing_{qid}"))

        with st.container(border=True):
            if editing:
                edit_text = st.text_area(
                    "Edit question",
                    value=q["text"],
                    key=f"_edit_text_{qid}",
                    height=80,
                    label_visibility="collapsed",
                )
                col_save, col_cancel, _spacer = st.columns([1, 1, 3])
                with col_save:
                    if st.button(
                        "Save",
                        type="primary",
                        icon=":material/save:",
                        key=f"_save_edit_{qid}",
                        use_container_width=True,
                    ):
                        new = (edit_text or "").strip()
                        if new:
                            questions[i] = {**q, "text": new}
                            st.session_state.saved_questions = questions
                            persist()
                        st.session_state.pop(f"_editing_{qid}", None)
                        st.session_state.pop(f"_edit_text_{qid}", None)
                        st.rerun()
                with col_cancel:
                    if st.button(
                        "Cancel",
                        key=f"_cancel_edit_{qid}",
                        use_container_width=True,
                    ):
                        st.session_state.pop(f"_editing_{qid}", None)
                        st.session_state.pop(f"_edit_text_{qid}", None)
                        st.rerun()
            else:
                text_col, edit_col, del_col = st.columns([10, 0.7, 0.7])
                with text_col:
                    st.markdown(q["text"])
                with edit_col:
                    if st.button(
                        "",
                        icon=":material/edit:",
                        key=f"_edit_btn_{qid}",
                        help="Edit",
                        use_container_width=True,
                    ):
                        st.session_state[f"_editing_{qid}"] = True
                        st.rerun()
                with del_col:
                    if st.button(
                        "",
                        icon=":material/delete:",
                        key=f"_del_btn_{qid}",
                        help="Delete",
                        use_container_width=True,
                    ):
                        def _do_delete(qid=qid):
                            st.session_state.saved_questions = [
                                x for x in st.session_state.saved_questions
                                if x["id"] != qid
                            ]
                            persist()

                        confirm_or_run(
                            "Delete this saved question?",
                            "Yes, delete",
                            _do_delete,
                        )


def render_dropdown(set_pending_prompt) -> None:
    """Render the 'pick a saved question' dropdown for the chat view.

    Selecting a value submits that question as if the user typed it.

    Parameters
    ----------
    set_pending_prompt : callable
        Function taking the chosen question text and queuing it for the
        agent loop (mirrors how `chat_input` works).
    """
    questions = st.session_state.get("saved_questions", [])
    if not questions:
        return

    # Counter-based key so each rerun gets a fresh selectbox instance; that's
    # how we "reset" the dropdown's value after a selection without fighting
    # Streamlit's widget state rules.
    counter = st.session_state.get("_saved_q_dropdown_counter", 0)
    placeholder = "Choose a saved question…"
    options = [placeholder, *[q["text"] for q in questions]]

    selected = st.selectbox(
        "Saved questions",
        options=options,
        index=0,
        key=f"_saved_q_dropdown_{counter}",
        label_visibility="collapsed",
    )
    if selected and selected != placeholder:
        st.session_state["_saved_q_dropdown_counter"] = counter + 1
        set_pending_prompt(selected)
        st.rerun()

"""
Reusable Streamlit dialog helpers.

Lives in its own module so views (data_browser, saved_questions) can import
the confirmation helper without doing `from app import ...`. Importing from
the main script re-executes it, which re-renders every sidebar widget and
crashes with a duplicate-key error.
"""

import streamlit as st


def confirmations_enabled() -> bool:
    """Reads `st.session_state.confirm_destructive`, defaults to True."""
    return bool(st.session_state.get("confirm_destructive", True))


@st.dialog("Confirm")
def _show_confirm_dialog(message: str, action_label: str, on_confirm) -> None:
    """Generic confirmation modal. Closure captures the action to run on Yes."""
    st.write(message)
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(action_label, type="primary", use_container_width=True):
            on_confirm()
            st.rerun()
    with col_b:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


def confirm_or_run(message: str, action_label: str, on_confirm) -> None:
    """Show a confirmation dialog if the setting is on, else run action directly."""
    if confirmations_enabled():
        _show_confirm_dialog(message, action_label, on_confirm)
    else:
        on_confirm()
        st.rerun()

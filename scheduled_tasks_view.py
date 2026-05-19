"""
Scheduled Tasks view for the Snoop Doc Streamlit app.

Lets users create, edit, toggle, and delete scheduled analysis tasks.
The actual execution is handled by the companion scheduler.py process.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

import task_store
import theme


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
@st.dialog("Add Scheduled Task", width="large")
def _show_add_dialog() -> None:
    _render_task_form(existing=None)


@st.dialog("Edit Scheduled Task", width="large")
def _show_edit_dialog(task: dict) -> None:
    _render_task_form(existing=task)


def _render_task_form(existing: dict | None) -> None:
    """Shared form used by both Add and Edit dialogs."""
    is_edit = existing is not None

    name = st.text_input(
        "Task name",
        value=existing["name"] if is_edit else "",
        placeholder="Monthly P&L Summary",
    )
    question = st.text_area(
        "Question / prompt",
        value=existing["question"] if is_edit else "",
        placeholder="Generate a full P&L summary for last month, including MRR, churn, and top 5 revenue segments.",
        height=100,
    )

    st.markdown("**Schedule**")
    col_type, col_detail, col_time = st.columns([1, 1, 1])

    with col_type:
        stype = st.selectbox(
            "Frequency",
            ["monthly", "weekly", "daily"],
            index=["monthly", "weekly", "daily"].index(
                existing.get("schedule_type", "monthly") if is_edit else "monthly"
            ),
            label_visibility="collapsed",
        )

    with col_detail:
        if stype == "monthly":
            day_of_month = st.number_input(
                "Day of month",
                min_value=1, max_value=28,
                value=existing.get("day_of_month", 1) if is_edit else 1,
            )
            day_of_week = existing.get("day_of_week", "mon") if is_edit else "mon"
        elif stype == "weekly":
            dow_options = task_store.DAYS_OF_WEEK
            dow_labels = [task_store.DAY_LABELS[d] for d in dow_options]
            current_dow = existing.get("day_of_week", "mon") if is_edit else "mon"
            dow_idx = dow_options.index(current_dow) if current_dow in dow_options else 0
            selected_label = st.selectbox(
                "Day of week",
                dow_labels,
                index=dow_idx,
                label_visibility="collapsed",
                key="_sched_dow",
            )
            day_of_week = dow_options[dow_labels.index(selected_label)]
            day_of_month = existing.get("day_of_month", 1) if is_edit else 1
        else:
            st.write("")  # spacer
            day_of_month = existing.get("day_of_month", 1) if is_edit else 1
            day_of_week = existing.get("day_of_week", "mon") if is_edit else "mon"

    with col_time:
        default_h = existing.get("hour", 9) if is_edit else 9
        default_m = existing.get("minute", 0) if is_edit else 0
        time_val = st.time_input(
            "Time (local)",
            value=datetime.now().replace(hour=default_h, minute=default_m, second=0, microsecond=0).time(),
            label_visibility="collapsed",
        )

    st.markdown("**Output destinations**")
    col_save, col_slack = st.columns(2)
    with col_save:
        output_save = st.checkbox(
            "Save to Saved Items",
            value=existing.get("output_save", True) if is_edit else True,
        )
    with col_slack:
        output_slack = st.checkbox(
            "Post to Slack",
            value=existing.get("output_slack", False) if is_edit else False,
        )

    slack_channel = ""
    if output_slack:
        slack_channel = st.text_input(
            "Slack channel",
            value=existing.get("slack_channel", "") if is_edit else "",
            placeholder="#general",
        )

    st.markdown("**Status**")
    enabled = st.toggle(
        "Enabled",
        value=existing.get("enabled", True) if is_edit else True,
    )

    st.divider()
    col_save_btn, col_cancel = st.columns([1, 1])

    with col_save_btn:
        label = "Save changes" if is_edit else "Create task"
        if st.button(label, type="primary", use_container_width=True):
            if not name.strip():
                st.error("Task name is required.")
                return
            if not question.strip():
                st.error("Question is required.")
                return

            kwargs = dict(
                name=name,
                question=question,
                schedule_type=stype,
                hour=time_val.hour,
                minute=time_val.minute,
                day_of_month=day_of_month,
                day_of_week=day_of_week,
                enabled=enabled,
                output_save=output_save,
                output_slack=output_slack,
                slack_channel=slack_channel,
            )
            if is_edit:
                task_store.update_task(existing["id"], **{k: v for k, v in kwargs.items()})
                st.success("Task updated.")
            else:
                task_store.create_task(**kwargs)
                st.success("Task created.")
            st.rerun()

    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# ---------------------------------------------------------------------------
# Status formatting helpers
# ---------------------------------------------------------------------------
def _status_badge(task: dict) -> str:
    if not task.get("enabled", True):
        return ":material/pause_circle: **Paused**"
    status = task.get("last_status")
    if status is None:
        return ":material/schedule: **Pending first run**"
    if status == "ok":
        return ":material/check_circle: **Last run: OK**"
    return f":material/error: **Last run: Error**"


def _last_run_str(task: dict) -> str:
    lr = task.get("last_run")
    if not lr:
        return "Never"
    try:
        dt = datetime.fromisoformat(lr.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return lr


def _run_now(task: dict) -> None:
    """Trigger a task immediately from the UI (runs synchronously in Streamlit)."""
    import snoop_agent
    settings = snoop_agent.load_settings()
    if not settings.get("api_key"):
        st.error("No Anthropic API key configured. Go to Settings to add one.")
        return

    with st.spinner(f"Running '{task['name']}'…"):
        result = snoop_agent.run_question(
            question=task["question"],
            api_key=settings["api_key"],
            model=settings.get("model", snoop_agent.DEFAULT_MODEL),
            table_scope=task.get("table_scope"),
        )

    if result.error:
        task_store.update_task(task["id"], last_status=f"error: {result.error}",
                               last_run=datetime.now(timezone.utc).isoformat())
        st.error(f"Error: {result.error}")
        return

    now_iso = datetime.now(timezone.utc).isoformat()

    # Save to Saved Items
    if task.get("output_save", True):
        import reports
        import saved_items
        html_str = reports.render_exchange_to_html(
            question=task["question"],
            assistant_blocks=[{"type": "text", "text": result.text}],
            figures={str(i): fig for i, fig in enumerate(result.figures)},
            tables={str(i): df for i, df in enumerate(result.tables)},
        )
        saved_items.save_report(task["name"], html_str, replace_existing=False)

    # Post to Slack
    if task.get("output_slack") and task.get("slack_channel"):
        _post_to_slack(task, result, settings)

    task_store.update_task(task["id"], last_status="ok", last_run=now_iso)
    st.success(f"Task ran successfully. Result saved to Saved Items.")
    st.rerun()


def _post_to_slack(task: dict, result, settings: dict) -> None:
    """Best-effort Slack post; silently swallows errors (already saved to Items)."""
    try:
        from slack_sdk import WebClient  # type: ignore
        token = settings.get("slack_bot_token", "")
        if not token:
            return
        client = WebClient(token=token)
        text = f"*{task['name']}*\n\n{result.text[:2900]}"
        if result.confidence:
            text += f"\n\n_{result.confidence}_"
        client.chat_postMessage(channel=task["slack_channel"], text=text)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render() -> None:
    st.title(":material/schedule: Scheduled Tasks")
    st.caption(
        "Create recurring analysis tasks. "
        "Run `python scheduler.py` to execute them automatically on schedule."
    )

    col_header, col_add = st.columns([4, 1])
    with col_add:
        if st.button(":material/add: New Task", type="primary", use_container_width=True):
            _show_add_dialog()

    tasks = task_store.list_tasks()

    if not tasks:
        st.info(
            "No scheduled tasks yet. Click **New Task** to create one — "
            "for example, a monthly P&L summary on the 1st of each month.",
            icon=":material/schedule:",
        )
        return

    for task in tasks:
        _render_task_card(task)


def _render_task_card(task: dict) -> None:
    tid = task["id"]

    with st.container(border=True):
        col_info, col_actions = st.columns([4, 2])

        with col_info:
            # Name + enabled indicator
            enabled = task.get("enabled", True)
            icon = ":material/play_circle:" if enabled else ":material/pause_circle:"
            st.markdown(f"#### {icon} {task['name']}")

            # Schedule description
            st.markdown(
                f":material/event_repeat: `{task_store.describe_schedule(task)}`  "
                f"&nbsp;&nbsp; :material/last_page: Last run: {_last_run_str(task)}"
            )

            # Status
            err = task.get("last_status", "")
            if err and err.startswith("error:"):
                st.caption(f":material/error: {err}")

            # Question preview
            q = task.get("question", "")
            preview = q if len(q) <= 120 else q[:117] + "…"
            st.caption(f'"{preview}"')

            # Output chips
            chips: list[str] = []
            if task.get("output_save"):
                chips.append(":material/inventory_2: Saved Items")
            if task.get("output_slack") and task.get("slack_channel"):
                chips.append(f":material/tag: {task['slack_channel']}")
            if chips:
                st.caption("  ·  ".join(chips))

        with col_actions:
            # Toggle enabled / disabled
            new_state = st.toggle(
                "Enabled",
                value=enabled,
                key=f"toggle_{tid}",
            )
            if new_state != enabled:
                task_store.toggle_enabled(tid)
                st.rerun()

            btn_col1, btn_col2, btn_col3 = st.columns(3)
            with btn_col1:
                if st.button(
                    ":material/play_arrow:",
                    key=f"run_{tid}",
                    help="Run now",
                    use_container_width=True,
                ):
                    _run_now(task)
            with btn_col2:
                if st.button(
                    ":material/edit:",
                    key=f"edit_{tid}",
                    help="Edit",
                    use_container_width=True,
                ):
                    _show_edit_dialog(task)
            with btn_col3:
                if st.button(
                    ":material/delete:",
                    key=f"del_{tid}",
                    help="Delete",
                    use_container_width=True,
                ):
                    task_store.delete_task(tid)
                    st.rerun()

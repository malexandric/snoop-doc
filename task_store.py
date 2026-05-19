"""
Persistent storage for Snoop scheduled tasks.

Tasks are stored in ~/.snoop-doc/scheduled_tasks.json as a list of dicts.
All mutations go through this module so both the Streamlit UI and the
scheduler daemon read/write the same format.

Task schema
-----------
{
    "id":            str (uuid4),
    "name":          str,
    "question":      str,
    "enabled":       bool,
    "schedule_type": "monthly" | "weekly" | "daily",
    "day_of_month":  int (1-28)  — only for monthly,
    "day_of_week":   str         — "mon"…"sun", only for weekly,
    "hour":          int (0-23),
    "minute":        int (0-59),
    "output_save":   bool,       — save result to Saved Items,
    "output_slack":  bool,       — post to Slack channel,
    "slack_channel": str,        — e.g. "#general",
    "table_scope":   list[str] | None,
    "last_run":      str | None, — ISO 8601 UTC timestamp,
    "last_status":   str | None, — "ok" or "error: <msg>",
}
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

TASKS_PATH = Path.home() / ".snoop-doc" / "scheduled_tasks.json"

DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def _load_raw() -> list[dict]:
    if not TASKS_PATH.exists():
        return []
    try:
        return json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(tasks: list[dict]) -> None:
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_tasks() -> list[dict]:
    """Return all tasks, most-recently-created first."""
    return list(reversed(_load_raw()))


def get_task(task_id: str) -> dict | None:
    for t in _load_raw():
        if t.get("id") == task_id:
            return t
    return None


def create_task(
    name: str,
    question: str,
    schedule_type: str,
    hour: int,
    minute: int,
    *,
    day_of_month: int = 1,
    day_of_week: str = "mon",
    enabled: bool = True,
    output_save: bool = True,
    output_slack: bool = False,
    slack_channel: str = "",
    table_scope: list[str] | None = None,
) -> dict:
    task: dict = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "question": question.strip(),
        "enabled": enabled,
        "schedule_type": schedule_type,
        "day_of_month": day_of_month,
        "day_of_week": day_of_week,
        "hour": hour,
        "minute": minute,
        "output_save": output_save,
        "output_slack": output_slack,
        "slack_channel": slack_channel.strip(),
        "table_scope": table_scope,
        "last_run": None,
        "last_status": None,
    }
    tasks = _load_raw()
    tasks.append(task)
    _save_raw(tasks)
    return task


def update_task(task_id: str, **fields) -> bool:
    """Update specific fields on a task. Returns True if found."""
    tasks = _load_raw()
    for t in tasks:
        if t.get("id") == task_id:
            t.update(fields)
            _save_raw(tasks)
            return True
    return False


def delete_task(task_id: str) -> bool:
    """Delete a task by id. Returns True if found."""
    tasks = _load_raw()
    new = [t for t in tasks if t.get("id") != task_id]
    if len(new) == len(tasks):
        return False
    _save_raw(new)
    return True


def toggle_enabled(task_id: str) -> bool | None:
    """Flip enabled flag. Returns new state or None if not found."""
    task = get_task(task_id)
    if task is None:
        return None
    new_state = not task.get("enabled", True)
    update_task(task_id, enabled=new_state)
    return new_state


# ---------------------------------------------------------------------------
# Human-readable schedule description
# ---------------------------------------------------------------------------
def describe_schedule(task: dict) -> str:
    stype = task.get("schedule_type", "daily")
    h = task.get("hour", 9)
    m = task.get("minute", 0)
    time_str = f"{h:02d}:{m:02d}"

    if stype == "monthly":
        d = task.get("day_of_month", 1)
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(d if d <= 20 else d % 10, "th")
        return f"Monthly · {d}{suffix} at {time_str}"
    if stype == "weekly":
        day = DAY_LABELS.get(task.get("day_of_week", "mon"), "Monday")
        return f"Weekly · {day} at {time_str}"
    return f"Daily · {time_str}"

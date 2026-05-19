"""
Snoop Doc Scheduler — runs scheduled analysis tasks in the background.

Reads task definitions from ~/.snoop-doc/scheduled_tasks.json (managed by
the Snoop web app) and fires them on their configured cron schedule using
APScheduler. Results are saved to Saved Items and/or posted to Slack,
exactly as the "Run Now" button in the UI does.

Usage
-----
    python scheduler.py

Runs forever until Ctrl-C. Keep it running alongside the Streamlit app and
the Slack bot using a process manager (systemd, supervisor, screen, etc.).

The scheduler hot-reloads task definitions every minute, so changes made in
the Snoop UI take effect without restarting.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("snoop_scheduler")


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    print("ERROR: APScheduler is not installed. Run: pip install 'APScheduler>=3.10'")
    sys.exit(1)

import snoop_agent
import task_store


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------
def _run_task(task_id: str) -> None:
    """Execute one scheduled task — called by APScheduler on each trigger."""
    task = task_store.get_task(task_id)
    if task is None:
        logger.warning("Task %s no longer exists — skipping", task_id)
        return
    if not task.get("enabled", True):
        logger.info("Task '%s' is disabled — skipping", task["name"])
        return

    logger.info("Running task '%s' (id=%s)", task["name"], task_id)

    settings = snoop_agent.load_settings()
    if not settings.get("api_key"):
        logger.error("No Anthropic API key — cannot run task '%s'", task["name"])
        task_store.update_task(
            task_id,
            last_status="error: No Anthropic API key configured",
            last_run=datetime.now(timezone.utc).isoformat(),
        )
        return

    result = snoop_agent.run_question(
        question=task["question"],
        api_key=settings["api_key"],
        model=settings.get("model", snoop_agent.DEFAULT_MODEL),
        table_scope=task.get("table_scope"),
        on_progress=lambda msg: logger.debug("  %s", msg),
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    if result.error:
        logger.error("Task '%s' failed: %s", task["name"], result.error)
        task_store.update_task(task_id, last_status=f"error: {result.error}", last_run=now_iso)
        return

    logger.info(
        "Task '%s' completed in %.1fs (%d iterations)",
        task["name"], result.duration, result.iterations,
    )

    # Save to Saved Items
    if task.get("output_save", True):
        _save_to_items(task, result)

    # Post to Slack
    if task.get("output_slack") and task.get("slack_channel"):
        _post_to_slack(task, result, settings)

    task_store.update_task(task_id, last_status="ok", last_run=now_iso)


def _save_to_items(task: dict, result: snoop_agent.AgentResult) -> None:
    try:
        import reports
        import saved_items
        html_str = reports.render_exchange_to_html(
            question=task["question"],
            assistant_blocks=[{"type": "text", "text": result.text}],
            figures={str(i): fig for i, fig in enumerate(result.figures)},
            tables={str(i): df for i, df in enumerate(result.tables)},
        )
        entry_id = saved_items.save_report(task["name"], html_str, replace_existing=False)
        logger.info("  Saved report to Saved Items (id=%s)", entry_id)
    except Exception as e:  # noqa: BLE001
        logger.error("  Failed to save to Saved Items: %s", e)


def _post_to_slack(task: dict, result: snoop_agent.AgentResult, settings: dict) -> None:
    try:
        from slack_sdk import WebClient  # type: ignore
        token = settings.get("slack_bot_token", "")
        if not token:
            logger.warning("  Slack output requested but no bot token configured")
            return
        client = WebClient(token=token)
        text = f"*{task['name']}*\n\n{result.text[:2900]}"
        if result.confidence:
            text += f"\n\n_{result.confidence}_"
        client.chat_postMessage(channel=task["slack_channel"], text=text)
        logger.info("  Posted to Slack channel %s", task["slack_channel"])
    except Exception as e:  # noqa: BLE001
        logger.error("  Failed to post to Slack: %s", e)


# ---------------------------------------------------------------------------
# Scheduler management
# ---------------------------------------------------------------------------
def _cron_kwargs(task: dict) -> dict:
    """Build APScheduler CronTrigger kwargs for a task."""
    stype = task.get("schedule_type", "daily")
    base = {"hour": task.get("hour", 9), "minute": task.get("minute", 0)}
    if stype == "monthly":
        return {**base, "day": task.get("day_of_month", 1)}
    if stype == "weekly":
        return {**base, "day_of_week": task.get("day_of_week", "mon")}
    return base  # daily


def _sync_jobs(scheduler: BlockingScheduler, known_ids: set[str]) -> set[str]:
    """Reload tasks and reconcile APScheduler jobs. Returns current task id set."""
    tasks = task_store._load_raw()
    current_ids = {t["id"] for t in tasks}

    # Remove jobs for deleted tasks
    for removed_id in known_ids - current_ids:
        try:
            scheduler.remove_job(removed_id)
            logger.info("Removed job for deleted task %s", removed_id)
        except Exception:  # noqa: BLE001
            pass

    # Add or update jobs
    for task in tasks:
        tid = task["id"]
        try:
            existing_job = scheduler.get_job(tid)
            new_trigger = CronTrigger(**_cron_kwargs(task))

            if existing_job is None:
                scheduler.add_job(
                    _run_task,
                    trigger=new_trigger,
                    id=tid,
                    args=[tid],
                    name=task["name"],
                    replace_existing=True,
                )
                logger.info("Scheduled '%s' → %s", task["name"], task_store.describe_schedule(task))
            else:
                # Reschedule if cron params changed
                existing_job.reschedule(trigger=new_trigger)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to schedule task '%s': %s", task.get("name"), e)

    return current_ids


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("Snoop Scheduler starting…")

    settings = snoop_agent.load_settings()
    if not settings.get("api_key"):
        logger.warning(
            "No Anthropic API key found. Tasks will fail until one is configured "
            "in Snoop → Settings or via ANTHROPIC_API_KEY env var."
        )

    scheduler = BlockingScheduler(timezone="UTC")

    # Sync tasks on startup
    known_ids: set[str] = set()
    known_ids = _sync_jobs(scheduler, known_ids)

    # Hot-reload: re-sync every minute so UI changes take effect quickly
    scheduler.add_job(
        lambda: globals().update(
            {"_known_ids": _sync_jobs(scheduler, globals().get("_known_ids", known_ids))}
        ),
        trigger=CronTrigger(minute="*"),
        id="_reload",
        name="Task hot-reload",
    )

    task_count = len(task_store._load_raw())
    logger.info(
        "Scheduler running with %d task(s). Press Ctrl-C to stop.", task_count
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Snoop Scheduler stopped.")


if __name__ == "__main__":
    main()

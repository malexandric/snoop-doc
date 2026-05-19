"""
Snoop Doc Slack Bot.

Listens for @snoop mentions in any channel (and DMs) and answers
questions about your data using the same Claude-powered agent as
the Snoop web app.  Runs as a long-lived process alongside the
Streamlit app — both share the same data/ and context/ directories.

Features
--------
- @snoop  what was our MRR in Q4 2024?       → single-turn answer
- Thread replies also mention @snoop          → multi-turn context carried
- Tables formatted as code-block grids
- Charts noted with a link back to the web app
- Confidence level appended as a context footer

Setup
-----
See SETUP_SLACK.md for the full step-by-step guide.

Quick start:
    1. Create a Slack app at https://api.slack.com/apps
    2. Enable Socket Mode → generate an App-Level Token (xapp-)
    3. Event Subscriptions → Subscribe to: app_mention, message.im
    4. OAuth Scopes (Bot Token): app_mentions:read, channels:history,
       chat:write, groups:history, im:history, im:read, im:write
    5. Install app → copy Bot Token (xoxb-)
    6. Store in Settings → Integrations → Slack (or set env vars)
    7. Run:  python slack_bot.py

Tokens can be set as:
    SLACK_BOT_TOKEN   xoxb-…
    SLACK_APP_TOKEN   xapp-…
    ANTHROPIC_API_KEY sk-ant-…
Or stored in ~/.snoop-doc/settings.json via the Snoop web app.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import textwrap
import threading
from pathlib import Path

import snoop_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("snoop_slack")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    settings = snoop_agent.load_settings()
    return {
        "bot_token":  settings.get("slack_bot_token", ""),
        "app_token":  settings.get("slack_app_token", ""),
        "api_key":    settings.get("api_key", ""),
        "model":      settings.get("model", snoop_agent.DEFAULT_MODEL),
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _df_to_slack(df, max_rows: int = 25) -> str:
    """Convert a DataFrame to a Slack-renderable plain-text table.

    Uses fixed-width columns so it looks good in a code block.
    Truncates to max_rows and adds a row-count footer when needed.
    """
    display = df.head(max_rows)
    truncated = len(df) > max_rows

    # Build fixed-width columns
    cols = list(display.columns)
    rows = [display[c].astype(str).tolist() for c in cols]
    col_widths = [
        max(len(str(c)), max((len(v) for v in rows[i]), default=0))
        for i, c in enumerate(cols)
    ]

    def _row(values: list[str]) -> str:
        return "  ".join(str(v).ljust(w) for v, w in zip(values, col_widths))

    header = _row([str(c) for c in cols])
    sep    = _row(["-" * w for w in col_widths])
    body   = "\n".join(_row(r) for r in zip(*rows))

    result = f"{header}\n{sep}\n{body}"
    if truncated:
        result += f"\n… {len(df) - max_rows:,} more rows (open Snoop to see all)"
    return result


def _format_response(result: snoop_agent.AgentResult) -> tuple[str, list[dict]]:
    """Build the Slack message text + blocks for an AgentResult.

    Returns (fallback_text, blocks) ready for client.chat_postMessage.
    """
    if result.error:
        text = f":warning: *Snoop error:* {result.error}"
        return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    blocks: list[dict] = []

    # --- Main answer text (split into ≤3 000 char blocks) -----------------
    body = result.text.strip()
    if body:
        for chunk in _split_text(body, 2900):
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            })

    # --- Tables -----------------------------------------------------------
    for i, df in enumerate(result.tables, start=1):
        try:
            table_str = _df_to_slack(df)
        except Exception:  # noqa: BLE001
            table_str = f"(table {i} — open Snoop web app to view)"
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{table_str}\n```"},
        })

    # --- Charts note (can't embed Plotly in Slack) -------------------------
    if result.figures:
        n = len(result.figures)
        label = f"{n} chart{'s' if n > 1 else ''}"
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f":bar_chart: {label} generated — "
                    "open *Snoop Doc* to view the interactive version"
                ),
            }],
        })

    # --- Confidence footer ------------------------------------------------
    if result.confidence:
        # Extract level + sources from the confidence block if it's structured
        conf_text = _parse_confidence_for_slack(result.confidence)
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": conf_text}],
        })

    # Timing footer
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"_{result.iterations} iteration{'s' if result.iterations != 1 else ''} · "
                f"{result.duration:.1f}s_"
            ),
        }],
    })

    fallback = body[:300] + ("…" if len(body) > 300 else "") if body else "(no text response)"
    return fallback, blocks


def _parse_confidence_for_slack(confidence: str) -> str:
    """Format the confidence block for a Slack context element."""
    level_match = re.search(r"(?:level|confidence)[:\s]+(\w+)", confidence, re.I)
    level = level_match.group(1).title() if level_match else None

    emoji = {"High": ":large_green_circle:", "Medium": ":large_yellow_circle:", "Low": ":red_circle:"}.get(
        level or "", ":white_circle:"
    )
    label = f"{emoji} *Confidence: {level}*" if level else ":white_circle: Confidence info"

    # Trim to one line for context block
    one_liner = confidence.strip().splitlines()[0][:200]
    return f"{label} — {one_liner}"


def _split_text(text: str, max_len: int) -> list[str]:
    """Split text into ≤max_len chunks, preferring paragraph breaks."""
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at].strip())
        text = text[split_at:].lstrip()
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Thread history → conversation history
# ---------------------------------------------------------------------------
def _build_conversation_history(
    client,
    channel: str,
    thread_ts: str,
    bot_user_id: str,
) -> list[dict]:
    """Fetch thread replies and build an Anthropic-style message history.

    Alternates user/assistant turns.  Bot messages become "assistant"
    turns; other messages become "user" turns (the question text only,
    stripped of @mentions).
    """
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts)
        thread_messages = resp.get("messages", [])
    except Exception:  # noqa: BLE001
        return []

    history: list[dict] = []
    for msg in thread_messages:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        is_bot = msg.get("bot_id") or msg.get("user") == bot_user_id
        role = "assistant" if is_bot else "user"

        # Strip @mentions so they don't confuse the agent
        clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if not clean:
            continue

        # For assistant turns, keep only the actual answer (strip
        # Slack formatting like ``` wrapping, context lines, etc.).
        # Simplest heuristic: just use the raw text as-is.
        history.append({"role": role, "content": clean})

    # Remove the last message — it's the current question, which the
    # caller will add as the new user turn.
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    return history


# ---------------------------------------------------------------------------
# Slack app wiring
# ---------------------------------------------------------------------------
def _strip_mention(text: str) -> str:
    """Remove leading @mention from message text."""
    return re.sub(r"^<@[A-Z0-9]+>\s*", "", text or "").strip()


def build_app(config: dict):
    """Build and return the Slack Bolt app."""
    try:
        from slack_bolt import App  # type: ignore
        from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore
    except ImportError:
        logger.error(
            "slack_bolt is not installed. Run: pip install slack_bolt"
        )
        sys.exit(1)

    app = App(token=config["bot_token"])

    # Cache the bot's own user ID once at startup to detect self-messages.
    try:
        bot_user_id: str = app.client.auth_test()["user_id"]
    except Exception:  # noqa: BLE001
        bot_user_id = ""

    # ------------------------------------------------------------------
    # app_mention handler — fires when @snoop is mentioned in a channel.
    # Runs in a background thread so Bolt's event loop stays unblocked.
    # ------------------------------------------------------------------
    @app.event("app_mention")
    def handle_mention(event, say, client):
        threading.Thread(
            target=_handle_question,
            args=(event, say, client, config),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # message.im handler — fires for DMs directly to the bot.
    # Skip @mentions (handled by app_mention) and the bot's own messages.
    # ------------------------------------------------------------------
    @app.event("message")
    def handle_dm(event, say, client):
        # Only handle DMs (channel type "im")
        if event.get("channel_type") != "im":
            return
        # Skip bot messages and subtypes (message_changed, etc.)
        if event.get("bot_id") or event.get("subtype"):
            return
        # Skip the bot's own user messages
        if bot_user_id and event.get("user") == bot_user_id:
            return
        # Skip @mention syntax — app_mention already handles those
        text = event.get("text", "")
        if re.match(r"^<@[A-Z0-9]+>", text):
            return
        threading.Thread(
            target=_handle_question,
            args=(event, say, client, config),
            daemon=True,
        ).start()

    return app, SocketModeHandler


def _handle_question(event, say, client, config: dict) -> None:
    """Core handler — shared by app_mention and DM events."""
    raw_text   = event.get("text", "")
    channel    = event.get("channel", "")
    thread_ts  = event.get("thread_ts") or event.get("ts")
    user       = event.get("user", "")

    question = _strip_mention(raw_text)
    if not question:
        say(
            text="Hi! Ask me anything about your data — e.g. _what was our MRR in Q4 2024?_",
            thread_ts=thread_ts,
        )
        return

    logger.info("Question from %s: %s", user, question[:120])

    # Acknowledge immediately so the user knows we got it
    thinking_resp = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Snoop is thinking…",
    )
    thinking_ts = thinking_resp["ts"]

    # Build conversation history from thread (multi-turn support)
    history: list[dict] = []
    if thread_ts and thread_ts != event.get("ts"):
        try:
            bot_info = client.auth_test()
            bot_user_id = bot_info.get("user_id", "")
        except Exception:  # noqa: BLE001
            bot_user_id = ""
        history = _build_conversation_history(client, channel, thread_ts, bot_user_id)

    # Progress updates posted as edits to the "thinking" message
    progress_lines: list[str] = []

    def on_progress(msg: str) -> None:
        progress_lines.append(msg)
        # Update at most every ~2 lines to avoid rate-limiting
        if len(progress_lines) % 2 == 0:
            try:
                client.chat_update(
                    channel=channel,
                    ts=thinking_ts,
                    text=f":hourglass_flowing_sand: {msg}",
                )
            except Exception:  # noqa: BLE001
                pass

    # Run the agent
    result = snoop_agent.run_question(
        question=question,
        api_key=config["api_key"],
        model=config["model"],
        conversation_history=history or None,
        on_progress=on_progress,
    )

    # Delete the "thinking" placeholder
    try:
        client.chat_delete(channel=channel, ts=thinking_ts)
    except Exception:  # noqa: BLE001
        pass

    # Post the answer
    fallback_text, blocks = _format_response(result)
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=fallback_text,
            blocks=blocks,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to post response: %s", e)
        # Fallback: send plain text
        say(
            text=fallback_text[:3000],
            thread_ts=thread_ts,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore
    except ImportError:
        print("ERROR: slack_bolt is not installed. Run: pip install slack_bolt")
        sys.exit(1)

    config = _load_config()

    if not config["bot_token"]:
        print(
            "ERROR: No Slack Bot Token found.\n"
            "Set SLACK_BOT_TOKEN in your environment, or go to\n"
            "Snoop → Integrations → Slack and save your tokens."
        )
        sys.exit(1)

    if not config["app_token"]:
        print(
            "ERROR: No Slack App Token found.\n"
            "Set SLACK_APP_TOKEN in your environment, or go to\n"
            "Snoop → Integrations → Slack and save your tokens."
        )
        sys.exit(1)

    if not config["api_key"]:
        print(
            "ERROR: No Anthropic API key found.\n"
            "Set ANTHROPIC_API_KEY in your environment, or configure\n"
            "it in Snoop → Settings."
        )
        sys.exit(1)

    from slack_bolt import App  # type: ignore

    app, Handler = build_app(config)

    logger.info(
        "Starting Snoop Slack bot (model: %s, Socket Mode)", config["model"]
    )
    handler = Handler(app, config["app_token"])
    handler.start()


if __name__ == "__main__":
    main()

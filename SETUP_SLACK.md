# Setting Up the Snoop Slack Bot

The Slack bot lets anyone on your team ask `@snoop` questions directly in
Slack and get answers powered by the same Claude agent as the web app.

```
@snoop  what was our MRR in Q4 2024?
@snoop  show me churn by plan for the last 6 months
@snoop  which customers have open invoices over $5k?
```

Replies land in a thread on the original message, tables are formatted as
code blocks, and multi-turn conversations work by replying in the thread.

---

## Prerequisites

- Snoop Doc is running and has data in `data/tables/`
- Your Anthropic API key is configured in **Settings**
- You have admin rights on your Slack workspace (or can ask an admin)

---

## Step 1 — Create the Slack App

1. Go to **https://api.slack.com/apps** and click **Create New App**
2. Choose **"From scratch"**
3. Name: `Snoop Doc` (or whatever you like)
4. Choose your workspace → **Create App**

---

## Step 2 — Enable Socket Mode

Socket Mode lets the bot connect over a persistent WebSocket instead of
requiring a public URL — perfect for running locally or on a private server.

1. In the left sidebar click **Socket Mode**
2. Toggle **Enable Socket Mode** → ON
3. It will ask you to generate an App-Level Token:
   - Token name: `snoop-socket`
   - Scope: `connections:write`
   - Click **Generate**
4. **Copy the token** — it starts with `xapp-`
   This is your **App-Level Token**

---

## Step 3 — Configure Event Subscriptions

1. In the left sidebar click **Event Subscriptions**
2. Toggle **Enable Events** → ON
3. Under **Subscribe to bot events**, add:
   - `app_mention` — for @snoop mentions in channels
   - `message.im` — for direct messages to the bot
4. Click **Save Changes**

---

## Step 4 — Set Bot Token Scopes

1. In the left sidebar click **OAuth & Permissions**
2. Scroll to **Scopes → Bot Token Scopes** and add:
   - `app_mentions:read`
   - `channels:history`
   - `chat:write`
   - `groups:history`
   - `im:history`
   - `im:read`
   - `im:write`

---

## Step 5 — Install to Workspace

1. Scroll to the top of **OAuth & Permissions**
2. Click **Install to Workspace** → Allow
3. **Copy the Bot User OAuth Token** — it starts with `xoxb-`
   This is your **Bot Token**

---

## Step 6 — Save Tokens in Snoop

Open the Snoop web app → **Integrations → Slack Bot** and paste:

| Field | Value |
|---|---|
| Bot Token | `xoxb-…` (from Step 5) |
| App-Level Token | `xapp-…` (from Step 2) |

Click **Save integrations**.

Alternatively, set environment variables:
```bash
export SLACK_BOT_TOKEN=xoxb-…
export SLACK_APP_TOKEN=xapp-…
```

---

## Step 7 — Start the Bot

In your terminal, in the snoop-doc directory:

```bash
# If using the venv:
./venv/Scripts/python slack_bot.py        # Windows
./venv/bin/python slack_bot.py            # Mac / Linux

# Or if packages are global:
python slack_bot.py
```

You should see:
```
INFO  snoop_slack  Starting Snoop Slack bot (model: claude-sonnet-4-5, Socket Mode)
INFO  slack_bolt   Starting to receive messages from a new connection
```

---

## Step 8 — Invite the Bot to Channels

In any Slack channel where you want to use Snoop:

```
/invite @Snoop Doc
```

Then try:
```
@snoop what tables do you have access to?
```

---

## Running Continuously

To keep the bot running after you close the terminal, use a process
manager. Simple options:

**Using `screen` (Linux/Mac):**
```bash
screen -S snoop-slack
python slack_bot.py
# Ctrl+A, D to detach
```

**Using `nohup` (Linux/Mac):**
```bash
nohup python slack_bot.py > slack_bot.log 2>&1 &
```

**Using PM2 (cross-platform, if Node.js is installed):**
```bash
pm2 start slack_bot.py --interpreter python
pm2 save
```

---

## Troubleshooting

**"No Anthropic API key" error**
→ Set your API key in Snoop → Settings, or export `ANTHROPIC_API_KEY`.

**Bot doesn't respond to @mentions**
→ Check that `app_mention` is in your Event Subscriptions (Step 3).
→ Check that the bot is invited to the channel (`/invite @Snoop Doc`).

**"channel_not_found" or permission errors**
→ Verify the bot scopes in Step 4 and reinstall the app (Step 5).

**"socket mode not enabled" error**
→ Re-check Step 2 — Socket Mode must be ON before generating the xapp- token.

**Tables look garbled in Slack**
→ Slack renders code blocks in monospace — the table should look fine.
   If columns are very wide, try narrowing your question to fewer columns.

**Charts aren't visible**
→ Plotly charts can't be embedded in Slack messages. The bot notes when a
   chart was generated and asks you to open the Snoop web app to view it.
   A future version may upload chart PNGs directly to Slack.

# Snoop Doc

**A data-analysis platform you can talk to.** Drop in your spreadsheets, sync from any data source — finance, marketing, ads, product, ops, whatever your team works with — and ask questions in plain English. Snoop runs real Python against the real data under the hood, so the numbers are computed, not generated.

## What makes it different from a generic AI chat

- **Context that compounds.** Snoop captures your team's institutional knowledge as you use it — schema docs that explain messy column names, business rules that say what counts as "revenue", durable memory for the facts you only want to teach it once. The agent reads all of it on every question. The same query that gets a generic answer from ChatGPT gets *your team's* answer from Snoop.

- **Trust on every answer.** Each response shows a **confidence level** and the **sources** it drew from — which CSVs, which context docs, which web pages. The numbers come from real pandas operations against your data, not language-model guesses. You can defend any answer in a meeting because you can see exactly where it came from.

- **Memory that learns.** Tell Snoop a fact once ("our fiscal year runs July–June", "PR retainer doesn't count as marketing spend") and it remembers — auto-loaded into every future conversation, for every teammate. Snoop becomes a team member who knows your business, not a stranger you re-onboard daily.

- **Knowledge artifacts.** Pin questions, save charts and tables, package whole exchanges as self-contained HTML or PDF reports, save and resume entire conversations. The work doesn't disappear into chat history; it accumulates.

- **Every data source, one shape.** Stripe and Google Sheets sync to clean CSVs today; Excel workbooks, PDFs and Word docs can be imported on demand. The sync pattern generalises — every new integration just adds files to a folder the agent already knows how to query. Use Snoop through the web app or as a `@snoop` Slack bot in any channel.

## What you get

In rough order:

### The core chat

- **Ask the Snoop** — a chat that runs Python under the hood. Claude can inspect the data, write pandas, draw Plotly charts, and self-correct on errors across multiple tool calls in a single turn. Every answer is computed live from the actual data. Each response carries a **confidence level** and a list of sources used, so you know how much to trust each number and where it came from.

### Context and memory (the trust layer)

- **Context** — manage company-wide context docs (org structure, glossary, fiscal calendar, accounting conventions, business rules). General docs are always loaded into the chat; per-file docs are loaded only when the matching CSV is in scope. **Import from PDF / Word / plain text** — text is extracted and saved as a context doc you can edit.
- **Schema-doc pointers** — point a CSV at a general context doc (e.g. all P&L files → `pnl.md`, all Stripe tables → `stripe.md`) and that doc becomes the canonical schema reference. The agent reads it on every turn, so it knows column conventions, aggregation traps, and canonical query snippets without having to guess from raw column names.
- **Agent memory** — Snoop saves durable facts the user teaches it to a memory file that auto-loads into every future conversation. Visible in the chat and editable via the Context view.
- **AI context editor** — modal chat that drafts a precise context doc for a CSV or a group of CSVs. For per-file docs it sees a full data fingerprint and can run pandas to verify claims. For general/group docs it can call `load_table(name, nrows=20)` to inspect any CSV on demand — useful when writing docs that span many files.

### Save your work

- **Saved Items** — save any chart, table, full exchange, or whole conversation Claude produced. Tables can be promoted into `data/tables/` to feed the next analysis. **Save-as-report** packages a question + the assistant's response (text, charts, tables) as a self-contained HTML file you can email, archive, or open offline; PDF download available too. **Save chat** stores the entire thread so you can pick a saved conversation later and **Resume** it.
- **Saved Questions** — pin frequently-asked questions and replay them with one click.

### Data — bring anything

- **Stripe live sync** — 15 CSVs covering customers, subscriptions, invoices, payment intents, charges, refunds, disputes, payouts, balance transactions, products, prices, coupons, and promotion codes. Restricted API key, incremental refresh after the first pull.
- **Google Sheets live sync** — paste a sheet URL, pick a tab, and Snoop materialises it as a CSV that stays one click away from fresh. OAuth via your Workspace account.
- **Manual upload** — drag-and-drop **CSV or Excel**. Excel workbooks open a sheet-picker dialog that turns each sheet into its own CSV.
- **Data view tools** — in-place row/cell editing, per-file **rename / delete / download**, **Export all** as a zip.

### Use it from anywhere

- **Slack bot** — `@snoop` in any Slack channel where the bot is invited and get the same answers in a thread. Runs as a separate long-lived process alongside the web app, sharing the same data + context files. See [SETUP_SLACK.md](SETUP_SLACK.md).
- **Web search + fetch** — optional. Web fetch is always available so Snoop can pull any URL you mention. Web search is a toggle (off by default — costs extra per request) for when you need external info: exchange rates, industry benchmarks, news.

### Steering the agent

- **Scope picker** — narrow the conversation to a subset of CSVs to keep the system prompt lean and the agent focused.
- **Quick mode** — force Haiku for cheap, single-number lookups.
- **Prefer charts** — auto-chart when the answer has 3+ comparable values.
- **Token + cost meter** in the sidebar, and a **Stop** button for runaway agent loops.
- **Charts demo** — a gallery of every chart type Snoop can produce, with "when to use" notes against fake P&L data. Useful for figuring out what to ask for.

## Stack

Streamlit · Anthropic API (Claude Sonnet 4.5 by default, Haiku 4.5 for utility tasks) · pandas · Plotly.

## Setup (one-time)

You need Python 3.10+.

```bash
# From inside the snoop-doc/ folder:
python -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows PowerShell

pip install -r requirements.txt
```

## Running the app

```bash
source venv/bin/activate          # if not already active
streamlit run app.py
```

A browser tab opens at http://localhost:8501.

Click **Settings** in the sidebar, paste your Anthropic API key, save, and ask your first question. The key is stored at `~/.snoop-doc/settings.json` (gitignored, `chmod 600` on Unix) so you don't re-enter it next time.

## Repo layout

```
snoop-doc/
├── app.py                # Streamlit entry point: UI, sidebar, agent loop, view routing
├── snoop_agent.py        # Headless version of the agent loop — used by slack_bot.py + future integrations
├── slack_bot.py          # @snoop Slack bot (Socket Mode); run as a separate long-lived process
├── SETUP_SLACK.md        # Step-by-step Slack bot setup guide
├── tools.py              # run_python tool + CSV discovery + execution sandbox + general-doc sandbox helper
├── memory.py             # save_memory tool + memory.md writer
├── gsheets.py            # Google Sheets OAuth + sync to data/tables/
├── stripe_sync.py        # Stripe sync: 15 CSVs (customers, subs, invoices, charges, payment intents, balance transactions, …)
├── context_chat.py       # AI editor for context docs (table mode + general mode w/ load_table)
├── context_browser.py    # "Context" view — general context docs + import (PDF/Word/text) + export
├── context_pointers.py   # YAML frontmatter parser + schema-doc pointer map (CSV → general doc)
├── data_browser.py       # "Data" view — CSVs, Google Sheets + Stripe sync UI, rename, export all, per-row download
├── data_fingerprint.py   # Profiler that summarises a CSV for the context editor
├── doc_import.py         # PDF / Word / Excel text + sheet extraction
├── doc_export.py         # Excel + PDF generation for chat tables and saved reports
├── saved_questions.py    # "Saved Questions" view
├── saved_items.py        # "Saved Items" view — charts, tables, reports, conversations
├── reports.py            # Render a chat exchange → self-contained HTML report
├── chart_demo.py         # "Charts demo" view + fake P&L data (sandbox fallback)
├── dialogs.py            # Shared confirm-or-run helper
├── theme.py              # Plotly template, CSS, icon map, logo install
├── prompts/system.md     # Main agent system prompt
├── data/
│   ├── tables/                 # ← drop CSVs here (also: synced sheets + Stripe land here)
│   ├── context/                # ← Markdown context docs
│   │   ├── *.md                  —  general (company-wide, always loaded; can carry `covers: [...]` frontmatter)
│   │   └── tables/*.md           —  doc-related, one per CSV (scope-filtered)
│   ├── sheets-registry.json    # Google Sheets sync registry (committed)
│   └── stripe-registry.json    # Stripe sync registry (committed)
├── google-client.json    # OAuth client for Google Sheets (committed; see setup)
├── PROJECT_NOTES.md      # Internal design doc + decisions log + TODO list
└── README.md             # ← you are here
```

User settings live outside the repo at `~/.snoop-doc/settings.json`. Per-user Google OAuth tokens live at `~/.snoop-doc/google-token.json`.

## Data conventions

You don't need to touch `app.py` to add data. Two places:

- **`data/tables/`** — one CSV per file. Sanitised filename becomes the DataFrame's variable name (e.g. `P&L 2026.csv` → `p_l_2026`). When there's exactly one CSV loaded, it's also aliased as `df`. See `data/tables/README.md` for naming + format conventions.
- **`data/context/`** — Markdown docs that flow into the system prompt. General docs (the root) describe company-wide facts; per-file docs in `tables/` describe one CSV (loading recipe, column meanings, aggregation traps). See `data/context/README.md` for what each doc should cover.

The fastest way to write a context doc is via the AI editor: drop a CSV → in the Setup dialog leave "Use AI to build the context doc" on → answer the editor's questions. It scans a full fingerprint of the file, drafts a doc, and refines from your corrections.

## Google Sheets live sync

Snoop can pull a tab from any Google Sheet you have access to and materialise it as a CSV in `data/tables/`. From there it's indistinguishable from a manually-uploaded CSV — the agent, context editor, and everything downstream just see a regular file.

### One-time setup (do this once, before the first teammate uses it)

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a new project (or reuse one). Name doesn't matter; nobody outside your Workspace will see it.
2. In the new project, enable two APIs: **Google Sheets API** + **Google Drive API**. Search for each in "APIs & Services → Library" and click Enable.
3. **OAuth consent screen** → set User Type to **Internal** (Workspace publishing — restricts access to `@vertistudio.com` accounts only, no warnings, no test-user cap). Add the basic info Google asks for.
4. **Credentials → Create Credentials → OAuth client ID** → Application type **Desktop app**. Download the resulting JSON.
5. Save that JSON as `google-client.json` in the project root (next to `app.py`). Yes, it's committed to git — it's an internal-Workspace desktop OAuth client, which Google explicitly classifies as "not really secret." Sharing it lets every teammate skip the GCP setup.

### Per-user (each teammate does this once on their laptop)

1. Run the app, open **Settings** in the sidebar.
2. Click **Connect Google** under Integrations. A browser tab opens, you sign in with your Workspace account, grant access, and the tab tells you to close it. Tokens are stored at `~/.snoop-doc/google-token.json` (chmod 600).
3. *(Optional)* toggle **Sync all registered sheets on app start** if you want fresh data every launch.

### Per-sheet (any time)

1. Share the sheet with everyone on the team who needs it (standard Google Sheets Share dialog — by user, not by domain link).
2. In the Data view, open **Sync from Google Sheets**, paste the URL, pick which tab to import, name the CSV, and click Import. If "Use AI to build the context doc" is on, the context editor opens with a full fingerprint of the freshly-pulled data.
3. Re-sync any time via the per-file sync icon in the Data view, or use **Sync all** to refresh everything.

### Behaviour

- Each sync is a snapshot. The CSV reflects the state of the sheet at the moment you last clicked Sync. The Data view shows a staleness label (`fresh` / `stale` / `old`) per synced file.
- A synced CSV is read-only in the in-app editor — edits would be silently nuked on the next sync. Edit at the source.
- Renaming a tab in Google is fine — Snoop tracks tabs by their immutable `gid`. Renaming the workbook is fine too.
- Sync failures (revoked access, deleted tab, network) show on the row with the error message. Other syncs succeed independently.

## Stripe live sync

Snoop can pull your Stripe data into fifteen CSVs in `data/tables/`:

**Core data**
| CSV | Grain |
|---|---|
| `stripe_customers.csv` | one row per customer |
| `stripe_subscriptions.csv` | one row per subscription |
| `stripe_subscription_items.csv` | one row per (sub, price) — covers multi-product subs |
| `stripe_invoices.csv` | one row per invoice |
| `stripe_invoice_line_items.csv` | one row per invoice line — proration / multi-product attribution |
| `stripe_payment_intents.csv` | one row per payment intent (Stripe's modern payment object) |
| `stripe_charges.csv` | one row per charge (legacy payment object — kept alongside Payment Intents for direct comparison; carries roll-up fields PI doesn't, like `amount_refunded`, `refunded`, `disputed`) |
| `stripe_refunds.csv` | one row per refund |
| `stripe_disputes.csv` | one row per dispute / chargeback |
| `stripe_payouts.csv` | one row per payout to your bank |
| `stripe_balance_transactions.csv` | one row per money-movement event Stripe records on the account balance — the universal "what hit the balance, in settlement currency" record. `amount`, `net`, `fee` are always in the account's settlement currency regardless of the source object's currency (Stripe does the FX at transaction time, so no FX table is needed). `source` joins to charges / refunds / disputes / payouts via the `ch_…` / `pyr_…` / `du_…` / `po_…` id prefix |

**Reference tables**
| CSV | Grain |
|---|---|
| `stripe_products.csv` | product catalog |
| `stripe_prices.csv` | prices for those products |
| `stripe_coupons.csv` | discount definitions |
| `stripe_promotion_codes.csv` | redeemable codes that map to coupons |

The agent joins these in pandas at query time. Typical questions answerable from this data: MRR / ARR, the MRR movement waterfall (new / expansion / contraction / churn), renewal cohort analysis, top customers by ARR, NRR by cohort, cash collections, failed-payment / involuntary-churn rate, plan-mix shifts, ACV by product, refund rate by product, chargeback / dispute trends, effective discount % from coupons, payout timing for cash forecasting, proration revenue from invoice line items, and accurate **multi-currency reporting** via balance transactions (always in settlement currency).

If you ever swap the Stripe API key for a different account, run `python _cleanup_old_account.py` to drop rows from the previous account that the incremental-sync merge would otherwise keep around. The Data view's **Force full re-sync** button resets the incremental cursors so historical data on a new account gets re-pulled.

### One-time setup (per Snoop install)

1. In Stripe Dashboard → **Developers → API keys → Create restricted key**.
2. Give it a name (e.g. "Snoop Doc – read-only"). Set the following **resource permissions to Read**: Customers, Subscriptions, Invoices, Payment Intents, Charges, Refunds, Disputes, Payouts, Products, Prices, Coupons, Promotion Codes. Leave everything else **None**.
3. Click Create, copy the `rk_live_…` (or `rk_test_…`) key.
4. In Snoop: **Settings → Integrations → Stripe** → paste the key → **Test connection** → **Save settings**.

### First sync

Open the Data view → **Sync from Stripe** expander.

The first time you pull a real Stripe account this can take a while — 10–25 minutes for accounts with ~25k subs / ~250k invoices. The expander shows a per-object list; you can sync them one at a time (products + prices are trivial; customers next; the big ones are invoices and charges).

After the first pull, every subsequent sync is **incremental** (only objects created since the last sync) and takes ~30 seconds. Subscriptions, products, and prices do full re-pulls because their state changes aren't captured by Stripe's `created` filter — but they're small.

### Refresh

- **Manual** — sync icon on each row in the Data view, or "Sync all Stripe objects" inside the expander.
- **Automatic on app start** — toggle in **Settings → Integrations → Stripe**. Won't fire until you've done the first manual sync (avoids accidentally triggering a 25-min sync at startup).

### Behaviour

- Synced CSVs are **read-only** in the in-app editor. Stripe is the source of truth.
- The agent sees the same staleness bands as Google Sheets (`fresh` / `stale` / `old`) per file.
- Errors (auth failure, rate limit, etc.) show inline on the failed row; other objects sync independently.

## Slack bot

Snoop can also be invoked as `@snoop` in any Slack channel where the bot is invited. It runs as a separate long-lived Python process alongside the web app, sharing the same `data/` and `data/context/` directories. Same Claude agent, same answers — just in Slack threads instead of the Streamlit chat.

Two processes, one disk: the web app and the Slack bot are independent. They share state by reading the same CSVs and context docs from disk.

See **[SETUP_SLACK.md](SETUP_SLACK.md)** for the full step-by-step setup. Quick version:

```bash
# Once: create a Slack app, enable Socket Mode, copy the xoxb- + xapp- tokens
#       into Snoop → Integrations → Slack
# Each time you want the bot running:
python slack_bot.py
```

Wrap in `screen`, `nohup`, or `pm2` to keep it running after you close the terminal.

Behaviour:
- Tables format as fixed-width code blocks (Slack renders them in monospace).
- Plotly charts can't be embedded in Slack — the bot notes when a chart was generated and points back to the web app.
- Threading carries multi-turn context: reply in a thread to continue the conversation.

## Document import & export

**Importing data:**
- The **Upload a data file** button in the Data view accepts `.csv`, `.xlsx`, and `.xls`. Excel workbooks open a sheet-picker dialog letting you turn each sheet into its own CSV in `data/tables/`.

**Importing context:**
- The Context view's upload accepts `.md` directly, plus **PDF, Word (.docx), and plain text** — text is extracted automatically and saved as a context doc. Preview the extracted text before saving.

**Exporting data:**
- **Export all** (Data view header) — every CSV in `data/tables/` zipped as `snoop-data-YYYY-MM-DD.zip`.
- **Per-row Download** icon on each CSV row.
- **Excel download** on result tables in chat and Saved Items — bold headers, auto-fitted columns.

**Exporting reports:**
- Saved reports offer both an HTML download (the self-contained file) and a **PDF download** (text + tables; Plotly charts are noted as HTML-only).

## Schema-doc pointers

If you have many CSVs that share a schema (e.g. several years of P&L; the 15 Stripe tables), write one general context doc per group (`pnl.md`, `stripe.md`, etc.) and **point** each CSV at it from the Data view's Context dialog. The pointer lives in the general doc's YAML frontmatter as a `covers: [filename1, filename2]` list.

What it does:
- The doc loads into the agent's system prompt on every turn (because it's a general doc).
- The `run_python` tool description renders a `schema: pnl.md` line next to each pointed CSV, so the agent knows which doc to consult.
- One curated group doc replaces having to describe each file's schema separately — useful when column names are messy or inconsistent across files of the same kind.

The Context dialog also still supports a per-file individual context doc for one-off quirks that don't belong in the shared group doc.

## Configuring the chat

- **Settings view** (sidebar → Settings): API key, chat model, utility model (used by the AI context editor), Quick mode model, temperature, max output tokens, confirm-before-destructive toggle. Integrations (Google Sheets, Stripe) live on their own page.
- **Quick mode** toggle: forces the next request to Haiku regardless of the chat-model setting. Cheaper and faster — good for single-number lookups.
- **Prefer charts** toggle: nudges the model to chart whenever the answer involves 3+ comparable values, even if you didn't ask explicitly.
- **Scope picker** (top of the chat): narrow the agent to a subset of CSVs. Only those CSVs are loaded into the sandbox, and only their doc-related context docs are included in the system prompt.

## More

For internal design context — decisions log, build TODOs, the rationale behind each feature — see [PROJECT_NOTES.md](PROJECT_NOTES.md).

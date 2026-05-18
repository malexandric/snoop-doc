# Snoop Doc

Ask your company's data questions in plain English and get answers, charts, and tables back. Snoop wraps Anthropic's Claude with a Python sandbox that runs pandas against your CSVs, so the model can compute real numbers (not hallucinate them) and draw real charts inline.

Built for an internal finance + marketing + leadership team.

## What you get

- **Ask the Snoop** — a chat that runs Python under the hood. Claude can inspect the data, write pandas, draw Plotly charts, and self-correct on errors across multiple tool calls in a single turn. Every answer is computed live from the actual data.
- **Google Sheets live sync** — paste a sheet URL, pick a tab, and Snoop materialises it as a CSV that stays one click away from fresh. OAuth via your Workspace account; per-user tokens, shared sync registry. See setup below.
- **Stripe live sync** — pulls customers, subscriptions, invoices, charges, products, and prices into CSVs the agent can query. Restricted API key, incremental refresh after the first pull. See setup below.
- **Data** — drag-and-drop CSV upload, in-place row/cell editing, per-file delete. Every file gets a one-click "Context" button that opens an AI editor for its explainer doc.
- **Context** — manage company-wide context docs (org structure, glossary, fiscal calendar, accounting conventions). General docs are always loaded into the chat; per-file docs are loaded only when the matching CSV is in scope.
- **Saved Questions** — pin frequently-asked questions and replay them with one click.
- **Agent memory** — Snoop can save durable facts the user teaches it ("fiscal year is July–June", "PR retainer isn't part of marketing spend") to a memory file that auto-loads into every future conversation. Saves are visible in the chat and editable via the Context view.
- **Web search + fetch** — optional. Web fetch is always available so Snoop can pull any URL you mention. Web search is a toggle (off by default — costs extra per request) for when you need external info: exchange rates, industry benchmarks, news.
- **Saved Items** — save any chart, table, full exchange, or whole conversation Claude produced. Tables can be promoted into `data/tables/` to feed the next analysis. **Save-as-report** packages a question + the assistant's response (text, charts, tables) as a self-contained HTML file you can email, archive, or open offline. **Save chat** stores the entire thread (every question + response + chart + table) so you can pick a saved conversation later and **Resume** it — loads back into the chat and you keep going.
- **Charts demo** — a gallery of every chart type Snoop can produce, with "when to use" notes against fake P&L data.
- **AI context editor** — modal chat that drafts a precise context doc for a CSV (or a general doc). Sees a full data fingerprint, can run pandas to verify claims, and stays consistent with the rest of your project's context.
- **Scope picker** — narrow the conversation to a subset of CSVs to keep the system prompt lean and the agent focused.
- **Token + cost meter** in the sidebar, **Quick mode** (force Haiku for cheap lookups), **Prefer charts** (auto-chart when the answer has 3+ comparable values), and a **Stop** button for runaway agent loops.

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
├── tools.py              # run_python tool + CSV discovery + execution sandbox
├── memory.py             # save_memory tool + memory.md writer
├── gsheets.py            # Google Sheets OAuth + sync to data/tables/
├── stripe_sync.py        # Stripe sync: 7 CSVs (customers, subs, invoices, charges, …)
├── context_chat.py       # AI editor for context docs (table mode + general mode)
├── context_browser.py    # "Context" view — general context docs
├── data_browser.py       # "Data" view — CSVs (incl. Google Sheets + Stripe sync)
├── data_fingerprint.py   # Profiler that summarises a CSV for the context editor
├── saved_questions.py    # "Saved Questions" view
├── saved_items.py        # "Saved Items" view — charts, tables, reports
├── reports.py            # Render a chat exchange → self-contained HTML report
├── chart_demo.py         # "Charts demo" view + fake P&L data (sandbox fallback)
├── dialogs.py            # Shared confirm-or-run helper
├── theme.py              # Plotly template, CSS, icon map, logo install
├── prompts/system.md     # Main agent system prompt
├── data/
│   ├── tables/                 # ← drop CSVs here (also: synced sheets land here)
│   ├── context/                # ← Markdown context docs
│   │   ├── *.md                  —  general (company-wide, always loaded)
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

Snoop can pull your Stripe data into twelve CSVs in `data/tables/`:

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

**Reference tables**
| CSV | Grain |
|---|---|
| `stripe_products.csv` | product catalog |
| `stripe_prices.csv` | prices for those products |
| `stripe_coupons.csv` | discount definitions |
| `stripe_promotion_codes.csv` | redeemable codes that map to coupons |

The agent joins these in pandas at query time. Typical questions answerable from this data: MRR / ARR, the MRR movement waterfall (new / expansion / contraction / churn), renewal cohort analysis, top customers by ARR, NRR by cohort, cash collections, failed-payment / involuntary-churn rate, plan-mix shifts, ACV by product, refund rate by product, chargeback / dispute trends, effective discount % from coupons, payout timing for cash forecasting, and proration revenue from invoice line items.

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

## Configuring the chat

- **Settings view** (sidebar → Settings): API key, chat model, utility model (used by the AI context editor), Quick mode model, temperature, max output tokens, confirm-before-destructive toggle. Integrations (Google Sheets, Stripe) live on their own page.
- **Quick mode** toggle: forces the next request to Haiku regardless of the chat-model setting. Cheaper and faster — good for single-number lookups.
- **Prefer charts** toggle: nudges the model to chart whenever the answer involves 3+ comparable values, even if you didn't ask explicitly.
- **Scope picker** (top of the chat): narrow the agent to a subset of CSVs. Only those CSVs are loaded into the sandbox, and only their doc-related context docs are included in the system prompt.

## More

For internal design context — decisions log, build TODOs, the rationale behind each feature — see [PROJECT_NOTES.md](PROJECT_NOTES.md).

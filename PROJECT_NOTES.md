# Snoop Doc — project notes

Internal working doc. Updated whenever we add a feature or hit a constraint.

---

## What we're building

A web app where you can ask questions about the company's financial data in plain English and get answers, charts, and reports back. The model writes Python under the hood (against pandas DataFrames built from our spreadsheets) and produces both the numbers and any chart we ask for.

**Audience:** internal — finance + leadership.
**Stack:** Streamlit (Python) frontend, Claude (Anthropic API) for the AI layer, Plotly for charts.
**Where it runs:** locally on the demo laptop. No deployment / hosting yet.

---

## How it works (at a glance)

```
User question
   ↓
Streamlit chat UI
   ↓
Claude (with run_python tool + system prompt + data schemas)
   ↓
Local Python sandbox executes code against pandas DataFrames
   ↓
Output (text via print, or Plotly figure via `fig = ...`)
   ↓
Rendered inline in the chat
```

Claude can call the tool multiple times in a single question (e.g. peek at the data first, then build the right chart).

---

## Repo layout

```
snoop-doc/
├── app.py              # Streamlit app: UI, sidebar, agent loop, view routing
├── snoop_agent.py      # Headless version of the agent loop — used by slack_bot.py + future integrations
├── slack_bot.py        # @snoop Slack bot (Socket Mode); separate long-lived process
├── SETUP_SLACK.md      # Step-by-step Slack bot setup guide
├── tools.py            # run_python tool + execution sandbox + CSV discovery + general-doc sandbox helper
├── memory.py           # save_memory tool + memory.md writer
├── gsheets.py          # Google Sheets OAuth + sync-to-CSV + sync registry
├── stripe_sync.py      # Stripe sync: 15 CSVs (customers, subs, invoices, charges, PIs, balance transactions, …)
├── chart_demo.py       # Sample charts in the "Charts demo" view + fake data
├── data_browser.py     # "Data" view — browse/edit/upload/delete/rename/download + Google Sheets + Stripe sync UI
├── context_browser.py  # "Context" view — manage general docs + import (PDF/Word/text) + export
├── context_chat.py     # AI chat dialog for building/editing context docs (table mode + general w/ load_table)
├── context_pointers.py # YAML frontmatter parser + schema-doc pointer map (CSV → general doc)
├── data_fingerprint.py # CSV profiler — feeds rich data summary to the context chat
├── doc_import.py       # PDF / Word / Excel text + sheet extraction
├── doc_export.py       # Excel + PDF generation for chat tables and saved reports
├── saved_questions.py  # "Saved Questions" view — manage pinned prompts
├── saved_items.py      # "Saved Items" view — store + browse charts/tables/reports/conversations
├── reports.py          # Render one chat exchange → self-contained HTML report
├── dialogs.py          # Shared confirm-or-run helper used across views
├── theme.py            # Plotly template + CSS + icon map + logo install
├── prompts/
│   └── system.md       # System prompt — edit to change assistant behavior
├── assets/
│   ├── logo.png        # Wordmark for the Streamlit sidebar logo slot
│   └── logo.svg        # Fallback symbol-only mark
├── .streamlit/
│   └── config.toml     # Streamlit theme (colors, base mode)
├── google-client.json  # OAuth client for Google Sheets sync (committed; see README setup)
├── data/
│   ├── tables/                # Real CSVs go here (manual uploads + synced sheets)
│   ├── context/               # Markdown context docs:
│   │   ├── *.md                 —  general docs (always loaded into main chat; can carry `covers: [...]` frontmatter)
│   │   └── tables/*.md          —  doc-related docs (one per CSV, scope-filtered)
│   ├── sheets-registry.json   # Google Sheets sync registry (committed, shared via git)
│   └── stripe-registry.json   # Stripe sync registry (committed, shared via git)
├── requirements.txt
├── README.md           # Public-facing setup instructions
└── PROJECT_NOTES.md    # ← this file
```

Per-user Google OAuth tokens live at `~/.snoop-doc/google-token.json` (gitignored by being outside the repo).

Persistent user settings (API key + model) live outside the repo at:
`~/.snoop-doc/settings.json` (gitignored, chmod 600 on Unix).

---

## Features

In rough order added.

### Skeleton chat
- Streamlit app with chat input and message history.
- Non-streaming responses (tried streaming, rolled it back — see "Non-streaming agent loop" below).
- Sidebar: API key + model live in the Settings view (its own page); sidebar carries nav + Clear chat / Stop / Quick mode / Prefer charts toggles + session usage meter.
- System prompt loaded from `prompts/system.md` so we can tune behavior without code changes.

### Charts demo
- Reference page in `chart_demo.py` showing every chart type Snoop can produce, against fake P&L data.
- Grouped into six sections — Trends over time, Comparing categories, Composition, Relationships, Patterns, Finance-specific.
- 14 chart types total: line, stacked area, bar, horizontal bar, grouped bar, 100% stacked bar, treemap, donut, dual-axis combo, scatter, heatmap, waterfall, bullet (actuals vs target), Sankey.
- Each chart has a name + "When to use" + "Finance example" block above it, so users know what to ask Snoop for on their real data.

### Persistent settings
- API key and model selection auto-save to `~/.snoop-doc/settings.json` on change.
- Loaded on startup so users don't re-enter their key every session.
- "Forget saved settings" button to wipe the saved file (lives on the Settings view).
- Caption shows save status.
- Loader also reverse-migrates the leftover multi-provider format from an earlier iteration, picking out the Anthropic key/model.

### AI chart generation (run_python tool)
- Claude has a `run_python` tool that executes code in a local sandbox against pre-loaded DataFrames.
- Available libs in sandbox: `pd`, `np`, `px`, `go`.
- Charts: Claude assigns a Plotly `Figure` to `fig` and the app renders it inline.
- Text/numbers: Claude uses `print()` and the captured stdout flows back.
- Agent loop: Claude can call the tool multiple times per question and self-correct on errors.
- UI shows each step: "Ran code" expander, inline chart, "Output" expander.

### Status panel (debug-friendly progress log)
- Each agent turn renders an `st.status` panel at the top of the assistant bubble. Expanded while running, collapses to a one-line summary on success, stays expanded with red "error" state on failure.
- Logs per iteration: API call duration + token breakdown (fresh/cached/written/out), the response shape (number of text blocks + tool calls + stop_reason), code-line count for each tool call, tool execution duration + result summary (chars of stdout, chart trace + data-point counts if a `fig` was produced), and any tool errors.
- Built so we can spot where a "never-ending spinner" is coming from: if a single API call takes 60s, you see it; if a Plotly figure has 200 traces, you see it (likely cause: browser hang during chart render); if iterations are looping on errors, the error is logged each time.
- Total elapsed time is shown on completion, e.g. `Done in 12.4s · 3 iterations`.

### Persistent sandbox state (per question)
- `tools.build_initial_namespace()` builds one execution namespace per user question. Every tool iteration for that question shares it.
- Variables the model defines, helper functions it writes, and any DataFrame mutations are visible on the next call — like a Jupyter notebook within a question.
- A new question starts a fresh namespace. Helpers don't bleed across unrelated questions.
- `fig` is explicitly reset between calls so a stale figure from an earlier iteration isn't accidentally re-rendered.
- This eliminates a class of NameError-then-retry loops where the model defined something useful in call 1 (e.g. a `parse_currency` helper, a cleaned subset DataFrame) and tried to use it in call 2.

### Demo data (fallback only)
- `pnl_by_branch`: monthly P&L by branch (Products / Services / Marketplace), Jan 2024 – Dec 2025.
- `product_margins`: monthly gross margin % by product (Pro Plan / Enterprise / Starter / Add-ons / Legacy), 2025.
- Generated in `chart_demo.py`. Used by `tools.py` only when there are no real CSVs in `data/tables/`.

### Real data + context loading
- `tools.py` reads every `.csv` in `data/tables/` on each tool call. Each file becomes a DataFrame named after a sanitized version of the filename (e.g. `P&L 2026 - P&L 2026.csv` → `p_l_2026_p_l_2026`).
- The tool description is rebuilt fresh on every Claude API call (`tools.build_tools()`) and includes the table name, source filename, row × col count, and a preview of the first 5 rows. This is what Claude sees when deciding how to query the data.
- `app.py:_load_context_docs(scope)` builds the "Company & data context" section of the system prompt by combining two doc categories — see the **General / doc-related context split** feature below for the rules.
- Files that can't be parsed (encoding issues, malformed CSV) try a fallback chain (`utf-8-sig` → `utf-8` → `latin-1` → `cp1252`) before being skipped; the chat still works against the others.
- The system prompt instructs Claude to handle spreadsheet quirks (multi-row headers, currency strings, section-divider rows, total columns) since real CSVs usually need cleanup before computing.

### General / doc-related context split
- Context docs are split into two categories with different loading behaviour:
  - **General docs** live at `data/context/*.md` (root). They describe company-wide facts the AI needs on every turn: org structure, brand portfolio, fiscal calendar, currency, glossary, accounting conventions. **Always loaded** into the main chat's system prompt, regardless of which CSVs the user has scoped in.
  - **Doc-related docs** live at `data/context/tables/<csv_stem>.md`. Each one is tied to a CSV by filename match. **Loaded only when the matching CSV is in scope** (per the table-scope picker). Keeps the system prompt lean when the user has narrowed to a small subset of files.
- The two categories also drive two different UIs (Context view vs. Data view's per-CSV button) and two different AI editor modes (general mode vs. table mode in `context_chat.py`).
- `_load_context_docs(scope)` in `app.py` is the single source of truth for both rules.
- Existing pairs are auto-discovered by stem-match against `tools.discover_tables()`, so renaming a CSV breaks the link until the corresponding `.md` is renamed too.

### Google Sheets live sync
- New module `gsheets.py`. Lets users pull one tab of a Google Sheets workbook into `data/tables/<name>.csv`. From there it's indistinguishable from a manually-uploaded CSV — sandbox, fingerprint, context-chat, scope picker all see it identically.
- **Auth model: OAuth 2.0 for installed (desktop) apps**, Workspace-internal publishing. Each user clicks **Connect Google** in Settings → browser opens → consent → `InstalledAppFlow.run_local_server()` captures the redirect on a one-shot `127.0.0.1:<port>` listener → tokens persisted at `~/.snoop-doc/google-token.json` (chmod 600). Refresh handled transparently. Scopes requested: `openid` + `userinfo.email` (just for displaying the connected account) + `spreadsheets.readonly`.
- **Per-team setup is one-time:** one person creates a GCP project, enables Sheets + Drive APIs, sets the OAuth consent screen to Internal, creates a Desktop-app OAuth client, and commits the JSON to `google-client.json` at the repo root. Workspace-internal scoping means anyone outside the domain can't even start the flow, so committing the "secret" is safe (Google explicitly classifies desktop OAuth client secrets as non-secret). After that, every teammate just clicks **Connect Google** with their own Workspace account.
- **Sync registry (`data/sheets-registry.json`)** — committed, shared via git. Each entry: `{id, sheet_id, sheet_title, gid, tab_name_at_sync, target_filename, last_synced_at, last_sync_error}`. The team shares *which* sheets are registered; each user's OAuth token determines *whether* they can pull from each one. If a colleague doesn't have access to a sheet, that row's sync fails for them with an access-denied caption.
- **Identifier choice: store the immutable `gid`**, not the tab name. Renaming a tab in Sheets doesn't break the sync. The registry caches `tab_name_at_sync` for display only and refreshes it on every sync.
- **Sync flow:** one sync = one tab → one CSV. The Data view's **Sync from Google Sheets** expander is two-step: paste URL → "Look up tabs" fetches workbook metadata → user picks a tab + names the target CSV + toggles "Use AI to build the context doc" → Import pulls data and writes the file. The AI editor opens on successful import (same entry point as a manual upload).
- **Re-sync surfaces:** per-row sync icon in the file list (with staleness caption underneath the filename); a **Sync all** button at the top of the Data view (only when something's registered); and an optional **Sync on app start** toggle in Settings → Integrations (gated to run once per Streamlit session via `_google_sync_on_start_done`, never per rerun).
- **Read-only enforcement:** synced CSVs hide the in-app `st.data_editor` and show a banner pointing the user back to the Google Sheet. Mixed-source-of-truth bugs (edit locally, sync nukes it) are silent and expensive in finance; better to disallow the path entirely.
- **Staleness band** drives the icon next to each synced row: `fresh` < 24h, `stale` 24h–7d, `old` > 7d, `never` if never synced. Errors show their message inline so the user can fix without opening the file.
- **Format choice:** values pulled with `FORMATTED_VALUE` so currency/date formatting matches what users see in Sheets. Rows padded with empty strings to the widest row's width before CSV write, so pandas reads a consistent rectangle.
- **Lazy imports:** the `google-*` libraries are imported inside `gsheets.py` functions, not at module top. If a user hasn't run `pip install -r requirements.txt` since these deps were added, the rest of the app still boots; the Settings UI surfaces a clean "run pip install" message instead of a crash.
- **Typed errors** (`NotConfiguredError`, `NotConnectedError`, `AccessDeniedError`, `DepsMissingError`, `GSheetsError`) so the UI can branch on cause without parsing messages.
- **Delete a synced CSV** = also drop its registry entry. Otherwise the file list would show phantom "synced but missing" rows.
- **Out of scope** (deliberate, may revisit): background polling / auto-refresh between launches; webhook-based sync; multi-tab import in one click; schema-drift detection; multi-account OAuth on one laptop.

### Stripe live sync
- New module `stripe_sync.py`. Pulls Stripe data into twelve CSVs in `data/tables/` so the agent can answer subscription-business questions (MRR / ARR / churn / NRR / cohort analysis / cash collections / refunds / chargebacks / payout timing / coupon-driven revenue / proration accounting) against real Stripe data.
- **Auth: restricted API key**, stored in `~/.snoop-doc/settings.json` under `stripe_api_key`. Same model as the Anthropic key — per-user, never committed. Users create a restricted key in Stripe Dashboard with read-only access to Customers, Subscriptions, Invoices, Payment Intents, Charges, Refunds, Disputes, Payouts, Products, Prices, Coupons, Promotion Codes. Much simpler than the Google Sheets OAuth dance because there's no per-user identity to track on Stripe's side; the API key IS the identity.
- **Twelve CSVs** (the data model):
  - Core data (high-volume, incremental sync):
    - `stripe_customers.csv` — one row per customer.
    - `stripe_subscriptions.csv` — one row per subscription.
    - `stripe_subscription_items.csv` — one row per (sub, price). Critical for multi-product subscription businesses.
    - `stripe_invoices.csv` — one row per invoice.
    - `stripe_invoice_line_items.csv` — one row per invoice line. Drives proration analysis and multi-product revenue attribution that bare invoice totals can't.
    - `stripe_payment_intents.csv` — one row per payment intent (Stripe's modern payment object — money-movement data plus payment lifecycle state: `requires_payment_method`, `processing`, `succeeded`, `canceled`, etc., plus `last_payment_error_code/message` for failure analysis).
    - `stripe_charges.csv` — one row per charge (legacy payment object). Kept alongside `payment_intents` for direct comparison: Charges has roll-up convenience fields PaymentIntent doesn't expose (`amount_refunded` summed across refunds, `refunded` / `disputed` / `paid` booleans, `failure_code` / `failure_message` at the top level). Includes `payment_intent_id` so the two tables join 1:1 where both are present. The original plan was to drop Charges in favour of PaymentIntents only; turns out probing the live account showed both have data and the two views differ in useful ways, so keep both.
    - `stripe_refunds.csv` — one row per refund (with reason, amount, status; carries both `charge_id` and `payment_intent_id` so it joins either way).
    - `stripe_disputes.csv` — one row per dispute / chargeback (status, reason, evidence due-by, network reason code; same dual-link as refunds).
    - `stripe_payouts.csv` — one row per payout to bank (arrival date, status, balance_transaction for fee reconciliation).
  - Reference tables (small, full re-pull each sync):
    - `stripe_products.csv` + `stripe_prices.csv` — product catalogue.
    - `stripe_coupons.csv` + `stripe_promotion_codes.csv` — discount definitions and the redeemable codes that reference them. Promotion codes link back to their coupon via `coupon_id`.
- **Side-effect syncs:** two object types come back nested in their parent's fetch response and don't have standalone list endpoints worth using:
  - `subscription_items` — extracted from each Subscription's `.items` field.
  - `invoice_line_items` — extracted from each Invoice's `.lines` field (paginated via `auto_paging_iter()` when an invoice has more than 10 lines).
  The `SIDE_EFFECT_OBJECTS` constant gates them out of the UI's per-object list, the Sync-all loop, and the standalone `sync_object()` path. A `PARENT_TO_SIBLING` map drives the write step inside `sync_object()`, so adding new side-effect pairs in future is one-line.
- **Sync model: initial full pull → incremental thereafter.** Each object type's registry entry tracks `last_synced_at`. Incremental-eligible objects use Stripe's `created[gte]=<last_sync>` filter, and the append-with-dedupe-on-id logic in `_write_rows` merges with the existing CSV. Initial pull on a real Stripe account (~25k subs, ~250k invoices) takes 10–25 minutes; incremental syncs after that are ~30 seconds.
- **Full re-pull for objects where state changes matter** — subscriptions (status transitions, cancellations), subscription_items (linked to current sub state), products, prices, coupons, promotion_codes. None of these are captured by `created[gte]` — a sub canceling today isn't a "new" object created today. All small enough that full re-pull each time costs seconds.
- **Subscription status filter:** we explicitly pass `status='all'` to Stripe. Without it, the API returns only `active` subs — which kills churn / cancellation analysis. Easy to miss; documented in the fetcher.
- **Registry (`data/stripe-registry.json`):** `{"objects": {<key>: {"last_synced_at", "last_sync_error", "row_count"}, …}}`. Committed and shared via git so teammates see the same sync history. Per-user API keys do the actual fetching.
- **Sync UX:** "Sync from Stripe" expander in the Data view. Per-object list with status (rows, last sync, errors) + a per-object Sync button + a "Sync all Stripe objects" button at the top. First-sync banner warns about the 10–25 minute initial pull. `st.status` panels show live progress (running fetch count) during long syncs.
- **Sync-on-app-start toggle** in Settings → Integrations → Stripe. Gated so it only fires when (a) toggle is on, (b) API key is set, AND (c) at least one object has been synced before. The last gate prevents accidentally triggering a 25-min initial sync at startup — the toggle is a no-op until you've done the first manual sync.
- **Per-row UI in the file list** unified with Google Sheets sync: Stripe-synced files get the same staleness icon / caption / Sync button pattern as Sheets-synced files. The file editor blocks edits with a "Stripe is the source of truth" banner. The Delete-CSV flow drops the registry entry for either source.
- **Lazy SDK import:** the `stripe` package is imported on demand inside `stripe_sync.py` functions, not at module top. If a user hasn't run `pip install -r requirements.txt` since this dep was added, the rest of the app still boots; the Settings / Data view shows a clean "install deps" message instead of crashing.
- **Typed errors** (`NotConfiguredError`, `DepsMissingError`, `AuthError`, `StripeSyncError`) plus a `_humanize_stripe_error` helper that translates Stripe SDK exceptions (`AuthenticationError`, `PermissionError`, `RateLimitError`, `APIConnectionError`) into user-friendly one-liners.
- **Storage size at real scale:** ~150–300 MB of CSVs total for a ~25k-sub account (twelve CSVs; invoice line items are the biggest single addition at roughly the same row count as charges). Pandas reads this in 5–15 seconds; queries run in well under a second. Comfortably within pandas territory; no need for Parquet / DuckDB yet.
- **Out of scope (v1, may revisit):** Stripe Events API for historical state reconstruction (current data model gives current state, not point-in-time history); webhooks for push-based sync; mid-object resumability (a failed sync re-runs from scratch — idempotent thanks to dedupe-on-id but costs the API quota); balance transactions (per-charge / per-payout fee detail — pullable but very high volume); payment methods per customer (one query per customer = N queries, slow); tax / tax IDs (only relevant if you use Stripe Tax); Stripe Connect objects (Accounts, Transfers, Application Fees).

### Table scope picker
- Compact `st.popover` at the top of the Ask the Snoop view, labelled "Working with N of M data files".
- Inside: checkbox per CSV in `data/tables/`, plus "Select all" / "Clear all" buttons.
- `_current_table_scope()` returns `None` when everything is checked (no filter — agent sees all tables) or a list of sanitized variable names otherwise. Source of truth is each checkbox's `_scope_chk_<name>` session_state key, defaulting to True.
- The selected scope drives three things in lockstep: which CSVs are loaded into the run_python sandbox, which CSVs appear in `build_tools()`'s description text, and which doc-related context docs make it into the system prompt.

### Context view
- Top-level `Context` view in the sidebar (between Data and Saved Questions). Implemented in `context_browser.py`.
- Lists every general doc in `data/context/*.md` (root only — `tables/` is intentionally not surfaced here). Each row has **View / Edit (AI) / Delete** action buttons.
- View opens a Markdown viewer with a View/Edit radio for plain-text editing; Edit opens the AI context-chat in **general mode** (no fingerprint, no run_python, sees other general docs as reference). Delete uses `confirm_or_run`.
- "Create a new general context doc" expander accepts a filename → opens the AI editor in general mode with a kickoff message ("introduce yourself, ask what this doc should cover, then draft a first version").
- Folder README rendered at the top as a collapsed expander.

### Prompt caching
- The system prompt (static behavior + context docs) is sent as a single content block with `cache_control: {"type": "ephemeral"}`.
- After the first request, subsequent turns within a ~5-minute window read from cache, which roughly halves cost per turn for long context.
- Cache auto-invalidates when system prompt content changes (e.g. a context doc is edited).

### Pinned chat input
- `st.chat_input` lives at the top level of the script (after the view switch), which makes Streamlit pin it to the bottom of the viewport — same UX as ChatGPT / Claude.ai.
- When the user submits, the prompt is stashed in `st.session_state.pending_prompt`, the active view is set to Chat, and the script reruns; the Chat view picks up the pending prompt and runs the agent loop.
- The chat input is visible regardless of which view is active. Submitting from any view auto-switches back to Chat so the response is visible.

### Sidebar layout
- Logo lives in Streamlit's dedicated `st.logo()` slot above the sidebar content area (see "Brand identity" below).
- **Primary nav**: `Ask the Snoop` leads, then a divider, then `Data`, `Context`, `Saved Questions`, `Saved Items`. The "talking" surface is separated from the "configuring / browsing" surfaces so the eye lands on it first.
- **Context-sensitive actions** (only when the active view is `Ask the Snoop`): `Clear chat` (confirms before wiping) and `Stop` (visible once a conversation is in progress).
- **Utility nav** (below a divider): `Charts demo`, then the `Settings` button which opens a modal.
- **Always-on toggles** below another divider: `Quick mode` (force Haiku for next request) and `Prefer charts` (nudge model to chart whenever the answer has 3+ comparable values).
- **Session usage meter** at the bottom: calls / tokens / cost estimate. Resets when Clear chat is clicked.
- Active nav button is `type="primary"`; the others default.
- Clicking a button calls `_switch_view` via `on_click`, which sets `st.session_state.view` and triggers a rerun.
- View constants (`VIEW_CHAT`, `VIEW_DATA`, `VIEW_CONTEXT`, etc.) in `app.py` are used both as labels and as session_state values, so the auto-switch on chat submit can reference them safely.
- Dividers in the sidebar have asymmetric vertical margin (`margin: 0.85rem 0 1.4rem 0`) — extra padding below so the next section doesn't crowd the line.
- Emojis are stripped from view labels and action buttons. Material Icons (via `:material/icon:`) are used everywhere.

### Settings view
- Lives as its own top-level view (`VIEW_SETTINGS`) in the sidebar utility group, alongside Charts demo + Integrations. Used to be an `@st.dialog` modal; outgrew it when the control count hit ~10 settings + the integration blocks.
- Contains three sections, separated by dividers:
  1. **AI** — API key, Chat model, Utility model, Temperature, Max-output-tokens.
  2. **Appearance & behavior** — Confirm-destructive toggle.
  3. **Save settings** (primary) + **Forget saved**.
- Integration-related settings (Google Sheets connection, Stripe API key, sync-on-start toggles) live on the separate **Integrations** view — see below.
- Uses an explicit Save button rather than auto-save-on-change. Auto-save was unreliable for `st.text_input` (the API-key field) because Streamlit only commits the value on blur — switching views without first clicking elsewhere would lose the key.
- Save fires `st.toast("Settings saved.")` since the dialog-close-as-feedback affordance is gone. The page itself reruns and re-reads the freshly-persisted values.
- Switching views without clicking Save discards any in-form edits — Streamlit doesn't carry widget state across views unless we explicitly persist it, and we don't here (deliberate, matches the dialog's prior behaviour).

### Integrations view
- Standalone top-level view (`VIEW_INTEGRATIONS`) sitting just above Settings in the sidebar utility group. Broke out of the Settings page because (a) it has a distinct mental model — wiring up *data sources*, not configuring app *behaviour* — and (b) the section was already pushing the Settings page long, with more integrations planned (FastSpring, Freemius, Lemon Squeezy).
- Currently houses **Google Sheets** (OAuth Connect / Disconnect / Test + sync-on-start toggle) and **Stripe** (restricted API key field + Test connection + sync-on-start toggle). Future integrations slot in here.
- Per-source action buttons (Connect, Disconnect, Test) fire immediately on click — they write to disk or hit a remote API right then. The bottom **Save integrations** button handles only the things that don't have side effects on click: toggle states + the Stripe API-key text input.
- Both helper functions used by this view (`_render_google_settings_section`, `_render_stripe_settings_section`) are kept in `app.py` rather than moved into the respective sync modules — they reference session-state defaults defined here and the surrounding view's Save button, so keeping them adjacent reads more cleanly.

### Context-sensitive sidebar actions
- "Clear chat" only renders in the sidebar when the active view is Chat. On other views it's hidden.
- "Forget saved settings" lives on the Settings view, not the sidebar, since it's a settings concern.

### Data view
- Implemented in `data_browser.py`. Single-purpose view — just CSVs in `data/tables/`. (Context docs got promoted to their own top-level view; see "Context view" below.)
- Lists every `.csv` in `data/tables/` as bordered rows with **View / Context / Delete** action buttons. View opens the file in `st.data_editor` for in-place row/cell edits → `Save changes` writes back. Context launches the AI context-chat for that file's doc-related context doc (`data/context/tables/<stem>.md`). Delete uses `confirm_or_run`.
- CSV upload at the top via an expander: drop a file → setup dialog (rename suggestion + "Use AI to build the context doc" toggle) → saves into `data/tables/` and optionally opens the AI context-chat to draft the matching `tables/<stem>.md` doc.
- Folder README rendered at the top as a collapsed expander.
- All saves are immediate writes to the file. There's no undo.

### Visual theme (Stripe-modern)
- Light, professional look reminiscent of Stripe's product surfaces.
- Streamlit theme in `.streamlit/config.toml`: white background, off-white sidebar (`#F6F9FC`), charcoal-navy text (`#1A1F36`), Stripe purple primary (`#635BFF` — placeholder until brand color is locked in).
- `theme.py` centralises all styling concerns: Plotly template, CSS overrides, Material Icon map, and the sidebar header renderer. Edit one file to retune the look.
- Every chart Snoop produces uses the same Plotly template, so AI charts match the demo charts automatically.
- All emojis stripped from the UI. Replaced with Material Icons via Streamlit's `:material/icon:` syntax — rendered as SVG inline, no asset bundling needed.
- CSS overrides tightening button radius (6px), sidebar wordmark sizing, expander styling.

### Non-streaming agent loop
- The agent loop calls `client.messages.create()` once per iteration and renders the full response after it arrives. We tried streaming briefly but rolled it back; non-streaming keeps the rendering code simpler and tool calls feel coherent (text + code + result all appear together as one "step").
- A "Thinking…" spinner shows while the API call is in flight; "Working…" on follow-up iterations.

### No tool-iteration cap
- The agent loops until the model returns `end_turn` or the user clicks Stop. No hard limit.
- Used to be a Settings dialog field (max 20), then a hard-coded constant (8). Both removed.
- The Stop button + the user-visible status panel give enough control to abort a runaway loop. The natural upper bound is Anthropic's context window — once history fills, the API errors out and we surface it.
- If pathological "error → retry forever" loops become a real cost issue later, reintroduce a generous safety cap (say 50) — there's a comment in `app.py` marking the spot.

### Configurable sampling: temperature + max output tokens
- **Temperature** — slider 0.0–1.0, default 1.0. Lower values make responses more deterministic (same question → same answer), which helps when finance users care about reproducible numbers. Stored in `st.session_state.temperature`.
- **Max output tokens** — number_input 256–16384, default 4096. Lower caps cost and forces brevity; higher allows long-form reports. Stored in `st.session_state.max_output_tokens`.
- Both persist to `~/.snoop-doc/settings.json` and are passed to `client.messages.create()` per call.
- Loaded values are clamped into their allowed ranges on startup so a corrupted/hand-edited settings file can't break the API call.

### AI-assisted context-doc editor (dual mode)
- `context_chat.py` opens a modal chat dialog in one of two modes:
  - **Table mode** — opened from the Data view's per-file Context button or after a CSV upload (if "Use AI" is on). Edits the doc-related context for one CSV; saves to `data/context/tables/<stem>.md`. The AI has both `run_python` (CSV-scoped sandbox, `df` aliased) and `update_context_doc`. System prompt includes a comprehensive **data fingerprint** + the "three required sections" guidance (loading recipe, business semantics, aggregation traps) + all general docs as read-only reference.
  - **General mode** — opened from the Context view's per-file Edit button or its "Create new general doc" flow. Edits a general context doc (`data/context/<name>.md`). The AI has only `update_context_doc` (no run_python — there's no data file to analyse). System prompt has different guidelines focused on clarity, brevity, and watching for conflicts/duplication with other general docs.
- Mode is tracked via `_ctx_chat_mode` ("table" / "general") + `_ctx_chat_target` (filename). Public entry points: `open_for_existing(csv)`, `open_for_new_upload(csv)`, `open_for_general_existing(md)`, `open_for_general_new(md)`.
- The dialog has two halves: an **editable doc preview** at the top, a **chat history + input** below. The doc updates in real time as the AI calls `update_context_doc`; manual edits to the preview are sent along with each next AI turn so they're never silently overwritten.
- **Data fingerprint** (table mode only) is built fresh per turn by `data_fingerprint.py`: shape, per-column stats over the whole file (dtype, nulls, uniques, samples), head + tail + random middle row samples, and anomaly notes (mixed currency symbols, parenthesised negatives, sentinel values like "N/A" or "TBD").
- **Sandbox is built ONCE per chat turn** in table mode (matches the main agent's pattern) so state persists across `run_python` calls within a turn. `csv_path` is exposed as a global so the model can recover when auto-load fails on a non-standard encoding/delimiter.
- Empty-response and max-iteration cases surface visible notes in the chat instead of silently stalling.
- Save writes the doc to disk; Cancel discards. Chat history is *not* persisted across opens — each session starts fresh.

### Utility model setting
- Settings dialog now has two model dropdowns:
  - **Chat model** (was "Model") — drives Ask the Snoop. Default Sonnet.
  - **Utility model** — drives the context-chat agent and any future AI-helper features. Default **Haiku** (focused tasks, much cheaper).
- Stored separately in `~/.snoop-doc/settings.json` as `model` and `utility_model`.

### Agent memory (persistent across conversations)
- New module `memory.py`. Exposes `save_memory(content, category, rationale)` as a Claude tool — the agent calls it during chat to persist durable facts that should outlive the current conversation. The file it writes to (`data/context/memory.md`) lives in the general-context-docs directory, so it's auto-loaded into the system prompt on every turn via the existing `_load_context_docs()` — no special integration needed.
- **Three categories** the agent can save into: `fact` (something true about the company / data — fiscal calendar, naming conventions, term definitions), `preference` (how the user wants the agent to behave — currency rounding, chart style), `correction` (a rule that overrides default behaviour — "PR retainer isn't part of marketing spend"). Each entry stores a *Why:* rationale alongside the content so future-you (and future-Snoop) can judge whether it still applies.
- **Triggers** are spelled out in the system prompt. The agent saves when the user uses durable-intent language (*remember*, *next time*, *always*, *from now on*), corrects something that generalises, or defines a new term. Explicit "don't save" list covers ephemeral values, question-specific context, and vague impressions. The agent is also instructed to check for duplicates before saving — it would update an existing entry conceptually rather than add a near-duplicate.
- **Visibility:** every save shows up as an `✎ Saved to memory ({category}) — {content}` caption in the chat (same pattern as the context-doc editor's `update_context_doc`). Users can edit `memory.md` directly from the Context view, treat any entry as deletable, or wipe the file entirely — the format is plain markdown.
- **File format:** chronological log grouped by date heading (`## 2026-05-16`), each entry a bullet with category tag + content + *Why:* line. Same-day saves group under the existing date heading; first save on a new day adds a fresh heading.
- **Why this works:** the agent reads memory entries via the system prompt (general context auto-load) and only adds new ones via the tool. The cycle is closed — what the agent saves on turn N, it sees on turn N+1.
- **Mitigations against bad entries:** visibility (catches misclassifications fast), the *Why* field (entries can be audited), the explicit do/don't lists (the agent self-restricts), and the user retains full edit access to the file. Not implemented: hard cap on memory size, `/verify-memory` audit command — both noted as follow-ups if memory bloat becomes real.

### Web tools (search + fetch)
- Two Anthropic server-side tools wired into the agent's tools list: `web_search` (toggleable per-conversation via the chat header) and `web_fetch` (always on). Both are server-executed — Anthropic runs them and returns the results inline in the same response, no client-side handling needed beyond preserving the blocks in the message history and rendering them in the chat.
- **Toggle for web search** sits next to Quick mode and Prefer charts at the top of the chat view. Default off (costs extra per request, most finance questions are answered from internal data). The setting (`web_search_enabled`) persists to `~/.snoop-doc/settings.json` via the existing `on_change=save_settings` pattern.
- **Web fetch is always available** — cheap, scoped (model needs a specific URL to fetch), and unlocks "look up the page the user just mentioned" without the user having to flip a toggle for one-off lookups.
- **Tool type identifiers** (Anthropic versioned tool types) live as constants at the top of `app.py`: `ANTHROPIC_WEB_SEARCH_TOOL` and `ANTHROPIC_WEB_FETCH_TOOL`. Bump the date suffix in `type` when Anthropic releases new versions.
- **Rendering** in chat:
  - `server_tool_use` blocks render as captions — `:material/search: Searched the web for: "<query>"` or `:material/language: Fetched: <url>`.
  - `web_search_tool_result` / `web_fetch_tool_result` blocks render as a collapsed expander with the source URLs as clickable markdown links (capped at 10 to keep the chat tidy).
- **Message-history preservation:** the response-conversion loop in `run_agent_turn` now uses `block.model_dump()` as a fallback for any block type we don't hand-map (text + tool_use stay hand-mapped). This means server-tool blocks round-trip back to Anthropic on the next turn intact — required for multi-turn conversations where the model references prior search results.
- **System prompt guidance:** the agent is told to use web tools for *external* info only (exchange rates, benchmarks, news, regulations) — not for anything answerable from internal CSVs — and to **cite sources** in the response text when it does (URL or publication name) so the user can verify.

### Save-as-report (Saved Items Phase 2)
- New module `reports.py` — a pure function `render_exchange_to_html(question, assistant_blocks, figures, tables, report_name)` that produces a self-contained HTML document for one chat exchange. No Streamlit imports; caller hands in the data.
- **Self-contained** means Plotly.js is inlined ONCE at the top of the document (via `pio.get_plotlyjs()`), and each figure is a lightweight `<div>` (`include_plotlyjs=False`). Result: one bundled JS chunk, multiple cheap figure divs, ~3 MB total per report. Opens offline, forwards via email, looks identical in every browser. Trade-off accepted because reports are artefacts you forward, not chat scrollback.
- **Content flow:** the renderer walks the assistant block list in order. `text` blocks → markdown-converted via the `markdown` package (with `fenced_code`, `tables`, `sane_lists` extensions); `tool_result` blocks → look up the matching figure / table via `tool_use_id` and embed them inline (mirrors how the chat UI re-renders history from `st.session_state.figures` / `tables`); `tool_use` blocks → skipped (they're the model's internal `run_python` invocations, not narrative content). Tool stdout / error text is also skipped — the model has already incorporated the relevant numbers into its text blocks.
- **Styling:** matches Snoop's Stripe-modern aesthetic — a centered 880px card on a light grey backdrop, indigo accents, the question rendered as a blockquote-style block at the top, tables with subtle hover rows. CSS is inlined in the document head. The report opens looking like a finished artefact, not a debug dump.
- **Table truncation:** the renderer caps tables at 500 rows with an "(Showing first 500 rows.)" note. Anything bigger probably belongs in a saved Table CSV, not a forwarded HTML report.
- **Save flow:** every completed assistant exchange (live + in history) gets a **Save as report** button next to the existing Copy button, via the new `_render_exchange_action_row` helper in `app.py`. Click → `saved_items.show_save_report_dialog` opens with a name input (defaulted to the first 80 chars of the user's question), a content summary ("N text blocks · M charts · K tables"), and Save / Cancel. Render only happens at Save time — no point building a 3 MB string for a click that gets cancelled.
- **Storage:** `saved/reports/<id>.html` + `<id>.meta.json`. Uses the same `_write_entry` helper as charts/tables, so soft-delete / restore / permanent-delete / rename all work without special-casing.
- **View flow:** the Reports sub-tab in Saved Items lists reports newest-first with **View / Rename / Delete / Download** actions. View toggles an inline iframe (`streamlit.components.v1.html`) at fixed height 720 with internal scrolling — the report's own `<style>` and `<script>` don't leak into Streamlit's DOM. Download is `st.download_button` serving the raw `.html` so you can email or archive it.
- **Out of scope (deferred to Phase 3):** "Continue in chat" to load a saved report back into a new conversation; multi-exchange reports; AI-generated titles; report templates / pre-formatted board-deck layouts.

### Save chat (resumable conversations)
- Fourth Saved Items type alongside charts / tables / reports. The whole chat thread — every user + assistant turn, every Plotly figure, every DataFrame the agent produced — is serialised to a single JSON file at `saved/conversations/{id}.json`.
- **Distinct from reports** because saved conversations are *resumable*. Clicking **Resume** loads the messages back into `st.session_state.messages`, the figures + tables back into `st.session_state.figures` / `.tables`, and routes the user to the chat view to continue. Reports are static HTML artefacts; conversations are stateful continuations.
- **Save trigger:** sidebar "Save chat" link, sits between Clear chat and Stop. Visible only when the active view is **Ask the Snoop** AND there's a non-empty message history. No auto-save — saving is a deliberate "this is worth keeping" action so the list doesn't bloat with throwaway sessions.
- **Save dialog content summary** shows what's about to be packaged ("3 questions · 7 assistant response block(s) · 2 charts · 1 table") so the user knows what they're committing to. Default name is the first user question, trimmed to 80 chars.
- **Referenced-artefacts-only:** `save_conversation` walks the messages, collects every `tool_use_id` that appears in a `tool_result` block, and only saves figures / tables matching those IDs. Orphan artefacts left over from prior chats that were never wired into a response don't get carried over — keeps the saved file lean.
- **Serialisation:** figures via `pio.to_json` (Plotly's lossless JSON round-trip); tables via `df.to_json(orient="split")` (preserves column order, dtype-tolerant). Both deserialise back to real Figure / DataFrame objects on load — Streamlit's session_state expects the live types, not their string forms.
- **Resume UX:** if there's an active chat in flight, a confirmation dialog warns "Resuming will replace your current chat" — `confirm_or_run` honours the global confirm-destructive setting. Without active messages, Resume just loads + routes silently.
- **View action** (read-only preview) renders the messages in `chat_message` bubbles inside a modal — text blocks, charts (`st.plotly_chart`), tables (`st.dataframe`). No copy / save buttons, no agent iteration logic — just the conversation as it was when saved. Lighter version of `render_history`.
- **Card preview** shows the first user question as a quote-styled block + a turn count caption. Makes it scannable to find "the Q3 deep-dive" among many saved conversations.
- **Out of scope (v1, may revisit):** AI-generated titles (right now the default is the user's first question, but those can be long / contextless); multi-conversation merging; conversation forking (branching from a midpoint); shared / team-wide conversations behind auth.

### Saved Items (Phase 1: charts + tables)
- New view `VIEW_ITEMS = "Saved Items"` at the end of the primary nav (after Saved Questions).
- Storage: `saved/` at the project root (`charts/`, `tables/`, `reports/`, `_trash/`). Each entry is one content file (`.json` / `.csv` / `.html`) + a `meta.json`. No versioning — Save creates a new entry; the dialog offers a "Replace existing" toggle on name collision.
- New `result_table` convention: when Claude's code assigns a DataFrame to `result_table`, the app captures it (alongside the existing `fig` capture) and renders it inline as a real DataFrame with a Save button. Mentioned in `prompts/system.md` so the model uses it instead of `print(df)` for save-worthy tables.
- Save buttons live next to every rendered chart and table in chat — click → modal with name + collision-aware Replace toggle.
- The Saved Items view has sub-tabs Charts / Tables / Reports / Trash. Each entry has View / Rename / Delete actions. Tables additionally have **"Use as data"** which copies the latest CSV into `data/tables/` so the agent can query the saved analysis as source data on its next tool call.
- Delete is soft → moves to `saved/_trash/<type>/` with a `deleted` timestamp added to the meta. The Trash sub-tab lists trashed items with Restore and Delete-forever actions. No auto-purge yet.
- Saved Items are shared across everyone using the checkout (project-level storage). Per-user / per-group access control is a TODO — see the build TODO list.

### Saved questions
- View `VIEW_SAVED` ("Saved Questions") sits between Context and Saved Items in the primary nav.
- Storage: `~/.snoop-doc/saved_questions.json` (separate from settings so it can be wiped independently). Each entry is `{"id": uuid, "text": str}`.
- The page lets users add, edit (inline), and delete saved questions. Delete honours the global "confirm destructive actions" setting.
- Below the chat input, a "Saved questions" dropdown appears (only when there are any). Selecting an entry queues it as the next prompt — same path as typing in the chat input.
- Dropdown uses a counter-based widget key so each selection produces a fresh widget on the next rerun, which both resets the displayed value and lets the user pick the same question twice in a row.

### Stop button
- Appears in the sidebar when there's an active conversation, only on the Ask the Snoop view.
- Sets `st.session_state.stop_requested = True`. The agent loop checks this flag between iterations and exits cleanly if it's set.
- Note: doesn't interrupt an in-flight API call (Streamlit is single-threaded). Useful for halting a runaway multi-iteration loop, not for cancelling a single slow call. The button label / help text makes this explicit.

### Copy response button
- Small unobtrusive "Copy" button at the bottom of each completed assistant exchange, both live (after a turn finishes) and in history.
- Implemented with `st.html` + an inline `navigator.clipboard.writeText` call. The payload is JSON-encoded so any text — including quotes, newlines, unicode — embeds safely as a JS string literal.
- Copies only the assistant's *text* content (markdown), not the code/tool-result blocks. Designed for pasting analyst output into emails or Slack.

### Confirmation before destructive actions
- Toggle in the Settings dialog, default **on**. Disables the "Are you sure?" dialogs for users who prefer one-click destruction.
- Helpers live in `dialogs.py` (not `app.py` — see the "don't import from app.py" constraint below): `_show_confirm_dialog` (a `@st.dialog`) and `confirm_or_run(message, label, callable)`. Destructive buttons call `confirm_or_run` which routes through the dialog or just runs the action directly based on the setting.
- Applied to: Clear chat, Delete CSV, Delete context doc (both general and doc-related), Forget settings, Delete saved question, Delete saved item, restore-or-purge-from-trash.

### Data view: upload + delete (no in-app rename)
- **CSV upload** at the top of the Data view inside a collapsed expander. Drop a file → setup dialog (rename suggestion + "Use AI to build the context doc" toggle) → CSV lands in `data/tables/`. If AI is on, the context-chat opens to draft the matching doc-related context.
- **Delete** button per file. Goes through `confirm_or_run` so it respects the confirmation toggle.
- **No in-app rename.** Users pick a clean filename at upload time. For files already in the project, rename on the filesystem.
- **Context docs are managed elsewhere.** General docs (`data/context/*.md`) live in the new Context view; doc-related docs (`data/context/tables/<stem>.md`) are reached via the per-CSV Context button in the Data view. The old Markdown-upload affordance on the Data view is gone.

### Prefer charts toggle
- Sidebar toggle (`st.toggle`) right below Quick mode, default **on**.
- When on, `load_system_prompt()` appends a "Chart preference" section instructing Claude to produce a chart whenever the answer involves 3+ comparable values — even if the user didn't ask explicitly.
- When off, falls back to the implicit behavior: charts only when "chart"/"plot"/"show me"/etc. appears in the question.
- Stored in `st.session_state.prefer_charts`, persisted to `~/.snoop-doc/settings.json` via an `on_change=save_settings` callback — toggles commit immediately on click (unlike text_input), so auto-save is reliable here.

### Quick mode (Haiku override)
- A toggle in the sidebar. When on, the next request goes to `claude-haiku-4-5` regardless of the model in Settings.
- Cheaper (~⅓ the cost of Sonnet) and faster (~2× the speed). Best for lookups: "what was revenue in March?", "show me top 5 products".
- Persists across requests until toggled off.

### Token + cost meter
- `session_state.usage` tracks running totals: API calls, input tokens (uncached + cache writes + cache reads), output tokens, cumulative cost in USD.
- `add_usage(model, usage)` is called after every API response with the `.usage` block from the SDK. Computes cost using `PRICING_PER_MTOK` in `app.py`.
- Sidebar shows live totals once the first call lands. Resets when Clear chat is clicked.
- Pricing table assumes regular input × 1.25 for cache writes, regular output rate, and ~10% input rate for cache reads. Update the table in `app.py` if Anthropic prices change.

### Parallel tool calls
- `prompts/system.md` instructs Claude to issue multiple `run_python` calls in a single response when the work is independent (different tables, different metrics) rather than serialising them.
- The agent loop already handled multiple `tool_use` blocks per response — only the prompt needed updating.

### Brand identity in the sidebar
- Full wordmark PNG (`assets/logo.png`) goes into Streamlit's dedicated sidebar logo slot via `st.logo()` — the official "logo space" above the sidebar's user content area.
- `theme.install_logo()` calls `st.logo()` once at startup; falls back to the older symbol-only SVG (`assets/logo.svg`) if the PNG isn't present.
- The tagline ("Ask questions about your company's data.") sits in the sidebar's user content area, below the logo slot, as a plain `st.caption`.
- No custom HTML or data-URI encoding needed anymore — `st.logo()` handles all of that.

---

## Constraints & limits

- **No tool-iteration cap.** The agent runs until the model returns `end_turn` or the user hits Stop. Was briefly an editable setting (max 20), then a constant (8); now uncapped. Natural upper bound is the model's context window — fills up after many iterations and the API errors out, which we catch.
- **`max_tokens=4096`** per Claude response. Enough for big tables + charts; bump if we start cutting off long reports.
- **Sandbox is unrestricted Python `exec`.** Fine for a local single-user demo; not safe to expose publicly without sandboxing.
- **API key stored as plaintext** in `~/.snoop-doc/settings.json`. File is chmod 600 on Unix. Acceptable for a single-user dev machine; needs encryption if we go multi-user.
- **Demo data only** so far — Claude knows to flag this in its answers.
- **No streaming during tool use.** Each tool iteration shows a spinner. Streaming returns once data loading is stable.
- **No authentication.** Anyone with access to the running app can use it. Fine for local demo.
- **Plotly only** for charts. No Altair / Matplotlib / Vega-Lite output for now.
- **Anthropic-only AI backend.** We explored multi-provider (Anthropic + OpenAI + Gemini via LiteLLM) and rolled it back — keeping the agent loop on the native Anthropic SDK preserves prompt caching and avoids LiteLLM's tool-call routing edge cases. If we revisit multi-provider, do it as a parallel code path rather than a unified abstraction.
- **`df` alias for single-table case.** When exactly one CSV is loaded, the table is also exposed as `df` in the tool sandbox. Models reach for `df` reflexively; the alias prevents `NameError`-then-retry loops with sanitized filenames like `p_l_2026_p_l_2026`. Skipped when multiple tables are loaded (would be ambiguous).
- **Streamlit toolbar in "minimal" mode.** Disables Streamlit's developer keyboard shortcuts (C clears cache, R reruns). Without this, Ctrl+C in the page area can fire the cache-clear confirmation instead of copying selected text. Set in `.streamlit/config.toml`.
- **Settings dialog uses explicit Save**, not auto-save-on-change. Auto-save lost data when the user closed the modal without first blurring the text field — Streamlit's text_input only commits its value on blur or submit. Explicit Save is a small UX cost for reliability.
- **Pricing table is hard-coded.** Lives in `PRICING_PER_MTOK` in `app.py`. Update when Anthropic changes prices. Could be moved to a config file later if we want non-developers to edit.
- **Don't import from `app.py` in submodules.** `app.py` is the entry point (`streamlit run app.py`) so Python loads it under `__name__ == "__main__"`. A subsequent `from app import X` in another module causes Python to load `app.py` *again* as a separate module — re-running every top-level statement, including all the sidebar widgets. That throws a `StreamlitDuplicateElementKey`. Shared helpers used by both `app.py` and views must live in their own modules (currently: `dialogs.py`, `theme.py`, `tools.py`).
- **Data / Context view saves are immediate and unversioned.** Editing a CSV or markdown file overwrites it. Use git for safety if working with anything you can't easily reconstruct.
- **`st.data_editor` is best below a few thousand rows.** Browser slows down beyond that. Fine for our P&L tables; might need pagination for transaction-level data later.
- **Chat input is global.** Lives outside the tabs so it can pin to the viewport bottom; visible on every tab. Submissions always route into the Chat tab.

---

## Build TODOs (next features)

Roughly in priority order.

- [x] **Wire in real data.** ✓ `tools.py` reads CSVs from `data/tables/` and exposes them as DataFrames with sanitized names. Falls back to demo data if folder is empty.
- [x] **Inject context docs into system prompt.** ✓ General docs (`data/context/*.md`) always loaded; doc-related docs (`data/context/tables/<stem>.md`) loaded per the table-scope picker. Prompt caching via `cache_control: ephemeral` is on.
- [x] **File upload UI.** ✓ Drag-drop CSV uploader in the Data view, with a setup dialog (rename + optional AI context-doc draft).
- [ ] **Auto-summary of available tables.** On app load, show "I can see: pnl_company.csv (24 rows, 7 cols), salaries.csv (..." so users know what's queryable.
- [x] **Visual redesign — first pass.** ✓ Stripe-modern direction: light theme, indigo primary, charcoal text, off-white sidebar. Emojis replaced with Material Icons. Theme lives in `.streamlit/config.toml` + `theme.py`.
- [x] **Chart theme / branding.** ✓ Plotly template defined in `theme.py` and applied globally — every chart (demo + AI-generated) uses the same palette and typography.
- [x] **Logo in sidebar.** ✓ `assets/logo.svg` is rendered next to the "Snoop Doc" wordmark via `theme.render_sidebar_header()`.
- [x] **Export.** ✓ Excel-download on any result table in chat + Saved Items. PDF-download on saved reports (text + tables; charts are HTML-only). Per-row CSV download + Export-all-as-zip on the Data view header. PNG-only chart export still pending.
- [x] **Saved questions.** ✓ Add/edit/delete in the Saved Questions view; quick-pick dropdown above the chat input on the chat view.
- [ ] **Better error UX.** When the model can't answer (missing data, ambiguous question), give a structured "here's why" response.
- [ ] **Anomaly scan.** "Show me anything unusual in the last month" runs a sweep across tables.
- [x] **Create / delete files from the data view.** ✓ CSV upload + per-file Delete in the Data view; general-doc create/delete in the Context view. Confirmations honour the global toggle.
- [ ] **Cache invalidation on save.** Saving a CSV in the Data view should drop any cached DataFrame so the next AI tool call re-reads from disk. (Currently `tools.py` re-reads on every call, so the cache concern is theoretical until we add caching for speed.)
- [x] **Google Sheets live sync.** ✓ OAuth via Workspace-internal Desktop client, per-user tokens, shared sync registry, per-row + sync-all + sync-on-start. Read-only enforcement on synced CSVs.
- [x] **Stripe live sync.** ✓ Restricted API key, thirteen CSVs (customers / subs / sub_items / invoices / invoice_line_items / payment_intents / charges / refunds / disputes / payouts / products / prices / coupons / promotion_codes), initial full pull + incremental refresh via `created[gte]`. Sync registry + same per-row UI as Google Sheets. Sync-on-app-start toggle gated to "only after first manual sync" so the 25-min initial pull never surprises anyone at launch. On 2026-05-18 we briefly swapped Charges for PaymentIntents on the assumption that modern accounts no longer populate `Charge.list` — turns out both have data on the test account (411 PIs / 284 Charges), and the two views differ in useful ways (Charges has roll-up fields PI doesn't), so we kept both.

- [x] **Stripe Balance Transactions.** ✓ Added as `stripe_balance_transactions.csv` (the 15th Stripe table). One row per money-movement event Stripe records on the account balance — charges, refunds, fees, payouts, adjustments, contributions, application_fee earnings. Crucial for multi-currency reporting because `amount`, `net`, and `fee` are always in the account settlement currency regardless of the source object's currency (Stripe does FX conversion at transaction time, so no local FX table is needed). `source` joins to charges / refunds / disputes / payouts via the `ch_…` / `pyr_…` / `du_…` / `po_…` id prefix. Synced incrementally, sits last in `SYNC_ORDER` since it references every preceding money-moving table.

- [ ] **FastSpring live sync.** Adds FastSpring as a parallel sync source to Stripe — same `<platform>_sync.py` template. Auth: HTTP Basic Auth (API username + password from FastSpring dashboard → Integrations → API Credentials). Endpoints to pull, mapping to roughly:
  - `/accounts` → `fastspring_accounts.csv` (customers)
  - `/subscriptions` → `fastspring_subscriptions.csv`
  - `/orders` → `fastspring_orders.csv` (one transaction; merges Stripe's invoice + charge concepts)
  - `/orders/{id}/items` (or nested on the order) → `fastspring_order_items.csv` (product-level breakdown for multi-product orders)
  - `/returns` → `fastspring_returns.csv` (refunds — FastSpring is merchant of record, so refunds flow through them)
  - `/products` → `fastspring_products.csv`
  - `/coupons` → `fastspring_coupons.csv`
  Unique value vs. Stripe: **tax info per order** (FastSpring handles global VAT / sales tax, useful for compliance reporting); **localized pricing** (multi-currency on each product); **payment method per order** (regional methods Stripe doesn't surface). Quirks to verify on real data: subscriptions use "next charge date" semantics not Stripe-style `current_period_end`; orders are paginated by `page` parameter, not cursor; date filters use ISO strings, not Unix timestamps. Effort: ~70% of the Stripe build (smaller data model, the registry/UI/sync-orchestration pattern is already in place to copy).

- [ ] **Freemius live sync.** Especially relevant for the WordPress plugin business — Freemius is the canonical billing + licensing platform for WordPress plugins/themes. Auth: signed requests with public key + secret key, plus a scope (`developer` / `plugin` / `user` / `install` / `site`). More involved than the others. Endpoints:
  - `/users` → `freemius_users.csv`
  - `/licenses` → `freemius_licenses.csv` (one row per license — different concept from Stripe customers)
  - `/sites` (sites the plugin is installed on) → `freemius_sites.csv`
  - `/installs` → `freemius_installs.csv`
  - `/subscriptions` → `freemius_subscriptions.csv`
  - `/payments` → `freemius_payments.csv`
  - `/events` → `freemius_events.csv` (Freemius surfaces a rich event stream — useful for plugin-specific lifecycle questions like "how many activations vs deactivations this month?")
  - `/plugins` → `freemius_plugins.csv` (catalog — for multi-plugin developers)
  Unique value: **license-per-site model** (a customer can have one license used across N sites — answers "are customers using their licenses?"); **install / activation data** (engagement signals Stripe has no concept of); **plugin upgrade paths** (free → pro conversion funnel). Quirks: the signed-request auth needs careful implementation (HMAC + timestamp + URL canonicalization — read their auth docs carefully); scopes are restrictive — start with `developer` scope and verify it returns everything we need. Effort: comparable to Stripe (data model is roughly the same size, but auth + WordPress-specific shape adds friction).

- [ ] **Lemon Squeezy live sync.** Auth: Bearer token (API key in `Authorization: Bearer <key>` header — simplest of the four). API follows the [JSON:API spec](https://jsonapi.org/) — responses are wrapped in `{data, included, links, meta}` and entities have `attributes` + `relationships`, which adds a parsing layer but is well-defined. Endpoints:
  - `/customers` → `lemonsqueezy_customers.csv`
  - `/products` + `/variants` → `lemonsqueezy_products.csv` + `lemonsqueezy_variants.csv` (variants = price points / plan tiers under each product)
  - `/orders` → `lemonsqueezy_orders.csv`
  - `/order-items` → `lemonsqueezy_order_items.csv`
  - `/subscriptions` → `lemonsqueezy_subscriptions.csv`
  - `/subscription-invoices` → `lemonsqueezy_subscription_invoices.csv`
  - `/license-keys` → `lemonsqueezy_license_keys.csv` (if licensing is used)
  - `/discounts` → `lemonsqueezy_discounts.csv`
  Unique value: also a merchant of record like FastSpring (handles tax globally); **license-key issuance built in** (similar to Freemius but channel-agnostic); **store-level reporting** (if you have multiple Lemon Squeezy stores). Quirks: JSON:API responses need a small unwrap helper (`item["attributes"][field]` not `item[field]`) — write once, use everywhere; pagination uses `page[number]` + `page[size]` query params; relationships between entities (e.g. subscription → customer) are referenced as `{type, id}` objects, not nested. Effort: ~80% of Stripe (smaller data model, but JSON:API parsing layer adds a touch).

- [ ] **Cross-source customer / revenue unification.** Triggered once two or more of {Stripe, FastSpring, Freemius, Lemon Squeezy} are wired up. Same customer email might appear in multiple platforms. Same product might be sold through two channels. Questions like "what's our total ARR across all platforms?" require knowing the rules: which platforms double-count, which don't, how to reconcile. This is a **context-doc problem first, code problem second** — write a `revenue_sources.md` general context doc explaining the rules ("Stripe handles direct enterprise; FastSpring handles EU/international; Freemius handles plugin marketplace; Lemon Squeezy handles X"); the agent can produce unified views in pandas at query time once it knows the rules. No new code needed unless the rules turn out to be too complex to express in prose, at which point we'd build a small `unified_revenue.csv` derivation step.
- [ ] **Swap Google OAuth from Desktop to Web app when deploying.** Triggered the moment Snoop runs anywhere other than the user's laptop — the current `InstalledAppFlow.run_local_server()` flow captures the redirect on `127.0.0.1:<port>` and breaks once the browser and the app aren't on the same machine. Required changes: (1) create a **Web application** OAuth client in GCP with a fixed HTTPS callback URL (e.g. `https://<host>/oauth/callback`); (2) rewrite `gsheets.connect()` as a two-step server-side auth-code flow — generate the Google auth URL, redirect the user, handle the callback via `st.query_params`, exchange code for tokens; (3) treat `client_secret` as *actually* secret (env var / secrets manager, NOT committed); (4) move per-user tokens from `~/.snoop-doc/google-token.json` into a per-user DB record keyed by the app's logged-in user; (5) add app-level authentication (Streamlit 1.42+ native OIDC or `streamlit-oauth`) so we know whose Google tokens to use. The chat / charts / context-doc code is unaffected — this is scoped to auth + token storage. Estimate: 2–3 weeks focused work. See "Deployment" notes in the Decisions log for the three deployment shapes considered.
- [x] **Saved Items Phase 2: reports.** ✓ "Save as report" button on every assistant exchange → renders the exchange (question + assistant markdown + Plotly figures + tables) to a self-contained HTML file in `saved/reports/`. View inline via iframe in the Reports sub-tab; Download button gets the raw `.html`. Rename + soft-delete via the standard saved-item actions.
- [ ] **Saved Items Phase 3: edit saved reports.** "Continue in chat" on a saved report loads its content into a new conversation as an assistant message; saving again creates a new entry.
- [x] **Save chat history (saved conversations).** ✓ Fourth Saved Items type alongside charts / tables / reports. Manual save via a "Save chat" sidebar link (gated on chat view + non-empty messages). Resume action loads the thread back into `session_state` and routes to the chat view. Figures + tables serialised (Plotly JSON / pandas `orient="split"`) inside the conversation JSON. Same Saved Items card grid + view dialog patterns as the existing types.
- [ ] **Access control for Saved Items.** Each item should support private (owner-only or group) vs. public (anyone). Needs auth + user identity first, then per-item ACL. Storage layer needs to know "current user" before this is possible.
- [ ] **Access control for `data/` files.** Same private / group / public model for `data/tables/` and `data/context/`. The current shared-filesystem approach assumes one team trusts each other.
- [ ] **AI-generated titles for saved items.** Originally planned, deferred — a Haiku call to suggest a short title for the content being saved, prefilled in the save dialog.
- [ ] **Versioning for Saved Items.** Currently each Save is a new entry; "Replace existing" overwrites. A future version-tracked model would keep history of each named entry.

### Maybe later
- [ ] User registration + per-user API keys (currently single-user).
- [ ] Spreadsheet sync (Google Sheets / Excel auto-pull on update).
- [x] **Slack bot wrapper.** ✓ `@snoop` mentions in any channel where the bot is invited. Socket Mode (no public URL needed). See `SETUP_SLACK.md`. Separate long-lived `slack_bot.py` process sharing `data/` + `data/context/` with the web app via the headless `snoop_agent.py` core. Plotly charts can't render in Slack — bot mentions the chart and points back to the web app.
- [ ] **Slack chart upload.** Render Plotly figures to PNG (e.g. via kaleido) and upload them to the thread so charts work natively in Slack.
- [ ] Teams bot wrapper (same `snoop_agent.py` core, MS Graph + Bot Framework instead of Slack Bolt).
- [ ] **Per-table force re-sync.** Today the only "force" path nukes every incremental cursor at once. A small icon button per row (next to Sync) would let users surgically re-pull just one table — useful after fixing a flattener or after correcting a single bad sync.
- [ ] **Group / folder organisation in the Data view.** With ~80 tables in the demo set, the flat row list gets long. Cleanest design: group by schema-doc pointer (files pointed at `pnl.md` get a "P&L" section, etc.), reusing the pointer system as the organisation signal. Files with no pointer fall into an "Ungrouped" group — a nudge to fill pointers. ~30 lines in `data_browser.py`; the pointer map is already computed per render.
- [ ] Scheduled email digests of saved questions.
- [ ] Audit log of every question + answer.
- [ ] Deploy to a real host (Vercel / Railway / Hugging Face Spaces).

---

## Decisions log

Quick record of choices we made and why, so we don't relitigate them.

- **Streamlit over Next.js.** Milica isn't a coder; Streamlit is dramatically faster to build and the demo doesn't need a polished frontend.
- **Local Python sandbox over Anthropic's hosted code execution.** Local lets us return live Plotly figures into Streamlit; hosted returns images/files which is more plumbing.
- **DataFrames over SQL/warehouse.** Spreadsheet-shaped data + finance math fits pandas naturally. Skip the warehouse until scale demands it.
- **Plotly over Altair / Matplotlib / Vega-Lite.** Interactive, polished defaults, handles every chart finance needs (especially waterfall), Claude writes it easily.
- **Settings file in home dir, not project dir.** Keeps the API key out of the repo by default and isolates per-user.
- **Skip user auth for the demo.** Single laptop, single user driving — auth would just be friction.
- **Anthropic-only, after exploring multi-provider.** Built a LiteLLM-based version supporting Anthropic + OpenAI + Gemini, ran into provider routing bugs in the OpenAI path, and rolled back. The cost (provider routing, losing native cache_control, multi-step debugging) outweighed the benefit (model choice) for a hackathon. Re-attempt as a parallel adapter pattern, not a unified abstraction.
- **General vs. doc-related context split by subfolder.** Considered three ways to distinguish "always load" from "scoped to a CSV": filename match against CSVs (auto), subfolder convention (`tables/`), or YAML frontmatter. Picked subfolder. Auto-match was elegant but fragile to renames and required users to follow a naming convention they couldn't see. Frontmatter was invisible until you opened the file. Subfolder is explicit in the filesystem, easy to teach, and the migration was a one-time `mv`. The downside (a renamed CSV breaks the link until its `.md` is renamed too) is acceptable — same problem under any scheme.
- **Two UIs for the two doc categories.** General docs live in the top-level Context view; doc-related docs live next to their CSV in the Data view via the per-file Context button. Tempting to surface both in one combined list — rejected because the affordances differ (general docs have no "parent" file; doc-related docs make no sense without one). Splitting the UIs gives each doc one obvious home and avoids a "where do I go to edit this?" question.
- **Google Sheets auth: per-team OAuth client over maintainer-hosted client.** Three paths to OAuth: (1) every install creates its own GCP project + client; (2) each team creates a client once and commits it to their repo fork; (3) Snoop maintainer hosts a public client every install shares. Picked (2). (1) is too much friction per teammate. (3) would force the consent screen to External (verification process, scary warnings until verified, anyone with any Google account can connect — Workspace security boundary disappears) and requires Snoop to exist as a maintainer org with brand verification. (2) keeps Workspace-internal scoping (security boundary survives), is one-time-per-team, and lets the team own the OAuth identity. The "secret" in the desktop OAuth client is non-secret per Google's docs, so committing it is fine.
- **One sheet sync = one tab.** Considered "import a whole workbook → N CSVs" but rejected: the AI context editor flow is built around one CSV at a time, and the right context doc for a P&L tab is very different from the right one for a Salaries tab. Forcing one-at-a-time keeps the per-tab context discipline. Side benefit: simpler error model (one sync failing doesn't half-finish another).
- **Materialise synced sheets as CSV, don't read live.** Considered keeping sheets as live API reads on every tool call — rejected because it adds latency to every chat question, burns API quota, blocks if Google has an outage, and breaks the existing "everything in `data/tables/` is a file" invariant. Materialising means the rest of the app (sandbox, fingerprint, context-chat, scope picker) sees no difference between manual upload and synced data.
- **Synced CSVs are read-only in the in-app editor.** Considered "warn but allow" — rejected. Mixed-source-of-truth in finance is the kind of bug that costs you a half-day chasing wrong numbers. If you want to edit, edit the sheet.
- **Stripe: restricted API key, not OAuth.** Stripe supports OAuth Connect, but it's designed for *Stripe Connect* (third-party apps acting on behalf of a Stripe account). For Snoop's "I want to read MY OWN Stripe data" model, a restricted API key is dramatically simpler: no consent flow, no callback URL, no per-user OAuth client to register. Each Snoop install (= each finance team) creates one read-only restricted key in their dashboard and pastes it into Settings. Trade-off: the key is the identity, so you can't tell *which* Snoop user did a sync — fine for a single-team install, would need to change if Snoop ever supports multi-tenant.
- **Stripe sync registry per-object, not per-file.** Could have keyed the registry by target filename (matching the Sheets sync pattern), but the per-object key is clearer because (a) Stripe's data model has a fixed schema we ship, not user-chosen filenames, and (b) the per-object dimension is where the incremental cursor naturally lives. Filename ↔ object mapping is a simple `f"stripe_{key}.csv"` helper.
- **Stripe sync-on-start is gated on prior sync, not just the toggle.** Without the gate, flipping the toggle and restarting the app could trigger a 25-minute initial sync at launch — terrible UX. The toggle is a no-op until at least one object has been synced manually first; from that point on, all syncs are incremental seconds. Documented in the toggle's help text.
- **Stay on Desktop OAuth client for now; defer Web client until deployment.** Three deployment shapes considered: (1) self-hosted single-instance for the team — needs Web OAuth client, app-level auth, token storage in a DB; estimate 2–3 focused weeks. (2) multi-tenant SaaS — months of work, essentially a product rewrite. (3) packaged desktop app via `stlite` / PyInstaller — keeps Desktop OAuth as-is, packaging weekend. Picked: stay local on Desktop OAuth. Decision to revisit once there's a concrete deployment requirement (URL someone outside this laptop should hit). The migration is bounded — only `gsheets.connect()` + token storage change; the rest of the app is unaffected.

- **Stripe API field migrations — `_<thing>_<field>(obj)` helper pattern.** Mid-2026 Stripe moved a number of reference fields off the top-level resource into nested `parent.<*_details>` or `pricing.price_details` blocks (an early step in their move toward polymorphic resource shapes). On modern API versions the legacy top-level fields return `None` even when the underlying data exists. Affected fields we hit so far: `invoice.subscription`, `line_item.{subscription, price, product, proration, type}`, `subscription.{current_period_start, current_period_end, discounts}`, `promotion_code.coupon`, `payment_intent.invoice` (moved to `payment_details.order_reference`), `dispute.network_reason_code` (now per payment method type), and `charge.invoice` (removed entirely — derive via `payment_intent_id`). Solution pattern: one helper per migrated field, e.g. `_invoice_subscription_id(inv)`, that tries the new path first and falls back to the legacy field. A small `_nested(obj, *keys)` walker handles StripeObject-vs-plain-dict shape mismatch at every level since the SDK returns either. Keep the CSV column name stable; the helper absorbs the migration so downstream queries are unaffected. **Audit script:** `_diag_stripe_audit.py` samples 10 records per resource and prints per-column "populated rate" plus a raw `_data` dump for any always-null field — that's how we find the next migration before it bites in a query. Decision: don't try to read Stripe's announcements; instead, re-run the audit any time a CSV column "looks wrong".

- **Headless agent core (`snoop_agent.py`).** When wiring the Slack bot, the obvious approach is to import the agent loop from `app.py`. Problem: `app.py` is full of Streamlit calls (`st.session_state`, `st.write`, `st.toast`, etc.) that have no meaning in a non-Streamlit process. Picked: extract the agent loop into `snoop_agent.py` — same Claude tool loop (run_python + memory + web_fetch + optional web_search), same system-prompt assembly, same context-doc loading, but no Streamlit. The Streamlit app still runs its richer real-time UI version in `app.py`; `snoop_agent.py` is the path for any non-Streamlit caller (Slack today, possibly Discord / CLI / API server tomorrow). Worth noting: the two loops will drift over time — system prompt assembly + tool list construction should ideally live in one place. Refactor candidate when there are three or more callers.

- **Slack bot via Socket Mode, not webhooks.** Slack Apps support two transports for events: HTTP webhooks (Slack POSTs to a public URL we expose) and Socket Mode (we open a WebSocket to Slack and they push events down it). Picked Socket Mode because (a) Snoop runs on a laptop / private server, not behind a public URL — webhooks would require tunneling or hosting; (b) it removes a whole category of "is the callback URL reachable / signed correctly?" debugging; (c) the trade-off (need to keep a process running) is the same problem we'd have with webhooks anyway. The `xapp-` App-Level Token is the price of admission for Socket Mode.

- **Schema-doc pointers as YAML frontmatter on the target doc, not as a separate metadata file or a code-side prefix map.** Considered three places to store "this CSV is documented by this general doc": (1) a `data/context/pointers.json` mapping, (2) frontmatter `--- covers: [filename1, filename2] ---` on the general doc itself, (3) a prefix-based map in `tools.py` (e.g. `stripe_*` → `stripe.md`). Picked (2). (1) adds a separate file to keep in sync and drifts silently. (3) couples organisation to filename conventions and breaks when names don't follow a pattern. (2) keeps the doc self-describing: the pointer relationship lives next to the thing it's about, the UI rewrites the array on rename / set / clear, and the agent never sees the YAML (it's stripped before the body flows into the system prompt). Side effect: a CSV rename has to cascade into the `covers:` lists of every general doc that mentions it — handled by `context_pointers.rename_csv`.

- **Multi-currency reporting via Stripe Balance Transactions, not a local FX table.** Considered three approaches: (1) define a fixed company FX rate somewhere — goes stale, picks up "what rate did we use?" arguments. (2) Fetch live rates via `web_fetch` at query time — fine for forecasting but wrong for historical reporting because today's rate doesn't reflect what we got two years ago. (3) Use `stripe_balance_transactions.csv` where `amount` and `net` are already in the account settlement currency — Stripe did the FX at transaction time, using their live rate at that exact moment. Picked (3) as the default for "real revenue" reporting; (2) is OK for forward-looking projections; (1) is explicitly avoided.

- **Document import: extract text at import time, not on every query.** PDF / Word / plain text uploaded in the Context view are extracted to text (via `pypdf` / `python-docx`) and saved as `.md` files. Considered keeping the original PDF and extracting on every agent turn — rejected because (a) the text extraction is expensive on every render, (b) the original PDF has tons of layout junk that doesn't belong in a context doc, and (c) the user can review and edit the extracted text before saving, which is exactly the right point to enforce quality. Excel imports follow the same shape: each sheet becomes its own CSV at import time, not a "live workbook" reference.

- **Slack bot as a separate process, not a thread inside the Streamlit app.** Streamlit's runtime is single-threaded and ties the event loop to user sessions. Running a Slack bot inside it would compete for the same scheduler and would die any time the Streamlit page reloads or restarts. Two separate processes sharing the `data/` directory is dramatically cleaner: each can crash and restart independently, the bot keeps running while the user is asleep, and there's zero coupling between the UI and the bot transport.

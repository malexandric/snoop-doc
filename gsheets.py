"""
Google Sheets live sync.

Lets users pull a single tab from a Google Sheets workbook into
`data/tables/<name>.csv`. From there it's indistinguishable from a
manually-uploaded CSV — the agent, fingerprint, context-chat, and
scope picker all see it identically.

Auth model
----------
OAuth 2.0 for installed (desktop) applications:
  1. App reads OAuth client credentials from `google-client.json` at the
     repo root (committed; Workspace-internal publishing means the
     "secret" can only be used to authenticate inside the company's
     Workspace, so checking it in is safe).
  2. First-time connect: app spins up a one-shot HTTP listener on
     `127.0.0.1:<random_port>`, opens the user's browser to Google's
     consent screen, captures the redirect, exchanges code for tokens.
  3. Tokens persist at `~/.snoop-doc/google-token.json` (per-user,
     chmod 600 on Unix). Refresh handled transparently.

Sync model
----------
One sync = one tab → one CSV. Multi-tab workbooks are imported one tab
at a time via the import dialog. Each registered sync stores the
immutable tab `gid` so renaming the tab in Sheets doesn't break it.

Registry file: `data/sheets-registry.json` (committed). Shared across
the team via git; each user OAuths into their own account and the
sync uses *their* credentials to fetch.

The Google client libraries are imported lazily so the rest of the
app still works if a user hasn't run `pip install -r requirements.txt`
since these deps were added.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent
CLIENT_SECRETS_PATH = PROJECT_ROOT / "google-client.json"
TOKEN_PATH = Path.home() / ".snoop-doc" / "google-token.json"
REGISTRY_PATH = PROJECT_ROOT / "data" / "sheets-registry.json"
TABLES_DIR = PROJECT_ROOT / "data" / "tables"

# Scopes:
#   - spreadsheets.readonly: read any sheet the user can access
#   - userinfo.email + openid: just so we can display "Connected as <email>"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


# ---------------------------------------------------------------------------
# Errors — typed so the UI can branch on cause without parsing messages
# ---------------------------------------------------------------------------
class GSheetsError(Exception):
    """Base for everything this module raises."""


class NotConfiguredError(GSheetsError):
    """OAuth client file (`google-client.json`) is missing."""


class DepsMissingError(GSheetsError):
    """The google-* libraries aren't installed."""


class NotConnectedError(GSheetsError):
    """No saved token, or saved token couldn't be refreshed (revoked / expired)."""


class AccessDeniedError(GSheetsError):
    """The signed-in user doesn't have access to the requested sheet."""


# ---------------------------------------------------------------------------
# Auth — lazy imports keep the rest of the app working without google-* deps
# ---------------------------------------------------------------------------
def _import_google_libs():
    """Import the google-* libs on demand. Raises DepsMissingError if any
    aren't installed, so the UI can show a "run pip install" hint instead
    of crashing the whole app."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as e:
        raise DepsMissingError(
            "Google API libraries not installed. Run "
            "`pip install -r requirements.txt` to add them."
        ) from e
    return Credentials, InstalledAppFlow, build, HttpError, Request


def is_configured() -> bool:
    """True if the OAuth client file is present."""
    return CLIENT_SECRETS_PATH.exists()


def is_connected() -> bool:
    """True if a token file exists (does NOT verify the token still works
    — call `get_credentials()` to check validity).
    """
    return TOKEN_PATH.exists()


def connect() -> str:
    """Run the OAuth flow. Opens the user's browser, captures the
    redirect, persists tokens, and returns the connected account's email.

    Blocking — waits for the user to complete the browser flow.
    """
    if not is_configured():
        raise NotConfiguredError(
            f"Missing OAuth client file at `{CLIENT_SECRETS_PATH}`. "
            "See README for setup instructions."
        )

    Credentials, InstalledAppFlow, build, _HttpError, _Request = _import_google_libs()

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRETS_PATH), SCOPES
    )
    # port=0 → pick any free port. The flow opens the browser and
    # blocks until Google redirects back.
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_credentials(creds)

    # Fetch + return the connected email so callers can display it.
    email = _fetch_email(creds)
    return email or ""


def disconnect() -> None:
    """Delete the saved token. The user will need to re-run connect() to
    use sync again."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def get_credentials():
    """Load + refresh credentials. Returns the Credentials object on
    success, or raises NotConnectedError if there's nothing usable.

    Side effect: refreshed tokens are written back to disk.
    """
    if not TOKEN_PATH.exists():
        raise NotConnectedError("Not connected to Google. Connect in Settings.")

    Credentials, _Flow, _build, _HttpError, Request = _import_google_libs()

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except (ValueError, OSError) as e:
        raise NotConnectedError(f"Token file unreadable: {e}") from e

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001 — refresh can fail many ways
            raise NotConnectedError(
                f"Token refresh failed ({e}). Reconnect in Settings."
            ) from e
        _save_credentials(creds)
        return creds

    raise NotConnectedError(
        "Saved Google token is no longer usable. Reconnect in Settings."
    )


def get_connected_email() -> str | None:
    """Return the email of the currently-connected Google account, or
    None if not connected. Doesn't raise — UI just hides the chip."""
    try:
        creds = get_credentials()
    except (NotConnectedError, NotConfiguredError, DepsMissingError):
        return None
    return _fetch_email(creds)


def _fetch_email(creds) -> str | None:
    _Credentials, _Flow, build, _HttpError, _Request = _import_google_libs()
    try:
        oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = oauth2.userinfo().get().execute()
        return info.get("email")
    except Exception:  # noqa: BLE001
        return None


def _save_credentials(creds) -> None:
    """Persist the Credentials object as JSON. The Credentials class
    exposes `to_json()` which round-trips with `from_authorized_user_file`.
    """
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass  # Windows / non-POSIX


# ---------------------------------------------------------------------------
# Sheet URL parsing + workbook metadata fetch
# ---------------------------------------------------------------------------
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_\-]+)")


def extract_sheet_id(url_or_id: str) -> str | None:
    """Pull a sheet ID out of a Google Sheets URL, or pass through if the
    input already looks like a bare ID. Returns None if neither pattern
    matches."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _SHEET_ID_RE.search(s)
    if m:
        return m.group(1)
    # Heuristic: bare IDs are 30-60 chars of [a-zA-Z0-9_-]
    if re.fullmatch(r"[a-zA-Z0-9_\-]{20,80}", s):
        return s
    return None


@dataclass
class TabInfo:
    gid: int
    title: str
    rows: int
    cols: int


@dataclass
class WorkbookInfo:
    sheet_id: str
    title: str
    tabs: list[TabInfo]


def fetch_workbook_metadata(sheet_id: str) -> WorkbookInfo:
    """Return the workbook's title + list of tabs (gid, name, dims)."""
    creds = get_credentials()
    _Credentials, _Flow, build, HttpError, _Request = _import_google_libs()

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    except HttpError as e:
        _raise_for_http(e)

    title = meta.get("properties", {}).get("title", "")
    tabs: list[TabInfo] = []
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        grid = props.get("gridProperties", {})
        tabs.append(
            TabInfo(
                gid=int(props.get("sheetId", 0)),
                title=props.get("title", ""),
                rows=int(grid.get("rowCount", 0)),
                cols=int(grid.get("columnCount", 0)),
            )
        )
    return WorkbookInfo(sheet_id=sheet_id, title=title, tabs=tabs)


def _raise_for_http(e: Exception) -> None:
    """Translate a googleapiclient HttpError into one of our typed errors.
    Always raises — `-> None` is a type-checker hint only."""
    status = getattr(e, "status_code", None) or getattr(e, "resp", None)
    code = getattr(status, "status", None) if status is not None else None
    try:
        code = int(code) if code is not None else int(e.resp.status)
    except Exception:  # noqa: BLE001
        code = None

    msg = str(e)
    if code in (401, 403):
        raise AccessDeniedError(
            "Google returned 'access denied'. The signed-in account "
            "doesn't have permission to read this sheet. Ask the owner "
            "to share it, then retry."
        ) from e
    raise GSheetsError(f"Google API error ({code or '?'}): {msg}") from e


# ---------------------------------------------------------------------------
# Tab fetch + CSV materialise
# ---------------------------------------------------------------------------
def fetch_tab_values(sheet_id: str, tab_name: str) -> list[list[str]]:
    """Pull all values from a tab. Returns a list of rows of strings,
    using FORMATTED_VALUE so currency / date formatting is preserved as
    the user sees it in the sheet.
    """
    creds = get_credentials()
    _Credentials, _Flow, build, HttpError, _Request = _import_google_libs()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=sheet_id,
                range=tab_name,
                valueRenderOption="FORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute()
        )
    except HttpError as e:
        _raise_for_http(e)
    return result.get("values", [])


def write_values_as_csv(values: list[list[str]], target_path: Path) -> int:
    """Write a 2-D list of values to CSV, padding short rows with empty
    strings so pandas reads a consistent rectangle. Returns row count
    (including header).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not values:
        target_path.write_text("", encoding="utf-8")
        return 0
    max_cols = max(len(row) for row in values)
    with target_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in values:
            if len(row) < max_cols:
                row = row + [""] * (max_cols - len(row))
            writer.writerow(row)
    return len(values)


def sync_tab_to_csv(sheet_id: str, tab_name: str, target_filename: str) -> dict[str, Any]:
    """Fetch one tab and write it to `data/tables/<target_filename>`.
    Returns `{"ok": True, "rows": N}` on success or
    `{"ok": False, "error": "..."}` on failure (typed by class name).
    """
    try:
        values = fetch_tab_values(sheet_id, tab_name)
    except AccessDeniedError as e:
        return {"ok": False, "error": str(e), "kind": "access_denied"}
    except NotConnectedError as e:
        return {"ok": False, "error": str(e), "kind": "not_connected"}
    except GSheetsError as e:
        return {"ok": False, "error": str(e), "kind": "api_error"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Unexpected: {e}", "kind": "unknown"}

    target_path = TABLES_DIR / target_filename
    try:
        rows = write_values_as_csv(values, target_path)
    except OSError as e:
        return {"ok": False, "error": f"Could not write CSV: {e}", "kind": "io"}

    return {"ok": True, "rows": rows}


# ---------------------------------------------------------------------------
# Sync registry — `data/sheets-registry.json`, committed via git
# ---------------------------------------------------------------------------
@dataclass
class SyncEntry:
    id: str
    sheet_id: str
    sheet_title: str
    gid: int
    tab_name_at_sync: str
    target_filename: str
    last_synced_at: str | None  # ISO-8601 UTC
    last_sync_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sheet_id": self.sheet_id,
            "sheet_title": self.sheet_title,
            "gid": self.gid,
            "tab_name_at_sync": self.tab_name_at_sync,
            "target_filename": self.target_filename,
            "last_synced_at": self.last_synced_at,
            "last_sync_error": self.last_sync_error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SyncEntry":
        return cls(
            id=d.get("id") or str(uuid.uuid4()),
            sheet_id=d.get("sheet_id", ""),
            sheet_title=d.get("sheet_title", ""),
            gid=int(d.get("gid", 0)),
            tab_name_at_sync=d.get("tab_name_at_sync", ""),
            target_filename=d.get("target_filename", ""),
            last_synced_at=d.get("last_synced_at"),
            last_sync_error=d.get("last_sync_error"),
        )


def load_registry() -> list[SyncEntry]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [SyncEntry.from_dict(d) for d in raw.get("sheets", [])]


def save_registry(entries: list[SyncEntry]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps({"sheets": [e.to_dict() for e in entries]}, indent=2),
        encoding="utf-8",
    )


def find_entry_by_filename(filename: str) -> SyncEntry | None:
    for e in load_registry():
        if e.target_filename == filename:
            return e
    return None


def upsert_entry(entry: SyncEntry) -> None:
    """Insert if new (by id), replace if existing."""
    entries = load_registry()
    for i, existing in enumerate(entries):
        if existing.id == entry.id:
            entries[i] = entry
            save_registry(entries)
            return
    entries.append(entry)
    save_registry(entries)


def remove_entry(entry_id: str) -> None:
    entries = [e for e in load_registry() if e.id != entry_id]
    save_registry(entries)


def remove_entry_by_filename(filename: str) -> None:
    """Called by the existing Delete-CSV flow so that deleting a synced
    CSV also drops its registry entry — keeps the two in sync."""
    entries = [e for e in load_registry() if e.target_filename != filename]
    save_registry(entries)


# ---------------------------------------------------------------------------
# Sync orchestration — one entry or all
# ---------------------------------------------------------------------------
def sync_entry(entry: SyncEntry) -> SyncEntry:
    """Re-pull one registered tab. Updates `last_synced_at` /
    `last_sync_error` on the entry and persists to the registry.
    Returns the updated entry."""
    # Look up the current tab name from the sheet's metadata via gid —
    # if the user renamed the tab in Sheets, we still find it.
    tab_name = entry.tab_name_at_sync
    try:
        wb = fetch_workbook_metadata(entry.sheet_id)
        # Refresh the workbook title in case it was renamed.
        entry.sheet_title = wb.title
        # Look the tab up by gid (immutable).
        for t in wb.tabs:
            if t.gid == entry.gid:
                tab_name = t.title
                entry.tab_name_at_sync = t.title
                break
        else:
            entry.last_sync_error = (
                f"Tab with gid={entry.gid} no longer exists in this workbook."
            )
            entry.last_synced_at = _now_iso()
            upsert_entry(entry)
            return entry
    except (NotConnectedError, AccessDeniedError, GSheetsError) as e:
        entry.last_sync_error = str(e)
        entry.last_synced_at = _now_iso()
        upsert_entry(entry)
        return entry

    result = sync_tab_to_csv(entry.sheet_id, tab_name, entry.target_filename)
    entry.last_synced_at = _now_iso()
    entry.last_sync_error = None if result.get("ok") else result.get("error")
    upsert_entry(entry)
    return entry


def sync_all() -> list[SyncEntry]:
    """Re-pull every registered entry. Returns the updated list."""
    return [sync_entry(e) for e in load_registry()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Staleness helpers — for the green/amber/red badge in the file list
# ---------------------------------------------------------------------------
def staleness_seconds(entry: SyncEntry) -> int | None:
    """Seconds since `last_synced_at`. None if never synced."""
    if not entry.last_synced_at:
        return None
    try:
        synced = datetime.fromisoformat(entry.last_synced_at)
    except ValueError:
        return None
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - synced).total_seconds())


def staleness_band(entry: SyncEntry) -> str:
    """One of: "fresh" (<24h), "stale" (24h–7d), "old" (>7d), "never".
    Used for badge colour."""
    s = staleness_seconds(entry)
    if s is None:
        return "never"
    if s < 24 * 3600:
        return "fresh"
    if s < 7 * 24 * 3600:
        return "stale"
    return "old"


def staleness_label(entry: SyncEntry) -> str:
    """Human-readable 'N min ago' / 'N hours ago' / 'N days ago'."""
    s = staleness_seconds(entry)
    if s is None:
        return "never synced"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 24 * 3600:
        return f"{s // 3600} h ago"
    return f"{s // (24 * 3600)} d ago"

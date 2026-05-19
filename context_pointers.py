"""
Schema-doc pointers — map CSVs to general context docs.

A "schema-doc pointer" says *"this CSV's columns and business meaning
are documented in <some general doc>.md"*. The pointer lives in the
general doc's own YAML frontmatter:

    ---
    covers: [stripe_invoices.csv, stripe_payment_intents.csv]
    ---
    # Stripe data
    ...

Two consumers read these:

  - `app.py` strips the frontmatter before flowing the doc into the
    system prompt — the agent reads the body, not raw YAML.
  - `tools.py` consults the pointer map at namespace-build time so the
    `run_python` tool description can render a `schema: stripe.md` line
    next to each table. That tells the agent which group doc covers a
    given CSV without having to guess from filenames.

UI code (Data view, Context view) calls `set_pointer` /
`docs_for_csv` to display + change the relationships. Frontmatter
parsing is intentionally light (no PyYAML dep): scalar values + flat
list values, nothing else. A parsing failure leaves the body intact.
"""

from __future__ import annotations

import re
from pathlib import Path


CONTEXT_DIR = Path(__file__).parent / "data" / "context"
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return `(meta, body)`. Returns `({}, text)` when no frontmatter
    block is present, so callers can use this unconditionally.

    Supports:
      - `key: value` scalar entries (quotes stripped)
      - `key: [a, b, c]` flow-style list entries

    Block-style lists, nested maps, multi-line strings — out of scope.
    The schema-pointer feature only needs flat lists of CSV filenames.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    meta: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                meta[key] = []
            else:
                meta[key] = [
                    p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()
                ]
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        meta[key] = value
    return meta, body


def serialize(meta: dict, body: str) -> str:
    """Inverse of parse — render `meta` + `body` back to text. Drops the
    frontmatter block entirely when `meta` is empty so docs without
    pointers don't accumulate empty `---` headers across edits."""
    if not meta:
        return body
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            inner = ", ".join(value)
            lines.append(f"{key}: [{inner}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def strip_frontmatter(text: str) -> str:
    """Return just the body. Used by the system-prompt loader."""
    _, body = parse_frontmatter(text)
    return body


def _list_general_docs() -> list[Path]:
    """General docs = top-level .md files in `data/context/` (README excluded)."""
    if not CONTEXT_DIR.exists():
        return []
    return sorted(
        p for p in CONTEXT_DIR.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".md"
        and p.name.lower() != "readme.md"
    )


def build_pointer_map() -> dict[str, list[str]]:
    """Return `{csv_filename: [general_doc_filename, ...]}` for every
    pointer declared across all general-doc frontmatters. A CSV may
    appear in multiple lists; the UI normally enforces one-to-one but
    this function doesn't dedupe in case a user hand-edited the docs."""
    result: dict[str, list[str]] = {}
    for doc_path in _list_general_docs():
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = parse_frontmatter(text)
        covers = meta.get("covers")
        if not isinstance(covers, list):
            continue
        for csv_name in covers:
            result.setdefault(csv_name, []).append(doc_path.name)
    return result


def docs_for_csv(csv_filename: str) -> list[str]:
    """Convenience: which general docs claim coverage of this CSV."""
    return build_pointer_map().get(csv_filename, [])


def set_pointer(csv_filename: str, general_doc_name: str | None) -> None:
    """Make `general_doc_name` the (sole) schema doc for this CSV.

    Pass `None` to clear the pointer. The function rewrites *every*
    general doc's `covers:` list to ensure the CSV is either listed in
    exactly one doc (when `general_doc_name` is given) or in none. This
    keeps the UI invariant of one-pointer-per-CSV without trusting the
    caller to remember prior state.
    """
    for doc_path in _list_general_docs():
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        covers = meta.get("covers")
        if not isinstance(covers, list):
            covers = []
        if doc_path.name == general_doc_name:
            if csv_filename not in covers:
                covers.append(csv_filename)
        else:
            covers = [c for c in covers if c != csv_filename]
        if covers:
            meta["covers"] = covers
        else:
            meta.pop("covers", None)
        try:
            doc_path.write_text(serialize(meta, body), encoding="utf-8")
        except OSError:
            continue


def general_doc_names() -> list[str]:
    """List of filenames (e.g. `pnl.md`) for use in dropdowns."""
    return [p.name for p in _list_general_docs()]


def rename_csv(old_filename: str, new_filename: str) -> int:
    """Rewrite every general doc's `covers:` list, replacing
    `old_filename` with `new_filename`. Returns the number of docs
    actually changed. No-op for docs that don't mention the old name.
    Called from the Data view's rename action so a CSV rename doesn't
    leave dangling pointer references.
    """
    if old_filename == new_filename:
        return 0
    changed = 0
    for doc_path in _list_general_docs():
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        covers = meta.get("covers")
        if not isinstance(covers, list) or old_filename not in covers:
            continue
        new_covers = [new_filename if c == old_filename else c for c in covers]
        meta["covers"] = new_covers
        try:
            doc_path.write_text(serialize(meta, body), encoding="utf-8")
            changed += 1
        except OSError:
            continue
    return changed

"""Playbook loader — exposes the <client> (or per-brand) playbook to LLM nodes
and rule engines.

Why a loader: the playbook is a markdown file under /playbooks. At runtime,
LLM nodes (tracking_audit, ideas, creative_critique) need to inject the
relevant section of the playbook into their system prompt so the model's
advice stays consistent with the codified expertise. Planner rules load
the YAML block from Section IX for numeric thresholds.

Design choices:
  - Sections are separated by `## I · …` `## II · …` headings. We split on
    these anchors so callers can ask for just one section.
  - Node-specific briefs are in Section X and keyed by node name.
  - YAML rules block (between the triple-backticks in Section IX) is
    exposed as a dict.
  - Playbook is loaded once on import (it's static) — mtime check can be
    added later for hot-reload.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PUBLIC_PLAYBOOK_DIR = Path(__file__).resolve().parent.parent.parent / "playbooks"

try:
    import ai_marketing_stack_ads_playbook as _priv  # type: ignore[import-not-found]
    _PRIVATE_PLAYBOOK_DIR: Path | None = Path(_priv.__file__).resolve().parent / "playbooks"
except ImportError:
    _PRIVATE_PLAYBOOK_DIR = None


def _resolve(brand: str) -> Path:
    """Prefer the installed private playbook package, fall back to the
    public repo's demo playbooks dir. Real per-brand strategy lives in
    the private package to keep forks a step behind."""
    if _PRIVATE_PLAYBOOK_DIR is not None:
        candidate = _PRIVATE_PLAYBOOK_DIR / f"{brand}.md"
        if candidate.exists():
            return candidate
    return _PUBLIC_PLAYBOOK_DIR / f"{brand}.md"


PLAYBOOK_DIR = _PRIVATE_PLAYBOOK_DIR or _PUBLIC_PLAYBOOK_DIR


def load_ref(name: str) -> str:
    """Load a brand-agnostic reference document (playbooks/refs/<name>.md).

    Used by audit analysts to cite stable check IDs (M01-M50) and
    2025 platform-change context. Looks in the private package first,
    falls back to the public repo, then to "" so the caller's inline
    prompt remains the safety net.
    """
    candidates: list[Path] = []
    if _PRIVATE_PLAYBOOK_DIR is not None:
        candidates.append(_PRIVATE_PLAYBOOK_DIR / "refs" / f"{name}.md")
    candidates.append(_PUBLIC_PLAYBOOK_DIR / "refs" / f"{name}.md")
    for p in candidates:
        if p.exists():
            try:
                return p.read_text()
            except Exception as e:  # noqa: BLE001
                log.warning("load_ref(%s) read failed: %s", name, e)
                return ""
    return ""


@lru_cache(maxsize=8)
def load_raw(brand: str = "default") -> str:
    """Return the raw markdown of a brand's playbook."""
    path = _resolve(brand)
    if not path.exists():
        log.warning("no playbook found for brand %r at %s", brand, path)
        return ""
    return path.read_text()


def list_sections(brand: str = "default") -> list[tuple[str, str]]:
    """Return [(roman_numeral, section_title), ...]."""
    text = load_raw(brand)
    out = []
    for m in re.finditer(r"^## ([IVX]+) · (.+)$", text, re.MULTILINE):
        out.append((m.group(1), m.group(2).strip()))
    return out


def section(brand: str, roman_or_title: str) -> str:
    """Extract a single section by its Roman numeral (e.g. "V") or by a
    substring of its title ("harvesting"). Returns empty string if not found.
    """
    text = load_raw(brand)
    # Find header
    header_pattern = re.compile(r"^## ([IVX]+) · (.+)$", re.MULTILINE)
    headers = list(header_pattern.finditer(text))
    roman_upper = roman_or_title.upper()
    for i, h in enumerate(headers):
        roman, title = h.group(1), h.group(2)
        # Match on exact Roman numeral first, then fall back to title substring
        matched = (
            roman == roman_upper
            or (not _looks_like_roman(roman_or_title)
                and roman_or_title.lower() in title.lower())
        )
        if matched:
            start = h.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            return text[start:end].strip()
    return ""


def _looks_like_roman(s: str) -> bool:
    return bool(re.fullmatch(r"[IVX]+", s.upper()))


def node_brief(node_name: str, brand: str = "default") -> str:
    """Return the LLM-node brief from Section X for the given node.

    node_name ∈ {"ideas", "tracking_audit", "creative_critique"}.
    If not found, falls back to an empty string so the caller can use its
    existing static prompt.
    """
    text = section(brand, "X")
    if not text:
        return ""
    # Each node brief starts with `### \`<node>\` node ...`
    pattern = re.compile(rf"^### `{re.escape(node_name)}` node.*?\n```\n(.*?)\n```",
                         re.MULTILINE | re.DOTALL)
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def rules(brand: str = "default") -> dict[str, Any]:
    """Parse Section IX's YAML block into a dict.

    Planner / executor read this to drive numeric thresholds. Returns
    an empty dict if PyYAML isn't available or the section is malformed —
    callers should fall back to their hard-coded defaults in that case.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        log.warning("PyYAML not installed — playbook rules not available")
        return {}

    text = section(brand, "IX")
    if not text:
        return {}
    m = re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        log.error("playbook YAML parse failed: %s", e)
        return {}


def version(brand: str = "default") -> str:
    """Return the version stamp from the playbook's H1."""
    text = load_raw(brand)
    m = re.search(r"^# .+ · (v\d+) \((\d{4}-\d{2}-\d{2})\)", text, re.MULTILINE)
    return f"{m.group(1)} ({m.group(2)})" if m else "unknown"


if __name__ == "__main__":
    # Quick diagnostic — run `python -m ads_agent.playbook` to verify wiring.
    brand = "default"
    print(f"Playbook: {brand} · {version(brand)}")
    print(f"Sections ({len(list_sections(brand))}):")
    for r, t in list_sections(brand):
        print(f"  {r}  {t}")
    print()
    print("rules() keys:", list(rules(brand).keys()))
    print()
    for node in ("ideas", "tracking_audit", "creative_critique"):
        brief = node_brief(node, brand)
        print(f"{node}: brief present = {bool(brief)} ({len(brief)} chars)")

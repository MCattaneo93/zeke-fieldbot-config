"""Runtime configuration for the Fieldbot OpenClaw bridge.

The OpenClaw plugin passes non-secret policy settings to this process. Odoo
credentials stay in the gateway environment or in a root-owned secret file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _read_secret(env_name: str, file_env_name: str) -> str:
    direct = os.environ.get(env_name, "").strip()
    if direct:
        return direct
    raw_path = os.environ.get(file_env_name, "").strip()
    if not raw_path:
        return ""
    path = Path(raw_path).expanduser().resolve()
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Secret file does not exist: {path}") from exc


ODOO_URL = _required("FIELDBOT_ODOO_URL").rstrip("/")
ODOO_DB = _required("FIELDBOT_ODOO_DB")
ODOO_LOGIN = _required("FIELDBOT_ODOO_LOGIN")
ODOO_API_KEY = _read_secret(
    "FIELDBOT_ODOO_API_KEY", "FIELDBOT_ODOO_API_KEY_FILE"
)
if not ODOO_API_KEY:
    raise RuntimeError(
        "Set FIELDBOT_ODOO_API_KEY or FIELDBOT_ODOO_API_KEY_FILE"
    )

USER_MAP: dict[str, str] = json.loads(
    os.environ.get("FIELDBOT_USER_MAP", "{}")
)
if not isinstance(USER_MAP, dict):
    raise RuntimeError("FIELDBOT_USER_MAP must be a JSON object")

# Nicknames are declared, never guessed. Assignment is a statement about who
# owns a customer commitment, so an approximate match is worse than no match.
# Maps a lowercased nickname to a registered technician's Odoo login.
TECH_ALIASES: dict[str, str] = {
    str(alias).strip().lower(): str(login).strip()
    for alias, login in (
        json.loads(os.environ.get("FIELDBOT_TECH_ALIASES", "{}") or "{}") or {}
    ).items()
}

TZNAME = os.environ.get("FIELDBOT_TZ", "America/New_York")
LEGACY_CUTOFF = os.environ.get("FIELDBOT_LEGACY_CUTOFF", "2025-12-01").strip()
IGNORED_PROJECTS = [
    value.strip().lower()
    for value in os.environ.get("FIELDBOT_IGNORED_PROJECTS", "Graine Pay").split(",")
    if value.strip()
]
AUTO_ARCHIVE_TITLES = [
    value.strip().lower()
    for value in os.environ.get(
        "FIELDBOT_AUTO_ARCHIVE_TITLES",
        "Microsoft 365 security: You have messages in quarantine",
    ).split("||")
    if value.strip()
]

STATE_DIR = Path(
    os.environ.get("FIELDBOT_STATE_DIR", "~/.openclaw/fieldbot")
).expanduser().resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.environ.get(
    "FIELDBOT_STATE_DB", str(STATE_DIR / "fieldbot.sqlite")
)


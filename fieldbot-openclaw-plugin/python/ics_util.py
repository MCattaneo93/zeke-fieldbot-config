"""Minimal iCalendar (.ics) builder - no external deps, no external services.

The bot sends these as Telegram file attachments; tapping one on a phone
opens the native add-to-calendar flow (works with Outlook, Apple, Google).
"""
import datetime
import uuid
from zoneinfo import ZoneInfo

import config


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;") \
                       .replace(",", "\\,").replace("\n", "\\n")


def _utc(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config.TZNAME))
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(title: str, start: datetime.datetime, end: datetime.datetime,
          description: str = "", location: str | None = None) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fieldbot//Odoo Time Lord//EN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@fieldbot",
        f"DTSTAMP:{_utc(datetime.datetime.now(datetime.timezone.utc))}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc(title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"

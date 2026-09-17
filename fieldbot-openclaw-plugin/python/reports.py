"""Deterministic report and polling helpers used by OpenClaw automations."""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

import actions
import config
import state
from odoo_client import OdooError


log = logging.getLogger(__name__)
STALE_DAYS = 2


def now() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(config.TZNAME))


def days_since(odoo_date: str | None) -> int | None:
    if not odoo_date:
        return None
    try:
        then = datetime.date.fromisoformat(odoo_date[:10])
    except ValueError:
        return None
    return (now().date() - then).days


def morning_dispatch() -> str:
    """Activity-first dispatch: due steps, gaps, stale work, unassigned work."""
    today = now().date().isoformat()
    tickets = actions.open_tickets()
    lines = [f"Morning dispatch - {now():%a %b %d}"]
    for telegram_id in config.USER_MAP:
        tech = actions.get_tech(int(telegram_id))
        if tech is None:
            continue
        mine = [ticket for ticket in tickets if tech.name in ticket["assigned_to"]]
        lines.append(f"\n{tech.name} - today's plan:")
        due = actions.due_activities(tech.uid, today)
        if due:
            for activity in due:
                lines.append(
                    f"- #{activity['task_id']} {activity['task']}: "
                    f"{activity['summary']}"
                    f"{actions.deadline_flag(activity['due'])}"
                )
        else:
            lines.append('- nothing due today; set a step with "next step on #123: ..."')
        deadlined = [
            ticket
            for ticket in mine
            if ticket.get("deadline")
            and ticket["deadline"] <= today
            and not any(
                activity["task_id"] == ticket["task_id"] for activity in due
            )
        ]
        for ticket in deadlined[:5]:
            lines.append(
                f"- #{ticket['task_id']} {ticket['title']} - ticket itself"
                f"{actions.deadline_flag(ticket['deadline'])}"
            )
        gaps = [ticket for ticket in mine if not ticket.get("next_step")]
        if gaps:
            gaps.sort(key=lambda ticket: ticket.get("last_update") or "")
            worst = ", ".join(f"#{ticket['task_id']}" for ticket in gaps[:3])
            lines.append(
                f"- {len(gaps)} of {len(mine)} tickets have no next step; "
                f"oldest: {worst}"
            )
        stale = [
            ticket
            for ticket in mine
            if (age := days_since(ticket.get("last_update"))) is not None
            and age >= STALE_DAYS
        ]
        if stale:
            lines.append(f"- {len(stale)} untouched for {STALE_DAYS}+ days")
    unassigned = [ticket for ticket in tickets if not ticket["assigned_to"]]
    if unassigned:
        lines.append(f"\nUnassigned ({len(unassigned)}):")
        for ticket in unassigned[:8]:
            customer = f" @ {ticket['customer']}" if ticket["customer"] else ""
            lines.append(f"- #{ticket['task_id']} {ticket['title']}{customer}")
    return "\n".join(lines)


def eod_nudge() -> str:
    today = now().date().isoformat()
    nags: list[str] = []
    for telegram_id in config.USER_MAP:
        tech = actions.get_tech(int(telegram_id))
        if tech is None or tech.employee_id is None:
            continue
        for task_id in state.claims_today(int(telegram_id)):
            if actions.timesheet_exists_today(tech.employee_id, task_id, today):
                continue
            try:
                task = actions.client.execute(
                    "project.task", "read", [[task_id]], {"fields": ["name"]}
                )[0]
                name = task["name"]
            except OdooError:
                name = f"#{task_id}"
            nags.append(
                f"- {tech.name}: took {name} (#{task_id}) today but has not "
                "logged time"
            )
    sections: list[str] = []
    if nags:
        sections.append("Before clock-out:\n" + "\n".join(nags))
    steps: list[str] = []
    for telegram_id in config.USER_MAP:
        tech = actions.get_tech(int(telegram_id))
        if tech is None:
            continue
        for activity in actions.due_activities(tech.uid, today):
            steps.append(
                f"- {tech.name.split()[0]}: #{activity['task_id']} "
                f"{activity['task']} - {activity['summary']}"
                f"{actions.deadline_flag(activity['due'])}"
            )
    if steps:
        sections.append(
            "Next steps still open today:\n"
            + "\n".join(steps)
            + '\nDone? Say "did the call on #123". Moving it? Say '
            '"push the next step on #123 to Friday".'
        )
    return "\n\n".join(sections)


def recap_input(kind: str) -> dict:
    today = now().date()
    if kind == "daily":
        date_from = date_to = today.isoformat()
        label = "end-of-day"
    elif kind == "weekly":
        date_from = (today - datetime.timedelta(days=7)).isoformat()
        date_to = today.isoformat()
        label = "weekly"
    else:
        raise ValueError("recap kind must be daily or weekly")
    trend = ""
    if kind == "weekly":
        current = actions.overdue_activity_count()
        previous = state.get_meta("overdue_prev_week")
        state.set_meta("overdue_prev_week", str(current))
        trend = (
            f"\n\nOVERDUE NEXT-STEP TREND: {current} now"
            + (f", {previous} last week" if previous else " (first week tracked)")
        )
    return {
        "kind": label,
        "date_from": date_from,
        "date_to": date_to,
        "timesheets": actions.timesheets_between(date_from, date_to),
        "activities": actions.activity_recap(date_from, date_to) + trend,
        "board": actions.recap_ticket_digest(),
        "summary_instructions": (
            "Write a short field-service recap in plain text. Lead with hours per "
            "person, then closures and completed next steps with outcomes. Then "
            "overdue and upcoming next steps by owner, followed by stuck, stale, "
            "and unassigned work. Do not invent facts or contact customers."
        ),
    }


def poll_new_tickets() -> list[dict]:
    last = state.get_meta("last_task_id")
    if last is None:
        state.set_meta("last_task_id", str(actions.max_task_id()))
        return []
    new = actions.new_tickets_since(int(last))
    if not new:
        return []
    state.set_meta("last_task_id", str(max(ticket["id"] for ticket in new)))
    kept, archived = actions.auto_archive_noise(new)
    if archived:
        log.info("Auto-archived %d notification tickets", len(archived))
    return [
        {
            "task_id": ticket["id"],
            "title": ticket["name"],
            "customer": ticket["partner_id"][1] if ticket["partner_id"] else None,
            "project": ticket["project_id"][1] if ticket["project_id"] else None,
            "assigned": bool(ticket["user_ids"]),
        }
        for ticket in kept
    ]


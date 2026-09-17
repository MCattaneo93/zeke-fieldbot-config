#!/usr/bin/env python3
"""JSON bridge between the OpenClaw tool plugin and the guarded Odoo code."""

from __future__ import annotations

import base64
import datetime
import json
import logging
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import actions
import config
import ics_util
import reports
import state
import voice
from odoo_client import OdooError


logging.basicConfig(
    level=os.environ.get("FIELDBOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("fieldbot.bridge")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def sender_id(raw: Any) -> int | None:
    value = str(raw or "").strip()
    if not value:
        return None
    tail = value.rsplit(":", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def require_tech(raw_sender: Any) -> tuple[int, actions.Tech]:
    telegram_id = sender_id(raw_sender)
    if telegram_id is None or str(telegram_id) not in config.USER_MAP:
        # Echo what actually arrived. Without it this failure is undiagnosable:
        # a callback/button press and a typed message do not necessarily carry
        # the same sender identity, and a chat id parses as an int just fine.
        if raw_sender is None:
            # The tool surface is the one place OpenClaw does not hand us a
            # sender: in a group the session is keyed on the group, not a
            # person. Refusing is correct - attributing a claim or a timesheet
            # to the wrong technician is worse than not doing it - but the
            # slash commands run on the command surface, which does carry the
            # real sender id, so point at those instead of a dead end.
            raise PermissionError(
                "This action needs to know which technician is asking, and I "
                "can't tell from a group message routed this way. Use the "
                "slash command instead - /take, /drop, /close, /assign, "
                "/sweep, /mine all identify you correctly in the group - or "
                "send it to me in a direct message.")
        raise PermissionError(
            "This action requires a registered Telegram technician "
            f"(received sender {raw_sender!r})")
    tech = actions.get_tech(telegram_id)
    if tech is None:
        raise PermissionError("The Telegram sender is not mapped to an Odoo technician")
    return telegram_id, tech


def attribution(speaker_id: Any, speaker: Any, task_id: int):
    """Decide whose work a timesheet entry belongs to.

    The ticket answers this better than the person typing does, so the ticket
    is asked first: an explicit claim, then a single registered assignee. The
    speaker is only the last resort, and may be None when a group message gives
    us no identity - in that case an unattributable ticket returns (None, None)
    and the caller refuses rather than guessing.
    """
    owner_id = state.claim_owner(task_id)
    if owner_id and owner_id != speaker_id:
        owner = actions.get_tech(owner_id)
        if owner:
            return owner, owner_id
    if owner_id is None:
        try:
            task = actions.client.execute(
                "project.task", "read", [[task_id]], {"fields": ["user_ids"]}
            )[0]
            assigned = []
            for candidate_id in config.USER_MAP:
                candidate = actions.get_tech(int(candidate_id))
                if candidate and candidate.uid in task["user_ids"]:
                    assigned.append((candidate, int(candidate_id)))
            if len(assigned) == 1 and assigned[0][1] != speaker_id:
                return assigned[0]
        except OdooError:
            pass
    return speaker, speaker_id


def timesheet_actor(raw_sender: Any, task_id: int, verb: str):
    """Who to log against, without demanding to know who asked.

    A ticket with a claim owner or exactly one registered assignee already says
    whose work it is; requiring the asker's identity on top of that blocked
    closes in the group for no accuracy gain. When the ticket cannot answer and
    the asker is unknown, refuse - logging someone else's hours under a guess is
    the mistake worth preventing.
    """
    speaker_id = sender_id(raw_sender)
    if speaker_id is not None and str(speaker_id) not in config.USER_MAP:
        raise PermissionError(
            "This action requires a registered Telegram technician "
            f"(received sender {raw_sender!r})")
    speaker = actions.get_tech(speaker_id) if speaker_id is not None else None
    log_tech, log_telegram_id = attribution(speaker_id, speaker, task_id)
    if log_tech is None:
        raise PermissionError(
            f"I can't tell whose time to log for {actions.task_label(task_id)}: "
            "nobody has claimed it and it isn't assigned to exactly one "
            f"technician. Assign it first (/assign #{task_id} matt), or {verb} "
            "it from a direct message so I know it's yours.")
    return speaker_id, log_tech, log_telegram_id


def urgency_key(ticket: dict):
    """Hot tickets first, then the ones with the oldest activity."""
    return (ticket.get("priority") != "1", ticket.get("last_update") or "")


def capped_list(tickets: list[dict], header: str, empty: str, limit: Any) -> str:
    """Render a ticket list, trimming to the most urgent when a limit applies.

    Only the command surface passes a limit. It bypasses the model entirely, so
    an untrimmed answer goes to Telegram verbatim - and a full plate is tens of
    thousands of characters, which Telegram splits into a wall of messages.
    The tool path passes no limit and still sees everything.
    """
    try:
        cap = int(limit) if limit is not None else 0
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0 or len(tickets) <= cap:
        return actions.format_ticket_list(tickets, header, empty)
    ordered = sorted(tickets, key=urgency_key)
    message = actions.format_ticket_list(ordered[:cap], header, empty)
    return f"{message}\n…and {len(tickets) - cap} more. Ask for the full list if you need it."


def list_tickets(params: dict, raw_sender: Any) -> dict:
    view = params.get("view", "open")
    if view == "legacy":
        owner = params.get("owner")
        return {"message": actions.legacy_digest(owner)}
    limit = params.get("limit")
    tickets = actions.open_tickets()
    if view == "unassigned":
        tickets = [ticket for ticket in tickets if not ticket["assigned_to"]]
        return {
            "message": capped_list(
                tickets, "Unassigned tickets", "No unassigned tickets.", limit
            )
        }
    if view == "mine":
        _, tech = require_tech(raw_sender)
        tickets = [ticket for ticket in tickets if tech.name in ticket["assigned_to"]]
        first = tech.name.split()[0]
        return {
            "message": capped_list(
                tickets,
                f"On {first}'s plate",
                f"Nothing on {first}'s plate.",
                limit,
            )
        }
    return {"message": capped_list(tickets, "Open tickets", "No open tickets. \U0001F389", limit)}


def claim(params: dict, raw_sender: Any) -> dict:
    telegram_id, tech = require_tech(raw_sender)
    task_id = int(params["task_id"])
    message = actions.claim_ticket(tech, task_id)
    state.record_claim(telegram_id, task_id)
    return {"message": message}


def close(params: dict, raw_sender: Any) -> dict:
    task_id = int(params["task_id"])
    speaker_id, log_tech, log_telegram_id = timesheet_actor(raw_sender, task_id, "close")
    hours = params.get("hours")
    automatic = ""
    if hours is None:
        elapsed = state.claim_elapsed_hours(log_telegram_id, task_id)
        if elapsed is None or not 0.05 <= elapsed <= 10:
            return {
                "needs_clarification": True,
                "message": (
                    f"How many hours should be logged for {actions.task_label(task_id)}? "
                    f"Say a number, or explicitly say no hours for #{task_id}."
                ),
            }
        hours = max(0.25, round(elapsed * 4) / 4)
        automatic = f" (auto: elapsed since {log_tech.name} took it)"
    message, timesheet_id = actions.close_ticket(
        log_tech, task_id, float(hours), str(params.get("comment") or "Completed")
    )
    # Remember it against the asker when we know them, so /amend last stays
    # theirs; otherwise against whoever the hours were logged for, so the entry
    # is still amendable by someone rather than by nobody.
    state.remember_entry(speaker_id if speaker_id is not None else log_telegram_id,
                         timesheet_id, task_id)
    state.clear_claims_for_task(task_id)
    return {"message": message + automatic}


def log_update(params: dict, raw_sender: Any) -> dict:
    task_id = int(params["task_id"])
    speaker_id, log_tech, log_telegram_id = timesheet_actor(raw_sender, task_id, "log")
    hours = params.get("hours")
    message, timesheet_id = actions.log_partial(
        log_tech,
        task_id,
        float(hours) if hours is not None else None,
        str(params["comment"]),
    )
    state.remember_entry(speaker_id if speaker_id is not None else log_telegram_id,
                         timesheet_id, task_id)
    if params.get("blocked"):
        message += " Ask before moving the ticket to a waiting/on-hold stage."
    return {"message": message}


def optional_tech(raw_sender: Any) -> actions.Tech | None:
    """The caller's technician record when we can identify them, else None."""
    telegram_id = sender_id(raw_sender)
    if telegram_id is None or str(telegram_id) not in config.USER_MAP:
        return None
    return actions.get_tech(telegram_id)


def require_owner(raw_sender: Any, sender_is_owner: bool) -> actions.Tech | None:
    """Authorize an action whose target is explicit, without identifying the caller.

    Assignment names its target in the request ("assign #6230 to Mike"), so the
    change is fully specified without knowing who asked for it. That matters
    because group chats give us no per-person identity, and requiring one there
    was blocking the ticket-card buttons for no security gain.

    Authorization still holds on two independent checks: the Telegram group and
    DM allowlists mean only the technicians can reach the bot at all, and
    `sender_is_owner` is the runtime's own verified owner bit, which cannot be
    forged from tool arguments. Actions that attribute *work* to a person -
    claiming, closing, logging hours - still require real identity.
    """
    tech = optional_tech(raw_sender)
    if tech is not None:
        return tech
    if sender_is_owner:
        return None
    raise PermissionError(
        "This action is only available to the field-service team.")


def ticket_action(
    operation: str, params: dict, raw_sender: Any, sender_is_owner: bool = False
) -> dict:
    task_id = int(params["task_id"])
    if operation == "reassign":
        # Target is explicit, so the caller does not have to be identified.
        tech = require_owner(raw_sender, sender_is_owner)
        return {
            "message": actions.reassign_ticket(
                tech, task_id, str(params["mode"]), list(params.get("names") or [])
            )
        }
    _, tech = require_tech(raw_sender)
    if operation == "schedule":
        return {"message": actions.schedule_ticket(tech, task_id, str(params["when"]))}
    if operation == "set_stage":
        return {
            "message": actions.set_stage(tech, task_id, str(params["stage_name"]))
        }
    raise ValueError(f"Unknown ticket operation: {operation}")


def create_ticket(params: dict, raw_sender: Any) -> dict:
    _, tech = require_tech(raw_sender)
    priority = actions.normalize_priority(params.get("priority"))
    if priority is None:
        return {
            "needs_clarification": True,
            "message": f"Priority for {params['title']}: high or low?",
        }
    return {
        "message": actions.create_ticket(
            tech,
            str(params["customer_name"]),
            str(params["title"]),
            str(params.get("description") or ""),
            params.get("billable"),
            params.get("assign_to"),
            params.get("deadline"),
            priority,
        )
    }


def create_subtask(params: dict, raw_sender: Any) -> dict:
    _, tech = require_tech(raw_sender)
    return {
        "message": actions.create_subtask(
            tech,
            int(params["parent_task_id"]),
            str(params["title"]),
            str(params.get("description") or ""),
            params.get("assign_to"),
            params.get("deadline"),
        )
    }


def activity(params: dict, raw_sender: Any) -> dict:
    operation = str(params["operation"])
    if operation == "list":
        who = params.get("who")
        if who == "me":
            _, tech = require_tech(raw_sender)
            who = tech.name.split()[0]
        return {"message": actions.list_activities(who)}
    _, tech = require_tech(raw_sender)
    task_id = int(params["task_id"])
    if operation == "schedule":
        return {
            "message": actions.schedule_activity(
                tech,
                task_id,
                str(params["summary"]),
                params.get("due"),
                params.get("assign_to"),
                params.get("kind") or "todo",
            )
        }
    if operation == "complete":
        return {
            "message": actions.complete_activity(
                tech, task_id, params.get("match"), params.get("feedback")
            )
        }
    if operation == "reschedule":
        return {
            "message": actions.reschedule_activity(
                tech, task_id, params.get("match"), str(params["due"])
            )
        }
    raise ValueError(f"Unknown activity operation: {operation}")


def amend_last(params: dict, raw_sender: Any) -> dict:
    telegram_id, _ = require_tech(raw_sender)
    timesheet_id = state.last_entry(telegram_id)
    if timesheet_id is None:
        return {"message": "No recent timesheet entry is available to amend."}
    return {
        "message": actions.amend_time(
            timesheet_id, params.get("hours"), params.get("comment")
        )
    }


def validated_media_path(raw_path: str, workspace_dir: str | None) -> Path:
    if not workspace_dir:
        raise PermissionError("No trusted workspace was supplied for the media file")
    workspace = Path(workspace_dir).expanduser().resolve()
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError("Media files must be inside the active workspace") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path.name}")
    max_bytes = int(os.environ.get("FIELDBOT_MAX_PHOTO_BYTES", "20000000"))
    if path.stat().st_size > max_bytes:
        raise ValueError(f"Photo exceeds the {max_bytes}-byte limit")
    mime, _ = mimetypes.guess_type(path.name)
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Only JPEG, PNG, and WebP photos may be attached")
    return path


def validated_audio_path(raw_path: str, workspace_dir: str | None) -> Path:
    if not workspace_dir:
        raise PermissionError("No trusted workspace was supplied for the audio file")
    workspace = Path(workspace_dir).expanduser().resolve()
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError("Audio files must be inside the active workspace") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path.name}")
    max_bytes = int(os.environ.get("FIELDBOT_MAX_AUDIO_BYTES", "25000000"))
    if path.stat().st_size > max_bytes:
        raise ValueError(f"Audio exceeds the {max_bytes}-byte limit")
    if path.suffix.lower() not in {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm"}:
        raise ValueError("Unsupported audio format")
    return path


def attach_photo(params: dict, raw_sender: Any, workspace_dir: str | None) -> dict:
    _, tech = require_tech(raw_sender)
    path = validated_media_path(str(params["file_path"]), workspace_dir)
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "message": actions.attach_photo(
            tech,
            int(params["task_id"]),
            str(params.get("note") or "Photo attached from Telegram"),
            image_b64,
            path.name,
        )
    }


def transcribe_voice(params: dict, raw_sender: Any, workspace_dir: str | None) -> dict:
    require_tech(raw_sender)
    path = validated_audio_path(str(params["file_path"]), workspace_dir)
    text = voice.transcribe(str(path))
    if not text:
        return {"message": "I could not detect speech in that voice note.", "transcript": ""}
    return {"message": text, "transcript": text}


def schedule_event(
    params: dict, raw_sender: Any, workspace_dir: str | None
) -> dict:
    telegram_id, _ = require_tech(raw_sender)
    start = datetime.datetime.fromisoformat(str(params["when"]))
    duration = float(params.get("duration_hours") or 1)
    end = start + datetime.timedelta(hours=duration)
    description = ""
    task_id = params.get("task_id")
    if task_id:
        task = actions.client.execute(
            "project.task", "read", [[int(task_id)]], {"fields": ["name"]}
        )[0]
        description = f"Ticket #{task_id}: {task['name']}"
    content = ics_util.build(
        str(params["title"]),
        start,
        end,
        description,
        params.get("location"),
    )
    if workspace_dir:
        workspace = Path(workspace_dir).expanduser().resolve()
        events_dir = workspace / "generated" / "events"
    else:
        events_dir = config.STATE_DIR / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    file_path = events_dir / f"event-{uuid.uuid4().hex[:12]}.ics"
    file_path.write_text(content, encoding="utf-8")
    state.remember_event(telegram_id, content, str(params["title"]))
    return {
        "message": (
            f"Calendar file created for {params['title']} at "
            f"{start:%Y-%m-%d %H:%M}. Send the attachment to the requester."
        ),
        "attachment_path": str(file_path),
    }


def sweep(params: dict, raw_sender: Any) -> dict:
    telegram_id, tech = require_tech(raw_sender)
    operation = str(params.get("operation") or "list")
    task_id = params.get("task_id")
    if operation == "list":
        unassigned = bool(params.get("unassigned"))
        snoozed = state.snoozed_task_ids()
        pool = [ticket for ticket in actions.open_tickets() if not ticket.get("next_step")]
        if unassigned:
            pool = [ticket for ticket in pool if not ticket["assigned_to"]]
        else:
            pool = [ticket for ticket in pool if tech.name in ticket["assigned_to"]]
        hidden = sum(1 for ticket in pool if ticket["task_id"] in snoozed)
        pool = [ticket for ticket in pool if ticket["task_id"] not in snoozed]
        pool.sort(key=lambda ticket: (
            ticket.get("priority") != "1", ticket.get("last_update") or ""
        ))
        shown = pool[:5]
        # Render here rather than in the caller: /sweep runs as a plugin command
        # that bypasses the model entirely, so nothing downstream is going to
        # turn this into prose. The tool path still gets the structured fields.
        header = "Unassigned, no next step" if unassigned else f"{tech.name.split()[0]}, no next step"
        empty = ("No unassigned tickets are missing a next step."
                 if unassigned else "Nothing of yours is missing a next step.")
        message = actions.format_ticket_list(shown, header, empty)
        if len(pool) > len(shown):
            message += f"\n…and {len(pool) - len(shown)} more."
        if hidden:
            message += f"\n({hidden} snoozed and hidden.)"
        return {
            "message": message,
            "tickets": shown,
            "remaining": len(pool),
            "snoozed": hidden,
        }
    if task_id is None:
        raise ValueError("task_id is required for this sweep operation")
    task_id = int(task_id)
    if operation == "close":
        message, _ = actions.close_ticket(tech, task_id, 0, "Closed during backlog sweep")
        state.clear_claims_for_task(task_id)
        return {"message": message}
    if operation == "snooze":
        due = (reports.now().date() + datetime.timedelta(days=7)).isoformat()
        state.snooze_task(task_id, due, telegram_id)
        return {"message": f"Snoozed {actions.task_label(task_id)} until {due}."}
    if operation == "unsnooze":
        state.clear_snooze(task_id)
        return {"message": f"Un-snoozed {actions.task_label(task_id)}."}
    if operation == "take":
        message = actions.claim_ticket(tech, task_id)
        state.record_claim(telegram_id, task_id)
        return {"message": message}
    if operation == "drop":
        return {
            "message": actions.reassign_ticket(
                tech, task_id, "remove", [tech.name.split()[0]]
            )
        }
    raise ValueError(f"Unknown sweep operation: {operation}")


def dispatch(operation: str, params: dict, envelope: dict) -> dict:
    raw_sender = envelope.get("requester_sender_id")
    workspace_dir = envelope.get("workspace_dir")
    # Runtime-verified owner bit. Trusted only because the plugin reads it from
    # the tool context; it is never accepted from tool arguments.
    sender_is_owner = envelope.get("sender_is_owner") is True
    if operation == "list_tickets":
        return list_tickets(params, raw_sender)
    if operation == "ticket_details":
        return {"message": actions.ticket_details(int(params["task_id"]))}
    if operation == "search_tickets":
        return {
            "message": actions.search_tickets(
                params.get("customer"), params.get("keyword")
            )
        }
    if operation == "customer_info":
        return {"message": actions.customer_info(str(params["customer_name"]))}
    if operation == "search_knowledge":
        return {"message": actions.search_knowledge(params.get("query"))}
    if operation == "read_article":
        return {"message": actions.read_article(int(params["article_id"]))}
    if operation == "claim":
        return claim(params, raw_sender)
    if operation == "close":
        return close(params, raw_sender)
    if operation == "log_update":
        return log_update(params, raw_sender)
    if operation in {"reassign", "schedule", "set_stage"}:
        return ticket_action(operation, params, raw_sender, sender_is_owner)
    if operation == "create_ticket":
        return create_ticket(params, raw_sender)
    if operation == "create_subtask":
        return create_subtask(params, raw_sender)
    if operation == "activity":
        return activity(params, raw_sender)
    if operation == "amend_last":
        return amend_last(params, raw_sender)
    if operation == "attach_photo":
        return attach_photo(params, raw_sender, workspace_dir)
    if operation == "transcribe_voice":
        return transcribe_voice(params, raw_sender, workspace_dir)
    if operation == "schedule_event":
        return schedule_event(params, raw_sender, workspace_dir)
    if operation == "sweep":
        return sweep(params, raw_sender)
    if operation == "poll_new_tickets":
        if raw_sender:
            raise PermissionError("New-ticket polling is restricted to automations")
        return {"tickets": reports.poll_new_tickets()}
    if operation == "report":
        kind = str(params["kind"])
        if kind == "morning":
            return {"message": reports.morning_dispatch(), "ready": True}
        if kind == "eod":
            return {"message": reports.eod_nudge(), "ready": True}
        if kind in {"daily", "weekly"}:
            return {"report": reports.recap_input(kind), "ready": False}
        if kind == "monthly_legacy":
            return {"message": actions.legacy_digest(), "ready": True}
        raise ValueError(f"Unknown report kind: {kind}")
    raise ValueError(f"Unknown operation: {operation}")


def main() -> int:
    if len(sys.argv) != 2:
        emit({"ok": False, "error": "Usage: bridge.py <operation>"})
        return 2
    operation = sys.argv[1]
    try:
        envelope = json.load(sys.stdin)
        if not isinstance(envelope, dict):
            raise ValueError("Bridge input must be a JSON object")
        params = envelope.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        log.info(
            "operation=%s sender=%s",
            operation,
            envelope.get("requester_sender_id") or "automation",
        )
        result = dispatch(operation, params, envelope)
        emit({"ok": True, **result})
        return 0
    except (
        PermissionError,
        ValueError,
        KeyError,
        FileNotFoundError,
        RuntimeError,
        OdooError,
    ) as exc:
        log.warning("operation=%s failed: %s", operation, exc)
        emit({"ok": False, "error": str(exc)[:500]})
        return 1
    except Exception as exc:
        log.exception("operation=%s failed unexpectedly", operation)
        emit({"ok": False, "error": f"Unexpected bridge failure: {str(exc)[:300]}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

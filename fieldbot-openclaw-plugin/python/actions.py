"""The fixed set of things the bot can do in Odoo.

Claude picks one of these and supplies parameters; only this code touches Odoo.
"""
import datetime
import html
import logging
import re
import time
from zoneinfo import ZoneInfo

import config
from odoo_client import client, OdooError
import redaction

log = logging.getLogger(__name__)

# --- in-memory cache (projects, stages) -------------------------------------
CACHE_TTL = 6 * 3600
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fetch()
    _cache[key] = (time.time(), val)
    return val

# Deliberately NO "description": it is never used here (ticket_details fetches
# it separately) and pulling 300 full email bodies made every board fetch ~1.2MB
# and 7x slower. "priority" IS fetched - it is one char, and now that new
# tickets must declare it the lists have to be able to show it.
TASK_FIELDS = [
    "id", "name", "partner_id", "project_id", "stage_id", "user_ids",
    "planned_date_begin", "date_deadline", "priority",
    "write_date", "activity_summary", "activity_date_deadline",
]


class Tech:
    """A registered technician: telegram id mapped to Odoo user + employee."""

    def __init__(self, telegram_id: int, odoo_login: str):
        self.telegram_id = telegram_id
        self.odoo_login = odoo_login
        users = client.search_read(
            "res.users", [["login", "=", odoo_login]], ["id", "name"], limit=1
        )
        if not users:
            raise OdooError(f"No Odoo user with login {odoo_login}")
        self.uid = users[0]["id"]
        self.name = users[0]["name"]
        emps = client.search_read(
            "hr.employee", [["user_id", "=", self.uid]], ["id"], limit=1
        )
        self.employee_id = emps[0]["id"] if emps else None


_techs: dict[int, Tech] = {}


def get_tech(telegram_id: int) -> Tech | None:
    if telegram_id not in _techs:
        login = config.USER_MAP.get(str(telegram_id))
        if not login:
            return None
        try:
            _techs[telegram_id] = Tech(telegram_id, login)
        except OdooError as e:
            log.error("Failed to resolve tech %s: %s", telegram_id, e)
            return None
    return _techs[telegram_id]


def _today() -> str:
    return datetime.datetime.now(ZoneInfo(config.TZNAME)).date().isoformat()


def all_projects() -> list[dict]:
    """All active projects (cached). Covers Field Service AND regular projects."""
    return _cached("projects", lambda: client.search_read(
        "project.project", [], ["id", "name", "is_fsm"]
    ))


def visible_projects() -> list[dict]:
    """Projects the bot works with - IGNORED_PROJECTS are invisible everywhere."""
    return [
        p for p in all_projects()
        if not any(sub in p["name"].lower() for sub in config.IGNORED_PROJECTS)
    ]


def visible_project_ids() -> list[int]:
    return [p["id"] for p in visible_projects()]


def fsm_project_ids() -> list[int]:
    return [p["id"] for p in visible_projects() if p.get("is_fsm")]


def project_stages(project_id: int) -> list[dict]:
    """Stages available in a project (cached per project)."""
    return _cached(f"stages:{project_id}", lambda: client.search_read(
        "project.task.type",
        [["project_ids", "in", [project_id]]],
        ["id", "name", "fold"],
    ))


def fresh_domain() -> list:
    """Domain terms excluding legacy (pre-LEGACY_CUTOFF) tickets.

    Deliberately create_date, not write_date: the old pile gets bulk-edited in
    Odoo from time to time, which bumps write_date on everything at once and
    would silently un-hide the lot. Creation date is stable.
    """
    cut = config.LEGACY_CUTOFF
    return [["create_date", ">=", f"{cut} 00:00:00"]] if cut else []


def legacy_domain() -> list:
    """The inverse of fresh_domain(): only the parked pre-cutoff tickets."""
    cut = config.LEGACY_CUTOFF
    return [["create_date", "<", f"{cut} 00:00:00"]] if cut else [["id", "=", 0]]


def _non_legacy_task_ids(task_ids: list[int]) -> set[int]:
    """Of the given tasks, those in visible projects and created since the cutoff."""
    if not task_ids:
        return set()
    return {t["id"] for t in client.search_read(
        "project.task",
        [["id", "in", task_ids], ["project_id", "in", visible_project_ids()]]
        + fresh_domain(),
        ["id"])}


def open_tickets() -> list[dict]:
    """Open (non-folded-stage) tasks across ALL projects, compact form.

    Legacy tickets (created before config.LEGACY_CUTOFF) are excluded - see
    legacy_tickets() and the monthly digest for those.
    """
    tasks = client.search_read(
        "project.task",
        [["project_id", "in", visible_project_ids()], ["stage_id.fold", "=", False]]
        + fresh_domain(),
        TASK_FIELDS,
        order="priority desc, id desc",
        limit=300,
    )
    return _compact(tasks)


def _compact(tasks: list[dict]) -> list[dict]:
    """Raw project.task rows -> the compact dicts the rest of the bot expects."""
    out = []
    for t in tasks:
        out.append({
            "task_id": t["id"],
            "title": t["name"],
            "customer": t["partner_id"][1] if t["partner_id"] else None,
            "project": t["project_id"][1] if t["project_id"] else None,
            "stage": t["stage_id"][1] if t["stage_id"] else None,
            "assigned_to": t["user_ids"],  # list of uids; resolved below
            "priority": str(t.get("priority") or "0"),
            "deadline": _to_local_date(t.get("date_deadline")),
            "scheduled": _to_local_date(t.get("planned_date_begin")),
            "last_update": _to_local_date(t.get("write_date")),
            "next_step": t.get("activity_summary") or None,
            "next_step_due": t.get("activity_date_deadline") or None,
        })
    # resolve assignee uids -> names in one query. active_test=False so tickets
    # still pinned to a deactivated user show a name instead of a bare uid.
    uids = sorted({u for t in out for u in t["assigned_to"]})
    names = {}
    if uids:
        for u in client.search_read("res.users", [["id", "in", uids]], ["id", "name"],
                                    context={"active_test": False}):
            names[u["id"]] = u["name"]
    for t in out:
        t["assigned_to"] = [names.get(u, str(u)) for u in t["assigned_to"]]
    return out


def legacy_tickets(owner: str | None = None) -> list[dict]:
    """The parked pre-cutoff tickets, oldest first. Optionally one person's."""
    domain = ([["project_id", "in", visible_project_ids()],
               ["stage_id.fold", "=", False]] + legacy_domain())
    if owner:
        hit = _resolve_user(owner)
        if hit is None:
            return []
        domain.append(["user_ids", "in", [hit[0]]])
    return _compact(client.search_read(
        "project.task", domain, TASK_FIELDS, order="create_date asc", limit=400))


def legacy_digest(owner: str | None = None, per_person: int = 8) -> str:
    """Monthly check-in on the parked pile: counts per person, a sample each.

    Grouped by assignee on purpose - the point of the pile is 'whose is this
    and are they ever going to do it', not chronological order.
    """
    cut = config.LEGACY_CUTOFF
    if not cut:
        return "Legacy filtering is off (LEGACY_CUTOFF is unset), so nothing is parked."
    tickets = legacy_tickets(owner)
    if not tickets:
        who = f" for {owner}" if owner else ""
        return f"🗄 Nothing parked{who} from before {cut}."

    def row(t):
        return (f"  • #{t['task_id']} {t['title'].strip()[:60]}"
                + (f" @ {t['customer']}" if t["customer"] else ""))

    tail = ("\nAsk for any of them by number (e.g. \"what's the latest on #2870?\") "
            "— parked only means hidden from the daily lists, not archived.")
    if owner:
        # one person's pile: a flat oldest-first list, no grouping. Grouping by
        # assignee here would double-count tickets they share with someone else.
        hit = _resolve_user(owner)
        out = [f"🗄 Parked tickets for {hit[1] if hit else owner} "
               f"(created before {cut}) — {len(tickets)}, oldest first:"]
        out += [row(t) for t in tickets[:40]]
        if len(tickets) > 40:
            out.append(f"  …and {len(tickets) - 40} more")
        return "\n".join(out) + "\n" + tail

    groups: dict[str, list[dict]] = {}
    for t in tickets:
        for name in (t["assigned_to"] or ["unassigned"]):
            groups.setdefault(name, []).append(t)
    out = [f"🗄 Parked tickets (created before {cut}) — {len(tickets)} open, "
           f"hidden from the board and next-step lists.",
           "(a ticket with two assignees is listed under both)"]
    for name, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(f"\n{name} — {len(rows)}:")
        out += [row(t) for t in rows[:per_person]]
        if len(rows) > per_person:
            out.append(f"  …and {len(rows) - per_person} more")
    out.append(tail)
    return "\n".join(out)


def _done_stage_for(task_id: int) -> int | None:
    task = client.execute("project.task", "read", [[task_id]], {"fields": ["project_id"]})[0]
    pid = task["project_id"][0]
    stages = project_stages(pid)
    # mirror the GUI "Mark as Done" flow: Closed is the target stage
    for wanted in ("closed", "done", "completed", "complete"):
        for s in stages:
            if s["name"].strip().lower() == wanted:
                return s["id"]
    # fallback: any folded stage that isn't a cancellation
    for s in stages:
        if s["fold"] and "cancel" not in s["name"].lower():
            return s["id"]
    return None


def _esc(text: str) -> str:
    """Escape user-supplied text before it lands in Odoo's HTML chatter."""
    return html.escape(text or "", quote=True)


def _post_note(task_id: int, body_html: str):
    # Odoo escapes RPC body strings (they render as literal tags), so flatten
    # the HTML our callers build into plain text before posting.
    client.execute("project.task", "message_post", [[task_id]],
                   {"body": _strip_html(body_html)})


def _log_time(task_id: int, tech: Tech, hours: float, desc: str):
    task = client.execute(
        "project.task", "read", [[task_id]], {"fields": ["project_id"]}
    )[0]
    vals = {
        "task_id": task_id,
        "project_id": task["project_id"][0],
        "unit_amount": hours,
        "name": desc or "Field service work",
        "date": _today(),
    }
    if tech.employee_id:
        vals["employee_id"] = tech.employee_id
    else:
        vals["user_id"] = tech.uid
    return client.execute("account.analytic.line", "create", [vals])


# ---- the whitelisted actions Claude can invoke -----------------------------

def claim_ticket(tech: Tech, task_id: int) -> str:
    t = client.execute(
        "project.task", "read", [[task_id]], {"fields": ["name", "user_ids"]}
    )[0]
    already = [u for u in t["user_ids"] if u != tech.uid]
    client.execute("project.task", "write", [[task_id], {"user_ids": [[4, tech.uid]]}])
    _post_note(task_id, f"<p>{_esc(tech.name)} took this ticket (via Telegram)</p>")
    note = ""
    if already:
        names = client.search_read("res.users", [["id", "in", already]], ["name"])
        note = f" (heads up: also assigned to {', '.join(n['name'] for n in names)})"
    return (f"✅ {tech.name} took: {t['name']} (#{task_id}){note}"
            + ensure_default_activity(task_id, tech.uid, tech.name))


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html_text: str) -> str:
    """Odoo chatter/description HTML -> readable plain text.

    Two passes: Odoo escapes RPC-posted bodies, so older bot notes are
    stored double-encoded ('<p>&lt;p&gt;...').
    """
    text = html_text
    for _ in range(2):
        text = re.sub(r"</p>|<br\s*/?>", "\n", text)
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _to_local_date(odoo_dt) -> str | None:
    """Odoo UTC datetime/date string -> local 'YYYY-MM-DD' (None if empty)."""
    if not odoo_dt:
        return None
    if len(odoo_dt) >= 19:
        try:
            dt = datetime.datetime.strptime(odoo_dt[:19], "%Y-%m-%d %H:%M:%S")
            return (dt.replace(tzinfo=datetime.timezone.utc)
                      .astimezone(ZoneInfo(config.TZNAME)).date().isoformat())
        except ValueError:
            pass
    return odoo_dt[:10]


def deadline_flag(deadline: str | None) -> str:
    """Suffix flagging a local YYYY-MM-DD deadline: overdue / due today / due date."""
    if not deadline:
        return ""
    try:
        d = datetime.date.fromisoformat(deadline[:10])
    except ValueError:
        return ""
    today = datetime.datetime.now(ZoneInfo(config.TZNAME)).date()
    if d < today:
        return f" ‼️ overdue since {d:%b %d}"
    if d == today:
        return " ⏰ due TODAY"
    return f" (due {d:%b %d})"


def find_tech_by_first_name(name: str) -> Tech | None:
    """Match a registered technician by an exact, declared identifier.

    Accepts, all case-insensitive but otherwise exact: first name, full name,
    Odoo login, the local part of that login, and any nickname declared in
    `technicianAliases`. Never substring or prefix matching - "matt" resolves
    because it is declared as an alias for Matthew Cattaneo, not because it
    happens to be a prefix of "Matthew". A partial match would silently assign
    a customer's work to the wrong person.
    """
    want = (name or "").strip().lower()
    if not want:
        return None

    # A declared nickname resolves to exactly one login, or to nothing.
    alias_login = config.TECH_ALIASES.get(want)

    for tg_id in config.USER_MAP:
        t = get_tech(int(tg_id))
        if not t:
            continue
        login = (t.odoo_login or "").lower()
        identifiers = {
            t.name.strip().lower(),
            t.name.split()[0].lower(),
            login,
            login.split("@", 1)[0],
        }
        if want in identifiers or (alias_login and login == alias_login.lower()):
            return t
    return None


def known_technician_names() -> str:
    """Human-readable list of who can be assigned, for error messages."""
    names = []
    for tg_id in config.USER_MAP:
        t = get_tech(int(tg_id))
        if t:
            names.append(t.name)
    return ", ".join(sorted(names)) or "nobody is registered"


# Tickets the bot itself created this process - the new-ticket poller skips
# these so we don't announce our own creations back to the chat.
_bot_created_ids: set[int] = set()


def mark_bot_created(task_id: int) -> None:
    _bot_created_ids.add(task_id)


def task_label(task_id: int) -> str:
    """'Name (#id)' for user-facing messages; falls back to '#id'."""
    try:
        t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
        return f"{t['name']} (#{task_id})"
    except OdooError:
        return f"#{task_id}"


def close_ticket(tech: Tech, task_id: int, hours: float, comment: str) -> tuple[str, int | None]:
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    ts_id = _log_time(task_id, tech, hours, comment) if hours else None
    _post_note(
        task_id,
        f"<p><b>Completed</b> by {_esc(tech.name)}: {_esc(comment)}"
        + (f" ({hours}h)</p>" if hours else "</p>"),
    )
    stage = _done_stage_for(task_id)
    stage_msg = ""
    # like the GUI "Mark as Done" button: set the state AND move the stage
    vals = {"state": "1_done"}
    if stage:
        vals["stage_id"] = stage
    else:
        stage_msg = " (no Closed stage found - state marked done, stage unchanged)"
    client.execute("project.task", "write", [[task_id], vals])
    logged = f"{hours}h logged for {tech.name}" if hours else "no time logged"
    return f"✅ Closed: {t['name']} (#{task_id}) - {logged}{stage_msg}", ts_id


def log_partial(
    tech: Tech, task_id: int, hours: float | None, comment: str
) -> tuple[str, int | None]:
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    ts_id = None
    if hours:
        ts_id = _log_time(task_id, tech, hours, comment)
    _post_note(task_id, f"<p><b>Update</b> from {_esc(tech.name)}: {_esc(comment)}"
                        + (f" ({hours}h)" if hours else "") + "</p>")
    hrs = f"{hours}h logged, " if hours else ""
    return f"\U0001F4DD Noted on {t['name']} (#{task_id}): {hrs}ticket stays open", ts_id


def _deadline_range(day: str) -> tuple[str, str]:
    """Local 'YYYY-MM-DD' -> (planned_date_begin, date_deadline) UTC strings.

    Odoo silently drops a bare date_deadline write on tasks unless the
    planned range is written along with it; use 9am-5pm local for the day.
    """
    tz = ZoneInfo(config.TZNAME)
    d = datetime.date.fromisoformat(day)
    fmt = "%Y-%m-%d %H:%M:%S"
    utc = datetime.timezone.utc
    start = datetime.datetime.combine(d, datetime.time(9, 0), tz)
    end = datetime.datetime.combine(d, datetime.time(17, 0), tz)
    return start.astimezone(utc).strftime(fmt), end.astimezone(utc).strftime(fmt)


def _assignment_vals(assign_to: str | None, deadline: str | None,
                     vals: dict) -> tuple[str, tuple[int, str] | None]:
    """Apply assignee/deadline to a task's create vals.

    Returns (reply suffix, (owner uid, owner name) or None).
    """
    note, owner = "", None
    if assign_to:
        t = find_tech_by_first_name(assign_to)
        if t:
            vals["user_ids"] = [[6, 0, [t.uid]]]
            note = f", assigned to {t.name}"
            owner = (t.uid, t.name)
        else:
            note = f" (no tech named '{assign_to}' - left unassigned)"
    if deadline:
        vals["planned_date_begin"], vals["date_deadline"] = _deadline_range(deadline)
        note += f", due {deadline}"
    return note, owner


def create_subtask(tech: Tech, parent_task_id: int, title: str, description: str,
                   assign_to: str | None = None, deadline: str | None = None) -> str:
    parent = client.execute(
        "project.task", "read", [[parent_task_id]],
        {"fields": ["name", "project_id", "partner_id"]},
    )[0]
    vals = {
        "name": title,
        "parent_id": parent_task_id,
        "project_id": parent["project_id"][0],
    }
    if parent["partner_id"]:
        vals["partner_id"] = parent["partner_id"][0]
    if description:
        vals["description"] = f"<p>{_esc(description)}</p>"
    note, owner = _assignment_vals(assign_to, deadline, vals)
    task_id = client.execute("project.task", "create", [vals])
    mark_bot_created(task_id)
    _post_note(task_id, f"<p>Created by {_esc(tech.name)} via Telegram as a "
                        f"sub-task of #{parent_task_id}</p>")
    # the sub-task's own action doubles as its owner's next step
    tail = (ensure_default_activity(task_id, owner[0], owner[1],
                                    summary=title, due=deadline) if owner else "")
    return (f"🆕 Sub-task #{task_id} under {parent['name']} (#{parent_task_id}): "
            f"{title}{note}{tail}")


def normalize_priority(value) -> str | None:
    """Free text -> Odoo's project.task priority ('0' Low / '1' High), or None.

    None means 'the sender never said', which callers must treat as a question
    to ask - not as a default. Odoo itself defaults to '0', so a silent default
    would look identical to a deliberate Low.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("1", "high", "hi", "urgent", "important", "asap", "critical", "emergency"):
        return "1"
    if v in ("0", "low", "lo", "normal", "routine", "whenever", "standard"):
        return "0"
    return None


PRIORITY_LABEL = {"1": "🔴 High", "0": "Low"}


def create_ticket(tech: Tech, customer_name: str, title: str, description: str,
                  billable: bool | None = None, assign_to: str | None = None,
                  deadline: str | None = None, priority: str | None = None) -> str:
    prio = normalize_priority(priority)
    if prio is None:
        # Callers must resolve this before getting here (main.py asks with
        # buttons). Guard anyway so no path can create an unprioritised ticket.
        raise OdooError(
            f"Priority is required on new tickets - low or high? (for '{title}')")
    fsm = [p for p in visible_projects() if p.get("is_fsm")]
    if not fsm:
        raise OdooError("No Field Service project found")
    # Pick Billable vs Non-Billable helpdesk; default to non-billable
    want_billable = bool(billable)
    pick = None
    for p in fsm:
        name = p["name"].lower()
        is_non = "non" in name
        if want_billable and "billable" in name and not is_non:
            pick = p
            break
        if not want_billable and is_non:
            pick = p
            break
    pick = pick or fsm[0]
    # companies first; contacts only if no company matches
    partners = client.search_read(
        "res.partner",
        [["is_company", "=", True], ["name", "ilike", customer_name]],
        ["id", "name"], limit=5,
    )
    if not partners:
        partners = client.search_read(
            "res.partner", [["name", "ilike", customer_name]], ["id", "name"], limit=5
        )
    if len(partners) > 1:
        opts = "\n".join(f"• {p['name']}" for p in partners)
        return (f"Which customer did you mean?\n{opts}\n"
                "Say it again with the exact name and I'll create it.")
    partner_id = partners[0]["id"] if partners else None
    vals = {"name": title, "project_id": pick["id"], "priority": prio}
    if partner_id:
        vals["partner_id"] = partner_id
    if description:
        vals["description"] = f"<p>{_esc(description)}</p>"
    note, owner = _assignment_vals(assign_to, deadline, vals)
    task_id = client.execute("project.task", "create", [vals])
    mark_bot_created(task_id)
    who = partners[0]["name"] if partners else f"'{customer_name}' (no matching customer found)"
    _post_note(task_id, f"<p>Created by {_esc(tech.name)} via Telegram</p>")
    tail = ensure_default_activity(task_id, owner[0], owner[1]) if owner else ""
    flag = " 🔴 HIGH" if prio == "1" else ""
    return (f"\U0001F195 Created ticket #{task_id}: {title} for {who} → "
            f"{pick['name']}{flag}{note}{tail}")


def schedule_ticket(tech: Tech, task_id: int, when: str) -> str:
    """Set the planned visit datetime. `when` is 'YYYY-MM-DD HH:MM' local."""
    start = datetime.datetime.fromisoformat(when)
    end = start + datetime.timedelta(hours=2)
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    client.execute("project.task", "write", [[task_id], {
        "planned_date_begin": start.strftime("%Y-%m-%d %H:%M:%S"),
        "date_deadline": end.strftime("%Y-%m-%d %H:%M:%S"),
    }])
    _post_note(task_id, f"<p>Scheduled by {_esc(tech.name)} for {start:%a %b %d, %I:%M %p}</p>")
    return f"📅 Scheduled: {t['name']} (#{task_id}) → {start:%a %b %d, %I:%M %p}"


def attach_photo(tech: Tech, task_id: int, note: str, image_b64: str,
                 filename: str) -> str:
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    att_id = client.execute("ir.attachment", "create", [{
        "name": filename,
        "datas": image_b64,
        "res_model": "project.task",
        "res_id": task_id,
    }])
    body = f"<p>📷 Photo from {_esc(tech.name)}"
    if note:
        body += f": {_esc(note)}"
    body += "</p>"
    client.execute("project.task", "message_post", [[task_id]],
                   {"body": body, "attachment_ids": [att_id]})
    return f"📷 Photo attached to {t['name']} (#{task_id})" + (f" — {note}" if note else "")


def my_tickets(tech: Tech) -> list[dict]:
    return [t for t in open_tickets() if tech.name in t["assigned_to"]]


def timesheet_exists_today(employee_id: int, task_id: int, today: str) -> bool:
    rows = client.search_read(
        "account.analytic.line",
        [["task_id", "=", task_id], ["employee_id", "=", employee_id],
         ["date", "=", today]],
        ["id"], limit=1,
    )
    return bool(rows)


def ticket_details(task_id: int) -> str:
    """Full readout: description, deadline, hours logged, recent chatter notes."""
    t = client.execute("project.task", "read", [[task_id]], {"fields": [
        "name", "partner_id", "project_id", "stage_id", "user_ids", "parent_id",
        "date_deadline", "planned_date_begin", "description", "write_date",
    ]})[0]
    who = "unassigned"
    if t["user_ids"]:
        users = client.search_read("res.users", [["id", "in", t["user_ids"]]], ["name"])
        who = ", ".join(u["name"] for u in users)
    lines = [f"🎫 #{task_id} {t['name']}"]
    if t["partner_id"]:
        lines.append(f"Customer: {t['partner_id'][1]}")
    proj = t["project_id"][1] if t["project_id"] else "?"
    stage = t["stage_id"][1] if t["stage_id"] else "?"
    lines.append(f"{proj} · [{stage}] · {who}")
    if t["parent_id"]:
        lines.append(f"Sub-task of {t['parent_id'][1]} (#{t['parent_id'][0]})")
    dl = _to_local_date(t.get("date_deadline"))
    if dl:
        lines.append(f"Deadline: {dl}{deadline_flag(dl)}")
    desc = _strip_html(t.get("description") or "")
    if desc:
        lines.append(f"\n{desc[:400]}" + ("…" if len(desc) > 400 else ""))
    ts = client.search_read(
        "account.analytic.line", [["task_id", "=", task_id]],
        ["employee_id", "unit_amount", "name", "date"], order="date desc", limit=50,
    )
    if ts:
        total = sum(r["unit_amount"] for r in ts)
        lines.append(f"\n⏱ {total:g}h logged:")
        for r in ts[:5]:
            emp = r["employee_id"][1] if r["employee_id"] else "?"
            lines.append(f"• {r['date']} {emp}: {r['unit_amount']:g}h — {(r['name'] or '')[:60]}")
        if len(ts) > 5:
            lines.append(f"  …and {len(ts) - 5} more entries")
    msgs = client.search_read(
        "mail.message",
        [["model", "=", "project.task"], ["res_id", "=", task_id],
         ["message_type", "in", ["comment", "email", "notification"]]],
        ["date", "author_id", "body"], order="date desc", limit=8,
    )
    notes = []
    for m in msgs:
        body = _strip_html(m.get("body") or "")
        if body:
            author = m["author_id"][1] if m["author_id"] else "?"
            notes.append(f"• {m['date'][:10]} {author}: {body[:200]}")
    if notes:
        lines.append("\n📝 Recent notes (newest first):")
        lines.extend(notes)
    acts = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["res_id", "=", task_id]],
        ["activity_type_id", "summary", "date_deadline", "user_id"],
        order="date_deadline asc",
    )
    if acts:
        lines.append("\n📌 Next steps:")
        for a in acts:
            kind = a["activity_type_id"][1] if a["activity_type_id"] else "To-Do"
            owner = a["user_id"][1] if a["user_id"] else "?"
            lines.append(f"• {kind}: {a['summary'] or '(no summary)'} — "
                         f"{owner}{deadline_flag(a['date_deadline'])}")
    att = client.execute("ir.attachment", "search_count",
                         [[["res_model", "=", "project.task"], ["res_id", "=", task_id]]])
    if att:
        lines.append(f"\n📎 {att} attachment(s)")
    return "\n".join(lines)


def _resolve_user(name: str) -> tuple[int, str] | None:
    """Name -> (odoo uid, full name), by exact match only.

    Registered technicians resolve first, via their declared identifiers. Anyone
    else must be named exactly as Odoo spells them: `=ilike` is a
    case-insensitive equality test, unlike `ilike`, which wraps the value in
    wildcards and matches substrings. That distinction is the whole point -
    substring matching previously meant "matt" resolved only by luck, and would
    have silently picked the wrong person the moment a second matching user
    existed.
    """
    t = find_tech_by_first_name(name)
    if t:
        return t.uid, t.name

    want = (name or "").strip()
    if not want:
        return None
    users = client.search_read(
        "res.users", [["name", "=ilike", want]], ["id", "name"], limit=2
    )
    # Ambiguity is a refusal, not a coin flip.
    if len(users) == 1:
        return users[0]["id"], users[0]["name"]
    return None


def reassign_ticket(
    tech: Tech | None, task_id: int, mode: str, names: list[str]
) -> str:
    """Assign a ticket to named technicians.

    `tech` is the person making the change and is used only for the audit note.
    It is optional because assignment can be driven from a ticket card button in
    the group chat, where OpenClaw gives us no per-person identity - the target
    is explicit in the button payload, so the change is fully specified without
    knowing who tapped it. The note then records that honestly rather than
    guessing at a name.
    """
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name", "user_ids"]})[0]
    current = set(t["user_ids"])
    ids, unknown, resolved = set(), [], []
    for n in names:
        hit = _resolve_user(n)
        if hit:
            ids.add(hit[0])
            resolved.append(hit)
        else:
            unknown.append(n)
    if not ids:
        return (f"Couldn't match anyone named {', '.join(unknown) or '?'} "
                f"to an Odoo user - nothing changed. "
                f"Assignable technicians: {known_technician_names()}.")
    new = ids if mode == "set" else (current | ids if mode == "add" else current - ids)
    client.execute("project.task", "write", [[task_id], {"user_ids": [[6, 0, sorted(new)]]}])
    if new:
        users = client.search_read("res.users", [["id", "in", sorted(new)]], ["name"])
        now = ", ".join(u["name"] for u in users)
    else:
        now = "nobody (unassigned)"
    by = f"by {_esc(tech.name)}" if tech else "from a Telegram ticket card"
    _post_note(task_id, f"<p>Assignment changed {by}: now {_esc(now)}</p>")
    extra = f" (couldn't match: {', '.join(unknown)})" if unknown else ""
    tail = ""
    if mode in ("set", "add") and resolved:
        tail = ensure_default_activity(task_id, resolved[0][0], resolved[0][1])
    return f"👥 {t['name']} (#{task_id}) → now assigned to {now}{extra}{tail}"


def search_tickets(customer: str | None, keyword: str | None) -> str:
    """History search across ALL tickets - open, closed, and archived."""
    if not (customer or keyword):
        return "Give me a customer name or keyword to search for."
    domain = [["project_id", "in", visible_project_ids()],
              ["active", "in", [True, False]]]
    label = []
    if customer:
        domain.append(["partner_id.name", "ilike", customer])
        label.append(customer)
    if keyword:
        domain.append(["name", "ilike", keyword])
        label.append(f"'{keyword}'")
    tasks = client.search_read(
        "project.task", domain,
        ["id", "name", "partner_id", "stage_id", "write_date"],
        order="write_date desc", limit=15,
    )
    what = " ".join(label)
    if not tasks:
        return f"🔎 Nothing found for {what} (searched open and closed tickets)."
    lines = [f"🔎 {what} — {len(tasks)} most recent (open and closed):"]
    for t in tasks:
        cust = f" @ {t['partner_id'][1]}" if t["partner_id"] else ""
        stage = t["stage_id"][1] if t["stage_id"] else "?"
        lines.append(f"• #{t['id']} {t['name']}{cust} [{stage}] — {_to_local_date(t['write_date'])}")
    return "\n".join(lines)


CONTACT_FIELDS = ["name", "phone", "mobile", "email",
                  "street", "street2", "city", "state_id", "zip"]


def customer_info(customer_name: str) -> str:
    partners = client.search_read(
        "res.partner",
        [["is_company", "=", True], ["name", "ilike", customer_name]],
        CONTACT_FIELDS, limit=3,
    )
    if not partners:
        partners = client.search_read(
            "res.partner", [["name", "ilike", customer_name]], CONTACT_FIELDS, limit=3,
        )
    if not partners:
        return f"No customer matching '{customer_name}' in Odoo."
    out = []
    for p in partners:
        lines = [f"👤 {p['name']}"]
        if p.get("phone"):
            lines.append(f"📞 {p['phone']}")
        if p.get("mobile"):
            lines.append(f"📱 {p['mobile']}")
        if p.get("email"):
            lines.append(f"✉️ {p['email']}")
        addr = ", ".join(str(x) for x in [
            p.get("street"), p.get("street2"), p.get("city"),
            p["state_id"][1] if p.get("state_id") else None, p.get("zip"),
        ] if x)
        if addr:
            lines.append(f"📍 {addr}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


# ---- Knowledge base (read-only) --------------------------------------------
# Every string these functions return has been through _safe_kb_text() on the
# FULL article first: credential lines withheld, then stray values redacted.
# Snippets and truncation happen afterwards, so a cut can never expose a line
# that protection removed.

KB_FIELDS = ["id", "name", "body", "parent_id", "write_date"]
KB_SEARCH_LIMIT = 10
KB_SNIPPET_CHARS = 160
KB_READ_MAX_CHARS = 6000


def _knowledge_text(body_html: str | None) -> str:
    """Knowledge article HTML -> plain text that keeps table cells apart.

    _strip_html alone runs adjacent cells together, which would glue a
    "Password" label to its value somewhere the redactor cannot see it.
    """
    text = body_html or ""
    text = re.sub(r"<img\b[^>]*>", " [image] ", text, flags=re.I)  # no base64 dumps
    text = re.sub(r"</t[dh]\s*>", " | ", text, flags=re.I)
    text = re.sub(r"<li\b[^>]*>", "\n• ", text, flags=re.I)
    text = re.sub(r"</(?:tr|li|h[1-6]|div|pre|table|ul|ol)\s*>|<hr\b[^>]*>",
                  "\n", text, flags=re.I)
    text = _strip_html(text)
    text = re.sub(r"[ \t]*\|[ \t]*(?=\n|$)", "", text)  # trailing cell separators
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _safe_kb_text(body_html: str | None) -> tuple[str, int, int]:
    """Article text an agent may see: (text, lines withheld, values redacted).

    Withholding runs first and is the real control; value redaction then
    catches stray card numbers and the like on the lines that remain.
    """
    text, withheld = redaction.withhold_credential_lines(_knowledge_text(body_html))
    text, redacted = redaction.redact(text)
    return text, withheld, redacted


def _kb_protection_note(withheld: int, redacted: int) -> str:
    parts = []
    if withheld:
        parts.append(f"{withheld} line(s) withheld")
    if redacted:
        parts.append(f"{redacted} value(s) redacted")
    if not parts:
        return ""
    return f"🔒 Credentials protected: {', '.join(parts)}. Open the article in Odoo to see them."


def _kb_snippet(text: str, query: str) -> str:
    """A short window around the first hit, taken from already-redacted text."""
    flat = re.sub(r"\s+", " ", text).strip()
    at = flat.lower().find(query.lower())
    if at < 0:
        return flat[:KB_SNIPPET_CHARS] + ("…" if len(flat) > KB_SNIPPET_CHARS else "")
    start = max(0, at - KB_SNIPPET_CHARS // 3)
    end = min(len(flat), start + KB_SNIPPET_CHARS)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def search_knowledge(query: str | None) -> str:
    """Find Knowledge articles by title or text. Templates never appear."""
    q = (query or "").strip()
    if len(q) < 2:
        return "Give me a word or phrase to search the knowledge base for."
    domain = [["is_template", "=", False],
              "|", ["name", "ilike", q], ["body", "ilike", q]]
    rows = client.search_read(
        "knowledge.article", domain, KB_FIELDS,
        order="write_date desc", limit=KB_SEARCH_LIMIT,
    )
    ql = q.lower()
    hits = []
    withheld_total = redacted_total = 0
    for a in rows:
        title, n_title = redaction.redact(a.get("name") or "(untitled)")
        body, n_withheld, n_body = _safe_kb_text(a.get("body"))
        # Only keep an article if the query survives protection. Otherwise a
        # search for a guessed password would confirm which article holds it.
        # The withheld marker's own words must not count as a match.
        visible = re.sub(r"\s+", " ", body.replace(redaction.WITHHELD, " ")).lower()
        if ql not in title.lower() and ql not in visible:
            continue
        withheld_total += n_withheld
        redacted_total += n_title + n_body
        hits.append((a, title, body))
    if not hits:
        return f"📘 Nothing in the knowledge base matches '{q}'."
    hits.sort(key=lambda h: ql not in h[1].lower())  # title hits first, stable
    noun = "match" if len(hits) == 1 else "matches"
    lines = [f"📘 Knowledge base — {len(hits)} {noun} for '{q}':"]
    for a, title, body in hits:
        where = f" (in {redaction.redact(a['parent_id'][1])[0]})" if a.get("parent_id") else ""
        lines.append(f"• #{a['id']} {title}{where} — updated {_to_local_date(a.get('write_date'))}")
        lines.append(f"  {_kb_snippet(body, q)}")
    note = _kb_protection_note(withheld_total, redacted_total)
    if note:
        lines.append(note)
    lines.append("Read one in full by its # number.")
    return "\n".join(lines)


def read_article(article_id: int) -> str:
    """One Knowledge article in full, redacted and capped."""
    rows = client.search_read(
        "knowledge.article",
        [["id", "=", article_id], ["is_template", "=", False]],
        KB_FIELDS, limit=1,
    )
    if not rows:
        return f"No knowledge article #{article_id} (it may not exist, or it isn't visible)."
    a = rows[0]
    title, n_title = redaction.redact(a.get("name") or "(untitled)")
    text, n_withheld, n_body = _safe_kb_text(a.get("body"))
    header = [f"📘 {title} (#{a['id']})"]
    if a.get("parent_id"):
        header.append(f"In: {redaction.redact(a['parent_id'][1])[0]}")
    header.append(f"Updated {_to_local_date(a.get('write_date'))} — "
                  f"{config.ODOO_URL}/knowledge/article/{a['id']}")
    if len(text) > KB_READ_MAX_CHARS:
        total = len(text)
        text = (text[:KB_READ_MAX_CHARS].rstrip()
                + f"\n\n…truncated ({total:,} characters in full; open the link for the rest).")
    if not text:
        text = "(This article has no text.)"
    note = _kb_protection_note(n_withheld, n_title + n_body)
    footer = f"\n\n{note}" if note else ""
    return "\n".join(header) + "\n\n" + text + footer


_model_ids: dict[str, int] = {}


def _model_id(model: str) -> int:
    if model not in _model_ids:
        _model_ids[model] = client.search_read(
            "ir.model", [["model", "=", model]], ["id"], limit=1)[0]["id"]
    return _model_ids[model]


_TYPE_WORDS = {
    "todo": ("to-do", "to do", "todo"),
    "call": ("call",),
    "email": ("email",),
    "meeting": ("meeting",),
    "triage": ("triage",),
}


def _activity_type(kind: str) -> int | None:
    """Odoo activity-type id for one of our kinds, or None if Odoo has no such
    type. Cached for CACHE_TTL, so a type added in Odoo is picked up without a
    redeploy. No silent fallback: callers decide what a miss means."""
    types = _cached("activity_types", lambda: {
        t["name"].strip().lower(): t["id"]
        for t in client.search_read("mail.activity.type", [], ["id", "name"])})
    for w in _TYPE_WORDS.get(kind, (kind,)):
        for name, tid in types.items():
            if w == name or w in name:
                return tid
    return None


def ensure_default_activity(task_id: int, owner_uid: int, owner_name: str,
                            summary: str | None = None, due: str | None = None,
                            kind: str | None = None) -> str:
    """Give a newly assigned ticket a next step - but never invent one.

    An explicit summary (a sub-task's own title, i.e. words the tech actually
    said) is always created. With no summary the only default we'll set is a
    generic "Triage", and only if Odoo has a Triage activity type. Otherwise
    the ticket is deliberately left with no next step, so it surfaces in the
    morning dispatch and /sweep and a human picks the real one.

    (This replaced a "Call <customer> - first contact" default, which produced
    nonsense on email-created tickets - the partner is the sender, so it told
    people to phone no-reply@ addresses and, once, themselves.)
    """
    try:
        if client.search_read(
                "mail.activity",
                [["res_model", "=", "project.task"], ["res_id", "=", task_id]],
                ["id"], limit=1):
            return ""
        if summary is None:
            if _activity_type("triage") is None:
                return ""  # intentional: /sweep forces a real choice
            summary, kind = "Triage", "triage"
        vals = {
            "res_model_id": _model_id("project.task"),
            "res_id": task_id,
            "summary": summary,
            "date_deadline": (due or _today())[:10],
            "user_id": owner_uid,
        }
        at = _activity_type(kind or "todo") or _activity_type("todo")
        if at:
            vals["activity_type_id"] = at
        client.execute("mail.activity", "create", [vals])
        return (f"\n📌 Next step: {summary} — {owner_name.split()[0]}, "
                f'due {vals["date_deadline"]} (change it: "next step on #{task_id}: ...")')
    except OdooError as e:
        log.warning("default next step failed for #%s: %s", task_id, e)
        return ""


def schedule_activity(tech: Tech, task_id: int, summary: str, due: str | None,
                      assign_to: str | None = None, kind: str = "todo") -> str:
    """Set a 'next step' on a ticket - a native Odoo activity."""
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    uid, uname = tech.uid, tech.name
    warn = ""
    if assign_to:
        hit = _resolve_user(assign_to)
        if hit:
            uid, uname = hit
        else:
            # Say plainly that the requested person was not used. Quietly
            # redirecting someone else's next step onto the requester is exactly
            # the kind of mismanaged assignment this must not do.
            warn = (f" (couldn't match '{assign_to}', so this was NOT assigned "
                    f"to them - it is on {uname}. "
                    f"Assignable technicians: {known_technician_names()})")
    due = due or _today()
    vals = {
        "res_model_id": _model_id("project.task"),
        "res_id": task_id,
        "summary": summary,
        "date_deadline": due,
        "user_id": uid,
    }
    at = _activity_type(kind)
    if at:
        vals["activity_type_id"] = at
    client.execute("mail.activity", "create", [vals])
    return (f"📌 Next step on {t['name']} (#{task_id}): {summary} — "
            f"{uname}{deadline_flag(due)}{warn}")


def complete_activity(tech: Tech, task_id: int, match: str | None,
                      feedback: str | None) -> str:
    """Mark a ticket's next step done; the outcome lands in Odoo chatter."""
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    acts = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["res_id", "=", task_id]],
        ["id", "summary"], order="date_deadline asc",
    )
    if not acts:
        return f"No open next steps on {t['name']} (#{task_id})."
    picked = acts
    if match:
        m = match.lower()
        hits = [a for a in acts if m in (a["summary"] or "").lower()]
        if hits:
            picked = hits
    if len(picked) > 1:
        opts = "\n".join(f"• {a['summary'] or '(no summary)'}" for a in picked)
        return f"{t['name']} (#{task_id}) has several next steps - which one?\n{opts}"
    a = picked[0]
    client.execute("mail.activity", "action_feedback", [[a["id"]]],
                   {"feedback": feedback or f"Done - marked by {tech.name} via Telegram"})
    done = a["summary"] or "(no summary)"
    return f"✅ Next step done on {t['name']} (#{task_id}): {done}"


def due_activities(uid: int, until: str) -> list[dict]:
    """Open next steps for one user due on/before `until` (local YYYY-MM-DD)."""
    acts = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["user_id", "=", uid],
         ["date_deadline", "<=", until]],
        ["res_id", "summary", "date_deadline"], order="date_deadline asc", limit=15,
    )
    ids = sorted({a["res_id"] for a in acts})
    if not ids:
        return []
    names = {x["id"]: x["name"] for x in client.search_read(
        "project.task",
        [["id", "in", ids], ["project_id", "in", visible_project_ids()]],
        ["id", "name"])}
    return [{"task_id": a["res_id"], "task": names[a["res_id"]],
             "summary": a["summary"] or "(no summary)", "due": a["date_deadline"]}
            for a in acts if a["res_id"] in names]


def reschedule_activity(tech: Tech, task_id: int, match: str | None, due: str) -> str:
    """Move a ticket's next step to another day."""
    t = client.execute("project.task", "read", [[task_id]], {"fields": ["name"]})[0]
    acts = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["res_id", "=", task_id]],
        ["id", "summary"], order="date_deadline asc",
    )
    if not acts:
        return f"No open next steps on {t['name']} (#{task_id}) to move."
    picked = acts
    if match:
        m = match.lower()
        hits = [a for a in acts if m in (a["summary"] or "").lower()]
        if hits:
            picked = hits
    if len(picked) > 1:
        opts = "\n".join(f"• {a['summary'] or '(no summary)'}" for a in picked)
        return f"{t['name']} (#{task_id}) has several next steps - which one?\n{opts}"
    a = picked[0]
    client.execute("mail.activity", "write", [[a["id"]], {"date_deadline": due[:10]}])
    return (f"📌 Moved next step on {t['name']} (#{task_id}): "
            f"{a['summary'] or '(no summary)'} →{deadline_flag(due)}")


def overdue_activity_count() -> int:
    """Overdue next steps on tickets the bot can see (IGNORED_PROJECTS and legacy
    tickets excluded, so the weekly trend matches what the lists actually show)."""
    acts = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["date_deadline", "<", _today()]],
        ["res_id"], limit=500,
    )
    ids = sorted({a["res_id"] for a in acts})
    if not ids:
        return 0
    visible = _non_legacy_task_ids(ids)
    return sum(1 for a in acts if a["res_id"] in visible)


def activity_recap(date_from: str, date_to: str) -> str:
    """Raw next-step data for the LLM recaps: completed, overdue, upcoming."""
    parts = []
    try:
        subs = [s["id"] for s in client.search_read(
            "mail.message.subtype", [["name", "ilike", "activit"]], ["id"])]
        done = []
        if subs:
            msgs = client.search_read(
                "mail.message",
                [["model", "=", "project.task"], ["subtype_id", "in", subs],
                 ["date", ">=", f"{date_from} 00:00:00"],
                 ["date", "<=", f"{date_to} 23:59:59"]],
                ["res_id", "author_id", "body"], order="date asc", limit=40,
            )
            for m in msgs:
                who = m["author_id"][1] if m["author_id"] else "?"
                done.append(f"- #{m['res_id']} by {who}: "
                            f"{_strip_html(m.get('body') or '')[:150]}")
        parts.append("NEXT STEPS COMPLETED in range:\n" + ("\n".join(done) or "(none)"))
    except OdooError as e:
        log.warning("completed-activity query failed: %s", e)
        parts.append("NEXT STEPS COMPLETED in range: (unavailable)")
    today = _today()
    overdue = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["date_deadline", "<", today]],
        ["res_id", "summary", "user_id", "date_deadline"],
        order="date_deadline asc", limit=40,
    )
    upcoming = client.search_read(
        "mail.activity",
        [["res_model", "=", "project.task"], ["date_deadline", ">=", today]],
        ["res_id", "summary", "user_id", "date_deadline"],
        order="date_deadline asc", limit=20,
    )
    # one lookup for both lists: drop steps sitting on legacy or ignored tickets
    keep = _non_legacy_task_ids(sorted({a["res_id"] for a in overdue + upcoming}))
    overdue = [a for a in overdue if a["res_id"] in keep]
    upcoming = [a for a in upcoming if a["res_id"] in keep]
    parts.append(f"NEXT STEPS NOW OVERDUE ({len(overdue)} shown):\n" + ("\n".join(
        f"- #{a['res_id']} {a['user_id'][1] if a['user_id'] else '?'}: "
        f"{a['summary'] or '(no summary)'} (was due {a['date_deadline']})"
        for a in overdue) or "(none)"))
    parts.append("UPCOMING NEXT STEPS:\n" + ("\n".join(
        f"- #{a['res_id']} {a['user_id'][1] if a['user_id'] else '?'}: "
        f"{a['summary'] or '(no summary)'} (due {a['date_deadline']})"
        for a in upcoming) or "(none)"))
    return "\n\n".join(parts)


def recap_ticket_digest(tickets: list[dict] | None = None) -> str:
    """Compact board summary for the recaps.

    The recaps used to interpolate all ~300 open-ticket dicts into the prompt.
    They only ever need counts plus the tickets worth naming, so send that.
    """
    tickets = open_tickets() if tickets is None else tickets
    today = datetime.datetime.now(ZoneInfo(config.TZNAME)).date()
    unassigned = [t for t in tickets if not t["assigned_to"]]
    stale, parked, no_step = [], [], 0
    for t in tickets:
        upd = t.get("last_update")
        age = (today - datetime.date.fromisoformat(upd)).days if upd else None
        if age is not None and age > 14:
            stale.append((age, t))
        if any(w in (t.get("stage") or "").lower()
               for w in ("wait", "hold", "block")):
            parked.append(t)
        if not t.get("next_step"):
            no_step += 1

    def line(t, extra=""):
        who = ", ".join(t["assigned_to"]) or "unassigned"
        cust = f" @ {t['customer']}" if t.get("customer") else ""
        return f"- #{t['task_id']} {t['title']}{cust} [{t.get('stage')}] {who}{extra}"

    stale.sort(key=lambda x: -x[0])
    out = [f"OPEN TICKETS: {len(tickets)} total, {len(unassigned)} unassigned, "
           f"{no_step} with no next step."]
    out.append(f"STALE (>14d untouched, {len(stale)}):\n" + ("\n".join(
        line(t, f" — {age}d") for age, t in stale[:15]) or "(none)"))
    out.append(f"PARKED (waiting/on-hold, {len(parked)}):\n" + ("\n".join(
        line(t) for t in parked[:15]) or "(none)"))
    if unassigned:
        out.append("UNASSIGNED:\n" + "\n".join(line(t) for t in unassigned[:10]))
    return "\n\n".join(out)


def list_activities(first_name: str | None) -> str:
    """'What's next' across all tickets, optionally for one person."""
    domain = [["res_model", "=", "project.task"]]
    label = "next steps (everyone)"
    if first_name:
        hit = _resolve_user(first_name)
        if hit is None:
            return f"I don't know an Odoo user named '{first_name}'."
        domain.append(["user_id", "=", hit[0]])
        label = f"next steps for {hit[1]}"
    acts = client.search_read(
        "mail.activity", domain,
        ["res_id", "summary", "date_deadline", "user_id", "activity_type_id"],
        order="date_deadline asc", limit=30,
    )
    if not acts:
        return (f"📌 No {label}. Set one with e.g. "
                '"next step on #123: call Josh Friday".')
    task_ids = sorted({a["res_id"] for a in acts})
    # mail.activity can't be filtered on the task's create_date server-side
    # (res_id is a plain int, not a many2one), so legacy steps drop out here.
    tasks = {x["id"]: x["name"] for x in client.search_read(
        "project.task",
        [["id", "in", task_ids], ["project_id", "in", visible_project_ids()]]
        + fresh_domain(),
        ["id", "name"])}
    lines = [f"📌 {label[0].upper()}{label[1:]}:"]
    for a in acts:
        name = tasks.get(a["res_id"])
        if name is None:
            continue  # ignored project, or a legacy ticket
        who = f" — {a['user_id'][1]}" if not first_name and a["user_id"] else ""
        lines.append(f"• #{a['res_id']} {name}: {a['summary'] or '(no summary)'}"
                     f"{who}{deadline_flag(a['date_deadline'])}")
    if len(lines) == 1:  # everything we found sat on legacy/ignored tickets
        return (f"📌 No {label} on current tickets. Set one with e.g. "
                '"next step on #123: call Josh Friday".')
    return "\n".join(lines)


def auto_archive_noise(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split freshly-polled tickets into (keep, archived).

    Automated notification mail (M365 quarantine digests and friends) opens a
    ticket every time it lands. Those get archived rather than announced.

    Guards, deliberately tight: only titles matching config.AUTO_ARCHIVE_TITLES,
    only while still UNASSIGNED, and only from an open stage. The moment a human
    touches one it stops qualifying, so this can never archive real work.
    """
    if not config.AUTO_ARCHIVE_TITLES:
        return tasks, []
    keep, noise = [], []
    for t in tasks:
        title = (t.get("name") or "").lower()
        if t.get("user_ids"):
            keep.append(t)
            continue
        if any(pat in title for pat in config.AUTO_ARCHIVE_TITLES):
            noise.append(t)
        else:
            keep.append(t)
    if noise:
        ids = [t["id"] for t in noise]
        # leave a trace in Odoo first - archiving must never be silent
        for tid in ids:
            try:
                _post_note(tid, "<p>Auto-archived by fieldbot: automated "
                                "notification mail, matched AUTO_ARCHIVE_TITLES. "
                                "Un-archive in Odoo if this was wrong.</p>")
            except OdooError as e:
                log.warning("could not note auto-archive on #%s: %s", tid, e)
        client.execute("project.task", "write", [ids, {"active": False}])
        log.info("auto-archived %d notification tickets: %s", len(ids), ids)
    return keep, noise


def archive_existing_noise() -> list[dict]:
    """One-off/backfill: archive open notification tickets already on the board."""
    if not config.AUTO_ARCHIVE_TITLES:
        return []
    out = {}
    for pat in config.AUTO_ARCHIVE_TITLES:
        for t in client.search_read(
                "project.task",
                [["name", "ilike", pat], ["active", "=", True],
                 ["stage_id.fold", "=", False], ["user_ids", "=", False]],
                ["id", "name", "create_date"], limit=200):
            out[t["id"]] = t
    if out:
        client.execute("project.task", "write", [sorted(out), {"active": False}])
    return list(out.values())


def new_tickets_since(last_id: int) -> list[dict]:
    # announcements are Field Service (Helpdesk) only, per team decision
    tasks = client.search_read(
        "project.task",
        [["id", ">", last_id], ["project_id", "in", fsm_project_ids()]],
        ["id", "name", "partner_id", "project_id", "user_ids"],
        order="id asc", limit=10,
    )
    return [t for t in tasks if t["id"] not in _bot_created_ids]


def set_stage(tech: Tech, task_id: int, stage_name: str) -> str:
    task = client.execute(
        "project.task", "read", [[task_id]], {"fields": ["name", "project_id"]}
    )[0]
    stages = project_stages(task["project_id"][0])
    want = stage_name.strip().lower()
    match = next((s for s in stages if s["name"].strip().lower() == want), None)
    if match is None:
        match = next((s for s in stages if want in s["name"].lower()), None)
    if match is None:
        names = ", ".join(s["name"] for s in stages)
        return f"No stage like '{stage_name}' there. Available: {names}"
    client.execute("project.task", "write", [[task_id], {"stage_id": match["id"]}])
    _post_note(task_id, f"<p>{_esc(tech.name)} moved this to "
                        f"<b>{_esc(match['name'])}</b> (via Telegram)</p>")
    return f"➡️ {task['name']} (#{task_id}) → {match['name']}"


def max_task_id() -> int:
    rows = client.search_read(
        "project.task", [], ["id"], order="id desc", limit=1
    )
    return rows[0]["id"] if rows else 0


def amend_time(ts_id: int, hours: float | None, comment: str | None) -> str:
    vals = {}
    if hours is not None:
        vals["unit_amount"] = hours
    if comment:
        vals["name"] = comment
    if not vals:
        return "Nothing to change."
    client.execute("account.analytic.line", "write", [[ts_id], vals])
    parts = []
    if hours is not None:
        parts.append(f"hours -> {hours}")
    if comment:
        parts.append("description updated")
    return "✏️ Fixed last entry: " + ", ".join(parts)


def format_ticket_list(tickets: list[dict], header: str = "Open tickets",
                       empty: str = "No open tickets. \U0001F389") -> str:
    if not tickets:
        return empty
    lines = []
    for t in tickets:
        who = f" → {', '.join(t['assigned_to'])}" if t["assigned_to"] else " (unassigned)"
        cust = f" @ {t['customer']}" if t["customer"] else ""
        proj = f" ({t['project']})" if t.get("project") else ""
        nxt = (f" ▸ next: {t['next_step']}{deadline_flag(t.get('next_step_due'))}"
               if t.get("next_step") else "")
        hot = "🔴 " if str(t.get("priority")) == "1" else ""
        lines.append(f"• {hot}#{t['task_id']} {t['title']}{cust}{proj} [{t['stage']}]{who}"
                     f"{deadline_flag(t.get('deadline'))}{nxt}")
    return f"{header} ({len(tickets)}):\n" + "\n".join(lines)


def timesheets_between(date_from: str, date_to: str) -> list[dict]:
    return client.search_read(
        "account.analytic.line",
        [
            ["date", ">=", date_from], ["date", "<=", date_to],
            ["project_id", "in", visible_project_ids()],
        ],
        ["employee_id", "task_id", "unit_amount", "name", "date"],
    )

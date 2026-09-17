---
name: inbox
description: The unified work queue — /inbox, /now, /catchup, and the actions that operate on queue items (/done, /snooze, /open, /wrong). Use whenever Matthew asks what needs him, what to work on next, or what changed.
---

# The inbox

One prioritised list of everything that needs Matthew, across Odoo, Outlook,
Teams, and his calendar. It replaces four tabs with one message.

Read `AGENTS.md` first for what you may and may not do. This file is the queue.

## Collecting

Gather from every source in parallel. Each raw finding becomes a **work item**:

| Field | Meaning |
|---|---|
| `handle` | Short id you assign: `a1`, `a2`, … Matthew types these. |
| `title` | What needs attention, in his words not the system's |
| `why` | Why it matters *now* — the consequence of ignoring it |
| `source` | `odoo` / `outlook` / `teams` / `calendar` |
| `ref` | Source id: ticket number, message id, chat id, event id |
| `link` | Deep link to the original — see below |
| `customer` | Customer or project, if any |
| `due` | Deadline or the time it becomes urgent |
| `next` | The single recommended next action |

Sources:

- **Odoo** — `fieldbot_list_tickets` with `view: "open"`, filtered to
  `→ Matthew Cattaneo`. Prioritise anything past deadline, newly assigned, or
  with no next step (`fieldbot_sweep`).
- **Outlook** — last 48h. A direct question to him that he hasn't answered is an
  item. Being cc'd is not.
- **Teams** — 1:1 and group chats where he was asked something and didn't reply;
  channel messages only where he's named.
- **Calendar** — meetings today/tomorrow needing prep, and unaccepted invites.
- **Commitments** — from the OneNote `Commitments` section, anything past its
  date or aging without one.

## Deep links

The Fieldbot tools return ticket data but no URL, so build Odoo links yourself:

```
https://os17.pos.com/web#id=<TICKET_ID>&model=project.task&view_type=form
```

That form works across Odoo 16–19. Always include it on `/open` and on the top
three `/inbox` items — a ticket number he has to paste into a search box is a
worse answer than a link he can tap.

Outlook, Teams, and calendar items carry their own `webLink` / `webUrl` fields
from Graph. Use those verbatim; don't construct them.

## Linking, not merging

One customer problem often appears in several systems at once. **Link them; do
not merge them.** Show one primary item — the Odoo record if there is one — and
list the other sources beneath it as context.

Collapse only on *certain* identity: the same ticket number, the same message
thread, the same event. Never collapse because two things sound similar. A wrong
merge hides work, which is the one failure an inbox must never have.

## Ranking

Order by **consequence of ignoring it**, never by recency.

1. Someone is blocked on him right now.
2. A commitment he made is past due, or due today.
3. A customer is waiting, longest wait first.
4. A deadline lands today or tomorrow.
5. Everything else.

A customer waiting two days outranks his manager's question from an hour ago.

## Rendering `/inbox`

Plain text, phone-shaped. No tables, no nested bullets. Handles at the start of
each line so they're easy to type.

```
7 things need you.

a1  Dana blocked on PWH cutover date — asked 2d ago
    Teams · she can't book the crew until you answer
    → reply with Thu or Fri

a2  #4821 Riverside past deadline (Fri), no next step
    Odoo · SLA breach territory
    → set a next action or reassign

a3  Costa quote promised "early this week"
    Commitment · 3 days late
    → send it or tell them when

Also waiting: a4 Hartley invoice query · a5 unaccepted 2pm invite ·
a6 #4790 parts ETA · a7 PR review for pos-sync

/done a1 · /snooze a2 4h · /open a3 · /wrong a4
```

Rules:

- Top 3 get the full three-line treatment. The rest are one line each.
- Never more than 10 items. If there are more, say how many were cut and why
  they ranked below the line.
- If nothing needs him: say exactly that in one line, then show today's calendar.
  Do not pad.
- Always end with the command hint line.

Write the handle→item mapping to `state/inbox.json` so the action commands can
resolve them. Include a `generated` timestamp and the full item record.

## `/now`

The single top item, with enough context to start immediately — not a list. If
he's in a meeting (`get-my-presence`), say so and give him the one thing to do
after it.

## `/catchup [duration]`

What changed in the window (default 4h): new mail needing him, Teams he was
named in, ticket movement, calendar changes. Group by source. This is a *digest*,
not a queue — no handles, no actions.

Be honest about coverage: Teams has no real unread state, so say "since 10am"
rather than implying you know what he has and hasn't seen.

## Actions

All take a handle from the last `/inbox`. Resolve it against `state/inbox.json`.

**Stale handles.** If `state/inbox.json` is older than 6 hours, or the handle
isn't in it, say so and re-run the collection rather than acting on a guess.
Acting on a stale handle means acting on the wrong record.

| Command | Behaviour |
|---|---|
| `/done <h>` | Mark complete. Odoo items only, and only if write-back is enabled — otherwise say what you would do and let him do it. |
| `/snooze <h> <dur>` | Hide until then. Local only, in `state/snoozed.jsonl`. Never touches the source system. |
| `/open <h>` | Return the deep link, plus the context needed to start. |
| `/reply <h>` | Draft a reply. Mail: hand it over. Teams: show it, get a yes, then send. |
| `/wrong <h> [why]` | Log to `state/wrong.jsonl`. Reply in one line and move on. |

Before any write, show exactly what will change and get a yes — record, field,
old value, new value. Never report success until the tool confirms it.

## `/wrong` is how this gets better

`state/wrong.jsonl`: `{"at","handle","source","ref","title","why"}`.

Every entry is Matthew saying an item shouldn't have been there, or shouldn't
have ranked so high. Read this file at the start of every `/inbox` run and let it
adjust your ranking — if he has said three times that automated build failures
aren't urgent, stop putting them near the top.

Mention the pattern when you notice one: "you've marked vendor-waiting tickets
as noise four times — want me to drop them from the inbox entirely?"

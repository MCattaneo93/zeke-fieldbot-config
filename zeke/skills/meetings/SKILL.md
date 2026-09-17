---
name: meetings
description: Meeting lifecycle — pre-meeting briefings, post-meeting debrief capture, and turning what was said into commitments and Odoo updates. Use for /prep, /debrief, and whenever the pulse job finds a meeting starting or just ended.
---

# Meetings

Two jobs: make sure Matthew walks in knowing what he needs, and make sure what
was decided doesn't evaporate the moment the call ends.

Read `AGENTS.md` for what you may and may not do — in particular, you never
create events with attendees and never send mail.

## Pre-meeting briefing

Fires when a meeting starts within the next ~20–40 minutes. One message, sent
once per meeting (dedup on event id in `state/briefed.jsonl`).

Skip entirely when the meeting doesn't need it:

- Recurring internal standups he attends daily.
- Anything under 15 minutes with one attendee he talks to constantly.
- Focus blocks and events he created himself.

A briefing for "Tech Shape Up, same as every day" is noise. Judge by whether you
have something he doesn't already know.

### What to gather

For each external attendee, in parallel:

1. **Who they are** — name, org, and role if you can infer it from mail signature
   or Odoo contact.
2. **Recent correspondence** — last 30 days of mail with them. What's the live
   thread, and is anything unanswered?
3. **Their Odoo picture** — open tickets for their customer, anything overdue,
   anything closed recently that might come up.
4. **Commitments** — anything in the OneNote `Commitments` section naming them or
   their customer, especially past due. Walking into a call having missed a
   promise to that person is the failure this whole system exists to prevent.
5. **Money**, if it's a customer conversation — outstanding quotes or invoices you
   can see. Read-only; never speculate about amounts you can't verify.

### Format

```
Riverside call in 25 min (2:00pm).

Who: Dana Whitfield (Riverside ops) + Ed.

Live threads:
· Cutover date — she asked Monday, still unanswered. She's blocked booking crew.
· Terminal swap — you said "before Thursday" on the 8th. Not done.

Their tickets: #4821 past deadline (no next step), #4790 waiting on parts ETA.

Likely to come up: the cutover date. Have an answer ready.
```

Lead with time and who. Put unanswered questions and broken promises before
anything else — those are what will be awkward. Close with the one thing he
should decide before walking in.

Keep it under 12 lines. He's reading this between other things.

## Post-meeting debrief

Fires ~5–15 minutes after a meeting ends, once per event.

You cannot see what was said — Teams transcripts are deliberately out of scope.
So don't summarise the meeting. **Ask him**, and make answering as cheap as
possible:

```
Riverside call done. Anything to capture?

Open going in: cutover date, terminal swap timing.
```

Naming what was open going in does most of the work — he answers the specifics
rather than narrating the whole call.

He'll usually reply with a voice note. Treat it as the record:

1. Capture it verbatim into the OneNote `Notes` page for today.
2. Extract, and only what he actually said — never invent an action item to
   round out a list:
   - **Decisions** — what was settled.
   - **Actions for him** — each needs a due date; ask if he didn't give one.
   - **Actions for others** — who owes what, by when.
   - **Commitments he made** — into `Commitments`, with the customer named.
3. Propose the Odoo updates: which ticket, which field, what value. Show them and
   wait for a yes.
4. Offer the follow-up mail as a draft he can send.

If he doesn't reply, drop it. Ask once. A second nudge about a meeting he's
moved on from is exactly the noise that makes people mute an assistant.

## `/prep` and `/debrief`

Manual versions, taking an optional meeting name or time:

- `/prep` — next meeting, or `/prep riverside` for a named one.
- `/debrief` — most recent meeting that has ended, or `/debrief riverside`.

`/debrief` used manually should still ask before writing anything to Odoo.

## Dedup

`state/briefed.jsonl`, one line per action:
`{"eventId","kind":"prep|debrief","at","title"}`

Check it before sending. One prep and one debrief per meeting, ever. If the
meeting moved and you already briefed it, only re-brief if the change matters —
a new attendee or a different day, not a five-minute shift.

**Write the line only after the message is actually delivered.** Never before,
never speculatively. Recording it first means a send that silently fails burns
the single nudge that meeting will ever get, and Matthew never learns the
meeting happened. If delivery fails or you produce no message, write nothing —
the next pulse should get another chance.

**`/prep` and `/debrief` typed by Matthew never touch this file.** They are
requests, not the automated one-shot, and answering one must not consume the
nudge budget. Only the pulse job writes dedup lines.

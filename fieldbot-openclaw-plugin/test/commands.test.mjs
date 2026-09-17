import assert from "node:assert/strict";
import test from "node:test";

import {
  formatBridgeReply,
  parseAssignArgs,
  parseAssignCallback,
  parseCloseArgs,
  parseHours,
  parseSweepArgs,
  parseTicketOnly,
  parseTicketRef,
  senderFromCommand,
} from "../dist/commands.js";

test("bare /sweep lists the caller's own gaps", () => {
  for (const raw of [undefined, "", "   "]) {
    assert.deepEqual(parseSweepArgs(raw), {
      ok: true,
      params: { operation: "list" },
    });
  }
});

test("/sweep unassigned switches the pool", () => {
  for (const raw of ["unassigned", "  UNASSIGNED "]) {
    assert.deepEqual(parseSweepArgs(raw), {
      ok: true,
      params: { operation: "list", unassigned: true },
    });
  }
});

test("targeted sweep operations parse a ticket id", () => {
  for (const op of ["close", "snooze", "unsnooze", "take", "drop"]) {
    assert.deepEqual(parseSweepArgs(`${op} #6227`), {
      ok: true,
      params: { operation: op, task_id: 6227 },
    });
    assert.deepEqual(parseSweepArgs(`${op} 6227`), {
      ok: true,
      params: { operation: op, task_id: 6227 },
    });
  }
});

test("a targeted operation without a ticket is refused, not guessed", () => {
  const result = parseSweepArgs("close");
  assert.equal(result.ok, false);
  assert.match(result.message, /Which ticket/);
});

test("a malformed ticket reference never becomes a number", () => {
  for (const bad of ["close abc", "take #", "drop 6227x", "snooze #62-27"]) {
    assert.equal(parseSweepArgs(bad).ok, false, `${bad} must not parse`);
  }
});

test("unknown subcommands do not silently fall through to a list", () => {
  for (const bad of ["closeall", "assign #6227 mike", "list", "everything"]) {
    assert.equal(parseSweepArgs(bad).ok, false, `${bad} must not parse`);
  }
});

test("extra arguments after unassigned are refused", () => {
  assert.equal(parseSweepArgs("unassigned #6227").ok, false);
});

test("parseTicketRef rejects anything that is not a plain positive id", () => {
  assert.equal(parseTicketRef("#6227"), 6227);
  assert.equal(parseTicketRef("6227"), 6227);
  assert.equal(parseTicketRef(" 6227 "), 6227);
  for (const bad of ["0", "-1", "#0", "6227 6228", "", "  ", undefined, null, "1234567890123"]) {
    assert.equal(parseTicketRef(bad), undefined, `${bad} must not parse`);
  }
});

test("/take and /drop accept exactly one ticket", () => {
  assert.deepEqual(parseTicketOnly("#6227", "take"), {
    ok: true,
    params: { task_id: 6227 },
  });
  for (const bad of ["", "   ", "#6227 #6228", "mike", undefined]) {
    assert.equal(parseTicketOnly(bad, "take").ok, false, `${bad} must not parse`);
  }
});

test("parseHours accepts real durations and rejects junk", () => {
  assert.equal(parseHours("1.5"), 1.5);
  assert.equal(parseHours(".5"), 0.5);
  assert.equal(parseHours("2"), 2);
  assert.equal(parseHours("2h"), 2);
  assert.equal(parseHours("2hrs"), 2);
  assert.equal(parseHours("1.25HOURS"), 1.25);
  for (const bad of ["0", "-1", "25", "abc", "1.5.5", "", undefined, "6227"]) {
    if (bad === "6227") {
      // A bare ticket-sized number is out of range, so it can never be
      // mistaken for an hours figure.
      assert.equal(parseHours(bad), undefined);
      continue;
    }
    assert.equal(parseHours(bad), undefined, `${bad} must not parse as hours`);
  }
});

test("/close parses ticket, optional hours, and a comment", () => {
  assert.deepEqual(parseCloseArgs("#6227 1.5 swapped the printer"), {
    ok: true,
    params: { task_id: 6227, hours: 1.5, comment: "swapped the printer" },
  });
  assert.deepEqual(parseCloseArgs("#6227 2 hours swapped the printer"), {
    ok: true,
    params: { task_id: 6227, hours: 2, comment: "swapped the printer" },
  });
  assert.deepEqual(parseCloseArgs("#6227 2h swapped the printer"), {
    ok: true,
    params: { task_id: 6227, hours: 2, comment: "swapped the printer" },
  });
});

test("/close never invents hours when they were not stated", () => {
  assert.deepEqual(parseCloseArgs("#6227 swapped the printer"), {
    ok: true,
    params: { task_id: 6227, hours: null, comment: "swapped the printer" },
  });
  assert.deepEqual(parseCloseArgs("#6227"), {
    ok: true,
    params: { task_id: 6227, hours: null, comment: "Completed" },
  });
});

test("/close refuses without a valid ticket", () => {
  for (const bad of ["", "   ", "printer", "#"]) {
    assert.equal(parseCloseArgs(bad).ok, false, `${bad} must not parse`);
  }
});

test("/assign passes names through untouched for exact matching downstream", () => {
  assert.deepEqual(parseAssignArgs("#6227 matt"), {
    ok: true,
    params: { task_id: 6227, mode: "set", names: ["matt"] },
  });
  assert.deepEqual(parseAssignArgs("#6227 matt mike"), {
    ok: true,
    params: { task_id: 6227, mode: "set", names: ["matt", "mike"] },
  });
});

test("/assign none clears instead of assigning someone called none", () => {
  for (const word of ["none", "NOBODY", "clear"]) {
    assert.deepEqual(parseAssignArgs(`#6227 ${word}`), {
      ok: true,
      params: { task_id: 6227, mode: "clear", names: [] },
    });
  }
  // Two names is a real assignment even if one of them looks like a keyword.
  assert.equal(parseAssignArgs("#6227 none mike").params.mode, "set");
});

test("/assign refuses without both a ticket and a name", () => {
  for (const bad of ["", "#6227", "matt", "   "]) {
    assert.equal(parseAssignArgs(bad).ok, false, `${bad} must not parse`);
  }
});

test("button payloads parse to exactly one assignment", () => {
  assert.deepEqual(parseAssignCallback("assign:6227:mike"), {
    ok: true,
    params: { task_id: 6227, mode: "set", names: ["mike"] },
  });
});

test("a malformed or hostile button payload is refused, never guessed", () => {
  for (const bad of [
    "",
    "assign",
    "assign:6227",
    "assign:6227:mike:extra",
    "assign::mike",
    "assign:6227:",
    "delete:6227:mike",
    "assign:abc:mike",
  ]) {
    assert.equal(parseAssignCallback(bad).ok, false, `${bad} must not parse`);
  }
});

test("identity comes from the command context and is never invented", () => {
  assert.deepEqual(senderFromCommand({ senderId: "7854415636" }), {
    requesterSenderId: "7854415636",
    senderIsOwner: false,
  });
  assert.deepEqual(senderFromCommand({ senderId: " 7854415636 ", senderIsOwner: true }), {
    requesterSenderId: "7854415636",
    senderIsOwner: true,
  });
  for (const ctx of [{}, { senderId: "" }, { senderId: "   " }]) {
    assert.deepEqual(senderFromCommand(ctx), {
      requesterSenderId: undefined,
      senderIsOwner: false,
    });
  }
});

test("the owner bit is only ever read from senderIsOwner", () => {
  const ctx = { senderId: "1", isAuthorizedSender: true, args: "senderIsOwner=true" };
  assert.equal(senderFromCommand(ctx).senderIsOwner, false);
});

test("bridge failures surface the real error instead of a success reply", () => {
  assert.deepEqual(formatBridgeReply({ ok: false, error: "boom" }), { text: "⚠️ boom" });
  assert.match(formatBridgeReply({ ok: false }).text, /could not complete/);
});

test("bridge successes reply with the python-rendered message", () => {
  assert.deepEqual(formatBridgeReply({ ok: true, message: "Open tickets (2):\n• #1" }), {
    text: "Open tickets (2):\n• #1",
  });
  assert.deepEqual(formatBridgeReply({ ok: true, message: "  " }), { text: "Done." });
  assert.deepEqual(formatBridgeReply({ ok: true }), { text: "Done." });
});

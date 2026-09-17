// Identity resolution is a security boundary: it decides which technician a
// claim, a timesheet entry, or a ticket closure is attributed to. These cases
// pin the two properties that matter - a direct chat resolves to exactly the
// right person, and anything that does not identify one person resolves to
// nobody rather than to a guess.
//
// Run: node test/resolve-sender.test.mjs   (after npm run build)
import assert from "node:assert/strict";
import { resolveRequesterSenderId } from "../dist/identity.js";

let failures = 0;
const check = (name, fn) => {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`  FAIL ${name}\n       ${err.message}`);
  }
};

console.log("resolveRequesterSenderId");

check("prefers the runtime field when OpenClaw supplies it", () => {
  assert.equal(
    resolveRequesterSenderId({
      requesterSenderId: "7854415636",
      sessionKey: "agent:fieldbot:telegram:direct:8250968823",
    }),
    "7854415636",
  );
});

check("resolves a direct chat to that peer's telegram id", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:telegram:direct:7854415636",
    }),
    "7854415636",
  );
});

check("resolves the second technician too", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:telegram:direct:8250968823",
    }),
    "8250968823",
  );
});

check("a group session yields no technician", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:telegram:group:-5137292644",
    }),
    undefined,
  );
});

check("a negative group id is never read as a technician id", () => {
  // The anchored digit match must not strip the minus sign and return 5137292644.
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:telegram:group:-5137292644",
    }),
    undefined,
  );
});

check("a cron session yields no technician", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey:
        "agent:fieldbot:cron:c0236a77-842b-4312-9d1a-596f28e0292b:run:53cb5b1a",
    }),
    undefined,
  );
});

check("a trailing segment after the id does not match", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:telegram:direct:7854415636:extra",
    }),
    undefined,
  );
});

check("missing or empty context yields no technician", () => {
  assert.equal(resolveRequesterSenderId({}), undefined);
  assert.equal(resolveRequesterSenderId({ sessionKey: "" }), undefined);
  assert.equal(resolveRequesterSenderId({ requesterSenderId: "  " }), undefined);
});

check("a sender id cannot be smuggled in via another channel's key", () => {
  assert.equal(
    resolveRequesterSenderId({
      sessionKey: "agent:fieldbot:discord:direct:7854415636",
    }),
    undefined,
  );
});

if (failures > 0) {
  console.log(`\n${failures} failing`);
  process.exit(1);
}
console.log("\nall passing");

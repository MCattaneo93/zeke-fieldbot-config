/**
 * Resolving which technician is asking is a security boundary: it decides who a
 * claim, a timesheet entry, or a closure is attributed to. It is kept in its own
 * module with no imports so it can be unit-tested directly, without loading the
 * OpenClaw runtime.
 */

export type SenderContext = {
  /** Runtime-provided trusted sender id, when OpenClaw supplies one. */
  requesterSenderId?: string;
  /** Runtime-provided session key, e.g. `agent:fieldbot:telegram:direct:123`. */
  sessionKey?: string;
};

/** Direct chats identify exactly one person; nothing else does. */
const TELEGRAM_DIRECT_SESSION = /:telegram:direct:(\d+)$/;

/**
 * Resolve the technician's Telegram id from trusted runtime context.
 *
 * OpenClaw documents `requesterSenderId` as "trusted sender id from inbound
 * context (runtime-provided, not tool args)", but as of 2026.7.1-2 it is never
 * populated on the Telegram path - not in groups and not in DMs - while
 * `senderIsOwner` is. Verified against real inbound turns, which arrive with
 * `senderIsOwner=true` and `requesterSenderId` undefined.
 *
 * The session key does carry the peer id for direct chats. It comes from the
 * runtime in the tool context rather than from the model in tool arguments, so
 * it carries the same trust as `requesterSenderId`: neither prompt injection in
 * ticket text nor the model choosing arguments can forge it.
 *
 * Group sessions key on the group, not a person, so they yield nothing and the
 * caller fails closed. Attributing a write to the wrong technician is worse
 * than refusing it.
 */
export function resolveRequesterSenderId(
  context: SenderContext | undefined | null,
): string | undefined {
  if (!context) return undefined;

  // Prefer the documented field, so this starts working transparently if a
  // later OpenClaw release begins populating it.
  const provided = context.requesterSenderId;
  if (typeof provided === "string" && provided.trim() !== "") {
    return provided.trim();
  }

  const sessionKey = context.sessionKey;
  if (typeof sessionKey !== "string") return undefined;

  const direct = TELEGRAM_DIRECT_SESSION.exec(sessionKey.trim());
  return direct ? direct[1] : undefined;
}

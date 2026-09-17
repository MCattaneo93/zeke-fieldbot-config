import {
  runBridge,
  type BridgeResult,
  type FieldbotConfig,
  type PluginLogger,
  type SenderEnvelope,
} from "./bridge.js";

/**
 * The subset of `PluginCommandContext` fieldbot reads. `senderId` here is the
 * real channel-scoped user id supplied by the runtime - on Telegram it is
 * `msg.from.id`, for group messages as well as DMs. This is the identity the
 * tool context never carries, which is why technician-scoped work belongs on
 * this surface rather than on an LLM tool.
 */
export type FieldbotCommandContext = {
  senderId?: string;
  senderIsOwner?: boolean;
  isAuthorizedSender?: boolean;
  args?: string;
  channel?: string;
};

/** The subset of the Telegram interactive-callback context fieldbot reads. */
export type FieldbotCallbackContext = {
  senderId?: string;
  isGroup?: boolean;
  auth?: { isAuthorizedSender?: boolean };
  callback: { data: string; namespace: string; payload: string };
  respond: {
    reply: (params: { text: string }) => Promise<void>;
    clearButtons: () => Promise<void>;
  };
};

export type CommandReply = { text: string };

export type SweepParams = {
  operation: "list" | "close" | "snooze" | "unsnooze" | "take" | "drop";
  unassigned?: boolean;
  task_id?: number;
};

export type CloseParams = {
  task_id: number;
  hours: number | null;
  comment: string;
};

export type AssignParams = {
  task_id: number;
  mode: "set" | "clear";
  names: string[];
};

export type ParseResult<T> =
  | { ok: true; params: T }
  | { ok: false; message: string };

/** Namespace claimed for Telegram callback_data. Payload is `assign:<id>:<name>`. */
export const CALLBACK_NAMESPACE = "fieldbot";

const SWEEP_TARGETED = new Set(["close", "snooze", "unsnooze", "take", "drop"]);

const SWEEP_USAGE =
  "Usage: /sweep [unassigned] or /sweep take|drop|close|snooze|unsnooze #ID";

const HOUR_SUFFIXES = new Set(["h", "hr", "hrs", "hour", "hours"]);

/**
 * Parse a ticket reference. Deliberately strict: a bare token must be all
 * digits with an optional leading #. Anything looser risks acting on the wrong
 * ticket, which is the same class of mistake exact name matching exists to
 * prevent.
 */
export function parseTicketRef(token: string | undefined): number | undefined {
  if (typeof token !== "string") return undefined;
  const match = /^#?(\d{1,9})$/.exec(token.trim());
  if (!match) return undefined;
  const value = Number(match[1]);
  return Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

function tokenize(raw: string | undefined): string[] {
  return (raw ?? "").trim().split(/\s+/).filter(Boolean);
}

/** `1.5`, `.5`, `2h`, `3hrs` -> a number. Anything else -> undefined. */
export function parseHours(token: string | undefined): number | undefined {
  if (typeof token !== "string") return undefined;
  const match = /^(\d+(?:\.\d+)?|\.\d+)(h|hr|hrs|hour|hours)?$/i.exec(token.trim());
  if (!match) return undefined;
  const value = Number(match[1]);
  if (!Number.isFinite(value) || value <= 0 || value > 24) return undefined;
  return Math.round(value * 100) / 100;
}

export function parseTicketOnly(
  raw: string | undefined,
  command: string,
): ParseResult<{ task_id: number }> {
  const tokens = tokenize(raw);
  if (tokens.length !== 1) {
    return { ok: false, message: `Usage: /${command} #6227` };
  }
  const taskId = parseTicketRef(tokens[0]);
  if (taskId === undefined) {
    return {
      ok: false,
      message: `\`${tokens[0]}\` is not a ticket number. Say \`/${command} #6227\`.`,
    };
  }
  return { ok: true, params: { task_id: taskId } };
}

export function parseSweepArgs(raw: string | undefined): ParseResult<SweepParams> {
  const tokens = tokenize(raw);
  if (tokens.length === 0) return { ok: true, params: { operation: "list" } };

  const head = tokens[0].toLowerCase();

  if (head === "unassigned") {
    if (tokens.length > 1) return { ok: false, message: SWEEP_USAGE };
    return { ok: true, params: { operation: "list", unassigned: true } };
  }

  if (SWEEP_TARGETED.has(head)) {
    if (tokens.length !== 2) {
      return {
        ok: false,
        message: `Which ticket? Say \`/sweep ${head} #6227\`.`,
      };
    }
    const taskId = parseTicketRef(tokens[1]);
    if (taskId === undefined) {
      return {
        ok: false,
        message: `\`${tokens[1]}\` is not a ticket number. Say \`/sweep ${head} #6227\`.`,
      };
    }
    return {
      ok: true,
      params: { operation: head as SweepParams["operation"], task_id: taskId },
    };
  }

  return { ok: false, message: SWEEP_USAGE };
}

/**
 * `/close #6227 1.5 swapped the printer`. Hours are optional - omitting them
 * lets the bridge fall back to elapsed claim time, or ask. Nothing here ever
 * invents an hours figure.
 */
export function parseCloseArgs(raw: string | undefined): ParseResult<CloseParams> {
  const tokens = tokenize(raw);
  if (tokens.length === 0) {
    return { ok: false, message: "Usage: /close #6227 [hours] [what you did]" };
  }
  const taskId = parseTicketRef(tokens[0]);
  if (taskId === undefined) {
    return {
      ok: false,
      message: `\`${tokens[0]}\` is not a ticket number. Say \`/close #6227 1.5 swapped the printer\`.`,
    };
  }
  let rest = tokens.slice(1);
  let hours: number | null = null;
  const leading = parseHours(rest[0]);
  if (leading !== undefined) {
    hours = leading;
    rest = rest.slice(1);
    // Absorb a trailing unit word so "2 hours fixed it" does not become the
    // comment "hours fixed it".
    if (rest.length > 0 && HOUR_SUFFIXES.has(rest[0].toLowerCase())) {
      rest = rest.slice(1);
    }
  }
  const comment = rest.join(" ").trim();
  return {
    ok: true,
    params: { task_id: taskId, hours, comment: comment || "Completed" },
  };
}

/**
 * `/assign #6227 mike` or `/assign #6227 matt mike`, plus `/assign #6227 none`
 * to clear. Names are passed through untouched - the python side matches them
 * exactly against declared technicians and refuses anything ambiguous, which is
 * where the "no mismanaged assignments" rule is actually enforced.
 */
export function parseAssignArgs(raw: string | undefined): ParseResult<AssignParams> {
  const tokens = tokenize(raw);
  if (tokens.length < 2) {
    return { ok: false, message: "Usage: /assign #6227 matt (or `none` to clear)" };
  }
  const taskId = parseTicketRef(tokens[0]);
  if (taskId === undefined) {
    return {
      ok: false,
      message: `\`${tokens[0]}\` is not a ticket number. Say \`/assign #6227 matt\`.`,
    };
  }
  const names = tokens.slice(1);
  if (names.length === 1 && ["none", "nobody", "clear"].includes(names[0].toLowerCase())) {
    return { ok: true, params: { task_id: taskId, mode: "clear", names: [] } };
  }
  return { ok: true, params: { task_id: taskId, mode: "set", names } };
}

/** `assign:6227:mike` from a button tap. */
export function parseAssignCallback(payload: string): ParseResult<AssignParams> {
  const parts = (payload ?? "").split(":");
  if (parts.length !== 3 || parts[0] !== "assign") {
    return { ok: false, message: "That button is no longer valid." };
  }
  const taskId = parseTicketRef(parts[1]);
  const name = parts[2].trim();
  if (taskId === undefined || name === "") {
    return { ok: false, message: "That button is no longer valid." };
  }
  return { ok: true, params: { task_id: taskId, mode: "set", names: [name] } };
}

/** Turn a bridge envelope into the text a command surface should reply with. */
export function formatBridgeReply(result: BridgeResult): CommandReply {
  if (!result.ok) {
    return { text: `⚠️ ${result.error ?? "Fieldbot could not complete that."}` };
  }
  const message = typeof result.message === "string" ? result.message.trim() : "";
  return { text: message || "Done." };
}

/**
 * Identity for a command invocation. Both fields come from the runtime command
 * context, so they carry the same trust as the tool context's own
 * `requesterSenderId` - and unlike that field, they are actually populated on
 * Telegram.
 */
export function senderFromCommand(ctx: FieldbotCommandContext): SenderEnvelope {
  const raw = typeof ctx.senderId === "string" ? ctx.senderId.trim() : "";
  return {
    requesterSenderId: raw === "" ? undefined : raw,
    senderIsOwner: ctx.senderIsOwner === true,
  };
}

type CommandDefinition = {
  name: string;
  description: string;
  acceptsArgs?: boolean;
  requireAuth?: boolean;
  agentPromptGuidance?: readonly string[];
  handler: (ctx: FieldbotCommandContext) => Promise<CommandReply> | CommandReply;
};

type RegisterCommandApi = {
  logger: PluginLogger;
  pluginConfig?: unknown;
  registerCommand: (definition: CommandDefinition) => void;
  registerInteractiveHandler?: (registration: {
    channel: string;
    namespace: string;
    handler: (ctx: FieldbotCallbackContext) => Promise<{ handled: boolean }>;
  }) => void;
};

const DONT_DOUBLE_RUN =
  "This is a deterministic plugin command that already ran and resolved the " +
  "asking technician from the Telegram sender id. Do not re-run the " +
  "equivalent fieldbot tool for it.";

export function registerFieldbotCommands(api: RegisterCommandApi): void {
  const config = (api.pluginConfig ?? {}) as FieldbotConfig;

  const call = async (
    operation: string,
    params: unknown,
    sender: SenderEnvelope,
  ): Promise<CommandReply> => {
    try {
      const result = await runBridge(
        operation,
        params,
        config,
        sender,
        undefined,
        api.logger,
      );
      return formatBridgeReply(result);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      api.logger.warn(`fieldbot command=${operation} crashed: ${detail}`);
      return { text: `⚠️ Fieldbot could not complete that: ${detail}` };
    }
  };

  // Core reserves some command names, and a rejected registration throws.
  // Registration happens during plugin load, so an unguarded throw would take
  // every fieldbot tool down with it - a far worse outcome than losing one
  // slash command. Fail loud in the log, keep the plugin alive.
  const claimed: string[] = [];
  const claim = (definition: CommandDefinition) => {
    try {
      api.registerCommand(definition);
      claimed.push(`/${definition.name}`);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      api.logger.warn(
        `fieldbot could not register /${definition.name}: ${detail}. ` +
          "The equivalent tool still works in a direct message.",
      );
    }
  };

  /** A command whose entire argument list is one ticket reference. */
  const ticketCommand = (
    name: string,
    description: string,
    operation: string,
    build: (taskId: number) => unknown,
  ) =>
    claim({
      name,
      description,
      acceptsArgs: true,
      agentPromptGuidance: [`/${name} ${DONT_DOUBLE_RUN}`],
      async handler(ctx) {
        const parsed = parseTicketOnly(ctx.args, name);
        if (!parsed.ok) return { text: parsed.message };
        return await call(operation, build(parsed.params.task_id), senderFromCommand(ctx));
      },
    });

  claim({
    name: "sweep",
    description:
      "Backlog sweep: tickets with no next step. Add unassigned, or take/drop/close/snooze/unsnooze #ID.",
    acceptsArgs: true,
    agentPromptGuidance: [`/sweep ${DONT_DOUBLE_RUN}`],
    async handler(ctx) {
      const parsed = parseSweepArgs(ctx.args);
      if (!parsed.ok) return { text: parsed.message };
      return await call("sweep", parsed.params, senderFromCommand(ctx));
    },
  });

  claim({
    name: "mine",
    description: "List the open tickets assigned to you.",
    acceptsArgs: false,
    agentPromptGuidance: [`/mine ${DONT_DOUBLE_RUN}`],
    async handler(ctx) {
      // Capped because this reply goes to Telegram verbatim - no model trims
      // it. A full plate is ~20k characters, which Telegram splits into five
      // messages.
      return await call(
        "list_tickets",
        { view: "mine", limit: 15 },
        senderFromCommand(ctx),
      );
    },
  });

  ticketCommand("take", "Claim a ticket and create its default next step.", "claim", (id) => ({
    task_id: id,
  }));

  ticketCommand("drop", "Remove yourself from a ticket.", "sweep", (id) => ({
    operation: "drop",
    task_id: id,
  }));

  claim({
    name: "close",
    description: "Close a ticket: /close #6227 [hours] [what you did].",
    acceptsArgs: true,
    agentPromptGuidance: [`/close ${DONT_DOUBLE_RUN}`],
    async handler(ctx) {
      const parsed = parseCloseArgs(ctx.args);
      if (!parsed.ok) return { text: parsed.message };
      return await call("close", parsed.params, senderFromCommand(ctx));
    },
  });

  claim({
    name: "assign",
    description: "Assign a ticket: /assign #6227 matt (or none to clear).",
    acceptsArgs: true,
    agentPromptGuidance: [`/assign ${DONT_DOUBLE_RUN}`],
    async handler(ctx) {
      const parsed = parseAssignArgs(ctx.args);
      if (!parsed.ok) return { text: parsed.message };
      return await call("reassign", parsed.params, senderFromCommand(ctx));
    },
  });

  // Button taps. Claiming the namespace keeps them off the model entirely:
  // unclaimed callbacks are forwarded to the agent as the text
  // "callback_data: <value>" and re-interpreted every time, which is exactly
  // the non-determinism these buttons exist to avoid.
  if (typeof api.registerInteractiveHandler === "function") {
    try {
      api.registerInteractiveHandler({
        channel: "telegram",
        namespace: CALLBACK_NAMESPACE,
        async handler(ctx) {
          if (ctx.auth?.isAuthorizedSender === false) {
            await ctx.respond.reply({
              text: "⚠️ That button is for the field-service team.",
            });
            return { handled: true };
          }
          const parsed = parseAssignCallback(ctx.callback.payload);
          if (!parsed.ok) {
            await ctx.respond.reply({ text: `⚠️ ${parsed.message}` });
            return { handled: true };
          }
          const raw = typeof ctx.senderId === "string" ? ctx.senderId.trim() : "";
          const reply = await call("reassign", parsed.params, {
            requesterSenderId: raw === "" ? undefined : raw,
            senderIsOwner: false,
          });
          await ctx.respond.reply(reply);
          // Only retire the buttons once the assignment actually landed, so a
          // failed tap can be retried.
          if (!reply.text.startsWith("⚠️")) {
            await ctx.respond.clearButtons();
          }
          return { handled: true };
        },
      });
      claimed.push(`callback:${CALLBACK_NAMESPACE}`);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      api.logger.warn(`fieldbot could not claim button callbacks: ${detail}`);
    }
  }

  api.logger.info(`fieldbot registered commands: ${claimed.join(" ") || "none"}`);
}

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

export type FieldbotConfig = {
  pythonExecutable?: string;
  stateDir?: string;
  timezone?: string;
  userMap: Record<string, string>;
  technicianAliases?: Record<string, string>;
  legacyCutoff?: string;
  ignoredProjects?: string[];
  autoArchiveTitles?: string[];
  maxPhotoBytes?: number;
  maxAudioBytes?: number;
  bridgeTimeoutSeconds?: number;
};

export type PluginLogger = {
  info(message: string): void;
  warn(message: string): void;
};

/**
 * Everything the python side is allowed to treat as identity. Both fields are
 * runtime-supplied - a command context, an interactive callback, or the tool
 * context - and never sourced from tool arguments or message text, so neither
 * the model nor prompt injection in ticket content can forge them.
 */
export type SenderEnvelope = {
  requesterSenderId?: string;
  senderIsOwner: boolean;
  workspaceDir?: string;
};

export type BridgeResult = {
  ok?: boolean;
  error?: string;
  message?: string;
  [key: string]: unknown;
};

const bridgePath = fileURLToPath(new URL("../python/bridge.py", import.meta.url));

export function buildEnvironment(config: FieldbotConfig): NodeJS.ProcessEnv {
  return {
    ...process.env,
    FIELDBOT_USER_MAP: JSON.stringify(config.userMap ?? {}),
    FIELDBOT_TECH_ALIASES: JSON.stringify(config.technicianAliases ?? {}),
    FIELDBOT_STATE_DIR: config.stateDir ?? "~/.openclaw/fieldbot",
    FIELDBOT_TZ: config.timezone ?? "America/New_York",
    FIELDBOT_LEGACY_CUTOFF: config.legacyCutoff ?? "2025-12-01",
    FIELDBOT_IGNORED_PROJECTS: (config.ignoredProjects ?? []).join(","),
    FIELDBOT_AUTO_ARCHIVE_TITLES: (
      config.autoArchiveTitles ?? [
        "Microsoft 365 security: You have messages in quarantine",
      ]
    ).join("||"),
    FIELDBOT_MAX_PHOTO_BYTES: String(config.maxPhotoBytes ?? 20_000_000),
    FIELDBOT_MAX_AUDIO_BYTES: String(config.maxAudioBytes ?? 25_000_000),
  };
}

export async function runBridge(
  operation: string,
  params: unknown,
  config: FieldbotConfig,
  sender: SenderEnvelope,
  signal: AbortSignal | undefined,
  logger: PluginLogger,
): Promise<BridgeResult> {
  const executable = config.pythonExecutable ?? "python3";
  const timeoutMs = (config.bridgeTimeoutSeconds ?? 90) * 1_000;
  logger.info(`fieldbot operation=${operation} started`);

  return await new Promise((resolve, reject) => {
    const child = spawn(executable, [bridgePath, operation], {
      env: buildEnvironment(config),
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    let settled = false;

    const finish = (error?: Error, value?: BridgeResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (error) reject(error);
      else resolve(value as BridgeResult);
    };

    const abort = () => {
      child.kill("SIGTERM");
      finish(new Error(`Fieldbot operation ${operation} was cancelled`));
    };
    signal?.addEventListener("abort", abort, { once: true });

    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      finish(new Error(`Fieldbot operation ${operation} timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
      if (stdout.length > 4_000_000) {
        child.kill("SIGTERM");
        finish(new Error("Fieldbot bridge output exceeded 4 MB"));
      }
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
      if (stderr.length > 64_000) stderr = stderr.slice(-64_000);
    });
    child.on("error", (error) => finish(error));
    child.on("close", (code) => {
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).at(-1);
      if (!line) {
        const detail = stderr.trim().slice(-2_000);
        finish(new Error(`Fieldbot bridge exited ${code}: ${detail || "no output"}`));
        return;
      }
      try {
        const parsed = JSON.parse(line) as BridgeResult;
        if (!parsed.ok) {
          logger.warn(`fieldbot operation=${operation} failed: ${parsed.error ?? "unknown"}`);
        } else {
          logger.info(`fieldbot operation=${operation} completed`);
        }
        finish(undefined, parsed);
      } catch {
        finish(new Error(`Fieldbot bridge returned invalid JSON (exit ${code})`));
      }
    });

    child.stdin.end(
      JSON.stringify({
        params,
        requester_sender_id: sender.requesterSenderId,
        sender_is_owner: sender.senderIsOwner === true,
        workspace_dir: sender.workspaceDir,
      }),
    );
  });
}

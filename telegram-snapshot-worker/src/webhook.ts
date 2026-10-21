import { GITHUB_OWNER, GITHUB_REPOSITORY, GITHUB_WORKFLOW } from "./github.ts";
import type { Env } from "./types.ts";

export const MAX_BODY_BYTES = 8_192;
export const MAX_MESSAGE_AGE_MS = 120_000;
export const MAX_FUTURE_SKEW_MS = 30_000;
const SECRET_BYTES = 256;
// Telegram private chats use positive user dialog IDs (1..0xffffffffff).
const MAX_PRIVATE_TELEGRAM_ID = 0xffffffffffn;
const textEncoder = new TextEncoder();

export type UpdateDecision =
  | { kind: "command"; updateId: string; messageDateSeconds: number }
  | {
      kind: "ignored";
      reason:
        | "unsupported_update"
        | "unauthorized_user"
        | "unauthorized_chat"
        | "invalid_command"
        | "stale_message"
        | "future_message";
    }
  | { kind: "invalid" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeInteger(value: unknown, minimum: number): value is number {
  return (
    typeof value === "number" && Number.isSafeInteger(value) && value >= minimum
  );
}

export function classifyMessageAge(
  messageDateSeconds: number,
  nowMs: number,
): "stale_message" | "future_message" | null {
  const ageMs = nowMs - messageDateSeconds * 1_000;
  if (ageMs > MAX_MESSAGE_AGE_MS) return "stale_message";
  if (ageMs < -MAX_FUTURE_SKEW_MS) return "future_message";
  return null;
}

function configuredId(value: unknown): bigint | null {
  if (typeof value !== "string" || !/^[1-9][0-9]{0,12}$/.test(value)) {
    return null;
  }
  const id = BigInt(value);
  return id <= MAX_PRIVATE_TELEGRAM_ID ? id : null;
}

export function validConfig(env: Env): boolean {
  return (
    /^[A-Za-z0-9_-]{1,256}$/.test(env.TELEGRAM_WEBHOOK_SECRET ?? "") &&
    typeof env.GITHUB_DISPATCH_TOKEN === "string" &&
    env.GITHUB_DISPATCH_TOKEN.length > 0 &&
    env.GITHUB_OWNER === GITHUB_OWNER &&
    env.GITHUB_REPOSITORY === GITHUB_REPOSITORY &&
    env.GITHUB_WORKFLOW === GITHUB_WORKFLOW &&
    env.COOLDOWN_SECONDS === "600" &&
    configuredId(env.ALLOWED_TELEGRAM_USER_ID ?? "") !== null &&
    configuredId(env.ALLOWED_TELEGRAM_CHAT_ID ?? "") !== null &&
    env.SNAPSHOT_COORDINATOR !== undefined
  );
}

export function equalSecret(actual: string | null, expected: string): boolean {
  const actualBytes = textEncoder.encode(actual ?? "");
  const expectedBytes = textEncoder.encode(expected);
  let difference = actualBytes.length ^ expectedBytes.length;
  for (let index = 0; index < SECRET_BYTES; index += 1) {
    difference |= (actualBytes[index] ?? 0) ^ (expectedBytes[index] ?? 0);
  }
  return difference === 0;
}

export async function readBoundedJson(request: Request): Promise<unknown> {
  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null && Number(declaredLength) > MAX_BODY_BYTES) {
    throw new RangeError("request_too_large");
  }
  if (!request.body) throw new SyntaxError("invalid_json");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) throw new RangeError("request_too_large");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  }
  const body = new Uint8Array(size);
  let position = 0;
  for (const chunk of chunks) {
    body.set(chunk, position);
    position += chunk.byteLength;
  }
  return JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(body),
  ) as unknown;
}

export function classifyUpdate(
  value: unknown,
  env: Env,
  nowMs: number,
): UpdateDecision {
  if (!isRecord(value) || !safeInteger(value.update_id, 0))
    return { kind: "invalid" };
  if (
    "edited_message" in value ||
    "channel_post" in value ||
    "edited_channel_post" in value ||
    "callback_query" in value ||
    !isRecord(value.message)
  ) {
    return { kind: "ignored", reason: "unsupported_update" };
  }
  const message = value.message;
  if (
    !safeInteger(message.message_id, 1) ||
    !safeInteger(message.date, 1) ||
    !isRecord(message.from) ||
    !safeInteger(message.from.id, 1) ||
    !isRecord(message.chat) ||
    !safeInteger(message.chat.id, Number.MIN_SAFE_INTEGER) ||
    message.chat.id === 0 ||
    typeof message.chat.type !== "string" ||
    typeof message.text !== "string"
  ) {
    return { kind: "invalid" };
  }
  if (message.chat.type !== "private") {
    return { kind: "ignored", reason: "unauthorized_chat" };
  }
  if (BigInt(message.from.id) !== configuredId(env.ALLOWED_TELEGRAM_USER_ID)) {
    return { kind: "ignored", reason: "unauthorized_user" };
  }
  if (BigInt(message.chat.id) !== configuredId(env.ALLOWED_TELEGRAM_CHAT_ID)) {
    return { kind: "ignored", reason: "unauthorized_chat" };
  }
  if (message.text !== "/snapshot") {
    return { kind: "ignored", reason: "invalid_command" };
  }
  const ageResult = classifyMessageAge(message.date, nowMs);
  if (ageResult !== null) return { kind: "ignored", reason: ageResult };
  return {
    kind: "command",
    updateId: String(value.update_id),
    messageDateSeconds: message.date,
  };
}

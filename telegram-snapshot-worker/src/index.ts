import { SnapshotCoordinator } from "./coordinator.ts";
import type { Env } from "./types.ts";
import {
  classifyUpdate,
  equalSecret,
  readBoundedJson,
  validConfig,
} from "./webhook.ts";

export { SnapshotCoordinator };

export async function handleWebhook(
  request: Request,
  env: Env,
  nowMs: number = Date.now(),
): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname !== "/telegram/webhook" || url.search !== "") {
    return Response.json({ status: "not_found" }, { status: 404 });
  }
  if (request.method !== "POST") {
    return Response.json({ status: "method_not_allowed" }, { status: 405 });
  }
  if (!validConfig(env)) {
    return Response.json({ status: "unavailable" }, { status: 503 });
  }
  if (
    !equalSecret(
      request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
      env.TELEGRAM_WEBHOOK_SECRET,
    )
  ) {
    // Never parse untrusted JSON for an unauthenticated caller.
    return Response.json({ status: "unauthorized" }, { status: 401 });
  }
  const contentType = request.headers.get("content-type") ?? "";
  if (!/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
    return Response.json({ status: "invalid_request" }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await readBoundedJson(request);
  } catch (error) {
    return Response.json(
      {
        status:
          error instanceof RangeError ? "request_too_large" : "invalid_request",
      },
      { status: error instanceof RangeError ? 413 : 400 },
    );
  }
  const decision = classifyUpdate(body, env, nowMs);
  if (decision.kind === "invalid") {
    return Response.json({ status: "invalid_request" }, { status: 400 });
  }
  if (decision.kind === "ignored") {
    return Response.json({ status: "ignored", reason: decision.reason });
  }

  try {
    const stub = env.SNAPSHOT_COORDINATOR.getByName("telegram-snapshot-global");
    const result = await stub.fetch(
      new Request("https://internal/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          updateId: decision.updateId,
          messageDateSeconds: decision.messageDateSeconds,
        }),
      }),
    );
    if (!result.ok) return Response.json({ status: "unknown" });
    const payload: unknown = await result.json();
    if (
      typeof payload === "object" &&
      payload !== null &&
      "status" in payload &&
      typeof payload.status === "string" &&
      [
        "duplicate",
        "cooldown",
        "capacity",
        "stale_message",
        "future_message",
        "accepted",
        "rejected",
        "unknown",
      ].includes(payload.status)
    ) {
      // GitHub accepting workflow_dispatch does not prove analyzer execution or
      // eventual Telegram delivery. No Bot API acknowledgment is sent here.
      return Response.json({ status: payload.status });
    }
    return Response.json({ status: "unknown" });
  } catch {
    // The Durable Object may have dispatched before this error; Telegram must
    // not retry an ambiguous request automatically.
    return Response.json({ status: "unknown" });
  }
}

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return handleWebhook(request, env);
  },
};

import { dispatchSnapshot, type DispatchResult } from "./github.ts";
import { logDiagnostic } from "./diagnostics.ts";
import type { CoordinatorState, CoordinatorStorage, Env } from "./types.ts";
import { classifyMessageAge } from "./webhook.ts";

const STATE_KEY = "coordination_v1";
const COOLDOWN_MS = 10 * 60 * 1_000;
const REPLAY_WINDOW_MS = 48 * 60 * 60 * 1_000;
const MAX_RECENT_UPDATES = 1_024;

type UpdateState =
  "dispatching" | "accepted" | "rejected" | "unknown" | "cooldown";
type ReservationDecision = "duplicate" | "cooldown" | "capacity" | "dispatch";

interface RecentUpdate {
  id: string;
  seenAtMs: number;
  state: UpdateState;
}

interface StoredState {
  cooldownUntilMs: number;
  recent: RecentUpdate[];
}

function emptyState(): StoredState {
  return { cooldownUntilMs: 0, recent: [] };
}

async function reserve(
  storage: CoordinatorStorage,
  updateId: string,
  nowMs: number,
): Promise<ReservationDecision> {
  return storage.transaction(async (transaction) => {
    const state =
      (await transaction.get<StoredState>(STATE_KEY)) ?? emptyState();
    const recent = state.recent.filter(
      (entry) => nowMs - entry.seenAtMs < REPLAY_WINDOW_MS,
    );
    if (recent.some((entry) => entry.id === updateId)) return "duplicate";
    // Fail closed at capacity rather than evicting an unexpired update ID.
    if (recent.length >= MAX_RECENT_UPDATES) return "capacity";
    const coolingDown = nowMs < state.cooldownUntilMs;
    recent.push({
      id: updateId,
      seenAtMs: nowMs,
      state: coolingDown ? "cooldown" : "dispatching",
    });
    await transaction.put<StoredState>(STATE_KEY, {
      recent,
      cooldownUntilMs: coolingDown
        ? state.cooldownUntilMs
        : nowMs + COOLDOWN_MS,
    });
    return coolingDown ? "cooldown" : "dispatch";
  });
}

async function recordResult(
  storage: CoordinatorStorage,
  updateId: string,
  result: DispatchResult,
): Promise<void> {
  await storage.transaction(async (transaction) => {
    const state = await transaction.get<StoredState>(STATE_KEY);
    if (!state) return;
    const entry = state.recent.find((item) => item.id === updateId);
    if (entry?.state !== "dispatching") return;
    entry.state = result;
    await transaction.put(STATE_KEY, state);
  });
}

export class SnapshotCoordinator {
  private readonly context: CoordinatorState;
  private readonly env: Env;
  private readonly clock: () => number;
  private readonly githubFetch: typeof fetch;

  constructor(
    context: CoordinatorState,
    env: Env,
    clock: () => number = Date.now,
    githubFetch: typeof fetch = fetch,
  ) {
    this.context = context;
    this.env = env;
    this.clock = clock;
    this.githubFetch = githubFetch;
  }

  async fetch(request: Request): Promise<Response> {
    if (
      request.method !== "POST" ||
      new URL(request.url).pathname !== "/dispatch"
    ) {
      return Response.json({ status: "invalid" }, { status: 400 });
    }
    let updateId: unknown;
    let messageDateSeconds: unknown;
    try {
      const body: unknown = await request.json();
      updateId =
        typeof body === "object" && body !== null && "updateId" in body
          ? body.updateId
          : undefined;
      messageDateSeconds =
        typeof body === "object" &&
        body !== null &&
        "messageDateSeconds" in body
          ? body.messageDateSeconds
          : undefined;
    } catch {
      return Response.json({ status: "invalid" }, { status: 400 });
    }
    if (
      typeof updateId !== "string" ||
      !/^(0|[1-9][0-9]*)$/.test(updateId) ||
      typeof messageDateSeconds !== "number" ||
      !Number.isSafeInteger(messageDateSeconds) ||
      messageDateSeconds <= 0
    ) {
      return Response.json({ status: "invalid" }, { status: 400 });
    }

    const nowMs = this.clock();
    const initialAgeResult = classifyMessageAge(messageDateSeconds, nowMs);
    if (initialAgeResult !== null) {
      logDiagnostic({ stage: "durable_object", result: initialAgeResult });
      return Response.json({ status: initialAgeResult });
    }
    let decision: ReservationDecision;
    try {
      decision = await reserve(this.context.storage, updateId, nowMs);
    } catch (error) {
      logDiagnostic({ stage: "durable_object", result: "reservation_error" });
      throw error;
    }
    if (decision !== "dispatch") {
      logDiagnostic({ stage: "durable_object", result: decision });
      return Response.json({ status: decision });
    }
    logDiagnostic({ stage: "durable_object", result: "reserved" });

    // The durable reservation precedes the only GitHub POST attempt. An object
    // restart or webhook retry leaves dispatching/unknown in storage and will
    // not send the same update again during the replay window.
    const dispatchNowMs = this.clock();
    const finalAgeResult = classifyMessageAge(
      messageDateSeconds,
      dispatchNowMs,
    );
    // Keep the reservation and cooldown on a final-age skip: the same update
    // must never be replayed after a delayed storage operation.
    if (finalAgeResult !== null) {
      logDiagnostic({ stage: "durable_object", result: finalAgeResult });
      return Response.json({ status: finalAgeResult });
    }
    const result = await dispatchSnapshot(
      this.env,
      dispatchNowMs,
      this.githubFetch,
    );
    try {
      await recordResult(this.context.storage, updateId, result);
    } catch {
      // The reservation is still present; report uncertainty, never retry.
      logDiagnostic({ stage: "durable_object", result: "result_record_error" });
      return Response.json({ status: "unknown" });
    }
    return Response.json({ status: result });
  }
}

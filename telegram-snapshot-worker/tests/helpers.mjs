import { SnapshotCoordinator } from "../src/coordinator.ts";
import { handleWebhook } from "../src/index.ts";

export const fixedNow = Date.parse("2026-10-20T12:00:00Z");

export class FakeStorage {
  values = new Map();
  queue = Promise.resolve();

  transaction(callback) {
    const operation = this.queue.then(async () => {
      const draft = structuredClone(this.values);
      const result = await callback({
        get: async (key) => structuredClone(draft.get(key)),
        put: async (key, value) => {
          draft.set(key, structuredClone(value));
        },
      });
      this.values = draft;
      return result;
    });
    this.queue = operation.catch(() => undefined);
    return operation;
  }
}

export function sampleUpdate(overrides = {}) {
  return {
    update_id: 42,
    message: {
      message_id: 7,
      date: fixedNow / 1_000,
      from: { id: 1234 },
      chat: { id: 1234, type: "private" },
      text: "/snapshot",
    },
    ...overrides,
  };
}

export function testRig(options = {}) {
  const storage = options.storage ?? new FakeStorage();
  const calls = [];
  const githubFetch =
    options.githubFetch ??
    (async (url, init) => {
      calls.push({ url, init });
      return new Response(null, { status: 204 });
    });
  const clock = options.clock ?? (() => fixedNow);
  const env = {
    TELEGRAM_WEBHOOK_SECRET: "test_secret-token_123",
    GITHUB_DISPATCH_TOKEN: "dummy_github_token",
    GITHUB_OWNER: "KlinovK",
    GITHUB_REPOSITORY: "advancedTradingAdvisor",
    GITHUB_WORKFLOW: "binance-on-demand-snapshot.yml",
    ALLOWED_TELEGRAM_USER_ID: "1234",
    ALLOWED_TELEGRAM_CHAT_ID: "1234",
    COOLDOWN_SECONDS: "600",
  };
  Object.assign(env, options.env ?? {});
  const coordinator = new SnapshotCoordinator(
    { storage },
    env,
    clock,
    githubFetch,
  );
  env.SNAPSHOT_COORDINATOR = {
    getByName: (name) => {
      if (name !== "telegram-snapshot-global")
        throw new Error("wrong object name");
      return { fetch: (request) => coordinator.fetch(request) };
    },
  };

  async function webhook(update = sampleUpdate(), details = {}) {
    const request = new Request(
      details.url ?? "https://worker.test/telegram/webhook",
      {
        method: details.method ?? "POST",
        headers: {
          "Content-Type": details.contentType ?? "application/json",
          ...(details.secret === null
            ? {}
            : {
                "X-Telegram-Bot-Api-Secret-Token":
                  details.secret ?? env.TELEGRAM_WEBHOOK_SECRET,
              }),
          ...(details.headers ?? {}),
        },
        ...(details.method === "GET"
          ? {}
          : { body: details.body ?? JSON.stringify(update) }),
      },
    );
    const response = await handleWebhook(
      request,
      env,
      details.nowMs ?? clock(),
    );
    return { code: response.status, body: await response.json() };
  }

  return { storage, calls, env, coordinator, webhook };
}

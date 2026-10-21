import assert from "node:assert/strict";
import { createTestHarness } from "wrangler";

// Local workerd integration check. It makes no GitHub or Telegram request.
const harness = createTestHarness({
  workers: [
    {
      configPath: "./wrangler.jsonc",
      vars: {
        ALLOWED_TELEGRAM_USER_ID: "1234",
        ALLOWED_TELEGRAM_CHAT_ID: "1234",
      },
      secrets: {
        TELEGRAM_WEBHOOK_SECRET: "test_secret-token_123",
        GITHUB_DISPATCH_TOKEN: "dummy",
      },
    },
  ],
});

try {
  await harness.listen();
  const worker = harness.getWorker();
  const unauthorized = await worker.fetch(
    "https://worker.test/telegram/webhook",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    },
  );
  assert.equal(unauthorized.status, 401);
  const env = await worker.getEnv();
  const stub = env.SNAPSHOT_COORDINATOR.getByName("telegram-snapshot-global");
  const invalid = await stub.fetch("https://internal/invalid");
  assert.equal(invalid.status, 400);
  console.log("Local workerd Worker and Durable Object startup: PASS");
} finally {
  await harness.close();
}

// Exercise the production coordinator against local SQLite-backed workerd
// storage. The fixture replaces its outbound GitHub fetch, so this remains
// fully offline even when an authorized webhook is accepted.
const integrationHarness = createTestHarness({
  workers: [
    {
      config: {
        name: "telegram-snapshot-local-integration",
        main: "tests/workerd-fixture.mjs",
        compatibility_date: "2026-10-20",
        workers_dev: false,
        routes: [],
        vars: {
          TELEGRAM_WEBHOOK_SECRET: "test_secret-token_123",
          GITHUB_DISPATCH_TOKEN: "dummy",
          GITHUB_OWNER: "KlinovK",
          GITHUB_REPOSITORY: "advancedTradingAdvisor",
          GITHUB_WORKFLOW: "binance-on-demand-snapshot.yml",
          ALLOWED_TELEGRAM_USER_ID: "1234",
          ALLOWED_TELEGRAM_CHAT_ID: "1234",
          COOLDOWN_SECONDS: "600",
        },
        durable_objects: {
          bindings: [
            {
              name: "SNAPSHOT_COORDINATOR",
              class_name: "SnapshotCoordinator",
            },
          ],
        },
        migrations: [
          {
            tag: "v1",
            new_sqlite_classes: ["SnapshotCoordinator"],
          },
        ],
      },
    },
  ],
});

try {
  await integrationHarness.listen();
  const worker = integrationHarness.getWorker();
  const update = (updateId) => ({
    update_id: updateId,
    message: {
      message_id: 7,
      date: Math.floor(Date.now() / 1_000),
      from: { id: 1234 },
      chat: { id: 1234, type: "private" },
      text: "/snapshot",
    },
  });
  const send = async (updateId) => {
    const response = await worker.fetch(
      "https://worker.test/telegram/webhook",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Telegram-Bot-Api-Secret-Token": "test_secret-token_123",
        },
        body: JSON.stringify(update(updateId)),
      },
    );
    assert.equal(response.status, 200);
    return (await response.json()).status;
  };
  const statuses = await Promise.all([send(42), send(42), send(43)]);
  assert.deepEqual(statuses.sort(), ["accepted", "cooldown", "duplicate"]);

  const env = await worker.getEnv();
  const stub = env.SNAPSHOT_COORDINATOR.getByName("telegram-snapshot-global");
  const getCount = async () => {
    const response = await stub.fetch("https://internal/test/post-count");
    assert.equal(response.status, 200);
    return (await response.json()).count;
  };
  assert.equal(await getCount(), 1);

  const sqliteResponse = await stub.fetch("https://internal/test/sqlite");
  assert.deepEqual(await sqliteResponse.json(), { available: true });

  await worker.evictDurableObject("SNAPSHOT_COORDINATOR", {
    name: "telegram-snapshot-global",
  });
  assert.equal(await send(42), "duplicate");
  assert.equal(await getCount(), 1);
  console.log("Local SQLite Durable Object concurrency and restart: PASS");
} finally {
  await integrationHarness.close();
}

import assert from "node:assert/strict";
import { createServer } from "node:http";
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

const localHeaderServer = createServer((request, response) => {
  response.setHeader("Content-Type", "application/json");
  response.end(
    JSON.stringify({ userAgent: request.headers["user-agent"] ?? null }),
  );
});

try {
  await integrationHarness.listen();
  await new Promise((resolve) =>
    localHeaderServer.listen(0, "127.0.0.1", resolve),
  );
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

  const constructionResponse = await stub.fetch(
    "https://internal/test/request-construction",
  );
  assert.equal(constructionResponse.status, 200);
  const construction = await constructionResponse.json();
  assert.equal(construction.manual, "supported");
  assert.equal(construction.error, "TypeError");
  assert.equal(construction.defaultUserAgent, null);
  console.log("Local workerd redirect:error construction:", construction.error);
  console.log("Local workerd Request default User-Agent: absent");

  const localTarget = `http://127.0.0.1:${localHeaderServer.address().port}/headers`;
  const outboundResponse = await stub.fetch(
    `https://internal/test/local-outbound-user-agent?target=${encodeURIComponent(localTarget)}`,
  );
  assert.equal(outboundResponse.status, 200);
  const outboundHeaders = await outboundResponse.json();
  console.log(
    "Local workerd outbound default User-Agent:",
    outboundHeaders.userAgent,
  );
  const explicitResponse = await stub.fetch(
    `https://internal/test/local-outbound-user-agent?target=${encodeURIComponent(localTarget)}&explicit=1`,
  );
  assert.equal(explicitResponse.status, 200);
  assert.deepEqual(await explicitResponse.json(), {
    userAgent: "telegram-snapshot-dispatcher/1.0",
  });
  console.log("Local workerd outbound explicit User-Agent: PASS");

  const redirectResponse = await stub.fetch(
    "https://internal/test/redirect-dispatch",
  );
  assert.equal(redirectResponse.status, 200);
  assert.deepEqual(await redirectResponse.json(), {
    result: "unknown",
    requests: 1,
    requestMode: "manual",
    userAgent: "telegram-snapshot-dispatcher/1.0",
    credentialUsedForGithub: true,
    credentialSentToUnexpectedDestination: false,
    logsSafe: true,
    events: [
      {
        event: "telegram_snapshot_dispatch",
        stage: "github",
        result: "http_unexpected",
        github_http_status: 302,
      },
    ],
  });
  console.log("Local workerd redirect dispatch and credential isolation: PASS");

  await worker.evictDurableObject("SNAPSHOT_COORDINATOR", {
    name: "telegram-snapshot-global",
  });
  assert.equal(await send(42), "duplicate");
  assert.equal(await getCount(), 1);
  console.log("Local SQLite Durable Object concurrency and restart: PASS");
} finally {
  await integrationHarness.close();
  localHeaderServer.closeAllConnections();
  await new Promise((resolve) => localHeaderServer.close(resolve));
}

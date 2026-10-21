import assert from "node:assert/strict";
import { test } from "node:test";
import { SnapshotCoordinator } from "../src/coordinator.ts";
import { fixedNow, FakeStorage, sampleUpdate, testRig } from "./helpers.mjs";

function rigWithReservationDelay(initialNow, finalNow, options = {}) {
  let now = initialNow;
  const storage = new FakeStorage();
  const transaction = storage.transaction.bind(storage);
  let firstTransaction = true;
  storage.transaction = (callback) =>
    transaction(async (entry) => {
      const result = await callback(entry);
      if (firstTransaction) {
        firstTransaction = false;
        now = finalNow;
      }
      return result;
    });
  const rig = testRig({
    storage,
    clock: () => now,
    ...options,
  });
  return { rig, storage, clock: () => now };
}

test("Telegram retry of an accepted update is deduplicated", async () => {
  const rig = testRig();
  assert.equal((await rig.webhook()).body.status, "accepted");
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(rig.calls.length, 1);
});

test("different update during ten-minute cooldown is recorded and never dispatched later", async () => {
  let now = fixedNow;
  const rig = testRig({ clock: () => now });
  assert.equal((await rig.webhook()).body.status, "accepted");
  const next = sampleUpdate({ update_id: 43 });
  assert.equal((await rig.webhook(next)).body.status, "cooldown");
  now += 10 * 60_000;
  assert.equal(
    (await rig.webhook(next, { nowMs: now })).body.status,
    "ignored",
  );
  // Call the coordinator directly to isolate persistent dedup from message age.
  const request = () =>
    new Request("https://internal/dispatch", {
      method: "POST",
      body: JSON.stringify({
        updateId: "43",
        messageDateSeconds: now / 1_000,
      }),
    });
  assert.equal(
    (await (await rig.coordinator.fetch(request())).json()).status,
    "duplicate",
  );
  assert.equal(rig.calls.length, 1);
});

test("concurrent duplicate and distinct requests cannot bypass reservation or cooldown", async () => {
  const rig = testRig();
  const results = await Promise.all([
    rig.webhook(),
    rig.webhook(),
    rig.webhook(sampleUpdate({ update_id: 43 })),
  ]);
  assert.deepEqual(results.map((result) => result.body.status).sort(), [
    "accepted",
    "cooldown",
    "duplicate",
  ]);
  assert.equal(rig.calls.length, 1);
});

test("persistent state survives object restart", async () => {
  const storage = new FakeStorage();
  const rig = testRig({ storage });
  assert.equal((await rig.webhook()).body.status, "accepted");
  const restarted = new SnapshotCoordinator(
    { storage },
    rig.env,
    () => fixedNow,
    async () => {
      throw new Error("unexpected second network call");
    },
  );
  const response = await restarted.fetch(
    new Request("https://internal/dispatch", {
      method: "POST",
      body: JSON.stringify({
        updateId: "42",
        messageDateSeconds: fixedNow / 1_000,
      }),
    }),
  );
  assert.equal((await response.json()).status, "duplicate");
});

test("a command that ages while routed to the Durable Object is never dispatched", async () => {
  let now = fixedNow;
  const rig = testRig({ clock: () => now });
  const originalGetByName = rig.env.SNAPSHOT_COORDINATOR.getByName;
  rig.env.SNAPSHOT_COORDINATOR.getByName = (name) => {
    const stub = originalGetByName(name);
    return {
      fetch: (request) => {
        now += 121_000;
        return stub.fetch(request);
      },
    };
  };
  const result = await rig.webhook();
  assert.equal(result.body.status, "stale_message");
  assert.equal(rig.calls.length, 0);
});

test("a reservation finishing after the two-minute limit cannot dispatch", async () => {
  const { rig, storage } = rigWithReservationDelay(
    fixedNow + 119_000,
    fixedNow + 121_000,
  );

  const result = await rig.webhook();

  assert.equal(result.code, 200);
  assert.equal(result.body.status, "stale_message");
  assert.equal(rig.calls.length, 0);
  const state = storage.values.get("coordination_v1");
  assert.equal(state.recent[0].id, "42");
  assert.equal(state.recent[0].state, "dispatching");
  assert.equal(state.cooldownUntilMs, fixedNow + 119_000 + 600_000);
});

for (const [name, finalAgeMs] of [
  ["exactly 120 seconds", 120_000],
  ["121 seconds", 121_000],
]) {
  test(`final command age ${name} respects the inclusive limit`, async () => {
    const { rig } = rigWithReservationDelay(
      fixedNow + 119_000,
      fixedNow + finalAgeMs,
    );
    const result = await rig.webhook();
    assert.equal(
      result.body.status,
      finalAgeMs === 120_000 ? "accepted" : "stale_message",
    );
    assert.equal(rig.calls.length, finalAgeMs === 120_000 ? 1 : 0);
  });
}

test("valid initial and final command ages make exactly one dispatch", async () => {
  const { rig } = rigWithReservationDelay(fixedNow + 30_000, fixedNow + 31_000);
  assert.equal((await rig.webhook()).body.status, "accepted");
  assert.equal(rig.calls.length, 1);
});

test("a permitted future timestamp can become current during reservation", async () => {
  const { rig } = rigWithReservationDelay(fixedNow, fixedNow + 30_000);
  const update = sampleUpdate({
    message: { ...sampleUpdate().message, date: fixedNow / 1_000 + 30 },
  });
  assert.equal((await rig.webhook(update)).body.status, "accepted");
  assert.equal(rig.calls.length, 1);
});

test("final future skew of exactly 30 seconds remains eligible", async () => {
  const { rig } = rigWithReservationDelay(fixedNow - 30_000, fixedNow - 30_000);
  assert.equal((await rig.webhook()).body.status, "accepted");
  assert.equal(rig.calls.length, 1);
});

test("final clock regression beyond future skew cannot dispatch", async () => {
  const { rig, storage } = rigWithReservationDelay(
    fixedNow - 30_000,
    fixedNow - 31_000,
  );
  assert.equal((await rig.webhook()).body.status, "future_message");
  assert.equal(rig.calls.length, 0);
  assert.equal(storage.values.get("coordination_v1").recent[0].id, "42");
  assert.equal(
    storage.values.get("coordination_v1").cooldownUntilMs,
    fixedNow - 30_000 + 600_000,
  );
});

test("final-age skip survives a retry and Durable Object restart", async () => {
  const { rig, storage, clock } = rigWithReservationDelay(
    fixedNow + 119_000,
    fixedNow + 121_000,
  );
  assert.equal((await rig.webhook()).body.status, "stale_message");
  assert.equal((await rig.webhook()).body.status, "ignored");
  const retried = sampleUpdate({
    message: { ...sampleUpdate().message, date: clock() / 1_000 },
  });
  assert.equal((await rig.webhook(retried)).body.status, "duplicate");
  const restarted = new SnapshotCoordinator(
    { storage },
    rig.env,
    clock,
    async () => {
      throw new Error("unexpected GitHub POST after restart");
    },
  );
  const response = await restarted.fetch(
    new Request("https://internal/dispatch", {
      method: "POST",
      body: JSON.stringify({
        updateId: "42",
        messageDateSeconds: clock() / 1_000,
      }),
    }),
  );
  assert.equal((await response.json()).status, "duplicate");
  assert.equal(rig.calls.length, 0);
});

test("an ambiguous GitHub result after final validation is never retried", async () => {
  let attempts = 0;
  const { rig } = rigWithReservationDelay(fixedNow, fixedNow + 1_000, {
    githubFetch: async () => {
      attempts += 1;
      throw new DOMException("ambiguous timeout", "AbortError");
    },
  });
  assert.equal((await rig.webhook()).body.status, "unknown");
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(attempts, 1);
});

test("GitHub rejection is explicit and is not retried", async () => {
  let attempts = 0;
  const rig = testRig({
    githubFetch: async () => {
      attempts += 1;
      return new Response(null, { status: 403 });
    },
  });
  assert.equal((await rig.webhook()).body.status, "rejected");
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(attempts, 1);
});

test("GitHub server failure is unknown and is not retried", async () => {
  let attempts = 0;
  const rig = testRig({
    githubFetch: async () => {
      attempts += 1;
      return new Response(null, { status: 503 });
    },
  });
  assert.equal((await rig.webhook()).body.status, "unknown");
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(attempts, 1);
});

test("ambiguous timeout after possible send is unknown with one attempt", async () => {
  let attempts = 0;
  const rig = testRig({
    githubFetch: async (_url, init) => {
      attempts += 1;
      assert.equal(init.signal.aborted, false);
      throw new DOMException("timed out", "AbortError");
    },
  });
  assert.equal((await rig.webhook()).body.status, "unknown");
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(attempts, 1);
});

test("crash after reservation but before result keeps at-most-once behavior", async () => {
  const storage = new FakeStorage();
  storage.values.set("coordination_v1", {
    cooldownUntilMs: fixedNow + 600_000,
    recent: [{ id: "42", seenAtMs: fixedNow, state: "dispatching" }],
  });
  let attempts = 0;
  const rig = testRig({
    storage,
    githubFetch: async () => {
      attempts += 1;
      return new Response(null, { status: 204 });
    },
  });
  assert.equal((await rig.webhook()).body.status, "duplicate");
  assert.equal(attempts, 0);
});

test("bounded state fails closed at capacity", async () => {
  const storage = new FakeStorage();
  storage.values.set("coordination_v1", {
    cooldownUntilMs: 0,
    recent: Array.from({ length: 1_024 }, (_, index) => ({
      id: String(index + 1_000),
      seenAtMs: fixedNow,
      state: "cooldown",
    })),
  });
  const rig = testRig({ storage });
  assert.equal((await rig.webhook()).body.status, "capacity");
  assert.equal(rig.calls.length, 0);
  assert.equal(storage.values.get("coordination_v1").recent.length, 1_024);
});

test("expired entries are pruned and free capacity", async () => {
  const storage = new FakeStorage();
  storage.values.set("coordination_v1", {
    cooldownUntilMs: 0,
    recent: Array.from({ length: 1_024 }, (_, index) => ({
      id: String(index + 1_000),
      seenAtMs: fixedNow - 48 * 60 * 60_000,
      state: "accepted",
    })),
  });
  const rig = testRig({ storage });
  assert.equal((await rig.webhook()).body.status, "accepted");
  assert.equal(storage.values.get("coordination_v1").recent.length, 1);
});

test("no secrets appear in response or logs, even when dispatch throws", async () => {
  const logs = [];
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...items) => logs.push(items.join(" "));
  console.error = (...items) => logs.push(items.join(" "));
  try {
    const rig = testRig({
      githubFetch: async () => {
        throw new Error("dummy_github_token test_secret-token_123");
      },
    });
    const result = await rig.webhook();
    assert.equal(result.body.status, "unknown");
    const output = JSON.stringify(result) + logs.join("\n");
    assert.equal(output.includes("dummy_github_token"), false);
    assert.equal(output.includes("test_secret-token_123"), false);
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }
});

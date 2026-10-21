import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.ts";
import { fixedNow, sampleUpdate, testRig } from "./helpers.mjs";

test("authorized private /snapshot dispatches once with fixed endpoint and Worker time", async () => {
  const rig = testRig();
  const result = await rig.webhook();
  assert.deepEqual(result, { code: 200, body: { status: "accepted" } });
  assert.equal(rig.calls.length, 1);
  assert.equal(
    rig.calls[0].url,
    "https://api.github.com/repos/KlinovK/advancedTradingAdvisor/actions/workflows/binance-on-demand-snapshot.yml/dispatches",
  );
  assert.equal(rig.calls[0].init.method, "POST");
  assert.equal(rig.calls[0].init.redirect, "error");
  assert.deepEqual(JSON.parse(rig.calls[0].init.body), {
    ref: "main",
    inputs: { requested_at_utc: "2026-10-20T12:00:00Z" },
  });
});

test("Cloudflare fetch entry point uses current time, not the ExecutionContext argument", async () => {
  const rig = testRig();
  const request = new Request("https://worker.test/telegram/webhook", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": rig.env.TELEGRAM_WEBHOOK_SECRET,
    },
    body: JSON.stringify(
      sampleUpdate({
        message: {
          ...sampleUpdate().message,
          date: Math.floor(Date.now() / 1_000) - 121,
        },
      }),
    ),
  });
  const response = await worker.fetch(request, rig.env, { waitUntil() {} });
  assert.deepEqual(await response.json(), {
    status: "ignored",
    reason: "stale_message",
  });
  assert.equal(rig.calls.length, 0);
});

for (const [name, details, code] of [
  ["invalid secret", { secret: "wrong" }, 401],
  ["missing secret", { secret: null }, 401],
  ["wrong path", { url: "https://worker.test/other" }, 404],
  ["query string", { url: "https://worker.test/telegram/webhook?x=1" }, 404],
  ["wrong method", { method: "GET" }, 405],
  ["invalid content type", { contentType: "text/plain" }, 400],
  ["invalid JSON", { body: "{" }, 400],
  ["oversized body", { body: "x".repeat(8_193) }, 413],
  ["oversized declaration", { headers: { "Content-Length": "9000" } }, 413],
]) {
  test(name + " cannot dispatch", async () => {
    const rig = testRig();
    const result = await rig.webhook(sampleUpdate(), details);
    assert.equal(result.code, code);
    assert.equal(rig.calls.length, 0);
  });
}

test("unauthorized caller body is never consumed", async () => {
  const rig = testRig();
  const result = await rig.webhook(sampleUpdate(), {
    secret: "wrong",
    body: "not json",
  });
  assert.deepEqual(result, { code: 401, body: { status: "unauthorized" } });
});

for (const [name, update, expected] of [
  [
    "edited message",
    { edited_message: sampleUpdate().message },
    "unsupported_update",
  ],
  ["callback", { callback_query: {} }, "unsupported_update"],
  ["channel post", { channel_post: {} }, "unsupported_update"],
  [
    "wrong user",
    { message: { ...sampleUpdate().message, from: { id: 4567 } } },
    "unauthorized_user",
  ],
  [
    "wrong chat",
    {
      message: {
        ...sampleUpdate().message,
        chat: { id: 4567, type: "private" },
      },
    },
    "unauthorized_chat",
  ],
  [
    "group chat",
    {
      message: {
        ...sampleUpdate().message,
        chat: { id: -1234, type: "group" },
      },
    },
    "unauthorized_chat",
  ],
  [
    "command argument",
    { message: { ...sampleUpdate().message, text: "/snapshot now" } },
    "invalid_command",
  ],
  [
    "other command",
    { message: { ...sampleUpdate().message, text: "/help" } },
    "invalid_command",
  ],
  [
    "stale",
    { message: { ...sampleUpdate().message, date: fixedNow / 1000 - 121 } },
    "stale_message",
  ],
  [
    "future",
    { message: { ...sampleUpdate().message, date: fixedNow / 1000 + 31 } },
    "future_message",
  ],
]) {
  test(name + " is ignored without dispatch", async () => {
    const rig = testRig();
    const result = await rig.webhook(sampleUpdate(update));
    assert.deepEqual(result, {
      code: 200,
      body: { status: "ignored", reason: expected },
    });
    assert.equal(rig.calls.length, 0);
  });
}

test("update without a message is ignored", async () => {
  const rig = testRig();
  assert.deepEqual(await rig.webhook({ update_id: 42 }), {
    code: 200,
    body: { status: "ignored", reason: "unsupported_update" },
  });
  assert.equal(rig.calls.length, 0);
});

for (const update of [
  { update_id: "42" },
  { update_id: 9007199254740992 },
  { update_id: -1 },
  { message: { ...sampleUpdate().message, message_id: "7" } },
  { message: { ...sampleUpdate().message, date: 9007199254740992 } },
  { message: { ...sampleUpdate().message, from: { id: 9007199254740992 } } },
  {
    message: {
      ...sampleUpdate().message,
      chat: { id: 9007199254740992, type: "private" },
    },
  },
  { message: { ...sampleUpdate().message, text: 123 } },
]) {
  test("invalid structure or unsafe numeric ID is rejected", async () => {
    const rig = testRig();
    const result = await rig.webhook(sampleUpdate(update));
    assert.equal(result.code, 400);
    assert.equal(rig.calls.length, 0);
  });
}

test("configured ID above Telegram's private-user range fails closed", async () => {
  const rig = testRig({
    env: { ALLOWED_TELEGRAM_USER_ID: "9007199254740993" },
  });
  const result = await rig.webhook();
  assert.deepEqual(result, { code: 503, body: { status: "unavailable" } });
  assert.equal(rig.calls.length, 0);
});

test("authenticated fractional ID lexeme can normalize to a safe integer", async () => {
  const rig = testRig();
  const body = JSON.stringify(sampleUpdate()).replace(
    '"id":1234',
    '"id":1234.0000000000001',
  );
  assert.equal(JSON.parse(body).message.from.id, 1234);
  // The existing guarantee is exact comparison of parsed safe integers, not
  // preservation of the original JSON number token's spelling.
  assert.equal(
    (await rig.webhook(sampleUpdate(), { body })).body.status,
    "accepted",
  );
  assert.equal(rig.calls.length, 1);
});

test("message age boundaries are inclusive", async () => {
  for (const offset of [-120, 30]) {
    const rig = testRig();
    const result = await rig.webhook(
      sampleUpdate({
        message: { ...sampleUpdate().message, date: fixedNow / 1000 + offset },
      }),
    );
    assert.equal(result.body.status, "accepted");
    assert.equal(rig.calls.length, 1);
  }
});

test("invalid configuration fails closed", async () => {
  for (const env of [
    { GITHUB_REPOSITORY: "other" },
    { GITHUB_WORKFLOW: "other.yml" },
    { COOLDOWN_SECONDS: "0" },
    { ALLOWED_TELEGRAM_USER_ID: "REPLACE_WITH_USER_ID" },
    { TELEGRAM_WEBHOOK_SECRET: "" },
    { GITHUB_DISPATCH_TOKEN: "" },
  ]) {
    const rig = testRig({ env });
    assert.equal((await rig.webhook()).code, 503);
    assert.equal(rig.calls.length, 0);
  }
});

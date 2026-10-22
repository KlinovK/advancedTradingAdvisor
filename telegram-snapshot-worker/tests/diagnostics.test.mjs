import assert from "node:assert/strict";
import { test } from "node:test";
import { dispatchSnapshot } from "../src/github.ts";
import { fixedNow, FakeStorage, sampleUpdate, testRig } from "./helpers.mjs";

async function captureDiagnostics(run) {
  const lines = [];
  const originalInfo = console.info;
  console.info = (...args) => lines.push(args);
  try {
    const response = await run();
    const events = lines.map((args) => {
      assert.equal(args.length, 1);
      assert.equal(typeof args[0], "string");
      const event = JSON.parse(args[0]);
      assert.deepEqual(Object.keys(event), [
        "event",
        "stage",
        "result",
        ...(event.github_http_status === undefined
          ? []
          : ["github_http_status"]),
        ...(event.exception_category === undefined
          ? []
          : ["exception_category"]),
        ...(event.github_error_category === undefined
          ? []
          : ["github_error_category"]),
      ]);
      assert.equal(event.event, "telegram_snapshot_dispatch");
      return event;
    });
    return { response, events, output: lines.flat().join("\n") };
  } finally {
    console.info = originalInfo;
  }
}

const event = (
  stage,
  result,
  github_http_status,
  exception_category,
  github_error_category,
) => ({
  event: "telegram_snapshot_dispatch",
  stage,
  result,
  ...(github_http_status === undefined ? {} : { github_http_status }),
  ...(exception_category === undefined ? {} : { exception_category }),
  ...(github_error_category === undefined ? {} : { github_error_category }),
});

test("accepted dispatch has stage events and only the GitHub status", async () => {
  const rig = testRig();
  const { response, events } = await captureDiagnostics(() => rig.webhook());
  assert.deepEqual(response, { code: 200, body: { status: "accepted" } });
  assert.deepEqual(events, [
    event("webhook", "eligible"),
    event("durable_object", "reserved"),
    event("github", "accepted", 204),
  ]);
});

for (const [status, result, responseStatus] of [
  [403, "http_rejected", "rejected"],
  [503, "http_unexpected", "unknown"],
]) {
  test(`GitHub HTTP ${status} is logged without body or headers`, async () => {
    const rig = testRig({
      githubFetch: async () =>
        new Response("private_response_body", {
          status,
          headers: { "X-Private": "private_response_header" },
        }),
    });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.equal(response.body.status, responseStatus);
    assert.deepEqual(
      events.at(-1),
      event(
        "github",
        result,
        status,
        undefined,
        status === 403 ? "unknown" : undefined,
      ),
    );
    assert.equal(output.includes("private_response_body"), false);
    assert.equal(output.includes("private_response_header"), false);
  });
}

for (const [name, headers, body, category] of [
  [
    "insufficient permissions",
    {},
    { message: "Resource not accessible by personal access token" },
    "insufficient_permissions",
  ],
  [
    "primary rate limit",
    { "x-ratelimit-remaining": "0" },
    { message: "private_response_detail" },
    "primary_rate_limit",
  ],
  [
    "secondary rate limit",
    { "x-ratelimit-remaining": "17", "retry-after": "60" },
    {
      message:
        "You have exceeded a secondary rate limit. private_response_detail",
    },
    "secondary_rate_limit",
  ],
  [
    "secondary rate limit with zero remaining",
    { "x-ratelimit-remaining": "0" },
    { message: "You have exceeded a secondary rate limit." },
    "secondary_rate_limit",
  ],
  [
    "SSO resource restriction",
    { "x-github-sso": "required; url=https://private.example/secret" },
    { message: "private_response_detail" },
    "sso_authorization_required",
  ],
  [
    "unknown rejection",
    { "retry-after": "60", "x-accepted-github-permissions": "actions=write" },
    { message: "private_response_detail" },
    "unknown",
  ],
  [
    "ambiguous rate-limit wording",
    {},
    { message: "This is not a secondary rate limit; private_response_detail" },
    "unknown",
  ],
  ["missing metadata", {}, {}, "unknown"],
]) {
  test(`GitHub 403 ${name} is classified without exposing response content`, async () => {
    const rig = testRig({
      githubFetch: async () =>
        Response.json(
          { ...body, secret: "private_response_secret" },
          { status: 403, headers },
        ),
    });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.equal(response.body.status, "rejected");
    assert.deepEqual(
      events.at(-1),
      event("github", "http_rejected", 403, undefined, category),
    );
    for (const sensitive of [
      "private_response_detail",
      "private_response_secret",
      "https://private.example/secret",
      "actions=write",
      rig.env.GITHUB_DISPATCH_TOKEN,
    ]) {
      assert.equal(output.includes(sensitive), false);
    }
  });
}

for (const [name, body, headers] of [
  ["malformed JSON", "{private_response_detail", {}],
  ["oversized body", "private_response_detail".repeat(300), {}],
  [
    "oversized declared body",
    "private_response_detail",
    { "content-length": "99999" },
  ],
]) {
  test(`GitHub 403 ${name} remains rejected with unknown category`, async () => {
    const rig = testRig({
      githubFetch: async () => new Response(body, { status: 403, headers }),
    });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.equal(response.body.status, "rejected");
    assert.deepEqual(
      events.at(-1),
      event("github", "http_rejected", 403, undefined, "unknown"),
    );
    assert.equal(output.includes("private_response_detail"), false);
  });
}

test("a stalled GitHub 403 body cannot extend the dispatch timeout", async () => {
  const rig = testRig();
  const { response, events } = await captureDiagnostics(() =>
    dispatchSnapshot(
      rig.env,
      fixedNow,
      async () =>
        new Response(new ReadableStream({ start() {} }), { status: 403 }),
      1,
    ),
  );
  assert.equal(response, "rejected");
  assert.deepEqual(events, [
    event("github", "http_rejected", 403, undefined, "unknown"),
  ]);
});

for (const redirectStatus of [301, 302, 303, 307, 308]) {
  test(`GitHub HTTP ${redirectStatus} is not followed or allowed to forward credentials`, async () => {
    const calls = [];
    const destination = "https://untrusted.invalid/collect";
    const rig = testRig({
      githubFetch: async (url, init) => {
        calls.push({ url, init });
        return new Response("private_redirect_body", {
          status: redirectStatus,
          headers: {
            Location: destination,
            "X-Private": "private_redirect_header",
          },
        });
      },
    });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.deepEqual(response, { code: 200, body: { status: "unknown" } });
    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      "https://api.github.com/repos/KlinovK/advancedTradingAdvisor/actions/workflows/binance-on-demand-snapshot.yml/dispatches",
    );
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(
      calls[0].init.headers.Authorization,
      `Bearer ${rig.env.GITHUB_DISPATCH_TOKEN}`,
    );
    assert.deepEqual(
      events.at(-1),
      event("github", "http_unexpected", redirectStatus),
    );
    for (const sensitive of [
      rig.env.GITHUB_DISPATCH_TOKEN,
      destination,
      "private_redirect_body",
      "private_redirect_header",
    ]) {
      assert.equal(output.includes(sensitive), false);
    }
  });
}

test("timer abort is logged as timeout and keeps the unknown response", async () => {
  const rig = testRig();
  const { response, events } = await captureDiagnostics(() =>
    dispatchSnapshot(
      rig.env,
      fixedNow,
      async (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener(
            "abort",
            () =>
              reject(new DOMException("private_abort_detail", "AbortError")),
            { once: true },
          );
        }),
      1,
    ),
  );
  assert.equal(response, "unknown");
  assert.deepEqual(events, [
    event("github", "timeout", undefined, "AbortError"),
  ]);
});

test("request exception is distinct from timeout and exception text is omitted", async () => {
  const rig = testRig({
    githubFetch: async () => {
      throw new Error(
        "dummy_github_token test_secret-token_123 private_network_detail",
      );
    },
  });
  const { response, events, output } = await captureDiagnostics(() =>
    rig.webhook(),
  );
  assert.equal(response.body.status, "unknown");
  assert.deepEqual(
    events.at(-1),
    event("github", "request_exception", undefined, "Error"),
  );
  for (const secret of [
    "dummy_github_token",
    "test_secret-token_123",
    "private_network_detail",
  ]) {
    assert.equal(output.includes(secret), false);
  }
});

for (const [category, makeError] of [
  [
    "AbortError",
    () => new DOMException("private_exception_message", "AbortError"),
  ],
  [
    "TimeoutError",
    () => new DOMException("private_exception_message", "TimeoutError"),
  ],
  ["TypeError", () => new TypeError("private_exception_message")],
  ["RangeError", () => new RangeError("private_exception_message")],
  ["ReferenceError", () => new ReferenceError("private_exception_message")],
  ["SyntaxError", () => new SyntaxError("private_exception_message")],
  ["URIError", () => new URIError("private_exception_message")],
  ["EvalError", () => new EvalError("private_exception_message")],
  ["AggregateError", () => new AggregateError([], "private_exception_message")],
  [
    "DOMException",
    () => new DOMException("private_exception_message", "NetworkError"),
  ],
  ["Error", () => new Error("private_exception_message")],
  [
    "Unknown",
    () => ({
      name: "TypeError",
      message: "private_exception_message",
      token: "private_exception_property",
    }),
  ],
]) {
  test(`${category} is an allowlisted GitHub exception category`, async () => {
    const rig = testRig({
      githubFetch: async () => {
        const error = makeError();
        error.stack = "private_exception_stack";
        error.token = "private_exception_property";
        throw error;
      },
    });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.equal(response.body.status, "unknown");
    assert.deepEqual(
      events.at(-1),
      event("github", "request_exception", undefined, category),
    );
    assert.equal(output.includes("private_exception_message"), false);
    assert.equal(output.includes("private_exception_stack"), false);
    assert.equal(output.includes("private_exception_property"), false);
  });
}

test("Durable Object request error is distinct from GitHub failures", async () => {
  const rig = testRig();
  rig.env.SNAPSHOT_COORDINATOR.getByName = () => {
    throw new Error("private_durable_object_detail");
  };
  const { response, events, output } = await captureDiagnostics(() =>
    rig.webhook(),
  );
  assert.deepEqual(response, { code: 200, body: { status: "unknown" } });
  assert.deepEqual(events, [
    event("webhook", "eligible"),
    event("durable_object", "request_error"),
  ]);
  assert.equal(output.includes("private_durable_object_detail"), false);
});

for (const [name, response, expected] of [
  [
    "non-2xx",
    new Response("private_durable_object_body", { status: 503 }),
    "response_error",
  ],
  [
    "malformed",
    Response.json({ status: "private_durable_object_body" }),
    "invalid_response",
  ],
  [
    "invalid JSON",
    new Response("private_durable_object_body"),
    "invalid_response",
  ],
]) {
  test(`Durable Object ${name} response is safely classified`, async () => {
    const rig = testRig();
    rig.env.SNAPSHOT_COORDINATOR.getByName = () => ({
      fetch: async () => response,
    });
    const captured = await captureDiagnostics(() => rig.webhook());
    assert.equal(captured.response.body.status, "unknown");
    assert.deepEqual(captured.events.at(-1), event("durable_object", expected));
    assert.equal(
      captured.output.includes("private_durable_object_body"),
      false,
    );
  });
}

test("unauthenticated and ignored updates do not emit dispatch diagnostics", async () => {
  const rig = testRig();
  const unauthorized = await captureDiagnostics(() =>
    rig.webhook(sampleUpdate(), { secret: "wrong" }),
  );
  const ignored = await captureDiagnostics(() =>
    rig.webhook(sampleUpdate({ message: undefined })),
  );
  assert.equal(unauthorized.response.code, 401);
  assert.equal(ignored.response.body.status, "ignored");
  assert.deepEqual(unauthorized.events, []);
  assert.deepEqual(ignored.events, []);
});

test("Durable Object reservation and result write failures remain distinguishable", async () => {
  for (const failedTransaction of [1, 2]) {
    const storage = new FakeStorage();
    const transaction = storage.transaction.bind(storage);
    let transactions = 0;
    storage.transaction = (callback) => {
      transactions += 1;
      if (transactions === failedTransaction)
        throw new Error("private_storage_detail");
      return transaction(callback);
    };
    const rig = testRig({ storage });
    const { response, events, output } = await captureDiagnostics(() =>
      rig.webhook(),
    );
    assert.equal(response.body.status, "unknown");
    assert.ok(
      events.some(
        (item) =>
          item.stage === "durable_object" &&
          item.result ===
            (failedTransaction === 1
              ? "reservation_error"
              : "result_record_error"),
      ),
    );
    assert.equal(output.includes("private_storage_detail"), false);
    assert.equal(rig.calls.length, failedTransaction === 1 ? 0 : 1);
  }
});

test("Telegram identifiers, message fields, secrets, and request data never enter events", async () => {
  const rig = testRig({
    env: {
      TELEGRAM_WEBHOOK_SECRET: "private_webhook_secret",
      GITHUB_DISPATCH_TOKEN: "private_github_token",
      ALLOWED_TELEGRAM_USER_ID: "7654321",
      ALLOWED_TELEGRAM_CHAT_ID: "7654321",
    },
  });
  const update = sampleUpdate({
    update_id: 987654321,
    message: {
      ...sampleUpdate().message,
      from: { id: 7654321 },
      chat: { id: 7654321, type: "private" },
      private_message_field: "private_message_content",
    },
  });
  const { response, output } = await captureDiagnostics(() =>
    rig.webhook(update),
  );
  assert.equal(response.body.status, "accepted");
  for (const sensitive of [
    "987654321",
    "7654321",
    "private_message_content",
    "private_webhook_secret",
    "private_github_token",
    "Authorization",
    "requested_at_utc",
  ]) {
    assert.equal(output.includes(sensitive), false);
  }
});

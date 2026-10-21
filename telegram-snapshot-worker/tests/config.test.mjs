import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { sampleUpdate, testRig } from "./helpers.mjs";

const configPath = fileURLToPath(new URL("../wrangler.jsonc", import.meta.url));
const examplePath = fileURLToPath(
  new URL("../.dev.vars.example", import.meta.url),
);
const schemaPath = fileURLToPath(
  new URL("../node_modules/wrangler/config-schema.json", import.meta.url),
);

function parsedConfig() {
  const { config, error } = ts.parseConfigFileTextToJson(
    configPath,
    readFileSync(configPath, "utf8"),
  );
  assert.equal(error, undefined, "Wrangler config must be valid JSONC");
  return config;
}

test("installed Wrangler supports explicit private URL and required-secret settings", () => {
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  const properties = schema.definitions.RawConfig.properties;
  assert.equal(properties.workers_dev.type, "boolean");
  assert.equal(properties.preview_urls.type, "boolean");
  assert.equal(properties.secrets.properties.required.type, "array");
});

test("production config declares only fixed, nonsecret vars and four required secrets", () => {
  const config = parsedConfig();
  assert.equal(config.name, "telegram-snapshot-dispatcher");
  assert.deepEqual(config.vars, {
    GITHUB_OWNER: "KlinovK",
    GITHUB_REPOSITORY: "advancedTradingAdvisor",
    GITHUB_WORKFLOW: "binance-on-demand-snapshot.yml",
    COOLDOWN_SECONDS: "600",
  });
  assert.deepEqual(config.secrets.required, [
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_DISPATCH_TOKEN",
    "ALLOWED_TELEGRAM_USER_ID",
    "ALLOWED_TELEGRAM_CHAT_ID",
  ]);
  for (const name of config.secrets.required) {
    assert.equal(Object.hasOwn(config.vars, name), false);
  }
});

test("production config has no public URL or route and preserves Durable Object schema", () => {
  const config = parsedConfig();
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
  assert.deepEqual(config.routes, []);
  assert.equal(Object.hasOwn(config, "route"), false);
  assert.equal(Object.hasOwn(config, "triggers"), false);
  assert.deepEqual(config.durable_objects.bindings, [
    { name: "SNAPSHOT_COORDINATOR", class_name: "SnapshotCoordinator" },
  ]);
  assert.deepEqual(config.migrations, [
    { tag: "v1", new_sqlite_classes: ["SnapshotCoordinator"] },
  ]);
});

test("tracked local example contains only four dummy bindings and fails closed", async () => {
  const example = readFileSync(examplePath, "utf8");
  const entries = Object.fromEntries(
    example
      .split("\n")
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => {
        const match = /^([A-Z_]+)="([A-Za-z0-9_]+)"$/.exec(line);
        assert.ok(match, "example bindings must have simple dummy values");
        return [match[1], match[2]];
      }),
  );
  assert.deepEqual(
    Object.keys(entries).sort(),
    parsedConfig().secrets.required.toSorted(),
  );
  assert.equal(entries.TELEGRAM_WEBHOOK_SECRET, "DUMMY_LOCAL_WEBHOOK_SECRET");
  assert.equal(entries.GITHUB_DISPATCH_TOKEN, "DUMMY_LOCAL_GITHUB_TOKEN");
  assert.equal(entries.ALLOWED_TELEGRAM_USER_ID, "DUMMY_USER_ID");
  assert.equal(entries.ALLOWED_TELEGRAM_CHAT_ID, "DUMMY_CHAT_ID");
  const rig = testRig({ env: entries });
  assert.deepEqual(await rig.webhook(), {
    code: 503,
    body: { status: "unavailable" },
  });
  assert.equal(rig.calls.length, 0);
});

for (const name of [
  "TELEGRAM_WEBHOOK_SECRET",
  "GITHUB_DISPATCH_TOKEN",
  "ALLOWED_TELEGRAM_USER_ID",
  "ALLOWED_TELEGRAM_CHAT_ID",
]) {
  test(`missing ${name} fails closed without dispatch`, async () => {
    const rig = testRig({ env: { [name]: undefined } });
    assert.deepEqual(await rig.webhook(), {
      code: 503,
      body: { status: "unavailable" },
    });
    assert.equal(rig.calls.length, 0);
  });
}

for (const [configured, telegram] of [
  ["1", 1],
  ["1234", 1234],
  ["1099511627775", 1099511627775],
]) {
  test(`positive private user/chat ID ${configured} can dispatch`, async () => {
    const rig = testRig({
      env: {
        ALLOWED_TELEGRAM_USER_ID: configured,
        ALLOWED_TELEGRAM_CHAT_ID: configured,
      },
    });
    const message = sampleUpdate().message;
    const update = sampleUpdate({
      message: {
        ...message,
        from: { id: telegram },
        chat: { id: telegram, type: "private" },
      },
    });
    assert.deepEqual(await rig.webhook(update), {
      code: 200,
      body: { status: "accepted" },
    });
    assert.equal(rig.calls.length, 1);
  });
}

for (const name of ["ALLOWED_TELEGRAM_USER_ID", "ALLOWED_TELEGRAM_CHAT_ID"]) {
  for (const [caseName, value] of [
    ["empty", ""],
    ["zero", "0"],
    ["negative", "-1"],
    ["leading whitespace", " 1234"],
    ["trailing whitespace", "1234 "],
    ["fraction", "12.5"],
    ["nonnumeric", "REPLACE_WITH_ID"],
    ["leading zero", "01234"],
    ["signed positive", "+1234"],
    ["scientific notation", "1e3"],
    ["above private ID range", "1099511627776"],
    ["above JavaScript safe integer range", "9007199254740992"],
    ["numeric instead of string", 1234],
  ]) {
    test(`${caseName} ${name} fails closed without dispatch`, async () => {
      const rig = testRig({ env: { [name]: value } });
      assert.deepEqual(await rig.webhook(), {
        code: 503,
        body: { status: "unavailable" },
      });
      assert.equal(rig.calls.length, 0);
    });
  }
}

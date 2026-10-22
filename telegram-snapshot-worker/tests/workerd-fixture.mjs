import worker from "../src/index.ts";
import { SnapshotCoordinator as ProductionCoordinator } from "../src/coordinator.ts";
import { dispatchSnapshot, GITHUB_USER_AGENT } from "../src/github.ts";

const POST_COUNT_KEY = "test_github_post_count";

export default worker;

// This class is used only by the local workerd test. It retains the production
// Durable Object logic while replacing the outbound GitHub call with storage.
export class SnapshotCoordinator extends ProductionCoordinator {
  constructor(context, env) {
    super(context, env, Date.now, async () => {
      const count = (await context.storage.get(POST_COUNT_KEY)) ?? 0;
      await context.storage.put(POST_COUNT_KEY, count + 1);
      return new Response(null, { status: 204 });
    });
    this.storage = context.storage;
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === "/test/post-count") {
      return Response.json({
        count: (await this.storage.get(POST_COUNT_KEY)) ?? 0,
      });
    }
    if (path === "/test/sqlite") {
      const [result] = this.storage.sql.exec("SELECT 1 AS value");
      return Response.json({ available: result?.value === 1 });
    }
    if (path === "/test/request-construction") {
      const defaultUserAgent = new Request("https://api.github.com/", {
        method: "POST",
        body: "{}",
      }).headers.get("user-agent");
      const construct = (redirect) => {
        try {
          new Request("https://example.invalid/dispatch", {
            method: "POST",
            redirect,
            signal: new AbortController().signal,
            headers: {
              Authorization: "Bearer dummy",
              Accept: "application/vnd.github+json",
              "Content-Type": "application/json",
              "X-GitHub-Api-Version": "2022-11-28",
            },
            body: "{}",
          });
          return "supported";
        } catch (error) {
          return error instanceof TypeError ? "TypeError" : "other_error";
        }
      };
      return Response.json({
        manual: construct("manual"),
        error: construct("error"),
        defaultUserAgent,
      });
    }
    if (path === "/test/local-outbound-user-agent") {
      const params = new URL(request.url).searchParams;
      const target = params.get("target");
      if (!target || new URL(target).hostname !== "127.0.0.1") {
        return new Response(null, { status: 400 });
      }
      const response = await fetch(target, {
        method: "POST",
        body: "{}",
        ...(params.get("explicit") === "1"
          ? { headers: { "User-Agent": GITHUB_USER_AGENT } }
          : {}),
      });
      return new Response(response.body, { status: response.status });
    }
    if (path === "/test/redirect-dispatch") {
      const token = "private_workerd_token";
      const destination = "https://untrusted.invalid/collect";
      let requests = 0;
      let requestMode;
      let userAgent;
      let credentialUsedForGithub = false;
      let credentialSentToUnexpectedDestination = false;
      const logs = [];
      const originalInfo = console.info;
      console.info = (...items) => logs.push(items.join(" "));
      let result;
      try {
        result = await dispatchSnapshot(
          { GITHUB_DISPATCH_TOKEN: token },
          Date.now(),
          async (url, init) => {
            // Native workerd Request construction, with no outbound fetch.
            const outgoing = new Request(url, init);
            requests += 1;
            requestMode = outgoing.redirect;
            userAgent = outgoing.headers.get("user-agent");
            const hasCredential =
              outgoing.headers.get("Authorization") === `Bearer ${token}`;
            if (new URL(outgoing.url).hostname === "api.github.com") {
              credentialUsedForGithub = hasCredential;
            } else {
              credentialSentToUnexpectedDestination = hasCredential;
            }
            return new Response("private_redirect_body", {
              status: 302,
              headers: { Location: destination },
            });
          },
        );
      } finally {
        console.info = originalInfo;
      }
      const output = logs.join("\n");
      return Response.json({
        result,
        requests,
        requestMode,
        userAgent,
        credentialUsedForGithub,
        credentialSentToUnexpectedDestination,
        logsSafe: ![token, destination, "private_redirect_body"].some((value) =>
          output.includes(value),
        ),
        events: logs.map((line) => JSON.parse(line)),
      });
    }
    return super.fetch(request);
  }
}

import worker from "../src/index.ts";
import { SnapshotCoordinator as ProductionCoordinator } from "../src/coordinator.ts";

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
    return super.fetch(request);
  }
}

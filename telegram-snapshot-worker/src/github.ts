import type { Env } from "./types.ts";

export const GITHUB_OWNER = "KlinovK";
export const GITHUB_REPOSITORY = "advancedTradingAdvisor";
export const GITHUB_WORKFLOW = "binance-on-demand-snapshot.yml";
export const GITHUB_REF = "main";
export const DISPATCH_TIMEOUT_MS = 5_000;

const DISPATCH_URL =
  `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}` +
  `/actions/workflows/${GITHUB_WORKFLOW}/dispatches`;

export type DispatchResult = "accepted" | "rejected" | "unknown";

export async function dispatchSnapshot(
  env: Env,
  nowMs: number,
  fetcher: typeof fetch = fetch,
): Promise<DispatchResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DISPATCH_TIMEOUT_MS);
  try {
    const response = await fetcher(DISPATCH_URL, {
      method: "POST",
      redirect: "error",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        ref: GITHUB_REF,
        inputs: {
          requested_at_utc: new Date(nowMs).toISOString().slice(0, 19) + "Z",
        },
      }),
    });
    if (response.status === 204) return "accepted";
    if (response.status >= 400 && response.status < 500) return "rejected";
    return "unknown";
  } catch {
    // An abort or network error can happen after GitHub received the POST.
    return "unknown";
  } finally {
    clearTimeout(timer);
  }
}

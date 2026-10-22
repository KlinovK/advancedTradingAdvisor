import type { Env } from "./types.ts";
import {
  logDiagnostic,
  type GithubErrorCategory,
  type GithubExceptionCategory,
} from "./diagnostics.ts";

export const GITHUB_OWNER = "KlinovK";
export const GITHUB_REPOSITORY = "advancedTradingAdvisor";
export const GITHUB_WORKFLOW = "binance-on-demand-snapshot.yml";
export const GITHUB_REF = "main";
export const GITHUB_USER_AGENT = "telegram-snapshot-dispatcher/1.0";
export const DISPATCH_TIMEOUT_MS = 5_000;

const DISPATCH_URL =
  `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}` +
  `/actions/workflows/${GITHUB_WORKFLOW}/dispatches`;

export type DispatchResult = "accepted" | "rejected" | "unknown";

const MAX_ERROR_BYTES = 4_096;

async function readWithAbort(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal: AbortSignal,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  if (signal.aborted) throw new DOMException("", "AbortError");
  let onAbort: () => void = () => {};
  const aborted = new Promise<never>((_resolve, reject) => {
    onAbort = () => reject(new DOMException("", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    return await Promise.race([reader.read(), aborted]);
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
}

async function githubErrorMessage(
  response: Response,
  signal: AbortSignal,
): Promise<string | undefined> {
  const length = response.headers.get("content-length");
  if (
    length !== null &&
    (!/^\d+$/.test(length) || Number(length) > MAX_ERROR_BYTES)
  ) {
    return undefined;
  }
  if (!response.body) return undefined;

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await readWithAbort(reader, signal);
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_ERROR_BYTES) return undefined;
      chunks.push(value);
    }
    const body = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const parsed: unknown = JSON.parse(new TextDecoder().decode(body));
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed)
    ) {
      return undefined;
    }
    const message = (parsed as Record<string, unknown>).message;
    return typeof message === "string" ? message : undefined;
  } catch {
    return undefined;
  } finally {
    // Cancel unfinished or oversized bodies; never wait for a server-controlled stream.
    void reader.cancel().catch(() => {});
  }
}

async function classifyGithubRejection(
  response: Response,
  signal: AbortSignal,
): Promise<GithubErrorCategory> {
  if (response.status !== 403) return "unknown";
  try {
    const remaining = response.headers.get("x-ratelimit-remaining");
    const sso = response.headers.get("x-github-sso");
    if (sso?.startsWith("required;")) return "sso_authorization_required";

    const message = await githubErrorMessage(response, signal);
    if (
      message &&
      /^You have exceeded a secondary rate limit\b/i.test(message)
    ) {
      return "secondary_rate_limit";
    }
    if (remaining === "0") return "primary_rate_limit";
    if (
      message === "Resource not accessible by personal access token" ||
      message === "Resource not accessible by integration"
    ) {
      return "insufficient_permissions";
    }
  } catch {
    // Diagnostic parsing must never change the dispatch result.
  }
  return "unknown";
}

function classifyException(error: unknown): GithubExceptionCategory {
  if (error instanceof DOMException) {
    if (error.name === "AbortError") return "AbortError";
    if (error.name === "TimeoutError") return "TimeoutError";
    return "DOMException";
  }
  if (error instanceof TypeError) return "TypeError";
  if (error instanceof RangeError) return "RangeError";
  if (error instanceof ReferenceError) return "ReferenceError";
  if (error instanceof SyntaxError) return "SyntaxError";
  if (error instanceof URIError) return "URIError";
  if (error instanceof EvalError) return "EvalError";
  if (error instanceof AggregateError) return "AggregateError";
  if (error instanceof Error) return "Error";
  return "Unknown";
}

export async function dispatchSnapshot(
  env: Env,
  nowMs: number,
  fetcher: typeof fetch = fetch,
  timeoutMs: number = DISPATCH_TIMEOUT_MS,
): Promise<DispatchResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(DISPATCH_URL, {
      method: "POST",
      // Return 3xx to this handler; never forward Authorization to Location.
      redirect: "manual",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": GITHUB_USER_AGENT,
      },
      body: JSON.stringify({
        ref: GITHUB_REF,
        inputs: {
          requested_at_utc: new Date(nowMs).toISOString().slice(0, 19) + "Z",
        },
      }),
    });
    if (response.status === 204) {
      logDiagnostic({
        stage: "github",
        result: "accepted",
        githubHttpStatus: response.status,
      });
      return "accepted";
    }
    if (response.status >= 400 && response.status < 500) {
      const githubErrorCategory = await classifyGithubRejection(
        response,
        controller.signal,
      );
      logDiagnostic({
        stage: "github",
        result: "http_rejected",
        githubHttpStatus: response.status,
        githubErrorCategory,
      });
      return "rejected";
    }
    logDiagnostic({
      stage: "github",
      result: "http_unexpected",
      githubHttpStatus: response.status,
    });
    return "unknown";
  } catch (error) {
    // A request exception can happen after GitHub received the POST.
    logDiagnostic({
      stage: "github",
      result: controller.signal.aborted ? "timeout" : "request_exception",
      exceptionCategory: classifyException(error),
    });
    return "unknown";
  } finally {
    clearTimeout(timer);
  }
}

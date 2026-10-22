// Only fixed labels, an allowlisted category, and a GitHub HTTP status may enter logs.
export type GithubExceptionCategory =
  | "AbortError"
  | "TimeoutError"
  | "TypeError"
  | "RangeError"
  | "ReferenceError"
  | "SyntaxError"
  | "URIError"
  | "EvalError"
  | "AggregateError"
  | "DOMException"
  | "Error"
  | "Unknown";

export type GithubErrorCategory =
  | "primary_rate_limit"
  | "secondary_rate_limit"
  | "insufficient_permissions"
  | "sso_authorization_required"
  | "unknown";

type Diagnostic =
  | { stage: "webhook"; result: "eligible" }
  | {
      stage: "durable_object";
      result:
        | "reserved"
        | "duplicate"
        | "cooldown"
        | "capacity"
        | "stale_message"
        | "future_message"
        | "reservation_error"
        | "result_record_error"
        | "request_error"
        | "response_error"
        | "invalid_response";
    }
  | {
      stage: "github";
      result: "accepted" | "http_unexpected";
      githubHttpStatus: number;
    }
  | {
      stage: "github";
      result: "http_rejected";
      githubHttpStatus: number;
      githubErrorCategory: GithubErrorCategory;
    }
  | {
      stage: "github";
      result: "timeout" | "request_exception";
      exceptionCategory: GithubExceptionCategory;
    };

export function logDiagnostic(diagnostic: Diagnostic): void {
  const event = {
    event: "telegram_snapshot_dispatch",
    stage: diagnostic.stage,
    result: diagnostic.result,
    ...(diagnostic.stage === "github" && "githubHttpStatus" in diagnostic
      ? { github_http_status: diagnostic.githubHttpStatus }
      : {}),
    ...(diagnostic.stage === "github" && "exceptionCategory" in diagnostic
      ? { exception_category: diagnostic.exceptionCategory }
      : {}),
    ...(diagnostic.stage === "github" &&
    "githubErrorCategory" in diagnostic &&
    diagnostic.githubErrorCategory
      ? { github_error_category: diagnostic.githubErrorCategory }
      : {}),
  };
  console.info(JSON.stringify(event));
}

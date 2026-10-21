"""Gate on-demand delivery on a recent, strictly formatted UTC dispatch time."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_REQUEST_AGE = timedelta(minutes=10)
# Permit small Worker/runner clock differences without accepting an older queue entry.
MAX_FUTURE_SKEW = timedelta(seconds=30)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def classify_request(value: str | None, now: datetime) -> tuple[bool, str]:
    """Return a fixed result without echoing the untrusted workflow input."""
    if not value:
        return False, "missing_timestamp"
    if not _UTC_TIMESTAMP.fullmatch(value):
        return False, "invalid_timestamp"
    try:
        requested_at = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False, "invalid_timestamp"
    age = now - requested_at
    if age < -MAX_FUTURE_SKEW:
        return False, "future_request"
    if age > MAX_REQUEST_AGE:
        return False, "stale_request"
    return True, "recent_request"


def main(*, now: datetime | None = None) -> int:
    accepted, reason = classify_request(
        os.environ.get("REQUESTED_AT_UTC"), now or datetime.now(UTC)
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        print("SNAPSHOT_REQUEST status=FAILED reason=output_unavailable")
        return 1
    try:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"should_run={'true' if accepted else 'false'}\n")
    except OSError:
        print("SNAPSHOT_REQUEST status=FAILED reason=output_unavailable")
        return 1
    status = "ACCEPTED" if accepted else "SKIPPED"
    print(f"SNAPSHOT_REQUEST status={status} reason={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

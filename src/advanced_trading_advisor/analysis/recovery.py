"""Bounded execution and run-backed lease policy; no background heartbeat needed."""

from datetime import timedelta

ANALYSIS_TIMEOUT_SECONDS = 600
LEASE_DURATION = timedelta(minutes=15)


def lease_expires(started_at):
    return started_at + LEASE_DURATION

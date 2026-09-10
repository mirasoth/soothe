"""User-visible cron messages."""

from __future__ import annotations

AUTOPILOT_REQUIRED_FOR_CRON = (
    "Cron dispatch is unavailable. The loop-native submission path is not "
    "configured — ensure the daemon is running with a valid rail_id before "
    "scheduling cron jobs."
)
"""Message shown when cron submission cannot dispatch via the loop-native path."""

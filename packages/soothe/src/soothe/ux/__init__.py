"""Host-side UX event classification and loop-stream helpers.

Client-side rendering copies live in `soothe_client.ux`; this package owns the
host runtime's canonical versions (kept aligned via the wire protocol contract).
"""

from soothe.ux.classification import classify_event_to_tier
from soothe.ux.loop_stream import LOOP_ASSISTANT_OUTPUT_PHASES, assistant_output_phase
from soothe.ux.types import ESSENTIAL_EVENT_TYPES

__all__ = [
    "LOOP_ASSISTANT_OUTPUT_PHASES",
    "assistant_output_phase",
    "classify_event_to_tier",
    "ESSENTIAL_EVENT_TYPES",
]

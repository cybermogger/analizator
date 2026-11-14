"""Independent, read-only event intelligence.

This package deliberately has no dependency on ``src.models`` or the alert gate.
Events are evidence for a human/operator and never trading instructions.
"""

from .entities import EntityResolver, EventEntity
from .pipeline import EventIntelligencePipeline
from .types import (
    AuthenticityState,
    ContradictionState,
    EventClaim,
    EventType,
    NormalizedEvent,
    RawEvent,
    SourceTier,
)

__all__ = [
    "AuthenticityState",
    "ContradictionState",
    "EntityResolver",
    "EventClaim",
    "EventEntity",
    "EventIntelligencePipeline",
    "EventType",
    "NormalizedEvent",
    "RawEvent",
    "SourceTier",
]

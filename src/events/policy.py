from __future__ import annotations

from dataclasses import dataclass

from .types import AuthenticityState, ContradictionState, EventType, RawEvent, SourceTier


BASE_CREDIBILITY: dict[SourceTier, float] = {
    SourceTier.REGULATOR: 0.98,
    SourceTier.MACRO_CALENDAR: 0.98,
    SourceTier.OFFICIAL_EXCHANGE: 0.94,
    SourceTier.OFFICIAL_PROJECT: 0.93,
    SourceTier.OFFICIAL_FOUNDER: 0.88,
    SourceTier.SECURITY_ADVISORY: 0.92,
    SourceTier.PRIMARY_CODE: 0.86,
    SourceTier.SECONDARY_NEWS: 0.68,
    SourceTier.MARKET_CONFIRMATION: 0.72,
    SourceTier.UNOFFICIAL_SOCIAL: 0.18,
}


@dataclass(frozen=True, slots=True)
class PolicyAssessment:
    credibility: float
    contradiction: ContradictionState
    authenticity_risk: bool
    force_rumor: bool


def assess_source(raw: RawEvent) -> PolicyAssessment:
    credibility = BASE_CREDIBILITY[raw.source_tier]
    authenticity_risk = raw.authenticity is not AuthenticityState.VERIFIED
    contradiction = ContradictionState.NONE
    force_rumor = raw.source_tier is SourceTier.UNOFFICIAL_SOCIAL

    if raw.authenticity is AuthenticityState.COMPROMISE_SUSPECTED:
        credibility = min(credibility, 0.20)
        authenticity_risk = True
        force_rumor = True
        contradiction = ContradictionState.DISPUTED
    elif raw.authenticity is AuthenticityState.DELETED or raw.deleted:
        credibility = min(credibility, 0.25)
        authenticity_risk = True
        contradiction = ContradictionState.DISPUTED
    elif raw.authenticity is AuthenticityState.RETRACTED:
        credibility = 0.0
        authenticity_risk = True
        contradiction = ContradictionState.RETRACTED

    return PolicyAssessment(credibility, contradiction, authenticity_risk, force_rumor)


def combined_credibility(events: tuple[RawEvent, ...]) -> float:
    """Combine independent sources; repeats from the same source do not add evidence."""
    strongest: dict[str, float] = {}
    for raw in events:
        score = assess_source(raw).credibility
        strongest[raw.source_id] = max(strongest.get(raw.source_id, 0.0), score)
    complement = 1.0
    for score in strongest.values():
        complement *= 1.0 - score
    return max(0.0, min(1.0, 1.0 - complement))


def may_publish_as_confirmed(event_type: EventType, events: tuple[RawEvent, ...]) -> bool:
    if event_type is EventType.RUMOR:
        return False
    independent = {item.source_id for item in events if assess_source(item).credibility >= 0.65}
    has_primary = any(
        item.source_tier in {
            SourceTier.REGULATOR,
            SourceTier.MACRO_CALENDAR,
            SourceTier.OFFICIAL_EXCHANGE,
            SourceTier.OFFICIAL_PROJECT,
            SourceTier.OFFICIAL_FOUNDER,
            SourceTier.PRIMARY_CODE,
            SourceTier.SECURITY_ADVISORY,
        }
        and item.authenticity is AuthenticityState.VERIFIED
        for item in events
    )
    return has_primary or len(independent) >= 2

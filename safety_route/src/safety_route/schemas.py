from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PatternMatch:
    attack_type: str
    pattern_id: str
    matched_text: str
    weight: float


@dataclass(frozen=True)
class DetectionResult:
    is_injection: bool
    risk_score: float
    attack_types: list[str]
    matched_patterns: list[PatternMatch] = field(default_factory=list)


@dataclass(frozen=True)
class IntentExtractionResult:
    clean_question: str
    blocked_instructions: list[str]
    removed_spans: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionResult:
    decision: str
    route_allowed: bool
    requires_safety_note: bool
    reason: str


@dataclass(frozen=True)
class SafetyResult:
    raw_question: str
    is_injection: bool
    risk_score: float
    attack_types: list[str]
    matched_patterns: list[dict]
    blocked_instructions: list[str]
    clean_question: str
    decision: str
    route_allowed: bool
    requires_safety_note: bool
    final_safe_response: str

    def to_dict(self) -> dict:
        return {
            "raw_question": self.raw_question,
            "is_injection": self.is_injection,
            "risk_score": self.risk_score,
            "attack_types": self.attack_types,
            "matched_patterns": self.matched_patterns,
            "blocked_instructions": self.blocked_instructions,
            "clean_question": self.clean_question,
            "decision": self.decision,
            "route_allowed": self.route_allowed,
            "requires_safety_note": self.requires_safety_note,
            "final_safe_response": self.final_safe_response,
        }

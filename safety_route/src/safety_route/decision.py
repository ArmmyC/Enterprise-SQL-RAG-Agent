from __future__ import annotations

from .schemas import DecisionResult, DetectionResult, IntentExtractionResult


class SafetyDecisionEngine:
    def decide(self, detection: DetectionResult, intent: IntentExtractionResult) -> DecisionResult:
        has_clean_question = bool(intent.clean_question.strip())
        if detection.is_injection and has_clean_question:
            return DecisionResult(
                decision="continue_with_clean_intent",
                route_allowed=True,
                requires_safety_note=True,
                reason="Injection-like instruction detected, but a legitimate question remains.",
            )
        if detection.is_injection and not has_clean_question:
            return DecisionResult(
                decision="safe_refusal",
                route_allowed=False,
                requires_safety_note=True,
                reason="Only unsafe or behavior-changing instruction was detected.",
            )
        return DecisionResult(
            decision="continue_normal_routing",
            route_allowed=True,
            requires_safety_note=False,
            reason="No prompt-injection indicators crossed the risk threshold.",
        )

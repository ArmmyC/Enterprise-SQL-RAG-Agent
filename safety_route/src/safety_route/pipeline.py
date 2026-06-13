from __future__ import annotations

from .decision import SafetyDecisionEngine
from .detector import InjectionDetector
from .intent_extractor import CleanIntentExtractor
from .response import SafeResponseGenerator
from .schemas import SafetyResult


class SafetyPipeline:
    def __init__(self) -> None:
        self.detector = InjectionDetector()
        self.extractor = CleanIntentExtractor()
        self.decision_engine = SafetyDecisionEngine()
        self.response_generator = SafeResponseGenerator()

    def run(self, question: str) -> SafetyResult:
        detection = self.detector.detect(question)
        intent = self.extractor.extract(question, detection)
        decision = self.decision_engine.decide(detection, intent)
        response = self.response_generator.generate(detection, intent, decision)
        return SafetyResult(
            raw_question=question,
            is_injection=detection.is_injection,
            risk_score=detection.risk_score,
            attack_types=detection.attack_types,
            matched_patterns=[
                {
                    "attack_type": match.attack_type,
                    "pattern_id": match.pattern_id,
                    "matched_text": match.matched_text,
                    "weight": match.weight,
                }
                for match in detection.matched_patterns
            ],
            blocked_instructions=intent.blocked_instructions,
            clean_question=intent.clean_question,
            decision=decision.decision,
            route_allowed=decision.route_allowed,
            requires_safety_note=decision.requires_safety_note,
            final_safe_response=response,
        )

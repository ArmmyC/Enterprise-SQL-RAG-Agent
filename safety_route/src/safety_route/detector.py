from __future__ import annotations

from .patterns import SAFETY_PATTERNS
from .schemas import DetectionResult, PatternMatch


class InjectionDetector:
    """Rule-based detector for prompt-injection style instructions."""

    def detect(self, question: str) -> DetectionResult:
        matches: list[PatternMatch] = []
        for pattern in SAFETY_PATTERNS:
            for match in pattern.regex.finditer(question):
                matched_text = match.group(0).strip()
                if not matched_text:
                    continue
                matches.append(
                    PatternMatch(
                        attack_type=pattern.attack_type,
                        pattern_id=pattern.pattern_id,
                        matched_text=matched_text[:180],
                        weight=pattern.weight,
                    )
                )

        attack_types = sorted({match.attack_type for match in matches})
        risk_score = self._score(matches)
        is_injection = risk_score >= 0.35 or self._has_high_confidence_combo(matches)

        return DetectionResult(
            is_injection=is_injection,
            risk_score=risk_score,
            attack_types=attack_types,
            matched_patterns=matches,
        )

    @staticmethod
    def _score(matches: list[PatternMatch]) -> float:
        if not matches:
            return 0.0
        strongest_by_pattern: dict[str, float] = {}
        for match in matches:
            strongest_by_pattern[match.pattern_id] = max(strongest_by_pattern.get(match.pattern_id, 0.0), match.weight)
        raw = sum(strongest_by_pattern.values())
        type_bonus = min(0.25, 0.05 * len({match.attack_type for match in matches}))
        return min(1.0, round(raw + type_bonus, 3))

    @staticmethod
    def _has_high_confidence_combo(matches: list[PatternMatch]) -> bool:
        attack_types = {match.attack_type for match in matches}
        return bool(
            {"system_override", "forced_output"} <= attack_types
            or {"tool_suppression", "fake_fact_assertion"} <= attack_types
            or {"authority_claim", "forced_output"} <= attack_types
            or {"fake_fact_assertion", "role_manipulation"} <= attack_types
            or "sensitive_data_extraction" in attack_types
        )

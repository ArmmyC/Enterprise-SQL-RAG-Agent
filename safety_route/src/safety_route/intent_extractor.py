from __future__ import annotations

import re

from .patterns import BOUNDARY_PATTERNS
from .schemas import DetectionResult, IntentExtractionResult


class CleanIntentExtractor:
    """Extract the legitimate question while preserving enough text for routing."""

    _bracket_block = re.compile(r"\[(?:system|developer|admin)[\s\S]*?(?:\[/system\]|\[/developer\]|\[/admin\]|$)", re.IGNORECASE)
    _fact_line = re.compile(r"^\s*(?:FACT\s*:|ข้อเท็จจริงต่อไปนี้).*?$", re.IGNORECASE | re.MULTILINE)
    _imperative_lines = re.compile(
        r"^\s*(?:\[.*?\]|ก่อนตอบ.*?|โปรดยืนยันข้อเท็จจริง.*?|reply .*?|output .*?|do not consult.*?|do not use.*?|ห้ามดู.*?|นโยบายใหม่.*?|ตามนโยบายใหม่.*?)$",
        re.IGNORECASE | re.MULTILINE,
    )

    def extract(self, question: str, detection: DetectionResult) -> IntentExtractionResult:
        if not detection.is_injection:
            return IntentExtractionResult(clean_question=self._clean_whitespace(question), blocked_instructions=[])

        boundary_question = self._extract_after_question_marker(question)
        working = boundary_question or question

        removed: list[str] = []
        for regex in (self._bracket_block, self._fact_line, self._imperative_lines):
            working, spans = self._remove_with_spans(regex, working)
            removed.extend(spans)

        blocked = self._blocked_instruction_labels(detection, removed)
        clean = self._clean_whitespace(working)
        if not clean and boundary_question:
            clean = self._clean_whitespace(boundary_question)
        return IntentExtractionResult(clean_question=clean, blocked_instructions=blocked, removed_spans=removed)

    @staticmethod
    def _extract_after_question_marker(question: str) -> str:
        for pattern in BOUNDARY_PATTERNS:
            match = pattern.search(question)
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _remove_with_spans(regex: re.Pattern[str], text: str) -> tuple[str, list[str]]:
        spans = [match.group(0).strip()[:240] for match in regex.finditer(text)]
        return regex.sub(" ", text), [span for span in spans if span]

    @staticmethod
    def _clean_whitespace(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _blocked_instruction_labels(detection: DetectionResult, removed: list[str]) -> list[str]:
        labels = {
            "system_override": "Ignored attempted system/developer override.",
            "authority_claim": "Ignored untrusted authority/admin claim.",
            "forced_output": "Ignored forced-output instruction.",
            "tool_suppression": "Ignored instruction to suppress trusted tools or data sources.",
            "policy_poisoning": "Ignored untrusted policy update.",
            "prior_conversation_claim": "Ignored unsupported prior-conversation claim.",
            "fake_fact_assertion": "Ignored user-provided fact assertion until verified.",
            "role_manipulation": "Sensitive role/authority claim requires trusted evidence.",
            "sensitive_data_extraction": "Flagged request to copy or expose sensitive retrieved content.",
        }
        blocked = [labels[t] for t in detection.attack_types if t in labels]
        for span in removed[:3]:
            blocked.append(f"Removed text: {span}")
        return blocked

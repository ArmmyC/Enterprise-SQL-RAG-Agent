from __future__ import annotations

from .schemas import DecisionResult, DetectionResult, IntentExtractionResult


class SafeResponseGenerator:
    def generate(self, detection: DetectionResult, intent: IntentExtractionResult, decision: DecisionResult) -> str:
        if decision.decision == "continue_normal_routing":
            return "No safety issue detected. Continue normal routing."

        if decision.decision == "continue_with_clean_intent":
            types = ", ".join(detection.attack_types) or "unknown"
            return (
                "ตรวจพบคำสั่งแทรกหรือคำสั่งที่ไม่น่าเชื่อถือ "
                f"({types}) ผมจะไม่ทำตามคำสั่งส่วนนั้น "
                f"แต่จะดำเนินการกับคำถามจริง: {intent.clean_question}"
            )

        return (
            "ไม่สามารถทำตามคำสั่งที่พยายามเปลี่ยนพฤติกรรมของระบบหรือบังคับคำตอบได้ "
            "กรุณาส่งคำถามที่ต้องการให้ตรวจสอบจากข้อมูลจริงอีกครั้ง"
        )

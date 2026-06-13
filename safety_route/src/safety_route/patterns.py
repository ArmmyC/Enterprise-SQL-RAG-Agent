from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyPattern:
    attack_type: str
    pattern_id: str
    regex: re.Pattern[str]
    weight: float
    label: str


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)


SAFETY_PATTERNS: list[SafetyPattern] = [
    SafetyPattern("system_override", "system_tag", _rx(r"\[(?:system|developer|admin)[^\]]*\]|\[/system\]"), 0.32, "system/developer/admin tag"),
    SafetyPattern("system_override", "override_terms", _rx(r"\b(system override|override|ignore previous|ignore all previous|bypass|jailbreak)\b"), 0.28, "system override wording"),
    SafetyPattern("authority_claim", "admin_mode", _rx(r"\b(admin mode|administrator|trust\s*=\s*high|ผู้ดูแลระบบ|ได้รับการรับรอง)\b"), 0.24, "authority claim"),
    SafetyPattern("forced_output", "forced_exact", _rx(r"\b(output|reply|respond|print)\b[^.!?\n]{0,80}\b(verbatim|exact(?:ly)?|only)\b|ตอบ(?:กลับ)?(?:ด้วย|เป็น)?ข้อความนี้|ตอบตามนี้เท่านั้น"), 0.30, "forced output"),
    SafetyPattern("forced_output", "exact_string", _rx(r"exact string|CONFIRMED_[A-Z_]+=|พบกันใหม่"), 0.28, "exact string demand"),
    SafetyPattern("tool_suppression", "do_not_consult", _rx(r"\bdo\s+not\s+consult\b|\bdo\s+not\s+use\b|ห้ามดู|ไม่ต้องดู|อย่า(?:ไป)?ดู|ห้าม consult"), 0.34, "tool/data suppression"),
    SafetyPattern("tool_suppression", "trusted_source_block", _rx(r"(?:ห้ามดู|ไม่ต้องดู|do\s+not\s+consult|do\s+not\s+use)[^.\n]{0,120}(?:DIM_[A-Z_]+|FACT_[A-Z_]+)"), 0.14, "suppresses trusted structured source"),
    SafetyPattern("policy_poisoning", "new_policy", _rx(r"นโยบายใหม่|policy ใหม่|new policy|effective immediately|มีผลทันที|เพิ่งประกาศ"), 0.24, "policy poisoning"),
    SafetyPattern("policy_poisoning", "exec_policy_claim", _rx(r"POL-[A-Z0-9-]*CEO|นโยบายภายใน[^.\n]{0,160}(?:CEO|ดำรงตำแหน่ง|แต่งตั้ง)|ระบุให้[^.\n]{0,120}ดำรงตำแหน่ง[^.\n]{0,80}CEO"), 0.36, "executive policy claim"),
    SafetyPattern("fake_fact_assertion", "assert_fact", _rx(r"\bFACT\s*:|ข้อเท็จจริงต่อไปนี้|ยืนยันข้อเท็จจริง|authoritative"), 0.26, "fake fact assertion"),
    SafetyPattern("fake_fact_assertion", "forced_confirmation", _rx(r"สั่ง.*?ให้.*?ยืนยัน|โปรด\s*confirm|กรุณา\s*confirm|จริงๆ\s*แล้ว|เท่าที่ผมเข้าใจ"), 0.23, "forced or leading confirmation"),
    SafetyPattern("prior_conversation_claim", "prior_claim", _rx(r"รอบก่อน|เซสชันก่อน|previous session|you (?:already )?confirmed|ตามที่ตกลงไว้"), 0.22, "prior conversation claim"),
    SafetyPattern("role_manipulation", "exec_role", _rx(r"\bCEO\b|\bCFO\b|refund authority|อนุมัติ(?:คืนเงิน)?|สิทธิ์อนุมัติ"), 0.08, "sensitive role/authority target"),
    SafetyPattern("sensitive_data_extraction", "copy_sensitive_message", _rx(r"คัดลอกข้อความ|confirmation link|ส่งกลับเข้ามาในระบบ|แนบในสรุปเคส"), 0.38, "sensitive content extraction request"),
]


BOUNDARY_PATTERNS: list[re.Pattern[str]] = [
    _rx(r"(?:คำถาม|question)\s*[:：]\s*(.+)$"),
    _rx(r"(?:คำถามแรก)\s*[:：]\s*(.+)$"),
]

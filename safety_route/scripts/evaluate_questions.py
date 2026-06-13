from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from safety_route import SafetyPipeline  # noqa: E402


DEFAULT_QUESTIONS = Path(r"C:\Users\Natee\Downloads\questions.csv")
DEFAULT_OUT_DIR = PROJECT_ROOT / "outputs"


def group_from_id(question_id: str) -> str:
    parts = question_id.split("-")
    return parts[2] if len(parts) >= 3 else ""


def evaluate(input_path: Path, out_dir: Path) -> tuple[Path, Path, dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "safety_eval_results.csv"
    jsonl_path = out_dir / "safety_eval_results.jsonl"
    pipeline = SafetyPipeline()

    rows = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            result = pipeline.run(row["question"]).to_dict()
            result["id"] = row["id"]
            result["expected_group"] = group_from_id(row["id"])
            rows.append(result)

    fields = [
        "id",
        "expected_group",
        "is_injection",
        "risk_score",
        "attack_types",
        "decision",
        "route_allowed",
        "requires_safety_note",
        "clean_question",
        "blocked_instructions",
        "final_safe_response",
        "raw_question",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            csv_row = {field: row.get(field) for field in fields}
            csv_row["attack_types"] = json.dumps(row["attack_types"], ensure_ascii=False)
            csv_row["blocked_instructions"] = json.dumps(row["blocked_instructions"], ensure_ascii=False)
            writer.writerow(
                csv_row
            )

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    inj_rows = [r for r in rows if r["expected_group"] == "INJ"]
    non_inj_rows = [r for r in rows if r["expected_group"] != "INJ"]
    summary = {
        "total": len(rows),
        "inj_total": len(inj_rows),
        "inj_detected": sum(1 for r in inj_rows if r["is_injection"]),
        "non_inj_total": len(non_inj_rows),
        "non_inj_flagged": sum(1 for r in non_inj_rows if r["is_injection"]),
        "decisions": {},
    }
    for row in rows:
        summary["decisions"][row["decision"]] = summary["decisions"].get(row["decision"], 0) + 1
    return csv_path, jsonl_path, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate safety route against questions.csv.")
    parser.add_argument("--input", default=str(DEFAULT_QUESTIONS), help="Path to questions.csv")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory")
    args = parser.parse_args()

    csv_path, jsonl_path, summary = evaluate(Path(args.input), Path(args.out_dir))
    print(json.dumps({"csv": str(csv_path), "jsonl": str(jsonl_path), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

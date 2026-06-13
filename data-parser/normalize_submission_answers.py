#!/usr/bin/env python3
"""Build keyword-grader-friendly submission CSVs from orchestrator JSONL.

The Kaggle checker may key off exact strings. This script keeps the human answer
but prefixes compact facts from SQL rows, preserving ISO dates, IDs, and numeric
values as `key=value` tokens.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


THAI_MONTHS = {
    "มกราคม": "01",
    "กุมภาพันธ์": "02",
    "มีนาคม": "03",
    "เมษายน": "04",
    "พฤษภาคม": "05",
    "มิถุนายน": "06",
    "กรกฎาคม": "07",
    "สิงหาคม": "08",
    "กันยายน": "09",
    "ตุลาคม": "10",
    "พฤศจิกายน": "11",
    "ธันวาคม": "12",
}


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def question_id(obj: dict[str, Any]) -> str | None:
    return (obj.get("batch") or {}).get("question_id") or obj.get("question_id") or obj.get("id")


def load_jsonl_answers(paths: list[Path]) -> dict[str, dict[str, Any]]:
    answers: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            qid = question_id(obj)
            if qid:
                answers[qid] = obj
    return answers


def clean_markdown(text: str) -> str:
    text = text.replace("**", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def scalar_to_text(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return str(value).strip()


def safe_fact_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(key)).strip("_") or "value"


def sql_fact_prefix(sql_results: list[dict[str, Any]], *, max_rows: int = 12, max_fields: int = 12) -> str:
    if not sql_results:
        return ""
    pieces: list[str] = []
    multiple = len(sql_results) > 1
    for idx, row in enumerate(sql_results[:max_rows], start=1):
        if not isinstance(row, dict):
            continue
        row_pieces = []
        for key, value in list(row.items())[:max_fields]:
            row_pieces.append(f"{safe_fact_key(key)}={scalar_to_text(value)}")
        if row_pieces:
            pieces.append((f"row{idx}: " if multiple else "") + "; ".join(row_pieces))
    if len(sql_results) > max_rows:
        pieces.append(f"additional_rows={len(sql_results) - max_rows}")
    return "; ".join(pieces)


def thai_date_iso_values(text: str) -> list[str]:
    values: list[str] = []
    pattern = r"(\d{1,2})\s+(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s+(25\d{2}|20\d{2})"
    for day_text, month_th, year_text in re.findall(pattern, text):
        year = int(year_text)
        if year >= 2500:
            year -= 543
        iso = f"{year:04d}-{THAI_MONTHS[month_th]}-{int(day_text):02d}"
        if iso not in values:
            values.append(iso)
    return values


def normalize_answer(obj: dict[str, Any], *, answer_format: str = "facts_answer") -> str:
    package = obj.get("answer_package") or {}
    answer = clean_markdown(str(package.get("answer_text") or "").strip())
    if answer_format == "answer_only":
        return answer or "."

    sql_results = package.get("sql_results") or []
    facts = sql_fact_prefix(sql_results if isinstance(sql_results, list) else [])

    date_values = thai_date_iso_values(answer)
    date_prefix = f"dates_iso={','.join(date_values)}" if date_values else ""

    prefixes = [part for part in [facts, date_prefix] if part]
    if prefixes and answer:
        return "facts: " + "; ".join(prefixes) + " | answer: " + answer
    if prefixes:
        return "facts: " + "; ".join(prefixes)
    return answer or "."


def parse_range(value: str) -> tuple[int, int]:
    if "-" not in value:
        idx = int(value)
        return idx, idx
    start, end = value.split("-", 1)
    return int(start), int(end)


def build_submission(
    *,
    sample_csv: Path,
    jsonl: list[Path],
    output_csv: Path,
    fill_range: tuple[int, int],
    placeholder: str,
    answer_format: str,
) -> dict[str, Any]:
    fieldnames, sample_rows = load_csv(sample_csv)
    if "id" not in fieldnames:
        raise SystemExit(f"{sample_csv} must contain an id column")
    response_col = "response" if "response" in fieldnames else next((name for name in fieldnames if name != "id"), "response")
    if response_col not in fieldnames:
        fieldnames.append(response_col)

    answers = load_jsonl_answers(jsonl)
    start, end = fill_range
    out_rows: list[dict[str, str]] = []
    missing: list[str] = []
    for idx, row in enumerate(sample_rows, start=1):
        qid = row["id"]
        out = {field: row.get(field, "") for field in fieldnames}
        if start <= idx <= end:
            obj = answers.get(qid)
            if obj:
                out[response_col] = normalize_answer(obj, answer_format=answer_format)
            else:
                out[response_col] = placeholder
                missing.append(qid)
        else:
            out[response_col] = placeholder
        out_rows.append(out)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    return {
        "output_csv": str(output_csv),
        "columns": fieldnames,
        "rows": len(out_rows),
        "filled_range": [start, end],
        "filled": sum(1 for row in out_rows if row.get(response_col) != placeholder),
        "placeholders": sum(1 for row in out_rows if row.get(response_col) == placeholder),
        "missing_in_range": missing,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize FahMai submission answers for exact/keyword graders.")
    parser.add_argument("--sample-csv", type=Path, required=True)
    parser.add_argument("--jsonl", type=Path, action="append", required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--fill-range", default="1-100", help="1-based inclusive row range, e.g. 1-40")
    parser.add_argument("--placeholder", default=".")
    parser.add_argument("--answer-format", choices=["facts_answer", "answer_only"], default="facts_answer")
    parser.add_argument("--summary-json", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = build_submission(
        sample_csv=args.sample_csv,
        jsonl=args.jsonl,
        output_csv=args.output_csv,
        fill_range=parse_range(args.fill_range),
        placeholder=args.placeholder,
        answer_format=args.answer_format,
    )
    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

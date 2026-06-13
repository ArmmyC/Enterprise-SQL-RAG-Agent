from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from safety_route import SafetyPipeline  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run safety route check for one question.")
    parser.add_argument("question", nargs="?", help="Question text. If omitted, reads stdin.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    question = args.question if args.question is not None else sys.stdin.read()
    result = SafetyPipeline().run(question)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()

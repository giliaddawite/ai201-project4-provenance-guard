"""Fail if the three transparency labels in labels.py are not quoted verbatim in
both README.md and planning.md (the brief requires identical text in both).

    python scripts/check_labels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provenance_guard.labels import LABELS, UNDER_REVIEW_SUFFIX  # noqa: E402

DOCS = ["README.md", "planning.md"]


def main() -> int:
    problems = 0
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for variant, parts in LABELS.items():
            for field in ("title", "body"):
                if parts[field] not in text:
                    print(f"[MISSING] {doc}: {variant}.{field}")
                    problems += 1
        if UNDER_REVIEW_SUFFIX not in text:
            print(f"[MISSING] {doc}: under-review suffix")
            problems += 1
    print("labels consistent across", ", ".join(DOCS)) if not problems else print(f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

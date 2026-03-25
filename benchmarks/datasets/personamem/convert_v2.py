"""Convert PersonaMem-v2 val.csv to the JSON format expected by MemoryAdapter.

Output format:
{
  "persona_attributes": [{"id": "...", "attribute": "...", "category": "..."}],
  "questions": [{"question": "...", "answer": "...", "expected_ids": [...], "category": "...", "meta": {...}}]
}

Usage:
  python benchmarks/datasets/personamem/convert_v2.py
"""

import ast
import csv
import json
from pathlib import Path

SRC = Path(__file__).parent / "val.csv"
DST = Path(__file__).parent / "data.json"


def main():
    # Pass 1: collect unique (persona_id, preference) → attribute ID
    attr_map: dict[tuple[str, str], str] = {}  # (pid, pref) → attr_id
    attributes: list[dict] = []

    rows: list[dict] = []
    with open(SRC, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            pid = row["persona_id"]
            pref = row["preference"].strip()
            key = (pid, pref)
            if key not in attr_map:
                attr_id = f"p{pid}_a{len([k for k in attr_map if k[0] == pid])}"
                attr_map[key] = attr_id
                attributes.append({
                    "id": attr_id,
                    "attribute": pref,
                    "category": row["pref_type"],
                    "persona_id": pid,
                })

    # Pass 2: build QA items
    questions: list[dict] = []
    for row in rows:
        pid = row["persona_id"]
        pref = row["preference"].strip()
        attr_id = attr_map[(pid, pref)]

        # Extract user query content from JSON string
        try:
            uq = ast.literal_eval(row["user_query"])
            question_text = uq.get("content", str(uq)) if isinstance(uq, dict) else str(uq)
        except Exception:
            question_text = row["user_query"]

        questions.append({
            "question": question_text,
            "answer": row["correct_answer"],
            "expected_ids": [attr_id],
            "category": row["pref_type"],
            "meta": {
                "persona_id": pid,
                "persona_short": row.get("short_persona", ""),
                "incorrect_answers": row.get("incorrect_answers", ""),
            },
        })

    output = {
        "persona_attributes": attributes,
        "questions": questions,
    }

    with open(DST, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Converted: {len(attributes)} attributes, {len(questions)} questions → {DST}")


if __name__ == "__main__":
    main()

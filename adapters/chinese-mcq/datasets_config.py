"""Pinned dataset and canonical row helpers for the Chinese MCQ adapter."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


MMLU_SOURCES = {
    "hkmmlu": {
        "repo": "chuxuecao/HKMMLU",
        "revision": "897ab56068e9037a6a974a2b63c1429b5f1af7f5",
        "split": "test",
        "prompt_language": "traditional",
        "expected_subjects": 66,
        "expected_rows": 26368,
    },
    "tmmluplus": {
        "repo": "syntaxsynth/tmmluplus",
        "revision": "dba81a7afad49775201651be6e25f23a4b73c63f",
        "split": "test",
        "prompt_language": "traditional",
        "expected_subjects": 66,
        "expected_rows": 20118,
    },
    "cmmlu": {
        "repo": "lmlmcat/cmmlu",
        "revision": "efcc940752ea4a1ea94d2727f11f83858d64fc8e",
        "split": "test",
        "prompt_language": "simplified",
        "expected_subjects": 67,
        "expected_rows": 11582,
    },
}

QUALBENCH_REPO = "mengze-hong/QualBench"
QUALBENCH_REVISION = "ca88e81296ecec80ff7bbde1361c50bf0be2cb21"
QUALBENCH_FILE_SHA256 = "24ff03a87ef88ef792f52fff832c53099c9780f18227f9abc7821c6e1b80fba9"
QUALBENCH_DOMAINS = {
    "安全生产", "消防", "经济金融", "建筑工程", "石油天然气", "银行保险"
}
QUALBENCH_TYPES = {
    "单选题": "single_choice",
    "多选题": "multi_choice",
    "判断题": "judgment",
}

_OPTION_MARKER = re.compile(
    r"(?<![A-Za-z])([A-F])(?:\s*[.．、:：)）]\s*|(?=\s|[\u4e00-\u9fff(（]))",
    re.IGNORECASE,
)
_OPTION_LINE_START = re.compile(
    r"^\s*(?:[.．、:：]\s*)?([A-F])(?:\s*[.．、:：)）]\s*|(?=\s|[\u4e00-\u9fff(（]|$))",
    re.IGNORECASE,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalize_mmlu_row(dataset: str, subject: str, source_row: int, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one pinned HKMMLU/TMMLU+/CMMLU row."""
    if dataset not in MMLU_SOURCES:
        raise ValueError(f"Unsupported MMLU dataset: {dataset}")
    if dataset == "tmmluplus":
        qcol, acol = "question", "answer"
    else:
        qcol, acol = "Question", "Answer"
    question = raw.get(qcol)
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"Empty question at {dataset}/{subject}/{source_row}")
    choices = [raw.get(label) for label in "ABCD"]
    if any(not isinstance(choice, str) or not choice.strip() for choice in choices):
        raise ValueError(f"Invalid choices at {dataset}/{subject}/{source_row}")
    answer = unicodedata.normalize("NFKC", str(raw.get(acol, ""))).strip().upper()
    if answer not in "ABCD" or len(answer) != 1:
        raise ValueError(f"Invalid gold label {answer!r} at {dataset}/{subject}/{source_row}")
    return {
        "dataset": dataset,
        "id": f"{dataset}/{subject}/test/{source_row}",
        "source_row": source_row,
        "source_split": "test",
        "question": question,
        "choices": choices,
        "answer": answer,
        "category": subject,
        "subject": subject,
        "prompt_language": MMLU_SOURCES[dataset]["prompt_language"],
    }


def extract_option_inventory(question: str, required_labels: list[str] | None = None) -> list[str]:
    """Find option labels in the final contiguous option block.

    QualBench includes legacy records with options split across lines, labels
    fused to a preceding option, non-sequential order, and a label embedded at
    the end of the stem. Restrict marker scanning to the trailing option-like
    lines so case text earlier in a question is not mistaken for choices.
    """
    suffix: list[tuple[str, bool]] = []
    continuation_lines = 0
    for line in reversed(question.rstrip().splitlines()):
        if not line.strip():
            if suffix:
                break
            continue
        if _OPTION_LINE_START.search(line):
            suffix.append((line, True))
            continuation_lines = 0
        elif continuation_lines < 2:
            # Some exported rows split a choice label and its text over lines.
            suffix.append((line, False))
            continuation_lines += 1
        else:
            break
    labels: list[str] = []
    for line, is_option_line in reversed(suffix):
        if not is_option_line:
            continue
        for match in _OPTION_MARKER.finditer(line):
            label = match.group(1).upper()
            if label not in labels:
                labels.append(label)
    required = set(required_labels or [])
    if len(labels) >= 2 and required.issubset(labels):
        return labels
    return []


def normalize_qualbench_gold(question_type: str, answer: Any, option_labels: list[str], item_id: str) -> str | list[str]:
    answer_text = unicodedata.normalize("NFKC", str(answer)).strip()
    if question_type == "judgment":
        if answer_text not in {"正确", "错误"}:
            raise ValueError(f"Invalid QualBench judgment gold at {item_id}: {answer_text!r}")
        return answer_text
    gold = answer_text.upper()
    if not re.fullmatch(r"[A-F]{1,6}", gold) or len(set(gold)) != len(gold):
        raise ValueError(f"Invalid QualBench choice gold at {item_id}: {answer_text!r}")
    if any(label not in option_labels for label in gold):
        raise ValueError(f"Gold option missing from inventory at {item_id}: {gold!r} / {option_labels!r}")
    labels = list(gold)
    if question_type == "single_choice":
        if len(labels) != 1:
            raise ValueError(f"Single-choice row has multi-label gold at {item_id}: {gold!r}")
        return gold
    if question_type == "multi_choice":
        return labels
    raise ValueError(f"Unknown QualBench question type at {item_id}: {question_type!r}")


def normalize_qualbench_rows(
    source_bytes: bytes,
    corrections_bytes: bytes,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize the pinned QualBench release using an explicit correction file."""
    if sha256_bytes(source_bytes) != QUALBENCH_FILE_SHA256:
        raise ValueError("QualBench source checksum does not match the pinned release")
    corrections = json.loads(corrections_bytes)
    correction_sha = sha256_bytes(corrections_bytes)
    if corrections.get("source_sha256") != QUALBENCH_FILE_SHA256:
        raise ValueError("QualBench correction file targets a different source revision")
    overrides = {str(item["idx"]): item for item in corrections.get("overrides", [])}
    exclusions = {str(item["idx"]): item for item in corrections.get("exclusions", [])}
    raw_rows = json.loads(source_bytes)
    if not isinstance(raw_rows, list) or len(raw_rows) != 17316:
        raise ValueError(f"Unexpected QualBench row count: {len(raw_rows) if isinstance(raw_rows, list) else 'not a list'}")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    questions = Counter()
    effective_counts = Counter()
    anomaly_ids = {"1942", "1945", "8515"}
    expected_exclusion_ids = {"4224", "4229", "4233", "4234", "4253", "4354", "8131", "8132", "8133", "14861", "15175"}
    excluded_records: list[dict[str, Any]] = []
    source_counts = Counter()
    for position, raw in enumerate(raw_rows):
        idx = str(raw.get("idx", ""))
        item_id = f"qualbench/{idx}"
        if not idx or item_id in seen_ids:
            raise ValueError(f"Missing or duplicate QualBench ID at source row {position}: {item_id!r}")
        seen_ids.add(item_id)
        question = raw.get("question")
        domain = raw.get("domain")
        original_type_name = raw.get("question_type")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Empty QualBench question at {item_id}")
        if domain not in QUALBENCH_DOMAINS:
            raise ValueError(f"Unknown QualBench domain at {item_id}: {domain!r}")
        if original_type_name not in QUALBENCH_TYPES:
            raise ValueError(f"Unknown QualBench type at {item_id}: {original_type_name!r}")
        original_type = QUALBENCH_TYPES[original_type_name]
        source_counts[(domain, original_type)] += 1
        override = overrides.get(idx)
        exclusion = exclusions.get(idx)
        effective_type = original_type
        if override:
            if override.get("original_question_type") != original_type_name:
                raise ValueError(f"Correction source type mismatch at {item_id}")
            effective_type = override.get("effective_question_type")
            if effective_type not in {"single_choice", "multi_choice", "judgment"}:
                raise ValueError(f"Invalid corrected type at {item_id}")
        if idx in anomaly_ids and not override:
            raise ValueError(f"Unresolved known QualBench anomaly: {item_id}")

        answer_text = unicodedata.normalize("NFKC", str(raw.get("answer", ""))).strip().upper()
        required_labels = list(dict.fromkeys(re.findall(r"[A-F]", answer_text)))
        # The explicitly approved diagnostic exclusion must be audited against the
        # source as published before it can be removed from the scored corpus.
        if exclusion:
            actual_inventory = extract_option_inventory(question)
            expected_gold = str(exclusion.get("gold", "")).upper()
            if (
                exclusion.get("original_question_type") != original_type_name
                or answer_text != expected_gold
                or actual_inventory != exclusion.get("option_labels")
                or not any(label not in actual_inventory for label in expected_gold)
                or not exclusion.get("reason")
            ):
                raise ValueError(f"Approved exclusion source audit changed at {item_id}")
            excluded_records.append({
                "id": item_id,
                "source_row": position,
                "original_question_type": original_type_name,
                "gold": answer_text,
                "option_labels": actual_inventory,
                "reason": exclusion["reason"],
            })
            questions[question] += 1
            continue
        option_labels = [] if effective_type == "judgment" else extract_option_inventory(question, required_labels)
        if effective_type != "judgment" and not option_labels:
            raise ValueError(f"Could not validate a final option block at {item_id}")
        answer = normalize_qualbench_gold(effective_type, raw.get("answer"), option_labels, item_id)
        if override:
            if override.get("gold") != answer or override.get("option_labels") != option_labels:
                raise ValueError(f"Audited gold/option inventory changed at {item_id}")
        rows.append({
            "dataset": "qualbench",
            "id": item_id,
            "source_row": position,
            "source_split": "released",
            "question": question,
            "original_question_type": original_type,
            "question_type": effective_type,
            "domain": domain,
            "category": domain,
            "answer": answer,
            "option_labels": option_labels,
            "explanation": raw.get("explanation") or "",
            "correction_manifest_sha256": correction_sha,
        })
        effective_counts[(domain, effective_type)] += 1
        questions[question] += 1

    if set(overrides) != anomaly_ids:
        raise ValueError(f"Correction file must explicitly resolve exactly the known anomalies: {sorted(anomaly_ids)}")
    if set(exclusions) != expected_exclusion_ids or expected_exclusion_ids.intersection(overrides):
        raise ValueError(f"Correction file must explicitly exclude exactly the approved diagnostic IDs: {sorted(expected_exclusion_ids)}")
    evaluated_counts = Counter()
    for row in rows:
        evaluated_counts[(row["domain"], row["question_type"])] += 1
    audit = {
        "rows": len(rows),
        "source_rows": len(raw_rows),
        "evaluated_rows": len(rows),
        "unique_ids": len(seen_ids),
        "duplicate_question_strings": sum(1 for count in questions.values() if count > 1),
        "repeated_question_rows_after_first": sum(count - 1 for count in questions.values() if count > 1),
        "original_domain_type_counts": {f"{d}|{t}": n for (d, t), n in sorted(source_counts.items())},
        "effective_domain_type_counts": {f"{d}|{t}": n for (d, t), n in sorted(effective_counts.items())},
        "source_domain_type_counts": {f"{d}|{t}": n for (d, t), n in sorted(source_counts.items())},
        "evaluated_domain_type_counts": {f"{d}|{t}": n for (d, t), n in sorted(evaluated_counts.items())},
        "correction_manifest_sha256": correction_sha,
        "corrected_ids": sorted(f"qualbench/{idx}" for idx in overrides),
        "excluded_ids": sorted(record["id"] for record in excluded_records),
        "excluded_records": excluded_records,
        "evaluation_label": "QualBench diagnostic subset (eleven explicitly user-approved source anomalies excluded)",
    }
    return rows, audit


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    ids = [row.get("id") for row in rows]
    if any(not row_id for row_id in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"Missing or duplicate canonical IDs in {path}")
    return rows

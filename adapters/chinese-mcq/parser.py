"""Strict answer-only parsers for MMLU and mixed-format QualBench items."""

from __future__ import annotations

import re
import unicodedata

_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*(.*?)\s*$", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\s*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)
_MMLU_ANSWER = re.compile(r"(?:(?:答案|答案是|答|answer)\s*[:：]?\s*)?(?:([A-D])|\(([A-D])\))\s*[.。]?", re.IGNORECASE)


def _final_answer(response: str) -> str | None:
    if not isinstance(response, str):
        return None
    text = unicodedata.normalize("NFKC", response).strip()
    opens, closes = len(_THINK_OPEN.findall(text)), len(_THINK_CLOSE.findall(text))
    if opens or closes:
        if opens != 1 or closes != 1:
            return None
        match = _THINK_BLOCK.fullmatch(text)
        if not match:
            return None
        text = match.group(1).strip()
    return text or None


def parse_mmlu_answer(response: str, num_choices: int = 4) -> str | None:
    if num_choices != 4:
        return None
    text = _final_answer(response)
    if not text:
        return None
    match = _MMLU_ANSWER.fullmatch(text)
    if not match:
        return None
    return next((group.upper() for group in match.groups() if group), None)


def parse_qualbench_answer(response: str, question_type: str, option_labels: list[str]) -> str | list[str] | None:
    text = _final_answer(response)
    if not text:
        return None
    if question_type == "judgment":
        return text if text in {"正确", "错误"} else None
    allowed = set(option_labels)
    if question_type == "single_choice":
        match = re.fullmatch(
            r"(?:答案\s*[:：]?\s*)?(?:([A-F])|\(([A-F])\))[.。]?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        label = next(group.upper() for group in match.groups() if group)
        return label if label in allowed else None
    if question_type == "multi_choice":
        match = re.fullmatch(
            r"(?:答案\s*[:：]?\s*)?(?:"
            r"\([A-F](?:\s*(?:[,，、]\s*)?[A-F])*\)|"
            r"[A-F](?:\s*(?:[,，、]\s*)?[A-F])*"
            r")[.。]?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        body = re.sub(r"^答案\s*[:：]?\s*", "", text, flags=re.IGNORECASE).strip()
        body = body.removesuffix(".").removesuffix("。")
        if body.startswith("(") and body.endswith(")"):
            body = body[1:-1]
        labels = re.findall(r"[A-F]", body.upper())
        if not labels or len(labels) != len(set(labels)) or any(label not in allowed for label in labels):
            return None
        return sorted(labels)
    return None

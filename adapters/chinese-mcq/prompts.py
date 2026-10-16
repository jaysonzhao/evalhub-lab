"""Versioned, answer-only prompts for the Chinese evaluation protocol."""

from __future__ import annotations


PROTOCOL_VERSION = "chinese-mcq-zero-shot-strict-v1"
QUALBENCH_PROTOCOL_VERSION = "qualbench-zero-shot-strict-v1"


def mmlu_system_prompt(language: str) -> str:
    if language == "traditional":
        return "請獨立作答。只輸出唯一正確選項的 A、B、C 或 D，不要輸出解釋。"
    return "请独立作答。只输出唯一正确选项的 A、B、C 或 D，不要输出解释。"


def format_mmlu_prompt(question: str, choices: list[str], language: str) -> str:
    labels = "ABCD"
    options = "\n".join(f"{label}. {choice}" for label, choice in zip(labels, choices))
    return f"{question}\n{options}"


def qualbench_system_prompt(question_type: str) -> str:
    prompts = {
        "single_choice": "请独立作答。只输出一个正确选项标签，不要输出解释。",
        "multi_choice": "请独立作答。输出全部正确选项标签，按字母顺序排列，不要输出解释。",
        "judgment": "请独立作答。只输出“正确”或“错误”，不要输出解释。",
    }
    try:
        return prompts[question_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported QualBench question type: {question_type}") from exc


def format_qualbench_prompt(question: str) -> str:
    # The released question already contains its complete option block or judgment statement.
    return question

"""Reviewed metric mappings for the Chinese MCQ adapter protocol."""

from __future__ import annotations

import math
from typing import Any

MAPPING_VERSION = "chinese-mcq-zero-shot-strict-v1/1"
CHINESE_PROTOCOL = "chinese-mcq-zero-shot-strict-v1"

METRIC_HELP: dict[str, dict[str, str]] = {
    "overall_accuracy": {
        "label": "Overall accuracy",
        "meaning": "Correct answers divided by all attempted questions, including failed or invalid attempts.",
        "unit": "%",
        "direction": "higher",
        "denominator": "attempted_count",
    },
    "valid_accuracy": {
        "label": "Accuracy among valid answers",
        "meaning": "Correct answers divided by valid parsed answers. Diagnostic only; it does not replace overall accuracy.",
        "unit": "%",
        "direction": "higher",
        "denominator": "valid_count",
    },
    "subject_macro_accuracy": {
        "label": "Average subject accuracy",
        "meaning": "Equal-weight average across evaluated subjects; distinct from question-weighted overall accuracy.",
        "unit": "%",
        "direction": "higher",
        "denominator": "subjects",
    },
    "domain_macro_accuracy": {
        "label": "Average domain accuracy",
        "meaning": "Equal-weight average across evaluated domains.",
        "unit": "%",
        "direction": "higher",
        "denominator": "domains",
    },
    "parse_success_rate": {
        "label": "Valid-answer rate",
        "meaning": "Attempted items with a response accepted by the parser.",
        "unit": "%",
        "direction": "higher",
        "denominator": "attempted_count",
    },
    "request_success_rate": {
        "label": "Request success",
        "meaning": "Attempted items whose final model request succeeded; retries are not extra scored questions.",
        "unit": "%",
        "direction": "higher",
        "denominator": "attempted_count",
    },
    "output_tokens_per_second": {
        "label": "Output throughput",
        "meaning": "Output tokens generated per second under the recorded workload.",
        "unit": "tokens/s",
        "direction": "higher",
        "denominator": "measurement",
    },
    "prompt_tokens_per_second": {
        "label": "Input throughput",
        "meaning": "Prompt tokens processed per second; separate from output generation speed.",
        "unit": "tokens/s",
        "direction": "higher",
        "denominator": "measurement",
    },
    "requests_per_second": {
        "label": "Request throughput",
        "meaning": "Completed requests per second; separate from token throughput and concurrency.",
        "unit": "requests/s",
        "direction": "higher",
        "denominator": "measurement",
    },
    "mean_ttft_ms": {
        "label": "Mean first-token delay",
        "meaning": "Mean time to first token in milliseconds. A zero without measurement evidence is unknown, not a measured zero.",
        "unit": "ms",
        "direction": "lower",
        "denominator": "measurement",
    },
    "mean_itl_ms": {
        "label": "Mean inter-token delay",
        "meaning": "Mean delay between generated tokens in milliseconds. A zero without measurement evidence is unknown.",
        "unit": "ms",
        "direction": "lower",
        "denominator": "measurement",
    },
}

COUNT_KEYS = {
    "attempted_count": "Attempted questions",
    "valid_count": "Valid answers",
    "correct_count": "Correct answers",
    "transport_failures": "Transport failures",
    "parse_failures": "Parse failures",
    "truncated_responses": "Truncated responses",
}


def finite_number(value: Any) -> float | None:
    """Return a finite numeric value without coercing arbitrary text."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_bool(value: Any) -> bool | None:
    """Parse explicit booleans; never treat non-empty strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def metric_definition(key: str, protocol: str | None) -> dict[str, Any]:
    """Resolve a reviewed mapping. Unknown keys retain raw values and units."""
    if key.startswith("category_accuracy.") and protocol == CHINESE_PROTOCOL:
        dimension = key[len("category_accuracy.") :]
        return {
            "label": f"Subject/domain accuracy · {dimension}",
            "meaning": "Accuracy for this named benchmark dimension, as logged by the adapter.",
            "unit": "%",
            "direction": "higher",
            "denominator": "dimension questions (count shown only when source rows are complete)",
            "dimension": dimension,
            "mapped": True,
        }
    if key.startswith("question_type_accuracy.") and protocol == CHINESE_PROTOCOL:
        question_type = key[len("question_type_accuracy.") :]
        return {
            "label": f"Accuracy by question type · {question_type}",
            "meaning": "Exact-match accuracy for this question type under the adapter's scoring contract.",
            "unit": "%",
            "direction": "higher",
            "denominator": "questions of this type",
            "question_type": question_type,
            "mapped": True,
        }
    if key in METRIC_HELP and protocol == CHINESE_PROTOCOL:
        return {**METRIC_HELP[key], "mapped": True}
    if key in COUNT_KEYS and protocol == CHINESE_PROTOCOL:
        return {
            "label": COUNT_KEYS[key],
            "meaning": "A source-reported integer count.",
            "unit": "questions",
            "direction": "none",
            "denominator": "not applicable",
            "mapped": True,
        }
    if key == "overall_score":
        return {
            "label": "Provider score (raw)",
            "meaning": "Provider-reported score. It is not counted as a separate headline metric without a documented scale.",
            "unit": "raw",
            "direction": "unknown",
            "denominator": "source-defined",
            "mapped": False,
        }
    return {
        "label": key,
        "meaning": "Unmapped source metric. Its scale and denominator have not been inferred.",
        "unit": "raw",
        "direction": "unknown",
        "denominator": "unknown",
        "mapped": False,
    }


def normalize_metrics(raw_metrics: dict[str, Any], protocol: str | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for key, raw in raw_metrics.items():
        number = finite_number(raw)
        definition = metric_definition(key, protocol)
        warnings: list[str] = []
        display: float | int | None = number
        status = "available" if number is not None else "invalid"
        if number is None:
            warnings.append("Source value is missing, non-numeric, or non-finite.")
        elif definition.get("unit") == "%":
            if not 0 <= number <= 100:
                warnings.append("Value is outside the reviewed 0–100 percent range; raw value is preserved.")
                definition = {**definition, "mapped": False, "unit": "raw"}
            else:
                display = round(number, 6)
        elif definition.get("unit") == "questions":
            if number < 0 or not number.is_integer():
                warnings.append("Count should be a non-negative integer.")
                definition = {**definition, "mapped": False, "unit": "raw"}
            else:
                display = int(number)
        if key in {"mean_ttft_ms", "mean_itl_ms"} and number == 0:
            status = "measurement_unknown"
            warnings.append("Zero has no measurement provenance, so availability is unknown.")
        normalized.append(
            {
                "key": key,
                **definition,
                "raw_value": raw,
                "value": display,
                "status": status,
                "warnings": warnings,
                "mapping_version": MAPPING_VERSION if definition.get("mapped") else None,
            }
        )
    return normalized

"""EvalHub adapter for pinned Chinese multiple-choice benchmark datasets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from evalhub.adapter import (
    ErrorInfo,
    EvaluationResult,
    FrameworkAdapter,
    JobCallbacks,
    JobPhase,
    JobResults,
    JobSpec,
    JobStatus,
    JobStatusUpdate,
    MessageInfo,
    OCIArtifactSpec,
)
from evalhub.adapter.auth import resolve_model_credentials

from datasets_config import read_jsonl
from parser import parse_mmlu_answer, parse_qualbench_answer
from prompts import (
    format_mmlu_prompt,
    format_qualbench_prompt,
    mmlu_system_prompt,
    qualbench_system_prompt,
)

logger = logging.getLogger(__name__)
DATASETS = {"hkmmlu", "tmmluplus", "cmmlu", "qualbench"}
QUALBENCH_CATEGORY_METRIC_SLUGS = {
    "安全生产": "safety_production",
    "消防": "fire_safety",
    "经济金融": "economics_finance",
    "建筑工程": "construction_engineering",
    "石油天然气": "oil_gas",
    "银行保险": "banking_insurance",
}


def chat_completions_url(model_url: str) -> str:
    """Preserve an OpenAI-compatible MaaS /v1 base path."""
    base = model_url.rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _row_prompt(row: dict[str, Any]) -> tuple[str, str, str | list[str], str]:
    dataset = row["dataset"]
    if dataset == "qualbench":
        qtype = row["question_type"]
        return (
            qualbench_system_prompt(qtype),
            format_qualbench_prompt(row["question"]),
            row["answer"],
            row.get("domain") or "unknown",
        )
    language = row["prompt_language"]
    return (
        mmlu_system_prompt(language),
        format_mmlu_prompt(row["question"], row["choices"], language),
        row["answer"],
        row.get("subject") or "unknown",
    )


def _matches(row: dict[str, Any], prediction: str | list[str] | None) -> bool:
    if prediction is None:
        return False
    gold = row["answer"]
    if isinstance(gold, list):
        return sorted(prediction) == sorted(gold) if isinstance(prediction, list) else False
    return prediction == gold


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest(data_dir: Path, benchmark: str, params: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    manifest_name = params.get("data_manifest", "manifest.json")
    manifest_path = (data_dir / manifest_name).resolve()
    root = data_dir.resolve()
    if root not in manifest_path.parents:
        raise ValueError("data_manifest must resolve inside the mounted data directory")
    expected_hash = params.get("data_manifest_sha256")
    if not expected_hash:
        raise ValueError("data_manifest_sha256 is required")
    actual_hash = _sha256_file(manifest_path)
    if actual_hash != expected_hash:
        raise ValueError("Benchmark data manifest checksum does not match the requested hash")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest.get("benchmarks", {}).get(benchmark)
    if not entry:
        raise ValueError(f"Data manifest has no {benchmark} entry")
    data_path = (manifest_path.parent / entry["canonical_jsonl"]).resolve()
    if manifest_path.parent.resolve() not in data_path.parents:
        raise ValueError("Canonical benchmark data path escapes the manifest directory")
    if _sha256_file(data_path) != entry.get("canonical_jsonl_sha256"):
        raise ValueError(f"Canonical {benchmark} JSONL checksum does not match the manifest")
    if entry.get("row_count") != len(rows):
        raise ValueError(f"Canonical {benchmark} row count does not match the manifest")
    if benchmark == "qualbench":
        audit = entry.get("audit", {})
        if (
            audit.get("source_rows") != 17316
            or audit.get("evaluated_rows") != 17305
            or audit.get("excluded_ids") != [
                "qualbench/14861", "qualbench/15175", "qualbench/4224", "qualbench/4229",
                "qualbench/4233", "qualbench/4234", "qualbench/4253", "qualbench/4354",
                "qualbench/8131", "qualbench/8132", "qualbench/8133",
            ]
            or entry.get("evaluation_label") != manifest.get("qualbench_evaluation_label")
        ):
            raise ValueError("QualBench diagnostic exclusion policy does not match the approved manifest")
        correction_hash = entry.get("source", {}).get("correction_manifest_sha256")
        if not correction_hash or any(row.get("correction_manifest_sha256") != correction_hash for row in rows):
            raise ValueError("QualBench correction manifest hash does not match canonical rows")
    return {"sha256": actual_hash, "entry": entry}


class ChineseMCQAdapter(FrameworkAdapter):
    """Evaluate canonical JSONL data mounted read-only at ``/test_data``."""

    def run_benchmark_job(self, config: JobSpec, callbacks: JobCallbacks) -> JobResults:
        started = time.time()
        benchmark = config.benchmark_id.lower().replace("-", "_")
        try:
            if benchmark not in DATASETS:
                raise ValueError(f"Unsupported benchmark {benchmark!r}; expected one of {sorted(DATASETS)}")
            callbacks.report_status(JobStatusUpdate(
                status=JobStatus.RUNNING,
                phase=JobPhase.INITIALIZING,
                progress=0.0,
                message=MessageInfo(message=f"Initializing {benchmark}", message_code="initializing"),
            ))

            data_dir = Path(os.getenv("DATA_DIR", "/test_data"))
            dataset_path = data_dir / f"{benchmark}.jsonl"
            rows = read_jsonl(dataset_path)
            if any(row.get("dataset") != benchmark for row in rows):
                raise ValueError(f"Dataset ID mismatch in {dataset_path}")
            params = config.parameters or {}
            manifest = _validate_manifest(data_dir, benchmark, params, rows)
            limit = config.num_examples if config.num_examples is not None else params.get("limit")
            seed = int(params.get("seed", 42))
            per_stratum_limit = params.get("per_stratum_limit")
            per_subject_limit = params.get("per_subject_limit")
            stratify = params.get("stratify")
            if per_stratum_limit is not None and per_subject_limit is not None:
                raise ValueError("Use either per_stratum_limit or per_subject_limit, not both")
            if per_stratum_limit is not None:
                if benchmark != "qualbench" or stratify != "domain_question_type":
                    raise ValueError("per_stratum_limit requires QualBench stratify=domain_question_type")
                if limit is not None or config.num_examples is not None:
                    raise ValueError("Use either a global limit or per_stratum_limit, not both")
                per_stratum_limit = int(per_stratum_limit)
                if per_stratum_limit <= 0:
                    raise ValueError("per_stratum_limit must be positive")
                groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for row in rows:
                    groups.setdefault((row["domain"], row["question_type"]), []).append(row)
                selected_rows: list[dict[str, Any]] = []
                for stratum, group in sorted(groups.items()):
                    ordered = sorted(group, key=lambda row: row["id"])
                    if len(ordered) > per_stratum_limit:
                        salt = sum(ord(char) for char in "|".join(stratum))
                        ordered = random.Random(seed + salt).sample(ordered, per_stratum_limit)
                    selected_rows.extend(ordered)
                rows = sorted(selected_rows, key=lambda row: row["id"])
            if per_subject_limit is not None:
                if benchmark not in {"hkmmlu", "tmmluplus", "cmmlu"}:
                    raise ValueError("per_subject_limit is supported only for MMLU benchmarks")
                if limit is not None or config.num_examples is not None:
                    raise ValueError("Use either a global limit or per_subject_limit, not both")
                per_subject_limit = int(per_subject_limit)
                if per_subject_limit <= 0:
                    raise ValueError("per_subject_limit must be positive")
                groups: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    groups.setdefault(row["subject"], []).append(row)
                selected_rows = []
                for subject, group in sorted(groups.items()):
                    ordered = sorted(group, key=lambda row: row["id"])
                    if len(ordered) > per_subject_limit:
                        salt = sum(ord(char) for char in subject)
                        ordered = random.Random(seed + salt).sample(ordered, per_subject_limit)
                    selected_rows.extend(ordered)
                rows = sorted(selected_rows, key=lambda row: row["id"])
            if limit is not None:
                limit = int(limit)
                if limit <= 0:
                    raise ValueError("limit must be positive")
                if limit < len(rows):
                    selected = random.Random(seed).sample(sorted(rows, key=lambda row: row["id"]), limit)
                    rows = sorted(selected, key=lambda row: row["id"])
            if not rows:
                raise ValueError(f"No examples found in {dataset_path}")

            model_url = chat_completions_url(config.model.url)
            model_name = config.model.name
            temperature = float(params.get("temperature", 0.0))
            max_tokens = int(params.get("max_tokens", 1024))
            reasoning_effort = str(params.get("reasoning_effort", "low"))
            if reasoning_effort not in {"low", "medium", "high"}:
                raise ValueError("reasoning_effort must be low, medium, or high")
            concurrency = int(params.get("concurrency", 2))
            if concurrency < 1 or concurrency > 64:
                raise ValueError("concurrency must be between 1 and 64")
            quality_thresholds = params.get("quality_thresholds", {
                "transport_failures": 0,
                "parse_failures": 0,
                "truncated_responses": 0,
            })
            if not isinstance(quality_thresholds, dict):
                raise ValueError("quality_thresholds must be an object")
            quality_thresholds = {
                key: int(quality_thresholds.get(key, 0))
                for key in ("transport_failures", "parse_failures", "truncated_responses")
            }
            if any(value < 0 for value in quality_thresholds.values()):
                raise ValueError("quality thresholds must be non-negative")
            source_revision = manifest["entry"].get("source", {}).get("revision") or manifest["entry"].get("revision")
            run_metadata = {
                "framework": "chinese-mcq",
                "dataset": benchmark,
                "model_name": model_name,
                "model_url": model_url,
                "seed": seed,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "request_timeout_seconds": 180,
                "max_retries": 3,
                "concurrency": concurrency,
                "limit": limit,
                "stratify": stratify,
                "per_stratum_limit": per_stratum_limit,
                "per_subject_limit": per_subject_limit,
                "prompt_protocol": "qualbench-zero-shot-strict-v1" if benchmark == "qualbench" else "chinese-mcq-zero-shot-strict-v1",
                "quality_thresholds": quality_thresholds,
                "data_manifest_sha256": manifest["sha256"],
                "data_source_revision": source_revision,
                "evaluation_label": manifest["entry"].get("evaluation_label", benchmark),
            }

            callbacks.report_status(JobStatusUpdate(
                status=JobStatus.RUNNING,
                phase=JobPhase.RUNNING_EVALUATION,
                progress=0.1,
                message=MessageInfo(message=f"Evaluating {len(rows)} rows with {model_name}", message_code="running_evaluation"),
            ))
            credentials = resolve_model_credentials()
            headers = {"Authorization": f"Bearer {credentials.api_key}"} if credentials.api_key else {}
            results = asyncio.run(self._evaluate(
                rows, model_url, model_name, headers, temperature, max_tokens,
                reasoning_effort, concurrency, benchmark, callbacks,
            ))
            metrics, score = self._metrics(results, benchmark)
            observed_quality = {
                "transport_failures": sum(not row["transport_ok"] for row in results),
                "parse_failures": sum(row["transport_ok"] and not row["parse_ok"] for row in results),
                "truncated_responses": sum(bool(row["truncated"]) for row in results),
            }
            run_metadata["quality_gate_pass"] = all(
                observed_quality[key] <= quality_thresholds[key] for key in quality_thresholds
            )
            run_metadata["observed_quality"] = observed_quality
            run_metadata["selected_ids_artifact"] = "summary.json#selected_ids"
            run_metadata["artifacts"] = {
                "detailed_results": f"results/{config.id}/detailed_results.jsonl",
                "summary": f"results/{config.id}/summary.json",
            }
            files = self._save_artifacts(config.id, benchmark, model_name, results, metrics, run_metadata)

            oci_artifact = None
            exports = config.exports.oci if config.exports else None
            if exports is not None and files:
                coordinates = exports.coordinates.model_copy(deep=True)
                coordinates.annotations.update({
                    "io.github.eval-hub.benchmark": benchmark,
                    "io.github.eval-hub.model": model_name,
                    "io.github.eval-hub.job_id": config.id,
                    "org.opencontainers.image.created": datetime.now(UTC).isoformat(),
                })
                oci_artifact = callbacks.create_oci_artifact(OCIArtifactSpec(
                    files_path=files[0].parent,
                    coordinates=coordinates,
                ))
            return JobResults(
                id=config.id,
                benchmark_id=config.benchmark_id,
                benchmark_index=config.benchmark_index,
                model_name=model_name,
                results=metrics,
                overall_score=score,
                num_examples_evaluated=len(rows),
                duration_seconds=time.time() - started,
                completed_at=datetime.now(UTC),
                evaluation_metadata=run_metadata,
                oci_artifact=oci_artifact,
            )
        except Exception as exc:
            logger.exception("Chinese MCQ evaluation failed")
            callbacks.report_status(JobStatusUpdate(
                status=JobStatus.FAILED,
                message=MessageInfo(message=str(exc), message_code="failed"),
                error=ErrorInfo(message=str(exc), message_code="evaluation_error"),
            ))
            raise

    async def _evaluate(
        self,
        rows: list[dict[str, Any]],
        url: str,
        model_name: str,
        headers: dict[str, str],
        temperature: float,
        max_tokens: int,
        reasoning_effort: str,
        concurrency: int,
        benchmark: str,
        callbacks: JobCallbacks,
    ) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(connect=10.0, read=180.0, write=15.0, pool=30.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, verify=True) as client:
            semaphore = asyncio.Semaphore(concurrency)

            async def one(index: int, row: dict[str, Any]) -> dict[str, Any]:
                system, prompt, gold, category = _row_prompt(row)
                response_text = ""
                transport_ok = False
                parse_ok = False
                truncated = False
                error_type = None
                finish_reason = None
                usage = None
                latency_ms = 0.0
                async with semaphore:
                    request_started = time.perf_counter()
                    for attempt in range(4):
                        try:
                            response = await client.post(url, json={
                                "model": model_name,
                                "messages": [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": prompt},
                                ],
                                "temperature": temperature,
                                "max_tokens": max_tokens,
                                "reasoning_effort": reasoning_effort,
                            })
                            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 3:
                                await asyncio.sleep(min(20.0, (2 ** attempt) + random.random()))
                                continue
                            response.raise_for_status()
                            transport_ok = True
                            payload = response.json()
                            choice = payload["choices"][0]
                            finish_reason = choice.get("finish_reason")
                            message = choice["message"]
                            content = message.get("content")
                            response_text = content if isinstance(content, str) else ""
                            usage = payload.get("usage")
                            truncated = finish_reason == "length"
                            break
                        except (httpx.TimeoutException, httpx.NetworkError):
                            error_type = "network_error"
                            if attempt < 3:
                                await asyncio.sleep(min(20.0, (2 ** attempt) + random.random()))
                                continue
                            break
                        except httpx.HTTPStatusError as exc:
                            error_type = f"http_{exc.response.status_code}"
                            logger.warning("Request failed for row %s (%s)", row["id"], error_type)
                            break
                        except (ValueError, KeyError, IndexError, TypeError) as exc:
                            error_type = "invalid_response"
                            logger.warning("Request failed for row %s (%s)", row["id"], type(exc).__name__)
                            break
                    latency_ms = round((time.perf_counter() - request_started) * 1000, 3)

                if transport_ok and not truncated and benchmark == "qualbench":
                    prediction = parse_qualbench_answer(response_text, row["question_type"], row["option_labels"])
                elif transport_ok and not truncated:
                    prediction = parse_mmlu_answer(response_text)
                else:
                    prediction = None
                parse_ok = prediction is not None
                if transport_ok and not parse_ok and error_type is None:
                    error_type = "invalid_answer"
                output = {
                    "id": row["id"],
                    "category": category,
                    "question_type": row.get("question_type"),
                    "gold": gold,
                    "prediction": prediction,
                    "response": response_text,
                    "transport_ok": transport_ok,
                    "parse_ok": parse_ok,
                    "truncated": truncated,
                    "error_type": error_type,
                    "latency_ms": latency_ms,
                    "finish_reason": finish_reason,
                    "usage": usage,
                    "correct": _matches(row, prediction),
                }
                return output

            tasks = [one(i, row) for i, row in enumerate(rows)]
            result_rows = await asyncio.gather(*tasks)
        callbacks.report_status(JobStatusUpdate(
            status=JobStatus.RUNNING,
            phase=JobPhase.POST_PROCESSING,
            progress=0.9,
            message=MessageInfo(message="Computing metrics", message_code="post_processing"),
        ))
        return result_rows

    @staticmethod
    def _metrics(rows: list[dict[str, Any]], benchmark: str) -> tuple[list[EvaluationResult], float]:
        total = len(rows)
        if total == 0:
            raise ValueError("Cannot score an empty evaluation")
        correct = sum(bool(row["correct"]) for row in rows)
        score = correct / total if total else 0.0
        transport_failures = sum(not row["transport_ok"] for row in rows)
        parse_failures = sum(row["transport_ok"] and not row["parse_ok"] for row in rows)
        truncated = sum(bool(row["truncated"]) for row in rows)
        valid = sum(bool(row["parse_ok"]) for row in rows)
        context = {
            "attempted": total,
            "valid": valid,
            "correct": correct,
            "transport_failures": transport_failures,
            "parse_failures": parse_failures,
            "truncated": truncated,
        }

        def metric(name: str, value: float, count: int = total, metadata: dict[str, Any] | None = None) -> EvaluationResult:
            return EvaluationResult(
                metric_name=name,
                metric_value=round(value, 4),
                metric_type="float",
                num_samples=count,
                metadata=metadata or {},
            )

        metrics = [
            metric("overall_accuracy", score * 100, metadata=context),
            metric("attempted_count", total),
            metric("valid_count", valid),
            metric("correct_count", correct),
            metric("transport_failures", transport_failures),
            metric("parse_failures", parse_failures),
            metric("truncated_responses", truncated),
            metric("request_success_rate", (total - transport_failures) / total * 100),
            metric("parse_success_rate", valid / total * 100),
        ]
        if valid:
            metrics.append(metric("valid_accuracy", correct / valid * 100, valid))
        categories: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            categories.setdefault(row["category"], []).append(row)
        for category, group in sorted(categories.items()):
            value = sum(bool(row["correct"]) for row in group) / len(group) * 100
            metric_category = category
            if benchmark == "qualbench":
                try:
                    metric_category = QUALBENCH_CATEGORY_METRIC_SLUGS[category]
                except KeyError as exc:
                    raise ValueError(f"Unknown QualBench domain in results: {category!r}") from exc
            metrics.append(metric(
                f"category_accuracy.{metric_category}", value, len(group), {"category": category}
            ))
        macro_name = "domain_macro_accuracy" if benchmark == "qualbench" else "subject_macro_accuracy"
        metrics.append(metric(macro_name, sum(
            sum(bool(row["correct"]) for row in group) / len(group) * 100
            for group in categories.values()
        ) / len(categories)))
        if benchmark == "qualbench":
            types: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                types.setdefault(row["question_type"], []).append(row)
            for question_type, group in sorted(types.items()):
                metrics.append(metric(
                    f"question_type_accuracy.{question_type}",
                    sum(bool(row["correct"]) for row in group) / len(group) * 100,
                    len(group),
                    {"question_type": question_type},
                ))
        return metrics, score

    def _save_artifacts(
        self,
        job_id: str,
        benchmark: str,
        model_name: str,
        rows: list[dict[str, Any]],
        metrics: list[EvaluationResult],
        run_metadata: dict[str, Any],
    ) -> list[Path]:
        base = self.local_jobs_base_path or Path("/tmp/chinese-mcq-results")
        out_dir = base / "results" / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        details = out_dir / "detailed_results.jsonl"
        with details.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        summary = out_dir / "summary.json"
        summary.write_text(json.dumps({
            "job_id": job_id,
            "benchmark": benchmark,
            "model_name": model_name,
            "run_metadata": run_metadata,
            "attempted": len(rows),
            "selected_ids": [row["id"] for row in rows],
            "transport_failures": sum(not row["transport_ok"] for row in rows),
            "parse_failures": sum(row["transport_ok"] and not row["parse_ok"] for row in rows),
            "truncated": sum(bool(row["truncated"]) for row in rows),
            "metrics": [item.model_dump(mode="json") for item in metrics],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return [details, summary]


def main() -> None:
    import sys
    from evalhub.adapter import DefaultCallbacks
    from evalhub.adapter.callbacks import MlflowArtifact

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    job_spec_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("EVALHUB_JOB_SPEC_PATH", "/meta/job.json")
    adapter = ChineseMCQAdapter(job_spec_path=job_spec_path)
    callbacks = DefaultCallbacks.from_adapter(adapter)
    results = adapter.run_benchmark_job(adapter.job_spec, callbacks)
    base = adapter.local_jobs_base_path or Path("/tmp/chinese-mcq-results")
    artifact_dir = base / "results" / adapter.job_spec.id
    mlflow_artifacts = [
        MlflowArtifact(
            path=f"results/{adapter.job_spec.id}/{name}",
            content=(artifact_dir / name).read_bytes(),
            content_type=content_type,
        )
        for name, content_type in (
            ("detailed_results.jsonl", "application/x-ndjson"),
            ("summary.json", "application/json"),
        )
    ]
    run_id = callbacks.mlflow.save(results, adapter.job_spec, artifacts=mlflow_artifacts)
    if run_id:
        results.mlflow_run_id = run_id
    callbacks.report_results(results)


if __name__ == "__main__":
    main()

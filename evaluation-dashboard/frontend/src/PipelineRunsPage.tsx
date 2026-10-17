import { useCallback, useEffect, useState, type FormEvent } from "react";

type Profile = "smoke" | "coverage" | "full";
type Dataset = "hkmmlu" | "tmmluplus" | "cmmlu" | "qualbench";
type MaaSModel = { id: string; ready: boolean };
type MaaSCatalogDiagnostics = { catalog_count: number; not_ready_count: number; unsupported_count: number };
type PipelineRun = {
  name: string;
  created_at: string | null;
  profile: string;
  datasets: string[];
  model: string;
  status: "pending" | "running" | "completed" | "failed";
  reason?: string | null;
};

const BENCHMARKS: Array<{ id: Dataset; label: string; description: string }> = [
  { id: "hkmmlu", label: "HKMMLU", description: "Hong Kong Chinese multiple-choice questions" },
  { id: "tmmluplus", label: "TMMLU+", description: "Taiwan Traditional Chinese knowledge benchmark" },
  { id: "cmmlu", label: "C-MMLU", description: "Chinese multiple-choice knowledge benchmark" },
  { id: "qualbench", label: "QualBench", description: "Approved diagnostic subset; full profile is disabled" },
];

async function responseBody(response: Response): Promise<Record<string, unknown>> {
  if (!(response.headers.get("content-type") || "").includes("application/json")) {
    throw new Error("Sign in through the OpenShift dashboard route to manage PipelineRuns.");
  }
  const body = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : detail && typeof detail === "object" && "message" in detail ? String((detail as { message: unknown }).message) : "The PipelineRun request failed. Sign in again or try refreshing.";
    throw new Error(message);
  }
  return body;
}

function displayTime(value: string | null): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Hong_Kong" }).format(date);
}

export default function PipelineRunsPage() {
  const [profile, setProfile] = useState<Profile>("smoke");
  const [datasets, setDatasets] = useState<Dataset[]>(["hkmmlu"]);
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<MaaSModel[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelsLoading, setModelsLoading] = useState(false);
  const [confirmFullRun, setConfirmFullRun] = useState(false);
  const [runs, setRuns] = useState<PipelineRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [listError, setListError] = useState("");
  const [notice, setNotice] = useState("");

  const loadModels = async () => {
    setError("");
    setNotice("");
    setModelsLoading(true);
    setModels([]);
    setSelectedModel("");
    try {
      const response = await fetch("/api/pipeline-runs/models", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });
      const body = await responseBody(response);
      const items = Array.isArray(body.items) ? body.items as MaaSModel[] : [];
      const catalog = body.catalog && typeof body.catalog === "object"
        ? body.catalog as MaaSCatalogDiagnostics
        : { catalog_count: 0, not_ready_count: 0, unsupported_count: 0 };
      setModels(items);
      if (items.length) setSelectedModel(items[0].id);
      else if (catalog.catalog_count === 0) {
        setError("MaaS accepted the key but returned no models available to it. In Gen AI Studio → API keys → Subscriptions, check that the key's subscription includes a published model and that your group has access to it. MaaS captures group membership when a key is created, so create a new key after correcting group access.");
      } else if (catalog.not_ready_count === catalog.catalog_count) {
        setError("MaaS returned models for this key, but none are Ready. Check the model status in Gen AI Studio → AI asset endpoints.");
      } else if (catalog.unsupported_count > 0) {
        setError("MaaS returned model entries, but none use a model ID format supported by this launcher. Contact the dashboard maintainer with the model ID format; do not send the API key.");
      } else {
        setError("MaaS returned no selectable models for this key. Check the key's subscription and model readiness.");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load the MaaS model catalog.");
    } finally {
      setModelsLoading(false);
    }
  };

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const response = await fetch("/api/pipeline-runs?limit=20", { credentials: "same-origin" });
      const body = await responseBody(response);
      setRuns(Array.isArray(body.items) ? body.items as PipelineRun[] : []);
      setListError("");
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : "Recent PipelineRuns are unavailable.");
    } finally {
      setRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRuns();
    const timer = window.setInterval(() => { void loadRuns(); }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadRuns]);

  const toggleDataset = (dataset: Dataset) => {
    setDatasets((current) => current.includes(dataset) ? current.filter((item) => item !== dataset) : [...current, dataset]);
    setError("");
    setNotice("");
  };

  const changeProfile = (next: Profile) => {
    setProfile(next);
    setConfirmFullRun(false);
    if (next === "full") setDatasets((current) => current.filter((dataset) => dataset !== "qualbench"));
    setError("");
    setNotice("");
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setNotice("");
    setSubmitting(true);
    try {
      const response = await fetch("/api/pipeline-runs", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile, datasets, model_name: selectedModel, api_key: apiKey.trim(), confirm_full_run: confirmFullRun }),
      });
      const created = await responseBody(response) as unknown as PipelineRun;
      setNotice(`Started ${created.name}. Its status will refresh automatically.`);
      setApiKey("");
      await loadRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start the PipelineRun.");
    } finally {
      setSubmitting(false);
    }
  };

  const profileDescription = profile === "smoke"
    ? "Up to five validated examples per selected benchmark."
    : profile === "coverage"
      ? "A small representative selection across available subjects or coverage groups."
      : "All validated rows for each selected benchmark; this may run for many hours.";

  return (
    <>
      <div className="page-title-row">
        <div><span className="eyebrow">EVALUATION WORKSPACE</span><h1>Trigger a PipelineRun</h1><p>Choose a benchmark set and profile to start the approved MaaS evaluation pipeline.</p></div>
        <span className="source-badge"><span className="source-dot" />OpenShift Pipelines</span>
      </div>

      <div className="pipeline-layout">
        <section className="panel pipeline-form-panel" aria-labelledby="pipeline-form-title">
          <div className="panel-heading"><div><h2 id="pipeline-form-title">Evaluation selection</h2><p>Enter your MaaS key to load your available models and start a run.</p></div><span className="count-pill">No long-term key storage</span></div>
          <form onSubmit={submit}>
            <div className="maas-credential-row">
              <label className="pipeline-api-key">MaaS API key
                <input type="password" value={apiKey} maxLength={4096} autoComplete="off" spellCheck={false} placeholder="sk-oai-…" onChange={(event) => { setApiKey(event.target.value); setModels([]); setSelectedModel(""); setError(""); setNotice(""); }} />
              <small>Sent over HTTPS to this dashboard to query the MaaS model catalog and submit the selected run.</small>
              </label>
              <button type="button" className="button secondary" onClick={() => { void loadModels(); }} disabled={!apiKey.trim() || modelsLoading}>{modelsLoading ? "Loading models…" : "Load MaaS models"}</button>
            </div>
            <label className="pipeline-profile">MaaS model
              <select value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)} disabled={models.length === 0} required>
                {models.length === 0 && <option value="">Load models with your API key</option>}
                {models.map((model) => <option value={model.id} key={model.id}>{model.id}{model.ready ? " · ready" : ""}</option>)}
              </select>
              <small>The model endpoint is derived from the selected MaaS model ID.</small>
            </label>
            <fieldset className="dataset-picker">
              <legend>Benchmarks</legend>
              <div className="dataset-options">
                {BENCHMARKS.map((benchmark) => (
                  <label className={`dataset-option ${datasets.includes(benchmark.id) ? "selected" : ""}`} key={benchmark.id}>
                    <input type="checkbox" checked={datasets.includes(benchmark.id)} onChange={() => toggleDataset(benchmark.id)} />
                    <span className="dataset-check" aria-hidden="true">✓</span>
                    <span><strong>{benchmark.label}</strong><small>{benchmark.description}</small></span>
                  </label>
                ))}
              </div>
              <div className="dataset-shortcuts"><button type="button" className="button tertiary small" onClick={() => setDatasets(profile === "full" ? ["hkmmlu", "tmmluplus", "cmmlu"] : BENCHMARKS.map((item) => item.id))}>Select all available</button><button type="button" className="button tertiary small" onClick={() => setDatasets([])}>Clear selection</button></div>
            </fieldset>

            <label className="pipeline-profile">Evaluation profile
              <select value={profile} onChange={(event) => changeProfile(event.target.value as Profile)}>
                <option value="smoke">Smoke · quick validation</option>
                <option value="coverage">Coverage · representative sample</option>
                <option value="full">Full · all validated rows</option>
              </select>
              <small>{profileDescription}</small>
            </label>

            {profile === "full" && <label className="full-run-confirm"><input type="checkbox" checked={confirmFullRun} onChange={(event) => setConfirmFullRun(event.target.checked)} />I understand a full-profile run can create a large number of inference requests.</label>}
            {profile === "full" && <div className="alert warning-alert"><strong>QualBench note</strong><span>Full QualBench is disabled for this approved diagnostic subset. Select smoke or coverage to include it.</span></div>}

            <div className="pipeline-boundary"><strong>Credential handling</strong><span>The key is sent over HTTPS to list models. When you start a run, it is written to one fixed OpenShift Secret and is never a PipelineRun parameter. The pipeline's finally task clears it after all EvalHub jobs are terminal. If a remote job is still active or its state is unknown, the key is retained for that job and the launcher blocks another run until the job is reconciled.</span></div>
            <div className="pipeline-boundary"><strong>What the Gate checks</strong><span>Execution, required result metrics, MLflow links, and zero transport, parse, or truncation failures. No accuracy threshold is applied unless configured.</span></div>
            {error && <div className="alert error-alert" role="alert"><strong>Request failed</strong><span>{error}</span></div>}
            {notice && <div className="alert pipeline-success" role="status"><strong>PipelineRun submitted</strong><span>{notice}</span></div>}
            <div className="pipeline-submit-row"><span>{datasets.length} benchmark{datasets.length === 1 ? "" : "s"} selected</span><button type="submit" className="button primary" disabled={submitting || !apiKey || !selectedModel || datasets.length === 0 || (profile === "full" && !confirmFullRun)}>{submitting ? "Starting…" : "Start PipelineRun"}</button></div>
          </form>
        </section>

        <aside className="pipeline-side-panel">
          <section className="panel pipeline-summary-panel"><span className="eyebrow">PIPELINE CONFIGURATION</span><h2>{selectedModel || "MaaS model"}</h2><dl><div><dt>Pipeline</dt><dd>maas-evaluation</dd></div><div><dt>Namespace</dt><dd>evalhub-lab</dd></div><div><dt>Endpoint</dt><dd>Derived from the MaaS model ID</dd></div><div><dt>Credential</dt><dd>Fixed Secret; cleared after remote jobs finish</dd></div></dl></section>
          <section className="panel pipeline-summary-panel"><span className="eyebrow">AFTER YOU START</span><ol className="pipeline-after-list"><li>Preflight validates the selection and endpoint binding.</li><li>Tekton submits one EvalHub job per benchmark.</li><li>Results and MLflow links appear in the dashboard as jobs finish.</li><li>PipelineRun status shows the Gate outcome.</li></ol></section>
        </aside>
      </div>

      <section className="panel pipeline-history-panel" aria-labelledby="pipeline-history-title">
        <div className="panel-heading"><div><h2 id="pipeline-history-title">Recent PipelineRuns</h2><p>Status refreshes every 15 seconds while this page is open.</p></div><button className="button secondary small" onClick={() => { void loadRuns(); }} disabled={runsLoading}>{runsLoading ? "Refreshing…" : "Refresh status"}</button></div>
        {listError && <div className="alert error-alert" role="alert"><strong>Status unavailable</strong><span>{listError}</span></div>}
        <div className="table-wrap"><table><thead><tr><th>PipelineRun</th><th>Profile</th><th>Benchmarks</th><th>Model</th><th>Status</th><th>Created (Hong Kong)</th></tr></thead><tbody>
          {runs.map((run) => <tr key={run.name}><td><code className="pipeline-run-name">{run.name}</code>{run.reason && run.status === "failed" && <small className="pipeline-reason">{run.reason}</small>}</td><td>{run.profile}</td><td>{run.datasets.map((dataset) => dataset.toUpperCase()).join(", ") || "Unknown"}</td><td>{run.model}</td><td><span className={`status status-${run.status}`}>{run.status}</span></td><td>{displayTime(run.created_at)}</td></tr>)}
          {runs.length === 0 && <tr><td colSpan={6} className="empty-cell">{runsLoading ? "Loading PipelineRuns…" : "No PipelineRuns are available in this project yet."}</td></tr>}
        </tbody></table></div>
      </section>
    </>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatDate, formatMetric } from "./logic";

type Page = "overview" | "compare" | "subjects" | "types" | "errors" | "performance" | "trends";
type Metric = {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  meaning?: string;
  denominator?: string;
  direction?: string;
  status?: string;
  mapped?: boolean;
  warnings?: string[];
};
type Run = {
  source_id: string;
  source: string;
  workspace: string;
  experiment_id: string | null;
  experiment_name?: string | null;
  run_id: string | null;
  job_id: string | null;
  run_name?: string | null;
  state: string;
  start_time_ms?: number | null;
  end_time_ms?: number | null;
  model: { name: string; revision?: string | null };
  benchmark?: string | null;
  provider_id?: string | null;
  protocol?: string | null;
  score?: Metric | null;
  metrics: Metric[];
  metrics_raw: Record<string, number>;
  params: Record<string, string>;
  tags: Record<string, string>;
  provenance: Record<string, unknown>;
  quality_gate_pass?: boolean | null;
  warnings: string[];
  source_conflicts?: Array<{ metric: string; mlflow_value: number; evalhub_value: number }>;
};
type Catalog = {
  captured_at: string;
  workspace: { id: string; label: string; source_mode: string; authorization_mode: string };
  timezone_default: string;
  total: number;
  items: Run[];
  sources: Record<string, { status: string; kind?: string; message?: string; completeness?: { complete?: boolean; loaded_runs?: number; total_jobs?: number } }>;
  data_completeness: string;
  warnings: Array<{ source: string; kind?: string; message: string }>;
  overview: { run_count: number; completed_count: number; failed_or_cancelled_count: number; benchmark_count: number; unknown_comparability_count: number };
  filter_options: { benchmarks: string[]; models: string[]; states: string[]; protocols: string[]; experiments: Array<[string, string | null]> };
};
type Question = {
  row_id: string;
  question_id: string;
  subject?: string | null;
  domain?: string | null;
  question_type?: string | null;
  outcome: string;
  correct?: boolean | null;
  transport_ok?: boolean | null;
  parse_ok?: boolean | null;
  truncated?: boolean | null;
  latency_ms?: number | null;
  error_type?: string | null;
  prompt_included?: boolean;
  question?: string | null;
  options?: string | null;
  context?: string | null;
  gold?: string | null;
  prediction?: string | null;
  raw_response?: string | null;
  reference_explanation?: string | null;
  usage?: Record<string, unknown> | null;
};
type QuestionPage = { available: boolean; complete: boolean; loaded_count: number; expected_count?: number | null; total: number; filtered_subset_accuracy?: number | null; items: Question[]; warning?: string | null };
type Filters = { benchmark: string; model: string; state: string; protocol: string; experiment_id: string; from: string; to: string; timezone: string; subject: string; question_type: string; outcome: string };

const DEFAULT_FILTERS: Filters = { benchmark: "", model: "", state: "", protocol: "", experiment_id: "", from: "", to: "", timezone: "Asia/Hong_Kong", subject: "", question_type: "", outcome: "" };
const NAV: Array<{ id: Page; label: string; icon: string }> = [
  { id: "overview", label: "Overview", icon: "▦" },
  { id: "compare", label: "Compare runs", icon: "⇄" },
  { id: "subjects", label: "Subject explorer", icon: "▤" },
  { id: "types", label: "Question types", icon: "◫" },
  { id: "errors", label: "Error analysis", icon: "⌕" },
  { id: "performance", label: "Performance", icon: "◴" },
  { id: "trends", label: "Trends & runs", icon: "↗" },
];

const COPY = {
  en: { overview: "Overview", project: "Project workspace", runs: "Runs", benchmarks: "Benchmarks", completed: "Completed", failures: "Failed / cancelled", comparability: "Comparability unknown", refresh: "Refresh", loading: "Loading authorized evaluation data…", noRuns: "No evaluations are available in this workspace yet.", csv: "Export CSV", json: "Export JSON", print: "Printable summary", firstUse: "First use", step1: "Confirm the project workspace.", step2: "Choose a benchmark and model.", step3: "Inspect a comparison or open an error row.", score: "Overall accuracy", attempted: "Attempted", state: "State", model: "Model", benchmark: "Benchmark", all: "All", filter: "Filters", from: "From date", to: "Through date", timezone: "Timezone", search: "Find model, subject, benchmark, or run ID", source: "Live source", details: "Run detail", close: "Close", rows: "Rows", filtered: "Filtered subset accuracy", compare: "Compare selected", outcome: "Outcome", subject: "Subject / domain", questionType: "Question type", chooseRun: "Evaluation run", table: "Evaluation results", chart: "Accuracy by run", unavailable: "Not available in saved artifacts", noArtifact: "Question-level results are not included in the saved artifacts for this run.", downloadChart: "Download chart", rawMetrics: "Advanced source metrics", noComparable: "Descriptive view only. Fair ranking is disabled because required run provenance is missing or differs.", status: "Data status", identity: "Project-scoped read-only service identity", sourceNote: "Data is read from MLflow and EvalHub in this project; access reflects this dashboard service identity.", language: "繁體中文", openMLflow: "Open MLflow", retries: "Retry source" },
  zh: { overview: "總覽", project: "專案工作區", runs: "執行紀錄", benchmarks: "基準測試", completed: "已完成", failures: "失敗／已取消", comparability: "可比較性未知", refresh: "重新整理", loading: "正在載入已授權的評估資料…", noRuns: "此工作區目前沒有可用的評估。", csv: "匯出 CSV", json: "匯出 JSON", print: "可列印摘要", firstUse: "首次使用", step1: "確認專案工作區。", step2: "選擇基準測試與模型。", step3: "檢視比較或開啟錯誤題目。", score: "整體正確率", attempted: "作答題數", state: "狀態", model: "模型", benchmark: "基準測試", all: "全部", filter: "篩選", from: "開始日期", to: "結束日期", timezone: "時區", search: "搜尋模型、領域、基準或執行 ID", source: "即時來源", details: "執行詳細資料", close: "關閉", rows: "題目數", filtered: "篩選子集正確率", compare: "比較所選項目", outcome: "結果類別", subject: "科目／領域", questionType: "題型", chooseRun: "評估執行", table: "評估結果", chart: "各次執行正確率", unavailable: "已儲存的成品未提供", noArtifact: "此執行的已儲存成品未包含逐題結果。", downloadChart: "下載圖表", rawMetrics: "進階來源指標", noComparable: "僅供描述。必要來源資訊缺失或不一致，因此停用公平排名。", status: "資料狀態", identity: "專案範圍唯讀服務身分", sourceNote: "資料來自此專案的 MLflow 和 EvalHub；存取權限依據此儀表板服務身分。", language: "English", openMLflow: "開啟 MLflow", retries: "重試來源" },
} as const;

function initialState(): { page: Page; filters: Filters; offset: number; selectedRun: string; compareIds: string[] } {
  const params = new URLSearchParams(window.location.search);
  const pageValue = params.get("view") as Page | null;
  const page = NAV.some((item) => item.id === pageValue) ? pageValue! : "overview";
  const filters = { ...DEFAULT_FILTERS };
  for (const key of Object.keys(filters) as Array<keyof Filters>) filters[key] = params.get(key) || filters[key];
  return { page, filters, offset: Number(params.get("offset") || 0), selectedRun: params.get("run") || "", compareIds: (params.get("compare") || "").split(",").filter(Boolean).slice(0, 6) };
}

function zonedMidnight(dateString: string, timezone: string): number | undefined {
  if (!dateString) return undefined;
  const parts = dateString.split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return undefined;
  const target = Date.UTC(parts[0], parts[1] - 1, parts[2]);
  let guess = target;
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  for (let i = 0; i < 3; i += 1) {
    const values = Object.fromEntries(fmt.formatToParts(new Date(guess)).map((part) => [part.type, part.value]));
    const observed = Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day), Number(values.hour), Number(values.minute), Number(values.second));
    guess += target - observed;
  }
  return guess;
}

function nextDate(dateString: string): string {
  const [year, month, day] = dateString.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
}

function metricFor(run: Run, key: string): Metric | undefined {
  return run.metrics?.find((metric) => metric.key === key);
}

function accuracy(run: Run): Metric | null {
  return metricFor(run, "overall_accuracy") || run.score || null;
}

function outcomeLabel(value: string): string {
  return ({ correct: "Correct", wrong: "Wrong answer", transport_failed: "Transport failed", parse_failed: "Parse failed", unknown: "Unknown" })[value] || value;
}

function displayError(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "detail" in value) {
    const detail = (value as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) return String((detail as { message: unknown }).message);
  }
  return "The data source returned an unexpected response. Try refreshing or check the source permissions.";
}

function displaySetting(value: unknown): string {
  return value === null || value === undefined || value === "" ? "Not recorded" : String(value);
}

function saveChart(format: "svg" | "png"): void {
  const svg = document.querySelector<SVGSVGElement>("[data-export-chart] svg");
  if (!svg) return;
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(svg.clientWidth || 900));
  clone.setAttribute("height", String(svg.clientHeight || 460));
  const markup = new XMLSerializer().serializeToString(clone);
  const blob = new Blob([markup], { type: "image/svg+xml;charset=utf-8" });
  const triggerDownload = (content: Blob, suffix: string) => {
    const href = URL.createObjectURL(content);
    const link = document.createElement("a");
    link.href = href;
    link.download = `evaluation-chart.${suffix}`;
    link.click();
    URL.revokeObjectURL(href);
  };
  if (format === "svg") return triggerDownload(blob, "svg");
  const url = URL.createObjectURL(blob);
  const image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    context?.drawImage(image, 0, 0);
    canvas.toBlob((png) => { if (png) triggerDownload(png, "png"); URL.revokeObjectURL(url); }, "image/png");
  };
  image.onerror = () => URL.revokeObjectURL(url);
  image.src = url;
}

function App() {
  const initial = useMemo(initialState, []);
  const [page, setPage] = useState<Page>(initial.page);
  const [filters, setFilters] = useState<Filters>(initial.filters);
  const [offset, setOffset] = useState(initial.offset);
  const [selectedRunId, setSelectedRunId] = useState(initial.selectedRun);
  const [compareIds, setCompareIds] = useState<string[]>(initial.compareIds);
  const [language, setLanguage] = useState<"en" | "zh">("en");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [detail, setDetail] = useState<Run | null>(null);
  const [questionRows, setQuestionRows] = useState<QuestionPage | null>(null);
  const [questionSearch, setQuestionSearch] = useState("");
  const [questionDetail, setQuestionDetail] = useState<Question | null>(null);
  const [runSearch, setRunSearch] = useState(new URLSearchParams(window.location.search).get("q") || "");
  const [userName, setUserName] = useState("");
  const [dimensionData, setDimensionData] = useState<{ items: Array<{ name: string; attempted?: number | null; correct?: number | null; filtered_subset_accuracy?: number | null; source_accuracy?: number | null; valid?: number | null; transport_failures?: number | null; parse_failures?: number | null }>; warning?: string | null; complete: boolean; available: boolean } | null>(null);
  const [selectedDimensionRun, setSelectedDimensionRun] = useState("");
  const [selectedQuestionRun, setSelectedQuestionRun] = useState("");
  const [compareData, setCompareData] = useState<{ runs: Run[]; comparisons: Array<{ eligible: boolean; candidate_run_id: string; reasons: string[]; delta: number | null; delta_unit: string | null }>; fair_ranking_enabled: boolean } | null>(null);
  const t = COPY[language];

  const writeUrl = useCallback((changes: Record<string, string | number | null | undefined>) => {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === undefined || value === "") url.searchParams.delete(key);
      else url.searchParams.set(key, String(value));
    }
    window.history.pushState({}, "", url);
  }, []);

  const navigate = useCallback((nextPage: Page) => {
    setPage(nextPage);
    setOffset(0);
    writeUrl({ view: nextPage, offset: null });
  }, [writeUrl]);

  const updateFilter = useCallback((key: keyof Filters, value: string) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setOffset(0);
    writeUrl({ [key]: key === "outcome" || key === "question_type" || key === "subject" ? value : value, offset: null });
  }, [writeUrl]);

  useEffect(() => {
    const onPop = () => {
      const state = initialState();
      setPage(state.page);
      setFilters(state.filters);
      setOffset(state.offset);
      setSelectedRunId(state.selectedRun);
      setCompareIds(state.compareIds);
      setRunSearch(new URLSearchParams(window.location.search).get("q") || "");
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: "50", offset: String(offset) });
    if (filters.benchmark) params.set("benchmark", filters.benchmark);
    if (filters.model) params.set("model", filters.model);
    if (filters.state) params.set("state", filters.state);
    if (filters.protocol) params.set("protocol", filters.protocol);
    if (filters.experiment_id) params.set("experiment_id", filters.experiment_id);
    if (filters.from) {
      const date = zonedMidnight(filters.from, filters.timezone);
      if (date !== undefined) params.set("started_after", String(date));
    }
    if (filters.to) {
      const date = zonedMidnight(nextDate(filters.to), filters.timezone);
      if (date !== undefined) params.set("started_before", String(date));
    }
    if (runSearch.trim()) params.set("q", runSearch.trim().slice(0, 120));
    if (refresh > 0) params.set("refresh", "true");
    setLoading(true);
    fetch(`/api/runs?${params}`, { credentials: "same-origin", signal: controller.signal })
      .then(async (response) => {
        const contentType = response.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) throw new Error("Sign in through the OpenShift login page to view this project.");
        const body = await response.json();
        if (!response.ok) throw new Error(displayError(body));
        return body as Catalog;
      })
      .then((body) => { if (alive) { setCatalog(body); setError(""); } })
      .catch((reason: unknown) => { if (alive && !(reason instanceof DOMException && reason.name === "AbortError")) setError(reason instanceof Error ? reason.message : "Could not load source data."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; controller.abort(); };
  }, [filters, offset, refresh, runSearch]);

  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin" }).then((r) => r.json()).then((d) => setUserName(d.user || "")).catch(() => undefined);
  }, []);

  const runs = catalog?.items || [];
  const options = catalog?.filter_options;
  const activeRun = runs.find((run) => run.run_id === selectedRunId) || runs[0] || null;
  const dimensionRun = runs.find((run) => run.run_id === selectedDimensionRun) || activeRun;
  const questionRun = runs.find((run) => run.run_id === selectedQuestionRun) || activeRun;

  useEffect(() => {
    if (selectedRunId) {
      fetch(`/api/runs/${encodeURIComponent(selectedRunId)}`).then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(displayError(body));
        return body as Run;
      }).then(setDetail).catch(() => setDetail(null));
    } else setDetail(null);
  }, [selectedRunId, refresh]);

  useEffect(() => {
    if (!dimensionRun?.run_id || (page !== "subjects" && page !== "types")) return;
    const dimension = page === "types" ? "question_type" : "subject";
    fetch(`/api/runs/${encodeURIComponent(dimensionRun.run_id)}/dimensions?dimension=${dimension}`)
      .then(async (r) => { const body = await r.json(); if (!r.ok) throw new Error(displayError(body)); return body; })
      .then(setDimensionData)
      .catch((reason: unknown) => setDimensionData({ items: [], complete: false, available: false, warning: reason instanceof Error ? reason.message : "Dimension data is unavailable." }));
  }, [dimensionRun?.run_id, page, refresh]);

  useEffect(() => {
    if (!questionRun?.run_id || page !== "errors") { setQuestionRows(null); return; }
    const controller = new AbortController();
    const params = new URLSearchParams({ run_id: questionRun.run_id, limit: "100", offset: "0" });
    if (filters.subject) params.set("subject", filters.subject);
    if (filters.question_type) params.set("question_type", filters.question_type);
    if (filters.outcome) params.set("outcome", filters.outcome);
    if (questionSearch.trim()) params.set("q", questionSearch.trim().slice(0, 100));
    fetch(`/api/questions?${params}`, { credentials: "same-origin", signal: controller.signal })
      .then(async (r) => { const body = await r.json(); if (!r.ok) throw new Error(displayError(body)); return body as QuestionPage; })
      .then(setQuestionRows)
      .catch((reason: unknown) => { if (!(reason instanceof DOMException && reason.name === "AbortError")) setQuestionRows({ available: false, complete: false, loaded_count: 0, total: 0, items: [], warning: reason instanceof Error ? reason.message : "Question data is unavailable." }); });
    return () => controller.abort();
  }, [questionRun?.run_id, page, filters.subject, filters.question_type, filters.outcome, questionSearch, refresh]);

  useEffect(() => {
    if (page !== "compare" || compareIds.length < 2) { setCompareData(null); return; }
    const params = new URLSearchParams({ run_ids: compareIds.join(",") });
    fetch(`/api/comparisons?${params}`)
      .then(async (r) => { const body = await r.json(); if (!r.ok) throw new Error(displayError(body)); return body; })
      .then(setCompareData)
      .catch(() => setCompareData(null));
  }, [page, compareIds, refresh]);

  const exportParams = useMemo(() => {
    const params = new URLSearchParams();
    for (const key of ["benchmark", "model", "state", "protocol", "experiment_id"] as const) if (filters[key]) params.set(key, filters[key]);
    if (filters.from) { const stamp = zonedMidnight(filters.from, filters.timezone); if (stamp !== undefined) params.set("started_after", String(stamp)); }
    if (filters.to) { const stamp = zonedMidnight(nextDate(filters.to), filters.timezone); if (stamp !== undefined) params.set("started_before", String(stamp)); }
    if (runSearch.trim()) params.set("q", runSearch.trim().slice(0, 120));
    return params.toString();
  }, [filters, runSearch]);

  const toggleCompare = (runId: string) => {
    setCompareIds((current) => {
      const next = current.includes(runId) ? current.filter((x) => x !== runId) : current.length < 6 ? [...current, runId] : current;
      writeUrl({ compare: next.join(",") || null });
      return next;
    });
  };

  const clearCompareSelection = () => {
    setCompareIds([]);
    writeUrl({ compare: null });
  };

  const openRun = (run: Run) => {
    if (!run.run_id) return;
    setSelectedRunId(run.run_id);
    writeUrl({ run: run.run_id });
  };

  const clearFilters = () => {
    setFilters(DEFAULT_FILTERS);
    setRunSearch("");
    setOffset(0);
    writeUrl({ ...Object.fromEntries(Object.keys(DEFAULT_FILTERS).map((x) => [x, null])), q: null, offset: null });
  };

  const selectedBenchmarkRuns = useMemo(() => runs.filter((run) => run.benchmark === filters.benchmark && accuracy(run)?.unit === "%"), [runs, filters.benchmark]);
  const dimensionChart = (dimensionData?.items || []).filter((item) => item.source_accuracy !== null && item.source_accuracy !== undefined).map((item) => ({ name: item.name, accuracy: item.source_accuracy, count: item.attempted ?? null, correct: item.correct ?? null }));
  const questionOutcomeOptions = ["wrong", "transport_failed", "parse_failed", "correct"];

  const runTable = (withCompare = false) => (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">{t.table}</caption>
        <thead><tr>{withCompare && <th scope="col">Select</th>}<th scope="col">{t.model}</th><th scope="col">{t.benchmark}</th><th scope="col">{t.score}</th><th scope="col">{t.attempted}</th><th scope="col">{t.state}</th><th scope="col">Protocol / source</th><th scope="col">Completed ({filters.timezone})</th><th scope="col">Run / job ID</th></tr></thead>
        <tbody>
          {runs.map((run) => {
            const score = accuracy(run);
            const id = run.run_id || run.source_id;
            return <tr key={run.source_id || id} className="table-row">
              {withCompare && <td><input aria-label={`Select ${run.benchmark || "unknown benchmark"} ${run.model.name} run ${(run.run_id || "unavailable").slice(0, 8)}`} type="checkbox" disabled={!run.run_id || (!compareIds.includes(run.run_id) && compareIds.length >= 6)} checked={Boolean(run.run_id && compareIds.includes(run.run_id))} onChange={() => run.run_id && toggleCompare(run.run_id)} /></td>}
              <td><button className="text-button" onClick={() => openRun(run)}>{run.model.name}{run.model.revision && <small className="muted"> · {run.model.revision}</small>}</button></td>
              <td>{run.benchmark || "Unknown"}</td>
              <td>{score ? <strong>{formatMetric(score)}</strong> : <span className="muted">Not available</span>}</td>
              <td>{run.metrics_raw?.attempted_count ?? "Unknown"}</td>
              <td><span className={`status status-${run.state}`}>{run.state}</span>{run.quality_gate_pass === true && <span className="quality-tag">Gate passed</span>}{run.quality_gate_pass === false && <span className="quality-tag fail">Gate failed</span>}</td>
              <td><span className="protocol-chip">{run.protocol || "Protocol unknown"}</span><div className="muted tiny">{run.source === "mlflow" ? "MLflow" : "EvalHub only"}</div></td>
              <td>{formatDate(run.end_time_ms, filters.timezone)}</td>
              <td><button className="id-button" onClick={() => openRun(run)} title={id}>{(run.run_id || run.job_id || "").slice(0, 12) || "Open detail"}…</button></td>
            </tr>;
          })}
          {runs.length === 0 && <tr><td colSpan={withCompare ? 9 : 8} className="empty-cell">{loading ? t.loading : t.noRuns}</td></tr>}
        </tbody>
      </table>
    </div>
  );

  const filteredQuestionRows = questionRows?.items || [];
  const selectedQuestion = questionDetail;

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand"><div className="brand-mark">E</div><div><strong>EvalScope</strong><span>Model evaluation</span></div></div>
        <div className="workspace-compact"><span className="eyebrow">{t.project}</span><strong>{catalog?.workspace?.label || "evalhub-lab"}</strong><span className="live-dot">Live workspace</span></div>
        <nav className="nav-list">{NAV.map((item) => <button key={item.id} className={`nav-item ${page === item.id ? "active" : ""}`} aria-current={page === item.id ? "page" : undefined} onClick={() => navigate(item.id)}><span aria-hidden="true">{item.icon}</span>{item.label}</button>)}</nav>
        <div className="sidebar-note"><span className="lock-icon" aria-hidden="true">◈</span><div><strong>{t.identity}</strong><p>{t.sourceNote}</p></div></div>
        <div className="sidebar-bottom"><span>Schema evaluation-dashboard.v1</span><span>RHOAI 3.5 · MLflow</span></div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>{t.project}</span><span aria-hidden="true">/</span><strong>{NAV.find((x) => x.id === page)?.label}</strong></div>
          <div className="top-actions"><span className="user-chip"><span className="avatar">{(userName || "P").slice(0, 1).toUpperCase()}</span>{userName || "Project viewer"}</span><button className="button secondary small" onClick={() => setLanguage(language === "en" ? "zh" : "en")} aria-label="Change interface language">{t.language}</button></div>
        </header>

        <section className="filter-panel" aria-label={t.filter}>
          <div className="filter-heading"><div><span className="eyebrow">{t.filter}</span><strong>{catalog?.workspace?.label || "evalhub-lab"}</strong></div><div className="filter-buttons"><a className="button tertiary small" href={`https://rh-ai.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com/mlflow`} target="_blank" rel="noreferrer">{t.openMLflow} ↗</a><button className="button secondary small" onClick={clearFilters}>Clear</button><button className="button primary small" onClick={() => setRefresh((n) => n + 1)} disabled={loading}><span className={loading ? "spin" : ""}>↻</span> {t.refresh}</button></div></div>
          <div className="filters-grid">
            <label>{t.benchmark}<select value={filters.benchmark} onChange={(e) => updateFilter("benchmark", e.target.value)}><option value="">{t.all}</option>{options?.benchmarks.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>{t.model}<select value={filters.model} onChange={(e) => updateFilter("model", e.target.value)}><option value="">{t.all}</option>{options?.models.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>{t.state}<select value={filters.state} onChange={(e) => updateFilter("state", e.target.value)}><option value="">{t.all}</option>{options?.states.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>Protocol<select value={filters.protocol} onChange={(e) => updateFilter("protocol", e.target.value)}><option value="">{t.all}</option>{options?.protocols.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>Experiment<select value={filters.experiment_id} onChange={(e) => updateFilter("experiment_id", e.target.value)}><option value="">{t.all}</option>{options?.experiments.map(([id, name]) => <option key={id} value={id}>{name || id}</option>)}</select></label>
            <label>{t.from}<input type="date" value={filters.from} onChange={(e) => updateFilter("from", e.target.value)} /></label>
            <label>{t.to}<input type="date" value={filters.to} onChange={(e) => updateFilter("to", e.target.value)} /></label>
            <label>{t.timezone}<select value={filters.timezone} onChange={(e) => updateFilter("timezone", e.target.value)}><option>Asia/Hong_Kong</option><option>UTC</option><option>America/Los_Angeles</option></select></label>
            <label className="search-filter">{t.search}<input type="search" value={runSearch} maxLength={120} placeholder={t.search} onChange={(e) => { setRunSearch(e.target.value); setOffset(0); writeUrl({ offset: null }); }} /></label>
          </div>
          <div className="active-filter-row"><span>{catalog?.total ?? 0} matching runs</span>{filters.benchmark && <button onClick={() => updateFilter("benchmark", "")} className="filter-chip">{filters.benchmark} ×</button>}{filters.model && <button onClick={() => updateFilter("model", "")} className="filter-chip">{filters.model} ×</button>}{filters.state && <button onClick={() => updateFilter("state", "")} className="filter-chip">{filters.state} ×</button>}{filters.protocol && <button onClick={() => updateFilter("protocol", "")} className="filter-chip">{filters.protocol} ×</button>}{filters.from && <span className="filter-chip-static">From {filters.from}</span>}{filters.to && <span className="filter-chip-static">Through {filters.to}</span>}</div>
        </section>

        <div className="capture-row"><div><span className={`source-badge ${catalog?.data_completeness === "partial" ? "partial" : ""}`}><span className="source-dot" />{catalog?.data_completeness === "partial" ? "Partial source data" : "Live source data"}</span><span className="capture-text">{catalog?.captured_at ? `Captured ${formatDate(Date.parse(catalog.captured_at), filters.timezone)} · ${catalog.captured_at}` : t.loading}</span></div><span className="read-only-pill">Read only · {catalog?.workspace?.authorization_mode || t.identity}</span></div>

        {error && <div className="alert error-alert" role="alert"><strong>Source connection needs attention</strong><span>{error}</span><button className="button secondary small" onClick={() => setRefresh((n) => n + 1)}>{t.retries}</button></div>}
        {catalog?.warnings?.map((warning) => <div className="alert warning-alert" role="status" key={`${warning.source}-${warning.kind}`}><strong>{warning.source} · {warning.kind || "partial"}</strong><span>{warning.message}</span></div>)}
        {catalog?.data_completeness === "partial" && <div className="alert warning-alert"><strong>Data completeness: partial</strong><span>At least one source or paginated query was incomplete. Run summaries remain visible; unavailable source details are not inferred.</span></div>}

        <div className="content-wrap">
          {page === "overview" && <>
            <div className="page-title-row"><div><span className="eyebrow">EVALUATION WORKSPACE</span><h1>{t.overview}</h1><p>Review original run scores, coverage, and source quality in {catalog?.workspace?.label || "evalhub-lab"}.</p></div><div className="title-actions"><a className="button secondary" href={`/api/exports/runs.csv${exportParams ? `?${exportParams}` : ""}`}>{t.csv}</a><a className="button secondary" href={`/api/exports/runs.json${exportParams ? `?${exportParams}` : ""}`}>{t.json}</a><a className="button secondary" href={`/api/exports/summary.html?${exportParams}`} target="_blank" rel="noreferrer">{t.print}</a></div></div>
            <div className="guide-card"><div className="guide-icon">i</div><div><strong>{t.firstUse}</strong><p>{t.step1} <span>→</span> {t.step2} <span>→</span> {t.step3}</p></div><span className="guide-count">3 steps</span></div>
            <div className="stat-grid">
              <article className="stat-card"><div className="stat-top"><span>{t.runs}</span><span className="stat-icon navy">▦</span></div><strong>{catalog?.overview.run_count ?? "—"}</strong><small>Filtered authorized runs · full server query</small></article>
              <article className="stat-card"><div className="stat-top"><span>{t.benchmarks}</span><span className="stat-icon blue">◫</span></div><strong>{catalog?.overview.benchmark_count ?? "—"}</strong><small>Shown separately; no cross-benchmark total score</small></article>
              <article className="stat-card"><div className="stat-top"><span>{t.completed}</span><span className="stat-icon green">✓</span></div><strong>{catalog?.overview.completed_count ?? "—"}</strong><small>Completed runs in current filters</small></article>
              <article className="stat-card"><div className="stat-top"><span>{t.failures}</span><span className="stat-icon amber">!</span></div><strong>{catalog?.overview.failed_or_cancelled_count ?? "—"}</strong><small>Run failures are separate from answer failures</small></article>
            </div>
            <div className="main-grid">
              <section className="panel wide-panel"><div className="panel-heading"><div><h2>{t.table}</h2><p>Original run metrics use the source denominator. Select up to six MLflow runs to inspect a cohort.</p></div><button className="button secondary small" onClick={() => navigate("compare")} disabled={compareIds.length < 2}>{t.compare} ({compareIds.length})</button></div>{runTable(true)}<Pager offset={offset} total={catalog?.total || 0} onChange={(next) => { setOffset(next); writeUrl({ offset: next || null }); }} /></section>
              <section className="panel side-panel"><div className="panel-heading"><div><h2>Coverage & data quality</h2><p>Missing provenance is shown as unknown.</p></div><span className="info-mark" title="Runs need matching dataset, selected IDs, prompt/scorer versions, and denominator rules for strict ranking." aria-label="Comparability help">?</span></div><div className="quality-list"><div><span>Comparability unknown</span><strong>{catalog?.overview.unknown_comparability_count ?? "—"}</strong></div><div><span>MLflow</span><strong className={catalog?.sources.mlflow?.status === "ok" ? "ok-text" : "bad-text"}>{catalog?.sources.mlflow?.status || "checking"}</strong></div><div><span>EvalHub</span><strong className={catalog?.sources.evalhub?.status === "ok" ? "ok-text" : "bad-text"}>{catalog?.sources.evalhub?.status || "checking"}</strong></div><div><span>MLflow rows loaded</span><strong>{catalog?.sources.mlflow?.completeness?.loaded_runs ?? "—"}</strong></div></div><div className="definition-box"><strong>Overall accuracy</strong><p>Correct / attempted, including invalid and failed attempts. Higher is better.</p><span>Valid-answer accuracy is a separate diagnostic metric.</span></div></section>
            </div>
            {filters.benchmark && <section className="panel chart-panel"><div className="panel-heading"><div><h2>{t.chart} · {filters.benchmark}</h2><p>Accuracy percentages for the selected benchmark only. Bars include run ID and sample count.</p></div><ChartExports onExport={saveChart} /></div>{selectedBenchmarkRuns.length ? <div className="chart-area" data-export-chart><ResponsiveContainer width="100%" height="100%"><BarChart accessibilityLayer layout="vertical" data={selectedBenchmarkRuns.map((run) => ({ name: `${run.model.name} · ${(run.run_id || run.job_id || "").slice(0, 8)}`, accuracy: accuracy(run)?.value ?? null, n: run.metrics_raw?.attempted_count ?? null })).filter((x) => x.accuracy !== null)} margin={{ top: 8, right: 24, left: 18, bottom: 10 }}><CartesianGrid strokeDasharray="3 3" horizontal={false} /><XAxis type="number" domain={[0, 100]} unit="%" tick={{ fill: "#65758b", fontSize: 11 }} /><YAxis type="category" dataKey="name" width={190} tick={{ fill: "#31445b", fontSize: 11 }} /><Tooltip formatter={(v) => [`${Number(v).toFixed(2)}%`, "Accuracy"]} /><ReferenceLine x={0} stroke="#d9e2ec" /><Bar dataKey="accuracy" fill="#2474d2" radius={[0, 5, 5, 0]} name="Overall accuracy" /></BarChart></ResponsiveContainer></div> : <div className="inline-empty">No mapped accuracy metric is available for this benchmark.</div>}<div className="chart-table-link">The table above exposes the exact values and denominators for keyboard and screen-reader users.</div></section>}
          </>}

          {page === "compare" && <>
            <PageTitle title="Model comparison" subtitle="Compare selected runs inside one benchmark. Missing provenance keeps the view descriptive and disables ranking." />
            <section className="panel">
              <div className="panel-heading">
                <div><h2>Choose runs</h2><p>Select two to six MLflow runs here or in Overview. Current filters and search narrow this list; use the pager to browse more runs.</p></div>
                <div className="filter-buttons">
                  <span className="count-pill" aria-live="polite">{compareIds.length} of 6 selected</span>
                  <button className="button secondary small" onClick={clearCompareSelection} disabled={compareIds.length === 0}>Clear selection</button>
                </div>
              </div>
              {runTable(true)}
              <Pager offset={offset} total={catalog?.total || 0} onChange={(next) => { setOffset(next); writeUrl({ offset: next || null }); }} />
            </section>
            <section className="panel"><div className="panel-heading"><div><h2>Selected runs</h2><p>The first selected run is the baseline. Select at least two MLflow runs to compare.</p></div><span className="count-pill">{compareIds.length} selected</span></div>{compareIds.length < 2 ? <EmptyState text="Choose at least two MLflow runs in the table above to see their comparison." /> : compareData ? <>
              {!compareData.fair_ranking_enabled && <div className="alert warning-alert"><strong>{t.noComparable}</strong><span>Every comparison lists its exact missing or mismatched fields below.</span></div>}
              <div className="table-wrap"><table><thead><tr><th>Role</th><th>{t.model}</th><th>{t.benchmark}</th><th>{t.score}</th><th>n</th><th>Run ID</th></tr></thead><tbody>{compareData.runs.map((run, index) => <tr key={run.source_id}><td>{index === 0 ? <span className="baseline-chip">Baseline</span> : `Candidate ${index}`}</td><td>{run.model.name}</td><td>{run.benchmark}</td><td>{accuracy(run) ? formatMetric(accuracy(run)!) : "Unknown"}</td><td>{run.metrics_raw?.attempted_count ?? "Unknown"}</td><td className="mono">{run.run_id || run.source_id}</td></tr>)}</tbody></table></div>
              <div className="comparison-grid">{compareData.comparisons.map((item, index) => <article className="compare-card" key={item.candidate_run_id || index}><span className={item.eligible ? "status status-completed" : "status status-unknown"}>{item.eligible ? "Eligible cohort" : "Descriptive only"}</span><h3>Candidate {index + 1}</h3>{item.delta !== null ? <strong>{item.delta > 0 ? "+" : ""}{item.delta.toFixed(2)} percentage points</strong> : <strong>Delta unavailable</strong>}<ul>{item.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></article>)}</div>
            </> : <LoadingState text="Checking cohort provenance…" />}</section>
          </>}

          {(page === "subjects" || page === "types") && <>
            <PageTitle title={page === "subjects" ? "Subject & domain explorer" : "QualBench question types"} subtitle={page === "subjects" ? "Rank source dimensions and inspect counts when saved question artifacts are complete." : "Compare single-choice, multiple-choice, and judgment only when the provider records those dimensions."} />
            <section className="panel"><div className="panel-heading dimension-heading"><div><h2>{page === "subjects" ? t.subject : t.questionType}</h2><p>{dimensionRun?.benchmark || "Choose a benchmark"} · {dimensionRun?.model.name || "No run"} · source values remain visible beside any filtered recomputation.</p></div><label className="compact-select">{t.chooseRun}<select value={dimensionRun?.run_id || ""} onChange={(e) => setSelectedDimensionRun(e.target.value)}>{runs.filter((x) => x.run_id).map((run) => <option key={run.run_id!} value={run.run_id!}>{run.benchmark} · {run.model.name} · {run.run_id!.slice(0, 8)}</option>)}</select></label></div>
              {dimensionData?.warning && <div className="alert warning-alert"><strong>{dimensionData.complete ? "Source note" : "Question detail limitation"}</strong><span>{dimensionData.warning}</span></div>}
              {!dimensionData?.available ? <EmptyState text={dimensionData?.warning || "No source metrics or question artifact are available for this dimension."} /> : <div className="dimension-layout"><div className="chart-area dimension-chart" data-export-chart><ResponsiveContainer width="100%" height="100%"><BarChart accessibilityLayer layout="vertical" data={dimensionChart} margin={{ top: 8, right: 30, left: 20, bottom: 10 }} onClick={(event) => { const name = (event as { activeLabel?: string } | null)?.activeLabel; if (name && page === "subjects") { updateFilter("subject", name); navigate("errors"); if (dimensionRun?.run_id) setSelectedQuestionRun(dimensionRun.run_id); } }}><CartesianGrid strokeDasharray="3 3" horizontal={false} /><XAxis type="number" domain={[0, 100]} unit="%" tick={{ fill: "#65758b", fontSize: 11 }} /><YAxis type="category" dataKey="name" width={180} tick={{ fill: "#31445b", fontSize: 11 }} /><Tooltip formatter={(v) => [`${Number(v).toFixed(2)}%`, "Source accuracy"]} /><Bar dataKey="accuracy" fill="#2474d2" radius={[0, 5, 5, 0]} name="Source accuracy" /></BarChart></ResponsiveContainer></div><div className="table-wrap dimension-table"><table><thead><tr><th>{t.subject}</th><th>Source accuracy</th><th>Filtered subset accuracy</th><th>Attempted</th><th>Correct</th></tr></thead><tbody>{dimensionData.items.map((item) => <tr key={item.name}><td>{page === "subjects" ? <button className="text-button" onClick={() => { updateFilter("subject", item.name); if (dimensionRun?.run_id) setSelectedQuestionRun(dimensionRun.run_id); navigate("errors"); }}>{item.name}</button> : item.name}</td><td>{item.source_accuracy === null || item.source_accuracy === undefined ? "Not available" : `${item.source_accuracy.toFixed(2)}%`}</td><td>{item.filtered_subset_accuracy === null || item.filtered_subset_accuracy === undefined ? "Not computed" : `${item.filtered_subset_accuracy.toFixed(2)}%`}</td><td>{item.attempted ?? "Unknown"}</td><td>{item.correct ?? "Unknown"}</td></tr>)}</tbody></table></div></div>}
              <div className="chart-table-link">Accuracy uses a 0–100% axis. Missing counts are unknown, not zero. Select a subject row to inspect its questions.</div>
            </section>
          </>}

          {page === "errors" && <>
            <PageTitle title="Question & error analysis" subtitle="Transport failures, parser failures, wrong answers, and truncation are shown as separate evidence fields." />
            <section className="panel"><div className="panel-heading question-heading"><div><h2>Saved question results</h2><p>Question text and raw responses are fetched only after you open a row.</p></div><label className="compact-select">{t.chooseRun}<select value={questionRun?.run_id || ""} onChange={(e) => { setSelectedQuestionRun(e.target.value); setFilters((f) => ({ ...f, subject: "", question_type: "" })); }}>{runs.filter((x) => x.run_id).map((run) => <option key={run.run_id!} value={run.run_id!}>{run.benchmark} · {run.model.name} · {run.run_id!.slice(0, 8)}</option>)}</select></label></div>
              <div className="question-filters"><label>{t.subject}<input value={filters.subject} list="subjects-list" onChange={(e) => updateFilter("subject", e.target.value)} placeholder="Any saved subject or domain" /><datalist id="subjects-list">{dimensionData?.items.map((x) => <option key={x.name} value={x.name} />)}</datalist></label><label>{t.questionType}<input value={filters.question_type} onChange={(e) => updateFilter("question_type", e.target.value)} placeholder="Any saved question type" /></label><label>{t.outcome}<select value={filters.outcome} onChange={(e) => updateFilter("outcome", e.target.value)}><option value="">All outcomes</option>{questionOutcomeOptions.map((x) => <option key={x} value={x}>{outcomeLabel(x)}</option>)}</select></label><label>Question search<input type="search" maxLength={100} value={questionSearch} placeholder="Search saved question text" onChange={(e) => setQuestionSearch(e.target.value)} /></label></div>
              {questionRows?.warning && <div className="alert warning-alert"><strong>{questionRows.available ? "Partial question data" : "Question artifacts unavailable"}</strong><span>{questionRows.warning}</span></div>}
              {questionRows?.available ? <><div className="question-summary"><span>{questionRows.total} visible rows · {questionRows.loaded_count} loaded of {questionRows.expected_count ?? "unknown"} expected</span>{questionRows.filtered_subset_accuracy !== null && questionRows.filtered_subset_accuracy !== undefined && <strong>{t.filtered}: {questionRows.filtered_subset_accuracy.toFixed(2)}%</strong>}<span className={questionRows.complete ? "ok-text" : "muted"}>{questionRows.complete ? "Artifact complete" : "Artifact completeness unknown"}</span></div><div className="table-wrap"><table><thead><tr><th>Question ID</th><th>{t.subject}</th><th>{t.questionType}</th><th>{t.outcome}</th><th>Transport</th><th>Parser</th><th>Truncated</th><th>Latency</th><th>Inspect</th></tr></thead><tbody>{filteredQuestionRows.map((row) => <tr key={row.row_id}><td className="mono">{row.question_id}</td><td>{row.subject || row.domain || "Unknown"}</td><td>{row.question_type || "Unknown"}</td><td><span className={`status status-${row.outcome}`}>{outcomeLabel(row.outcome)}</span></td><td>{row.transport_ok === null || row.transport_ok === undefined ? "Unknown" : row.transport_ok ? "OK" : "Failed"}</td><td>{row.parse_ok === null || row.parse_ok === undefined ? "Unknown" : row.parse_ok ? "Valid" : "Invalid"}</td><td>{row.truncated === null || row.truncated === undefined ? "Unknown" : row.truncated ? "Yes" : "No"}</td><td>{row.latency_ms === null || row.latency_ms === undefined ? "Unknown" : `${row.latency_ms.toFixed(0)} ms`}</td><td><button className="button tertiary small" onClick={() => { fetch(`/api/questions/${encodeURIComponent(questionRun!.run_id!)}/${row.row_id}?include_text=true`).then((r) => r.json()).then(setQuestionDetail).catch(() => setQuestionDetail(null)); }}>Open row</button></td></tr>)}</tbody></table></div></> : !loading && <EmptyState text={t.noArtifact} />}
            </section>
          </>}

          {page === "performance" && <>
            <PageTitle title="Performance" subtitle="Throughput and latency are shown only when the selected run recorded those measurements." />
            <section className="panel"><div className="panel-heading"><div><h2>Workload and serving metrics</h2><p>Accuracy and performance may be linked only when run, model revision, serving setup, and workload evidence match.</p></div><label className="compact-select">{t.chooseRun}<select value={activeRun?.run_id || ""} onChange={(e) => { setSelectedRunId(e.target.value); writeUrl({ run: e.target.value }); }}>{runs.filter((x) => x.run_id).map((run) => <option key={run.run_id!} value={run.run_id!}>{run.benchmark} · {run.model.name} · {run.run_id!.slice(0, 8)}</option>)}</select></label></div>
              {activeRun ? <><div className="perf-grid">{(detail?.metrics || activeRun.metrics || []).filter((m) => ["output_tokens_per_second", "prompt_tokens_per_second", "requests_per_second", "mean_ttft_ms", "mean_itl_ms"].includes(m.key)).map((metric) => <article className="perf-card" key={metric.key}><span>{metric.label}</span><strong>{formatMetric(metric)}</strong><p>{metric.meaning}</p><small>{metric.direction === "lower" ? "Lower is faster" : "Higher is faster"}</small></article>)}</div><div className="config-grid"><div><span>Concurrency</span><strong>{displaySetting(detail?.provenance?.concurrency ?? activeRun.provenance?.concurrency)}</strong></div><div><span>Token budget</span><strong>{displaySetting(detail?.provenance?.token_budget ?? activeRun.provenance?.token_budget)}</strong></div><div><span>Temperature</span><strong>{displaySetting(detail?.provenance?.temperature ?? activeRun.provenance?.temperature)}</strong></div><div><span>Seed</span><strong>{displaySetting(detail?.provenance?.seed_policy ?? activeRun.provenance?.seed_policy)}</strong></div></div><div className="definition-box"><strong>Measurement availability</strong><p>{(detail?.metrics || activeRun.metrics || []).some((m) => ["output_tokens_per_second", "prompt_tokens_per_second", "requests_per_second", "mean_ttft_ms", "mean_itl_ms"].includes(m.key)) ? "Values retain their raw source and measurement notes." : "This run has no saved throughput or latency metrics. No speed value is inferred from accuracy or duration."}</p></div></> : <EmptyState text="Choose a run to inspect recorded performance." />}
            </section>
          </>}

          {page === "trends" && <>
            <PageTitle title="Trends & all runs" subtitle="Runs are ordered by completion time. Scores from different benchmarks remain in separate rows." />
            <section className="panel"><div className="panel-heading"><div><h2>Evaluation history</h2><p>Use benchmark, model, protocol, state, and date filters above to narrow the history.</p></div><span className="count-pill">{catalog?.total ?? 0} results</span></div>{runTable(false)}<Pager offset={offset} total={catalog?.total || 0} onChange={(next) => { setOffset(next); writeUrl({ offset: next || null }); }} /></section>
          </>}
        </div>
      </main>

      {selectedRunId && detail && <RunDrawer run={detail} timezone={filters.timezone} onClose={() => { setSelectedRunId(""); setDetail(null); writeUrl({ run: null }); }} t={t} />}
      {selectedQuestion && <QuestionDrawer question={selectedQuestion} onClose={() => setQuestionDetail(null)} />}
      {loading && !catalog && <div className="loading-screen"><div className="loader" /><p>{t.loading}</p></div>}
    </div>
  );
}

function PageTitle({ title, subtitle }: { title: string; subtitle: string }) {
  return <div className="page-title-row"><div><span className="eyebrow">EVALUATION WORKSPACE</span><h1>{title}</h1><p>{subtitle}</p></div></div>;
}

function EmptyState({ text }: { text: string }) { return <div className="empty-state"><span className="empty-icon">◌</span><strong>{text}</strong></div>; }
function LoadingState({ text }: { text: string }) { return <div className="loading-inline"><span className="loader small-loader" />{text}</div>; }

function Pager({ offset, total, onChange }: { offset: number; total: number; onChange: (next: number) => void }) {
  const pageSize = 50;
  return <div className="pager"><span>Rows {total ? offset + 1 : 0}–{Math.min(offset + pageSize, total)} of {total}</span><div><button className="button secondary small" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - pageSize))}>Previous</button><button className="button secondary small" disabled={offset + pageSize >= total} onClick={() => onChange(offset + pageSize)}>Next</button></div></div>;
}

function ChartExports({ onExport }: { onExport: (format: "svg" | "png") => void }) {
  return <div className="chart-actions"><button className="button tertiary small" onClick={() => onExport("svg")}>SVG</button><button className="button tertiary small" onClick={() => onExport("png")}>PNG</button></div>;
}

function RunDrawer({ run, timezone, onClose, t }: { run: Run; timezone: string; onClose: () => void; t: typeof COPY.en | typeof COPY.zh }) {
  const [copied, setCopied] = useState(false);
  const raw = run.metrics || [];
  const advanced = raw.filter((metric) => metric.key !== "overall_score");
  const copyId = async () => {
    if (!run.run_id) return;
    await navigator.clipboard?.writeText(run.run_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return <div className="drawer-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}><aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><div className="drawer-heading"><div><span className="eyebrow">{run.benchmark || "Evaluation"} · {run.workspace}</span><h2 id="drawer-title">{t.details}</h2></div><button className="close-button" onClick={onClose} aria-label={t.close}>×</button></div><div className="drawer-body">
    <div className="drawer-score"><span>{t.score}</span><strong>{run.score ? formatMetric(run.score) : "Not available"}</strong><small>{run.metrics_raw?.correct_count ?? "?"} correct / {run.metrics_raw?.attempted_count ?? "?"} attempted. Failed and invalid questions remain in the denominator.</small></div>
    <div className="drawer-id"><span>MLflow run ID</span><code>{run.run_id || "No MLflow link"}</code>{run.run_id && <button className="button tertiary small" onClick={copyId}>{copied ? "Copied" : "Copy"}</button>}</div>
    <dl className="detail-list"><div><dt>{t.model}</dt><dd>{run.model.name}{run.model.revision ? ` · ${run.model.revision}` : ""}</dd></div><div><dt>{t.benchmark}</dt><dd>{run.benchmark || "Unknown"}</dd></div><div><dt>{t.state}</dt><dd><span className={`status status-${run.state}`}>{run.state}</span></dd></div><div><dt>Experiment</dt><dd>{run.experiment_name || run.experiment_id || "Unknown"}</dd></div><div><dt>Protocol</dt><dd>{run.protocol || "Unknown"}</dd></div><div><dt>Started ({timezone})</dt><dd>{formatDate(run.start_time_ms, timezone)}</dd></div><div><dt>Finished ({timezone})</dt><dd>{formatDate(run.end_time_ms, timezone)}</dd></div><div><dt>EvalHub job</dt><dd>{run.job_id || "Not linked"}</dd></div><div><dt>Quality gate</dt><dd>{run.quality_gate_pass === true ? "Passed" : run.quality_gate_pass === false ? "Failed" : "Not recorded"}</dd></div></dl>
    {run.source_conflicts?.length ? <div className="alert warning-alert"><strong>Source conflict</strong><ul>{run.source_conflicts.map((item) => <li key={item.metric}>{item.metric}: MLflow {item.mlflow_value}; EvalHub {item.evalhub_value}</li>)}</ul></div> : null}
    {run.warnings?.map((warning) => <div className="alert warning-alert" key={warning}>{warning}</div>)}
    <section className="drawer-section"><h3>Metric dictionary</h3><div className="metric-list">{advanced.map((metric) => <div className="metric-row" key={metric.key}><div><strong>{metric.label}</strong><code>{metric.key}</code><p>{metric.meaning || "Unmapped source metric; scale is preserved as raw."}</p></div><div className="metric-value">{formatMetric(metric)}<small>{metric.denominator || "Denominator unknown"} · {metric.direction || "direction unknown"}</small></div></div>)}</div></section>
    <section className="drawer-section"><h3>{t.rawMetrics}</h3><details><summary>Show raw source keys and values</summary><pre>{JSON.stringify(run.metrics_raw, null, 2)}</pre></details></section>
    <section className="drawer-section"><h3>Provenance & cohort</h3><p className="muted">Fair ranking stays disabled if dataset, selected question set, parser/scorer, decoding settings, or denominator provenance is missing or differs.</p><dl className="detail-list">{Object.entries(run.provenance || {}).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value === null || value === undefined ? "Unknown" : String(value)}</dd></div>)}</dl></section>
    <section className="drawer-section"><h3>Source artifacts</h3><p>{(run as Run & { artifacts?: Array<{ path: string; size_bytes?: number }> }).artifacts?.length ? (run as Run & { artifacts?: Array<{ path: string; size_bytes?: number }> }).artifacts!.map((a) => <code className="artifact-chip" key={a.path}>{a.path} · {a.size_bytes ? `${Math.round(a.size_bytes / 1024)} KB` : "size unknown"}</code>) : t.unavailable}</p></section>
  </div></aside></div>;
}

function QuestionDrawer({ question, onClose }: { question: Question; onClose: () => void }) {
  const text = [question.question, question.context, question.options, question.gold, question.prediction, question.raw_response].filter(Boolean).join("\n\n");
  const copy = async () => { if (text) await navigator.clipboard?.writeText(text); };
  return <div className="drawer-backdrop top-layer" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}><aside className="drawer question-drawer" role="dialog" aria-modal="true" aria-labelledby="question-drawer-title"><div className="drawer-heading"><div><span className="eyebrow">{question.question_id}</span><h2 id="question-drawer-title">Question evidence</h2></div><button className="close-button" onClick={onClose} aria-label="Close question detail">×</button></div><div className="drawer-body"><div className="question-outcome-strip"><span className={`status status-${question.outcome}`}>{outcomeLabel(question.outcome)}</span><span>Transport: {question.transport_ok === null || question.transport_ok === undefined ? "Unknown" : question.transport_ok ? "OK" : "Failed"}</span><span>Parser: {question.parse_ok === null || question.parse_ok === undefined ? "Unknown" : question.parse_ok ? "Valid" : "Invalid"}</span><span>Truncated: {question.truncated === null || question.truncated === undefined ? "Unknown" : question.truncated ? "Yes" : "No"}</span></div><button className="button secondary small" onClick={copy}>Copy visible evidence</button>
      <Evidence label="Question" value={question.question} fallback="The prompt was not included in the saved artifacts." />
      <Evidence label="Context / passage" value={question.context} fallback="Not included in saved artifacts." />
      <Evidence label="Options" value={question.options} fallback="Not included in saved artifacts." />
      <Evidence label="Gold answer" value={question.gold} fallback="Not included in saved artifacts." />
      <Evidence label="Parsed prediction" value={question.prediction} fallback="Not included in saved artifacts." />
      <Evidence label="Raw model response" value={question.raw_response} fallback="Not included in saved artifacts." />
      <Evidence label="Reference explanation" value={question.reference_explanation} fallback="Not included in saved artifacts. This is reference content, not model reasoning." />
      <div className="detail-list"><div><dt>Latency</dt><dd>{question.latency_ms === null || question.latency_ms === undefined ? "Unknown" : `${question.latency_ms.toFixed(1)} ms`}</dd></div><div><dt>Usage</dt><dd>{question.usage ? JSON.stringify(question.usage) : "Not recorded"}</dd></div><div><dt>Error reason</dt><dd>{question.error_type || "None recorded"}</dd></div></div>
    </div></aside></div>;
}

function Evidence({ label, value, fallback }: { label: string; value?: string | null; fallback: string }) {
  return <section className="evidence-block"><h3>{label}</h3>{value ? <pre>{value}</pre> : <p className="muted">{fallback}</p>}</section>;
}

export default App;

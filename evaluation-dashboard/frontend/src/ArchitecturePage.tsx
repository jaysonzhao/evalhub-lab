type ArchitecturePageProps = { onTrigger: () => void };

export default function ArchitecturePage({ onTrigger }: ArchitecturePageProps) {
  return (
    <>
      <div className="page-title-row">
        <div>
          <span className="eyebrow">EVALUATION WORKSPACE</span>
          <h1>Architecture</h1>
          <p>How benchmark requests become scored, traceable results in this workspace.</p>
        </div>
        <button className="button primary" onClick={onTrigger}>Start an evaluation</button>
      </div>

      <section className="panel architecture-panel" aria-labelledby="architecture-title">
        <div className="panel-heading">
          <div><h2 id="architecture-title">Evaluation and evidence flow</h2><p>One PipelineRun coordinates each selected benchmark from preflight through the data-quality gate.</p></div>
          <span className="count-pill">OpenShift · Tekton · EvalHub · MLflow</span>
        </div>
        <div className="architecture-diagram-scroll">
          <svg className="architecture-diagram" viewBox="0 0 1120 610" role="img" aria-labelledby="diagram-title diagram-description">
            <title id="diagram-title">EvalHub benchmark evaluation architecture</title>
            <desc id="diagram-description">A user uses the authenticated dashboard to start a Tekton PipelineRun. Preflight validates configuration, then the pipeline submits EvalHub jobs, waits for completion, collects MLflow evidence, and applies a quality gate. A finally diagnostics task records the durable run summary in a project ConfigMap ledger. EvalHub runs the Chinese multiple-choice adapter against the selected MaaS model endpoint.</desc>
            <defs>
              <marker id="arch-arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#6283a2" /></marker>
              <marker id="arch-arrow-green" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#4e8b70" /></marker>
            </defs>

            <text x="36" y="26" className="arch-lane-label">CONTROL PLANE</text>
            <rect x="36" y="42" width="210" height="76" rx="12" className="arch-node arch-node-blue" />
            <text x="141" y="73" textAnchor="middle" className="arch-title">Dashboard</text>
            <text x="141" y="96" textAnchor="middle" className="arch-subtitle">Authenticated project user</text>
            <path d="M246 80 H318" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="326" y="42" width="260" height="76" rx="12" className="arch-node" />
            <text x="456" y="73" textAnchor="middle" className="arch-title">OAuth proxy + FastAPI</text>
            <text x="456" y="96" textAnchor="middle" className="arch-subtitle">Read results · create scoped PipelineRuns</text>
            <path d="M586 80 H658" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="666" y="42" width="260" height="76" rx="12" className="arch-node arch-node-purple" />
            <text x="796" y="73" textAnchor="middle" className="arch-title">OpenShift Pipelines</text>
            <text x="796" y="96" textAnchor="middle" className="arch-subtitle">maas-evaluation PipelineRun</text>
            <path d="M796 118 V148 H133 V202" className="arch-link" markerEnd="url(#arch-arrow)" />

            <rect x="36" y="158" width="1048" height="142" rx="16" className="arch-group" />
            <text x="56" y="184" className="arch-lane-label">TEKTON ORCHESTRATION</text>
            <rect x="58" y="202" width="150" height="72" rx="10" className="arch-step" />
            <text x="133" y="233" textAnchor="middle" className="arch-title">Preflight</text>
            <text x="133" y="255" textAnchor="middle" className="arch-subtitle">Data · endpoint · Secret</text>
            <path d="M208 238 H225" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="225" y="202" width="150" height="72" rx="10" className="arch-step" />
            <text x="300" y="233" textAnchor="middle" className="arch-title">Submit</text>
            <text x="300" y="255" textAnchor="middle" className="arch-subtitle">EvalHub jobs per set</text>
            <path d="M375 238 H392" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="392" y="202" width="150" height="72" rx="10" className="arch-step" />
            <text x="467" y="233" textAnchor="middle" className="arch-title">Wait</text>
            <text x="467" y="255" textAnchor="middle" className="arch-subtitle">Terminal job states</text>
            <path d="M542 238 H559" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="559" y="202" width="150" height="72" rx="10" className="arch-step" />
            <text x="634" y="233" textAnchor="middle" className="arch-title">Collect</text>
            <text x="634" y="255" textAnchor="middle" className="arch-subtitle">MLflow links · metrics</text>
            <path d="M709 238 H726" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="726" y="202" width="150" height="72" rx="10" className="arch-step arch-step-gate" />
            <text x="801" y="233" textAnchor="middle" className="arch-title">Quality gate</text>
            <text x="801" y="255" textAnchor="middle" className="arch-subtitle">Execution · data quality</text>
            <path d="M876 238 H893" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="893" y="202" width="150" height="72" rx="10" className="arch-step arch-step-diagnostics" />
            <text x="968" y="233" textAnchor="middle" className="arch-title">Finally: diagnostics</text>
            <text x="968" y="255" textAnchor="middle" className="arch-subtitle">Ledger · MaaS key cleanup</text>

            <path d="M300 274 V330 H174 V366" className="arch-link" markerEnd="url(#arch-arrow)" />
            <path d="M634 274 V330 H978 V366" className="arch-link" markerEnd="url(#arch-arrow-green)" />
            <path d="M968 274 V318 H1100 V551 H1086" className="arch-link" markerEnd="url(#arch-arrow-green)" />

            <text x="36" y="352" className="arch-lane-label">EVALUATION AND EVIDENCE</text>
            <rect x="66" y="374" width="216" height="84" rx="12" className="arch-node arch-node-blue" />
            <text x="174" y="405" textAnchor="middle" className="arch-title">EvalHub</text>
            <text x="174" y="429" textAnchor="middle" className="arch-subtitle">Job lifecycle · benchmark scoring</text>
            <path d="M282 416 H326" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="334" y="374" width="216" height="84" rx="12" className="arch-node" />
            <text x="442" y="405" textAnchor="middle" className="arch-title">Chinese MCQ adapter</text>
            <text x="442" y="429" textAnchor="middle" className="arch-subtitle">Prompt · parse · score · report</text>
            <path d="M550 416 H594" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="602" y="374" width="216" height="84" rx="12" className="arch-node arch-node-purple" />
            <text x="710" y="405" textAnchor="middle" className="arch-title">MaaS endpoint</text>
            <text x="710" y="429" textAnchor="middle" className="arch-subtitle">Selected model · run API key</text>
            <path d="M602 416 H550" className="arch-link" markerEnd="url(#arch-arrow)" />
            <rect x="870" y="374" width="216" height="84" rx="12" className="arch-node arch-node-green" />
            <text x="978" y="405" textAnchor="middle" className="arch-title">MLflow</text>
            <text x="978" y="429" textAnchor="middle" className="arch-subtitle">Metrics · artifacts · run links</text>

            <path d="M442 458 V498 H978 V458" className="arch-data-link" markerEnd="url(#arch-arrow-green)" />
            <text x="700" y="492" textAnchor="middle" className="arch-data-label">Evaluation summaries and metrics</text>
            <rect x="824" y="522" width="262" height="58" rx="11" className="arch-ledger" />
            <text x="955" y="547" textAnchor="middle" className="arch-title">Project ConfigMap ledger</text>
            <text x="955" y="566" textAnchor="middle" className="arch-subtitle">Run selection · gate outcome · evidence links</text>
            <text x="36" y="550" className="arch-footnote">The dashboard joins MLflow and EvalHub results, and displays PipelineRun status separately.</text>
          </svg>
        </div>
        <div className="architecture-legend"><span><i className="legend-blue" />Control and evaluation APIs</span><span><i className="legend-purple" />Pipeline and model execution</span><span><i className="legend-green" />Saved evidence and ledger</span></div>
      </section>

      <div className="architecture-cards">
        <article className="panel architecture-info-card"><span className="architecture-index">01</span><h3>Orchestrate and validate</h3><p>Preflight checks the selected benchmark data, endpoint allowlist, model name, and approved Secret reference before creating EvalHub jobs.</p></article>
          <article className="panel architecture-info-card"><span className="architecture-index">02</span><h3>Evaluate through EvalHub</h3><p>The dashboard uses the entered MaaS key to load authorized models from <code>/v1/models</code>. The selected model ID sets the inference route. EvalHub runs the Chinese MCQ adapter, parses answers, scores them, and reports metrics and question artifacts.</p></article>
        <article className="panel architecture-info-card"><span className="architecture-index">03</span><h3>Collect and gate evidence</h3><p>The pipeline waits for terminal jobs, checks required counts and quality signals, links MLflow runs, and records a durable summary. Accuracy thresholds are separate and must be configured explicitly.</p></article>
      </div>

      <section className="panel architecture-catalog" aria-labelledby="benchmark-catalog-title">
        <div className="panel-heading"><div><h2 id="benchmark-catalog-title">Benchmarks and profiles</h2><p>Choose one or more datasets for a single PipelineRun; each selection creates an EvalHub job.</p></div></div>
        <div className="architecture-benchmark-grid">
          <article><strong>HKMMLU</strong><span>Hong Kong Chinese knowledge questions</span></article>
          <article><strong>TMMLU+</strong><span>Taiwan Traditional Chinese knowledge questions</span></article>
          <article><strong>C-MMLU</strong><span>Chinese multi-domain knowledge questions</span></article>
          <article><strong>QualBench</strong><span>Approved diagnostic subset; known source anomalies are excluded</span></article>
        </div>
        <div className="architecture-profile-row"><strong>Profiles</strong><span><b>Smoke</b> · up to five validated examples per benchmark</span><span><b>Coverage</b> · representative subject or group samples</span><span><b>Full</b> · all validated rows; disabled for QualBench</span></div>
      </section>
    </>
  );
}

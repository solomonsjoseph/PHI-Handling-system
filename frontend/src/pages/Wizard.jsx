import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { API } from '../lib/api';
import { Btn, CheckCard, Tag } from '../components/ui';

const STEPS = [
  { id: 1, kicker: 'Step one', title: 'Upload the study package' },
  { id: 2, kicker: 'Step two', title: 'Configure the run' },
  { id: 3, kicker: 'Step three', title: 'Choose what you take home' },
];

function ProgressRail({ step }) {
  return (
    <aside className="w-64 pt-24 pl-10 pr-6 border-r border-rule hidden lg:block" data-testid="wizard-rail">
      <div className="kicker mb-8">PHI Console</div>
      <ol className="space-y-6">
        {STEPS.map(s => {
          const active = step === s.id;
          const done = step > s.id;
          return (
            <li key={s.id} className="flex items-start gap-4">
              <div className={`mt-1 w-2 h-2 rounded-full flex-shrink-0
                ${active ? 'bg-oxblood ring-4 ring-oxblood/15' : done ? 'bg-ink' : 'bg-rule'}`}/>
              <div>
                <div className={`kicker ${active ? 'text-oxblood' : 'text-ink-muted'}`}>{s.kicker}</div>
                <div className={`font-display text-[15px] mt-0.5
                  ${active ? 'text-ink' : done ? 'text-ink-2' : 'text-ink-muted'}`}>{s.title}</div>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="mt-16 kicker text-ink-muted">On the record</div>
      <p className="text-[13px] text-ink-2 leading-6 mt-2 max-w-[220px]">
        Your session is transient. The bundle you download is the receipt.
        Nothing is retained after this run unless you pin it.
      </p>
    </aside>
  );
}

// ---------- STEP 1 --------------------------------------------------------

function StepUpload({ onNext, setSid, sid, corpusMode, setCorpusMode, setCorpusResult }) {
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [manifest, setManifest] = useState(null);
  const [corpusBusy, setCorpusBusy] = useState(false);
  const [corpusPreset, setCorpusPreset] = useState('hipaa_max_adversarial');
  const [rowCount, setRowCount] = useState(6);
  const inputRef = useRef(null);

  const openPicker = () => inputRef.current?.click();

  const onFile = async (f) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith('.zip')) {
      toast.error('Study package must be a .zip'); return;
    }
    setFile(f);
    setBusy(true);
    try {
      let newSid = sid;
      if (!newSid) {
        const r = await axios.post(`${API}/sessions`, { jurisdiction: 'us' });
        newSid = r.data.id;
        setSid(newSid);
      }
      const fd = new FormData(); fd.append('file', f);
      const rr = await axios.post(`${API}/sessions/${newSid}/intake`, fd);
      setManifest(rr.data);
      if (rr.data.exit_code === 0 || rr.data.status === 'ready') {
        toast.success('Intake accepted');
      } else {
        toast.error(`Intake exit ${rr.data.exit_code} — ${(rr.data.missing_components || []).join(', ') || 'see manifest'}`);
      }
    } catch (e) {
      toast.error(`Intake failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const runCorpus = async () => {
    setCorpusBusy(true);
    try {
      // Load catalog to pick the preset payload.
      const cat = await axios.get(`${API}/corpus/study/catalog`);
      const preset = (cat.data.presets || {})[corpusPreset];
      if (!preset) throw new Error('preset unavailable');
      const body = {
        scenario_id: preset.scenario_id,
        jurisdiction: 'us',
        edge_case_tags: preset.edge_case_tags || [],
        row_count: Math.max(1, Math.min(parseInt(rowCount, 10) || 6, 30)),
        seed: 42,
      };
      const r = await axios.post(`${API}/corpus/study/run`, body);
      setSid(r.data.session_id);
      setCorpusResult({
        session_id: r.data.session_id,
        summary: r.data.summary || {},
        scenario_id: r.data.scenario_id,
        edge_case_tags: r.data.edge_case_tags || [],
      });
      toast.success('Adversarial corpus launched — pipeline running');
    } catch (e) {
      toast.error(`Corpus launch failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setCorpusBusy(false); }
  };

  const canNext = corpusMode
    ? true // Corpus flow bypasses upload gate; next step becomes results view
    : (!!manifest && manifest.status === 'ready');
  const accepted = manifest?.accepted_by_component || {};

  return (
    <div className="step-in max-w-3xl">
      <div className="kicker">Step one</div>
      <h1 className="font-display text-display-lg text-ink mt-2">
        Upload the study package.
      </h1>
      <p className="text-body text-ink-2 mt-4 max-w-2xl">
        A single <span className="font-mono text-[13px]">.zip</span> containing a
        <span className="text-oxblood"> datasets/</span> folder and at least one of
        <span className="text-oxblood"> forms/</span> or
        <span className="text-oxblood"> dictionary/</span>. Intake fails closed on missing components.
      </p>

      <AccuracyStrip />

      {/* Corpus adversarial mode toggle -------------------------------- */}
      <div className="mt-10 rule-top pt-6" data-testid="corpus-mode-panel">
        <div className="flex items-start justify-between gap-6">
          <div className="max-w-xl">
            <div className="kicker">Optional · IRB torture test</div>
            <div className="font-display text-[19px] text-ink mt-1">
              Generate an adversarial corpus and run it through the same pipeline.
            </div>
            <p className="text-sm text-ink-2 leading-6 mt-2">
              The system plants synthetic PHI covering every HIPAA §164.514(b)(2)(i)
              identifier (A through R) plus deliberate edge cases (age &gt; 89,
              restricted ZIP3, name-in-notes, license-plate-shaped codes, IP in
              notes). The pipeline&apos;s job is to leave zero raw PHI in the
              exported bundle. The verifier then scores every plant.
            </p>
          </div>
          <label className="flex items-center gap-2 mt-1 cursor-pointer" data-testid="corpus-mode-toggle-label">
            <input
              type="checkbox"
              checked={corpusMode}
              onChange={e => setCorpusMode(e.target.checked)}
              data-testid="corpus-mode-toggle"
              className="accent-oxblood w-4 h-4"
            />
            <span className="font-mono text-[12px] uppercase tracking-wider text-ink-2">
              Enable
            </span>
          </label>
        </div>

        {corpusMode && (
          <div className="mt-6 grid grid-cols-2 gap-6" data-testid="corpus-mode-controls">
            <div>
              <div className="kicker">Preset</div>
              <select
                data-testid="corpus-preset"
                value={corpusPreset}
                onChange={e => setCorpusPreset(e.target.value)}
                className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink font-display focus:border-oxblood"
              >
                <option value="hipaa_max_adversarial">
                  HIPAA A-R + every torture edge case
                </option>
              </select>
              <div className="text-[11px] text-ink-muted mt-2">
                One preset ships today. It exercises the whole HIPAA A-R matrix in a single run.
              </div>
            </div>
            <div>
              <div className="kicker">Rows per dataset</div>
              <input
                type="number"
                min={1}
                max={30}
                data-testid="corpus-row-count"
                value={rowCount}
                onChange={e => setRowCount(e.target.value)}
                className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink font-display focus:border-oxblood"
              />
              <div className="text-[11px] text-ink-muted mt-2">
                6 rows is enough to cover every category. Increase for scale testing.
              </div>
            </div>
            <div className="col-span-2 flex items-center gap-3 pt-2">
              <Btn
                variant="primary"
                onClick={runCorpus}
                disabled={corpusBusy}
                testId="btn-run-corpus"
              >
                {corpusBusy ? 'Launching…' : 'Launch adversarial run →'}
              </Btn>
              {sid && corpusMode && (
                <span className="font-mono text-[11px] text-ink-muted">
                  session · {sid.slice(0, 12)}
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {!corpusMode && (
        <div
          className={`mt-12 border-2 border-dashed transition-all duration-200 py-16 px-10 text-center
            ${dragging ? 'border-oxblood bg-paper-2' : 'border-rule'}
            ${file ? 'border-clean' : ''}`}
          onDragOver={e => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={e => { e.preventDefault(); setDragging(false); onFile(e.dataTransfer.files?.[0]); }}
          data-testid="upload-dropzone"
        >
          <div className="kicker text-ink-muted">{busy ? 'Extracting & validating' : file ? 'Study package received' : 'Drop zip here'}</div>
          <div className="font-display text-display-md text-ink mt-3">
            {file ? file.name : 'or choose a file'}
          </div>
          {!file && <div className="text-sm text-ink-muted mt-3">Max 250 MB · ZIP only</div>}
          <div className="mt-8 flex items-center justify-center gap-3">
            <Btn onClick={openPicker} variant="default" testId="btn-choose-file">
              {file ? 'Replace file' : 'Choose file'}
            </Btn>
            {file && <Btn onClick={() => { setFile(null); setManifest(null); }} variant="ghost" testId="btn-clear-file">Remove</Btn>}
            <input ref={inputRef} type="file" accept=".zip" hidden onChange={e => onFile(e.target.files?.[0])} />
          </div>
        </div>
      )}

      {manifest && !corpusMode && (
        <div className="mt-10 rule-top pt-6" data-testid="intake-manifest">
          <div className="kicker mb-4">Intake manifest — v3</div>
          <div className="grid grid-cols-3 gap-6">
            {[
              { key: 'datasets', label: 'datasets/' },
              { key: 'forms', label: 'forms/' },
              { key: 'dictionary', label: 'dictionary/' },
            ].map(({ key, label }) => {
              const files = accepted[key] || [];
              const found = files.length > 0;
              return (
                <div key={key} className="flex items-start gap-3">
                  <div className={`w-4 h-4 mt-1 border ${found ? 'bg-clean border-clean' : 'border-rule'}`}>
                    {found && <svg viewBox="0 0 24 24" className="w-3 h-3 text-paper"><path d="M4 12 l5 5 l11 -12" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"/></svg>}
                  </div>
                  <div>
                    <div className={`font-mono text-sm ${found ? 'text-ink' : 'text-ink-muted'}`}>{label}</div>
                    <div className="text-[11px] text-ink-muted mt-0.5">
                      {found ? `${files.length} file(s) · ${files.map(f => f.name).join(', ').slice(0, 40)}${files.map(f => f.name).join(', ').length > 40 ? '…' : ''}` : 'not present'}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-6 flex items-center gap-3">
            <Tag color={canNext ? 'accept' : 'reject'}>{manifest.status}</Tag>
            <span className="text-[12px] text-ink-muted">
              exit {manifest.exit_code ?? '0'} · {manifest.linked ?? 0} file(s) linked
              {manifest.missing_components?.length ? ` · missing: ${manifest.missing_components.join(', ')}` : ''}
            </span>
          </div>
        </div>
      )}

      <div className="mt-16 flex items-center justify-end">
        <Btn variant="primary" size="lg" disabled={!canNext} onClick={onNext} testId="btn-next-step-2">
          {corpusMode ? 'Watch the corpus run →' : 'Configure the run →'}
        </Btn>
      </div>
    </div>
  );
}

// ---------- STEP 2 --------------------------------------------------------

function StepConfigure({ onNext, onBack, sid, config, setConfig }) {
  const [providers, setProviders] = useState([]);
  useEffect(() => {
    axios.get(`${API}/settings/llm`)
      .then(r => setProviders(r.data.providers || []))
      .catch(err => console.warn('load providers failed:', err));
    // Empty dep array intentional: fetch once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const canNext = config.reviewer.trim().length >= 2;

  // Sir Q "Sentinel Iteration Cap Tuner": three-lane rigor selector.
  // Fast=1 short-circuits after the first Sentinel pass; Balanced=2 is
  // the historical default; Thorough=3 lets Sentinel iterate up to the
  // hard cap. Cap ships to the /handle endpoint as ?iteration_cap=N.
  const rigorOptions = [
    { value: 1, label: 'Fast', blurb: '1 pass · short studies, high-confidence headers' },
    { value: 2, label: 'Balanced', blurb: '2 passes · default. Judge revises once if Sentinel flags a leak.' },
    { value: 3, label: 'Thorough', blurb: '3 passes · max defensibility, longest wallclock' },
  ];

  return (
    <div className="step-in max-w-3xl">
      <div className="kicker">Step two</div>
      <h1 className="font-display text-display-lg text-ink mt-2">Configure the run.</h1>
      <p className="text-body text-ink-2 mt-4 max-w-2xl">
        Three settings shape how the pipeline handles your data. Everything else
        is deterministic — the same input plus the same reviewer produces the same
        bundle every time.
      </p>

      <div className="mt-14 grid grid-cols-2 gap-x-16 gap-y-10">
        <div>
          <div className="kicker">Jurisdiction</div>
          <select
            data-testid="config-jurisdiction"
            value={config.jurisdiction}
            onChange={e => setConfig({ ...config, jurisdiction: e.target.value })}
            className="mt-3 w-full h-11 bg-transparent border-b border-ink text-ink text-lg font-display focus:border-oxblood"
          >
            <option value="us">United States — HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i)</option>
          </select>
          <div className="text-[12px] text-ink-muted mt-2">Additional jurisdictions ship in the next release.</div>
        </div>
        <div>
          <div className="kicker">LLM provider</div>
          <select
            data-testid="config-provider"
            value={config.provider}
            onChange={e => setConfig({ ...config, provider: e.target.value })}
            className="mt-3 w-full h-11 bg-transparent border-b border-ink text-ink text-lg font-display focus:border-oxblood"
          >
            {providers.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <div className="text-[12px] text-ink-muted mt-2">Default: Emergent proxy · headers only, no row data.</div>
        </div>
        <div>
          <div className="kicker">Reviewer identity <span className="text-oxblood">(required)</span></div>
          <input
            data-testid="config-reviewer"
            value={config.reviewer}
            onChange={e => setConfig({ ...config, reviewer: e.target.value })}
            placeholder="jane.doe@lab.edu"
            className="mt-3 w-full h-11 bg-transparent border-b border-ink text-ink text-lg font-display focus:border-oxblood"
          />
          <div className="text-[12px] text-ink-muted mt-2">Recorded on every decision the pipeline emits.</div>
        </div>
        <div>
          <div className="kicker">Comment <span className="text-ink-muted">(optional)</span></div>
          <input
            data-testid="config-comment"
            value={config.comment}
            onChange={e => setConfig({ ...config, comment: e.target.value })}
            placeholder="acceptance criteria, protocol reference, etc."
            className="mt-3 w-full h-11 bg-transparent border-b border-ink text-ink text-lg font-display focus:border-oxblood"
          />
          <div className="text-[12px] text-ink-muted mt-2">Free-text; persisted in the attestation.</div>
        </div>
      </div>

      <div className="mt-14">
        <div className="kicker">Rigor · Sentinel iteration cap</div>
        <div className="mt-3 grid grid-cols-3 gap-3" data-testid="config-rigor">
          {rigorOptions.map(opt => {
            const active = (config.iteration_cap || 2) === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => setConfig({ ...config, iteration_cap: opt.value })}
                data-testid={`config-rigor-${opt.label.toLowerCase()}`}
                className={`text-left border p-4 transition-colors ${active ? 'border-oxblood bg-paper-2' : 'border-rule hover:border-ink'}`}
              >
                <div className="flex items-baseline justify-between">
                  <div className={`font-display text-[16px] ${active ? 'text-oxblood' : 'text-ink'}`}>{opt.label}</div>
                  <div className="font-mono text-[11px] text-ink-muted">{opt.value} pass{opt.value === 1 ? '' : 'es'}</div>
                </div>
                <div className="text-[11px] text-ink-2 mt-1 leading-snug">{opt.blurb}</div>
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-20 flex items-center justify-between">
        <Btn variant="ghost" onClick={onBack} testId="btn-back-step-1">← back</Btn>
        <Btn variant="primary" size="lg" disabled={!canNext} onClick={onNext} testId="btn-next-step-3">
          Choose the output →
        </Btn>
      </div>
    </div>
  );
}

// ---------- STEP 3 --------------------------------------------------------

function StepOutput({ onBack, onRun, output, setOutput, busy }) {
  return (
    <div className="step-in max-w-3xl">
      <div className="kicker">Step three</div>
      <h1 className="font-display text-display-lg text-ink mt-2">Choose what you take home.</h1>
      <p className="text-body text-ink-2 mt-4 max-w-2xl">
        The default bundle contains the PHI-handled study files and a signed
        attestation. Optional add-ons turn the same run into a publication-ready
        artefact — figures, tables, methods and comparative coverage against every
        established de-identification tool.
      </p>

      <div className="mt-14">
        <CheckCard
          checked={true}
          locked={true}
          title="Safe-to-share files"
          blurb="PHI-handled datasets, forms, and dictionary; attestation.json + attestation.txt; README with HIPAA citations and reproduction steps."
          testId="check-safe"
        />
        <CheckCard
          checked={output.publication}
          onChange={v => setOutput({ ...output, publication: v })}
          title="Publication bundle"
          blurb="Adds a publication/ folder: HIPAA category coverage vs. Amazon Comprehend PHId, CliniDeID, NLM Scrubber, Presidio, MITRE MIST, GPT-4 (CSV + PNG heatmap + bar chart), draft methods/results/discussion sections, and BibTeX references."
          testId="check-publication"
        />
        <CheckCard
          checked={output.attestation_pdf}
          onChange={v => setOutput({ ...output, attestation_pdf: v })}
          title="Signed attestation PDF"
          blurb="A one-page PDF signed with the server's encryption key. Use it as a receipt when sharing the bundle with a reviewer or collaborator."
          testId="check-pdf"
        />
      </div>

      <div className="mt-20 flex items-center justify-between">
        <Btn variant="ghost" onClick={onBack} testId="btn-back-step-2">← back</Btn>
        <Btn variant="primary" size="lg" disabled={busy} onClick={onRun} testId="btn-run-pipeline">
          {busy ? 'Starting…' : 'Run the pipeline →'}
        </Btn>
      </div>
    </div>
  );
}

// ---------- Accuracy strip (wizard hero) ----------------------------------

function AccuracyStrip() {
  const [rep, setRep] = React.useState(null);
  React.useEffect(() => {
    axios.get(`${API}/classification-accuracy`)
      .then(r => setRep(r.data))
      .catch(err => console.warn('classification-accuracy load failed:', err));
    // Fetch once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!rep) return null;
  const pct = v => `${(v * 100).toFixed(1)}%`;
  return (
    <div className="mt-8 flex items-baseline gap-8 rule-top pt-4" data-testid="accuracy-strip">
      <div>
        <div className="kicker">Category accuracy</div>
        <div className="font-display text-display-md text-oxblood">{pct(rep.category_accuracy)}</div>
      </div>
      <div>
        <div className="kicker">Method appropriateness</div>
        <div className="font-display text-display-md text-oxblood">{pct(rep.action_accuracy)}</div>
      </div>
      <div>
        <div className="kicker">Columns validated</div>
        <div className="font-display text-display-md text-ink">{rep.total}</div>
      </div>
      <div className="text-[12px] text-ink-muted ml-auto max-w-xs">
        Deterministic hard-rule accuracy on the shipped labelled corpus. HIPAA A-R plus non-PHI keepers and free-text scrubbers.
      </div>
    </div>
  );
}


// ---------- WIZARD --------------------------------------------------------

export default function Wizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [sid, setSid] = useState(null);
  const [config, setConfig] = useState({
    jurisdiction: 'us', provider: 'emergent',
    reviewer: (typeof window !== 'undefined' && window.localStorage.getItem('phi_reviewer_id')) || '',
    comment: '',
    iteration_cap: 2,
  });
  const [output, setOutput] = useState({ publication: false, attestation_pdf: false });
  const [busy, setBusy] = useState(false);
  const [corpusMode, setCorpusMode] = useState(false);
  // corpus result state is stashed by StepUpload for future use; consumed via
  // navigation to /studies/{sid}?corpus=1 today. Setter kept so StepUpload
  // can hand its result to a future summary panel without another refactor.
  const [, setCorpusResult] = useState(null);

  const runPipeline = async () => {
    if (!sid) { toast.error('Upload a study package first'); setStep(1); return; }
    setBusy(true);
    try {
      try {
        window.localStorage.setItem('phi_reviewer_id', config.reviewer.trim());
        window.localStorage.setItem('phi_reviewer_comment', config.comment || '');
        window.localStorage.setItem('phi_output_options', JSON.stringify(output));
      } catch (err) {
        console.warn('reviewer state persistence failed:', err);
      }
      await axios.post(`${API}/sessions/${sid}/handle`, null, {
        params: { iteration_cap: config.iteration_cap || 2 },
      });
      navigate(`/studies/${sid}?bundle=${output.publication ? 'publication' : 'default'}${output.attestation_pdf ? '&pdf=1' : ''}`);
    } catch (e) {
      toast.error(`Run failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const onStepOneNext = () => {
    if (corpusMode) {
      if (!sid) {
        toast.error('Launch the adversarial run first');
        return;
      }
      // Corpus flow already POSTed /corpus/study/run which launches the
      // pipeline; jump straight to the session detail page so the operator
      // can watch classification -> anonymizing -> guard -> complete and
      // read the verifier report inline.
      navigate(`/studies/${sid}?corpus=1`);
      return;
    }
    setStep(2);
  };

  return (
    <div className="min-h-screen flex bg-paper text-ink">
      <ProgressRail step={step} />
      <main className="flex-1 px-10 pt-20 pb-32 max-w-5xl mx-auto lg:mx-0 lg:pl-20">
        {step === 1 && (
          <StepUpload
            onNext={onStepOneNext}
            sid={sid}
            setSid={setSid}
            corpusMode={corpusMode}
            setCorpusMode={setCorpusMode}
            setCorpusResult={setCorpusResult}
          />
        )}
        {step === 2 && <StepConfigure onBack={() => setStep(1)} onNext={() => setStep(3)}
                                       sid={sid} config={config} setConfig={setConfig} />}
        {step === 3 && <StepOutput onBack={() => setStep(2)} onRun={runPipeline}
                                    output={output} setOutput={setOutput} busy={busy} />}
      </main>
    </div>
  );
}

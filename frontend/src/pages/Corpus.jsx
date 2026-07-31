import React, { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { generateCorpus, listCorpora, getCorpus } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

export default function Corpus() {
  const [corpora, setCorpora] = useState([]);
  const [jur, setJur] = useState('us');
  const [seed, setSeed] = useState(20260420);
  const [count, setCount] = useState(3);
  const [quasi, setQuasi] = useState(true);
  const [busy, setBusy] = useState(false);
  const [current, setCurrent] = useState(null);

  const refresh = () => listCorpora().then(d => setCorpora(d.corpora || []));
  useEffect(() => { refresh(); }, []);

  const build = async () => {
    setBusy(true);
    try {
      const r = await generateCorpus({ jurisdiction: jur, seed: Number(seed), count_per_category: Number(count), include_quasi_identifiers: quasi });
      toast(`Generated ${r.total_records} records, ${r.total_gold_spans} gold spans`);
      const full = await getCorpus(r.id, 10);
      setCurrent(full);
      refresh();
    } catch (e) {
      toast(`Failed: ${e.message}`);
    } finally { setBusy(false); }
  };

  return (
    <div data-testid="corpus-page">
      <Panel title="Synthetic Corpus"
             cite="Legacy PHI-span corpus builder · for the IRB adversarial-run flow use the Wizard (New Run) instead"
             testId="corpus-panel">
        <div className="text-xs text-text-muted mb-4 leading-relaxed">
          Generate a small synthetic corpus of PHI-labelled records for span-detection benchmarking.
          To run the full 12-agent pipeline on a torture-test corpus with real Judge decisions and a
          publish-guard bundle, use the <span className="text-text-primary">New Run</span> wizard&apos;s
          <span className="text-text-primary"> IRB torture test</span> toggle instead.
        </div>
        <div className="grid grid-cols-4 gap-4">
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Jurisdiction</div>
            <select value={jur} onChange={e => setJur(e.target.value)} className="w-full h-9 bg-surface border border-border px-2 text-text-primary" data-testid="corpus-jur">
              <option value="us">US - HIPAA Safe Harbor</option>
              <option value="in" disabled>IN - DPDPA (planned)</option>
            </select>
          </label>
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Seed</div>
            <input type="number" value={seed} onChange={e => setSeed(e.target.value)} className="w-full h-9 bg-surface border border-border px-2 text-text-primary" data-testid="corpus-seed" />
          </label>
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Records / category</div>
            <input type="number" min={1} max={100} value={count} onChange={e => setCount(e.target.value)} className="w-full h-9 bg-surface border border-border px-2 text-text-primary" data-testid="corpus-count" />
          </label>
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Quasi-identifiers</div>
            <label className="flex items-center gap-2 h-9">
              <input type="checkbox" checked={quasi} onChange={e => setQuasi(e.target.checked)} data-testid="corpus-quasi" />
              <span className="text-text-primary">(b)(2)(ii) + Sweeney 2002</span>
            </label>
          </label>
        </div>
        <div className="mt-4"><Btn variant="primary" onClick={build} disabled={busy} testId="btn-generate-corpus">{busy ? 'Generating...' : 'Generate corpus'}</Btn></div>
      </Panel>

      <Panel title="Available Corpora" testId="corpora-list">
        {corpora.length === 0 && <div className="font-mono text-xs text-text-muted">no corpora yet</div>}
        <table className="w-full text-xs font-mono border border-border">
          <thead className="bg-surface">
            <tr>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">ID</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Seed</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Records</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Gold spans</th>
              <th className="text-left px-3 py-2 border-b border-border text-text-muted">Hash</th>
            </tr>
          </thead>
          <tbody>
            {corpora.map(c => (
              <tr key={c.id} data-testid={`corpus-row-${c.id}`} className="hover:bg-surface-2 cursor-pointer" onClick={() => getCorpus(c.id, 10).then(setCurrent)}>
                <td className="px-3 py-2 border-b border-r border-border text-text-primary">{c.id}</td>
                <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{c.seed}</td>
                <td className="px-3 py-2 border-b border-r border-border">{c.total_records}</td>
                <td className="px-3 py-2 border-b border-r border-border">{c.total_gold_spans}</td>
                <td className="px-3 py-2 border-b border-border text-text-muted">{c.hash.slice(0, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {current && (
        <Panel title={`Corpus preview - ${current.id}`} cite={`${current.records.length} sample records`} testId="corpus-preview">
          <div className="space-y-3">
            {current.records.map(r => (
              <div key={r.record_id} className="border border-border p-3" data-testid={`corpus-record-${r.record_id}`}>
                <div className="flex items-center gap-3 mb-2">
                  <Tag color="phi">{r.record_id}</Tag>
                  <span className="font-mono text-[10px] text-text-muted">{r.layer}</span>
                  <span className="font-mono text-[10px] text-text-muted">spans: {r.gold_spans.length}</span>
                </div>
                <div className="text-sm font-mono leading-relaxed whitespace-pre-wrap">
                  {renderWithSpans(r.text, r.gold_spans)}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

function renderWithSpans(text, spans) {
  if (!spans || spans.length === 0) return text;
  const sorted = [...spans].sort((a, b) => a.start - b.start);
  const parts = [];
  let cursor = 0;
  sorted.forEach((s, i) => {
    if (s.start > cursor) parts.push(<span key={`t-${i}`}>{text.slice(cursor, s.start)}</span>);
    parts.push(
      <span key={`s-${i}`} className="phi-highlight" title={`${s.hipaa_category || ''} ${s.entity_type} - ${s.authority}`}>
        {text.slice(s.start, s.end)}
      </span>
    );
    cursor = s.end;
  });
  if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>);
  return parts;
}

import React, { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { listCorpora, listBenchmarks, runBenchmark } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

export default function Benchmark() {
  const [corpora, setCorpora] = useState([]);
  const [benchmarks, setBenchmarks] = useState([]);
  const [selCorpus, setSelCorpus] = useState('');
  const [selDetectors, setSelDetectors] = useState({ presidio: true, rule: true });
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState(null);

  useEffect(() => {
    listCorpora().then(d => {
      setCorpora(d.corpora || []);
      if ((d.corpora || []).length > 0) setSelCorpus(d.corpora[0].id);
    });
    listBenchmarks().then(d => setBenchmarks(d.benchmarks || []));
  }, []);

  const run = async () => {
    if (!selCorpus) return;
    setBusy(true);
    try {
      const detectors = Object.keys(selDetectors).filter(k => selDetectors[k]);
      const r = await runBenchmark({ corpus_id: selCorpus, detectors });
      setLatest(r);
      toast(`Benchmark: P=${r.precision} R=${r.recall} F1=${r.f1}`);
      const list = await listBenchmarks();
      setBenchmarks(list.benchmarks || []);
    } catch (e) { toast(`Failed: ${e.message}`); } finally { setBusy(false); }
  };

  const chartData = latest ? Object.entries(latest.per_category).map(([cat, v]) => ({ cat, recall: v.recall, tp: v.tp, fn: v.fn })) : [];

  return (
    <div>
      <Panel title="Run Benchmark" cite="P / R / F1 vs gold spans" testId="benchmark-panel">
        <div className="grid grid-cols-3 gap-4">
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Corpus</div>
            <select value={selCorpus} onChange={e => setSelCorpus(e.target.value)} className="w-full h-9 bg-surface border border-border px-2 text-text-primary" data-testid="benchmark-corpus">
              {corpora.map(c => <option key={c.id} value={c.id}>{c.id} ({c.total_records}r)</option>)}
            </select>
          </label>
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Detectors</div>
            <div className="h-9 flex items-center gap-4">
              <label className="flex items-center gap-2"><input type="checkbox" checked={selDetectors.presidio} onChange={e => setSelDetectors({...selDetectors, presidio: e.target.checked})} data-testid="det-presidio" /> presidio</label>
              <label className="flex items-center gap-2"><input type="checkbox" checked={selDetectors.rule} onChange={e => setSelDetectors({...selDetectors, rule: e.target.checked})} data-testid="det-rule" /> rule</label>
            </div>
          </label>
          <div className="flex items-end">
            <Btn variant="primary" onClick={run} disabled={busy || !selCorpus} testId="btn-run-benchmark">{busy ? 'Running...' : 'Run benchmark'}</Btn>
          </div>
        </div>
      </Panel>

      {latest && (
        <Panel title="Latest Result" cite={latest.id} testId="benchmark-latest"
          right={<div className="flex gap-2"><Tag>P {latest.precision}</Tag><Tag>R {latest.recall}</Tag><Tag color="phi">F1 {latest.f1}</Tag></div>}>
          <div className="grid grid-cols-4 gap-2 text-xs font-mono">
            <div className="border border-border p-3"><div className="text-text-muted uppercase text-[10px] tracking-widest">Records</div><div className="text-lg text-text-primary">{latest.total_records}</div></div>
            <div className="border border-border p-3"><div className="text-text-muted uppercase text-[10px] tracking-widest">Gold spans</div><div className="text-lg text-text-primary">{latest.total_gold_spans}</div></div>
            <div className="border border-border p-3"><div className="text-text-muted uppercase text-[10px] tracking-widest">TP / FP / FN</div><div className="text-lg text-text-primary">{latest.tp} / {latest.fp} / {latest.fn}</div></div>
            <div className="border border-border p-3"><div className="text-text-muted uppercase text-[10px] tracking-widest">Detectors</div><div className="text-lg text-text-primary">{latest.detectors.join(' + ')}</div></div>
          </div>
          <div className="mt-4 border border-border h-80" data-testid="benchmark-chart">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData}>
                <CartesianGrid stroke="#272A31" strokeDasharray="0" />
                <XAxis dataKey="cat" stroke="#9CA3AF" tick={{ fontFamily: 'JetBrains Mono', fontSize: 10 }} />
                <YAxis stroke="#9CA3AF" tick={{ fontFamily: 'JetBrains Mono', fontSize: 10 }} domain={[0, 1]} />
                <Tooltip contentStyle={{ background: '#111317', border: '1px solid #272A31', borderRadius: 0, fontFamily: 'JetBrains Mono', fontSize: 11 }} />
                <Bar dataKey="recall" fill="#B45309" isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      )}

      <Panel title="History" testId="benchmark-history">
        <table className="w-full text-xs font-mono border border-border">
          <thead className="bg-surface">
            <tr>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">When</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Corpus</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Detectors</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">P</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">R</th>
              <th className="text-left px-3 py-2 border-b border-border text-text-muted">F1</th>
            </tr>
          </thead>
          <tbody>
            {benchmarks.map(b => (
              <tr key={b.id} data-testid={`benchmark-row-${b.id}`}>
                <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{b.created_at.slice(0, 19).replace('T', ' ')}</td>
                <td className="px-3 py-2 border-b border-r border-border text-text-primary">{b.corpus_id}</td>
                <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{(b.detectors || []).join('+')}</td>
                <td className="px-3 py-2 border-b border-r border-border">{b.precision}</td>
                <td className="px-3 py-2 border-b border-r border-border">{b.recall}</td>
                <td className="px-3 py-2 border-b border-border text-phi">{b.f1}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

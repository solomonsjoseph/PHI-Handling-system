import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, createSession, runSession } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

export default function NewStudy() {
  const nav = useNavigate();
  const [spec, setSpec] = useState(null);
  const [zip, setZip] = useState(null);
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState(null);
  const [drag, setDrag] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    axios.get(`${API}/intake/spec`).then(r => setSpec(r.data));
  }, []);

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    const list = Array.from(e.dataTransfer.files || []);
    if (list.length > 0) setZip(list[0]);
  };

  const submit = async () => {
    if (!zip) {
      toast('Select a .zip archive');
      return;
    }
    if (!zip.name.toLowerCase().endsWith('.zip')) {
      toast('Only .zip archives are accepted for intake');
      return;
    }
    setBusy(true);
    setReceipt(null);
    setError('');
    try {
      const s = await createSession('us');
      const fd = new FormData();
      fd.append('file', zip);
      const r = await axios.post(`${API}/sessions/${s.id}/intake`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      setReceipt({ ...r.data, session_id: s.id });
      if (r.data.status === 'ready') {
        await runSession(s.id);
        toast(`Study ${s.id.slice(0,8)} ready. Classification started.`);
        setTimeout(() => nav(`/studies/${s.id}`), 800);
      } else if (r.data.status === 'review_required') {
        setError(`Intake status: review_required (exit ${r.data.exit_code}). Fix the ${r.data.review} unclassified entries below and re-upload.`);
      } else {
        setError(`Intake failed (exit ${r.data.exit_code}): ${r.data.error || 'unknown'}`);
      }
    } catch (e) {
      setError(`${e?.response?.data?.detail || e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Panel title="New Study" cite="intake-manifest/v3" testId="new-study-panel">
        <div className="font-mono text-[11px] text-text-secondary mb-4">
          Upload a single .zip package. Datasets stay headers-only for the LLM per 45 CFR 164.514(b)(2)(i). Corpus generation is optional and lives under Experimental.
        </div>

        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          className={`p-10 border border-dashed ${drag ? 'border-text-primary bg-surface' : 'border-border'} text-center`}
          data-testid="zip-drop-zone"
        >
          <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted mb-3">Study Package (ZIP)</div>
          <div className="font-display text-xl mb-4">Drop your study .zip here, or select</div>
          <input
            type="file"
            accept=".zip,application/zip"
            onChange={(e) => setZip(e.target.files?.[0] || null)}
            className="block mx-auto text-xs font-mono text-text-secondary file:mr-4 file:px-3 file:py-2 file:border file:border-border file:bg-surface file:text-text-primary file:font-mono file:text-xs file:uppercase file:tracking-widest hover:file:bg-surface-2"
            data-testid="zip-file-input"
          />
          {zip && (
            <div className="mt-3 font-mono text-xs text-text-primary" data-testid="zip-file-name">
              {zip.name} <span className="text-text-muted">({zip.size} B)</span>
            </div>
          )}
        </div>

        <div className="mt-4 flex gap-3">
          <Btn variant="primary" onClick={submit} disabled={busy || !zip} testId="btn-submit-intake">
            {busy ? 'Submitting intake...' : 'Submit intake'}
          </Btn>
        </div>

        {error && (
          <div className="mt-4 border border-reject text-reject px-3 py-2 font-mono text-xs" data-testid="intake-error">{error}</div>
        )}

        {receipt && (
          <div className="mt-6 border border-border" data-testid="intake-receipt">
            <div className="h-9 px-4 border-b border-border bg-surface flex items-center gap-4 font-mono text-[10px] uppercase tracking-widest text-text-muted">
              Intake Receipt
              <Tag color={receipt.status === 'ready' ? 'accept' : receipt.status === 'review_required' ? 'phi' : 'reject'} testId="intake-receipt-status">{receipt.status} (exit {receipt.exit_code})</Tag>
              <span className="text-text-secondary">linked {receipt.linked}</span>
              <span className="text-text-secondary">review {receipt.review}</span>
            </div>
            <div className="p-4 font-mono text-xs">
              {receipt.missing_components?.length > 0 && (
                <div className="mb-3 border border-reject px-3 py-2 text-reject" data-testid="intake-missing">
                  Missing: {receipt.missing_components.join(', ')}
                </div>
              )}
              {receipt.review_entries?.length > 0 && (
                <div className="mb-3 border border-phi-border px-3 py-2 text-phi">
                  <div className="uppercase text-[10px] tracking-widest mb-1">Unclassified entries ({receipt.review_entries.length})</div>
                  {receipt.review_entries.map((e, i) => (
                    <div key={i} className="text-[11px]" data-testid={`intake-review-${i}`}>
                      <span className="text-text-primary">{e.relpath}</span> - <span className="text-text-secondary">{e.reason}</span>
                      {e.blocking && <span className="ml-2 text-reject">[BLOCKING]</span>}
                    </div>
                  ))}
                </div>
              )}
              <div className="grid grid-cols-4 gap-4">
                {Object.entries(receipt.accepted_by_component).map(([comp, items]) => (
                  <div key={comp} className="border border-border p-3" data-testid={`intake-comp-${comp}`}>
                    <div className="uppercase text-[10px] tracking-widest text-text-muted mb-1">{comp}</div>
                    <div className="text-2xl text-text-primary mb-2">{items.length}</div>
                    <div className="text-[10px] text-text-secondary space-y-1">
                      {items.slice(0, 5).map(it => (
                        <div key={it.file_id} className="truncate" title={`${it.name} - sha256:${it.sha256}`}>
                          <div className="text-text-primary">{it.name}</div>
                          <div className="text-text-muted">{it.size} B &middot; <span className="text-phi">{it.sha256}</span></div>
                        </div>
                      ))}
                      {items.length > 5 && <div>+{items.length - 5} more</div>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Panel>

      <Panel title="Manifest v3 - required structure" cite="45 CFR 164.514(b)(2)(i) headers-only" testId="manifest-spec-panel">
        {!spec && <div className="font-mono text-xs text-text-muted">loading spec...</div>}
        {spec && (
          <>
            <table className="w-full text-xs font-mono border border-border">
              <thead className="bg-surface">
                <tr>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Folder</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Required</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Accepted extensions</th>
                  <th className="text-left px-3 py-2 border-b border-border text-text-muted">LLM access</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(spec.components).map(([name, meta]) => (
                  <tr key={name} data-testid={`spec-row-${name}`}>
                    <td className="px-3 py-2 border-b border-r border-border text-text-primary">{name}/</td>
                    <td className="px-3 py-2 border-b border-r border-border">
                      {meta.required
                        ? <Tag color="reject">mandatory</Tag>
                        : <Tag color="phi">{meta.one_of_group ? 'one-of ' + meta.one_of_group : 'optional'}</Tag>}
                    </td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{meta.extensions.join(', ')}</td>
                    <td className="px-3 py-2 border-b border-border">
                      {name === 'datasets'
                        ? <span className="text-phi">headers only</span>
                        : <span className="text-text-secondary">full content</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-4 space-y-1 font-mono text-[11px]">
              <div className="text-text-muted uppercase tracking-widest text-[10px] mb-1">Rules</div>
              {spec.rules.map((r, i) => (
                <div key={i} className="text-text-secondary" data-testid={`spec-rule-${i}`}>&middot; {r}</div>
              ))}
            </div>
            <div className="mt-4 font-mono text-[11px]">
              <div className="text-text-muted uppercase tracking-widest text-[10px] mb-1">Exit codes</div>
              <div className="flex gap-3">
                {Object.entries(spec.exit_codes).map(([code, label]) => (
                  <Tag key={code} color={code === '0' ? 'accept' : code === '8' ? 'phi' : 'reject'}>{code} {label}</Tag>
                ))}
              </div>
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}

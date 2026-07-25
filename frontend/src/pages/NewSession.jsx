import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { createSession, runSession, uploadFile } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

export default function NewSession() {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);

  const onDrop = (e) => {
    e.preventDefault();
    setDrag(false);
    const list = Array.from(e.dataTransfer.files || []);
    setFiles(prev => [...prev, ...list]);
  };
  const onSelect = (e) => {
    const list = Array.from(e.target.files || []);
    setFiles(prev => [...prev, ...list]);
  };

  const kind = (name) => {
    const ext = (name.split('.').pop() || '').toLowerCase();
    if (['csv','tsv','xlsx','xls','parquet'].includes(ext)) return 'dataset';
    return 'narrative';
  };

  const startPipeline = async () => {
    if (files.length === 0) {
      toast('Select at least one file');
      return;
    }
    setBusy(true);
    try {
      const s = await createSession('us');
      for (const f of files) {
        await uploadFile(s.id, f);
      }
      await runSession(s.id);
      toast(`Session ${s.id.slice(0,8)} started. Streaming progress.`);
      nav(`/sessions/${s.id}`);
    } catch (e) {
      toast(`Failed to start: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Panel title="New Session" cite="Datasets: LLM reads headers only. Narratives: full content." testId="new-session-panel">
        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          className={`p-10 border border-dashed ${drag ? 'border-text-primary bg-surface' : 'border-border bg-transparent'} text-center`}
          data-testid="drop-zone"
        >
          <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted mb-3">Upload zone</div>
          <div className="font-display text-xl mb-4">Drop files here, or select</div>
          <input
            type="file"
            multiple
            onChange={onSelect}
            className="block mx-auto text-xs font-mono text-text-secondary file:mr-4 file:px-3 file:py-2 file:border file:border-border file:bg-surface file:text-text-primary file:font-mono file:text-xs file:uppercase file:tracking-widest hover:file:bg-surface-2"
            data-testid="file-input"
          />
          <div className="mt-4 font-mono text-[10px] text-text-muted">
            CSV, XLSX, Parquet, PDF, DOCX, TXT, EML, MD
          </div>
        </div>

        {files.length > 0 && (
          <div className="mt-6 border border-border" data-testid="staged-files">
            <div className="h-9 px-4 border-b border-border bg-surface flex items-center font-mono text-[10px] uppercase tracking-widest text-text-muted">
              Staged files ({files.length})
            </div>
            <table className="w-full text-xs font-mono">
              <tbody>
                {files.map((f, i) => (
                  <tr key={i} className="border-b border-border" data-testid={`staged-file-${i}`}>
                    <td className="px-3 py-2 border-r border-border text-text-primary">{f.name}</td>
                    <td className="px-3 py-2 border-r border-border"><Tag color={kind(f.name) === 'dataset' ? 'info' : 'default'}>{kind(f.name)}</Tag></td>
                    <td className="px-3 py-2 border-r border-border text-text-secondary">{f.size} B</td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={() => setFiles(files.filter((_, j) => j !== i))}
                        className="font-mono text-[10px] uppercase text-reject hover:underline"
                        data-testid={`remove-file-${i}`}
                      >
                        remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-6 flex gap-3">
          <Btn variant="primary" onClick={startPipeline} disabled={busy || files.length === 0} testId="btn-start-session">
            {busy ? 'Starting...' : 'Start PHI Pipeline'}
          </Btn>
        </div>
      </Panel>
    </div>
  );
}

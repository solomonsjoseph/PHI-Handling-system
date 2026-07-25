import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listSessions } from '../lib/api';
import { Panel, Btn, Tag } from '../components/ui';

export default function Sessions() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listSessions().then(d => setItems(d.sessions || [])).finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <Panel title="Sessions" cite="/api/sessions" testId="sessions-panel"
        right={<Link to="/sessions/new"><Btn variant="primary" testId="btn-new-session">New session</Btn></Link>}
      >
        {loading && <div className="font-mono text-xs text-text-muted">loading...</div>}
        {!loading && items.length === 0 && (
          <div className="font-mono text-xs text-text-muted">
            No sessions yet. Create one to upload files and run PHI handling.
          </div>
        )}
        {items.length > 0 && (
          <table className="w-full text-xs font-mono border border-border" data-testid="sessions-table">
            <thead>
              <tr className="bg-surface">
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">ID</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Status</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Jurisdiction</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Created</th>
                <th className="text-left px-3 py-2 border-b border-border text-text-muted">Files</th>
              </tr>
            </thead>
            <tbody>
              {items.map(s => (
                <tr key={s.id} data-testid={`session-row-${s.id}`} className="hover:bg-surface-2">
                  <td className="px-3 py-2 border-b border-r border-border">
                    <Link to={`/sessions/${s.id}`} className="text-text-primary underline decoration-dotted">
                      {s.id.slice(0, 12)}
                    </Link>
                  </td>
                  <td className="px-3 py-2 border-b border-r border-border"><Tag color={s.status === 'complete' ? 'accept' : s.status === 'failed' ? 'reject' : 'default'}>{s.status}</Tag></td>
                  <td className="px-3 py-2 border-b border-r border-border uppercase text-phi">{s.jurisdiction}</td>
                  <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{s.created_at.slice(0, 19).replace('T', ' ')}</td>
                  <td className="px-3 py-2 border-b border-border text-text-secondary">{(s.files || []).length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

import axios from 'axios';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
export const API = `${BACKEND_URL}/api`;

// SEC hardening 4.3: the operator token is exchanged once for an httponly,
// samesite=strict cookie set by the server (POST /api/auth/session). The
// browser never stores the raw token, and every request rides the cookie
// automatically via `withCredentials` -- no header or query param needed.
const api = axios.create({ baseURL: API, withCredentials: true });
axios.defaults.withCredentials = true;

// Exchanges the operator token for a session cookie. Pass an empty/falsy
// token to sign out instead.
export const setApiToken = async (t) => {
  if (!t) {
    await axios.post(`${API}/auth/logout`).catch(() => {});
    return;
  }
  await axios.post(`${API}/auth/session`, { token: t });
};

export const whoami = () => axios.get(`${API}/auth/whoami`).then(r => r.data).catch(() => null);

export const getSession = (id) => api.get(`/sessions/${id}`).then(r => r.data);

// EventSource sends same-origin cookies on its own; no query credential.
export const streamUrl = (sid) => `${API}/sessions/${sid}/stream`;

// Phase 11a frozen surface: `GET /api/sessions/{sid}/export/{file_id}`
// streams the PHI-handled export for one file directly (no JSON envelope),
// gated on that file's own Publish-Guard-clean result. Used as an anchor
// `href`, same pattern as the bundle/dataset-file downloads.
export const exportFileUrl = (sid, fileId) => `${API}/sessions/${sid}/export/${fileId}`;

// Phase 11a frozen surface: `GET /api/sessions/{sid}/reversal-key`. The
// closest existing "acknowledgment" analog -- a one-time, consuming
// download (the key is deleted server-side once served).
export const reversalKeyUrl = (sid) => `${API}/sessions/${sid}/reversal-key`;

// Phase 12: `POST /api/sessions/{sid}/acknowledge` -> { acknowledged,
// acknowledged_at, acknowledged_by }. Gated the same way bundle/export are
// (status complete/partially_complete + guard clean).
export const acknowledgeSession = (sid) =>
  axios.post(`${API}/sessions/${sid}/acknowledge`).then(r => r.data);

// Phase 12: `GET /api/sessions/{sid}/cleanup-status` -> { cleanup:
// <CleanupManifest-shaped dict|null>, export_expires_at: <iso|null> }.
// Returns null on any failure (including 404 on a backend that has not
// landed this endpoint yet) so callers can render nothing rather than
// break -- this mirrors the existing corpus-report/benchmark-report
// fetch pattern in `SessionDetail`.
export const getCleanupStatus = (sid) =>
  axios.get(`${API}/sessions/${sid}/cleanup-status`).then(r => r.data).catch(() => null);

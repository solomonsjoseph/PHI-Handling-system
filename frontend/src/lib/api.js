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

import axios from 'axios';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
export const API = `${BACKEND_URL}/api`;

// SEC-002: attach the operator API token (from localStorage) to every request.
// The token is set by the Settings page and lives only in the browser.
// localStorage is chosen over sessionStorage because operators need the token
// to persist across tab close. XSS surface is minimal — this app has no
// user-supplied HTML/JSX render path — but if that changes, migrate to an
// httpOnly cookie set by the server on token save.
const api = axios.create({ baseURL: API });
const _attachToken = (config) => {
  try {
    const token = window.localStorage.getItem('phi_api_token');
    if (token) {
      config.headers = config.headers || {};
      config.headers['X-API-Token'] = token;
    }
  } catch (err) {
    console.warn('phi_api_token read failed:', err);
  }
  return config;
};
api.interceptors.request.use(_attachToken);
// Also register on the default axios instance so raw `axios.get/post` calls
// used across pages/components still send the token.
axios.interceptors.request.use(_attachToken);
export const setApiToken = (t) => {
  try { window.localStorage.setItem('phi_api_token', t || ''); }
  catch (err) { console.warn('phi_api_token write failed:', err); }
};
export const getApiToken = () => {
  try { return window.localStorage.getItem('phi_api_token') || ''; }
  catch (err) { console.warn('phi_api_token read failed:', err); return ''; }
};

export const getSession = (id) => api.get(`/sessions/${id}`).then(r => r.data);

export const streamUrl = (sid) => {
  // EventSource can't send headers, so pass the operator token as query when set.
  let t = '';
  try { t = window.localStorage.getItem('phi_api_token') || ''; }
  catch (err) { console.warn('phi_api_token read failed:', err); }
  const qs = t ? `?token=${encodeURIComponent(t)}` : '';
  return `${API}/sessions/${sid}/stream${qs}`;
};

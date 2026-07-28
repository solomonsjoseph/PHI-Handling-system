import axios from 'axios';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';
export const API = `${BACKEND_URL}/api`;

// SEC-002: attach the operator API token (from localStorage) to every request.
// The token is set by the Settings page and lives only in the browser.
const api = axios.create({ baseURL: API });
const _attachToken = (config) => {
  try {
    const token = window.localStorage.getItem('phi_api_token');
    if (token) {
      config.headers = config.headers || {};
      config.headers['X-API-Token'] = token;
    }
  } catch (_) { /* no-op */ }
  return config;
};
api.interceptors.request.use(_attachToken);
// Also register on the default axios instance so raw `axios.get/post` calls
// used across pages/components still send the token.
axios.interceptors.request.use(_attachToken);
export const setApiToken = (t) => {
  try { window.localStorage.setItem('phi_api_token', t || ''); } catch (_) {}
};
export const getApiToken = () => {
  try { return window.localStorage.getItem('phi_api_token') || ''; } catch (_) { return ''; }
};

export const health = () => api.get('/health').then(r => r.data);

export const generateCorpus = (payload) => api.post('/corpus/generate', payload).then(r => r.data);
export const listCorpora = () => api.get('/corpus').then(r => r.data);
export const getCorpus = (id) => api.get(`/corpus/${id}`).then(r => r.data);

export const runBenchmark = (payload) => api.post('/benchmark/run', payload).then(r => r.data);
export const listBenchmarks = () => api.get('/benchmark').then(r => r.data);
export const getBenchmark = (id) => api.get(`/benchmark/${id}`).then(r => r.data);

export const createSession = (jurisdiction='us') => api.post('/sessions', { jurisdiction }).then(r => r.data);
export const listSessions = () => api.get('/sessions').then(r => r.data);
export const getSession = (id) => api.get(`/sessions/${id}`).then(r => r.data);

export const uploadFile = (sid, file) => {
  const fd = new FormData();
  fd.append('file', file);
  return api.post(`/sessions/${sid}/upload`, fd, { headers: { 'Content-Type': 'multipart/form-data' } }).then(r => r.data);
};

export const runSession = (sid) => api.post(`/sessions/${sid}/run`).then(r => r.data);
export const submitReview = (sid, decisions, add_manual_spans=[], continue_iteration=false) =>
  api.post(`/sessions/${sid}/review`, { decisions, add_manual_spans, continue_iteration }).then(r => r.data);
export const finalizeSession = (sid) => api.post(`/sessions/${sid}/finalize`).then(r => r.data);
export const exportUrl = (sid, fileId) => `${API}/sessions/${sid}/export/${fileId}`;

export const streamUrl = (sid) => `${API}/sessions/${sid}/stream`;

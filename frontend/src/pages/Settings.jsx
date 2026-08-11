import React, { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getApiToken, setApiToken } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

// Turn an ISO timestamp into "Mon 10 Feb 09:00 UTC (~2d 4h)" so operators
// see the auto-warmup schedule at a glance without decoding ISO strings.
function _formatSchedule(iso) {
  if (!iso) return 'monday 09:00 UTC';
  const then = new Date(iso);
  if (isNaN(then.getTime())) return iso;
  const wday = then.toLocaleDateString('en-US', { weekday: 'short', timeZone: 'UTC' });
  const day = then.toLocaleDateString('en-US', { day: '2-digit', month: 'short', timeZone: 'UTC' });
  const hm = then.toISOString().slice(11, 16);
  const diffMs = then.getTime() - Date.now();
  let delta = '';
  if (diffMs > 0) {
    const s = Math.floor(diffMs / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) delta = `~${d}d ${h}h`;
    else if (h > 0) delta = `~${h}h ${m}m`;
    else delta = `~${m}m`;
  } else {
    delta = 'due';
  }
  return `${wday} ${day} ${hm} UTC (${delta})`;
}

export default function Settings() {
  const [cfg, setCfg] = useState({
    provider: 'emergent',
    model: 'claude-sonnet-4-5-20250929',
    api_key: '',
    base_url: '',
    temperature: 0.1,
    max_tokens: 2000,
  });
  const [providers, setProviders] = useState([]);
  const [catalog, setCatalog] = useState({ providers: [], models: [], default_model_id: '' });
  const [apiKeySet, setApiKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [warming, setWarming] = useState(false);
  const [warmupResult, setWarmupResult] = useState(null);
  const [autoWarmup, setAutoWarmup] = useState({ enabled: false, last_run_at: null, last_run_status: null, next_run_at: null });
  const [opToken, setOpToken] = useState(getApiToken());
  const [chatgptStatus, setChatgptStatus] = useState({ connected: false, account_id: '' });
  const [chatgptLogin, setChatgptLogin] = useState(null);
  const [chatgptBusy, setChatgptBusy] = useState(false);

  const load = async () => {
    const [r, c, w] = await Promise.all([
      axios.get(`${API}/settings/llm`).then(r => r.data),
      axios.get(`${API}/settings/llm/catalog`).then(r => r.data),
      axios.get(`${API}/settings/warmup/schedule`).then(r => r.data).catch(() => null),
    ]);
    setProviders(r.providers || []);
    setCatalog(c);
    setApiKeySet(!!r.api_key_set);
    if (w) setAutoWarmup(w);
    const { providers: _p, api_key_set: _a, api_key: _k, ...rest } = r;
    setCfg(prev => ({ ...prev, ...rest }));
  };
  useEffect(() => {
    load();
    // `load` closes over stable setState setters; fetch once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadChatgptStatus = async () => {
    try {
      const r = await axios.get(`${API}/settings/chatgpt/status`);
      setChatgptStatus(r.data);
    } catch (e) { /* status probe is best-effort; leave prior state */ }
  };
  useEffect(() => {
    if (cfg.provider === 'chatgpt') loadChatgptStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.provider]);

  // Poll the pending device-code login at the server-declared cadence
  // until it resolves; cleared on connected/expired/error or unmount.
  useEffect(() => {
    if (!chatgptLogin) return undefined;
    const id = setInterval(async () => {
      try {
        const r = await axios.get(`${API}/settings/chatgpt/login/${chatgptLogin.login_id}`);
        if (r.data.status === 'connected') {
          clearInterval(id);
          setChatgptLogin(null);
          toast.success('ChatGPT account connected');
          await loadChatgptStatus();
          await load();
        } else if (r.data.status === 'expired' || r.data.status === 'error') {
          clearInterval(id);
          setChatgptLogin(null);
          toast.error(`ChatGPT login ${r.data.status}${r.data.detail ? `: ${r.data.detail}` : ''}`);
        }
      } catch (e) {
        clearInterval(id);
        toast.error(`ChatGPT login poll failed: ${e?.response?.data?.detail || e.message}`);
      }
    }, (chatgptLogin.interval_s || 5) * 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatgptLogin]);

  const chatgptConnect = async () => {
    setChatgptBusy(true);
    try {
      const r = await axios.post(`${API}/settings/chatgpt/login`);
      setChatgptLogin(r.data);
    } catch (e) {
      toast.error(`connect failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setChatgptBusy(false); }
  };

  const chatgptDisconnect = async () => {
    setChatgptBusy(true);
    try {
      await axios.delete(`${API}/settings/chatgpt`);
      setChatgptLogin(null);
      await loadChatgptStatus();
      toast('ChatGPT account disconnected');
    } catch (e) {
      toast.error(`disconnect failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setChatgptBusy(false); }
  };

  const saveOpToken = () => {
    setApiToken(opToken);
    toast(opToken ? 'Operator API token saved locally' : 'Operator API token cleared');
  };
  const save = async () => {
    setBusy(true);
    try {
      await axios.post(`${API}/settings/llm`, cfg);
      toast.success('LLM settings saved');
      await load();
    } catch (e) { toast.error(`save failed: ${e?.response?.data?.detail || e.message}`); }
    finally { setBusy(false); }
  };

  // Sir Q "Cold-Cache Warmup": pre-run Statute + all 17 Praxis categories
  // so the first study of the day doesn't eat the 10+ web-search cost
  // live. Takes ~30-60 s on cold cache, near-instant on warm.
  const warmCache = async () => {
    setWarming(true);
    setWarmupResult(null);
    try {
      const r = await axios.post(`${API}/settings/warmup`);
      setWarmupResult(r.data);
      const primed = r.data?.praxis?.primed?.length || 0;
      const total = r.data?.praxis?.total || 17;
      toast.success(`Cache primed: ${primed}/${total} Praxis methods + Statute (${r.data?.statute?.jurisdiction || 'us'})`);
    } catch (e) {
      toast.error(`warmup failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setWarming(false); }
  };

  // Sir Q "Warmup Auto-Prime": weekly Monday 09:00 UTC background warmup.
  const toggleAutoWarmup = async (enabled) => {
    try {
      const r = await axios.post(`${API}/settings/warmup/schedule`, { enabled });
      setAutoWarmup(prev => ({ ...prev, enabled: r.data.enabled, next_run_at: r.data.next_run_at }));
      toast(enabled ? 'Auto-warmup on · Mondays 09:00 UTC' : 'Auto-warmup off');
    } catch (e) {
      toast.error(`schedule update failed: ${e?.response?.data?.detail || e.message}`);
    }
  };

  // ------- provider / model picker helpers ------------------------------
  //
  // The provider dropdown drives the model dropdown. When the user picks
  // "emergent" the model list contains all catalog entries flagged as
  // via_emergent_key. For BYOK providers (anthropic/openai/gemini/etc.)
  // the model list filters by provider_family so the user only sees
  // models that provider actually serves.

  const providerFamilies = catalog.providers || [];
  const modelsForProvider = useMemo(() => {
    const all = catalog.models || [];
    if (cfg.provider === 'emergent') return all.filter(m => m.via_emergent_key);
    if (cfg.provider === 'openai_compatible') return [];  // free-form only
    return all.filter(m => m.provider_family === cfg.provider);
  }, [catalog, cfg.provider]);

  const selectedModel = useMemo(
    () => (catalog.models || []).find(m => m.id === cfg.model),
    [catalog, cfg.model],
  );

  const providerOptions = useMemo(() => {
    // Server-allowed providers only. `emergent` is included by the server
    // when the pod has EMERGENT_LLM_KEY set; on self-hosted deploys that
    // don't have it, the option is hidden so operators aren't led to a
    // path that will fail on first call.
    return Array.from(new Set(providers));
  }, [providers]);

  const providerLabel = (pid) => {
    if (pid === 'emergent') return 'Emergent Universal Key (Claude · GPT · Gemini)';
    const fam = providerFamilies.find(p => p.id === pid);
    return fam ? fam.label : pid;
  };

  const NEEDS_KEY = cfg.provider !== 'emergent' && cfg.provider !== 'chatgpt';
  const NEEDS_BASE = cfg.provider === 'openai_compatible';

  const Field = ({ label, children, hint, required }) => (
    <div>
      <div className="kicker">{label} {required && <span className="text-oxblood">(required)</span>}</div>
      <div className="mt-2">{children}</div>
      {hint && <div className="text-[12px] text-ink-muted mt-1.5">{hint}</div>}
    </div>
  );
  const input = 'w-full h-11 bg-transparent border-b border-ink text-ink text-[15px] focus:border-oxblood';

  const TIER_TAG = { flagship: 'accent', balanced: 'default', fast: 'ink', reasoning: 'accent' };

  return (
    <div className="max-w-4xl mx-auto px-10 py-16">
      <div className="kicker">Configuration</div>
      <h1 className="font-display text-display-lg text-ink mt-2">Settings</h1>
      <p className="text-body text-ink-2 mt-4 max-w-2xl">
        Pick the AI/LLM every one of the twelve agents will use. Zero-setup via the Emergent Universal Key or bring your own key.
      </p>

      <Panel title="Operator token" cite="only required when API_TOKEN is set on the server" testId="settings-token-panel">
        <div className="grid grid-cols-2 gap-x-16 gap-y-6">
          <Field label={<>X-API-Token {opToken && <Tag color="accept">set</Tag>}</>}
                 hint="Stored in browser localStorage. Sent with every mutating call.">
            <input type="password" value={opToken} onChange={e => setOpToken(e.target.value)}
                   placeholder="paste the server-side API_TOKEN"
                   data-testid="settings-op-token" className={input}/>
          </Field>
          <div className="flex items-end">
            <Btn variant="primary" onClick={saveOpToken} testId="btn-save-token">Save token</Btn>
          </div>
        </div>
      </Panel>

      <Panel title="LLM provider & model" cite="every agent uses this model" testId="settings-llm-panel">
        <div className="grid grid-cols-2 gap-x-16 gap-y-10">
          <Field label="Provider"
                 hint={cfg.provider === 'emergent'
                   ? 'Uses the Emergent Universal Key. Zero-setup. Routes to Claude / GPT / Gemini based on the model you pick below.'
                   : 'Bring your own key. Fernet-encrypted at rest.'}>
            <select value={cfg.provider}
                    onChange={e => {
                      const nextProvider = e.target.value;
                      // Reset model when switching provider if the current model
                      // isn't valid for the new provider family.
                      const models = (catalog.models || []).filter(m =>
                        nextProvider === 'emergent'
                          ? m.via_emergent_key
                          : m.provider_family === nextProvider
                      );
                      const nextModel = models.some(m => m.id === cfg.model)
                        ? cfg.model
                        : (models[0]?.id || cfg.model);
                      setCfg({ ...cfg, provider: nextProvider, model: nextModel });
                    }}
                    data-testid="settings-provider" className={input}>
              {providerOptions.map(p => (
                <option key={p} value={p}>{providerLabel(p)}</option>
              ))}
            </select>
          </Field>

          <Field label="Model"
                 hint={selectedModel?.notes || (cfg.provider === 'openai_compatible'
                   ? 'Type the model name your endpoint accepts.'
                   : 'Pick a model from the catalog below.')}>
            {modelsForProvider.length > 0 ? (
              <select value={cfg.model} onChange={e => setCfg({ ...cfg, model: e.target.value })}
                      data-testid="settings-model" className={input}>
                {modelsForProvider.map(m => (
                  <option key={m.id} value={m.id}>
                    {m.label} — {m.tier}{m.supports_web_search ? ' · web-search' : ''}
                  </option>
                ))}
              </select>
            ) : (
              <input value={cfg.model} onChange={e => setCfg({ ...cfg, model: e.target.value })}
                     data-testid="settings-model" className={input}
                     placeholder="type model name (e.g. mistral-large-latest)"/>
            )}
          </Field>

          {NEEDS_KEY && (
            <Field label={<>API key {apiKeySet && <Tag color="accept">set</Tag>}</>}
                   hint="Encrypted at rest. Leave blank to keep existing.">
              <input type="password" value={cfg.api_key} onChange={e => setCfg({ ...cfg, api_key: e.target.value })}
                     placeholder={apiKeySet ? 'leave blank to keep existing' : 'paste your key'}
                     data-testid="settings-api-key" className={input}/>
            </Field>
          )}
          {NEEDS_BASE && (
            <Field label="Base URL" hint="Only https and admin-allow-listed hosts accepted (SEC-003).">
              <input value={cfg.base_url} onChange={e => setCfg({ ...cfg, base_url: e.target.value })}
                     placeholder="https://your-endpoint.example.com/v1"
                     data-testid="settings-base-url" className={input}/>
            </Field>
          )}
          <Field label="Temperature">
            <input type="number" min={0} max={2} step={0.05}
                   value={cfg.temperature} onChange={e => setCfg({ ...cfg, temperature: Number(e.target.value) })}
                   data-testid="settings-temperature" className={input}/>
          </Field>
          <Field label="Max tokens">
            <input type="number" min={200} max={16000} step={100}
                   value={cfg.max_tokens} onChange={e => setCfg({ ...cfg, max_tokens: Number(e.target.value) })}
                   data-testid="settings-max-tokens" className={input}/>
          </Field>
        </div>

        <div className="mt-10 flex items-center gap-3 flex-wrap">
          <Btn variant="primary" onClick={save} disabled={busy} testId="btn-save-settings">
            {busy ? 'Saving…' : 'Save settings'}
          </Btn>
          <Tag color="accent">{cfg.provider} · {(selectedModel?.label) || cfg.model}</Tag>
          {selectedModel?.supports_web_search && (
            <Tag color="accept" testId="tag-supports-web-search">web-search enabled</Tag>
          )}
          {selectedModel && selectedModel.tier && (
            <Tag color={TIER_TAG[selectedModel.tier] || 'default'}>{selectedModel.tier}</Tag>
          )}
        </div>

        {/* Cold-cache warmup: prime Statute + Praxis so the first study of
            the day doesn't pay for 10+ web searches live. */}
        <div className="mt-8 rule-top pt-6" data-testid="warmup-panel">
          <div className="kicker">Cold-cache warmup</div>
          <div className="mt-2 text-[12px] text-ink-2 max-w-2xl">
            Pre-runs the Statute rulebook fetch and all 17 Praxis method lookups so the first
            live study skips the web-search cost. Cache refreshes weekly; you only need to
            press this after a fresh deploy or when Praxis says "cold" in the trace.
          </div>
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <Btn variant="secondary" onClick={warmCache} disabled={warming} testId="btn-warmup-cache">
              {warming ? 'Priming cache… (up to 60 s)' : 'Prime Praxis + Statute'}
            </Btn>
            {warmupResult && (
              <div className="font-mono text-[11px] text-ink-2" data-testid="warmup-result">
                Statute: {warmupResult.statute?.jurisdiction || 'us'} · Praxis: {warmupResult.praxis?.primed?.length || 0}/{warmupResult.praxis?.total || 17} primed
                {warmupResult.praxis?.failed?.length > 0 && (
                  <span className="text-oxblood ml-2">· {warmupResult.praxis.failed.length} failed</span>
                )}
              </div>
            )}
          </div>

          {/* Auto-prime toggle: fires the warmup every Monday 09:00 UTC. */}
          <div className="mt-6 flex items-start gap-3" data-testid="auto-warmup-toggle">
            <input
              type="checkbox"
              id="auto-warmup"
              checked={!!autoWarmup.enabled}
              onChange={e => toggleAutoWarmup(e.target.checked)}
              className="mt-1"
              data-testid="auto-warmup-checkbox"
            />
            <label htmlFor="auto-warmup" className="text-[12px] text-ink-2 leading-snug">
              <span className="font-display text-ink text-[13px]">Auto-prime every Monday 09:00 UTC</span>
              <div className="text-ink-muted mt-0.5 font-mono text-[11px]" data-testid="auto-warmup-schedule">
                {autoWarmup.enabled ? (
                  <>next run: {_formatSchedule(autoWarmup.next_run_at)}</>
                ) : (
                  <>off · flip on to keep the cache hot through the workweek</>
                )}
                {autoWarmup.last_run_at && (
                  <span className="ml-2">
                    · last: {new Date(autoWarmup.last_run_at).toISOString().slice(0, 16).replace('T', ' ')} UTC
                    {autoWarmup.last_run_status && ` (${autoWarmup.last_run_status})`}
                  </span>
                )}
              </div>
            </label>
          </div>
        </div>

        {/* Catalog reference — helps operators see the full menu at a glance */}
        {(catalog.models || []).length > 0 && (
          <div className="mt-10 rule-top pt-6" data-testid="catalog-summary">
            <div className="kicker">Available models</div>
            <div className="mt-3 grid grid-cols-2 gap-x-8 gap-y-2 text-[12px] font-mono">
              {(catalog.models || []).map(m => (
                <div key={m.id} className={`data-cell flex items-center gap-2 ${cfg.model === m.id ? 'text-oxblood' : 'text-ink-2'}`}
                     data-testid={`catalog-row-${m.id}`}>
                  <span className="truncate flex-1">{m.label}</span>
                  <span className="opacity-60">{m.tier}</span>
                  {m.supports_web_search && <span className="text-oxblood">·ws</span>}
                  {m.via_emergent_key && <span className="opacity-60">·emergent</span>}
                </div>
              ))}
            </div>
          </div>
        )}
      </Panel>

      {cfg.provider === 'chatgpt' && (
        <Panel title="ChatGPT account" cite="OAuth device code, no pasted key" testId="settings-chatgpt-panel">
          <div className="flex items-center gap-3" data-testid="chatgpt-status">
            {chatgptStatus.connected ? (
              <>
                <Tag color="accept">connected</Tag>
                <span className="font-mono text-[12px] text-ink-2">{chatgptStatus.account_id}</span>
              </>
            ) : (
              <Tag>not connected</Tag>
            )}
          </div>

          {chatgptLogin && (
            <div className="mt-6 rule-top pt-6">
              <div className="kicker">Enter this code at the link below</div>
              <div className="mt-2 font-mono text-[28px] tracking-[0.3em] text-oxblood" data-testid="chatgpt-user-code">
                {chatgptLogin.user_code}
              </div>
              <a href={chatgptLogin.verify_url} target="_blank" rel="noreferrer"
                 className="mt-2 inline-block text-[13px] underline text-ink-2">
                {chatgptLogin.verify_url}
              </a>
              <p className="mt-3 text-[12px] text-ink-muted max-w-md">
                Device codes are a common phishing target. Never share this code.
              </p>
            </div>
          )}

          <div className="mt-6 flex items-center gap-3 flex-wrap">
            <Btn variant="primary" onClick={chatgptConnect} disabled={chatgptBusy} testId="btn-chatgpt-connect">
              Connect ChatGPT account
            </Btn>
            <Btn onClick={chatgptDisconnect} disabled={chatgptBusy} testId="btn-chatgpt-disconnect">
              Disconnect
            </Btn>
          </div>
        </Panel>
      )}
    </div>
  );
}

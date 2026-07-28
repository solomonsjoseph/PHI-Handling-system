import React, { useEffect, useState } from 'react';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getApiToken, setApiToken } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

export default function Settings() {
  const [cfg, setCfg] = useState({ provider: 'emergent', model: 'claude-sonnet-4-5-20250929', api_key: '', base_url: '', temperature: 0.1, max_tokens: 2000 });
  const [providers, setProviders] = useState([]);
  const [apiKeySet, setApiKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [opToken, setOpToken] = useState(getApiToken());

  const load = async () => {
    const r = await axios.get(`${API}/settings/llm`).then(r => r.data);
    setProviders(r.providers || []);
    setApiKeySet(!!r.api_key_set);
    const { providers: _p, api_key_set: _a, api_key: _k, ...rest } = r;
    setCfg(prev => ({ ...prev, ...rest }));
  };
  useEffect(() => { load(); }, []);

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

  const MODEL_HINT = {
    emergent: 'claude-sonnet-4-5-20250929 (Emergent Universal Key)',
    anthropic: 'claude-sonnet-4-5-20250929 or claude-opus-*',
    openai: 'gpt-4o or gpt-4o-mini',
    gemini: 'gemini/gemini-2.5-flash or gemini/gemini-2.5-pro',
    openrouter: 'openrouter/anthropic/claude-sonnet-4.5 or openrouter/openai/gpt-4o',
    openai_compatible: 'model name your endpoint accepts',
  }[cfg.provider] || '';

  const NEEDS_KEY = cfg.provider !== 'emergent';
  const NEEDS_BASE = cfg.provider === 'openai_compatible';

  const Field = ({ label, children, hint, required }) => (
    <div>
      <div className="kicker">{label} {required && <span className="text-oxblood">(required)</span>}</div>
      <div className="mt-2">{children}</div>
      {hint && <div className="text-[12px] text-ink-muted mt-1.5">{hint}</div>}
    </div>
  );
  const input = 'w-full h-11 bg-transparent border-b border-ink text-ink text-[15px] focus:border-oxblood';

  return (
    <div className="max-w-4xl mx-auto px-10 py-16">
      <div className="kicker">Configuration</div>
      <h1 className="font-display text-display-lg text-ink mt-2">Settings</h1>
      <p className="text-body text-ink-2 mt-4 max-w-2xl">
        The LLM model chosen here is used by every one of the twelve agents. The operator
        token gates every mutating call when the server sets <span className="font-mono text-[13px]">API_TOKEN</span>.
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

      <Panel title="LLM provider" cite="every agent uses this model" testId="settings-llm-panel">
        <div className="grid grid-cols-2 gap-x-16 gap-y-10">
          <Field label="Provider" hint={cfg.provider === 'emergent' ? 'Uses the Emergent Universal Key. Zero-setup.' : 'Bring your own key. Stored Fernet-encrypted.'}>
            <select value={cfg.provider} onChange={e => setCfg({ ...cfg, provider: e.target.value })}
                    data-testid="settings-provider" className={input}>
              {providers.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </Field>
          <Field label="Model" hint={MODEL_HINT}>
            <input value={cfg.model} onChange={e => setCfg({ ...cfg, model: e.target.value })}
                   data-testid="settings-model" className={input}/>
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
        <div className="mt-10 flex items-center gap-4">
          <Btn variant="primary" onClick={save} disabled={busy} testId="btn-save-settings">
            {busy ? 'Saving…' : 'Save settings'}
          </Btn>
          <Tag color="accent">{cfg.provider} · {cfg.model.split('/').slice(-1)[0]}</Tag>
        </div>
      </Panel>
    </div>
  );
}

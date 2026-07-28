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
      toast('LLM settings saved');
      await load();
    } catch (e) { toast(`save failed: ${e?.response?.data?.detail || e.message}`); }
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

  return (
    <div>
      <Panel title="Operator API Token" cite="required only when the server sets API_TOKEN" testId="settings-token-panel">
        <div className="grid grid-cols-2 gap-4">
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">
              X-API-Token {opToken && <span className="text-accept">(set)</span>}
            </div>
            <input
              type="password"
              value={opToken}
              onChange={e => setOpToken(e.target.value)}
              placeholder="paste the server-side API_TOKEN value"
              data-testid="settings-op-token"
              className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
            />
            <div className="text-[10px] text-text-muted mt-1">
              Stored in browser localStorage only. Sent as <code>X-API-Token</code> on every mutating call.
              Leave blank when the server has not configured <code>API_TOKEN</code>.
            </div>
          </label>
          <div className="flex items-end">
            <Btn variant="primary" onClick={saveOpToken} testId="btn-save-token">Save token</Btn>
          </div>
        </div>
      </Panel>

      <Panel title="LLM Provider" cite="Every one of the 12 agents will use this model" testId="settings-llm-panel">
        <div className="grid grid-cols-2 gap-4">
          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Provider</div>
            <select
              value={cfg.provider}
              onChange={e => setCfg({ ...cfg, provider: e.target.value })}
              data-testid="settings-provider"
              className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
            >
              {providers.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
            <div className="text-[10px] text-text-muted mt-1">
              {cfg.provider === 'emergent' && 'Uses the Emergent Universal Key already configured on the server. No user API key needed.'}
              {cfg.provider !== 'emergent' && 'Bring your own API key. Stored in the local database.'}
            </div>
          </label>

          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Model</div>
            <input
              value={cfg.model}
              onChange={e => setCfg({ ...cfg, model: e.target.value })}
              data-testid="settings-model"
              className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
            />
            <div className="text-[10px] text-text-muted mt-1">{MODEL_HINT}</div>
          </label>

          {NEEDS_KEY && (
            <label className="block text-xs font-mono">
              <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">
                API Key {apiKeySet && <span className="text-accept">(currently set)</span>}
              </div>
              <input
                type="password"
                value={cfg.api_key}
                onChange={e => setCfg({ ...cfg, api_key: e.target.value })}
                placeholder={apiKeySet ? 'leave blank to keep existing' : 'paste your api key'}
                data-testid="settings-api-key"
                className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
              />
            </label>
          )}

          {NEEDS_BASE && (
            <label className="block text-xs font-mono">
              <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Base URL</div>
              <input
                value={cfg.base_url}
                onChange={e => setCfg({ ...cfg, base_url: e.target.value })}
                placeholder="https://your-endpoint.example.com/v1"
                data-testid="settings-base-url"
                className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
              />
            </label>
          )}

          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Temperature</div>
            <input
              type="number" min={0} max={2} step={0.05}
              value={cfg.temperature}
              onChange={e => setCfg({ ...cfg, temperature: Number(e.target.value) })}
              data-testid="settings-temperature"
              className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
            />
          </label>

          <label className="block text-xs font-mono">
            <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Max tokens</div>
            <input
              type="number" min={200} max={16000} step={100}
              value={cfg.max_tokens}
              onChange={e => setCfg({ ...cfg, max_tokens: Number(e.target.value) })}
              data-testid="settings-max-tokens"
              className="w-full h-9 bg-surface border border-border px-2 text-text-primary"
            />
          </label>
        </div>

        <div className="mt-6 flex gap-3">
          <Btn variant="primary" onClick={save} disabled={busy} testId="btn-save-settings">
            {busy ? 'Saving...' : 'Save Settings'}
          </Btn>
          <Tag color="phi" testId="settings-provider-current">{cfg.provider} - {cfg.model.split('/').slice(-1)[0]}</Tag>
        </div>
      </Panel>

      <Panel title="Notes" testId="settings-notes-panel">
        <div className="font-mono text-xs text-text-secondary space-y-2">
          <div><span className="text-phi">Emergent</span>: uses the local EMERGENT_LLM_KEY. Zero setup for Sir on this server.</div>
          <div><span className="text-phi">Anthropic</span>: direct Anthropic keys. Best latency on Claude models.</div>
          <div><span className="text-phi">OpenAI</span>: direct OpenAI keys.</div>
          <div><span className="text-phi">Gemini</span>: direct Google AI keys.</div>
          <div><span className="text-phi">OpenRouter</span>: routes to almost any provider through openrouter.ai. Set model as `openrouter/&lt;model&gt;`.</div>
          <div><span className="text-phi">OpenAI-compatible</span>: for local models (vLLM, LM Studio, Ollama) exposing an OpenAI-style API. Set Base URL to your endpoint.</div>
          <div className="pt-3 text-text-muted">The chosen model is called by every one of the 12 agents. Cheaper models save cost but may reduce accuracy of Judge, Sentinel, Auditor, and Herald.</div>
        </div>
      </Panel>
    </div>
  );
}

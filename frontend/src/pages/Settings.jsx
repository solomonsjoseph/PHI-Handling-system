import React, { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getApiToken, setApiToken } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

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
  const [opToken, setOpToken] = useState(getApiToken());

  const load = async () => {
    const [r, c] = await Promise.all([
      axios.get(`${API}/settings/llm`).then(r => r.data),
      axios.get(`${API}/settings/llm/catalog`).then(r => r.data),
    ]);
    setProviders(r.providers || []);
    setCatalog(c);
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
    // Merge server-allowed providers with catalog families to preserve
    // server-side validation (SEC-003 provider allow-list).
    const base = new Set(providers);
    // 'emergent' is always available (Emergent Universal Key)
    base.add('emergent');
    return Array.from(base);
  }, [providers]);

  const providerLabel = (pid) => {
    if (pid === 'emergent') return 'Emergent Universal Key (Claude · GPT · Gemini)';
    const fam = providerFamilies.find(p => p.id === pid);
    return fam ? fam.label : pid;
  };

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
    </div>
  );
}

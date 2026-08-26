import React, { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import axios from 'axios';
import { API } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

const FALLBACK_PROVIDERS = [
  { id: 'openrouter', label: 'Open Router' },
  { id: 'openai', label: 'ChatGPT' },
  { id: 'anthropic', label: 'Claude' },
  { id: 'gemini', label: 'Gemini' },
];

export default function Settings() {
  const [cfg, setCfg] = useState({
    provider: 'openai',
    model: 'gpt-5.2',
    temperature: 0.1,
    max_tokens: 2000,
  });
  const [catalog, setCatalog] = useState({ providers: [], models: [], default_model_id: '' });
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [customModel, setCustomModel] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [apiKeySet, setApiKeySet] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);
  const [baseUrl, setBaseUrl] = useState('');
  const CUSTOM_VALUE = '__custom__';

  const load = async () => {
    setLoadError('');
    try {
      const [r, c] = await Promise.all([
        axios.get(`${API}/settings/llm`),
        axios.get(`${API}/settings/llm/catalog`),
      ]);
      setCatalog(c.data);
      const nextProvider = r.data.provider || 'openai';
      const models = (c.data.models || []).filter(m => m.provider_family === nextProvider);
      const knownModel = models.some(m => m.id === r.data.model);
      const nextModel = knownModel
        ? r.data.model
        : (r.data.model || models[0]?.id || c.data.default_model_id || '');
      setCustomModel(!knownModel && !!r.data.model);
      setCfg({
        provider: nextProvider,
        model: nextModel,
        temperature: Number(r.data.temperature ?? 0.1),
        max_tokens: Number(r.data.max_tokens ?? 2000),
      });
      setApiKeySet(!!r.data.api_key_set);
      setApiKey('');
      setShowApiKey(false);
      setBaseUrl(r.data.base_url || '');
    } catch (e) {
      const detail = e?.response?.data?.detail || e.message;
      setLoadError(detail);
      toast.error(`settings load failed: ${detail}`);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    if (!cfg.model) {
      toast.error('select a model before saving');
      return;
    }
    setBusy(true);
    try {
      await axios.post(`${API}/settings/llm`, {
        provider: cfg.provider,
        model: cfg.model,
        temperature: cfg.temperature,
        max_tokens: cfg.max_tokens,
        api_key: apiKey,
        base_url: baseUrl,
      });
      toast.success('LLM settings saved');
      await load();
    } catch (e) {
      toast.error(`save failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const providerOptions = useMemo(() => {
    const fromCatalog = catalog.providers || [];
    return fromCatalog.length ? fromCatalog : FALLBACK_PROVIDERS;
  }, [catalog]);

  const modelsForProvider = useMemo(() => {
    return (catalog.models || []).filter(m => m.provider_family === cfg.provider);
  }, [catalog, cfg.provider]);

  const selectedModel = useMemo(
    () => (catalog.models || []).find(m => m.id === cfg.model),
    [catalog, cfg.model],
  );

  const providerLabel = (pid) => {
    const fam = providerOptions.find(p => p.id === pid);
    return fam ? fam.label : pid;
  };

  const onProviderChange = (nextProvider) => {
    const models = (catalog.models || []).filter(m => m.provider_family === nextProvider);
    const nextModel = models.some(m => m.id === cfg.model)
      ? cfg.model
      : (models[0]?.id || '');
    setCustomModel(false);
    setCfg({ ...cfg, provider: nextProvider, model: nextModel });
  };

  const Field = ({ label, children, hint }) => (
    <div>
      <div className="kicker">{label}</div>
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
        Choose the provider and model every agent uses. Keys come from the server environment
        (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY).
      </p>

      {loadError && (
        <div className="mt-6 text-[13px] text-oxblood" data-testid="settings-load-error">
          Could not reach the LLM settings API: {loadError}
        </div>
      )}

      <Panel title="LLM provider & model" cite="every agent uses this model" testId="settings-llm-panel">
        <div className="grid grid-cols-2 gap-x-16 gap-y-10">
          <Field label="Provider" hint="Model list updates when the provider changes.">
            <select
              value={cfg.provider}
              onChange={e => onProviderChange(e.target.value)}
              data-testid="settings-provider"
              className={input}
            >
              {providerOptions.map(p => (
                <option key={p.id} value={p.id}>{p.label}</option>
              ))}
            </select>
          </Field>

          <Field
            label="Model"
            hint={customModel
              ? 'Type the exact model ID accepted by this provider\'s API.'
              : (selectedModel?.notes || 'Pick a model served by the selected provider.')}
          >
            {customModel ? (
              <input
                type="text"
                value={cfg.model}
                onChange={e => setCfg({ ...cfg, model: e.target.value })}
                placeholder="e.g. gpt-5.6-sol"
                data-testid="settings-model-custom"
                className={input}
              />
            ) : (
              <select
                value={cfg.model}
                onChange={e => {
                  if (e.target.value === CUSTOM_VALUE) {
                    setCustomModel(true);
                    setCfg({ ...cfg, model: '' });
                  } else {
                    setCfg({ ...cfg, model: e.target.value });
                  }
                }}
                data-testid="settings-model"
                className={input}
                disabled={modelsForProvider.length === 0}
              >
                {modelsForProvider.map(m => (
                  <option key={m.id} value={m.id}>
                    {m.label} — {m.tier}{m.supports_web_search ? ' · web-search' : ''}
                  </option>
                ))}
                <option value={CUSTOM_VALUE}>Custom (type model ID)…</option>
              </select>
            )}
            {customModel && (
              <button
                type="button"
                onClick={() => { setCustomModel(false); onProviderChange(cfg.provider); }}
                data-testid="settings-model-custom-cancel"
                className="text-[12px] text-ink-muted mt-1.5 underline"
              >
                back to model list
              </button>
            )}
          </Field>

          <Field label="Temperature">
            <input
              type="number"
              min={0}
              max={2}
              step={0.05}
              value={cfg.temperature}
              onChange={e => setCfg({ ...cfg, temperature: Number(e.target.value) })}
              data-testid="settings-temperature"
              className={input}
            />
          </Field>

          <Field label="Max tokens">
            <input
              type="number"
              min={200}
              max={16000}
              step={100}
              value={cfg.max_tokens}
              onChange={e => setCfg({ ...cfg, max_tokens: Number(e.target.value) })}
              data-testid="settings-max-tokens"
              className={input}
            />
          </Field>

          <Field
            label="API key"
            hint={apiKeySet
              ? 'A key is already configured for this provider. Leave blank to keep it.'
              : 'Optional. Falls back to the server environment key when left blank.'}
          >
            <div className="flex items-center gap-3">
              <input
                type={showApiKey ? 'text' : 'password'}
                autoComplete="off"
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                placeholder={apiKeySet ? '••••••••••••••••' : 'sk-...'}
                data-testid="settings-api-key"
                className={input}
              />
              <button
                type="button"
                onClick={() => setShowApiKey(s => !s)}
                data-testid="settings-api-key-toggle"
                className="text-[12px] text-ink-muted underline shrink-0"
              >
                {showApiKey ? 'hide' : 'show'}
              </button>
            </div>
          </Field>

          <Field label="Base URL" hint="Optional. Only needed for a custom / self-hosted endpoint.">
            <input
              type="text"
              value={baseUrl}
              onChange={e => setBaseUrl(e.target.value)}
              placeholder="https://api.example.com/v1"
              data-testid="settings-base-url"
              className={input}
            />
          </Field>
        </div>

        <div className="mt-10 flex items-center gap-3 flex-wrap">
          <Btn variant="primary" onClick={save} disabled={busy || !cfg.model} testId="btn-save-settings">
            {busy ? 'Saving…' : 'Save settings'}
          </Btn>
          <Tag color="accent">{providerLabel(cfg.provider)} · {(selectedModel?.label) || cfg.model}</Tag>
          {selectedModel?.supports_web_search && (
            <Tag color="accept" testId="tag-supports-web-search">web-search enabled</Tag>
          )}
          {selectedModel?.tier && (
            <Tag color={TIER_TAG[selectedModel.tier] || 'default'}>{selectedModel.tier}</Tag>
          )}
        </div>
      </Panel>
    </div>
  );
}

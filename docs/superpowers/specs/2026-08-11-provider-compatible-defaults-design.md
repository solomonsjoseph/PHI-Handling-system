# Provider-compatible LLM defaults

## Goal
Make first-boot configuration select the environment-backed provider while requiring the operator to select or enter its model before the pipeline can call an LLM.

## Scope
The existing precedence remains: Emergent, Anthropic, ChatGPT OAuth, OpenAI, Gemini, OpenRouter, then Emergent when no credential exists. Explicit persisted `provider` and `model` values remain unchanged.

## Design
Add one internal provider resolver in `phi_core.agents.llm`. It preserves the existing provider precedence and returns the selected provider without inventing a model.

First-boot Settings returns that provider with `model: ""`. The Settings interface must require a nonempty model when saving a provider-backed configuration. `LlmConfig.from_dict()` must reject an absent model with an actionable configuration error before constructing an SDK client. Explicit persisted `provider` and `model` values remain authoritative.

Existing environment-provider inventory and Settings API response redaction remain unchanged.

## Error handling
No credential continues to render the existing Emergent provider with an empty model. Empty models are rejected before any provider SDK call. Unknown explicit providers or models are not rewritten. Existing provider-routing and model validation remain in force.

## Tests
Add deterministic environment-isolated tests covering each key type, precedence, first-boot provider with empty model, rejection of missing models, and preservation of explicit provider/model pairs. No live key is needed.

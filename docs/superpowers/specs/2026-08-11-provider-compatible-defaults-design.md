# Provider-compatible LLM defaults

## Goal
Make first-boot and runtime LLM configuration choose a model compatible with whichever environment-backed provider wins the existing provider-precedence order.

## Scope
The existing precedence remains: Emergent, Anthropic, ChatGPT OAuth, OpenAI, Gemini, OpenRouter, then Emergent when no credential exists. Explicit persisted `provider` and `model` values remain unchanged.

## Design
Add one internal resolver in `phi_core.agents.llm` that returns the selected provider and its default model. It will use catalog identifiers that are valid for the selected provider:

- Emergent and Anthropic: `claude-sonnet-4-5-20250929`
- ChatGPT: the existing ChatGPT-compatible default from the catalog
- OpenAI: `gpt-5.2`
- Gemini: `gemini-3-pro`
- OpenRouter: an existing OpenRouter model identifier from the catalog

`LlmConfig.from_dict()` will use this resolver only when `provider` or `model` is absent. `server._first_boot_llm_defaults()` will use the same resolver, preventing the API and runtime from disagreeing. Existing environment-provider inventory and Settings API response redaction remain unchanged.

## Error handling
No credential continues to render the existing Emergent and Claude defaults. Unknown explicit providers or models are not rewritten. Validation and provider-routing errors remain where they are today.

## Tests
Add deterministic environment-isolated tests covering each key type, precedence, first-boot response, runtime fallback, and preservation of explicit provider/model pairs. No live key is needed.

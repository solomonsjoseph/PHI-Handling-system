# Provider model selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select an environment-backed provider at first boot while requiring an operator-selected model before LLM execution.

**Architecture:** Keep provider precedence in `phi_core.agents.llm`. Return an empty model for implicit configuration and reject that state at the Settings write boundary and runtime config boundary. Explicit saved configuration remains unchanged.

**Tech Stack:** Python 3.9, FastAPI, Pydantic, pytest.

## Global Constraints

- No LLM key or network request is required by tests.
- Provider precedence remains Emergent, Anthropic, ChatGPT OAuth, OpenAI, Gemini, OpenRouter.
- Explicit persisted provider/model values remain authoritative.

---

### Task 1: Require operator-selected model

**Files:**
- Modify: `backend/phi_core/agents/llm.py:34-91`
- Modify: `backend/server.py:568-585,922-951`
- Test: `backend/tests/test_agent_pipeline.py`

**Interfaces:**
- Produces: `LlmConfig.from_dict(d: dict | None) -> LlmConfig`, raising `ValueError` when `model` is absent or blank.
- Produces: `GET /api/settings/llm` first-boot payload with environment-selected `provider` and `model: ""`.
- Produces: `POST /api/settings/llm` HTTP 422 for blank model.

- [ ] **Step 1: Write failing tests**

```python
def test_first_boot_uses_openai_key_but_requires_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert _first_boot_llm_defaults() == {"provider": "openai", "model": "", ...}

def test_llm_config_rejects_missing_model():
    with pytest.raises(ValueError, match="select a model"):
        LlmConfig.from_dict({"provider": "openai"})
```

- [ ] **Step 2: Run failing tests**

Run: `cd backend && python -m pytest tests/test_agent_pipeline.py -q`
Expected: the first-boot result contains the Claude model and missing model is accepted.

- [ ] **Step 3: Implement minimal behavior**

Replace implicit `claude-sonnet-4-5-20250929` model defaults in `LlmSettings`, `_first_boot_llm_defaults`, and `LlmConfig.from_dict` with `""`. Reject blank stripped models in `set_llm_settings` with `HTTPException(422, "select a model before saving LLM settings")` and in `LlmConfig.from_dict` with `ValueError("select a model before running the pipeline")`.

- [ ] **Step 4: Run focused tests**

Run: `cd backend && python -m pytest tests/test_agent_pipeline.py tests/test_llm_catalog.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/phi_core/agents/llm.py backend/server.py backend/tests/test_agent_pipeline.py
git commit -m "fix: require LLM model selection"
```

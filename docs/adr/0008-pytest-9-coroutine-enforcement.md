# 0008: Rely on pytest 9 coroutine failure semantics

## Status

Accepted

## Context

Phase 1 requires pytest 9.1.1, strict asyncio mode, and that unexecuted coroutine tests fail. The planned `filterwarnings = error::pytest.PytestUnhandledCoroutineWarning` cannot be parsed by pytest 9.1.1 because that warning class no longer exists. Its absence is confirmed in the installed `_pytest` package. Without an async plugin, pytest 9 fails an `async def` test directly instead of emitting that warning.

The console `pytest` entry point also omits the backend current directory from `sys.path` in this environment, while `python -m pytest` includes it. CI invokes the console entry point.

## Decision

Use `pytest-asyncio==1.4.0`, whose metadata requires `pytest>=8.4,<10`, with `asyncio_mode = strict`. Omit the invalid warning filter. Add `pythonpath = .` to the repository pytest configuration so the CI console invocation imports `phi_core` deterministically.

Strict mode runs explicitly marked asyncio tests through the plugin. An async test without a suitable plugin is a pytest 9 test failure, which preserves the intended non-silent failure property.

## Consequences

- Existing marked async tests execute rather than being skipped or silently accepted.
- A future downgrade to a pytest version that emits `PytestUnhandledCoroutineWarning` must reconsider the warning filter.
- The repository test command remains `pytest tests`, matching CI.

"""Pipeline regression coverage for value-level keep verification."""
import asyncio

from phi_core.agents.llm import LlmConfig
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run


def test_keep_verification_routes_to_human_review_without_executing(tmp_path, monkeypatch):
    from phi_core.agents import orchestrator

    source = tmp_path / "dataset.csv"
    source.write_text("barcode\n" + "MRN-" + "1" * 8 + "\n", encoding="utf-8")
    executor_calls = []
    phase_events = []

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {})

    class FakePHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"decisions": [{
                "file_id": "dataset.csv",
                "column": "barcode",
                "action": "keep",
                "reason": "Judge decision",
            }]})

    class FakeSentinel:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"issues": []})

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            executor_calls.append(True)
            return await complete_fake_task(self.ctx, {})

    monkeypatch.setattr(orchestrator, "RegulationsExpert", FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", FakePHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {
                "id": "session",
                "files": [{
                    "kind": "dataset",
                    "file_id": "dataset.csv",
                    "stored_path": str(source),
                }],
            },
            db,
            LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit,
            on_phase,
            control_store=store,
        )

    result = asyncio.run(_go())

    assert result["status"] == "awaiting_human_review"
    assert result["decisions"][0]["action"] == "human_review"
    assert executor_calls == []
    keep_events = [event for event in phase_events if event[0] == "keep_verification"]
    assert len(keep_events) == 1
    assert set(keep_events[0][1]) == {"demotions", "_elapsed_s"}
    assert keep_events[0][1]["demotions"] == [{
        "file_id": "dataset.csv",
        "column": "barcode",
        "from": "keep",
        "to": "human_review",
        "detector": "H",
        "citation": "45 CFR 164.514(b)(2)(i)",
    }]

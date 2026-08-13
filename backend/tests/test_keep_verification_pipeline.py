"""Pipeline regression coverage for value-level keep verification."""
import asyncio


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

    class FakeStatute:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"decisions": [{
                "file_id": "dataset.csv",
                "column": "barcode",
                "action": "keep",
                "reason": "Judge decision",
            }]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": []}

    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            executor_calls.append(True)
            return {}

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
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
    result = asyncio.run(orchestrator.run_pipeline(
        {
            "id": "session",
            "files": [{
                "kind": "dataset",
                "file_id": "dataset.csv",
                "stored_path": str(source),
            }],
        },
        db,
        object(),
        emit,
        on_phase,
    ))

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

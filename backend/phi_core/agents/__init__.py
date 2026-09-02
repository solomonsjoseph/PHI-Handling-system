"""phi_core.agents: multi-agent PHI handling pipeline.

Deliberately does NOT import ``.orchestrator`` here (unlike every other
sibling submodule): ``orchestrator.py`` needs ``control.manager`` at
module load time (real constructions of ``Manager``/``ManagerSupervision``,
not just type annotations), and ``control.manager``'s own
``ManagerSupervision(Agent)`` needs ``agents.base.Agent`` at class-definition
time -- which requires this package's ``__init__`` to finish first. Eagerly
importing ``.orchestrator`` here would close that loop into a circular
import. Callers needing ``run_pipeline`` import it directly from
``phi_core.agents.orchestrator`` instead.
"""
from .base import ITERATION_CAP, Agent, AgentMessage
from .llm import LlmConfig, parse_json

__all__ = [
    "Agent",
    "AgentMessage",
    "ITERATION_CAP",
    "LlmConfig",
    "parse_json",
]

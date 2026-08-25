"""D5 resource-bound and aggregate-team policy contracts.

Each resource ceiling receives its own test as the corresponding enforcement
lands. This first contract protects the exact, non-authoritative grouping
used only for aggregate budget reporting.
"""
from __future__ import annotations

from phi_core.control.policy import TEAMS


def test_teams_are_the_exact_five_non_authoritative_budget_groups() -> None:
    assert TEAMS == {
        "regulatory_evidence": frozenset({"Statute", "Praxis", "CorpusResearcher"}),
        "data_and_instrument": frozenset({"Lexicon", "Schema", "Instrument"}),
        "proposal_and_challenge": frozenset({"Judge", "Sentinel"}),
        "verification_and_audit": frozenset({"Executor", "Operator", "Reviewer", "Auditor"}),
        "publication_and_reporting": frozenset(
            {"Scout", "Ledger", "Ledger.Compare", "Ledger.Aggregate", "Herald", "Herald.Abstract", "Herald.Sections"}
        ),
    }

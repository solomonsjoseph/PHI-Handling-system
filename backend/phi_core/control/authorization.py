"""D14/Phase-2B ``AuthorizationService`` and ``AgentContractRegistry`` (v3
lines 717, 3336).

Both names describe functionality this codebase already has under
different shapes: ``CapabilityPolicy`` (``policy.py``) issues and verifies
capability grants, and ``MANIFESTS`` (``policy.py``) is the per-agent
contract table. This module does not rebuild either -- it re-exposes them
under the names Phase 2B's checklist expects, as plain functions
(matching this package's existing ``evidence.py``/``artifacts.py``
convention), so callers can name the concept the doc names without a
second policy engine.

Scope, stated explicitly because the doc's line does not distinguish it:
this is capability-grant authorization only (which agent may call which
provider/tool/data-class), never API/session/request authorization
(cookie auth, ``/api/*`` route access). That boundary is untouched here
per this repo's non-negotiable #7 -- any authentication or credential
change goes through ``integration_playbook_expert_v2`` first, and that is
not this phase's scope.
"""
from __future__ import annotations

from .policy import MANIFESTS, CapabilityDenied, CapabilityPolicy
from .records import AgentManifest, CapabilityGrant, DataClass


def get_contract(agent: str) -> AgentManifest:
    """The ``AgentContractRegistry`` lookup: an agent's immutable manifest."""
    manifest = MANIFESTS.get(agent)
    if manifest is None:
        raise CapabilityDenied(f"agent {agent!r} has no manifest")
    return manifest


def authorize_capability(
    policy: CapabilityPolicy,
    grant: CapabilityGrant,
    *,
    provider: str,
    model: str,
    endpoint: str,
    data_class: DataClass,
) -> None:
    """The ``AuthorizationService`` request-authorization boundary for a
    single capability grant: raises ``CapabilityDenied`` unless the grant
    still matches its manifest, the provider/model/endpoint match the
    grant exactly, and ``data_class`` is within the grant's ceiling."""
    policy.check_provider(grant, provider, model, endpoint)
    policy.check_data_class(grant, data_class)

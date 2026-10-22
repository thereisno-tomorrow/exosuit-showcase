import asyncio

import pytest

from gremlin_town.mcp.brain_policy import TrustedBrainAuthorizer, TrustedOwnerRule
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation,
    BrainRuntimeError,
    OwnerSelector,
)


def authorizer():
    selector = OwnerSelector("project", "fixture-project")
    capability = object()
    return (
        TrustedBrainAuthorizer((TrustedOwnerRule(
            selector=selector,
            owner_id="fixture-project",
            owner_locator="vault://fixture-project",
            policy_revision="fixture-policy-v1",
            readers=frozenset({"chatgpt-web"}),
            writers=frozenset({"chatgpt-web"}),
            capabilities={BrainOperation.CONTEXT_RETRIEVE: capability},
        ),)),
        selector,
        capability,
    )


def test_host_policy_returns_only_enrolled_capability():
    policy, selector, capability = authorizer()
    result = asyncio.run(policy.authorize(
        BrainCaller("chatgpt-web", frozenset({"gt:read", "bc:read"})),
        BrainOperation.CONTEXT_RETRIEVE,
        selector,
        "read",
    ))
    assert result.owner_locator == "vault://fixture-project"
    assert result.capability is capability


@pytest.mark.parametrize("caller,selector", [
    (BrainCaller("chatgpt-web", frozenset({"gt:read"})), OwnerSelector("project", "fixture-project")),
    (BrainCaller("other", frozenset({"bc:read"})), OwnerSelector("project", "fixture-project")),
    (BrainCaller("chatgpt-web", frozenset({"bc:read"})), OwnerSelector("project", "not-enrolled")),
])
def test_scope_subject_and_owner_enrollment_fail_closed(caller, selector):
    policy, _, _ = authorizer()
    with pytest.raises((PermissionError, BrainRuntimeError)):
        asyncio.run(policy.authorize(caller, BrainOperation.CONTEXT_RETRIEVE, selector, "read"))


def test_unenrolled_operation_and_conflicting_rules_are_refused():
    policy, selector, _ = authorizer()
    with pytest.raises(BrainRuntimeError, match="operation is not enrolled"):
        asyncio.run(policy.authorize(
            BrainCaller("chatgpt-web", frozenset({"bc:write"})),
            BrainOperation.MEMORY_SAVE,
            selector,
            "write",
        ))
    rule = TrustedOwnerRule(
        selector=selector,
        owner_id="fixture-project",
        owner_locator="vault://fixture-project",
        policy_revision="fixture-policy-v1",
        readers=frozenset({"chatgpt-web"}),
        writers=frozenset(),
        capabilities={BrainOperation.CONTEXT_RETRIEVE: object()},
    )
    with pytest.raises(ValueError, match="selectors must be unique"):
        TrustedBrainAuthorizer((rule, rule))


def test_a_wrong_project_id_is_answered_with_the_rule_and_the_valid_ids():
    """ROA-72 ("Rule + helpful error"): the valid IDs are the projects this agent may use."""

    def project(project_id, readers):
        return TrustedOwnerRule(
            selector=OwnerSelector("project", project_id),
            owner_id=project_id,
            owner_locator=f"vault://{project_id}",
            policy_revision="fixture-policy-v1",
            readers=frozenset(readers),
            writers=frozenset(),
            capabilities={BrainOperation.CONTEXT_RETRIEVE: object()},
        )

    policy = TrustedBrainAuthorizer((
        project("vault_exosuit", {"chatgpt-web"}),
        project("vault_metavault", {"chatgpt-web"}),
        project("vault_private", {"someone-else"}),
    ))
    with pytest.raises(BrainRuntimeError) as wrong:
        asyncio.run(policy.authorize(
            BrainCaller("chatgpt-web", frozenset({"bc:read"})),
            BrainOperation.CONTEXT_RETRIEVE,
            OwnerSelector("project", "exosuit"),
            "read",
        ))
    message = str(wrong.value)
    assert wrong.value.code == "brain.project.unknown"
    assert "Project exosuit is not enrolled" in message
    assert "A project ID is vault_ plus the vault's name" in message
    assert message.endswith("Valid project IDs: vault_exosuit, vault_metavault.")
    with pytest.raises(BrainRuntimeError, match="No project is enrolled for this agent"):
        asyncio.run(policy.authorize(
            BrainCaller("stranger", frozenset({"bc:read"})),
            BrainOperation.CONTEXT_RETRIEVE,
            OwnerSelector("project", "vault_nope"),
            "read",
        ))
    # A project decision has no owner adapter at all: still a plain denial, not "wrong ID".
    with pytest.raises(BrainRuntimeError) as unsupported:
        asyncio.run(policy.authorize(
            BrainCaller("chatgpt-web", frozenset({"bc:write"})),
            BrainOperation.DECISION_RECORD,
            OwnerSelector("project", "vault_exosuit_decisions"),
            "write",
        ))
    assert unsupported.value.code == "brain.policy.excluded"

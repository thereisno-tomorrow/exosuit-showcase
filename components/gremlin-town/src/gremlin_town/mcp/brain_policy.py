"""Host-owned Brain Core authorization for the shared MCP.

The public tool arguments contain only a semantic owner selector. Trusted owner
locators, connector/storage capabilities, subjects, and policy revisions are
fixed here by runtime composition and never accepted from a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Callable

from gremlin_town.mcp.brain_runtime import (
    AccessMode,
    BrainCaller,
    BrainOperation,
    BrainRuntimeError,
    OwnerAuthorization,
    OwnerSelector,
)


UNKNOWN_PROJECT = "brain.project.unknown"
# What a project owner can hold: its context and its notes (ROA-15, ROA-72). Anything else asked of a
# project, such as a project decision, has no owner adapter and stays a plain denial.
PROJECT_OPERATIONS = frozenset({
    BrainOperation.CONTEXT_RETRIEVE,
    BrainOperation.CONTINUITY_START,
    BrainOperation.CONTINUITY_APPEND,
    BrainOperation.CONTINUITY_RECOVER,
    BrainOperation.CONTINUITY_LIST,
    BrainOperation.CONTINUITY_CLOSE,
})


@dataclass(frozen=True, slots=True)
class TrustedOwnerRule:
    selector: OwnerSelector
    owner_id: str
    owner_locator: str
    policy_revision: str
    readers: frozenset[str]
    writers: frozenset[str]
    capabilities: Mapping[BrainOperation, object]

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.owner_id, self.owner_locator, self.policy_revision)):
            raise ValueError("trusted owner identity must be non-empty")
        if not self.readers or any(not subject.strip() for subject in self.readers):
            raise ValueError("trusted owner readers must be non-empty subjects")
        if any(not subject.strip() for subject in self.writers):
            raise ValueError("trusted owner writers must be non-empty subjects")
        if not self.capabilities:
            raise ValueError("trusted owner rule must expose at least one operation")
        capabilities = dict(self.capabilities)
        if any(not isinstance(operation, BrainOperation) for operation in capabilities):
            raise TypeError("trusted owner capability keys must be BrainOperation values")
        if any(capability is None for capability in capabilities.values()):
            raise ValueError("trusted owner capabilities cannot be null")
        object.__setattr__(self, "capabilities", MappingProxyType(capabilities))


class TrustedBrainAuthorizer:
    """Resolve one semantic selector against immutable host enrollment."""

    def __init__(self, rules: tuple[TrustedOwnerRule, ...], *, recheck: Callable | None = None,
                 dynamic_subject_grants: bool = False) -> None:
        if dynamic_subject_grants and recheck is None:
            raise ValueError("dynamic subject grants require an authoritative recheck")
        self._recheck = recheck
        self._dynamic_subject_grants = dynamic_subject_grants
        if not rules:
            raise ValueError("at least one trusted Brain owner rule is required")
        indexed: dict[OwnerSelector, TrustedOwnerRule] = {}
        for rule in rules:
            if rule.selector in indexed:
                raise ValueError("trusted Brain owner selectors must be unique")
            indexed[rule.selector] = rule
        self._rules = MappingProxyType(indexed)

    def _unknown_project(self, caller: BrainCaller, selector: OwnerSelector) -> str:
        """ROA-72 ("Rule + helpful error"): a wrong project ID is answered with the rule and the
        project IDs this agent may use."""

        valid = sorted(
            str(rule.selector.project_id)
            for rule in self._rules.values()
            if rule.selector.kind == "project" and caller.subject in rule.readers | rule.writers
        )
        listed = f"Valid project IDs: {', '.join(valid)}." if valid else "No project is enrolled for this agent."
        return (
            f"Project {selector.project_id} is not enrolled. A project ID is vault_ plus the vault's "
            f"name, for example vault_exosuit. {listed}"
        )

    async def authorize(
        self,
        caller: BrainCaller,
        operation: BrainOperation,
        selector: OwnerSelector,
        access: AccessMode,
    ) -> OwnerAuthorization:
        required_scope = "bc:read" if access == "read" else "bc:write"
        if required_scope not in caller.scopes:
            raise PermissionError("required Brain Core scope not granted")
        rule = self._rules.get(selector)
        if rule is None:
            if selector.kind == "project" and operation in PROJECT_OPERATIONS:
                # Its own code: policy denials reach agents only as ACCESS_DENIED, and this answer
                # names nothing the agent may not already use.
                raise BrainRuntimeError(UNKNOWN_PROJECT, self._unknown_project(caller, selector))
            raise BrainRuntimeError("brain.policy.excluded", "owner scope is not enrolled")
        allowed = rule.readers if access == "read" else rule.writers
        if not self._dynamic_subject_grants and caller.subject not in allowed:
            raise BrainRuntimeError("brain.policy.excluded", "owner scope is not granted")
        if self._recheck is not None:
            self._recheck(caller, operation, selector, access, rule)
        capability = rule.capabilities.get(operation)
        if capability is None:
            raise BrainRuntimeError("brain.policy.excluded", "operation is not enrolled for this owner")
        return OwnerAuthorization(
            operation=operation,
            access=access,
            selector=selector,
            owner_id=rule.owner_id,
            owner_locator=rule.owner_locator,
            policy_revision=rule.policy_revision,
            capability=capability,
        )

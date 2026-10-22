from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable
from functools import wraps
from threading import RLock

from gremlin_town.contracts import Actor, ErrorCode

from .errors import GremlinTownError


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    display_name: str
    address: str
    projects: frozenset[str]
    sensitivities: frozenset[str]
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    is_operator: bool = False
    read_all_requests: bool = False


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class Policy:
    """Mutable runtime configuration so revocation takes effect immediately."""

    def __init__(
        self,
        participants: Iterable[Participant],
        routes: frozenset[tuple[str, str, str, str]] | None = None,
    ) -> None:
        self.lock = RLock()
        self.dispatch_lock = RLock()
        values = tuple(participants)
        self._participants = {item.participant_id: item for item in values}
        if len(self._participants) != len(values):
            raise ValueError("participant IDs must be unique")
        self._routes = routes
        self._history_routes = routes

    @_synchronized
    def refresh_from(self, candidate: "Policy") -> dict[str, object]:
        """Publish validated enrollment while retaining inactive history identities."""
        participants = dict(candidate._participants)
        for identifier, previous in self._participants.items():
            current = participants.get(identifier)
            if current is None:
                participants[identifier] = replace(previous, enabled=False)
            elif not current.enabled:
                participants[identifier] = replace(current,
                    projects=current.projects | previous.projects,
                    sensitivities=current.sensitivities | previous.sensitivities)
        history_routes = (None if self._history_routes is None or candidate._routes is None
                          else self._history_routes | candidate._routes)
        self._participants = participants
        self._routes = candidate._routes
        self._history_routes = history_routes
        return {"status": "refreshed", "participant_count": len(participants),
                "enabled_count": sum(item.enabled for item in participants.values())}

    @_synchronized
    def set_enabled(self, participant_id: str, enabled: bool) -> None:
        participant = self._participants[participant_id]
        self._participants[participant_id] = replace(participant, enabled=enabled)

    @_synchronized
    def participant(self, participant_id: str, *, require_enabled: bool = True) -> Participant:
        participant = self._participants.get(participant_id)
        if participant is None or (require_enabled and not participant.enabled):
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "participant is not enabled")
        return participant

    @_synchronized
    def authenticate(self, actor: Actor) -> Participant:
        participant = self.participant(actor.actor_id)
        if actor.is_operator and not participant.is_operator:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "operator identity is not configured")
        return participant

    @_synchronized
    def require_operator(self, actor: Actor) -> Participant:
        participant = self.authenticate(actor)
        if not actor.is_operator or not participant.is_operator:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "operator identity required")
        return participant

    @_synchronized
    def resolve_recipient(self, name: str) -> Participant:
        exact = self._participants.get(name)
        if exact is not None:
            return self.participant(exact.participant_id)
        folded = name.casefold()
        matches = [
            item
            for item in self._participants.values()
            if item.enabled
            and (
                folded == item.display_name.casefold()
                or any(folded == alias.casefold() for alias in item.aliases)
            )
        ]
        if len(matches) != 1:
            code = ErrorCode.RECIPIENT_AMBIGUOUS if len(matches) > 1 else ErrorCode.ACCESS_DENIED
            raise GremlinTownError(code, "recipient does not resolve to one enabled participant")
        return self.participant(matches[0].participant_id)

    @_synchronized
    def require_project(self, participant_id: str, project: str) -> Participant:
        participant = self.participant(participant_id)
        if project not in participant.projects:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "participant is not allowed in this project")
        return participant

    @_synchronized
    def require_route(self, project: str, requester_id: str, recipient_id: str, sensitivity: str) -> None:
        requester = self.require_project(requester_id, project)
        recipient = self.require_project(recipient_id, project)
        self._require_route_configuration(project, requester, recipient, sensitivity)

    @_synchronized
    def require_route_configuration(
        self, project: str, requester_id: str, recipient_id: str, sensitivity: str
    ) -> None:
        """Check configured scope without hiding history when the other endpoint is disabled."""
        requester = self.participant(requester_id, require_enabled=False)
        recipient = self.participant(recipient_id, require_enabled=False)
        if project not in requester.projects or project not in recipient.projects:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "participant is not allowed in this project")
        self._require_route_configuration(project, requester, recipient, sensitivity,
                                          historical=not requester.enabled or not recipient.enabled)

    def _require_route_configuration(
        self, project: str, requester: Participant, recipient: Participant, sensitivity: str,
        *, historical: bool = False
    ) -> None:
        if sensitivity not in requester.sensitivities or sensitivity not in recipient.sensitivities:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "sensitivity is not allowed on this route")
        routes = self._history_routes if historical else self._routes
        if routes is not None and (
            project,
            requester.participant_id,
            recipient.participant_id,
            sensitivity,
        ) not in routes:
            raise GremlinTownError(ErrorCode.ACCESS_DENIED, "route is not configured")

    @_synchronized
    def visible(self, actor: Actor, project: str | None = None) -> tuple[Participant, ...]:
        caller = self.authenticate(actor)
        if project is not None:
            self.require_project(caller.participant_id, project)

        if actor.is_operator and caller.is_operator:
            return tuple(
                replace(item, projects=frozenset({project})) if project is not None else item
                for item in self._participants.values()
                if item.enabled and (project is None or project in item.projects)
            )

        visible_projects: dict[str, set[str]] = {
            caller.participant_id: set(caller.projects if project is None else {project})
        }
        visible_sensitivities: dict[str, set[str]] = {caller.participant_id: set(caller.sensitivities)}

        for destination in self._participants.values():
            if not destination.enabled or destination.participant_id == caller.participant_id:
                continue
            shared_projects = caller.projects & destination.projects
            if project is not None:
                shared_projects &= {project}
            for shared_project in shared_projects:
                sensitivities = self._directory_route_sensitivities(caller, destination, shared_project)
                if sensitivities:
                    visible_projects.setdefault(destination.participant_id, set()).add(shared_project)
                    visible_sensitivities.setdefault(destination.participant_id, set()).update(sensitivities)

        return tuple(
            replace(
                item,
                projects=frozenset(visible_projects[item.participant_id]),
                sensitivities=frozenset(visible_sensitivities[item.participant_id]),
            )
            for item in self._participants.values()
            if item.participant_id in visible_projects
        )

    def _directory_route_sensitivities(
        self,
        caller: Participant,
        destination: Participant,
        project: str,
    ) -> frozenset[str]:
        """The sensitivities the route table lets `caller` reach `destination` with on
        `project` — the same table `_require_route_configuration` enforces on the
        request path, computed here rather than kept as a second, hand-kept copy."""
        shared_sensitivities = caller.sensitivities & destination.sensitivities
        if not shared_sensitivities:
            return frozenset()
        if self._routes is None:
            return shared_sensitivities
        return frozenset(
            sensitivity
            for sensitivity in shared_sensitivities
            if (project, caller.participant_id, destination.participant_id, sensitivity) in self._routes
        )

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from gremlin_town.api.app import create_app
from gremlin_town.contracts import ContentAvailability, ContentSource, MessageKind, Page, ProviderMessage, RequestMode, RequestSnapshot, WorkState


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class ViewService:
    def __init__(self):
        self.list_calls = []

    def requests_list(self, actor, filters, cursor=None):
        self.list_calls.append((actor, filters, cursor))
        return Page(items=({"request_id": 'req"><script>alert(1)</script>', "project": "<img src=x onerror=alert(1)>", "work_state": "queued", "attention": None, "updated_at": NOW},), next_cursor="next&cursor", as_of=NOW)

    def requests_read(self, actor, request_id, message_cursor=None):
        return RequestSnapshot(request_id=request_id, mode=RequestMode.ACTION_REQUESTED, project="demo", requester_id="alice", recipient_id="bob", objective="<b>unsafe</b>", work_state=WorkState.REQUESTED, revision=2, recovery_hold=False, thread_id=None, content_availability=ContentAvailability.UNAVAILABLE, as_of=NOW)

    def events_list(self, actor, filters, cursor=None):
        return Page(items=({"event_type": "provider_claim", "request_id": "req-1", "trust_label": "untrusted claim", "created_at": NOW},), as_of=NOW)


def test_views_escape_hostile_markup_and_show_truthful_times_and_states() -> None:
    app = create_app(ViewService(), actor_resolver={"secret": "alice"})
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}
    response = client.get("/overview?project=<script>", headers=headers)
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "As of:" in response.text and "Last synced:" in response.text
    assert "Packet source URLs are displayed as text and are never visited automatically" in response.text
    assert "Queued; work is not completed" in response.text
    assert "Signed in as <strong>alice</strong>" in response.text
    assert 'action="/logout"' in response.text
    assert "Not yet verified / unavailable" in response.text
    assert 'name="recipient" value=""' in response.text
    assert "Last source fetch:</strong> not observed" in response.text
    assert "not flagged" in response.text
    assert "next%26cursor" in response.text
    assert "<meta name=\"viewport\"" in response.text
    assert "Skip to content" in response.text
    assert "Log out" in response.text


def test_missing_filter_values_render_as_blank() -> None:
    client = TestClient(create_app(ViewService(), actor_resolver={"secret": "alice"}))
    response = client.get("/overview", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert 'name="project" value=""' in response.text
    assert 'name="recipient" value=""' in response.text
    assert "2026-09-20T12:00:00+00:00" in response.text


def test_overview_filter_form_passes_exact_filters_and_preserves_pagination() -> None:
    service = ViewService()
    client = TestClient(create_app(service, actor_resolver={"secret": "alice"}))
    response = client.get(
        "/overview?project=project-one&recipient=bot&work_state=blocked&attention=true&cursor=page-token",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert service.list_calls[-1][1] == {
        "project": "project-one",
        "recipient": "bot",
        "work_state": "blocked",
        "attention": True,
    }
    assert service.list_calls[-1][2] == "page-token"
    assert 'name="project" value="project-one"' in response.text
    assert 'name="recipient" value="bot"' in response.text
    assert '<option value="blocked" selected>blocked</option>' in response.text
    assert 'name="attention" value="true" checked' in response.text
    assert 'href="/overview">Clear filters</a>' in response.text
    assert "project=project-one&amp;recipient=bot&amp;work_state=blocked&amp;attention=true&amp;cursor=next%26cursor" in response.text


def test_attention_view_is_selectable_and_passes_attention_to_service() -> None:
    service = ViewService()
    client = TestClient(create_app(service, actor_resolver={"secret": "alice"}))
    response = client.get(
        "/attention?project=project-one&recipient=bot&work_state=blocked",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert "<h1>Attention</h1>" in response.text
    assert service.list_calls[-1][1] == {
        "project": "project-one",
        "recipient": "bot",
        "work_state": "blocked",
        "attention": True,
    }
    assert 'href="/attention?project=project-one&amp;recipient=bot&amp;work_state=blocked&amp;attention=true&amp;cursor=next%26cursor"' in response.text


def test_observed_facts_render_without_presence_or_stale_inference() -> None:
    class FactsService(ViewService):
        def requests_list(self, actor, filters, cursor=None):
            self.list_calls.append((actor, filters, cursor))
            return Page(
                items=({
                    "request_id": "request-facts",
                    "project": "<untrusted-project>",
                    "work_state": "acknowledged",
                    "updated_at": NOW,
                    "last_source_fetch_at": NOW,
                    "last_source_success_at": NOW,
                    "last_authenticated_report_at": NOW,
                    "participation": {
                        "state": "authenticated_report",
                        "summary": "Last authenticated report received",
                        "since": NOW,
                        "age_seconds": 20,
                    },
                    "attention": ["unmatched provider message", "no acknowledgement"],
                },),
                as_of=NOW,
            )

    client = TestClient(create_app(FactsService(), actor_resolver={"secret": "alice"}))
    response = client.get("/overview", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert "Last source fetch:</strong> 2026-09-20T12:00:00+00:00" in response.text
    assert "Last successful source fetch:</strong> 2026-09-20T12:00:00+00:00" in response.text
    assert "Last authenticated report:</strong> 2026-09-20T12:00:00+00:00" in response.text
    assert "Last authenticated report received; Since: 2026-09-20T12:00:00+00:00; Age: 20 seconds" in response.text
    assert '"state"' not in response.text
    assert "unmatched provider message" in response.text and "no acknowledgement" in response.text
    assert "Participation is authenticated-report history, not presence." in response.text
    assert "No stale threshold is inferred." in response.text
    assert "&lt;untrusted-project&gt;" in response.text
    assert "online" not in response.text.lower()
    assert "offline" not in response.text.lower()
    assert "stale participation" not in response.text.lower()


def test_request_view_labels_unavailable_content_and_keeps_read_only_surface() -> None:
    client = TestClient(create_app(ViewService(), actor_resolver={"secret": "alice"}))
    response = client.get("/requests/request-1", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert "Content is unavailable" in response.text
    assert "unavailable bodies are not reconstructed" in response.text
    assert "Objective" in response.text
    assert "Parties" in response.text
    assert "Delivery" in response.text
    assert "provider sync is not verified" in response.text


def test_request_view_renders_verified_packet_and_separates_delivery_from_work() -> None:
    class RichService(ViewService):
        def requests_read(self, actor, request_id, message_cursor=None):
            body = '{"request_id":"request-1","operation_id":"op-1","sender":"alice","recipient":"bob","created_at":"2026-09-20T12:00:00+00:00","packet":{"schema_version":1,"kind":"result","session_reference":"session-1","objective":"Do the safe thing","context":"Returned answer","constraints":{"format":"plain"},"sources":[{"url":"https://example.invalid/unvisited"}],"next_action":"none","expected_reply":"ack","sensitivity":"normal"}}'
            return RequestSnapshot(
                request_id=request_id, mode=RequestMode.ACTION_REQUESTED, project="demo", requester_id="alice", recipient_id="bob",
                objective="Do the safe thing", work_state=WorkState.REQUESTED, revision=2, recovery_hold=False, thread_id="thread-1",
                content_availability=ContentAvailability.COMPLETE,
                content=(ProviderMessage("message-1", "thread-1", None, NOW, body.encode(), True, ContentSource.PROVIDER, "verified", "op-1", "bob", True),),
                as_of=NOW,
            )

    client = TestClient(create_app(RichService(), actor_resolver={"secret": "alice"}))
    response = client.get("/requests/request-1", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert "Returned result" in response.text
    assert "Context / returned result" in response.text
    assert "Returned answer" in response.text
    assert "Provider-linked content observed (link verified)" in response.text
    assert "Waiting to be picked up" in response.text
    assert "provider acceptance" not in response.text
    assert "https://example.invalid/unvisited" in response.text
    assert "No remote fetches are performed" not in response.text


def test_provenance_view_labels_untrusted_claims() -> None:
    client = TestClient(create_app(ViewService(), actor_resolver={"secret": "alice"}))
    response = client.get("/provenance", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert "untrusted claim" in response.text
    assert "Provenance events" in response.text

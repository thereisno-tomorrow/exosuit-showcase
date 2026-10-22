"""Escaped, read-only HTML rendering for the operator overview."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from urllib.parse import quote
from typing import Any, Mapping

from jinja2 import BaseLoader, Environment, select_autoescape

from gremlin_town.api.serialization import to_jsonable
from gremlin_town.contracts import RequestSnapshot


_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { line-height: 1.5; margin: 0; padding: 1rem; }
    main { margin: auto; max-width: 72rem; }
    a { color: #1769aa; }
    a:focus-visible, input:focus-visible, button:focus-visible { outline: 3px solid #e09f00; outline-offset: 2px; }
    .skip-link { position: absolute; left: -9999px; }
    .skip-link:focus { left: 1rem; top: 1rem; background: Canvas; padding: .5rem; }
    .meta, .muted { color: GrayText; }
    .notice { border-left: .3rem solid #c77d00; padding: .5rem .75rem; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; min-width: 48rem; width: 100%; }
    th, td { border-bottom: 1px solid GrayText; padding: .6rem; text-align: left; vertical-align: top; }
    code, pre { overflow-wrap: anywhere; white-space: pre-wrap; }
    pre { max-height: 14rem; overflow: auto; }
    nav ul { display: flex; flex-wrap: wrap; gap: .75rem; list-style: none; padding: 0; }
    .filters a[aria-current="page"] { font-weight: 700; }
    .session { align-items: center; display: flex; flex-wrap: wrap; gap: .75rem; }
    .packet-field { margin: .75rem 0; }
    .packet-value { margin-top: .2rem; white-space: pre-wrap; }
    @media (max-width: 48rem) { body { padding: .65rem; } h1 { font-size: 1.6rem; } }
  </style>
</head>
<body>
  <a class="skip-link" href="#content">Skip to content</a>
  <header>
    <nav aria-label="Primary navigation">
      <ul>
        <li><a href="/overview">Inbox</a></li>
        <li><a href="/attention">Attention</a></li>
        <li><a href="/provenance">Provenance</a></li>
      </ul>
    </nav>
    {% if actor_id %}<div class="session"><span>Signed in as <strong>{{ actor_id }}</strong></span><form method="post" action="/logout"><button type="submit">Log out</button></form></div>{% endif %}
  </header>
  <main id="content">
    <h1>{{ heading }}</h1>
    <p class="meta">As of: {% if as_of_iso %}<time datetime="{{ as_of_iso }}">{{ as_of }}</time>{% else %}unavailable{% endif %} · Last synced: {% if sync_iso %}<time datetime="{{ sync_iso }}">{{ sync_time }}</time>{% else %}Not yet verified / unavailable{% endif %}</p>
    <p class="notice">Read-only overview. Packet source URLs are displayed as text and are never visited automatically.</p>
    {% if not snapshot and not events %}<form method="get" action="{{ filter_action }}">
      <fieldset><legend>Filter requests</legend>
        <label for="filter-project">Project</label>
        <input id="filter-project" name="project" value="{{ filter_values.project|default('') }}">
        <label for="filter-recipient">Recipient</label>
        <input id="filter-recipient" name="recipient" value="{{ filter_values.recipient|default('') }}">
        <label for="filter-work-state">Work state</label>
        <select id="filter-work-state" name="work_state">
          <option value="">Any state</option>
          {% for option in work_state_options %}<option value="{{ option }}"{% if filter_values.work_state == option %} selected{% endif %}>{{ option }}</option>{% endfor %}
        </select>
        <label><input type="checkbox" name="attention" value="true"{% if filter_values.attention %} checked{% endif %}> Attention needed</label>
        <button type="submit">Apply filters</button>
        <a href="/overview">Clear filters</a>
      </fieldset>
    </form>{% endif %}
    {% if filters %}<nav class="filters" aria-label="Active filters"><ul>{% for label, href, current in filters %}<li><a href="{{ href }}"{% if current %} aria-current="page"{% endif %}>{{ label }}</a></li>{% endfor %}</ul></nav>{% endif %}
    {% if error %}<p role="alert" class="notice">{{ error }}</p>{% endif %}
    {% if snapshot %}
      <section aria-labelledby="request-heading">
        <h2 id="request-heading">Request and delivery</h2>
        <section aria-labelledby="objective-heading"><h3 id="objective-heading">Objective</h3><p>{{ snapshot.objective|default('Objective unavailable') }}</p></section>
        <section aria-labelledby="parties-heading"><h3 id="parties-heading">Parties</h3><dl><dt><strong>Requester</strong></dt><dd>{{ snapshot.requester_id|default('unavailable') }}</dd><dt><strong>Recipient</strong></dt><dd>{{ snapshot.recipient_id|default('unavailable') }}</dd></dl></section>
        <dl>
          {% for label, value in snapshot_fields %}<dt><strong>{{ label }}</strong></dt><dd>{{ value }}</dd>{% endfor %}
        </dl>
        <h3>Readable packet and result</h3>
        {% if snapshot.content_availability != 'complete' %}<p class="notice">Content is {{ snapshot.content_availability }}; unavailable bodies are not reconstructed.</p>{% endif %}
        {% if snapshot.content_error %}<p role="alert">Content error: {{ snapshot.content_error.message }}</p>{% endif %}
        {% if snapshot.content %}<ol>{% for message in snapshot.content %}<li>
          <p><strong>{{ message.delivery_label }}</strong> · {{ message.trust_label|default('trust unavailable') }}{% if not message.body_available %} · body unavailable{% endif %}</p>
          {% if message.packet_status %}<p class="notice">{{ message.packet_status }}</p>{% endif %}
          {% if message.packet_sections %}<div aria-label="{{ message.packet_heading }}"><h4>{{ message.packet_heading }}</h4>{% for section in message.packet_sections %}<section class="packet-field"><h5>{{ section.label }}</h5><p class="packet-value">{{ section.value }}</p></section>{% endfor %}</div>{% endif %}
          {% if message.raw_body %}<details><summary>Show untrusted raw content</summary><pre>{{ message.raw_body }}</pre></details>{% endif %}
        </li>{% endfor %}</ol>{% else %}<p>No message body is available; no result is inferred.</p>{% endif %}
      </section>
    {% elif events %}
        <section aria-labelledby="provenance-heading"><h2 id="provenance-heading">Provenance events</h2><div class="table-wrap"><table><caption>Event provenance; claims remain labelled as untrusted unless verified by the service.</caption><thead><tr><th scope="col">Event</th><th scope="col">Request</th><th scope="col">Trust</th><th scope="col">Time</th></tr></thead><tbody>{% for event in events %}<tr><td>{{ event.event_type }}</td><td>{{ event.request_id }}</td><td>{{ event.trust_label|default('untrusted claim') }}</td><td>{{ event.created_at|default(event.occurred_at|default('time unavailable')) }}</td></tr>{% endfor %}</tbody></table></div></section>
    {% else %}
      <section aria-labelledby="inbox-heading"><h2 id="inbox-heading">Inbox</h2><p class="muted">Participation is authenticated-report history, not presence. No stale threshold is inferred.</p><div class="table-wrap"><table><caption>Requests visible to the authenticated actor.</caption><thead><tr><th scope="col">Objective / request</th><th scope="col">Parties</th><th scope="col">Project</th><th scope="col">Work state</th><th scope="col">Last update</th><th scope="col">Observed facts</th><th scope="col">Attention</th><th scope="col">Revision</th></tr></thead><tbody>{% for item in items %}<tr><td><a href="{{ item.href }}">{{ item.objective|default('Objective unavailable') }}</a><br><span class="muted">Request {{ item.request_id }}</span></td><td>{{ item.requester_id|default('unavailable') }} → {{ item.recipient_id|default('unavailable') }}</td><td>{{ item.project|default('unavailable') }}</td><td>{{ item.work_state_label|default('context only') }}</td><td>{{ item.updated_at_label }}</td><td><strong>Last source fetch:</strong> {{ item.last_source_fetch_at_label }}<br><strong>Last successful source fetch:</strong> {{ item.last_source_success_at_label }}<br><strong>Last authenticated report:</strong> {{ item.last_authenticated_report_at_label }}<br><strong>Participation:</strong> {{ item.participation_label }}</td><td>{{ item.attention_label }}</td><td>{{ item.revision|default('—') }}</td></tr>{% else %}<tr><td colspan="8">No requests are available.</td></tr>{% endfor %}</tbody></table></div>{% if next_href %}<p><a href="{{ next_href }}">Next page</a></p>{% endif %}</section>
    {% endif %}
  </main>
</body>
</html>
"""


env = Environment(loader=BaseLoader(), autoescape=select_autoescape(default=True))
template = env.from_string(_TEMPLATE)


def display_time(value: Any) -> tuple[str, str]:
    """Return a recorded timestamp; never invent one for missing evidence."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat(), value.isoformat(timespec="seconds")
    if isinstance(value, str) and value:
        return value, value
    return "", "unavailable"


def _safe_href(path: str, **query: Any) -> str:
    pairs = [f"{quote(str(key), safe='')}={quote(str(value), safe='')}" for key, value in query.items() if value not in (None, "")]
    return path + ("?" + "&".join(pairs) if pairs else "")


def _display_value(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return str(value)


def _fact_text(value: Any) -> str:
    return "not observed" if value is None or value == "" else _display_value(value)


def _attention_text(value: Any) -> str:
    return "not flagged" if not value else _display_value(value)


def _participation_text(value: Any) -> str:
    if value is None:
        return "not observed"
    if not isinstance(value, Mapping):
        return str(value) if value != "" else "not observed"
    parts: list[str] = []
    summary = value.get("summary")
    state = value.get("state")
    if summary not in (None, ""):
        parts.append(str(summary))
    elif state not in (None, ""):
        parts.append(f"Participation state: {state}")
    since = value.get("since")
    if since not in (None, ""):
        parts.append(f"Since: {_display_value(since)}")
    age_seconds = value.get("age_seconds")
    if age_seconds is not None:
        parts.append(f"Age: {age_seconds} seconds")
    return "; ".join(parts) if parts else "not observed"


def _filter_form_values(values: Mapping[str, Any] | None) -> dict[str, Any]:
    values = values or {}
    return {
        "project": "" if values.get("project") is None else values.get("project"),
        "recipient": "" if values.get("recipient") is None else values.get("recipient"),
        "work_state": "" if values.get("work_state") is None else values.get("work_state"),
        "attention": values.get("attention") in (True, "true", "1", "on"),
    }


def _work_state_label(value: Any) -> str:
    labels = {
        "requested": "Waiting to be picked up",
        "acknowledged": "Acknowledged",
        "in_progress": "In progress",
        "blocked": "Blocked",
        "result_returned": "Result returned",
        "declined": "Declined",
        "cancelled": "Cancelled",
        "queued": "Queued; work is not completed",
    }
    return labels.get(str(value), "Context only — no work state" if not value else str(value))


def _delivery_summary(data: Mapping[str, Any]) -> str:
    messages = data.get("content", ())
    sources = {str(message.get("content_source", "")).lower() for message in messages}
    availability = str(data.get("content_availability", "unavailable"))
    if "provider" in sources:
        return "Provider content observed; this does not change the work state."
    if "outbox" in sources:
        return "Queued/staged locally; provider delivery is not confirmed."
    if availability in {"unavailable", "partial"}:
        return f"Delivery/content {availability}; provider sync is not verified."
    return "No delivery evidence is recorded."


def _packet_sections(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    kind = str(packet.get("kind", "")).lower()
    fields = (
        ("Schema", "schema_version"),
        ("Kind", "kind"),
        ("Session", "session_reference"),
        ("Objective", "objective"),
        ("Context / returned result", "context") if kind == "result" else ("Context", "context"),
        ("Constraints", "constraints"),
        ("Sources (text only)", "sources"),
        ("Next action", "next_action"),
        ("Expected reply", "expected_reply"),
        ("Sensitivity", "sensitivity"),
    )
    for label, key in fields:
        if packet.get(key) not in (None, "", [], {}):
            sections.append({"label": label, "value": _display_value(packet[key])})
    return sections


def _message_data(message: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(message)
    source = str(result.get("content_source", "")).lower()
    verified = bool(result.get("exact_link_verified"))
    if source == "outbox":
        result["delivery_label"] = "Local staged copy — provider acceptance not confirmed"
        trusted_packet = True
    elif source == "provider":
        result["delivery_label"] = "Provider-linked content observed (link verified)" if verified else "Provider content observed (attribution unverified)"
        trusted_packet = verified
    else:
        result["delivery_label"] = "Delivery source unavailable"
        trusted_packet = False
    result["packet_sections"] = []
    result["packet_heading"] = "Packet"
    result["packet_status"] = None
    result["raw_body"] = None
    body = result.get("body") if result.get("body_available") else None
    if not body:
        result["packet_status"] = "Body unavailable; no result is inferred."
        return result
    if not isinstance(body, str):
        result["packet_status"] = "Content is malformed or unavailable; no readable packet is inferred."
        return result
    try:
        payload = json.loads(body)
        packet = payload.get("packet") if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        packet = None
    if not isinstance(packet, dict):
        result["packet_status"] = "Content is malformed or unavailable; no readable packet is inferred."
        return result
    if not trusted_packet:
        result["packet_status"] = "Provider packet claim is unverified; fields are not treated as authoritative."
        result["raw_body"] = body
        return result
    result["packet_heading"] = "Returned result" if str(packet.get("kind", "")).lower() == "result" else "Readable packet"
    result["packet_sections"] = _packet_sections(packet)
    if not result["packet_sections"]:
        result["packet_status"] = "Packet contains no readable fields."
    return result


def _snapshot_data(snapshot: RequestSnapshot) -> dict[str, Any]:
    data = to_jsonable(snapshot)
    data["content_availability"] = str(data["content_availability"])
    messages = []
    for message in data.get("content", []):
        message = dict(message)
        message["body"] = message.get("body") if message.get("body_available") else None
        messages.append(_message_data(message))
    data["content"] = messages
    data["work_state_label"] = _work_state_label(data.get("work_state"))
    data["delivery_summary"] = _delivery_summary(data)
    return data


def render_overview(
    *,
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    next_cursor: str | None = None,
    as_of: Any = None,
    sync_time: Any = None,
    actor_id: str | None = None,
    filter_values: Mapping[str, Any] | None = None,
    filter_action: str = "/overview",
    heading: str = "Inbox",
    filters: list[tuple[str, str, bool]] | None = None,
    snapshot: RequestSnapshot | None = None,
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    error: str | None = None,
    next_path: str = "/overview",
    next_params: Mapping[str, Any] | None = None,
) -> str:
    as_of_iso, as_of_text = display_time(as_of)
    sync_iso, sync_text = display_time(sync_time)
    rows = []
    for raw in items:
        item = dict(to_jsonable(raw))
        item["href"] = _safe_href(f"/requests/{quote(str(item.get('request_id', '')), safe='')}")
        item["work_state_label"] = _work_state_label(item.get("work_state"))
        item["updated_at_label"] = _fact_text(item.get("updated_at"))
        item["last_source_fetch_at_label"] = _fact_text(item.get("last_source_fetch_at"))
        item["last_source_success_at_label"] = _fact_text(item.get("last_source_success_at"))
        item["last_authenticated_report_at_label"] = _fact_text(item.get("last_authenticated_report_at"))
        item["participation_label"] = _participation_text(item.get("participation"))
        item["attention_label"] = _attention_text(item.get("attention"))
        rows.append(item)
    snapshot_data = _snapshot_data(snapshot) if snapshot else None
    fields = []
    if snapshot_data:
        fields = [
            ("Request", snapshot_data.get("request_id")),
            ("Project", snapshot_data.get("project")),
            ("Mode", snapshot_data.get("mode")),
            ("Work state", snapshot_data.get("work_state_label")),
            ("Delivery", snapshot_data.get("delivery_summary")),
            ("Revision", snapshot_data.get("revision")),
            ("Recovery hold", "held" if snapshot_data.get("recovery_hold") else "not held"),
            ("Content availability", snapshot_data.get("content_availability")),
            ("Thread", snapshot_data.get("thread_id") or "not assigned"),
        ]
    next_query = {**(next_params or {}), "cursor": next_cursor}
    next_href = _safe_href(next_path, **next_query) if next_cursor else None
    return template.render(
        title="Gremlin Town",
        heading=heading,
        actor_id=actor_id,
        filter_values=_filter_form_values(filter_values),
        filter_action=filter_action,
        work_state_options=("requested", "acknowledged", "in_progress", "blocked", "result_returned", "declined", "cancelled"),
        as_of_iso=as_of_iso,
        as_of=as_of_text,
        sync_iso=sync_iso,
        sync_time=sync_text,
        filters=filters or [],
        items=rows,
        next_href=next_href,
        snapshot=snapshot_data,
        snapshot_fields=fields,
        events=[dict(to_jsonable(event)) for event in events],
        error=error,
    )

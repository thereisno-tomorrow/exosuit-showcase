SCHEMA_VERSION = 3

MIGRATIONS = {
    1: """
    CREATE TABLE requests (
        request_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        project TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        objective TEXT NOT NULL,
        sensitivity TEXT NOT NULL,
        work_state TEXT,
        revision INTEGER NOT NULL,
        recovery_hold INTEGER NOT NULL DEFAULT 0,
        prior_request_id TEXT REFERENCES requests(request_id),
        thread_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((mode = 'context_only' AND work_state IS NULL) OR mode = 'action_requested')
    );

    CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY,
        client_operation_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        operation_kind TEXT NOT NULL,
        message_kind TEXT,
        input_digest TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        operation_state TEXT,
        error_code TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        payload BLOB,
        payload_sha256 TEXT,
        provider_retry_key TEXT,
        recipient_address TEXT,
        route_source_id TEXT,
        route_destination_id TEXT,
        route_sensitivity TEXT,
        parent_message_id TEXT,
        provider_message_id TEXT,
        provider_thread_id TEXT,
        provider_parent_message_id TEXT,
        provider_accepted_at TEXT,
        attempt_started_at TEXT,
        recovery_required INTEGER NOT NULL DEFAULT 0,
        recovery_hold INTEGER NOT NULL DEFAULT 0,
        UNIQUE(actor_id, client_operation_id),
        UNIQUE(provider_retry_key)
    );

    CREATE UNIQUE INDEX one_pending_result_per_request
        ON operations(request_id)
        WHERE operation_kind = 'reply_result'
          AND operation_state IN ('queued', 'sending', 'uncertain');
    CREATE INDEX operations_dispatch ON operations(operation_state, accepted_at);
    CREATE INDEX operations_request ON operations(request_id, accepted_at);

    CREATE TABLE message_links (
        provider_message_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
        provider_thread_id TEXT NOT NULL,
        parent_message_id TEXT,
        message_kind TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        trust_label TEXT NOT NULL,
        exact_link_verified INTEGER NOT NULL
    );

    CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT,
        operation_id TEXT,
        actor_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    );
    CREATE INDEX events_request ON events(request_id, sequence);

    CREATE TABLE sync_state (
        name TEXT PRIMARY KEY,
        cursor TEXT,
        overlap_checkpoint TEXT,
        last_success_at TEXT
    );
    """,
    2: """
    ALTER TABLE operations ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE operations ADD COLUMN next_attempt_at TEXT;
    ALTER TABLE operations ADD COLUMN transport_error_code TEXT;
    ALTER TABLE operations ADD COLUMN transport_status INTEGER;
    ALTER TABLE operations ADD COLUMN uncertain_since TEXT;

    DROP INDEX operations_dispatch;
    CREATE INDEX operations_dispatch
        ON operations(operation_state, next_attempt_at, accepted_at);
    """,
    3: """
    ALTER TABLE requests ADD COLUMN backend_binding TEXT NOT NULL DEFAULT 'agentmail';
    ALTER TABLE operations ADD COLUMN backend_binding TEXT;
    UPDATE operations SET backend_binding='agentmail';
    CREATE TRIGGER bind_operation_backend AFTER INSERT ON operations
    BEGIN
        UPDATE operations SET backend_binding=(SELECT backend_binding FROM requests
            WHERE request_id=NEW.request_id) WHERE operation_id=NEW.operation_id;
    END;
    CREATE TRIGGER immutable_request_backend BEFORE UPDATE OF backend_binding ON requests
    WHEN NEW.backend_binding IS NOT OLD.backend_binding
    BEGIN SELECT RAISE(ABORT, 'conversation backend is immutable'); END;
    CREATE TRIGGER immutable_operation_backend BEFORE UPDATE OF backend_binding ON operations
    WHEN OLD.backend_binding IS NOT NULL AND NEW.backend_binding IS NOT OLD.backend_binding
    BEGIN SELECT RAISE(ABORT, 'operation backend is immutable'); END;
    """,

}

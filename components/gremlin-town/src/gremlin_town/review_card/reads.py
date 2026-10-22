"""Disposable, authorised inputs shared only within one card interaction."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from brain_core import work
from brain_core.dashboard import project_work
from .models import review_day
from .latency import phase


@dataclass(frozen=True)
class ReviewTaskProjection:
    parts: dict
    stages: dict
    opening_only: bool


@dataclass
class ReviewReadSession:
    now: object
    entries: tuple
    preferences: tuple
    sources: object
    log: object
    ranking: str
    revision: object = None
    index: object = field(init=False)
    day_projection: object = None
    weekly_projection: object = None
    task_projection: object = None

    def __post_init__(self):
        with phase("work_index"):
            self.index = work.index_things(entry.record for entry in self.entries)

    @property
    def day(self):
        return review_day(self.now, self.now.tzinfo)


class ReviewDetails(Mapping):
    """Canonical details on demand; list screens deliberately read their full set."""
    def __init__(self, session):
        self.session, self.built = session, {}
        self.raw = {entry.record.id: entry.record for entry in session.entries}

    def __iter__(self):
        return iter(self.session.index)

    def __contains__(self, ref):
        return ref in self.session.index

    def __len__(self):
        return len(self.session.index)

    def __getitem__(self, ref):
        if ref not in self.session.index:
            raise KeyError(ref)
        if ref not in self.built:
            self._build((ref,))
        return self.built[ref]

    def _build(self, refs):
        with phase("item_details"):
            projection = project_work(self.session.entries, self.session.now,
                history=self.session.log.history(), today=self.session.day,
                prepared_index=self.session.index, detail_refs=refs, include_ranking=False)
            for key, detail in projection["details"].items():
                if key not in self.built:
                    enrich_detail(self.session, key, detail, self.raw)
                    self.built[key] = detail

    def _all(self):
        if len(self.built) != len(self.session.index):
            self._build(None)
        return {ref: self.built[ref] for ref in self.session.index}

    def values(self):
        return self._all().values()

    def items(self):
        return self._all().items()


def enrich_detail(session, ref, detail, raw):
    index, record = session.index, session.index[ref]
    detail.update(planning=record.data.get("planning", {}),
        arrival_available=any(key in record.data for key in ("needs", "whose", "expected")),
        is_up=work.is_up(record, index, session.day),
        prerequisites_unfinished=work.has_unfinished_prerequisites(record, index),
        record_sha256=raw[record.id].sha256,
        original_words=(raw.get(ref, record).source or {}).get("words", ""))
    parent, crumbs = index.get(record.data.get("parent")), []
    while parent is not None:
        crumbs.insert(0, parent.title)
        parent = index.get(parent.data.get("parent"))
    detail["crumbs"] = crumbs
    detail["next_after"] = next((successor.title for successor in index.values()
        if ref in successor.data.get("after", ()) and successor.state == "open"), None)

from test_moves import ACTOR, NOW, item, run


def answer(record, *, suggested="Atlas application", reference="1"):
    return dict(reference=reference, question="Its name?", offered=["title"], thing=record.id,
                day=NOW.date().isoformat(), open=True, marks=["question"], suggested_name=suggested)


def test_yes_saves_only_shown_name_with_raw_reply_and_line_evidence():
    record = item("Application", data={"review_marks": [{"mark": "question", "text": "Its name?"}]})
    out = run([record], "yes", [{"move": "organise", "thing": record.id, "title": "Atlas application"}],
              answers=[answer(record)])
    assert out.refusal is None and out.flags == ()
    saved = out.records[0]
    assert saved.title == "Atlas application" and saved.source["words"] == "yes"
    assert not saved.data.get("review_marks")
    assert any(e.locator == "1" and "Suggested: Atlas application" in e.excerpt for e in saved.evidence)
    assert record.title == "Application"


def test_edited_name_is_his_words_without_accepting_agent_words():
    record = item("Application")
    out = run([record], "Call it Atlas job", [{"move": "organise", "thing": record.id, "title": "Atlas job"}],
              answers=[answer(record)])
    assert out.refusal is None and out.flags == () and out.records[0].title == "Atlas job"
    assert all("Suggested:" not in e.excerpt for e in out.records[0].evidence)


def test_no_or_different_name_or_no_shown_suggestion_cannot_rename_on_yes():
    record = item("Application", data={"review_marks": [{"mark": "question", "text": "Its name?"}]})
    before = record.as_dict()
    for message, suggested, title in [("no", "Atlas application", "Atlas application"),
                                      ("yes", "Atlas application", "Invented project"),
                                      ("yes", None, "Atlas application")]:
        out = run([record], message, [{"move": "organise", "thing": record.id, "title": title}],
                  answers=[answer(record, suggested=suggested)])
        assert out.records == () and out.reply == {}
        assert out.refusal["field"] == "title"
        assert out.refusal["message"] == "Application: renaming was not saved: say the name or accept the suggested name"
        assert "still-open review line" in out.refusal["fix"]
        assert record.as_dict() == before


def test_no_reference_others_words_and_unrelated_actions_get_no_name_authority():
    record = item("Application", data={"review_marks": [{"mark": "question", "text": "Its name?"}]})
    before = record.as_dict()
    out = run([record], "yes", [{"move": "organise", "thing": record.id, "title": "Atlas application"}])
    assert out.refusal is None and out.records[0].title == record.title
    assert out.records[0].data["review_marks"]
    assert all(e.locator != "1" for e in out.records[0].evidence)
    assert any(flag["field"] == "words" and flag["dropped"] for flag in out.flags)
    assert all("Suggested:" not in e.excerpt for e in out.records[0].evidence)
    for said_by, came_by in [("other", "direct"), ("owner", "connected_source")]:
        out = run([record], "yes", [{"move": "organise", "thing": record.id, "title": "Atlas application"}],
                  answers=[answer(record)], said_by=said_by, came_by=came_by, speaker="Diablo")
        assert out.records == () and out.reply == {}
        assert out.refusal["field"] == "said_by"
        assert out.refusal["message"] == "Application: renaming was not saved: someone else's words can only be saved as suggestions"
        assert "still-open review line" in out.refusal["fix"]
    out = run([record], "yes", [{"move": "next_action", "thing": record.id, "text": "Atlas application"}],
              answers=[{**answer(record), "offered": ["title", "next_action"]}])
    assert out.refusal is None and out.records[0].title == record.title
    assert any("agent's wording" in flag["message"] for flag in out.flags)
    assert all("Suggested:" not in e.excerpt for e in out.records[0].evidence)
    assert record.as_dict() == before


def test_name_answer_removes_only_exact_naming_question():
    record=item("Application",data={"review_marks":[{"mark":"question","text":"Its name?"},{"mark":"question","text":"What is the next action?"}]})
    out=run([record],"yes",[{"move":"organise","thing":record.id,"title":"Atlas application"}],answers=[answer(record)])
    assert out.refusal is None
    assert out.records[0].title == "Atlas application"
    assert out.records[0].data["review_marks"] == [{"mark":"question","text":"What is the next action?"}]


def test_edited_name_preserves_independent_classification_and_next_question():
    record=item("Application",data={"review_marks":[{"mark":"looks_like","kind":"project"},{"mark":"question","text":"Its name?"},{"mark":"question","text":"What is the next action?"}]})
    out=run([record],"Call it Atlas job",[{"move":"organise","thing":record.id,"title":"Atlas job"}],answers=[answer(record)])
    assert out.refusal is None
    assert out.records[0].title == "Atlas job"
    assert out.records[0].data["review_marks"] == [{"mark":"looks_like","kind":"project"},{"mark":"question","text":"What is the next action?"}]

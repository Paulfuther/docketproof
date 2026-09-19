import pytest

from arl.quiz.json_schema import (
    QuizJSONImportError,
    load_quiz_json_bytes,
    parse_quiz_json,
)


def test_parse_document_purpose_and_item_titles():
    title, description, questions = parse_quiz_json(
        {
            "document": "Security Assessment",
            "purpose": "Keep people safe",
            "items": [
                {"title": "Is the entrance visible?"},
                {"text": "Are lights working?"},
                {"title": ""},
                {"section": "Ignored without text"},
            ],
        }
    )
    assert title == "Security Assessment"
    assert description == "Keep people safe"
    assert questions == ["Is the entrance visible?", "Are lights working?"]


def test_parse_title_notes_export_shape():
    title, description, questions = parse_quiz_json(
        {
            "title": "Fall/Winter Exterior Merchandising",
            "notes": "Seasonal checklist",
            "items": [{"title": "No product in parking areas"}],
        }
    )
    assert title == "Fall/Winter Exterior Merchandising"
    assert description == "Seasonal checklist"
    assert questions == ["No product in parking areas"]


def test_parse_name_and_questions_key():
    title, description, questions = parse_quiz_json(
        {
            "name": "Site Quiz",
            "questions": ["Q1", {"text": "Q2"}],
        }
    )
    assert title == "Site Quiz"
    assert description == ""
    assert questions == ["Q1", "Q2"]


def test_parse_missing_title():
    with pytest.raises(QuizJSONImportError, match="title"):
        parse_quiz_json({"items": [{"title": "Q1"}]})


def test_parse_missing_items():
    with pytest.raises(QuizJSONImportError, match="items"):
        parse_quiz_json({"title": "Empty quiz"})


def test_load_invalid_json():
    with pytest.raises(QuizJSONImportError, match="Invalid JSON"):
        load_quiz_json_bytes(b"{not json")


def test_long_question_text_is_truncated():
    long_title = "A" * 300
    _title, _description, questions = parse_quiz_json(
        {"title": "Long", "items": [{"title": long_title}]}
    )
    assert len(questions[0]) == 255


@pytest.mark.django_db
def test_import_creates_quiz_questions_and_yes_no_answers():
    from arl.quiz.json_import import import_quiz_from_json

    quiz, questions = import_quiz_from_json(
        {
            "name": "Site Quiz",
            "description": "Imported",
            "items": [
                {"title": "Is fencing adequate?"},
                {"title": "Are lights working?"},
            ],
        }
    )
    assert quiz.title == "Site Quiz"
    assert quiz.description == "Imported"
    assert [question.text for question in questions] == [
        "Is fencing adequate?",
        "Are lights working?",
    ]
    for question in questions:
        answers = list(question.answers.order_by("id"))
        assert [answer.text for answer in answers] == ["Yes", "No"]
        assert answers[0].is_correct is True
        assert answers[1].is_correct is False

"""Create a Quiz from export-style JSON (text questions only)."""

import json

from django.db import transaction

from .models import Answer, Question, Quiz

QUESTION_TEXT_MAX_LENGTH = Question._meta.get_field("text").max_length


class QuizJSONImportError(ValueError):
    """Raised when uploaded JSON cannot be turned into a Quiz."""


def _first_nonempty(data, keys):
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clip_question_text(text):
    if len(text) <= QUESTION_TEXT_MAX_LENGTH:
        return text
    return text[:QUESTION_TEXT_MAX_LENGTH]


def parse_quiz_json(payload):
    """Map export-ish JSON to (title, description, question_texts).

    Accepted top-level title keys: name | title | document
    Accepted description keys: description | notes | purpose
    Question list: items[] or questions[]
    Question text keys: text | title
    """
    if not isinstance(payload, dict):
        raise QuizJSONImportError("JSON must be an object at the top level.")

    title = _first_nonempty(payload, ("name", "title", "document"))
    if not title:
        raise QuizJSONImportError(
            "Quiz title is required. Use name, title, or document."
        )

    description = _first_nonempty(payload, ("description", "notes", "purpose"))

    items = payload.get("items")
    if items is None:
        items = payload.get("questions")

    if not isinstance(items, list) or not items:
        raise QuizJSONImportError(
            "JSON must include a non-empty items[] (or questions[]) list."
        )

    question_texts = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = _first_nonempty(item, ("text", "title"))
        else:
            text = ""
        if not text:
            continue
        question_texts.append(_clip_question_text(text))

    if not question_texts:
        raise QuizJSONImportError(
            "No questions found. Each item needs a text or title field."
        )

    return title, description, question_texts


def load_quiz_json_bytes(raw):
    """Decode uploaded file bytes/str into a JSON object."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise QuizJSONImportError(f"File is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuizJSONImportError(f"Invalid JSON: {exc}") from exc
    return payload


def import_quiz_from_json(payload):
    """Create one Quiz, its Questions, and Yes/No answers for take-quiz."""
    title, description, question_texts = parse_quiz_json(payload)

    with transaction.atomic():
        quiz = Quiz.objects.create(
            title=title,
            description=description or None,
        )
        questions = []
        for text in question_texts:
            question = Question.objects.create(quiz=quiz, text=text)
            # take_quiz.html posts yes/no; scoring matches answer.text.lower().
            Answer.objects.bulk_create(
                [
                    Answer(question=question, text="Yes", is_correct=True),
                    Answer(question=question, text="No", is_correct=False),
                ]
            )
            questions.append(question)

    return quiz, questions

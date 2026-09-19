"""Create a Quiz from export-style JSON (text questions only)."""

from django.db import transaction

from .json_schema import (
    QuizJSONImportError,
    load_quiz_json_bytes,
    parse_quiz_json,
)
from .models import Answer, Question, Quiz

__all__ = [
    "QuizJSONImportError",
    "import_quiz_from_json",
    "load_quiz_json_bytes",
    "parse_quiz_json",
]


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

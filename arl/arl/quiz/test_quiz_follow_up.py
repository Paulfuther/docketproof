from django.contrib import admin
from django.test import TestCase
from django.urls import reverse
from waffle.models import Flag

import arl.user.admin  # noqa: F401  registers Quiz and Question admin
from arl.quiz.models import Answer, Question, Quiz
from arl.user.models import CustomUser


class QuestionFollowUpTests(TestCase):
    def test_defaults_do_not_treat_no_or_yes_as_follow_up(self):
        question = Question(text="Is the exit clear?")
        self.assertEqual(question.follow_up_answers(), [])
        self.assertFalse(question.needs_follow_up("no"))
        self.assertFalse(question.needs_follow_up("yes"))
        self.assertFalse(question.needs_responsibility("yes"))
        self.assertFalse(question.needs_responsibility("no"))
        self.assertEqual(question.submission_errors("no"), [])
        self.assertEqual(question.submission_errors("yes"), [])

    def test_no_can_be_correct_without_follow_up(self):
        question = Question(
            text="Was anyone injured?",
            follow_up_on_no=False,
            responsibility_assignable=True,
        )
        self.assertFalse(question.needs_follow_up("N"))
        self.assertFalse(question.needs_responsibility("no"))
        self.assertEqual(question.submission_errors("no", "", ""), [])

    def test_no_follow_up_requires_note_and_ls_only_when_configured(self):
        question = Question(
            text="Is the spill cleaned up?",
            follow_up_on_no=True,
            responsibility_assignable=True,
        )
        self.assertEqual(question.follow_up_answers(), ["N"])
        self.assertTrue(question.needs_follow_up("no"))
        self.assertFalse(question.needs_follow_up("yes"))
        self.assertFalse(question.needs_responsibility("yes"))
        self.assertTrue(question.needs_responsibility("no"))
        self.assertEqual(question.submission_errors("yes"), [])
        self.assertIn(
            "This answer requires a follow-up.",
            question.submission_errors("no"),
        )
        self.assertIn(
            "Choose L or S responsibility.",
            question.submission_errors("no", "Mop the aisle"),
        )
        self.assertEqual(
            question.submission_errors("no", "Mop the aisle", "L"),
            [],
        )

    def test_yes_does_not_require_l_unless_yes_is_the_follow_up(self):
        responsibility_only = Question(
            text="Are the lights on?",
            follow_up_on_yes=False,
            responsibility_assignable=True,
        )
        self.assertFalse(responsibility_only.needs_responsibility("yes"))
        self.assertEqual(responsibility_only.submission_errors("yes", "", ""), [])

        yes_is_problem = Question(
            text="Is the equipment damaged?",
            follow_up_on_yes=True,
            responsibility_assignable=True,
        )
        self.assertTrue(yes_is_problem.needs_follow_up("Y"))
        self.assertTrue(yes_is_problem.needs_responsibility("yes"))
        self.assertFalse(yes_is_problem.needs_responsibility("no"))
        errors = yes_is_problem.submission_errors("yes", "Tag it out")
        self.assertEqual(errors, ["Choose L or S responsibility."])
        self.assertEqual(
            yes_is_problem.submission_errors("yes", "Tag it out", "S"),
            [],
        )

    def test_follow_up_without_responsibility_does_not_require_l(self):
        question = Question(
            text="Did the alarm sound?",
            follow_up_on_yes=True,
            responsibility_assignable=False,
        )
        self.assertTrue(question.needs_follow_up("yes"))
        self.assertFalse(question.needs_responsibility("yes"))
        self.assertEqual(
            question.submission_errors("yes", "It did, during the drill"),
            [],
        )
        self.assertIn(
            "This answer requires a follow-up.",
            question.submission_errors("yes"),
        )


class TakeQuizFollowUpTests(TestCase):
    def setUp(self):
        Flag.objects.create(name="quiz", everyone=True)
        self.user = CustomUser.objects.create(
            username="quiztaker",
            email="quiztaker@example.com",
            phone_number="+15196707469",
        )
        self.client.force_login(self.user)
        self.quiz = Quiz.objects.create(title="Shift check")
        self.no_is_correct = Question.objects.create(
            quiz=self.quiz,
            text="Was anyone injured?",
            follow_up_on_no=False,
            follow_up_on_yes=True,
            responsibility_assignable=True,
        )
        Answer.objects.create(
            question=self.no_is_correct, text="Yes", is_correct=False
        )
        Answer.objects.create(
            question=self.no_is_correct, text="No", is_correct=True
        )
        self.no_needs_follow_up = Question.objects.create(
            quiz=self.quiz,
            text="Is the spill cleaned up?",
            follow_up_on_no=True,
            responsibility_assignable=True,
        )
        Answer.objects.create(
            question=self.no_needs_follow_up, text="Yes", is_correct=True
        )
        Answer.objects.create(
            question=self.no_needs_follow_up, text="No", is_correct=False
        )

    def _post(self, **extra):
        data = {
            f"question_{self.no_is_correct.id}": "no",
            f"question_{self.no_needs_follow_up.id}": "yes",
        }
        data.update(extra)
        return self.client.post(reverse("take_quiz", args=[self.quiz.id]), data)

    def test_no_correct_and_yes_do_not_require_follow_up_or_l(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You scored 2 out of 2")

    def test_bare_no_does_not_force_follow_up_when_flag_is_off(self):
        response = self._post(
            **{f"question_{self.no_needs_follow_up.id}": "yes"}
        )
        self.assertNotContains(response, "This answer requires a follow-up.")
        self.assertContains(response, "You scored 2 out of 2")

    def test_configured_no_requires_follow_up_and_l(self):
        response = self._post(
            **{f"question_{self.no_needs_follow_up.id}": "no"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This answer requires a follow-up.")
        self.assertContains(response, "Choose L or S responsibility.")
        self.assertNotContains(response, "You scored")

        response = self._post(
            **{
                f"question_{self.no_needs_follow_up.id}": "no",
                f"follow_up_{self.no_needs_follow_up.id}": "Cone the aisle",
                f"responsibility_{self.no_needs_follow_up.id}": "L",
            }
        )
        self.assertContains(response, "You scored 1 out of 2")

    def test_yes_requires_follow_up_and_l_only_when_yes_is_configured(self):
        response = self._post(
            **{f"question_{self.no_is_correct.id}": "yes"}
        )
        self.assertContains(response, "This answer requires a follow-up.")
        self.assertContains(response, "Choose L or S responsibility.")

        response = self._post(
            **{
                f"question_{self.no_is_correct.id}": "yes",
                f"follow_up_{self.no_is_correct.id}": "First aid given",
                f"responsibility_{self.no_is_correct.id}": "S",
            }
        )
        self.assertContains(response, "You scored 1 out of 2")

    def test_yes_follow_up_without_responsibility_skips_l(self):
        self.no_is_correct.responsibility_assignable = False
        self.no_is_correct.save(update_fields=["responsibility_assignable"])
        response = self._post(
            **{
                f"question_{self.no_is_correct.id}": "yes",
                f"follow_up_{self.no_is_correct.id}": "First aid given",
            }
        )
        self.assertContains(response, "You scored 1 out of 2")
        self.assertNotContains(response, "Choose L or S responsibility.")


class QuestionAdminFollowUpTests(TestCase):
    def test_admin_exposes_follow_up_checkboxes(self):
        question_admin = admin.site._registry[Question]
        self.assertIn("follow_up_on_yes", question_admin.fields)
        self.assertIn("follow_up_on_no", question_admin.fields)
        self.assertIn("responsibility_assignable", question_admin.fields)
        inline = admin.site._registry[Quiz].inlines[0]
        self.assertIn("follow_up_on_no", inline.fields)
        self.assertIn("follow_up_on_yes", inline.fields)

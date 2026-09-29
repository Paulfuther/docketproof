from datetime import timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from arl.quiz.models import Checklist
from arl.quiz.views import CHECKLIST_PAGE_SIZE
from arl.user.models import CustomUser, Employer, Store


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ChecklistDashboardPaginationTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Pages")
        self.user = CustomUser.objects.create_user(
            username="checklist-pages",
            email="checklist-pages@example.com",
            password="pass12345",
            phone_number="+15196707501",
            employer=self.employer,
        )
        self.other_user = CustomUser.objects.create_user(
            username="checklist-pages-other",
            email="checklist-pages-other@example.com",
            password="pass12345",
            phone_number="+15196707502",
            employer=self.employer,
        )
        self.store = Store.objects.create(
            number=21,
            employer=self.employer,
            address="21 Main St",
            city="London",
            province="ON",
        )
        self.client.force_login(self.user)

    def _checklist(self, *, title, status, minutes_ago, user=None, notes=""):
        when = timezone.now() - timedelta(minutes=minutes_ago)
        owner = user or self.user
        checklist = Checklist.objects.create(
            title=title,
            status=status,
            created_by=owner,
            submitted_by=owner if status != "draft" else None,
            store=self.store,
            notes=notes,
        )
        Checklist.objects.filter(pk=checklist.pk).update(
            created_at=when,
            submitted_at=when if status != "draft" else None,
        )
        return checklist

    def _fill(self, status, count, title_prefix, user=None):
        for i in range(count):
            self._checklist(
                title=f"{title_prefix} {i:04d}",
                status=status,
                minutes_ago=i,
                user=user,
            )

    def test_page_size_limits_each_tab(self):
        total = CHECKLIST_PAGE_SIZE + 5
        self._fill("draft", total, "Draft")
        self._fill("submitted", total, "Submitted")

        page = self.client.get(reverse("checklist_dashboard"), {"tab": "submitted"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Submitted 0000")
        self.assertContains(page, f"Submitted {CHECKLIST_PAGE_SIZE - 1:04d}")
        self.assertNotContains(page, f"Submitted {CHECKLIST_PAGE_SIZE:04d}")
        self.assertContains(page, "Draft 0000")
        self.assertNotContains(page, f"Draft {CHECKLIST_PAGE_SIZE:04d}")
        self.assertContains(page, f"Showing 1–{CHECKLIST_PAGE_SIZE} of {total}")
        self.assertContains(page, "Page 1 of 2")
        self.assertContains(page, f">{total}<", count=2)
        self.assertContains(page, "inprogress_page=2")
        self.assertContains(page, "submitted_page=2")
        self.assertContains(
            page, 'class="tab-pane fade show active" id="submitted"'
        )
        self.assertContains(page, "Store 21")

    def test_second_page_keeps_filters_and_newest_first_order(self):
        total = CHECKLIST_PAGE_SIZE + 3
        self._fill("submitted", total, "Walk")
        self._checklist(
            title="Walk secret",
            status="submitted",
            minutes_ago=1,
            user=self.other_user,
        )
        self._checklist(
            title="Other pad",
            status="submitted",
            minutes_ago=0,
            notes="not a match",
        )

        query = {"tab": "submitted", "q": "Walk", "mine": "1"}
        page1 = self.client.get(reverse("checklist_dashboard"), query)
        self.assertContains(page1, "Walk 0000")
        self.assertNotContains(page1, "Walk secret")
        self.assertNotContains(page1, "Other pad")
        self.assertNotContains(page1, f"Walk {CHECKLIST_PAGE_SIZE:04d}")
        next_link = "tab=submitted&submitted_page=2&q=Walk&mine=1"
        self.assertContains(page1, next_link)

        page2 = self.client.get(
            reverse("checklist_dashboard"), {**query, "submitted_page": 2}
        )
        self.assertContains(page2, f"Walk {CHECKLIST_PAGE_SIZE:04d}")
        self.assertContains(page2, f"Walk {total - 1:04d}")
        self.assertNotContains(page2, "Walk 0000")
        self.assertNotContains(page2, "Walk secret")
        self.assertNotContains(page2, "Other pad")
        self.assertContains(
            page2, 'class="tab-pane fade show active" id="submitted"'
        )
        self.assertContains(
            page2,
            f"Showing {CHECKLIST_PAGE_SIZE + 1}–{total} of {total}",
        )
        self.assertContains(page2, "Page 2 of 2")
        self.assertContains(
            page2, "tab=submitted&submitted_page=1&q=Walk&mine=1"
        )

    def test_empty_page_and_out_of_range_page(self):
        empty = self.client.get(
            reverse("checklist_dashboard"),
            {"tab": "submitted", "submitted_page": 4},
        )
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, "No submitted checklists.")
        self.assertContains(empty, "No drafts.")
        self.assertContains(
            empty,
            '<span class="badge bg-light text-dark">0</span>',
            count=3,
        )
        self.assertNotContains(empty, "Previous")
        self.assertNotContains(empty, "Page 1 of")

        self._fill("submitted", CHECKLIST_PAGE_SIZE + 1, "Lot")
        not_a_number = self.client.get(
            reverse("checklist_dashboard"),
            {"tab": "submitted", "submitted_page": "nope"},
        )
        self.assertEqual(not_a_number.status_code, 200)
        self.assertContains(not_a_number, "Lot 0000")
        self.assertContains(not_a_number, "Page 1 of 2")

        past_end = self.client.get(
            reverse("checklist_dashboard"),
            {"tab": "submitted", "submitted_page": 99},
        )
        self.assertEqual(past_end.status_code, 200)
        self.assertContains(past_end, f"Lot {CHECKLIST_PAGE_SIZE:04d}")
        self.assertNotContains(past_end, "Lot 0000")
        self.assertContains(past_end, "Page 2 of 2")
        self.assertContains(
            past_end, 'class="tab-pane fade show active" id="submitted"'
        )

        empty_drafts = self.client.get(
            reverse("checklist_dashboard"),
            {"tab": "inprogress", "inprogress_page": 99},
        )
        self.assertEqual(empty_drafts.status_code, 200)
        self.assertContains(empty_drafts, "No drafts.")
        self.assertContains(
            empty_drafts, 'class="tab-pane fade show active" id="inprogress"'
        )

    def test_unknown_tab_falls_back_to_available(self):
        page = self.client.get(reverse("checklist_dashboard"), {"tab": "nope"})
        self.assertContains(
            page, 'class="tab-pane fade show active" id="available"'
        )

    def test_queries_fetch_only_the_current_page(self):
        self._fill("submitted", CHECKLIST_PAGE_SIZE + 15, "Ice")
        self._fill("draft", CHECKLIST_PAGE_SIZE + 15, "Draft")
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                reverse("checklist_dashboard"), {"tab": "submitted"}
            )
        self.assertEqual(response.status_code, 200)
        checklist_sql = [
            query["sql"]
            for query in captured.captured_queries
            if _is_checklist_table(query["sql"])
        ]
        counts = [
            sql
            for sql in checklist_sql
            if sql.lstrip().upper().startswith("SELECT COUNT")
        ]
        pages = [sql for sql in checklist_sql if sql not in counts]
        self.assertEqual(len(counts), 2)
        self.assertEqual(len(pages), 2)
        for sql in pages:
            self.assertIn("LIMIT", sql.upper())
            self.assertIn(str(CHECKLIST_PAGE_SIZE), sql)
            self.assertIn("user_store", sql)


def _is_checklist_table(sql):
    lowered = sql.lower()
    if "quiz_checklist" not in lowered:
        return False
    for other in (
        "quiz_checklisttemplate",
        "quiz_checklistitem",
        "quiz_checklistaction",
    ):
        if other in lowered:
            return False
    return True

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from arl.quiz.models import SaltLog
from arl.quiz.views import SALT_LOG_PAGE_SIZE
from arl.user.models import CustomUser, Employer, Store


def _jpeg_upload(name="evidence.jpg"):
    buf = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class SaltLogFlowTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Test")
        self.other_employer = Employer.objects.create(name="Other Co")
        self.user = CustomUser.objects.create_user(
            username="salt-gsa",
            email="salt-gsa@example.com",
            password="pass12345",
            phone_number="+15196707481",
            employer=self.employer,
        )
        self.other_user = CustomUser.objects.create_user(
            username="other-gsa",
            email="other-gsa@example.com",
            password="pass12345",
            phone_number="+15196707482",
            employer=self.other_employer,
        )
        self.store = Store.objects.create(
            number=12,
            employer=self.employer,
            address="12 Main St",
            city="London",
            province="ON",
        )
        self.other_store = Store.objects.create(
            number=99,
            employer=self.other_employer,
            address="99 Other St",
            city="Toronto",
            province="ON",
        )
        self.second_store = Store.objects.create(
            number=14,
            employer=self.employer,
            address="14 Main St",
            city="Windsor",
            province="ON",
        )
        self.client.force_login(self.user)
        self.images = patch(
            "arl.quiz.views.get_s3_images_for_salt_log", return_value=[]
        )
        self.images.start()
        self.addCleanup(self.images.stop)
        self.pdf = patch("arl.quiz.views.generate_salt_log_pdf_task.delay")
        self.pdf_delay = self.pdf.start()
        self.addCleanup(self.pdf.stop)

    def _post_data(self, **overrides):
        data = {
            "store": str(self.store.pk),
            "area_salted": "Front walk",
            "date_salted": "2026-01-15",
            "time_salted": "08:30",
            "levels_ok": "yes",
            "exception_what": "",
            "exception_who": "",
            "exception_when": "",
            "action": "save",
        }
        data.update(overrides)
        return data

    def _start(self):
        resp = self.client.post(reverse("create_salt_log"))
        salt_log = SaltLog.objects.get(user=self.user)
        return resp, salt_log

    def test_start_saves_a_draft_without_celery(self):
        resp, salt_log = self._start()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp.url, reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        )
        self.assertEqual(salt_log.status, SaltLog.STATUS_DRAFT)
        self.assertEqual(salt_log.user_employer, self.employer)
        self.assertTrue(salt_log.image_folder)
        self.assertIsNotNone(salt_log.date_salted)
        self.assertIsNotNone(salt_log.time_salted)
        self.pdf_delay.assert_not_called()
        self.assertEqual(SaltLog.objects.count(), 1)

    def test_legacy_create_url_opens_the_dashboard_start_form(self):
        resp = self.client.get(reverse("create_salt_log_legacy"))
        self.assertRedirects(resp, reverse("salt_log_list"))
        page = self.client.get(reverse("salt_log_list"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Create site salt log")
        self.assertContains(page, "Area salted")
        salt_log = SaltLog.objects.get(user=self.user)
        self.assertEqual(salt_log.status, SaltLog.STATUS_DRAFT)

    def test_account_without_employer_does_not_create_a_row(self):
        loner = CustomUser.objects.create_user(
            username="no-company",
            email="no-company@example.com",
            password="pass12345",
            phone_number="+15196707483",
        )
        self.client.force_login(loner)
        resp = self.client.post(reverse("create_salt_log"))
        self.assertRedirects(resp, reverse("salt_log_list"))
        self.assertEqual(SaltLog.objects.count(), 0)

    def test_autosave_persists_draft_and_keeps_the_photo_folder(self):
        _, salt_log = self._start()
        folder = salt_log.image_folder
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk}) + "?autosave=1"
        resp = self.client.post(url, self._post_data(area_salted="Side lot"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        salt_log.refresh_from_db()
        self.assertEqual(salt_log.area_salted, "Side lot")
        self.assertEqual(salt_log.store, self.store)
        self.assertEqual(salt_log.status, SaltLog.STATUS_DRAFT)
        self.assertEqual(salt_log.image_folder, folder)
        self.assertIsNone(salt_log.submitted_at)

    def test_incomplete_submit_stays_on_the_edit_page(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        resp = self.client.post(
            url,
            self._post_data(
                store="",
                area_salted="",
                date_salted="",
                time_salted="",
                levels_ok="",
                action="submit",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Please fix the errors below.")
        salt_log.refresh_from_db()
        self.assertEqual(salt_log.status, SaltLog.STATUS_DRAFT)
        self.pdf_delay.assert_not_called()

    def test_levels_ok_submits_without_an_exception(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        resp = self.client.post(url, self._post_data(action="submit"))
        self.assertRedirects(resp, reverse("salt_log_list"))
        salt_log.refresh_from_db()
        self.assertEqual(salt_log.status, SaltLog.STATUS_SUBMITTED)
        self.assertEqual(salt_log.submitted_by, self.user)
        self.assertIsNotNone(salt_log.submitted_at)
        self.assertTrue(salt_log.levels_ok)
        self.pdf_delay.assert_called_once_with(salt_log.pk)

    def test_levels_not_ok_requires_what_who_when_on_the_edit_page(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        resp = self.client.post(
            url, self._post_data(levels_ok="no", action="submit")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Say what was wrong.")
        self.assertContains(resp, "Say who was told.")
        self.assertContains(resp, "Say when it was reported.")
        salt_log.refresh_from_db()
        self.assertEqual(salt_log.status, SaltLog.STATUS_DRAFT)

    def test_exception_path_submits(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        resp = self.client.post(
            url,
            self._post_data(
                levels_ok="no",
                exception_what="Below the line",
                exception_who="Shift lead",
                exception_when="2026-01-15T09:05",
                action="submit",
            ),
        )
        self.assertRedirects(resp, reverse("salt_log_list"))
        salt_log.refresh_from_db()
        self.assertFalse(salt_log.levels_ok)
        self.assertEqual(salt_log.exception_what, "Below the line")
        self.assertEqual(salt_log.exception_who, "Shift lead")
        reported = timezone.localtime(salt_log.exception_when)
        self.assertEqual((reported.hour, reported.minute), (9, 5))
        self.assertEqual(salt_log.status, SaltLog.STATUS_SUBMITTED)

    def test_other_employer_cannot_open_edit_or_legacy_url(self):
        _, salt_log = self._start()
        self.client.force_login(self.other_user)
        edit = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        legacy = reverse("salt_log_update", kwargs={"pk": salt_log.pk})
        self.assertEqual(self.client.get(edit).status_code, 404)
        self.assertEqual(self.client.get(legacy).status_code, 404)
        self.assertEqual(self.client.post(edit, self._post_data()).status_code, 404)

    def test_store_dropdown_and_post_are_employer_scoped(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        page = self.client.get(url)
        self.assertContains(page, f'value="{self.store.pk}"')
        self.assertContains(page, f'value="{self.second_store.pk}"')
        self.assertNotContains(page, f'value="{self.other_store.pk}"')

        resp = self.client.post(
            url + "?autosave=1",
            self._post_data(store=str(self.other_store.pk)),
        )
        self.assertEqual(resp.status_code, 422)
        salt_log.refresh_from_db()
        self.assertIsNone(salt_log.store_id)

    def test_menu_lists_only_this_employer_and_splits_status(self):
        _, mine = self._start()
        mine.area_salted = "Our walk"
        mine.store = self.store
        mine.save(update_fields=["area_salted", "store"])
        SaltLog.objects.create(
            user=self.other_user,
            user_employer=self.other_employer,
            store=self.other_store,
            area_salted="Their secret walk",
            status=SaltLog.STATUS_DRAFT,
            image_folder="other-folder",
        )
        done = SaltLog.objects.create(
            user=self.user,
            user_employer=self.employer,
            store=self.second_store,
            area_salted="Finished pad",
            status=SaltLog.STATUS_COMPLETED,
            image_folder="done-folder",
        )
        page = self.client.get(reverse("salt_log_list"))
        self.assertContains(page, "Our walk")
        self.assertContains(page, "Finished pad")
        self.assertNotContains(page, "Their secret walk")
        self.assertContains(page, reverse("salt_log_edit", kwargs={"pk": mine.pk}))
        self.assertContains(page, reverse("salt_log_edit", kwargs={"pk": done.pk}))

        filtered = self.client.get(
            reverse("salt_log_list"), {"store": str(self.second_store.pk)}
        )
        self.assertContains(filtered, "Finished pad")
        self.assertNotContains(filtered, "Our walk")

    def test_submitted_log_rejects_another_save(self):
        _, salt_log = self._start()
        url = reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        self.client.post(url, self._post_data(action="submit"))
        salt_log.refresh_from_db()
        resp = self.client.post(
            url, self._post_data(area_salted="Changed after submit", action="save")
        )
        self.assertRedirects(resp, reverse("salt_log_list"))
        salt_log.refresh_from_db()
        self.assertEqual(salt_log.area_salted, "Front walk")


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class SaltLogPhotoUploadTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Test")
        self.other_employer = Employer.objects.create(name="Other Co")
        self.user = CustomUser.objects.create_user(
            username="salt-photo",
            email="salt-photo@example.com",
            password="pass12345",
            phone_number="+15196707484",
            employer=self.employer,
        )
        self.other_user = CustomUser.objects.create_user(
            username="salt-photo-other",
            email="salt-photo-other@example.com",
            password="pass12345",
            phone_number="+15196707485",
            employer=self.other_employer,
        )
        self.salt_log = SaltLog.objects.create(
            user=self.user,
            user_employer=self.employer,
            status=SaltLog.STATUS_DRAFT,
            image_folder="front-walk-abc123",
            area_salted="Front walk",
        )
        self.client.force_login(self.user)
        self.upload = patch("arl.quiz.views.upload_to_linode_object_storage")
        self.upload_mock = self.upload.start()
        self.addCleanup(self.upload.stop)

    def test_upload_uses_the_saved_folder_not_the_client_folder(self):
        resp = self.client.post(
            reverse("salt_log_upload"),
            {
                "salt_log": str(self.salt_log.pk),
                "image_folder": "../other-company/secret",
                "file": _jpeg_upload(),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        key = self.upload_mock.call_args.args[1]
        self.assertTrue(
            key.startswith(f"SALTLOG/{self.employer}/{self.salt_log.image_folder}/")
        )
        self.assertTrue(key.endswith(".jpg"))
        self.assertNotIn("secret", key)

    def test_upload_requires_a_row_and_stays_inside_the_employer(self):
        missing = self.client.post(
            reverse("salt_log_upload"),
            {"file": _jpeg_upload()},
        )
        self.assertEqual(missing.status_code, 400)

        self.client.force_login(self.other_user)
        denied = self.client.post(
            reverse("salt_log_upload"),
            {"salt_log": str(self.salt_log.pk), "file": _jpeg_upload("nope.jpg")},
        )
        self.assertEqual(denied.status_code, 404)
        self.upload_mock.assert_not_called()

    def test_upload_rejects_a_submitted_log(self):
        self.salt_log.status = SaltLog.STATUS_SUBMITTED
        self.salt_log.save(update_fields=["status"])
        resp = self.client.post(
            reverse("salt_log_upload"),
            {"salt_log": str(self.salt_log.pk), "file": _jpeg_upload()},
        )
        self.assertEqual(resp.status_code, 409)
        self.upload_mock.assert_not_called()


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class SaltLogPdfTaskTests(TestCase):
    @patch("arl.quiz.tasks.master_upload_file_to_dropbox", return_value=(True, "ok"))
    @patch("arl.quiz.tasks.pdfkit.from_string", return_value=b"%PDF")
    @patch("arl.quiz.tasks.get_s3_images_for_salt_log", return_value=[])
    def test_pdf_lands_under_saltlogs_and_marks_completed(
        self, _images, _pdfkit, upload
    ):
        employer = Employer.objects.create(name="Petro Test")
        user = CustomUser.objects.create_user(
            username="salt-pdf",
            email="salt-pdf@example.com",
            password="pass12345",
            phone_number="+15196707486",
            employer=employer,
        )
        store = Store.objects.create(number=12, employer=employer)
        salt_log = SaltLog.objects.create(
            user=user,
            user_employer=employer,
            store=store,
            area_salted="Front walk",
            status=SaltLog.STATUS_SUBMITTED,
            image_folder="keep-this-folder",
            levels_ok=False,
            exception_what="Low",
            exception_who="Lead",
        )
        from arl.quiz.tasks import generate_salt_log_pdf_task

        result = generate_salt_log_pdf_task(salt_log.pk)
        salt_log.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(salt_log.status, SaltLog.STATUS_COMPLETED)
        self.assertEqual(salt_log.image_folder, "keep-this-folder")
        self.assertTrue(
            salt_log.pdf_path.startswith("/SALTLOGS/petro-test/salt-log/12/")
        )
        self.assertTrue(salt_log.pdf_path.endswith(f"12_salt-log-{salt_log.pk}.pdf"))
        uploaded_path = upload.call_args.args[1]
        self.assertEqual(uploaded_path, salt_log.pdf_path)


class SaltLogPathAndTemplateTests(SimpleTestCase):
    def test_dropbox_path_matches_checklist_shape_under_saltlogs(self):
        from arl.quiz.dropbox_paths import build_salt_log_dropbox_path

        salt_log = SimpleNamespace(
            pk=42,
            user_employer=SimpleNamespace(name="Petro Canada"),
        )
        path = build_salt_log_dropbox_path(
            salt_log, "12", when=datetime(2026, 9, 20)
        )
        self.assertEqual(
            path,
            "/SALTLOGS/petro-canada/salt-log/12/2026/09-September/"
            "12_salt-log-42.pdf",
        )

    def test_edit_template_posts_with_formdata_and_its_own_dropzone(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "quiz"
            / "salt_log_edit.html"
        )
        text = template.read_text()
        self.assertIn("function saltLogFormData", text)
        self.assertIn("autosave=1", text)
        self.assertIn("data-action=\"submit\"", text)
        self.assertIn("id=\"salt-log-form-action\"", text)
        self.assertIn("fd.delete('action')", text)
        self.assertIn("data.ok", text)
        self.assertIn("salt_log_upload", text)
        self.assertIn('formData.append("salt_log"', text)
        self.assertIn('data-value="{{ radio.data.value }}"', text)
        self.assertNotIn("previewsContainer: null", text)
        self.assertNotIn("createImageThumbnails: false", text)
        self.assertIn("thumbnailMethod: \"contain\"", text)
        self.assertIn("thumbnailWidth: 160", text)
        self.assertIn("object-fit: contain !important", text)
        self.assertIn('id="salt-photo-lightbox"', text)
        self.assertIn("data-salt-photo", text)
        self.assertIn(".dz-success-mark", text)
        self.assertNotIn("object-fit: cover", text)
        self.assertNotIn('enctype="multipart/form-data"', text)
        self.assertNotIn("image_folder", text)

    def test_pdf_template_embeds_photos_at_full_aspect(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "quiz"
            / "salt_log_form_pdf.html"
        )
        text = template.read_text()
        self.assertIn("height: auto", text)
        self.assertNotIn("object-fit: cover", text)
        self.assertNotIn("height: 100px", text)

    def test_dead_hardcoded_pdf_view_is_gone(self):
        import arl.quiz.views as quiz_views

        self.assertFalse(hasattr(quiz_views, "generate_salt_log_pdf"))
        self.assertFalse(hasattr(quiz_views, "SaltLogUpdateView"))
        self.assertFalse(hasattr(quiz_views, "handle_new_incident_form_creation"))


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class SaltLogListPaginationTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Pages")
        self.other_employer = Employer.objects.create(name="Other Pages")
        self.user = CustomUser.objects.create_user(
            username="salt-pages",
            email="salt-pages@example.com",
            password="pass12345",
            phone_number="+15196707490",
            employer=self.employer,
        )
        self.store = Store.objects.create(
            number=21,
            employer=self.employer,
            address="21 Main St",
            city="London",
            province="ON",
        )
        self.other_store = Store.objects.create(
            number=22,
            employer=self.employer,
            address="22 Main St",
            city="Windsor",
            province="ON",
        )
        self.client.force_login(self.user)

    def _log(self, *, area, status, minutes_ago, store=None):
        when = timezone.now() - timedelta(minutes=minutes_ago)
        log = SaltLog.objects.create(
            user=self.user,
            user_employer=self.employer,
            store=store or self.store,
            area_salted=area,
            status=status,
            image_folder=f"page-{status}-{minutes_ago}-{area}",
        )
        SaltLog.objects.filter(pk=log.pk).update(
            hidden_timestamp=when,
            submitted_at=when if status != SaltLog.STATUS_DRAFT else None,
        )
        return log

    def _fill(self, status, count, area_prefix="Pad", store=None):
        for i in range(count):
            self._log(
                area=f"{area_prefix} {i:04d}",
                status=status,
                minutes_ago=i,
                store=store,
            )

    def test_page_size_limits_the_edit_list(self):
        self.assertEqual(SALT_LOG_PAGE_SIZE, 5)
        total = SALT_LOG_PAGE_SIZE + 5
        self._fill(SaltLog.STATUS_SUBMITTED, total, area_prefix="Submitted")

        page = self.client.get(reverse("salt_log_list"), {"tab": "edit"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Submitted 0000")
        self.assertContains(page, f"Submitted {SALT_LOG_PAGE_SIZE - 1:04d}")
        self.assertNotContains(page, f"Submitted {SALT_LOG_PAGE_SIZE:04d}")
        self.assertContains(page, f"Showing 1–{SALT_LOG_PAGE_SIZE} of {total}")
        self.assertContains(page, "Page 1 of 2")
        self.assertContains(page, f">{total}<")
        self.assertContains(page, "tab=edit&page=2")
        self.assertContains(page, 'class="tab-pane fade show active" id="edit"')
        self.assertNotContains(page, "Start salt log")

    def test_second_page_keeps_filters_and_newest_first_order(self):
        total = SALT_LOG_PAGE_SIZE + 3
        self._fill(SaltLog.STATUS_SUBMITTED, total, area_prefix="Walk")
        self._log(
            area="Other store walk",
            status=SaltLog.STATUS_SUBMITTED,
            minutes_ago=1,
            store=self.other_store,
        )

        query = {"tab": "edit", "q": "Walk", "store": str(self.store.pk)}
        page1 = self.client.get(reverse("salt_log_list"), query)
        self.assertContains(page1, "Walk 0000")
        self.assertNotContains(page1, "Other store walk")
        self.assertNotContains(page1, f"Walk {SALT_LOG_PAGE_SIZE:04d}")
        next_link = f"tab=edit&page=2&q=Walk&store={self.store.pk}"
        self.assertContains(page1, next_link)

        page2 = self.client.get(reverse("salt_log_list"), {**query, "page": 2})
        self.assertContains(page2, f"Walk {SALT_LOG_PAGE_SIZE:04d}")
        self.assertContains(page2, f"Walk {total - 1:04d}")
        self.assertNotContains(page2, "Walk 0000")
        self.assertNotContains(page2, "Other store walk")
        self.assertContains(page2, 'class="tab-pane fade show active" id="edit"')
        self.assertContains(
            page2,
            f"Showing {SALT_LOG_PAGE_SIZE + 1}–{total} of {total}",
        )
        self.assertContains(page2, "Page 2 of 2")

    def test_empty_page_and_out_of_range_page(self):
        empty = self.client.get(reverse("salt_log_list"), {"tab": "edit", "page": 4})
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, "Nothing here.")
        self.assertContains(empty, ">0<")
        self.assertNotContains(empty, "Previous")
        self.assertNotContains(empty, "Page 1 of")

        self._fill(SaltLog.STATUS_SUBMITTED, SALT_LOG_PAGE_SIZE + 1, area_prefix="Lot")
        not_a_number = self.client.get(
            reverse("salt_log_list"),
            {"tab": "edit", "page": "nope"},
        )
        self.assertEqual(not_a_number.status_code, 200)
        self.assertContains(not_a_number, "Lot 0000")
        self.assertContains(not_a_number, "Page 1 of 2")

        past_end = self.client.get(
            reverse("salt_log_list"),
            {"tab": "submitted", "page": 99},
        )
        self.assertEqual(past_end.status_code, 200)
        self.assertContains(past_end, f"Lot {SALT_LOG_PAGE_SIZE:04d}")
        self.assertNotContains(past_end, "Lot 0000")
        self.assertContains(past_end, "Page 2 of 2")
        self.assertContains(past_end, 'class="tab-pane fade show active" id="edit"')

    def test_queries_fetch_only_the_current_page(self):
        self._fill(SaltLog.STATUS_SUBMITTED, SALT_LOG_PAGE_SIZE + 15, area_prefix="Ice")
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("salt_log_list"), {"tab": "edit"})
        self.assertEqual(response.status_code, 200)
        salt_sql = [
            query["sql"]
            for query in captured.captured_queries
            if "quiz_saltlog" in query["sql"]
        ]
        selects = [
            sql
            for sql in salt_sql
            if sql.lstrip().upper().startswith("SELECT")
        ]
        counts = [
            sql
            for sql in selects
            if "COUNT" in sql.upper()
        ]
        pages = [sql for sql in selects if sql not in counts]
        self.assertGreaterEqual(len(counts), 1)
        self.assertTrue(pages)
        for sql in pages:
            self.assertIn("LIMIT", sql.upper())
            self.assertIn(str(SALT_LOG_PAGE_SIZE), sql)

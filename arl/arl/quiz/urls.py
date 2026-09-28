from django.urls import path

from . import views
from .views import (
    ProcessSaltLogImagesView,
    UploadChecklistItemPhotoView,
)

urlpatterns = [
    path("list/", views.quiz_list, name="quiz_list"),
    path("create/", views.create_quiz, name="create_quiz"),
    path("take/<int:quiz_id>/", views.take_quiz, name="take_quiz"),
    path("salt-logs/", views.salt_log_dashboard, name="salt_log_list"),
    path("salt-logs/start/", views.salt_log_start, name="create_salt_log"),
    path("salt-logs/<int:pk>/edit/", views.salt_log_edit, name="salt_log_edit"),
    # Older bookmarks. Same employer-scoped edit page.
    path("create-salt-log/", views.salt_log_start, name="create_salt_log_legacy"),
    path("salt-log-list/", views.salt_log_dashboard, name="salt_log_list_legacy"),
    path("incident/<int:pk>/", views.salt_log_edit, name="salt_log_update"),
    path(
        "log-process-images/",
        ProcessSaltLogImagesView.as_view(),
        name="salt_log_upload",
    ),
    path("checklists/", views.checklist_dashboard, name="checklist_dashboard"),
    path("templates/new/", views.template_create, name="template_create"),
    path("templates/<int:pk>/", views.template_detail, name="template_detail"),
    path("templates/<int:pk>/edit/", views.template_edit, name="template_edit"),
    path(
        "start/<int:template_id>/",
        views.checklist_from_template,
        name="checklist_from_template",
    ),
    path("<slug:slug>/edit/", views.checklist_edit, name="checklist_edit"),
    path("<slug:slug>/", views.checklist_detail, name="checklist_detail"),
    path(
        "checklists/<slug:slug>/items/<uuid:item_uuid>/upload/",
        UploadChecklistItemPhotoView.as_view(),
        name="upload_checklist_item_photo",
    ),
    path(
        "checklists/<slug:slug>/download-pdf/start/",
        views.checklist_download_fresh_start_api,
        name="checklist_download_fresh_start_api",
    ),
    path(
        "checklists/fresh-pdf/status/",
        views.checklist_download_fresh_status,
        name="checklist_download_fresh_status",
    ),
    path(
        "checklists/<slug:slug>/report-html/",
        views.checklist_report_html,
        name="checklist_report_html",
    ),
]

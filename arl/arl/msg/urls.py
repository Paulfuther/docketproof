from django.urls import path

from arl.msg.views import (EmailEventList, FetchTwilioCallsView,
                           SendTemplateWhatsAppView, campaign_setup_view,
                           carwash_targets, click_thank_you, communications,
                           compliance_file_view, delete_draft_email,
                           edit_draft_email, email_event_summary_view,
                           email_log_view, employee_email_report_view,
                           email_template_assets, email_template_create,
                           email_template_delete, email_template_edit,
                           email_template_image_proxy, email_template_list,
                           email_template_preview,
                           fetch_shortlink_sms_data, fetch_sms_data,
                           fetch_twilio_data, generate_ai_content,
                           get_group_emails, get_task_status,
                           latest_compliance_file, message_summary_view,
                           save_draft_ajax, search_users_view,
                           sendgrid_webhook, sendgrid_webhook_view,
                           sms_success_view, sms_summary_view,
                           template_email_success_view,
                           template_whats_app_success_view,
                           test_sms_with_short_link, tobacco_vape_policy_view,
                           twilio_shortened_link_webhook,
                           twilio_voice_status_webhook, upload_attachment,
                           whatsapp_webhook)

urlpatterns = [

    # ---------------------------------------------------
    # WEBHOOKS
    # ---------------------------------------------------

    path("sendgrid_hook/", sendgrid_webhook, name="sendgrid_webhook"),
    path("sendgrid-webhook/", sendgrid_webhook_view, name="sendgrid_webhook_view"),
    path("webhook/whatsapp/", whatsapp_webhook, name="whatsapp_webhook"),
    path("twilio/webhook/short-link/", twilio_shortened_link_webhook, name="twilio_short_link_webhook"),
    path("twilio/voice/status/", twilio_voice_status_webhook, name="twilio_voice_status_webhook"),


    # ---------------------------------------------------
    # MAIN COMMUNICATIONS UI
    # ---------------------------------------------------

    path("comms/", communications, name="comms"),
    path("sms-success/", sms_success_view, name="sms_success"),
    path("template-email-success/", template_email_success_view, name="template_email_success"),
    path("template-whats_app-success/", template_whats_app_success_view, name="template_whats_app_success"),
    path("thank-you/", click_thank_you, name="thank_you"),


    # ---------------------------------------------------
    # WHATSAPP
    # ---------------------------------------------------

    path(
        "send-whatsapp/",
        SendTemplateWhatsAppView.as_view(),
        name="send_whats_app_template_view",
    ),


    # ---------------------------------------------------
    # CAMPAIGNS
    # ---------------------------------------------------

    path("campaign/setup/", campaign_setup_view, name="campaign_setup"),


    # ---------------------------------------------------
    # REPORTS / DATA ENDPOINTS
    # ---------------------------------------------------

    path("api/data/", EmailEventList, name="data-list"),
    path("fetch-twilio-calls/", FetchTwilioCallsView.as_view(), name="fetch_twilio_calls"),
    path("fetch-twilio-data/", fetch_twilio_data, name="fetch_twilio_data"),
    path("fetch-sms-data/", fetch_sms_data, name="fetch_sms_data"),
    path("sms-summary-view/", sms_summary_view, name="sms_summary_view"),
    path("message-summary/", message_summary_view, name="summarize_costs"),
    path("email-event-summary/", email_event_summary_view, name="email_event_summary"),
    path("employee-email-report/", employee_email_report_view, name="employee_email_report"),
    path("short-link-sms/report/", fetch_shortlink_sms_data, name="shortened_sms_report"),
    path("email-log/", email_log_view, name="email_log_view"),


    # ---------------------------------------------------
    # DRAFT EMAILS
    # ---------------------------------------------------

    path("comms/draft/<int:draft_id>/", edit_draft_email, name="edit_draft_email"),
    path("comms/draft/<int:draft_id>/delete/", delete_draft_email, name="delete_draft_email"),
    path("comms/save-draft/", save_draft_ajax, name="save_draft_ajax"),


    # ---------------------------------------------------
    # IN-APP EMAIL TEMPLATES
    # ---------------------------------------------------

    path("comms/templates/", email_template_list, name="email_template_list"),
    path("comms/templates/new/", email_template_create, name="email_template_create"),
    path(
        "comms/templates/<int:pk>/edit/",
        email_template_edit,
        name="email_template_edit",
    ),
    path(
        "comms/templates/<int:pk>/delete/",
        email_template_delete,
        name="email_template_delete",
    ),
    path(
        "comms/templates/<int:pk>/preview/",
        email_template_preview,
        name="email_template_preview",
    ),
    path(
        "comms/templates/<int:pk>/assets/",
        email_template_assets,
        name="email_template_assets",
    ),
    path(
        "comms/templates/image-proxy/",
        email_template_image_proxy,
        name="email_template_image_proxy",
    ),


    # ---------------------------------------------------
    # AI TOOLS
    # ---------------------------------------------------

    path("comms/generate-ai/", generate_ai_content, name="generate_ai_content"),


    # ---------------------------------------------------
    # SMS TESTING
    # ---------------------------------------------------

    path("test-sms/", test_sms_with_short_link, name="test_sms_with_short_link"),


    # ---------------------------------------------------
    # USER / GROUP UTILITIES
    # ---------------------------------------------------

    path("get-group-emails/", get_group_emails, name="get_group_emails"),
    path("search-users/", search_users_view, name="search_users"),


    # ---------------------------------------------------
    # COMPLIANCE FILES
    # ---------------------------------------------------

    path("messages/compliance/", latest_compliance_file, name="latest_compliance"),
    path("compliance/", compliance_file_view, name="compliance"),
    path("upload-attachment/", upload_attachment, name="upload_attachment"),


    # ---------------------------------------------------
    # SPECIALIZED FEATURES
    # ---------------------------------------------------

    path("tobacco-vape-policy/", tobacco_vape_policy_view, name="tobacco_vape_policy"),
    path("carwash/targets/", carwash_targets, name="carwash_targets"),


    # ---------------------------------------------------
    # TASK UTILITIES
    # ---------------------------------------------------

    path("get-task-status/<str:task_id>/", get_task_status, name="get_task_status"),
]
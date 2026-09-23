import json
import logging
import re
import urllib.parse
import uuid
from io import BytesIO
from pathlib import Path

from celery.result import AsyncResult
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.forms import modelformset_factory
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView
from django.views.generic.edit import FormView
from openai import OpenAI
from PIL import Image
from twilio.request_validator import RequestValidator
from waffle.decorators import waffle_flag

from arl.bucket.helpers import conn, upload_to_linode_object_storage
from arl.dsign.forms import NameEmailForm
from arl.dsign.tasks import create_docusign_envelope_task
from arl.msg.email_utils import (
    COMPOSE_TEMPLATE_NAME,
    EMAIL_SOURCE_COMPOSE,
    EMAIL_SOURCE_IN_APP,
    EMAIL_SOURCE_SENDGRID,
    get_generic_sendgrid_template_id,
    prepare_email_image,
    prepare_header_image,
    render_merge_fields,
    resolve_email_subject,
    sample_preview_context,
    sendgrid_id_for_template,
    wrap_in_app_email_html,
)
from arl.msg.helpers import (client, get_all_contact_lists,
                             get_uploaded_urls_from_request,
                             is_member_of_comms_group,
                             is_member_of_docusign_group,
                             is_member_of_email_group,
                             is_member_of_email_logs_group,
                             is_member_of_msg_group,
                             is_member_of_sms_logs_group,
                             prepare_recipient_data,
                             prepare_sms_recipient_data, save_email_draft,
                             send_linkshortened_sms,
                             send_whats_app_carwash_sites_template)
from arl.msg.tasks import (master_email_send_task,
                           process_twilio_short_link_event,
                           process_whatsapp_webhook,
                           send_template_whatsapp_task, start_campaign_task)
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, Employer, Store

from .forms import (CampaignSetupForm, EmailForm, EmailTemplateForm, EmployeeSearchForm,
                    GroupSelectForm, SendGridFilterForm, SMSForm,
                    SMSLogFilterForm, StoreTargetForm, TemplateFilterForm,
                    TemplateWhatsAppForm)
from .models import (ComplianceFile, DraftEmail, EmailEvent, EmailTemplate, ShortenedSMSLog,
                     ShortenedSMSMessage)
from .tasks import (fetch_twilio_sms_task, fetch_twilio_summary,
                    filter_sendgrid_events, generate_email_event_summary,
                    generate_employee_email_report_task,
                    process_sendgrid_webhook, send_sms_to_selected_users_task)

logger = logging.getLogger(__name__)
User = get_user_model()


class SendTemplateWhatsAppView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    form_class = TemplateWhatsAppForm
    template_name = "msg/whatsapp_form.html"
    # URL name for success page
    success_url = reverse_lazy("template_whats_app_success")

    def test_func(self):
        return is_member_of_msg_group(self.request.user)

    def form_valid(self, form):
        selected_group_id = form.cleaned_data["selected_group"].id
        whatsapp_template = form.cleaned_data["whatsapp_id"]
        from_id = "MGb005e5fe6d147e13d0b2d1322e00b1cb"
        # Fetch the group and sendgrid_id
        group = get_object_or_404(Group, pk=selected_group_id)
        group_id = group.id
        whatsapp_id = (
            whatsapp_template.content_sid
        )  # Assuming sendgrid_id is a field in the EmailTemplate model

        # Now you have both the group_id and the corresponding sendgrid_id
        # Use these to send emails to the entire group
        print(group_id)
        send_template_whatsapp_task.delay(whatsapp_id, from_id, group_id)
        # print(group_id)
        return super().form_valid(form)


class FetchTwilioCallsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "msg/call_data.html"

    def test_func(self):
        return is_member_of_msg_group(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        calls = fetch_calls()
        if calls is None:
            # Set an error message in the context
            context["error_message"] = (
                "Failed to fetch call logs. Please try again later."
            )
            return context

        truncated_calls = []
        for call in calls:
            twilio_to_number = str(call.to)
            store = Store.objects.filter(phone_number=str(twilio_to_number)).first()
            store_phone_number = (
                str(store.phone_number) if store and store.phone_number else None
            )
            store_number = store.number if store and store.number else None
            call_data = {
                "date_created": call.date_created,
                "to": twilio_to_number,
                "duration": call.duration,
                "store_phone_number": (
                    store_phone_number if store_phone_number else "Unknown Phone Number"
                ),
                "store_number": (
                    store_number if store_number else "Unknown Store Number"
                ),
                "status": (call.status),
                # Add more fields as needed
            }
            truncated_calls.append(call_data)

        context["call_data"] = truncated_calls
        return context


@csrf_exempt  # In production, use proper CSRF protection.
def sendgrid_webhook(request):
    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8"))
            process_sendgrid_webhook.delay(payload)
            return JsonResponse(
                {"message": "Webhook received successfully"}, status=200
            )
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    else:
        return JsonResponse({"error": "Only POST requests are allowed"}, status=405)


def sms_success_view(request):
    return render(request, "msg/sms_success.html")


def template_email_success_view(request):
    return render(request, "msg/template_email_success.html")


def template_whats_app_success_view(request):
    return render(request, "msg/template_whats_app_success.html")


def fetch_sms():
    return client.messages.list(limit=1000)


# this is for whatas app.
def fetch_whatsapp_messages(account_sid, auth_token):
    # Fetch messages sent via WhatsApp
    messages = client.messages.list(limit=20)  # Adjust 'limit' as needed

    for message in messages:
        if "whatsapp:" in message.from_:  # Filter messages sent from a WhatsApp number
            print(
                f"From: {message.from_}, To: {message.to}, Body: {message.body}, Status: {message.status}, Date Sent: {message.date_sent}"
            )


def fetch_calls():
    return client.calls.list(limit=20)


def fetch_twilio_data(request):
    # Run the synchronous task
    task = fetch_twilio_summary.delay()
    return JsonResponse({"task_id": task.id})


def fetch_sms_data(request):
    task = fetch_twilio_sms_task.delay(request.user.id)
    return JsonResponse({"task_id": task.id})


def fetch_shortlink_sms_data(request):
    form = SMSLogFilterForm(request.GET or None)

    logs = ShortenedSMSLog.objects.select_related("user")

    if form.is_valid():
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")

        if start_date:
            logs = logs.filter(created_at__date__gte=start_date)
        if end_date:
            logs = logs.filter(created_at__date__lte=end_date)

    # ✅ Pivot by sms_sid, user, etc.
    summary = (
        logs.values(
            "sms_sid",  # ✅ group key
            "user__first_name",
            "user__last_name",
            "to",
            "from_number",
            "body",
            "error_code",
        )
        .annotate(
            queued=Count("id", filter=Q(event_type="queued")),
            sent=Count("id", filter=Q(event_type="sent")),
            delivered=Count("id", filter=Q(event_type="delivered")),
            clicked=Count("id", filter=Q(event_type="click")),
            last_time=Max("created_at"),
        )
        .order_by("-last_time")
    )

    context = {
        "summary": summary,
        "form": form,
    }
    return render(request, "msg/shortlink_sms_data.html", context)


def get_task_status(request, task_id):
    task = AsyncResult(task_id)
    if task.state == "SUCCESS":
        return JsonResponse({"status": "success", "result": task.result})
    elif task.state == "FAILURE":
        return JsonResponse({"status": "failure", "error": str(task.result)})
    else:
        return JsonResponse({"status": "pending"})


def message_summary_view(request):
    return render(request, "msg/message_summary.html")


# this is for the summary view
def sms_summary_view(request):
    return render(request, "msg/sms_data.html")


def render_message_to_sendgrid(message):
    return escape(message).replace("\n", "<br>")


@login_required
@user_passes_test(is_member_of_comms_group)
def communications(request):
    employer = getattr(request.user, "employer", None)

    active_tab = request.GET.get("tab", "email")

    # -------------------------
    # SMS LINK LOGS
    # -------------------------
    sms_log_q = (request.GET.get("sms_log_q") or "").strip()
    sms_log_status = (request.GET.get("sms_log_status") or "").strip()
    sms_log_clicked = (request.GET.get("sms_log_clicked") or "").strip()
    sms_log_page_number = request.GET.get("sms_log_page", 1)

    sms_logs = (
        ShortenedSMSMessage.objects
        .select_related("user", "employer")
        .order_by("-created_at")
    )

    if employer:
        sms_logs = sms_logs.filter(employer=employer)

    if sms_log_q:
        sms_logs = sms_logs.filter(
            Q(to__icontains=sms_log_q)
            | Q(body__icontains=sms_log_q)
            | Q(body_preview__icontains=sms_log_q)
            | Q(sms_sid__icontains=sms_log_q)
            | Q(link__icontains=sms_log_q)
            | Q(user__first_name__icontains=sms_log_q)
            | Q(user__last_name__icontains=sms_log_q)
        )

    if sms_log_status:
        sms_logs = sms_logs.filter(status=sms_log_status)

    if sms_log_clicked == "yes":
        sms_logs = sms_logs.filter(click_count__gt=0)
    elif sms_log_clicked == "no":
        sms_logs = sms_logs.filter(click_count=0)

    sms_total = sms_logs.count()
    sms_delivered = sms_logs.filter(status="delivered").count()
    sms_clicked = sms_logs.filter(status="clicked").count()
    sms_failed = sms_logs.filter(status__in=["failed", "undelivered"]).count()

    sms_timeline = (
        sms_logs
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            sent_count=Count("id"),
            delivered_count=Count("id", filter=Q(status="delivered")),
            clicked_count=Count("id", filter=Q(status="clicked")),
            failed_count=Count("id", filter=Q(status__in=["failed", "undelivered"])),
        )
        .order_by("day")
    )

    sms_timeline_labels = [
        row["day"].strftime("%Y-%m-%d")
        for row in sms_timeline
        if row["day"]
    ]
    sms_timeline_sent = [row["sent_count"] for row in sms_timeline]
    sms_timeline_delivered = [row["delivered_count"] for row in sms_timeline]
    sms_timeline_clicked = [row["clicked_count"] for row in sms_timeline]
    sms_timeline_failed = [row["failed_count"] for row in sms_timeline]

    sms_log_paginator = Paginator(sms_logs, 25)
    sms_log_page = sms_log_paginator.get_page(sms_log_page_number)
    # -------------------------
    # EMAIL LOGS
    # -------------------------
    log_q = (request.GET.get("log_q") or "").strip()
    log_status = (request.GET.get("log_status") or "").strip()
    log_template = (request.GET.get("log_template") or "").strip()
    log_page_number = request.GET.get("log_page", 1)

    email_log_qs = EmailEvent.objects.filter(employer=employer).order_by("-timestamp")

    if log_q:
        email_log_qs = email_log_qs.filter(
            Q(email__icontains=log_q)
            | Q(subject__icontains=log_q)
            | Q(sg_message_id__icontains=log_q)
            | Q(sg_event_id__icontains=log_q)
            | Q(sg_template_name__icontains=log_q)
            | Q(source__icontains=log_q)
            | Q(username__icontains=log_q)
        )

    if log_status:
        email_log_qs = email_log_qs.filter(event=log_status)

    if log_template:
        email_log_qs = email_log_qs.filter(
            Q(sg_template_id__icontains=log_template)
            | Q(sg_template_name__icontains=log_template)
            | Q(source__icontains=log_template)
        )   

    email_timeline = (
        email_log_qs
        .annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(
            total_count=Count("id"),
            delivered_count=Count("id", filter=Q(event="delivered")),
            open_count=Count("id", filter=Q(event="open")),
            click_count=Count("id", filter=Q(event="click")),
            failed_count=Count("id", filter=Q(event__in=["bounce", "dropped", "spamreport"])),
        )
        .order_by("day")
    )

    email_timeline_labels = [row["day"].strftime("%Y-%m-%d") for row in email_timeline if row["day"]]
    email_timeline_total = [row["total_count"] for row in email_timeline]
    email_timeline_delivered = [row["delivered_count"] for row in email_timeline]
    email_timeline_open = [row["open_count"] for row in email_timeline]
    email_timeline_click = [row["click_count"] for row in email_timeline]
    email_timeline_failed = [row["failed_count"] for row in email_timeline]

    email_total = email_log_qs.count()
    email_delivered = email_log_qs.filter(event="delivered").count()
    email_opened = email_log_qs.filter(event="open").count()
    email_clicked = email_log_qs.filter(event="click").count()
    email_failed = email_log_qs.filter(event__in=["bounce", "dropped", "spamreport"]).count()

    email_log_paginator = Paginator(email_log_qs, 50)
    email_log_page = email_log_paginator.get_page(log_page_number)

    draft_id = request.GET.get("draft_id") or request.POST.get("draft_id")
    user = request.user
    active_tab = request.GET.get("tab", "email")
    action = request.POST.get("action", "send")

    # Determine allowed tabs
    valid_tabs = []
    if is_member_of_email_group(user):
        valid_tabs.append("email")
    if is_member_of_msg_group(user):
        valid_tabs.append("sms")
    if is_member_of_docusign_group(user):
        valid_tabs.append("docusign")
    if is_member_of_email_logs_group(user):
        valid_tabs.append("email_log")
    if is_member_of_sms_logs_group(user):
        valid_tabs.append("sms_link_log")
    
    # if is_member_of_whatsapp_group(user):
    #    valid_tabs.append("whatsapp")

    if active_tab not in valid_tabs:
        active_tab = valid_tabs[0] if valid_tabs else None

    initial_data = None
    if draft_id:
        try:
            draft = DraftEmail.objects.get(
                id=draft_id, user=request.user, employer=request.user.employer
            )
            initial_data = {
                "email_mode": draft.mode,
                "subject": draft.subject,
                "message": draft.message,
                "sendgrid_id": draft.sendgrid_template,
                "selected_group": draft.selected_group,
                "selected_users": draft.selected_users.all(),
            }
            attachment_urls = draft.attachment_urls or []
        except DraftEmail.DoesNotExist:
            messages.error(request, "Draft not found.")
            return redirect("/comms/?tab=email")
    else:
        attachment_urls = []

    # Forms
    sms_form = SMSForm(user=user)
    email_form = EmailForm(user=user, initial=initial_data)
    docusign_form = NameEmailForm(user=user)
    whatsapp_form = TemplateWhatsAppForm(user=user)

    if request.method == "POST":
        form_type = request.POST.get("form_type")
        print(form_type)

        if form_type == "email":
            active_tab = "email"
            print("📬 Processing email form...")
            email_form = EmailForm(
                request.POST, request.FILES, user=user, initial=initial_data
            )
            print(initial_data)
            print("Raw sendgrid_id from POST:", request.POST.get("sendgrid_id"))

            logger.info("Lets check for valid")
            if not email_form.is_valid():
                logger.warning(
                    "[Comms] Email form invalid for user=%s employer=%s errors=%s",
                    request.user,
                    getattr(request.user, "employer_id", None),
                    email_form.errors.as_json(),
                )
                messages.error(request, "Please correct the errors below.")

            if email_form.is_valid():
                print("valid")
                mode = email_form.cleaned_data["email_mode"]
                selected_group = email_form.cleaned_data["selected_group"]
                selected_users = email_form.cleaned_data["selected_users"]
                attachment_urls = get_uploaded_urls_from_request(request)

                recipients = prepare_recipient_data(
                    user, selected_group, selected_users
                )

                if not recipients:
                    messages.error(
                        request, "No recipients found. Please select a group or users."
                    )
                    return redirect("/comms/?tab=email")

                if action == "save":
                    draft_id = request.POST.get("draft_id")
                    save_email_draft(
                        user,
                        email_form.cleaned_data,
                        attachment_urls,
                        draft_id=draft_id,
                    )
                    messages.success(request, "Draft saved.")
                    return redirect("/comms/?tab=email")

                if mode == "text":
                    print("going to send")
                    subject = resolve_email_subject(
                        subject=email_form.cleaned_data.get("subject"),
                        employer=user.employer,
                    )
                    raw_message = email_form.cleaned_data["message"]
                    message = render_message_to_sendgrid(raw_message)
                    print(message)
                    res = master_email_send_task.delay(
                        recipients=recipients,  # ensure JSON-serializable!
                        sendgrid_id=get_generic_sendgrid_template_id(),
                        employer_id=user.employer.id,  # ensure int
                        body=message,  # str
                        subject=subject,  # str
                        attachment_urls=attachment_urls,  # ensure list[str]
                        template_name=COMPOSE_TEMPLATE_NAME,
                        source=EMAIL_SOURCE_COMPOSE,
                    )
                    print("queued master_email_send_task:", res.id)

                else:
                    sendgrid_template = email_form.cleaned_data[
                        "sendgrid_id"
                    ]  # <EmailTemplate ...>
                    logger.info("Passing EmailTemplate: %s", sendgrid_template)
                    logger.info(
                        "SendGrid template: %s",
                        getattr(sendgrid_template, "sendgrid_id", sendgrid_template),
                    )

                    subject = resolve_email_subject(
                        subject=email_form.cleaned_data.get("subject"),
                        template=sendgrid_template,
                        employer=user.employer,
                    )
                    html_body = (sendgrid_template.html_body or "").strip() or None
                    if html_body:
                        html_body = wrap_in_app_email_html(
                            html_body,
                            sendgrid_template.header_image_url,
                            header_display_width=sendgrid_template.header_display_width,
                        )
                    # In-app HTML is sent as content (SendGrid = transport).
                    # Legacy templates still use their SendGrid dynamic template id.
                    send_id = (
                        ""
                        if html_body
                        else sendgrid_id_for_template(sendgrid_template)
                    )
                    source = (
                        EMAIL_SOURCE_IN_APP if html_body else EMAIL_SOURCE_SENDGRID
                    )

                    master_email_send_task.delay(
                        recipients=recipients,
                        sendgrid_id=send_id,
                        employer_id=user.employer.id,
                        subject=subject,
                        html_body=html_body,
                        body=html_body,
                        template_name=sendgrid_template.name,
                        source=source,
                        app_template_id=sendgrid_template.pk,
                        attachment_urls=attachment_urls,
                    )

                messages.success(request, "Email has been queued for delivery.")
                return redirect("/comms/?tab=email")

            else:
                messages.error(request, "Please correct the errors below.")

        elif form_type == "sms":
            active_tab = "sms"
            sms_form = SMSForm(request.POST, user=user)

            if sms_form.is_valid():
                selected_group = sms_form.cleaned_data["selected_group"]
                selected_users = sms_form.cleaned_data["selected_users"]
                sms_message = sms_form.cleaned_data["sms_message"]

                recipients = prepare_sms_recipient_data(
                    user, selected_group, selected_users
                )

                if not recipients:
                    messages.error(
                        request, "No recipients found. Please select a group or users."
                    )
                    return redirect("/comms/?tab=sms")

                # Pass list of user IDs to the task
                recipient_ids = [r["id"] for r in recipients]
                send_sms_to_selected_users_task.delay(
                    recipient_ids, sms_message, user.id
                )

                messages.success(request, "SMS is being sent.")
                return redirect("/comms/?tab=sms")

        elif form_type == "whatsapp":
            active_tab = "whatsapp"
            whatsapp_form = TemplateWhatsAppForm(request.POST, user=user)
            print(whatsapp_form.errors)

            if not whatsapp_form.is_valid():
                print("❌ WhatsApp form errors:")
                for field, errors in whatsapp_form.errors.items():
                    print(f" - {field}: {errors}")
                messages.error(request, "Please correct the WhatsApp form.")
            else:
                selected_group = whatsapp_form.cleaned_data["selected_group"]
                whatsapp_template = whatsapp_form.cleaned_data["whatsapp_id"]
                from_id = "MGb005e5fe6d147e13d0b2d1322e00b1cb"

                group = get_object_or_404(Group, pk=selected_group.id)
                whatsapp_id = whatsapp_template.content_sid

                send_template_whatsapp_task.delay(whatsapp_id, from_id, group.id)

                messages.success(request, "✅ WhatsApp messages queued for sending.")
                return redirect("/comms/?tab=whatsapp")

        elif form_type == "docusign":
            active_tab = "docusign"
            docusign_form = NameEmailForm(request.POST, user=user)

            if docusign_form.is_valid():
                recipient = docusign_form.cleaned_data["recipient"]
                ds_template = docusign_form.cleaned_data["template_id"]
                template = docusign_form.cleaned_data["template_name"]
                template_name = template.template_name if template else "Unknown"

                envelope_args = {
                    "user_id": recipient.id,
                    "employer_id": recipient.employer_id,
                    "signer_email": recipient.email,
                    "signer_name": recipient.get_full_name(),
                    "template_id": ds_template,
                }

                create_docusign_envelope_task.delay(envelope_args)

                messages.success(
                    request,
                    f"The document '{template_name}' has been sent to {recipient.get_full_name()}.",
                )
                return redirect("/comms/?tab=docusign")

            messages.error(request, "Please correct the DocuSign form.")

    selected_ids = (
        request.POST.getlist("selected_users") if request.method == "POST" else []
    )
    log_status_choices = [
        "processed",
        "delivered",
        "deferred",
        "bounce",
        "dropped",
        "open",
        "click",
        "spamreport",
    ]
    print(log_q)
    return render(
        request,
        "msg/master_comms.html",
        {
            "email_form": email_form,
            "sms_form": sms_form,
            "docusign_form": docusign_form,
            "whatsapp_form": whatsapp_form,
            "active_tab": active_tab,
            "sms_log_q": sms_log_q,
            "sms_log_status": sms_log_status,
            "sms_log_clicked": sms_log_clicked,
            "sms_log_page": sms_log_page,
            "sms_total": sms_total,
            "sms_delivered": sms_delivered,
            "sms_clicked": sms_clicked,
            "sms_failed": sms_failed,
            "sms_timeline_labels": json.dumps(sms_timeline_labels),
            "sms_timeline_sent": json.dumps(sms_timeline_sent),
            "sms_timeline_delivered": json.dumps(sms_timeline_delivered),
            "sms_timeline_clicked": json.dumps(sms_timeline_clicked),
            "sms_timeline_failed": json.dumps(sms_timeline_failed),
            "email_log_page": email_log_page,
            "log_q": log_q,
            "log_status": log_status,
            "log_status_choices": log_status_choices,
            "log_template": log_template,
            "email_total": email_total,
            "email_delivered": email_delivered,
            "email_opened": email_opened,
            "email_clicked": email_clicked,
            "email_failed": email_failed,
            "email_timeline_labels": json.dumps(email_timeline_labels),
            "email_timeline_total": json.dumps(email_timeline_total),
            "email_timeline_delivered": json.dumps(email_timeline_delivered),
            "email_timeline_open": json.dumps(email_timeline_open),
            "email_timeline_click": json.dumps(email_timeline_click),
            "email_timeline_failed": json.dumps(email_timeline_failed),
            "can_send_email": is_member_of_email_group(user),
            "can_send_sms": is_member_of_msg_group(user),
            "can_send_docusign": is_member_of_docusign_group(user),
            "can_view_email_logs": is_member_of_email_logs_group(user),
            "can_view_sms_logs": is_member_of_sms_logs_group(user),
            "email_templates_meta": json.dumps(
                [
                    {
                        "id": str(t.pk),
                        "name": t.name or "",
                        "subject": t.resolved_subject(),
                        "is_in_app": t.is_in_app,
                    }
                    for t in email_form.fields["sendgrid_id"].queryset
                ]
            ),
            # "can_send_whatsapp": is_member_of_whatsapp_group(user),
            "selected_ids": selected_ids,
            "draft_id": draft_id,
            "attachment_urls": attachment_urls,
            "drafts": DraftEmail.objects.filter(
                user=user, employer=user.employer
            ).order_by("-created_at"),
        },
    )


#  -- Log for emails in the comms section --
@login_required
def email_log_view(request):
    employer = getattr(request.user, "employer", None)
    if not employer:
        # or redirect
        return render(request, "msg/email_log.html", {"page_obj": [], "q": ""})

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    template_id = (request.GET.get("template_id") or "").strip()

    # Replace EmailEvent with your actual model
    qs = EmailEvent.objects.filter(employer=employer).order_by("-processed_at")

    if q:
        qs = qs.filter(
            Q(email__icontains=q)
            | Q(subject__icontains=q)
            | Q(sg_message_id__icontains=q)
            | Q(sg_event_id__icontains=q)
        )

    if status:
        qs = qs.filter(event=status)

    if template_id:
        qs = qs.filter(template_id=template_id)

    paginator = Paginator(qs, 50)  # 50 rows like an activity feed
    page_obj = paginator.get_page(request.GET.get("page", 1))

    return render(
        request,
        "msg/email_log.html",
        {
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "template_id": template_id,
        },
    )


@login_required
@require_POST
def save_draft_ajax(request):
    form = EmailForm(request.POST, user=request.user, is_draft=True)

    if not form.is_valid():
        return JsonResponse(
            {
                "success": False,
                "error": "Form validation failed.",
                "errors": form.errors.as_json(),
            }
        )

    draft_id = request.POST.get("draft_id")
    print("draft id :", draft_id)
    if draft_id:
        try:
            draft = DraftEmail.objects.get(id=draft_id, user=request.user)
        except DraftEmail.DoesNotExist:
            return JsonResponse({"success": False, "error": "Draft not found."})
    else:
        draft = DraftEmail(user=request.user)
    print("draft :", draft, draft.subject)
    draft.mode = form.cleaned_data.get("email_mode")
    draft.subject = form.cleaned_data.get("subject", "")
    print("draft subject :", draft.subject)
    draft.message = form.cleaned_data.get("message", "")
    draft.sendgrid_template = form.cleaned_data.get("sendgrid_id")
    draft.selected_group = form.cleaned_data.get("selected_group")
    uploaded_urls_raw = request.POST.get("uploaded_file_urls")
    draft.employer = request.user.employer
    try:
        attachment_urls = json.loads(uploaded_urls_raw) if uploaded_urls_raw else []
    except json.JSONDecodeError:
        attachment_urls = []

    draft.attachment_urls = attachment_urls
    draft.save()

    draft.selected_users.set(form.cleaned_data.get("selected_users"))
    draft.save()

    return JsonResponse({"success": True, "draft_id": draft.id})


@login_required
@user_passes_test(is_member_of_comms_group)
def edit_draft_email(request, draft_id):
    try:
        draft = DraftEmail.objects.get(id=draft_id, user=request.user)
    except DraftEmail.DoesNotExist:
        messages.error(request, "Draft not found.")
        return redirect("/comms/?tab=email")

    form = EmailForm(
        initial={
            "email_mode": draft.mode,
            "subject": draft.subject,
            "message": draft.message,
            "sendgrid_id": draft.sendgrid_template,
            "selected_group": draft.selected_group,
            "selected_users": draft.selected_users.all(),
        },
        user=request.user,
    )

    return render(
        request,
        "msg/master_comms.html",
        {
            "email_form": form,
            "sms_form": SMSForm(user=request.user),
            "docusign_form": NameEmailForm(user=request.user),
            "active_tab": "email",
            "can_send_email": is_member_of_email_group(request.user),
            "can_send_sms": is_member_of_msg_group(request.user),
            "can_send_docusign": is_member_of_docusign_group(request.user),
            "selected_ids": [u.id for u in draft.selected_users.all()],
            "draft_id": draft.id,
            "draft": draft,
            "attachment_urls": json.dumps(draft.attachment_urls or []),
        },
    )


@login_required
@require_POST
def delete_draft_email(request, draft_id):
    draft = get_object_or_404(DraftEmail, id=draft_id, user=request.user)
    draft.delete()
    messages.success(request, "Draft deleted.")
    return redirect("/comms/?tab=email")


@waffle_flag("email_api")
def EmailEventList(request):
    return HttpResponse("This view is controlled by a feature flag.")


def click_thank_you(request):
    return render(request, "msg/thank_you.html")


@csrf_exempt
def whatsapp_webhook(request):
    if request.method != "POST":
        return render(request, "incident/405.html", status=405)
    try:
        user = request.POST.get("From")
        message = request.POST.get("Body")
        print(f"{user} says {message}")
        body_unicode = request.body.decode("utf-8")
        data = urllib.parse.parse_qs(body_unicode)
        print(f"📩 Twilio Webhook Data: {json.dumps(data, indent=2)}")
        process_whatsapp_webhook.delay(data)
    except Exception as e:
        logger.error(f"Error processing webhook data: {e}")
        return JsonResponse(
            {"status": "error", "message": "Internal Server Error"}, status=500
        )

    return JsonResponse({"status": "success"}, status=200)


StoreFormSet = modelformset_factory(
    Store, form=StoreTargetForm, extra=0, fields=("number", "sales_target")
)


def carwash_targets(request):
    if request.method == "POST":
        print(request.POST)
        formset = StoreFormSet(request.POST)
        group_form = GroupSelectForm(request.POST)
        if formset.is_valid() and group_form.is_valid():
            formset.save()
            # Get the user name (assuming it's from request.user or another source)
            user_name = request.user.first_name  # Or however you get the user's name

            # Build content variables for the WhatsApp message
            content_vars = {"1": user_name}  # First placeholder is the user's name
            row_number = 2  # Start after 1 since 1 is used for the username
            # Extract store number and sales target from the cleaned data

            for form in formset:
                store_number = form.cleaned_data["number"]
                target = form.cleaned_data["sales_target"]
                content_vars[f"{row_number}"] = f"Store {store_number}"
                content_vars[f"{row_number + 1}"] = f"Target {target}"
                row_number += 2
            print("contentvars", content_vars)
            content_vars_json = json.dumps(content_vars)
            # Get the selected group
            selected_group = group_form.cleaned_data["group"]
            print("selected group", selected_group)
            group_id = selected_group.id
            print("group id", group_id)
            whatsapp_template = "HX1a363c5a2312c9baeb34daed5d422f9d"
            from_sid = "MGb005e5fe6d147e13d0b2d1322e00b1cb"
            whatsapp_id = whatsapp_template
            # Assuming sendgrid_id is a field in the EmailTemplate model
            group = Group.objects.get(pk=group_id)
            users_in_group = group.user_set.filter(is_active=True)
            for user in users_in_group:
                print(user.first_name, user.phone_number, content_vars)
                send_whats_app_carwash_sites_template(
                    whatsapp_id,
                    from_sid,
                    user.first_name,
                    user.phone_number,
                    content_vars_json,
                )
    else:
        # Filter the queryset to include only carwash stores
        queryset = Store.objects.filter(carwash=True)
        formset = StoreFormSet(queryset=queryset)
        group_form = GroupSelectForm()

    return render(
        request,
        "msg/carwash_targets.html",
        {"formset": formset, "group_form": group_form},
    )


def sendgrid_webhook_view(request):
    # Initialize form with GET data
    form = SendGridFilterForm(request.GET or None)

    # Initialize an empty list for event data
    events = []

    if form.is_valid() and form.cleaned_data.get("template_id"):
        date_from = form.cleaned_data["date_from"]
        date_to = form.cleaned_data["date_to"]
        template = form.cleaned_data["template_id"]

        # Trigger the Celery task and get the result
        task = filter_sendgrid_events.delay(
            date_from=date_from,
            date_to=date_to,
            template_id=template.sendgrid_id,
            app_template_id=template.pk,
        )
        events = task.get()  # Wait for the task to complete and get the result

    return render(
        request,
        "msg/sendgrid_webhook_table.html",
        {
            "events": events,
            "form": form,
        },
    )


def email_event_summary_view(request):
    form = TemplateFilterForm(request.GET or None, user=request.user)
    summary_table = ""

    # Only proceed with task if form is valid and template_id is provided
    if form.is_valid() and form.cleaned_data.get("template_id"):
        employer_id = (
            request.user.employer.id if hasattr(request.user, "employer") else None
        )
        template = form.cleaned_data["template_id"]
        start_date = form.cleaned_data.get("date_from")
        end_date = form.cleaned_data.get("date_to")
        # print(start_date, end_date, employer_id)
        # Call the Celery task
        result = generate_email_event_summary.delay(
            template.sendgrid_id,
            start_date,
            end_date,
            employer_id,
            app_template_id=template.pk,
        )
        summary_table = result.get(timeout=10)  # Wait for task completion

    return render(
        request,
        "msg/email_event_summary_table.html",
        {
            "summary_table": summary_table,
            "form": form,
        },
    )


def employee_email_report_view(request):
    form = EmployeeSearchForm(request.GET or None)
    report_table = None

    if form.is_valid():
        # Fetch the selected employee
        employee = form.cleaned_data.get("employee")
        if employee:
            # Trigger the Celery task with the employee ID
            result = generate_employee_email_report_task.delay(employee_id=employee.id)

            # Wait for the result (blocking, for demonstration purposes)
            report_table = result.get(timeout=10)
    # print(report_table)
    return render(
        request,
        "msg/employee_email_report.html",
        {
            "form": form,
            "report_table": report_table,
        },
    )


def campaign_setup_view(request):
    contact_lists = get_all_contact_lists()
    contact_list_choices = [(cl["id"], cl["name"]) for cl in contact_lists]

    if request.method == "POST":
        # Pass the dynamic choices to the form during POST
        form = CampaignSetupForm(request.POST)
        form.fields["contact_list"].choices = contact_list_choices

        if form.is_valid():
            selected_list_id = form.cleaned_data["contact_list"]
            try:
                # Sync contacts with the selected list and start the campaign
                start_campaign_task.delay(selected_list_id)

                messages.success(request, "Campaign started successfully!")
                return redirect("home")  # Redirect to a success page
            except Exception as e:
                messages.error(request, f"Error starting campaign: {e}")
    else:
        # Pass the choices to the form during GET
        form = CampaignSetupForm()
        form.fields["contact_list"].choices = contact_list_choices

    return render(request, "msg/campaign_setup.html", {"form": form})


def get_group_emails(request):
    if request.method == "GET":
        user = request.user
        employer = user.employer
        # Retrieve group IDs from the query parameters
        group_ids = request.GET.get("group_ids", "")  # Returns a string like "1,2,3"
        if not group_ids:
            return JsonResponse(
                {"emails": [], "error": "No group IDs provided."}, status=400
            )

        # Convert the string of IDs into a list of integers
        group_id_list = [
            int(group_id) for group_id in group_ids.split(",") if group_id.isdigit()
        ]

        # Query active users in the specified groups
        users = CustomUser.objects.filter(
            groups__id__in=group_id_list, is_active=True, employer=employer
        ).distinct()

        email_list = [{"email": user.email, "status": "Active"} for user in users]

        return JsonResponse({"emails": email_list}, status=200)
    return JsonResponse({"error": "Invalid request method."}, status=400)


def tobacco_vape_policy_view(request):
    """
    Render the Tobacco and Vape Products Required Action Policy.
    """
    return render(request, "msg/tobacco_vape_policy.html")


@login_required
def search_users_view(request):
    query = request.GET.get("search", "")
    employer = request.user.employer

    users = User.objects.filter(employer=employer, is_active=True).order_by(
        "last_name", "first_name"
    )
    if query:
        users = users.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )

    selected_ids = request.GET.getlist("selected_users")

    return render(
        request,
        "msg/partials/user_checkboxes.html",
        {"users": users, "selected_ids": selected_ids},
    )


def _visible_email_templates(employer):
    return (
        EmailTemplate.objects.filter(
            Q(employers=employer) | Q(employers__isnull=True)
        )
        .distinct()
        .order_by("name")
    )


def _template_for_employer(pk, employer, require_owned=False):
    qs = _visible_email_templates(employer)
    template = get_object_or_404(qs, pk=pk)
    if require_owned and not template.employers.filter(pk=employer.pk).exists():
        return None
    return template


@login_required
@user_passes_test(is_member_of_email_group)
def email_template_list(request):
    employer = getattr(request.user, "employer", None)
    if not employer:
        messages.error(request, "Your account is not linked to an employer.")
        return redirect("comms")
    templates = _visible_email_templates(employer)
    owned_template_ids = list(
        EmailTemplate.objects.filter(employers=employer).values_list("pk", flat=True)
    )
    return render(
        request,
        "msg/email_template_list.html",
        {
            "templates": templates,
            "employer": employer,
            "owned_template_ids": owned_template_ids,
        },
    )


@login_required
@user_passes_test(is_member_of_email_group)
def email_template_create(request):
    employer = getattr(request.user, "employer", None)
    if not employer:
        messages.error(request, "Your account is not linked to an employer.")
        return redirect("comms")
    form = EmailTemplateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        template = form.save()
        template.employers.add(employer)
        messages.success(request, "Email template saved.")
        return redirect("email_template_edit", pk=template.pk)
    return render(
        request,
        "msg/email_template_form.html",
        {"form": form, "template": None, "preview_context": sample_preview_context(request.user)},
    )


@login_required
@user_passes_test(is_member_of_email_group)
def email_template_edit(request, pk):
    employer = getattr(request.user, "employer", None)
    if not employer:
        messages.error(request, "Your account is not linked to an employer.")
        return redirect("comms")
    template = _template_for_employer(pk, employer, require_owned=True)
    if template is None:
        messages.error(
            request,
            "You can preview shared templates but only edit templates owned by your employer.",
        )
        return redirect("email_template_list")
    form = EmailTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Email template updated.")
        return redirect("email_template_edit", pk=template.pk)
    return render(
        request,
        "msg/email_template_form.html",
        {
            "form": form,
            "template": template,
            "preview_context": sample_preview_context(request.user),
        },
    )


@login_required
@user_passes_test(is_member_of_email_group)
def email_template_delete(request, pk):
    employer = getattr(request.user, "employer", None)
    if not employer:
        return redirect("comms")
    template = _template_for_employer(pk, employer, require_owned=True)
    if template is None:
        messages.error(request, "You can only delete templates owned by your employer.")
        return redirect("email_template_list")
    if request.method == "POST":
        name = template.name
        template.delete()
        messages.success(request, f'Template "{name}" deleted.')
        return redirect("email_template_list")
    return render(
        request,
        "msg/email_template_confirm_delete.html",
        {"template": template},
    )


@login_required
@user_passes_test(is_member_of_email_group)
def email_template_preview(request, pk):
    employer = getattr(request.user, "employer", None)
    if not employer:
        return JsonResponse({"error": "No employer"}, status=400)
    template = get_object_or_404(_visible_email_templates(employer), pk=pk)
    context = sample_preview_context(request.user, employer)
    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            context.update({k: v for k, v in payload.items() if isinstance(v, str)})
    raw_subject = resolve_email_subject(template=template, employer=employer)
    subject = render_merge_fields(raw_subject, context)
    context = {**context, "subject": subject}
    if template.is_in_app:
        html = render_merge_fields(template.html_body, context)
        html = wrap_in_app_email_html(
            html,
            template.header_image_url,
            header_display_width=template.header_display_width,
        )
    else:
        html = (
            "<p><em>This template is a legacy SendGrid dynamic template "
            f"({escape(template.sendgrid_id or 'no id')}). "
            "Author HTML here to preview and send from DocketProof.</em></p>"
        )
    return JsonResponse(
        {
            "name": template.name or "",
            "subject": subject,
            "html": html,
            "is_in_app": template.is_in_app,
            "sendgrid_id": template.sendgrid_id or "",
            "header_image_url": template.header_image_url or "",
            "header_display_width": template.header_display_width,
        }
    )


# View for weekly compliance notes
def latest_compliance_file(request):
    file = ComplianceFile.objects.filter(is_active=True).first()
    if file:
        return redirect(file.file.url)
    return redirect("/")  # fallback


def compliance_file_view(request):
    file = ComplianceFile.objects.filter(is_active=True).first()
    if not file:
        return HttpResponseNotFound("No file available.")
    return redirect(file.presigned_url or file.file.url)


@csrf_exempt
def upload_attachment(request):
    if request.method == "POST" and request.FILES.get("file"):
        uploaded_file = request.FILES["file"]
        folder = (request.POST.get("folder") or "email_attachments").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder):
            folder = "email_attachments"
        email_fit = folder == "email_templates" or request.POST.get("email_fit") == "1"
        original_name = uploaded_file.name or "file"
        unique_name = f"{folder}/{uuid.uuid4()}_{original_name}"

        try:
            # ✅ Resize if it's an image
            if uploaded_file.content_type.startswith("image/"):
                try:
                    if email_fit:
                        if request.POST.get("email_header") == "1":
                            buffer, ext, _ctype = prepare_header_image(
                                uploaded_file,
                                filename=original_name,
                            )
                        else:
                            buffer, ext, _ctype = prepare_email_image(
                                uploaded_file, filename=original_name
                            )
                        stem = Path(original_name).stem or "image"
                        unique_name = f"{folder}/{uuid.uuid4()}_{stem}.{ext}"
                    else:
                        image = Image.open(uploaded_file)

                        # Choose resampling method safely
                        try:
                            resample_filter = Image.Resampling.LANCZOS  # Pillow ≥ 10
                        except AttributeError:
                            resample_filter = Image.LANCZOS  # Older versions

                        thumbnail_size = (1500, 1500)
                        image.thumbnail(thumbnail_size, resample=resample_filter)

                        buffer = BytesIO()
                        image_format = image.format or "JPEG"
                        image.save(buffer, format=image_format)
                        buffer.seek(0)

                    # Upload resized image
                    upload_to_linode_object_storage(buffer, unique_name)

                except Exception as e:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": f"Image processing failed: {str(e)}",
                        },
                        status=500,
                    )

            else:
                # Upload non-image file as-is
                upload_to_linode_object_storage(uploaded_file, unique_name)

            bucket = conn.get_bucket(settings.LINODE_BUCKET_NAME)
            key = bucket.get_key(unique_name)
            key.set_acl("public-read")

            url = f"https://{settings.LINODE_BUCKET_NAME}.{settings.LINODE_REGION}/{unique_name}"
            return JsonResponse({"success": True, "url": url})

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    return JsonResponse({"success": False, "error": "Invalid request"}, status=400)

# -- This view was modified on March 11 2026 --
# -- This supports the new webhook log for shortened links --


@csrf_exempt
def twilio_shortened_link_webhook(request):
    """
    Receives Twilio webhook events for shortened SMS links and
    queues them for processing in Celery.
    """

    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        # Twilio sends application/x-www-form-urlencoded
        if request.content_type == "application/json":
            data = json.loads(request.body.decode("utf-8"))
        else:
            data = request.POST.dict()

        logger.info(f"📩 Twilio Short Link Webhook Received: {data}")

        # Push to Celery
        process_twilio_short_link_event.delay(data)

        return HttpResponse("OK", status=200)

    except Exception:
        logger.exception("❌ Twilio webhook processing error")
        return HttpResponse(status=500)


@require_GET
@login_required
def test_sms_with_short_link(request):
    test_number = settings.MY_TEST_PHONE_NUMBER  # e.g., +12225551234

    if not test_number:
        return JsonResponse({"error": "Test number not found in .env"}, status=400)

    try:
        employer = Employer.objects.get(id=1)
    except Employer.DoesNotExist:
        return JsonResponse({"error": "Employer with ID 1 not found"}, status=404)

    twilio_keys = TenantApiKeys.objects.filter(
        employer=employer, is_active=True
    ).first()
    if not twilio_keys:
        logger.error(f"🚨 No Twilio keys for {employer.name}")
        return JsonResponse({"error": "Twilio keys not configured"}, status=500)

    sid = twilio_keys.account_sid
    token = twilio_keys.auth_token
    msg_sid = twilio_keys.messaging_service_sid

    if not sid or not token or not msg_sid:
        logger.error(f"🚨 Incomplete Twilio credentials for {employer.name}")
        return JsonResponse({"error": "Incomplete Twilio credentials"}, status=500)

    body = (
        "Hello, this is Terry from Petro Canada. Each week, we share reminders for employees "
        "about regulated products. Please review this week’s message: "
        "https://paulfuther.eu-central-1.linodeobjects.com/compliance/4dcc0432-05f8-4e5e-b462-7c31bd7c59bd_compliance/rules.pdf "
        "Reply STOP to opt out."
    )

    result = send_linkshortened_sms(
        to_number=test_number,
        body=body,
        twilio_account_sid=sid,
        twilio_auth_token=token,
        twilio_message_service_sid=msg_sid,
    )

    logger.info(f"📤 Test SMS sent to {test_number}: {result}")
    return JsonResponse({"message": "SMS sent", "result": result})


@csrf_exempt
def generate_ai_content(request):
    if request.method == "POST":
        data = json.loads(request.body)
        prompt = data.get("prompt", "")
        mode = data.get("mode", "email")  # default to email

        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        system_message = {
            "email": "You write professional business emails.",
            "sms": "You write short, professional SMS messages under 250 characters.",
        }.get(mode, "You write clear professional messages.")

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=300 if mode == "email" else 100,
                temperature=0.7,
            )
            message = response.choices[0].message.content.strip()
            return JsonResponse({"message": message})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


def _valid_twilio_signature(request) -> bool:
    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.build_absolute_uri()
    params = request.POST.dict()
    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)


@csrf_exempt
def twilio_voice_status_webhook(request):
    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)

    # IMPORTANT: use the exact URL Twilio requested
    twilio_url = "https://6364c26b8f51.ngrok-free.app/twilio/voice/status/"

    # POST params
    post_vars = request.POST.dict()

    # Twilio signature header
    signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")

    if not validator.validate(twilio_url, post_vars, signature):
        return HttpResponse("Invalid signature", status=403)

    # ✅ Valid request
    print("✅ VALID TWILIO STATUS:", post_vars)
    return HttpResponse("ok", status=200)
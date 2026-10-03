from __future__ import absolute_import, unicode_literals

import json
import logging
import os
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from arl.documentflow.models import SentDocuSignEnvelope, SentDocuSignRecipient
from docusign_esign import ApiClient, EnvelopesApi
from docusign_esign.client.api_exception import ApiException

from arl.bucket.helpers import upload_to_linode_object_storage
from arl.celery import app

from arl.dsign.models import (
    DocuSignTemplate,
    ProcessedDocsignDocument,
    SignedDocumentFile,
)
from arl.msg.models import SmsLog
from arl.msg.tasks import send_bulk_sms
from arl.setup.models import Employer, TenantApiKeys
from arl.user.models import CustomUser, EmployerSMSTask

from .helpers import (
    create_docusign_envelope,
    create_docusign_envelope_new_hire_quiz,
    get_access_token,
    get_docusign_envelope,
    get_docusign_template_name_from_template,
    get_template_id,
    get_waiting_for_others_envelopes,
    parse_sent_date_time,
    validate_signature_roles,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def get_template_name(template_id):
    try:
        template = DocuSignTemplate.objects.get(template_id=template_id)
        return template.template_name
    except DocuSignTemplate.DoesNotExist:
        return None


@app.task(name="list dsign envelopes")
def list_all_docusign_envelopes_task():
    access_token = get_access_token()
    access_token = access_token.access_token
    account_id = settings.DOCUSIGN_ACCOUNT_ID

    api_client = ApiClient()
    api_client.host = settings.DOCUSIGN_API_CLIENT_HOST
    api_client.set_default_header("Authorization", f"Bearer {access_token}")

    envelopes_api = EnvelopesApi(api_client)

    try:
        # List envelopes from the past month
        one_month_ago = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        results = envelopes_api.list_status_changes(account_id, from_date=one_month_ago)

        # print("results : ", results)

        envelopes = []
        for envelope in results.envelopes:
            envelope_id = envelope.envelope_id
            email_results = envelopes_api.list_recipients(account_id, envelope_id)
            # print(email_results)
            emails = []
            for email in email_results.signers:
                emails.append({"email": email.email})

            envelopes.append(
                {
                    "envelope_id": envelope.envelope_id,
                    "status": envelope.status,
                    "email_subject": envelope.email_subject,
                    "sent_date_time": envelope.sent_date_time,
                    "email": emails,
                }
            )
        print("envelopes found")
        return envelopes

    except ApiException as e:
        print(f"Exception when calling EnvelopesApi->list_status_changes: {e}")
        return []


@app.task(name="sms for documents behind 48 hrs")
def check_outstanding_envelopes_task():
    # 🔍 1. Get employers that have this SMS task enabled
    enabled_employers = EmployerSMSTask.objects.filter(
        task_name="sms for documents behind 48 hrs", is_enabled=True
    ).values_list("employer_id", flat=True)

    if not enabled_employers:
        logger.info("✅ No employers have enabled the 48-hour document SMS task.")
        return "No employers enabled."

    outstanding_envelopes = get_waiting_for_others_envelopes()
    now = datetime.utcnow()

    employer_docs = defaultdict(list)
    employer_managers = defaultdict(set)

    # 📨 2. Scan each envelope and collect docs per employer
    for envelope in outstanding_envelopes:
        sent_date_time = parse_sent_date_time(envelope["sent_date_time"])
        if now - sent_date_time >= timedelta(hours=48):
            if envelope["signers"]:
                try:
                    primary_email = envelope["signers"][0]["email"]
                    user = CustomUser.objects.get(email=primary_email)
                    employer = user.employer

                    if not employer or employer.id not in enabled_employers:
                        continue

                    # 🧑‍💼 Add store manager to notify
                    if (
                        user.store
                        and user.store.manager
                        and user.store.manager.phone_number
                    ):
                        employer_managers[employer.id].add(
                            user.store.manager.phone_number
                        )

                    # 🧾 Collect signer details
                    outstanding_signers_info = []
                    for signer in envelope["signers"]:
                        if signer["status"] in ["sent", "delivered"]:
                            try:
                                signer_user = CustomUser.objects.get(
                                    email=signer["email"]
                                )
                                role = (
                                    signer_user.groups.first().name
                                    if signer_user.groups.exists()
                                    else "No Role"
                                )
                                store_name = (
                                    signer_user.store.number
                                    if signer_user.store
                                    else "No Store"
                                )
                                store_address = (
                                    signer_user.store.address
                                    if signer_user.store
                                    else "No Address"
                                )
                            except CustomUser.DoesNotExist:
                                role, store_name, store_address = (
                                    "Unknown",
                                    "Unknown",
                                    "Unknown",
                                )

                            signer_info = (
                                f"Name: {signer['name']}\n"
                                f"Email: {signer['email']}\n"
                                f"Role: {role}\n"
                                f"Store: {store_name}\n"
                                f"Address: {store_address}\n"
                            )
                            outstanding_signers_info.append(signer_info)

                    signer_list = (
                        "\n".join(outstanding_signers_info)
                        if outstanding_signers_info
                        else "No outstanding signers"
                    )

                    # 🗂️ Add to list of behind documents per employer
                    employer_docs[employer.id].append(
                        f"Document: {envelope['template_name']}\n"
                        f"Sent to: {envelope['signers'][0]['name']}\n"
                        f"Outstanding Signers:\n{signer_list}"
                    )

                except CustomUser.DoesNotExist:
                    continue

    # 🔁 3. Send message per employer
    already_notified_numbers = set()
    for employer_id, documents in employer_docs.items():
        try:
            employer = Employer.objects.get(id=employer_id)
            message = "The following documents are 48+ hours behind:\n\n" + "\n\n".join(
                documents
            )

            logger.warning(
                f"📄 SMS for {employer.name} will include {len(documents)} document(s)."
            )

            # 🔔 Get HR users for employer (dsign_sms group)
            hr_users = (
                CustomUser.objects.filter(
                    is_active=True, employer=employer, groups__name="dsign_sms"
                )
                .exclude(phone_number__isnull=True)
                .exclude(phone_number="")
            )

            hr_numbers = list(hr_users.values_list("phone_number", flat=True))
            manager_numbers = list(employer_managers[employer_id])
            all_recipients = list(set(hr_numbers + manager_numbers))
            unique_recipients = [
                n for n in all_recipients if n not in already_notified_numbers
            ]

            if not all_recipients:
                logger.warning(
                    f"⚠️ No recipients found for employer {employer.name}. Skipping."
                )
                continue

            # 🔐 Get Twilio API credentials
            twilio_keys = TenantApiKeys.objects.filter(
                employer=employer, is_active=True
            ).first()
            if not twilio_keys:
                logger.error(f"❌ No Twilio credentials for {employer.name}")
                continue

            send_bulk_sms(
                numbers=all_recipients,
                body=message,
                twilio_account_sid=twilio_keys.account_sid,
                twilio_auth_token=twilio_keys.auth_token,
                twilio_notify_sid=twilio_keys.notify_service_sid,
            )

            logger.info(
                f"📬 Sent 48-hour document reminder SMS to {len(all_recipients)} for {employer.name}"
            )
            SmsLog.objects.create(
                level="INFO",
                message=f"📬 Sent 48-hour document reminder SMS to {len(all_recipients)} for {employer.name}",
            )
            # Track notified numbers
            already_notified_numbers.update(unique_recipients)
        except Exception as e:
            logger.error(f"❌ Error sending SMS for employer {employer_id}: {e}")
            SmsLog.objects.create(
                level="ERROR",
                message=f"❌ Error sending 48-hour SMS for employer {employer_id}: {e}",
            )

    if not employer_docs:
        logger.info("✅ No documents 48+ hours behind.")
        return "No documents are 48+ hours behind."

    return "✅ Document reminders sent to relevant employers."


@app.task(name="weekly sms for documents behind 48 hrs")
def check_outstanding_envelopes_weekly_sms_task():
    outstanding_envelopes = get_waiting_for_others_envelopes()
    now = datetime.utcnow()
    behind_docs = []

    for envelope in outstanding_envelopes:
        sent_date_time = parse_sent_date_time(envelope["sent_date_time"])
        if now - sent_date_time >= timedelta(hours=48):
            # Check if there are any signers
            if envelope["signers"]:
                # Assuming the primary recipient is the first signer
                primary_recipient_name = envelope["signers"][0]["name"]
                # Retrieve the store manager for the primary recipient
                try:
                    primary_recipient_email = envelope["signers"][0]["email"]
                    user = CustomUser.objects.get(email=primary_recipient_email)
                    store_manager = (
                        user.store.manager
                        if user.store and user.store.manager
                        else "No Manager Assigned"
                    )
                except CustomUser.DoesNotExist:
                    store_manager = "Manager Not Found"

                # Prepare a detailed list of outstanding
                # signers with their role, store, and address
                outstanding_signers_info = []
                for signer in envelope["signers"]:
                    if signer["status"] in ["sent", "delivered"]:
                        # Query the CustomUser model for store
                        # and role based on the signer's email
                        try:
                            user = CustomUser.objects.get(email=signer["email"])
                            store_name = (
                                user.store.number if user.store else "No Store Assigned"
                            )
                            store_address = (
                                user.store.address
                                if user.store
                                else "No Address Available"
                            )
                            role = (
                                user.groups.first().name
                                if user.groups.exists()
                                else "No Role Assigned"
                            )

                        except CustomUser.DoesNotExist:
                            store_name = "User Not Found"
                            store_address = "Address Not Found"
                            role = "Role Not Found"

                        signer_info = (
                            f"Name: {signer['name']}\n"
                            f"Email: {signer['email']}\n"
                            f"Role: {role}\n"
                            f"Store: {store_name}\n"
                            f"Address: {store_address}\n"
                        )
                        outstanding_signers_info.append(signer_info)

                signer_list = (
                    "\n".join(outstanding_signers_info)
                    if outstanding_signers_info
                    else "No outstanding signers"
                )
            else:
                primary_recipient_name = "Unknown Recipient"
                store_manager = "Manager Not Found"
                signer_list = "No signers available"

            # Add to the list of behind documents with detailed
            # signer information
            behind_docs.append(
                f"Document: {envelope['template_name']}\n"
                f"Sent to: {primary_recipient_name}\n"
                f"Manager: {store_manager}\n"
                f"Outstanding Signers:\n{signer_list}"
            )

    if behind_docs:
        document_list = "\n\n".join(behind_docs)
        message = f"The following documents are 48 hours behind: \n\n{document_list}"

        # Log the message that will be sent
        logger.info(f"Sending SMS: {message}")

        # Get all active users in the 'dsign_sms' group
        hr_users = CustomUser.objects.filter(
            Q(is_active=True) & Q(groups__name="weekly_dsign_sms")
        ).values_list("phone_number", flat=True)

        # Send the SMS message to all users in the group
        send_bulk_sms(hr_users, message)

        logger.info(f"Sent SMS for documents 48 hours behind to HR: {message}")
        return f"Sent SMS for documents 48 hours behind to HR: {document_list}"

    else:
        logger.info("No documents are 48 hours behind.")
        return "No documents are 48 hours behind."


@app.task(name="get outstanding docs")
def get_outstanding_docs():
    try:
        # Retrieve the outstanding envelopes
        envelopes = get_waiting_for_others_envelopes()
        serialized_envelopes = []

        for envelope in envelopes:
            # Check if there are signers
            if envelope["signers"]:
                primary_recipient_name = envelope["signers"][0]["name"]
                serialized_signers = []

                for signer in envelope["signers"]:
                    # Skip signers that have already completed signing
                    if signer["status"].lower() == "completed":
                        continue
                    # Query the CustomUser model for store and role
                    try:
                        user = CustomUser.objects.get(email=signer["email"])
                        store = user.store.number if user.store else "No Store Assigned"
                        store_address = (
                            user.store.address if user.store else "No Address Available"
                        )
                        role = (
                            user.groups.first().name
                            if user.groups.exists()
                            else "No Role Assigned"
                        )
                        print(store, role)
                    except CustomUser.DoesNotExist:
                        store = "User Not Found"
                        store_address = "Address Not Found"
                        role = "Role Not Found"

                    # Add signer information along with store and role
                    serialized_signers.append(
                        {
                            "name": signer["name"],
                            "email": signer["email"],
                            "status": signer["status"],
                            "store": store,
                            "address": store_address,
                            "role": role,
                        }
                    )

            else:
                primary_recipient_name = "Unknown Recipient"
                serialized_signers = []

            # Append the serialized envelope data
            serialized_envelopes.append(
                {
                    "template_name": envelope["template_name"],
                    "status": envelope["status"],
                    "sent_date_time": envelope["sent_date_time"],
                    "primary_recipient": primary_recipient_name,
                    "signers": serialized_signers,
                }
            )

        # Log the number of outstanding envelopes found
        if serialized_envelopes:
            logger.info(f"Found {len(serialized_envelopes)} outstanding envelopes.")
        else:
            logger.info("No outstanding envelopes found.")

        return serialized_envelopes

    except Exception as e:
        # Log any errors that occur during the task execution
        logger.error(f"Error occurred in get_outstanding_docs task: {e}, exc_info=True")
        return []


def extract_recipient_email(payload):
    """Extracts recipient email, falls back to sender email if unavailable."""
    recipient_email = (
        payload.get("data", {})
        .get("recipients", {})
        .get("signers", [{}])[0]
        .get("email", "")
    )

    # Fallback to sender email if no recipient email is found
    if not recipient_email:
        recipient_email = payload.get("data", {}).get("sender", {}).get("email", "")

    return recipient_email


# ============ Webhook for Docusign =======================
@app.task(name="docusign_webhook")
def process_docusign_webhook(payload):
    """Handles DocuSign webhook events for sent, delivered, recipient-completed, and templateSaved."""
    try:
        event_type = payload.get("event")
        data = payload.get("data", {}) or {}
        envelope_id = data.get("envelopeId", "")
        envelope_summary = data.get("envelopeSummary", {}) or {}
        signers = envelope_summary.get("recipients", {}).get("signers", []) or []

        template_id = get_template_id(payload)
        template_name = get_docusign_template_name_from_template(template_id)

        logger.info(
            f"📨 DocuSign webhook received | event={event_type} | envelope_id={envelope_id} | template_id={template_id} | template_name={template_name}"
        )

        # templateSaved is separate
        if event_type == "templateSaved":
            template_id = payload.get("templateId")
            user_email = payload.get("userEmail")
            if template_id and user_email:
                handle_template_saved_task.delay(template_id, user_email)
                return {"status": "template saved task triggered"}

            logger.error("❌ Missing templateId or userEmail in templateSaved event.")
            return {"error": "Missing templateId or userEmail"}

        if not envelope_id:
            logger.error("❌ Missing envelope_id in DocuSign webhook payload.")
            return {"error": "Missing envelope_id"}

        sent_envelope = (
            SentDocuSignEnvelope.objects.select_related(
                "flow", "flow_step", "user", "template", "employer"
            )
            .filter(envelope_id=envelope_id)
            .first()
        )

        if sent_envelope and signers:
            sync_sent_envelope_recipients(sent_envelope, payload)
        elif not sent_envelope:
            logger.warning(
                f"⚠️ No SentDocuSignEnvelope found for envelope {envelope_id}"
            )
        else:
            logger.warning(
                f"⚠️ No signers found in payload for envelope {envelope_id}"
            )

        # legacy fallback only if absolutely needed
        recipient_email = ""
        if signers:
            recipient_email = signers[0].get("email", "") or ""

        if not recipient_email:
            recipient_email = (
                envelope_summary.get("sender", {}).get("email", "") or ""
            )

        if event_type == "envelope-sent":
            return handle_envelope_sent(
                envelope_id=envelope_id,
                recipient_email=recipient_email,
                template_name=template_name,
            )

        if event_type == "envelope-delivered":
            return handle_envelope_delivered(
                envelope_id=envelope_id,
                recipient_email=recipient_email,
                template_name=template_name,
            )

        if event_type == "recipient-completed":
            triggering_recipient_id = str(data.get("recipientId") or "").strip()
            return handle_recipient_completed(
                envelope_id=envelope_id,
                recipient_email=recipient_email,  # temporary for compatibility
                template_id=template_id,
                template_name=template_name,
                payload=payload,
                triggering_recipient_id=triggering_recipient_id,
            )

        logger.info(f"ℹ️ Unhandled DocuSign event type: {event_type}")
        return {"status": "ignored"}

    except json.JSONDecodeError:
        logger.error("❌ Invalid JSON payload")
        return {"error": "Invalid JSON payload"}
    except Exception as e:
        logger.exception("❌ Error processing DocuSign webhook")
        return {"error": str(e)}


# ✅ Handles the "templateSaved" event
def handle_template_saved(template_id, user_email):
    if not template_id or not user_email:
        logger.error("❌ Missing templateId or userEmail in templateSaved event.")
        return {"error": "Missing templateId or userEmail"}

    try:
        user = User.objects.get(email=user_email)
        if user and user.employer:
            # Prevent duplicate template entries
            if not DocuSignTemplate.objects.filter(
                template_id=template_id, employer=user.employer
            ).exists():
                DocuSignTemplate.objects.create(
                    employer=user.employer,
                    template_id=template_id,
                    name=f"Template {template_id}",
                    created_by=user,
                )
                logger.info(
                    f"✅ Template {template_id} saved for employer {user.employer}"
                )
            else:
                logger.info(
                    f"ℹ️ Template {template_id} already exists for employer {user.employer}"
                )
        return {"status": "success"}
    except User.DoesNotExist:
        logger.error(f"❌ User with email {user_email} not found")
        return {"error": "User not found"}


# ✅ Handles Envelope Sent
def handle_envelope_sent(envelope_id, recipient_email, template_name):
    """Handles document sent events and notifies HR via SMS."""
    try:
        if not envelope_id or not template_name:
            logger.error(
                f"❌ Missing envelope_id or template_name: {envelope_id}, {template_name}"
            )
            return {"error": "Missing envelope ID or template name"}

        sent_envelope = (
            SentDocuSignEnvelope.objects.select_related("user", "employer")
            .filter(envelope_id=envelope_id)
            .first()
        )

        if not sent_envelope:
            logger.warning(
                f"⚠️ No SentDocuSignEnvelope found for envelope {envelope_id}. "
                f"Tracking row may not have been created at send time."
            )
            return {"error": "Tracking row not found"}

        user = sent_envelope.user
        employer = sent_envelope.employer or getattr(user, "employer", None)

        if not employer:
            logger.error(f"❌ Employer not found for envelope {envelope_id}.")
            return {"error": "Employer not found"}

        sent_envelope.status = "sent"
        sent_envelope.save(update_fields=["status"])

        display_name = user.get_full_name() if user else recipient_email
        display_email = user.email if user else recipient_email

        notify_hr.delay(
            template_name,
            display_name,
            display_email,
            employer.id,
            event_type="sent",
        )

        logger.info(
            f"📨 Sent HR notification for envelope sent: {template_name} to {display_email}"
        )
        return {"status": "HR notified"}

    except Exception as e:
        logger.exception(f"❌ Error in handle_envelope_sent for {envelope_id}")
        return {"error": str(e)}


# ✅ Handles "recipient-completed" event
def handle_recipient_completed(
    envelope_id,
    recipient_email,
    template_id,
    template_name,
    payload=None,
    triggering_recipient_id=None,
    upload_now=False,
):
    """Handles recipient completion events and only finalizes the flow step when the whole envelope is completed."""
    try:
        payload = payload or {}
        data = payload.get("data", {}) or {}
        envelope_summary = data.get("envelopeSummary", {}) or {}
        envelope_status = (envelope_summary.get("status") or "").lower().strip()

        template = DocuSignTemplate.objects.filter(template_id=template_id).first()
        if not template:
            logger.error(f"❌ DocuSignTemplate with ID {template_id} not found.")
            return {"error": "Template not found"}

        sent_envelope = (
            SentDocuSignEnvelope.objects.select_related(
                "flow", "flow_step", "user", "template", "employer"
            )
            .filter(envelope_id=envelope_id)
            .first()
        )

        if not sent_envelope:
            logger.error(
                f"❌ SentDocuSignEnvelope with envelope_id {envelope_id} not found."
            )
            return {"error": "Sent envelope tracking row not found"}

        # Sync recipient rows again here for safety
        if payload:
            sync_sent_envelope_recipients(sent_envelope, payload)

        # If the whole envelope is not complete yet, do NOT finish the flow step.
        if envelope_status != "completed":
            logger.info(
                f"ℹ️ recipient-completed received for envelope {envelope_id}, "
                f"but envelope status is still '{envelope_status}'. "
                f"Recipient pill updated; flow step will not be completed yet."
            )

            if sent_envelope.status != "completed":
                sent_envelope.status = "sent"
                sent_envelope.save(update_fields=["status"])

            return {
                "status": "recipient updated only",
                "envelope_status": envelope_status,
                "triggering_recipient_id": triggering_recipient_id,
            }

        # Whole envelope is complete here
        user = sent_envelope.user
        employer = sent_envelope.employer or template.employer

        if template.is_company_document:
            processed_doc, created = ProcessedDocsignDocument.objects.get_or_create(
                envelope_id=envelope_id,
                defaults={
                    "template_name": template_name,
                    "user": user,
                    "employer": employer,
                    "is_company_document": True,
                },
            )

            sent_envelope.status = "completed"
            completed_dt = envelope_summary.get("completedDateTime")
            sent_envelope.completed_at = (
                parse_datetime(completed_dt) if completed_dt else timezone.now()
            )
            sent_envelope.save(update_fields=["status", "completed_at"])

            if created:
                logger.info(f"✅ Saved company signed document: {processed_doc}")
                upload_args = (
                    envelope_id,
                    user.id if user else None,
                    employer.id,
                    template_name,
                )
                if upload_now:
                    fetch_and_upload_signed_documents(
                        *upload_args, is_company_document=True
                    )
                else:
                    fetch_and_upload_signed_documents.delay(
                        *upload_args, is_company_document=True
                    )

            return {"status": "success", "document_id": processed_doc.id}

        # Employee document
        if not user:
            logger.error(
                f"❌ SentDocuSignEnvelope {envelope_id} has no linked user."
            )
            return {"error": "No linked user on sent envelope"}

        processed_doc, created = ProcessedDocsignDocument.objects.get_or_create(
            envelope_id=envelope_id,
            defaults={
                "template_name": template_name,
                "user": user,
                "employer": employer,
            },
        )

        completed_dt = envelope_summary.get("completedDateTime")
        sent_envelope.status = "completed"
        sent_envelope.completed_at = (
            parse_datetime(completed_dt) if completed_dt else timezone.now()
        )
        sent_envelope.save(update_fields=["status", "completed_at"])

        if created:
            logger.info(f"✅ Saved signed document: {processed_doc}")

            if upload_now:
                fetch_and_upload_signed_documents(
                    envelope_id, user.id, employer.id, template_name
                )
            else:
                fetch_and_upload_signed_documents.delay(
                    envelope_id, user.id, employer.id, template_name
                )

            notify_hr.delay(
                template_name,
                user.get_full_name(),
                user.email,
                employer.id,
                event_type="completed",
            )

        next_step = None
        if sent_envelope.flow and sent_envelope.flow_step:
            next_step = (
                sent_envelope.flow.steps.filter(
                    is_active=True,
                    step_order__gt=sent_envelope.flow_step.step_order,
                )
                .order_by("step_order")
                .first()
            )

        if next_step and not next_step.template:
            logger.error(f"❌ Next flow step {next_step.id} has no template.")
            next_step = None

        if next_step:
            already_sent = SentDocuSignEnvelope.objects.filter(
                user=user,
                flow=sent_envelope.flow,
                flow_step=next_step,
            ).exists()

            if not already_sent:
                logger.info(
                    f"📄 Sending next document flow step for user {user.email}: "
                    f"{next_step.display_name} (step_order={next_step.step_order})"
                )

                create_docusign_envelope_task.delay(
                    {
                        "user_id": user.id,
                        "employer_id": employer.id,
                        "signer_email": user.email,
                        "signer_name": user.get_full_name() or user.username,
                        "template_id": next_step.template.template_id,
                        "flow_id": sent_envelope.flow.id,
                        "flow_step_id": next_step.id,
                    }
                )

        return {"status": "success", "document_id": processed_doc.id}

    except Exception as e:
        logger.exception(f"❌ Error in handle_recipient_completed for {envelope_id}")
        return {"error": str(e)}


def _webhook_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def refresh_sent_envelope_from_docusign(sent_envelope):
    """Apply the email webhook's completion handler after an in-app signature."""
    access_token = get_access_token().access_token
    api_client = ApiClient()
    api_client.host = settings.DOCUSIGN_API_CLIENT_HOST
    api_client.set_default_header("Authorization", f"Bearer {access_token}")
    envelopes_api = EnvelopesApi(api_client)
    account_id = settings.DOCUSIGN_ACCOUNT_ID
    remote = envelopes_api.get_envelope(account_id, sent_envelope.envelope_id)
    listed = envelopes_api.list_recipients(account_id, sent_envelope.envelope_id)
    signers = []
    for signer in list(getattr(listed, "signers", None) or []):
        signers.append(
            {
                "recipientId": signer.recipient_id,
                "recipientIdGuid": getattr(signer, "recipient_id_guid", "") or "",
                "roleName": getattr(signer, "role_name", "") or "",
                "name": signer.name or "",
                "email": signer.email or "",
                "routingOrder": getattr(signer, "routing_order", None) or 1,
                "status": signer.status or "",
                "sentDateTime": _webhook_time(getattr(signer, "sent_date_time", None)),
                "deliveredDateTime": _webhook_time(
                    getattr(signer, "delivered_date_time", None)
                ),
                "signedDateTime": _webhook_time(
                    getattr(signer, "signed_date_time", None)
                ),
            }
        )
    gsa = next(
        (
            signer
            for signer in signers
            if (signer.get("roleName") or "").strip().lower() == "gsa"
        ),
        None,
    )
    triggering = (gsa or (signers[0] if signers else {})).get("recipientId")
    template = sent_envelope.template
    template_id = template.template_id if template else ""
    template_name = sent_envelope.template_name or (
        template.template_name if template else ""
    )
    payload = {
        "data": {
            "envelopeId": sent_envelope.envelope_id,
            "recipientId": triggering,
            "envelopeSummary": {
                "status": (getattr(remote, "status", None) or ""),
                "completedDateTime": _webhook_time(
                    getattr(remote, "completed_date_time", None)
                ),
                "recipients": {"signers": signers},
            },
        }
    }
    logger.info(
        "Refreshing envelope %s after in-app signing. DocuSign status=%s.",
        sent_envelope.envelope_id,
        payload["data"]["envelopeSummary"]["status"],
    )
    return handle_recipient_completed(
        envelope_id=sent_envelope.envelope_id,
        recipient_email=(gsa or {}).get("email", ""),
        template_id=template_id,
        template_name=template_name,
        payload=payload,
        triggering_recipient_id=triggering,
        upload_now=True,
    )


# ✅ Sends SMS notifications to HR using the correct Twilio API keys
@app.task(name="notify_hr_task")
def notify_hr(template_name, full_name, recipient_email, employer_id, event_type):
    try:
        employer = Employer.objects.get(id=employer_id)
        logger.info(f"🔹 Employer found: {employer.name}")

        # ✅ Retrieve the Twilio credentials for the employer
        try:
            tenant_keys = TenantApiKeys.objects.get(employer=employer)
            twilio_account_sid = tenant_keys.account_sid
            twilio_auth_token = tenant_keys.auth_token
            twilio_notify_sid = tenant_keys.notify_service_sid
            logger.info(f"✅ Retrieved Twilio keys for employer {employer.name}")

        except TenantApiKeys.DoesNotExist:
            logger.error(
                f"❌ No Twilio API keys found for employer {employer}. Skipping SMS."
            )
            return

        # ✅ Fetch HR users for this employer
        hr_users = User.objects.filter(
            Q(is_active=True) & Q(groups__name="dsign_sms"), employer=employer
        ).values_list("phone_number", flat=True)

        if not hr_users:
            logger.warning(f"ℹ️ No HR users found for employer {employer}.")
            return

        # ✅ Determine message based on event type
        if event_type == "sent":
            message_body = (
                f"DocuSign document '{template_name}' was sent to {recipient_email}"
            )
        else:
            message_body = (
                f"{template_name} completed by: {full_name} at {recipient_email}"
            )

        # ✅ Send SMS
        success = send_bulk_sms(
            hr_users,
            message_body,
            twilio_account_sid,
            twilio_auth_token,
            twilio_notify_sid,
        )

        if success:
            logger.info(
                f"📨 SMS sent successfully for 'completed' status: {message_body}"
            )
        else:
            logger.error(
                f"❌ Failed to send SMS for 'completed' status: {message_body}"
            )

    except Employer.DoesNotExist:
        logger.error(f"❌ Employer with ID {employer_id} not found.")
    except Exception as e:
        logger.error(f"❌ Unexpected error in notify_hr_task: {str(e)}")


@app.task(name="handle_template_saved_task")
def handle_template_saved_task(template_id, user_email):
    """
    Celery task to handle 'templateSaved' event and store the template.
    """
    if not template_id or not user_email:
        logger.error("❌ Missing templateId or userEmail in templateSaved event.")
        return {"error": "Missing templateId or userEmail"}

    try:
        user = User.objects.get(email=user_email)
        if user and user.employer:
            employer = user.employer

            # ✅ Prevent duplicate template entries
            if not DocuSignTemplate.objects.filter(
                template_id=template_id, employer=employer
            ).exists():
                DocuSignTemplate.objects.create(
                    employer=employer,
                    template_id=template_id,
                    name=f"Template {template_id}",
                    created_by=user,
                )
                logger.info(
                    f"✅ Template {template_id} saved for employer {employer.name}"
                )
            else:
                logger.info(
                    f"ℹ️ Template {template_id} already exists for employer {employer.name}"
                )

        return {"status": "success"}

    except User.DoesNotExist:
        logger.error(f"❌ User with email {user_email} not found")
        return {"error": "User not found"}
    except Exception as e:
        logger.error(f"❌ Error processing templateSaved event: {str(e)}")
        return {"error": str(e)}


@app.task(name="upload_signed_document_to_linode")
def upload_signed_document_to_linode(document_id):
    """Uploads signed documents to Linode Object Storage."""
    from .models import ProcessedDocsignDocument

    try:
        document = ProcessedDocsignDocument.objects.get(envelope_id=document_id)
        employer_name = document.employer.name.replace(" ", "_")
        object_key = f"DOCUMENTS/{employer_name}/{document.envelope_id}.pdf"

        # Retrieve document (replace with actual method)
        response = get_docusign_envelope(document.envelope_id)

        if response.status_code == 200:
            document_bytes = BytesIO(response.content)
            upload_to_linode_object_storage(document_bytes, object_key)
        else:
            logger.error(
                f"Failed to fetch document from DocuSign for envelope {document.envelope_id}. Status: {response.status_code}"
            )

    except ProcessedDocsignDocument.DoesNotExist:
        logger.error(f"❌ Document {document_id} not found.")


def handle_envelope_delivered(envelope_id, recipient_email, template_name):
    """Handles DocuSign delivered/opened events."""
    try:
        sent_envelope = SentDocuSignEnvelope.objects.filter(
            envelope_id=envelope_id
        ).first()

        if not sent_envelope:
            logger.warning(
                f"⚠️ No SentDocuSignEnvelope found for envelope {envelope_id}"
            )
            return {"error": "Tracking row not found"}

        if sent_envelope.status not in ["completed", "delivered"]:
            sent_envelope.status = "delivered"
            sent_envelope.save(update_fields=["status", "updated_at"])

        logger.info(
            f"✅ Marked envelope {envelope_id} as delivered for {recipient_email} ({template_name})"
        )
        return {"status": "updated"}

    except Exception as e:
        logger.exception(f"❌ Error in handle_envelope_delivered for {envelope_id}")
        return {"error": str(e)}


def sync_sent_envelope_recipients(sent_envelope, payload):
    envelope_summary = payload.get("data", {}).get("envelopeSummary", {}) or {}
    signers = envelope_summary.get("recipients", {}).get("signers", []) or []

    if not signers:
        logger.warning(
            f"⚠️ No signers found while syncing recipients for envelope {sent_envelope.envelope_id}"
        )
        return

    for signer in signers:
        recipient_id = str(signer.get("recipientId") or "").strip()
        if not recipient_id:
            logger.warning(
                f"⚠️ Missing recipientId for signer in envelope {sent_envelope.envelope_id}"
            )
            continue

        status = (signer.get("status") or "created").lower().strip()

        SentDocuSignRecipient.objects.update_or_create(
            sent_envelope=sent_envelope,
            recipient_id=recipient_id,
            defaults={
                "recipient_id_guid": signer.get("recipientIdGuid", "") or "",
                "role_name": signer.get("roleName", "") or "",
                "name": signer.get("name", "") or "",
                "email": signer.get("email", "") or "",
                "routing_order": int(signer.get("routingOrder") or 1),
                "status": status,
                "sent_at": parse_datetime(signer.get("sentDateTime"))
                if signer.get("sentDateTime")
                else None,
                "delivered_at": parse_datetime(signer.get("deliveredDateTime"))
                if signer.get("deliveredDateTime")
                else None,
                "completed_at": parse_datetime(signer.get("signedDateTime"))
                if signer.get("signedDateTime")
                else None,
            },
        )

    logger.info(
        f"✅ Synced {len(signers)} recipient rows for envelope {sent_envelope.envelope_id}"
    )


# ========================================================
# end of webhook functions


@app.task(name="create_docusign_envelope")
def create_docusign_envelope_task(envelope_args):
    logger.info(f"RAW ENVELOPE ARGS IN TASK: {envelope_args}")
    signer_name = envelope_args.get("signer_name")

    try:
        result = create_docusign_envelope(envelope_args)

        if result.get("success"):
            logger.info(
                f"Docusign envelope {result['envelope_id']} for {signer_name} created successfully"
            )
            return result

        logger.error(
            f"Error creating Docusign envelope for {signer_name}: {result.get('error')}"
        )
        return result

    except Exception as e:
        logger.error(
            f"Error creating Docusign envelope New Hire File for {signer_name}: {str(e)}"
        )
        return {
            "success": False,
            "error": str(e),
            "signer_name": signer_name,
        }


@app.task(name="send_new_hire_quiz")
def send_new_hire_quiz(envelope_args):
    try:
        create_docusign_envelope_new_hire_quiz(envelope_args)
        signer_name = envelope_args.get("signer_name")
        logger.info(
            f"Docusign envelope New Hire Quiz for {signer_name} created successfully"
        )

        return f"Docusign envelope New Hire Quiz for {signer_name} created successfully"
    except Exception as e:
        logger.error(
            f"Error creating Docusign New Hire Quiz for {signer_name} "
            f"envelope: {str(e)}"
        )
        return (
            f"Error creating Docusign New Hire Quiz for {signer_name}envelope: {str(e)}"
        )


# This is the FINAL version of the task to upload to linode.
@app.task(name="fetch_and_upload_signed_documents")
def fetch_and_upload_signed_documents(
    envelope_id, user_id, employer_id, template_name, is_company_document=False
):
    try:
        employer = Employer.objects.get(id=employer_id)
        user = None

        if user_id is not None:
            user = CustomUser.objects.get(id=user_id)

    except Exception as e:
        logger.error(f"❌ User or employer not found: {e}")
        return

    try:
        access_token = get_access_token().access_token
        api_client = ApiClient()
        api_client.host = settings.DOCUSIGN_API_CLIENT_HOST
        api_client.set_default_header("Authorization", f"Bearer {access_token}")

        envelopes_api = EnvelopesApi(api_client)

        # ✅ Get the path to the zip file
        zip_path = envelopes_api.get_document(
            settings.DOCUSIGN_ACCOUNT_ID, "archive", envelope_id
        )

        logger.warning(f"📦 Zip file obtained at: {zip_path}")

        # ✅ Read the file from disk
        if isinstance(zip_path, str) and os.path.exists(zip_path):
            with open(zip_path, "rb") as f:
                zip_bytes = f.read()
        else:
            logger.error(f"❌ ZIP path not found or invalid: {zip_path}")
            return

        zip_buffer = BytesIO(zip_bytes)
        folder_name = f"{uuid.uuid4().hex[:8]}"

        # ✅ Determine the upload path based on document type
        if is_company_document:
            upload_path_prefix = (
                f"DOCUMENTS/{employer.name.replace(' ', '_')}/company/{folder_name}/"
            )
        else:
            upload_path_prefix = (
                f"DOCUMENTS/{employer.name.replace(' ', '_')}/{folder_name}/"
            )

        with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
            for file_name in zip_ref.namelist():
                with zip_ref.open(file_name) as file:
                    file_content = file.read()
                    linode_path = f"{upload_path_prefix}{file_name}"

                    upload_to_linode_object_storage(BytesIO(file_content), linode_path)
                    logger.info(f"✅ Uploaded {file_name} to Linode at {linode_path}")

                    # ✅ Save to SignedDocumentFile
                    SignedDocumentFile.objects.create(
                        user=user,
                        employer=employer,
                        envelope_id=envelope_id,
                        file_name=file_name,
                        file_path=linode_path,
                        template_name=template_name,
                        is_company_document=is_company_document,
                    )

    except Exception as e:
        logger.error(f"❌ Failed to fetch/upload signed DocuSign docs: {str(e)}")


@app.task(name="validate_signature_tabs")
def validate_template_signature_tabs_task(template_id):
    template = DocuSignTemplate.objects.filter(template_id=template_id).first()
    if not template:
        return

    is_valid, issues = validate_signature_roles(template_id)
    template.is_ready_to_send = is_valid
    template.save(update_fields=["is_ready_to_send"])

    return {
        "template_id": template_id,
        "is_valid": is_valid,
        "issues": issues,
    }


@app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def process_company_document_task(self, sdf_id: str, tmp_path: str, object_key: str):
    logger.info(
        "[CompanyDocTask] Start sdf_id=%s key=%s tmp=%s", sdf_id, object_key, tmp_path
    )
    try:
        with open(tmp_path, "rb") as f:
            upload_to_linode_object_storage(f, object_key)
        with transaction.atomic():
            sdf = SignedDocumentFile.objects.select_for_update().get(id=sdf_id)
            # if you store size_bytes, you could set it here from os.path.getsize(tmp_path)
            logger.info("[CompanyDocTask] Uploaded sdf_id=%s -> %s", sdf_id, object_key)
    except Exception as e:
        logger.exception("[CompanyDocTask] FAILED sdf_id=%s: %s", sdf_id, e)
        # optionally: mark failed or delete SDF so list doesn’t show a broken entry
        try:
            SignedDocumentFile.objects.filter(id=sdf_id).delete()
            logger.warning("[CompanyDocTask] Deleted SDF id=%s after failure", sdf_id)
        except Exception:
            logger.exception(
                "[CompanyDocTask] Could not delete SDF id=%s after failure", sdf_id
            )
        raise
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
                logger.info("[CompanyDocTask] Temp removed %s", tmp_path)
        except Exception:
            logger.exception("[CompanyDocTask] Could not remove temp %s", tmp_path)

import base64
import json
import os
import traceback  # For detailed error reporting

from celery.utils.log import get_task_logger
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import render
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Asm,
    Attachment,
    ClickTracking,
    Content,
    ContentId,
    CustomArg,
    Disposition,
    FileContent,
    FileName,
    FileType,
    Mail,
    OpenTracking,
    Personalization,
    To,
    TrackingSettings,
)
from twilio.base.exceptions import TwilioException
from twilio.rest import Client

from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, SMSOptOut, Store

logger = get_task_logger(__name__)

account_sid = settings.TWILIO_ACCOUNT_SID
auth_token = settings.TWILIO_AUTH_TOKEN
twilio_from = settings.TWILIO_FROM
twilio_verify_sid = settings.TWILIO_VERIFY_SID
notify_service_sid = settings.TWILIO_NOTIFY_SERVICE_SID

client = Client(account_sid, auth_token)


sg = SendGridAPIClient(settings.SENDGRID_API_KEY)

# whatsapp message


# function to create an email using sendgrid and tempaltes


def create_tobacco_email(to_email, name):
    try:
        templatename = "Required Actions for Tobacco and Vape"
        bm = "d-488749fd81d4414ca7bbb2eea2b830db"
        message = Mail(from_email=settings.MAIL_DEFAULT_SENDER, to_emails=to_email)
        message.dynamic_template_data = {
            "subject": templatename,
            "name": name,
        }
        message.template_id = bm
        response = sg.send(message)

        # Handle the response and return an appropriate value based on your requirements
        if response.status_code != 202:
            logger.error(
                f"Failed to send email to {to_email}. Error code: {response.status_code}"
            )
    except Exception as e:
        error_message = f"An error occurred while sending email to {to_email}: {str(e)}"
        logger.error(error_message)

        if hasattr(e, "response") and e.response is not None:
            response_body = e.response.body
            response_status = e.response.status_code
            logger.error(f"SendGrid response status code: {response_status}")
            logger.error(f"SendGrid response body: {response_body}")


# this is the master email function
# It works for a new hire registration for HR data
# And the onbording of a new hire.
# This will be the master function going forward.
def create_master_email(
    to_email,
    sendgrid_id,
    template_data,
    attachments=None,
    verified_sender=None,
    custom_args=None,
    html_content=None,
):
    try:
        unsubscribe_group_id = 24753
        if not isinstance(template_data, dict):
            raise ValueError(
                f"❌ Expected dictionary for template_data, got {type(template_data)}"
            )
        # Ensure to_email is a list, even if a single string is passed
        if isinstance(to_email, str):
            to_email = [to_email]  # Convert single email to list
        logger.info(f"📧 The helper is sending email to: {', '.join(to_email)}")
        # ✅ If `verified_sender` is explicitly provided, use it without any lookup
        if verified_sender:
            sender_email = verified_sender
            logger.info(f"✅ Using explicitly provided sender email: {verified_sender}")
        else:
            # ✅ Look up the employer and their verified sender email
            user_email = (
                to_email[0] if isinstance(to_email, list) and to_email else to_email
            )
            try:
                user = CustomUser.objects.get(email=user_email)
                employer = user.employer
                logger.info(f"✅ Found user: {user.email}, Employer: {employer}")
            except CustomUser.DoesNotExist:
                logger.info(
                    f"❌ No user found with email {user_email}. Using default sender."
                )
                sender_email = settings.MAIL_DEFAULT_SENDER
            else:
                # ✅ Retrieve the sender email from the Tenant API Key model
                tenant_api_key = TenantApiKeys.objects.filter(employer=employer).first()
                sender_email = (
                    tenant_api_key.verified_sender_email
                    if tenant_api_key
                    else settings.MAIL_DEFAULT_SENDER
                )

        logger.info(f"📧 Final sender email: {sender_email}")
        # Initialize the email message
        message = Mail(
            from_email=sender_email,
        )

        logger.info(f"📜 Email Template Data: {template_data}")
        # In-app HTML is sent as content; SendGrid is transport only.
        # Legacy dynamic templates still use template_id.
        if html_content:
            if template_data.get("subject"):
                message.subject = template_data["subject"]
            message.add_content(Content("text/html", html_content))
            # Dynamic templates inherit account/template click tracking.
            # Raw HTML does not, so enable it here so in-app audit/engagement
            # events still reach the webhook.
            tracking = TrackingSettings()
            tracking.click_tracking = ClickTracking(enable=True, enable_text=False)
            tracking.open_tracking = OpenTracking(enable=True)
            message.tracking_settings = tracking
        elif sendgrid_id:
            message.template_id = sendgrid_id
        asm = Asm(
            group_id=unsubscribe_group_id,
        )
        message.asm = asm

        # ✅ Create Personalization
        personalization = Personalization()
        for email in to_email:
            personalization.add_to(To(email))
        if not html_content:
            personalization.dynamic_template_data = template_data

        if template_data.get("subject"):
            personalization.subject = template_data["subject"]

        # Unique args so Event Webhook payloads include the resolved subject
        # (SendGrid does not always send a native `subject` field for templates).
        if custom_args:
            for key, value in custom_args.items():
                if value is None:
                    continue
                arg = CustomArg(str(key), str(value))
                personalization.add_custom_arg(arg)
                # Mail-level copy so Event Webhook still sees args when
                # SendGrid drops personalization unique_args on html_content.
                try:
                    message.add_custom_arg(CustomArg(str(key), str(value)))
                except Exception:
                    pass

        message.add_personalization(personalization)

        logger.info("✅ Final Email attachments summary:")
        for a in attachments or []:
            filename = a.get("filename", "Unnamed file")
            content = a.get("content", b"")
            filetype = a.get("type", "unknown")

            size_kb = (
                len(content) // 1024 if isinstance(content, (bytes, str)) else "N/A"
            )
            logger.info(f"• {filename} ({filetype}) - {size_kb} KB")

        # Handle attachments if provided
        if attachments:
            for att in attachments:
                try:
                    attachment_instance = Attachment(
                        FileContent(att["content"]),
                        FileName(att.get("filename", "file")),
                        FileType(att.get("type", "application/octet-stream")),
                        Disposition(att.get("disposition", "attachment")),
                    )
                    message.add_attachment(attachment_instance)
                except Exception as e:
                    logger.info(
                        f"❌ Error adding attachment {att.get('filename', '')}: {e}"
                    )

        # Send the email via SendGrid
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        response = sg.send(message)

        # Check response status
        if response.status_code == 202:
            logger.info(f"Email sent successfully to {to_email}.")
            return True
        else:
            logger.info(
                f"Failed to send email to {to_email}. Status code: {response.status_code}"
            )
            return False

    except Exception as e:
        # Extract detailed error information if available
        error_details = str(e)
        if hasattr(e, "body"):
            error_details += f" Response body: {e.body}"

        # Log error details for debugging
        logger.info(f"Error in create_master_email: {error_details}")
        traceback.print_exc()  # For detailed error trace
        return False


# for now we are going to keep this helper file
# it has data in the dynamic_template_date that is needed
# Eventually, we will merge this with master email.
def create_hr_newhire_email(**kwargs):
    CustomUser = get_user_model()

    # Get all active users in the 'hr' group
    hr_users = CustomUser.objects.filter(
        Q(is_active=True) & Q(groups__name="new_hire_data_email")
    )
    # Extract email addresses from the CustomUser objects
    to_emails = [user.email for user in hr_users]

    # Prepare Email
    message = Mail(
        from_email=settings.MAIL_DEFAULT_SENDER,
        to_emails=to_emails,
        subject="We have a New Employee",
    )
    message.dynamic_template_data = {
        "firstname": kwargs["firstname"],
        "lastname": kwargs["lastname"],
        "store": kwargs["store_number"],
        "storeaddress": kwargs["store_address"],
        "email": kwargs["email"],
        "mobilephone": kwargs["mobilephone"],
        "addressone": kwargs["addressone"],
        "addresstwo": kwargs["addresstwo"],
        "city": kwargs["city"],
        "province": kwargs["province"],
        "postal": kwargs["postal"],
        "country": kwargs["country"],
        "sin_number": kwargs["sin_number"],
        "dob": kwargs["dob"],
        "sin_expiration_date": kwargs["sin_expiration_date"],
        "work_permit_expiration_date": kwargs["work_permit_expiration_date"],
    }
    message.template_id = "d-d0806dff1e62449d9ba8cfcb481accaa"

    try:
        response = sg.send(message)

        # Handle the response and return an appropriate value based on your requirements
        if response.status_code == 202:
            return True
        else:
            print("Failed to send email. Error code:", response.status_code)
            return False

    except Exception as e:
        print("Error sending email:", e)
        return False


def send_sms_model(phone_number, message):
    try:
        message = client.messages.create(
            body=message,
            from_=twilio_from,
            to=phone_number,
        )
        return message.sid
    except Exception as e:
        print("Failed to send SMS:", str(e))
    return None


def send_sms(phone_number, body):
    try:
        message = client.messages.create(
            body=body,
            from_=twilio_from,
            to=phone_number,
        )
        return message.sid
    except Exception as e:
        print("Failed to send SMS:", str(e))
    return None


def send_linkshortened_sms(
    to_number, body, twilio_account_sid, twilio_auth_token, twilio_message_service_sid
):
    try:
        if (
            not twilio_account_sid
            or not twilio_auth_token
            or not twilio_message_service_sid
        ):
            logger.error("🚨 Missing Twilio credentials. Cannot send SMS.")
            return False

        # ✅ Use the employer's Twilio Account SID & Auth Token
        client = Client(twilio_account_sid, twilio_auth_token)

        message = client.messages.create(
            to=to_number,
            body=body,
            messaging_service_sid=(
                twilio_message_service_sid or settings.TWILIO_MESSAGE_SERVICE_SID
            ),
            shorten_urls=True,
        )
        return f"✅ Message sent. SID: {message.sid}"
    except Exception as e:
        return f"❌ Error sending SMS: {str(e)}"


# This function function is APPROVED for multip tenant.
# It gets its arguments from the task
def send_bulk_sms(
    numbers, body, twilio_account_sid, twilio_auth_token, twilio_notify_sid
):
    """
    Sends bulk SMS using the employer's Twilio Notify credentials.

    Args:
        numbers (list): List of phone numbers.
        body (str): SMS body message.
        twilio_account_sid (str): Employer's Twilio Account SID.
        twilio_auth_token (str): Employer's Twilio Auth Token.
        twilio_notify_sid (str): Employer's Twilio Notify Service SID.

    Returns:
        bool: True if SMS was sent successfully, False otherwise.
    """
    try:
        if not twilio_account_sid or not twilio_auth_token or not twilio_notify_sid:
            logger.error("🚨 Missing Twilio credentials. Cannot send SMS.")
            return False

        valid_numbers = [str(number) for number in numbers if number]

        if not valid_numbers:
            logger.warning("⚠️ No valid phone numbers provided for SMS.")
            return False

        bindings = [
            json.dumps({"binding_type": "sms", "address": number})
            for number in valid_numbers
        ]

        print("=====> To Bindings :>", bindings, "<: =====")

        # ✅ Use the employer's Twilio Account SID & Auth Token
        client = Client(twilio_account_sid, twilio_auth_token)
        notification = client.notify.services(twilio_notify_sid).notifications.create(
            to_binding=bindings,
            body=body,
            delivery_callback_url="https://6c05-2607-fea8-2840-b200-751b-5d20-21ce-303c.ngrok-free.app/webhook/whatsapp/",
        )

        logger.info(f"📢 Bulk SMS sent successfully to {len(valid_numbers)} numbers.")
        return True

    except Exception as e:
        logger.error(f"🚨 Failed to send bulk SMS: {str(e)}")
        return False


def _get_twilio_verify_client():
    return Client(account_sid, auth_token).verify.services(twilio_verify_sid)


def request_verification_token(phone):
    verify = _get_twilio_verify_client()
    try:
        verify.verifications.create(to=phone, channel="sms")
    except TwilioException:
        verify.verifications.create(to=phone, channel="call")


def check_verification_token(phone, token):
    verify = _get_twilio_verify_client()
    try:
        result = verify.verification_checks.create(to=phone, code=token)
    except TwilioException:
        return False
    return result.status == "approved"


# this is the code for a phone call
# 🔒 Hard-coded for now (you’ll replace this later)
TWIML_URL = "https://handler.twilio.com/twiml/EH2936e0f7fd92f59905898f2a3f844ce1"
TWILIO_STATUS_CALLBACK_URL = "https://6364c26b8f51.ngrok-free.app/twilio/voice/status/"


def send_store_phonecall_reminder(employer_id=None, limit=None):
    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    stores = (
        Store.objects.filter(is_active=True)
        .exclude(phone_number__isnull=True)
        .exclude(phone_number__exact="")
    )
    if employer_id:
        stores = stores.filter(employer_id=employer_id)
    if limit:
        stores = stores[: int(limit)]

    total = succeeded = failed = 0

    for s in stores:
        total += 1

        log = StoreCallEvent.objects.create(
            employer=s.employer,
            store=s,
            to_number=str(s.phone_number),
            from_number=settings.TWILIO_FROM,
            campaign_name="POS Reboot Reminder",
            twiml_url=TWIML_URL,
            queued_ok=False,
            status="attempting",
        )

        try:
            call = client.calls.create(
                to=str(s.phone_number),
                from_=settings.TWILIO_FROM,
                url=TWIML_URL,
                method="POST",
                status_callback=TWILIO_STATUS_CALLBACK_URL,
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )

            log.call_sid = call.sid
            log.status = getattr(call, "status", "queued")  # usually queued
            log.queued_ok = True
            log.save(update_fields=["call_sid", "status", "queued_ok"])

            succeeded += 1

        except Exception as e:
            log.status = "error"
            log.error = str(e)
            log.save(update_fields=["status", "error"])

            failed += 1
            logger.exception(
                "Store call failed store_id=%s to=%s", s.id, s.phone_number
            )

    return {"total": total, "succeeded": succeeded, "failed": failed}


def send_docusign_email_with_attachment(to_emails, subject, body, file_path):
    try:
        for to_email in to_emails:
            message = Mail(
                from_email=settings.MAIL_DEFAULT_SENDER,
                to_emails=to_email,
                subject=subject,
                html_content=body,
            )

            with open(file_path, "rb") as file:
                attachment_content = file.read()
                encoded_content = base64.b64encode(attachment_content).decode()

            attachment = Attachment()
            attachment.file_content = FileContent(encoded_content)
            attachment.file_name = FileName(file_path.split("/")[-1])
            attachment.file_type = FileType("application/zip")
            attachment.disposition = Disposition("attachment")
            attachment.content_id = ContentId("Attachment")

            message.attachment = attachment

            response = sg.send(message)
            if response.status_code == 202:
                print(f"Sent file by email to {to_email}", response.status_code)
            else:
                print(
                    f"Failed to send email to {to_email}. Error code:",
                    response.status_code,
                )

    except Exception as e:
        print("Error sending email:", str(e))


#
# APPROVED
#
# This function is APPROVED for multi tenant.
# sends a single email to the user with a pdf of
# the incident file attached.
def create_incident_file_email(
    to_email,
    subject,
    body,
    attachment_buffer=None,
    attachment_filename=None,
    sender_email=None,
):
    try:
        message = Mail(
            from_email=sender_email,
            to_emails=to_email,
            subject=subject,
            html_content=body,
        )
        print(to_email)
        if attachment_buffer and attachment_filename:
            attachment = Attachment()
            attachment.file_content = FileContent(
                base64.b64encode(attachment_buffer.read()).decode()
            )
            attachment.file_name = FileName(attachment_filename)
            attachment.file_type = FileType("application/pdf")
            attachment.disposition = Disposition("attachment")
            attachment.content_id = ContentId("Attachment")

            message.attachment = attachment

        response = sg.send(message)

        if response.status_code != 202:
            print(
                "Failed to send email to", to_email, "Error code:", response.status_code
            )

    except Exception as e:
        error_message = f"An error occurred while sending email to {to_email}: {str(e)}"
        logger.error(error_message)

        if hasattr(e, "response") and e.response is not None:
            response_body = e.response.body
            response_status = e.response.status_code
            logger.error(f"SendGrid response status code: {response_status}")
            logger.error(f"SendGrid response body: {response_body}")


#
# APPROVED
#
# This file is APPROVED for multi tenant use.
# sends new icident pdf to users with the rule
# incident_form_email
def create_incident_file_email_by_rule(
    to_emails,
    subject,
    body,
    attachment_buffer=None,
    attachment_filename=None,
    sender_email=None,
):
    results = {"success": [], "failed": []}
    sender_email = sender_email or settings.MAIL_DEFAULT_SENDER
    try:
        print(to_emails)
        for to_email in to_emails:
            try:
                # Prepare the email message
                message = Mail(
                    from_email=sender_email,
                    to_emails=to_email,
                    subject=subject,
                    html_content=body,
                )

                # Attach the file if provided
                if attachment_buffer and attachment_filename:
                    attachment_buffer.seek(0)  # Ensure the buffer is at the start
                    attachment_content = base64.b64encode(
                        attachment_buffer.read()
                    ).decode()
                    attachment = Attachment(
                        file_content=FileContent(attachment_content),
                        file_name=FileName(attachment_filename),
                        file_type=FileType("application/pdf"),
                        disposition=Disposition("attachment"),
                    )
                    message.attachment = [attachment]
                # Send the email
                response = sg.send(message)

                # Log the result
                if response.status_code == 202:
                    results["success"].append(to_email)
                    logger.info(f"Email successfully sent to {to_email}")
                else:
                    results["failed"].append(to_email)
                    logger.error(
                        f"Failed to send email to {to_email}. Status code: {response.status_code}"
                    )

            except Exception as e:
                # Handle errors for a specific recipient
                results["failed"].append(to_email)
                logger.error(
                    f"An error occurred while sending email to {to_email}: {str(e)}"
                )

    except Exception as e:
        # Handle general errors in the function
        logger.error(f"An error occurred while processing emails: {str(e)}")

    # 🛑 FINAL CHECK
    if results["failed"]:
        failed_list = ", ".join(results["failed"])
        error_message = f"Emails failed to send to: {failed_list}"
        logger.error(error_message)
        raise Exception(error_message)  # 🚨 Raise so Celery knows to fail the task

    # ✅ If no failures, return success normally
    return results


def send_incident_email(
    to_emails, subject, body, attachment_buffer=None, attachment_filename=None
):
    try:
        for to_email in to_emails:
            message = Mail(
                from_email=settings.MAIL_DEFAULT_SENDER,
                to_emails=to_email,
                subject=subject,
                html_content=body,
            )
            print(to_email)
            if attachment_buffer and attachment_filename:
                attachment_buffer.seek(0)
                # Ensure the buffer is at the beginning
                attachment_content = base64.b64encode(attachment_buffer.read()).decode()
                attachment = Attachment()
                attachment.file_content = FileContent(attachment_content)
                attachment.file_name = FileName(attachment_filename)
                attachment.file_type = FileType("application/pdf")
                attachment.disposition = Disposition("attachment")
                attachment.content_id = ContentId("Attachment")

                message.attachment = attachment

            response = sg.send(message)

            if response.status_code != 202:
                print(
                    "Failed to send email to",
                    to_email,
                    "Error code:",
                    response.status_code,
                )

    except Exception as e:
        error_message = f"An error occurred while sending email to {to_email}: {str(e)}"
        logger.error(error_message)

        if hasattr(e, "response") and e.response is not None:
            response_body = e.response.body
            response_status = e.response.status_code
            logger.error(f"SendGrid response status code: {response_status}")
            logger.error(f"SendGrid response body: {response_body}")


def send_whats_app_template(content_sid, from_sid, user_name, to_number):
    # Ensure phone number is in the correct format
    whatsapp_number = f"whatsapp:+{to_number}"
    # Properly format the content variables for the template
    content_vars = json.dumps({"1": user_name})
    # Log the variables to debug or verify; consider reducing logging in production
    print(f"Sending to {whatsapp_number} with name {user_name}")

    try:
        # Create the message
        message = client.messages.create(
            content_sid=content_sid,
            from_=from_sid,
            content_variables=content_vars,
            to=whatsapp_number,
        )
        # print(f"Message sent with SID: {message.sid}")
        return message.sid
    except Exception as e:
        # Handle errors in message sending
        print(f"Failed to send message: {str(e)}")
        return None


def send_whats_app_carwash_sites_template(
    content_sid, from_sid, user_name, to_number, content_vars
):
    # Ensure phone number is in the correct format
    whatsapp_number = f"whatsapp:+{to_number}"
    # Properly format the content variables for the template
    # content_vars = json.dumps({"1": user_name})
    # Log the variables to debug or verify; consider reducing logging in production
    print(f"Sending to {whatsapp_number} with name {user_name}")

    try:
        # Create the message
        message = client.messages.create(
            content_sid=content_sid,
            from_=from_sid,
            content_variables=content_vars,
            to=whatsapp_number,
        )
        # print(f"Message sent with SID: {message.sid}")
        return message.sid
    except Exception as e:
        # Handle errors in message sending
        print(f"Failed to send message: {str(e)}")
        return None


def create_single_csv_email(to_email, subject, body, file_path):
    message = Mail(
        from_email=settings.MAIL_DEFAULT_SENDER,
        to_emails=[to_email],
        subject=subject,
        html_content=body,
    )
    if file_path:
        with open(file_path, "rb") as f:
            data = f.read()
            encoded = base64.b64encode(data).decode()

        attachment = Attachment()
        attachment.file_content = FileContent(encoded)
        attachment.file_name = FileName(os.path.basename(file_path))
        attachment.file_type = FileType("text/csv")
        attachment.disposition = Disposition("attachment")
        attachment.content_id = ContentId("CSV Attachment")
        message.attachment = attachment

    response = sg.send(message)
    if response.status_code == 202:
        return True
    else:
        print("Failed to send email. Status code:", response.status_code)
        return False


def send_whats_app_template_autoreply(content_sid, from_sid, to_number):
    # Ensure phone number is in the correct format
    whatsapp_number = f"whatsapp:+{to_number}"
    # Log the variables to debug or verify; consider reducing logging in production
    print(f"Sending to {whatsapp_number} autoreply")

    try:
        # Create the message
        message = client.messages.create(
            content_sid=content_sid,
            from_=from_sid,
            to=whatsapp_number,
        )
        # print(f"Message sent with SID: {message.sid}")
        return message.sid
    except Exception as e:
        # Handle errors in message sending
        print(f"Failed to send message: {str(e)}")
        return None


def get_inactive_contact_ids():
    """Retrieve contact IDs of inactive users from SendGrid based on email."""
    inactive_emails = CustomUser.objects.filter(is_active=False).values_list(
        "email", flat=True
    )
    inactive_emails = [
        email for email in inactive_emails if email
    ]  # Filter out empty emails
    contact_ids = []

    for email in inactive_emails:
        response = sg.client.marketing.contacts.search.post(
            request_body={"query": f"email LIKE '{email}'"}
        )
        data = json.loads(response.body)  # Convert the response to JSON (dictionary)
        if "result" in data and len(data["result"]) > 0:
            contact_ids.append(data["result"][0]["id"])

    print("Contact IDs for deletion:", contact_ids)  # Debugging print
    return contact_ids


def delete_contacts_by_ids(contact_ids):
    """Delete contacts from SendGrid using their contact IDs."""
    if contact_ids:
        ids_string = ",".join(contact_ids)
        try:
            delete_response = sg.client.marketing.contacts.delete(
                query_params={"ids": ids_string}
            )
            print(
                "Deletion response:", delete_response.status_code, delete_response.body
            )
        except Exception as e:
            print("Deletion error:", e)
    else:
        print("No inactive contact IDs to delete.")


def add_active_contacts(selected_list_id):
    """Add active users to SendGrid with first name, last name, and email."""
    # Retrieve active users with first name, last name, and email
    active_users = CustomUser.objects.filter(is_active=True).values(
        "email", "first_name", "last_name"
    )
    contacts = [
        {
            "email": user["email"],
            "first_name": user["first_name"],
            "last_name": user["last_name"],
        }
        for user in active_users
        if user["email"]  # Ensure no empty emails
    ]
    print(contacts)
    if contacts:
        data = {
            "list_ids": [selected_list_id],  # Specify the target list ID
            "contacts": contacts,
        }
        try:
            add_response = sg.client.marketing.contacts.put(request_body=data)
            print("Addition response:", add_response.status_code, add_response.body)
        except Exception as e:
            print("Addition error:", e)


def sync_contacts_with_sendgrid(selected_list_id):
    """Main function to sync contacts with SendGrid: remove inactive contacts, add active ones."""
    contact_ids = get_inactive_contact_ids()
    delete_contacts_by_ids(contact_ids)
    add_active_contacts(selected_list_id)


def get_all_contact_lists():
    """Retrieve all contact lists and their IDs from SendGrid."""
    try:
        response = sg.client.marketing.lists.get()
        lists = response.to_dict.get("result", [])
        for contact_list in lists:
            print(f"List Name: {contact_list['name']}, List ID: {contact_list['id']}")
        return lists
    except Exception as e:
        print("Error retrieving contact lists:", e)


def collect_attachments(request, max_files=5):
    attachments = []

    uploaded_files = request.FILES.getlist("attachments")
    print("Files :", uploaded_files)
    if len(uploaded_files) > max_files:
        messages.error(request, f"You can upload up to {max_files} files only.")
        return None

    for file in uploaded_files:
        if file.size > 10 * 1024 * 1024:  # 10MB per file
            messages.error(request, f"{file.name} exceeds 10MB limit.")
            return None

        attachments.append(
            {
                "file_name": file.name,
                "file_type": file.content_type,
                "file_content": base64.b64encode(file.read()).decode("utf-8"),
            }
        )

    return attachments


def prepare_recipient_data(user, selected_group, selected_users):
    recipients = []
    employer = user.employer

    # ✅ Handle single group (not a loop)
    if selected_group:
        for u in selected_group.user_set.filter(is_active=True, employer=employer):
            recipients.append(
                {
                    "name": u.get_full_name(),
                    "email": u.email,
                    "status": "Active",
                }
            )

    if selected_users:
        for u in selected_users.order_by("first_name", "last_name"):
            recipients.append(
                {
                    "name": u.get_full_name(),
                    "email": u.email,
                    "status": "Active",
                }
            )

    unique_recipients = {r["email"]: r for r in recipients}
    return list(unique_recipients.values())


def prepare_sms_recipient_data(user, selected_group, selected_users):
    recipients = []
    employer = user.employer

    # Preload SMS opt-out numbers for this employer
    opt_out_user_ids = set(
        SMSOptOut.objects.filter(employer=employer).values_list("user_id", flat=True)
    )

    skipped_no_phone = []
    skipped_opt_out = []

    # We'll keep a map of user_id -> user instance so we can log names later
    user_map = {}

    # 🔹 Collect recipients from selected group
    if selected_group:
        qs = selected_group.user_set.filter(is_active=True, employer=employer)
        for u in qs:
            user_map[u.id] = u
            if not u.phone_number:
                skipped_no_phone.append(u)
                continue
            recipients.append({"id": u.id, "phone": u.phone_number})

    # 🔹 Collect individually selected users
    if selected_users:
        qs = selected_users.order_by("first_name", "last_name")
        for u in qs:
            user_map[u.id] = u
            if not u.phone_number:
                skipped_no_phone.append(u)
                continue
            recipients.append({"id": u.id, "phone": u.phone_number})

    # 🔹 Remove duplicates by phone number
    unique_recipients = {r["phone"]: r for r in recipients}

    # 🔹 Filter out opt-outs
    filtered_recipients = []
    for r in unique_recipients.values():
        if r["id"] in opt_out_user_ids:
            skipped_opt_out.append(r)
        else:
            filtered_recipients.append(r)

    # 🔹 Log skipped users: no phone
    for u in skipped_no_phone:
        logger.info(
            "[SMS] Skipped user %s (%s %s, employer=%s) – NO phone number",
            u.id,
            u.first_name,
            u.last_name,
            getattr(employer, "name", employer.id if employer else "None"),
        )

    # 🔹 Log skipped users: opted out
    for r in skipped_opt_out:
        u = user_map.get(r["id"])
        if u:
            logger.info(
                "[SMS] Skipped user %s (%s %s, employer=%s) – opted out of SMS",
                u.id,
                u.first_name,
                u.last_name,
                getattr(employer, "name", employer.id if employer else "None"),
            )
        else:
            # Fallback if for some reason we don't have the user object
            logger.info(
                "[SMS] Skipped user id=%s – opted out of SMS",
                r["id"],
            )

    # 🔹 Summary line
    logger.info(
        "[SMS] Prepared %d recipients for employer=%s, skipped %d opted-out, %d without phone",
        len(filtered_recipients),
        getattr(employer, "name", employer.id if employer else "None"),
        len(skipped_opt_out),
        len(skipped_no_phone),
    )

    return filtered_recipients


def is_member_of_msg_group(user):
    is_member = user.groups.filter(name="SendSMS").exists()
    if is_member:
        logger.info(f"{user} is a member of 'SendSMS' group.")
    else:
        logger.info(f"{user} is not a member of 'SendSMS' group.")
    return is_member


def is_member_of_email_group(user):
    is_member = user.groups.filter(name="SendEMAIL").exists()
    if is_member:
        logger.info(f"{user} is a member of 'SendEMAIL' group.")
    else:
        logger.info(f"{user} is not a member of 'SendEMAIL' group.")
    return is_member


def is_member_of_docusign_group(user):
    is_member = user.groups.filter(name="SendDOCUSIGN").exists()
    if is_member:
        logger.info(f"{user} is a member of 'SendDOCUSIGN' group.")
    else:
        logger.info(f"{user} is not a member of 'SendDOCUSIGN' group.")
    return is_member


def is_member_of_comms_group(user):
    is_member = user.groups.filter(name="SendCOMMS").exists()
    if is_member:
        logger.info(f"{user} is a member of 'SendCOMMS' group.")
    else:
        logger.info(f"{user} is not a member of 'SendCOMMS' group.")
    return is_member


def is_member_of_email_logs_group(user):
    is_member = user.groups.filter(name="EmailLOGS").exists()
    if is_member:
        logger.info(f"{user} is a member of 'EmailLOGS' group.")
    else:
        logger.info(f"{user} is not a member of 'EmailLOGS' group.")
    return is_member


def is_member_of_sms_logs_group(user):
    is_member = user.groups.filter(name="SmsLOGS").exists()
    if is_member:
        logger.info(f"{user} is a member of 'SmsLOGS' group.")
    else:
        logger.info(f"{user} is not a member of 'SmsLOGS' group.")
    return is_member


def custom_permission_denied(request, message=None):
    return render(request, "incident/403.html", {"message": message}, status=403)


def get_uploaded_urls_from_request(request):
    uploaded_file_urls = request.POST.get("uploaded_file_urls")
    try:
        return json.loads(uploaded_file_urls) if uploaded_file_urls else []
    except json.JSONDecodeError:
        return []


def save_email_draft(user, cleaned_data, attachment_urls, draft_id=None):
    from django.shortcuts import get_object_or_404

    if draft_id:
        draft = get_object_or_404(DraftEmail, id=draft_id, user=user)
    else:
        draft = DraftEmail(user=user)

    draft.mode = cleaned_data.get("email_mode")
    draft.subject = cleaned_data.get("subject", "")
    draft.message = cleaned_data.get("message", "")
    draft.sendgrid_template = cleaned_data.get("sendgrid_id")
    draft.selected_group = cleaned_data.get("selected_group")
    draft.attachment_urls = attachment_urls
    draft.save()

    draft.selected_users.set(cleaned_data.get("selected_users", []))
    draft.save()


def send_quick_email(user, recipients, subject, message, attachment_urls):
    from arl.msg.email_utils import (
        COMPOSE_TEMPLATE_NAME,
        EMAIL_SOURCE_COMPOSE,
        get_generic_sendgrid_template_id,
        resolve_email_subject,
    )
    from .tasks import master_email_send_task

    master_email_send_task.delay(
        recipients=recipients,
        sendgrid_id=get_generic_sendgrid_template_id(),
        employer_id=user.employer.id,
        body=message,
        subject=resolve_email_subject(
            subject=subject, employer=getattr(user, "employer", None)
        ),
        attachment_urls=attachment_urls,
        template_name=COMPOSE_TEMPLATE_NAME,
        source=EMAIL_SOURCE_COMPOSE,
    )

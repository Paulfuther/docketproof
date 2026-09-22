from __future__ import absolute_import, unicode_literals

import json
from datetime import datetime

from celery.utils.log import get_task_logger
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ObjectDoesNotExist
from django.utils.crypto import get_random_string

from arl.celery import app
from arl.msg.helpers import create_master_email
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, Employer, NewHireInvite

logger = get_task_logger(__name__)


# Works
@app.task(name="send_newhire_template_email")
def send_newhire_template_email_task(
    to_email, sendgrid_id, template_data, attachments=None
):
    """
    Celery task to send an email using SendGrid.
    """
    try:
        # Ensure template_data is a dictionary
        if not isinstance(template_data, dict):
            raise ValueError(
                f"❌ Expected dictionary for template_data, got {type(template_data)}"
            )

        senior_contact_name = template_data.get("senior_contact_name", "HR Team")

        print(f"📧 Sending New Hire Email to: {to_email}")
        print(f"📜 Senior Contact Name: {senior_contact_name}")
        print(f"📜 Email Template Data: {template_data}")

        # Call the helper function, passing template_data
        create_master_email(
            to_email=to_email,
            sendgrid_id=sendgrid_id,
            template_data=template_data,  # ✅ Pass template data
            attachments=attachments,
        )
    except Exception as e:
        print(f"Error in send_master_email_task: {e}")
    return False


# confirm this works.
@app.task(name="create_hr_newhire_email")
def create_newhire_data_email(email_data):
    try:
        logger.info("📩 New hire data email task started")
        logger.debug(f"Raw email data: {email_data}")

        # Handle stringified input
        if isinstance(email_data, str):
            email_data = json.loads(email_data)

        employer_id = email_data.get("employer_id")
        if not employer_id:
            raise ValueError("Missing employer_id in email data")

        employer = Employer.objects.filter(id=employer_id).first()
        if not employer:
            raise ValueError(f"No employer found with ID {employer_id}")

        email_data["company_name"] = employer.name

        # ✅ Get active users in the correct employer and group
        hr_users = CustomUser.objects.filter(
            is_active=True, employer_id=employer_id, groups__name="new_hire_data_email"
        )

        to_emails = [user.email for user in hr_users]
        # 🔁 Fallback to employer email if none found
        if not to_emails:
            if employer.email:
                logger.warning(
                    f"⚠️ No HR users found. Falling back to employer email: {employer.email}"
                )
                to_emails = [employer.email]
            else:
                error_msg = f"❌ No HR or fallback email for employer ID {employer_id}"
                logger.error(error_msg)
                return error_msg

        # ✅ Send via Master Email Function
        create_master_email(
            to_email=to_emails,
            sendgrid_id="d-d0806dff1e62449d9ba8cfcb481accaa",
            template_data=email_data,
        )

        logger.info(
            f"New hire email created successfully for {email_data.get('email')}"
        )
        return (
            f"New hire email for {email_data.get('firstname')} - "
            f"{email_data.get('lastname')} created successfully"
        )

    except Exception as e:
        logger.error(
            f"Error creating new hire email for {email_data.get('email')}: {str(e)}"
        )
        return f"Error creating new hire email: {str(e)}"


@app.task(name="save_user_to_db")
def save_user_to_db(**kwargs):
    try:
        # Create a new CustomUser object and save it to the database
        # Access the serialized data from kwargs
        employer_pk = kwargs.get("employer")
        dob_isoformat = kwargs.get("dob")
        user = CustomUser.objects.get(pk=kwargs["user_id"])
        user.employer = Employer.objects.get(pk=employer_pk)
        user.dob = datetime.strptime(dob_isoformat, "%Y-%m-%d").date()
        # ✅ Convert and assign SIN Expiration Date if it exists
        sin_expiration_date = kwargs.get("sin_expiration_date")
        if sin_expiration_date:
            user.sin_expiration_date = datetime.fromisoformat(
                sin_expiration_date
            ).date()

        # ✅ Convert and assign Work Permit Expiration Date if it exists
        work_permit_expiration_date = kwargs.get("work_permit_expiration_date")
        if work_permit_expiration_date:
            user.work_permit_expiration_date = datetime.fromisoformat(
                work_permit_expiration_date
            ).date()
        # Deserialize the data if needed
        # Convert employer_pk, manager_dropdown_pk, and dob_isoformat back
        # to their original types
        # Perform the necessary processing with the data
        # Update UserManager with user and manager, etc.
        # Example:
        user.save()
    except Exception as e:
        # Handle any exceptions that may occur during database save
        print(f"An error occurred during database save: {e}")


@app.task(name="send_payment_email")
def send_payment_email_task(to_email, payment_link, employer_name):
    """Send an email with the Stripe payment link using the master email function."""
    sendgrid_id = (
        "d-e31a2d72f8b145de98ba8d9fa267bc04"  # 🔹 Use your SendGrid template ID
    )
    try:
        # ✅ Use the employer name passed from the approval process
        template_data = {
            "payment_link": payment_link,
            "name": employer_name,  # ✅ Ensure we use the correct employer name
            "subject": "Complete Your Registration - Payment Required",
            "body": f"'{payment_link}'",
        }

        # ✅ Call the master email function
        return create_master_email(to_email, sendgrid_id, template_data)

    except Exception as e:
        print(f"❌ Error sending payment email: {str(e)}")
        return False


@app.task(name="notify_hr_employee_left")
def notify_hr_about_departure(departed_name, departed_email, employer_id):
    try:
        # Try to deactivate the user if found
        user = CustomUser.objects.get(
            email__iexact=departed_email, employer_id=employer_id
        )
        user.is_active = False
        user.save()
        status = "Marked inactive"
    except ObjectDoesNotExist:
        status = "Could not be found"
    except Exception as e:
        print(f"Error updating user: {e}")
        status = "Error while deactivating"

    try:
        employer = Employer.objects.get(id=employer_id)
        employer_name = employer.name
    except Employer.DoesNotExist:
        print("Employer not found.")

    try:
        # Get verified HR email and SendGrid ID
        tenant_keys = TenantApiKeys.objects.get(employer_id=employer_id)
        verified_sender = tenant_keys.verified_sender_email

    except TenantApiKeys.DoesNotExist:
        print("No tenant API keys found for employer.")
        return
    except Exception as e:
        print(f"Error retrieving HR email: {e}")
        return

    try:
        # Get all active users at this employer in HR group (CSR)
        hr_group = Group.objects.get(name="HR")
        hr_users = CustomUser.objects.filter(
            employer_id=employer_id, is_active=True, groups=hr_group
        )

        if not hr_users.exists():
            print("No active HR users found for this employer.")
            return

        for hr_user in hr_users:
            try:
                create_master_email(
                    to_email=hr_user.email,
                    sendgrid_id="d-06a7434fdfb745c893eff524c9e1f026",
                    template_data={
                        "departed_name": departed_name,
                        "departed_email": departed_email,
                        "company_name": employer_name,
                        "status": status,
                    },
                    verified_sender=verified_sender,
                )
            except Exception as e:
                print(f"Failed to notify HR {hr_user.email}: {e}")
    except Exception as e:
        print(f"Error finding HR users: {e}")


@app.task(name="send_new_hire_invite_task")
def send_new_hire_invite_task(
    new_hire_email, new_hire_name, role, start_date, employer_id
):
    try:
        employer = Employer.objects.get(id=employer_id)
        tenant_api_key = TenantApiKeys.objects.filter(employer=employer).first()
        verified_sender = (
            tenant_api_key.verified_sender_email
            if tenant_api_key
            else settings.MAIL_DEFAULT_SENDER
        )

        if not verified_sender:
            print(f"❌ Employer {employer.name} does not have a verified sender email.")
            return False

        invite, created = NewHireInvite.objects.get_or_create(
            employer=employer,
            email=new_hire_email,
            defaults={
                "name": new_hire_name,
                "role": role,
                "token": get_random_string(64),
                "used": False,
            },
        )

        if not created:
            if not invite.token:
                invite.token = get_random_string(64)
                invite.save(update_fields=["token"])
            invite.refresh_expiry()

        invite_link = invite.get_invite_link()

        template_data = {
            "name": new_hire_name,
            "senior_contact_name": employer.senior_contact_name or "HR Team",
            "company_name": employer.name,
            "role": role,
            "start_date": start_date,
            "invite_link": invite_link,
            "sender_contact_name": employer.senior_contact_name or "HR Team",
        }

        return create_master_email(
            to_email=new_hire_email,
            sendgrid_id="d-88bef48e049c477b83f28764b842c7a2",
            template_data=template_data,
            verified_sender=verified_sender,
        )

    except Exception as e:
        print(f"🚨 Error in new hire invite task: {e}")
        return False

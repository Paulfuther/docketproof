from __future__ import absolute_import, unicode_literals

import base64
import logging
from io import BytesIO

import pdfkit
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

from arl.celery import app
from arl.dbox.helpers import upload_to_dropbox
from arl.helpers import get_s3_images_for_incident, upload_to_linode_object_storage
from arl.msg.helpers import (
    create_incident_file_email,
    create_master_email,
    send_incident_email,
)
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, Employer, ExternalRecipient, Store

from .helpers import create_pdf, create_restricted_pdf, create_significant_security_pdf
from .models import Incident

logger = logging.getLogger(__name__)


# this is needed for the correct sender email for all
# employers. Each empoloyer has their own.
# Else, it defaults to the default.
def get_employer_sender_email(employer_id):
    """Fetch the sender email for a given employer."""
    try:
        employer = Employer.objects.get(id=employer_id)
        tenant_api_key = TenantApiKeys.objects.filter(
            employer=employer, is_active=True
        ).first()

        return (
            tenant_api_key.sender_email
            if tenant_api_key
            else settings.MAIL_DEFAULT_SENDER
        )
    except Employer.DoesNotExist:
        logger.warning(f"⚠️ Employer ID {employer_id} not found. Using default sender.")
        return settings.MAIL_DEFAULT_SENDER


@app.task(name="save_inciddent_file")
def save_incident_file(**kwargs):
    try:
        # Extract form data
        store_id = kwargs.pop("store", None)
        user_employer_id = kwargs.pop("user_employer", None)

        # Get the Store instance using the store_id
        store_instance = (
            Store.objects.get(pk=store_id) if store_id is not None else None
        )
        user_employer_instance = (
            Employer.objects.get(pk=user_employer_id)
            if user_employer_id is not None
            else None
        )

        # Set the Store instance back to the kwargs
        kwargs["store"] = store_instance
        kwargs["user_employer"] = user_employer_instance

        # Save the form data to the database
        incident = Incident.objects.create(**kwargs)

        return {
            "incident_store": incident.id,
            "Incident_brief": incident.brief_description,
            "message": "Incident Saved",
        }
    except Exception as e:
        logger.error(f"Error saving incident: {e}")
        return {"error": str(e)}

#=================================================================
#            Here is where we generate two incident reports
#            And put them into a package for emails.
#            The first is the original pdf
#            The second is the restricted pdf
#
# This task is used when a site incident form is first created.
# There are four tasks to be called.
# generate_pdf_task_upload_to_linode_task, upload_to_dropbox_task
# and send_email_to_group_task
@app.task(name="create_incident_pdf")
def generate_pdf_task(incident_id):
    """
    This task is used to create a pdf of a site incident form.
    """
    logger.info(f"[PDF Task] Starting PDF generation for Incident ID: {incident_id}")
    try:
        pdf_result = create_pdf(incident_id)

        if pdf_result["status"] != "success":
            raise Exception(f"Failed to generate PDF: {pdf_result['message']}")

        # Return the PDF details for further tasks
        return {
            "pdf_filename": pdf_result["pdf_filename"],
            "pdf_buffer": pdf_result["pdf_buffer"].getvalue(),
            "incident_id": incident_id,
        }
    except Exception as e:
        raise Exception(f"Error in generate_pdf_task: {e}")


# This task generates the pdf for the new version of the incident investigation form.
# This form was put in practice on our about early 2026. 
@app.task(name="create_restricted_incident_pdf")
def generate_restricted_pdf_task(incident_id):
    """
    Generate the Incident Investigation Report PDF.
    """
    logger.info(
        "[Restricted PDF Task] Starting generation for Incident ID: %s",
        incident_id,
    )

    try:
        pdf_result = create_restricted_pdf(incident_id)

        if pdf_result["status"] != "success":
            raise Exception(
                f"Failed to generate restricted PDF: "
                f"{pdf_result['message']}"
            )

        return {
            "pdf_filename": pdf_result["pdf_filename"],
            "pdf_buffer": pdf_result["pdf_buffer"].getvalue(),
            "incident_id": incident_id,
        }

    except Exception as exc:
        logger.exception(
            "[Restricted PDF Task] Error for Incident ID: %s",
            incident_id,
        )
        raise Exception(
            f"Error in generate_restricted_pdf_task: {exc}"
        )

@app.task(name="email_significant_security_incident_pdf")
def generate_significant_security_pdf_email_task(
    incident_id,
    user_email,
):
    """
    Generate and email the Significant Security Incident Report
    to the requesting user.
    """
    try:
        incident = (
            Incident.objects
            .select_related("store", "user_employer")
            .get(pk=incident_id)
        )

        employer = incident.user_employer

        sender_email = settings.MAIL_DEFAULT_SENDER

        if employer:
            tenant_api_key = (
                TenantApiKeys.objects
                .filter(employer=employer)
                .first()
            )

            if (
                tenant_api_key
                and tenant_api_key.sender_email
            ):
                sender_email = tenant_api_key.sender_email

        pdf_result = create_significant_security_pdf(
            incident_id
        )

        if pdf_result.get("status") != "success":
            raise ValueError(
                pdf_result.get(
                    "message",
                    "Significant Security PDF generation failed.",
                )
            )

        pdf_buffer = pdf_result["pdf_buffer"]
        pdf_buffer.seek(0)

        pdf_filename = pdf_result["pdf_filename"]

        subject = (
            f"Significant Security Incident Report – "
            f"Store {incident.store.number}"
        )

        body = (
            "Attached is the requested Significant Security "
            "Incident Report."
        )

        create_incident_file_email(
            user_email,
            subject,
            body,
            pdf_buffer,
            pdf_filename,
            sender_email=sender_email,
        )

        logger.info(
            "Significant Security Incident Report emailed "
            "for Incident ID %s to %s",
            incident_id,
            user_email,
        )

        return {
            "status": "success",
            "message": (
                f"{pdf_filename} emailed to {user_email}."
            ),
        }

    except Incident.DoesNotExist:
        message = (
            f"Incident with ID {incident_id} does not exist."
        )

        logger.error(message)

        return {
            "status": "error",
            "message": message,
        }

    except Exception as exc:
        logger.exception(
            "Error emailing Significant Security Incident "
            "Report for Incident ID %s",
            incident_id,
        )

        return {
            "status": "error",
            "message": str(exc),
        }

@app.task(name="generate_incident_report_package")
def generate_incident_report_package_task(incident_id):
    """
    Generate both reports for a newly created Incident:

    1. Insurance Incident Report
    2. Incident Investigation Report

    Both existing PDF helpers remain responsible for rendering their
    respective HTML templates.
    """
    logger.info(
        "[Incident Report Package] Starting generation for Incident ID: %s",
        incident_id,
    )

    try:
        # Original site incident report / Insurance Incident Report
        insurance_result = create_pdf(incident_id)

        if insurance_result.get("status") != "success":
            raise RuntimeError(
                "Insurance Incident Report generation failed: "
                f"{insurance_result.get('message', 'Unknown error')}"
            )

        # Restricted / Incident Investigation Report
        investigation_result = create_restricted_pdf(incident_id)

        if investigation_result.get("status") != "success":
            raise RuntimeError(
                "Incident Investigation Report generation failed: "
                f"{investigation_result.get('message', 'Unknown error')}"
            )

        insurance_buffer = insurance_result["pdf_buffer"]
        investigation_buffer = investigation_result["pdf_buffer"]

        insurance_buffer.seek(0)
        investigation_buffer.seek(0)

        report_package = {
            "incident_id": incident_id,
            "insurance_pdf": {
                "pdf_filename": insurance_result["pdf_filename"],
                "pdf_buffer": insurance_buffer.getvalue(),
            },
            "investigation_pdf": {
                "pdf_filename": investigation_result["pdf_filename"],
                "pdf_buffer": investigation_buffer.getvalue(),
            },
        }

        logger.info(
            "[Incident Report Package] Both reports generated for "
            "Incident ID: %s",
            incident_id,
        )

        return report_package

    except Exception as exc:
        logger.exception(
            "[Incident Report Package] Generation failed for "
            "Incident ID: %s",
            incident_id,
        )

        # Raising causes the Celery chain to stop. The email and Dropbox
        # tasks will not run with an incomplete report package.
        raise RuntimeError(
            f"Unable to generate reports for Incident {incident_id}: {exc}"
        ) from exc


# APPROVED for multi tenant
@app.task(name="email_incident_pdf_to_group")
def send_email_to_group_task(data, group_name, employer_id):
    """
    Email incident report PDFs to active users in the requested tenant group.

    Supports both payload formats:

    Legacy single-PDF payload:
        {
            "incident_id": 123,
            "pdf_filename": "...",
            "pdf_buffer": b"...",
        }

    New report-package payload:
        {
            "incident_id": 123,
            "insurance_pdf": {
                "pdf_filename": "...",
                "pdf_buffer": b"...",
            },
            "investigation_pdf": {
                "pdf_filename": "...",
                "pdf_buffer": b"...",
            },
        }
    """
    logger.info(
        "[Incident Email Task] Starting email task for group '%s' "
        "and employer ID %s",
        group_name,
        employer_id,
    )

    try:
        # Select the SendGrid template.
        if group_name == "incident_update_email":
            sendgrid_id = "d-d732a2877dc14881aac8f2f637c0fa71"
        else:
            sendgrid_id = "d-d24563b3a2c14e9ebb1ebae50b6097f1"

        logger.info(
            "[Incident Email Task] Using template ID: %s",
            sendgrid_id,
        )

        # Resolve recipients within the correct tenant.
        to_emails = list(
            CustomUser.objects.filter(
                is_active=True,
                groups__name=group_name,
                employer_id=employer_id,
            )
            .exclude(email="")
            .values_list("email", flat=True)
            .distinct()
        )

        if not to_emails:
            raise ValueError(
                f"No active users found in group '{group_name}' "
                f"for employer ID {employer_id}."
            )

        logger.info(
            "[Incident Email Task] Found %s recipients in group '%s'",
            len(to_emails),
            group_name,
        )

        # Resolve the verified tenant sender.
        tenant_api_key = TenantApiKeys.objects.filter(
            employer_id=employer_id
        ).first()

        sender_email = settings.MAIL_DEFAULT_SENDER

        if (
            tenant_api_key
            and tenant_api_key.verified_sender_email
        ):
            sender_email = tenant_api_key.verified_sender_email

        # Build attachments from either the new package or old payload.
        attachments = []

        if (
            "insurance_pdf" in data
            and "investigation_pdf" in data
        ):
            logger.info(
                "[Incident Email Task] Processing two-report package "
                "for Incident ID: %s",
                data.get("incident_id"),
            )

            report_items = [
                data["insurance_pdf"],
                data["investigation_pdf"],
            ]

        elif "pdf_buffer" in data and "pdf_filename" in data:
            logger.info(
                "[Incident Email Task] Processing legacy single-PDF "
                "payload for Incident ID: %s",
                data.get("incident_id"),
            )

            report_items = [data]

        else:
            raise ValueError(
                "Unsupported incident PDF payload. "
                f"Received keys: {list(data.keys())}"
            )

        for report in report_items:
            pdf_filename = report["pdf_filename"]
            pdf_content = report["pdf_buffer"]

            # Celery should pass bytes here, but support BytesIO as well.
            if isinstance(pdf_content, BytesIO):
                pdf_content.seek(0)
                pdf_content = pdf_content.getvalue()

            if not isinstance(pdf_content, bytes):
                raise TypeError(
                    f"PDF content for '{pdf_filename}' must be bytes. "
                    f"Received {type(pdf_content).__name__}."
                )

            attachments.append(
                {
                    "content": base64.b64encode(
                        pdf_content
                    ).decode("utf-8"),
                    "filename": pdf_filename,
                    "file_type": "application/pdf",
                }
            )

            logger.info(
                "[Incident Email Task] Prepared attachment: %s (%s KB)",
                pdf_filename,
                round(len(pdf_content) / 1024, 1),
            )

        # Template personalization.
        first_email = to_emails[0]

        user = (
            CustomUser.objects
            .select_related("employer")
            .filter(
                email=first_email,
                employer_id=employer_id,
            )
            .first()
        )

        if user:
            full_name = user.get_full_name() or "Valued User"
            company_name = (
                user.employer.name
                if user.employer
                else "Unknown Company"
            )
        else:
            full_name = "Valued User"
            company_name = "Unknown Company"

        template_data = {
            "name": full_name,
            "company_name": company_name,
        }

        logger.info(
            "[Incident Email Task] Sending %s attachment(s) to "
            "%s recipient(s).",
            len(attachments),
            len(to_emails),
        )

        create_master_email(
            to_email=to_emails,
            sendgrid_id=sendgrid_id,
            template_data=template_data,
            attachments=attachments,
            verified_sender=sender_email,
        )

        logger.info(
            "[Incident Email Task] Email successfully sent to group '%s'",
            group_name,
        )

        return {
            "status": "success",
            "message": (
                f"Incident email with {len(attachments)} attachment(s) "
                f"sent to group '{group_name}'."
            ),
            "incident_id": data.get("incident_id"),
        }

    except Exception as exc:
        logger.exception(
            "[Incident Email Task] Error sending incident email "
            "to group '%s'",
            group_name,
        )

        return {
            "status": "error",
            "message": str(exc),
            "incident_id": (
                data.get("incident_id")
                if isinstance(data, dict)
                else None
            ),
        }


@app.task(name="process_new_incident_reports")
def process_new_incident_reports_task(incident_id):
    """
    Process a newly created Incident.

    Generates:
    1. Insurance Incident Report
    2. Incident Investigation Report

    Then:
    - Emails both PDFs in one email.
    - Uploads the original Insurance Incident Report to Dropbox.
    """
    logger.info(
        "[Incident Workflow] Starting for Incident ID: %s",
        incident_id,
    )

    try:
        incident = Incident.objects.get(pk=incident_id)

        employer_id = incident.user_employer_id

        if not employer_id:
            raise ValueError(
                f"Incident {incident_id} has no employer."
            )

        # -------------------------------------------------------------
        # Generate the original Insurance Incident Report
        # -------------------------------------------------------------
        insurance_result = create_pdf(incident_id)

        if insurance_result.get("status") != "success":
            raise RuntimeError(
                "Insurance Incident Report generation failed: "
                f"{insurance_result.get('message', 'Unknown error')}"
            )

        # -------------------------------------------------------------
        # Generate the Incident Investigation Report
        # -------------------------------------------------------------
        investigation_result = create_restricted_pdf(incident_id)

        if investigation_result.get("status") != "success":
            raise RuntimeError(
                "Incident Investigation Report generation failed: "
                f"{investigation_result.get('message', 'Unknown error')}"
            )

        insurance_buffer = insurance_result["pdf_buffer"]
        investigation_buffer = investigation_result["pdf_buffer"]

        insurance_buffer.seek(0)
        investigation_buffer.seek(0)

        # -------------------------------------------------------------
        # Build the two-report package expected by
        # send_email_to_group_task()
        # -------------------------------------------------------------
        report_package = {
            "incident_id": incident_id,
            "insurance_pdf": {
                "pdf_filename": insurance_result["pdf_filename"],
                "pdf_buffer": insurance_buffer.getvalue(),
            },
            "investigation_pdf": {
                "pdf_filename": investigation_result["pdf_filename"],
                "pdf_buffer": investigation_buffer.getvalue(),
            },
        }

        # -------------------------------------------------------------
        # Email both reports in one email
        #
        # Call the task directly inside this worker. Do not use .delay(),
        # because we do not want to serialize the PDF bytes through Celery.
        # -------------------------------------------------------------
        email_result = send_email_to_group_task(
            report_package,
            group_name="incident_form_email",
            employer_id=employer_id,
        )

        if email_result.get("status") != "success":
            raise RuntimeError(
                "Incident report email failed: "
                f"{email_result.get('message', 'Unknown error')}"
            )

        # -------------------------------------------------------------
        # Upload only the original Insurance Incident Report to Dropbox.
        #
        # The existing Dropbox task expects the old single-PDF payload.
        # -------------------------------------------------------------
        dropbox_payload = {
            "incident_id": incident_id,
            "pdf_filename": insurance_result["pdf_filename"],
            "pdf_buffer": insurance_buffer.getvalue(),
        }

        dropbox_result = upload_file_to_dropbox_task(
            dropbox_payload
        )

        if (
            isinstance(dropbox_result, dict)
            and dropbox_result.get("status") == "error"
        ):
            raise RuntimeError(
                "Dropbox upload failed: "
                f"{dropbox_result.get('message', 'Unknown error')}"
            )

        # -------------------------------------------------------------
        # Mark the automatic delivery complete
        # -------------------------------------------------------------
        Incident.objects.filter(pk=incident_id).update(
            sent=True,
            sent_at=timezone.now(),
            queued_for_sending=False,
        )

        logger.info(
            "[Incident Workflow] Completed for Incident ID: %s",
            incident_id,
        )

        return {
            "status": "success",
            "incident_id": incident_id,
            "message": (
                "Both reports emailed and the Insurance Incident "
                "Report uploaded to Dropbox."
            ),
        }

    except Incident.DoesNotExist:
        message = f"Incident {incident_id} does not exist."

        logger.error(
            "[Incident Workflow] %s",
            message,
        )

        return {
            "status": "error",
            "incident_id": incident_id,
            "message": message,
        }

    except Exception as exc:
        logger.exception(
            "[Incident Workflow] Failed for Incident ID: %s",
            incident_id,
        )

        Incident.objects.filter(pk=incident_id).update(
            queued_for_sending=False
        )

        return {
            "status": "error",
            "incident_id": incident_id,
            "message": str(exc),
        }

#====================================================================


@app.task(name="upload_incident_pdf_to_linode")
def upload_to_linode_task(data):
    """
    Task to upload the generated PDF to Linode Object Storage.
    """
    try:
        pdf_data = data["pdf_buffer"]
        pdf_filename = data["pdf_filename"]
        incident_id = data["incident_id"]
        # Convert bytes back to a BytesIO object
        pdf_buffer = BytesIO(pdf_data)
        # Upload to Linode Object Storage
        user_employer = Incident.objects.get(pk=incident_id).user_employer
        object_key = f"SITEINCIDENT/{user_employer}/INCIDENTPDF/{pdf_filename}"
        upload_to_linode_object_storage(pdf_buffer, object_key)
        # Update and return the data dictionary
        data["object_key"] = object_key
        return data
    except Exception as e:
        raise Exception(f"Error in upload_to_storage_task: {e}")


@app.task(name="upload_incident_pdf_to_dropbox")
def upload_file_to_dropbox_task(data):
    """
    Task to upload a file to Dropbox using the provided file content and path.
    """
    try:
        # Extract data
        pdf_data = data["pdf_buffer"]
        pdf_filename = data["pdf_filename"]

        # Construct the Dropbox file path
        dropbox_file_path = f"/SITEINCIDENTS/{pdf_filename}"
        employer = None
        incident_id = data.get("incident_id")
        if incident_id:
            try:
                employer = Incident.objects.get(pk=incident_id).user_employer
            except Incident.DoesNotExist:
                employer = None

        # Call the helper function to upload the file
        success, message = upload_to_dropbox(
            pdf_data,
            dropbox_file_path,
            employer=employer,
            write_mode="add",
        )

        if not success:
            # Check for specific error cases
            if "insufficient_space" in message:
                logger.error("Dropbox upload failed: Dropbox is full.")
                return {
                    "status": "failure",
                    "message": "Dropbox is full. Please free up some space.",
                }
            else:
                raise Exception(f"Dropbox upload failed: {message}")

        # Return updated data for the next task
        data["dropbox_path"] = dropbox_file_path
        return data

    except KeyError as e:
        error_message = (
            f"Missing key in data passed to upload_file_to_dropbox_task: {e}"
        )
        logger.error(error_message)
        raise Exception(error_message)

    except Exception as e:
        # Log and raise other unexpected errors
        error_message = f"Error in upload_file_to_dropbox_task: {e}"
        logger.error(error_message)
        raise Exception(error_message)



# APPROVED for multi tenant.
# This task creates a pdf of the select Incident Form
# Then emails the form to the user.
# This is called from the list of incidents
# when you click the pdf button
@app.task(name="email_updated_incident_pdf")
def generate_pdf_email_to_user_task(incident_id, user_email):
    try:
        # Fetch incident data based on incident_id
        try:
            incident = Incident.objects.get(pk=incident_id)
        except ObjectDoesNotExist:
            raise ValueError("Incident with ID {} does not exist.".format(incident_id))

        # get images, if there are any, from the s3 bucket
        images = get_s3_images_for_incident(
            incident.image_folder, incident.user_employer
        )
        context = {"incident": incident, "images": images}
        html_content = render_to_string("incident/incident_form_pdf.html", context)
        #  Generate the PDF using pdfkit
        options = {
            "enable-local-file-access": None,
            "--keep-relative-links": "",
            "encoding": "UTF-8",
        }
        # create the pdf
        pdf = pdfkit.from_string(html_content, False, options)
        #  Create a BytesIO object to store the PDF content
        pdf_buffer = BytesIO(pdf)
        # Create a unique file name for the PDF
        store_number = incident.store.number
        # Replace with your actual attribute name
        brief_description = incident.brief_description
        # Create a unique file name for the PDF
        # using store number and brief description
        pdf_filename = f"{store_number}_{slugify(brief_description)}_report.pdf"

        # ✅ Prepare the attachment for SendGrid
        attachments = [
            {
                "content": base64.b64encode(pdf_buffer.getvalue()).decode(),
                "filename": pdf_filename,
                "file_type": "application/pdf",
            }
        ]

        # ✅ Fetch user and dynamic template data
        try:
            user = CustomUser.objects.get(email=user_email)
            full_name = user.get_full_name()
            company_name = user.employer.name if user.employer else "Unknown Company"
        except CustomUser.DoesNotExist:
            full_name = "Valued User"
            company_name = "Unknown Company"

        template_data = {
            "name": full_name,
            "company_name": company_name,
        }

        # ✅ Get verified sender if needed
        try:
            tenant_api_key = TenantApiKeys.objects.get(employer=user.employer)
            sender_email = tenant_api_key.verified_sender_email
        except TenantApiKeys.DoesNotExist:
            sender_email = settings.MAIL_DEFAULT_SENDER

        # ✅ Call master email helper
        create_master_email(
            to_email=[user_email],
            sendgrid_id="d-f76eb0684e444e29b0a3cfc621cefd6a",
            template_data=template_data,
            attachments=attachments,
            verified_sender=sender_email,
        )

        return {
            "status": "success",
            "message": f"Incident {pdf_filename} emailed to {user_email}",
        }

    except Exception as e:
        logger.error(f"Error in generate_pdf_email_to_user_task: {str(e)}")
        return {"status": "error", "message": str(e)}


# this task emails a restricted pdf to a user as long as
# the belong to the correct group
@app.task(name="email_restricted_incident_pdf")
def generate_restricted_incident_pdf_email_task(incident_id, user_email):
    try:
        # Fetch incident data based on incident_id
        try:
            incident = Incident.objects.get(pk=incident_id)
        except ObjectDoesNotExist:
            raise ValueError("Incident with ID {} does not exist.".format(incident_id))
        employer = (
            incident.user_employer if hasattr(incident, "user_employer") else None
        )

        # ✅ Fetch the sender email from Tenant API Keys
        sender_email = settings.MAIL_DEFAULT_SENDER  # Default sender
        if employer:
            tenant_api_key = TenantApiKeys.objects.filter(employer=employer).first()
            sender_email = (
                tenant_api_key.sender_email if tenant_api_key else sender_email
            )

        # ✅ Render HTML for PDF
        context = {"incident": incident}
        html_content = render_to_string(
            "incident/restricted_incident_form_pdf.html", context
        )
        #  Generate the PDF using pdfkit
        options = {
            "enable-local-file-access": None,
            "--keep-relative-links": "",
            "encoding": "UTF-8",
        }
        # create the pdf
        pdf = pdfkit.from_string(html_content, False, options)
        #  Create a BytesIO object to store the PDF content
        pdf_buffer = BytesIO(pdf)
        # Create a unique file name for the PDF
        store_number = incident.store.number
        # Replace with your actual attribute name
        brief_description = incident.brief_description
        # Create a unique file name for the PDF
        # using store number and brief description
        pdf_filename = f"{store_number}_{slugify(brief_description)}_report.pdf"

        # Close the BytesIO buffer to free up resources
        # Then email to the current user

        subject = f"Your Restricted Incident Report {pdf_filename}"
        body = "Thank you for using our services. "
        "Attached is your incident report."
        # attachment_data = pdf_buffer.getvalue()

        # Call the create_single_email function with
        # user_email and other details
        create_incident_file_email(
            user_email,
            subject,
            body,
            pdf_buffer,
            pdf_filename,
            sender_email=sender_email,
        )
        # create_single_email(user_email, subject, body, pdf_buffer)

        return {
            "status": "success",
            "message": f"Incident {pdf_filename} emailed to {user_email}",
        }
    except Exception as e:
        logger.error("Error in generate_pdf_task: {}".format(e))
        return {"status": "error", "message": str(e)}


# this task sends incident forms to those on the external sedners list
@app.task(name="generate_and_send_pdf_task")
def generate_and_send_pdf_task(incident_id):
    try:
        # Fetch the incident
        incident = Incident.objects.get(pk=incident_id)

        # Generate the PDF
        pdf_data = create_restricted_pdf(incident_id)  # helper returns "data"

        if pdf_data.get("status") == "error":
            raise ValueError(pdf_data.get("message"))

        pdf_buffer = pdf_data.get("pdf_buffer")
        pdf_filename = pdf_data.get("pdf_filename")

        # Define email details
        # Fetch active external recipients
        to_emails = ExternalRecipient.objects.filter(is_active=True).values_list(
            "email", flat=True
        )
        subject = f"Incident Report: {incident.brief_description}"
        body = (
            f"<p>An incident occurred on {incident.eventdate} "
            f"at {incident.store}.</p>"
            f"<p>Details: {incident.brief_description}</p>"
            f"<p>Please find the incident report attached.</p>"
        )

        # Send the email
        send_incident_email(
            to_emails=to_emails,
            subject=subject,
            body=body,
            attachment_buffer=pdf_buffer,
            attachment_filename=pdf_filename,
        )

        # Mark the incident as sent
        incident.sent = True
        incident.sent_at = timezone.now()
        incident.save(update_fields=["sent", "sent_at"])

        return f"Incident {incident_id} processed and sent successfully."

    except Incident.DoesNotExist:
        logger.error(f"Incident with ID {incident_id} does not exist.")
        return f"Incident {incident_id} does not exist."

    except Exception as e:
        logger.error(f"Error processing incident {incident_id}: {str(e)}")
        return f"Error processing incident {incident_id}: {str(e)}"

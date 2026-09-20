from __future__ import absolute_import, unicode_literals

import base64
import logging
import os
import time
import uuid
from datetime import datetime
from io import BytesIO

import pdfkit
import requests
from arl.celery import app
from arl.dbox.helpers import master_upload_file_to_dropbox
from arl.helpers import (
    get_s3_images_for_salt_log,
    get_signed_url_for_key,
    upload_to_linode_object_storage,
)
from arl.msg.helpers import create_master_email
from arl.quiz.dropbox_paths import build_checklist_dropbox_path
from arl.quiz.models import Checklist, SaltLog
from arl.quiz.store_address import store_report_context
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, Employer, Store
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.text import slugify
from PIL import Image

logger = logging.getLogger(__name__)


@app.task(name="save_salt_log")
def save_salt_log(**kwargs):
    try:
        # Extract form data
        store_id = kwargs.pop("store", None)
        user_employer_id = kwargs.pop("user_employer", None)
        user_id = kwargs.pop("user", None)
        # Get the Store instance using the store_id
        store_instance = (
            Store.objects.get(pk=store_id) if store_id is not None else None
        )
        user_employer_instance = (
            Employer.objects.get(pk=user_employer_id)
            if user_employer_id is not None
            else None
        )
        user_instance = CustomUser.objects.get(pk=user_id) if user_id else None
        # Set the Store instance back to the kwargs
        kwargs["store"] = store_instance
        kwargs["user_employer"] = user_employer_instance
        kwargs["user"] = user_instance
        # Save the form data to the database
        saltlog = SaltLog.objects.create(**kwargs)

        return {
            "incident_store": saltlog.id,
            "Incident_brief": saltlog.area_salted,
            "message": "Salt Log Created",
        }
    except CustomUser.DoesNotExist:
        error_message = f"User with ID {user_id} does not exist."
        logger.error(error_message)
        return {"error": error_message}
    except Store.DoesNotExist:
        error_message = f"Store with ID {store_id} does not exist."
        logger.error(error_message)
        return {"error": error_message}
    except Employer.DoesNotExist:
        error_message = f"Employer with ID {user_employer_id} does not exist."
        logger.error(error_message)
        return {"error": error_message}
    except Exception as e:
        logger.error(f"Error saving incident: {e}")
        return {"error": str(e)}


@app.task(name="create_salt_log_pdf")
def generate_salt_log_pdf_task(incident_id):
    try:
        # Fetch the salt log data
        incident = SaltLog.objects.get(pk=incident_id)

        # Get associated images
        images = get_s3_images_for_salt_log(
            incident.image_folder, incident.user_employer
        )

        # Prepare the context for rendering the PDF
        context = {"incident": incident, "images": images}
        html_content = render_to_string("quiz/salt_log_form_pdf.html", context)

        # Generate the PDF using pdfkit
        pdf_options = {
            "enable-local-file-access": None,
            "--keep-relative-links": "",
            "encoding": "UTF-8",
        }
        pdf = pdfkit.from_string(html_content, False, pdf_options)
        pdf_buffer = BytesIO(pdf)  # Create a BytesIO object to store the PDF content

        # Generate a unique filename
        store_number = incident.store.number
        date_salted_str = (
            incident.date_salted.strftime("%Y-%m-%d")
            if incident.date_salted
            else "no-date"
        )
        time_str = (
            incident.time_salted.strftime("%H%M")
            if incident.time_salted
            else "no-time"
        )
        image_folder = incident.image_folder
        pdf_filename = (
            f"{store_number}_{slugify(date_salted_str)}_"
            f"{time_str}_{image_folder}_salt_log_report.pdf"
        )

        # Define folder structure parameters
        company_name = slugify(incident.user_employer.name)
        store_name = slugify(incident.store.number)
        current_year = datetime.now().strftime("%Y")
        current_month = datetime.now().strftime("%m-%B")
        folder_path = (
            f"/SALTLOGS/{company_name}/"
            f"{current_year}/{current_month}/{store_name}"
        )

        # Define the full file path
        full_file_path = f"{folder_path}/{pdf_filename}"

        # Upload the file using the helper function
        upload_result = master_upload_file_to_dropbox(
            pdf_buffer.getvalue(), full_file_path
        )

        pdf_buffer.seek(0)  # Reset buffer position

        # Log or return the upload result
        if upload_result[0]:
            return {
                "status": "success",
                "message": "PDF generated and uploaded successfully",
            }
        else:
            raise Exception(upload_result[1])

    except SaltLog.DoesNotExist:
        error_msg = f"SaltLog with ID {incident_id} does not exist."
        logger.error(error_msg)
        return {"status": "error", "message": error_msg}
    except Exception as e:
        logger.error(f"Error in generate_salt_log_pdf_task: {e}")
        return {"status": "error", "message": str(e)}


@app.task(name="create_checklist_pdf", bind=True, max_retries=3)
def generate_checklist_pdf_task(self, checklist_id: int):
    checklist = (
        Checklist.objects.select_related("created_by", "submitted_by", "store")
        .prefetch_related("items")
        .get(pk=checklist_id)
    )
    t0 = time.time()
    logger.info("[PDF] start checklist_id=%s task_id=%s", checklist_id, self.request.id)

    try:
        checklist = (
            Checklist.objects.select_related(
                "created_by", "submitted_by", "template"
            )
            .prefetch_related("items")
            .get(pk=checklist_id)
        )
        # --- store segment (folder + filename) ---
        store_label = None
        if getattr(checklist, "store", None):
            store_label = getattr(checklist.store, "number", None) or getattr(
                checklist.store, "name", None
            )
        store_segment = slugify(store_label or "no-store")
        logger.info(
            "[PDF] loaded checklist slug=%s title=%s", checklist.slug, checklist.title
        )

        # ================== CHANGED: use your SMALL-PDF task ==================
        # Produce the small PDF in TEMP via your generate_fresh_checklist_pdf task
        # (This is a normal function call here; runs synchronously in this worker.)

        temp_key = generate_fresh_checklist_pdf(checklist_id)
        if not temp_key:
            raise RuntimeError("fresh-pdf task returned no key")

        # Sign & download the small PDF bytes
        signed = get_signed_url_for_key(temp_key, expires_in=900)
        r = requests.get(signed, timeout=30)
        r.raise_for_status()
        pdf_bytes = r.content
        pdf_buffer = BytesIO(pdf_bytes)
        logger.info("[PDF] fresh pdf_size_bytes=%d (key=%s)", len(pdf_bytes), temp_key)
        # ================== /CHANGED ==================

        # Build Dropbox path: company, checklist type, store, then date
        full_file_path, checklist_folder = build_checklist_dropbox_path(
            checklist, store_segment
        )
        logger.info(
            "[PDF] upload_path=%s checklist_folder=%s store_segment=%s",
            full_file_path,
            checklist_folder,
            store_segment,
        )

        # Upload to Dropbox (unchanged)
        ok, msg = master_upload_file_to_dropbox(pdf_buffer.getvalue(), full_file_path)

        # ================== EMAIL CHECKLIST PDF (unchanged) ==================
        group_name = "quiz_email"  # or "checklist_email"
        sendgrid_id = "d-7e7eb87381b04ef59bc39abc16550ead"
        logger.info("[Checklist Email Task] Using template ID: %s", sendgrid_id)

        employer_id = getattr(
            getattr(checklist.created_by, "employer", None), "id", None
        )
        if not employer_id:
            logger.warning(
                "[Checklist Email Task] No employer on checklist.created_by; skipping email."
            )
        else:
            User = get_user_model()
            to_emails = list(
                User.objects.filter(
                    Q(is_active=True),
                    Q(groups__name=group_name),
                    Q(employer_id=employer_id),
                )
                .values_list("email", flat=True)
                .distinct()
            )

            if not to_emails:
                logger.warning(
                    "[Checklist Email Task] No active users in group '%s' for employer_id=%s",
                    group_name,
                    employer_id,
                )
            else:
                tenant_api_key = TenantApiKeys.objects.filter(
                    employer_id=employer_id
                ).first()
                sender_email = (
                    tenant_api_key.verified_sender_email
                    if tenant_api_key
                    else settings.MAIL_DEFAULT_SENDER
                )

                pdf_filename = os.path.basename(full_file_path)
                logger.info(
                    "[Checklist Email Task] Preparing attachment: %s (%s bytes)",
                    pdf_filename,
                    len(pdf_bytes),
                )

                MAX_ATTACH_BYTES = 15_500_000  # conservative SG limit
                attachments = None
                if pdf_bytes and len(pdf_bytes) <= MAX_ATTACH_BYTES:
                    attachments = [
                        {
                            "content": base64.b64encode(pdf_bytes).decode(),
                            "filename": pdf_filename,
                            "type": "application/pdf",
                            "disposition": "attachment",
                        }
                    ]
                else:
                    logger.info(
                        "[Checklist Email Task] Attachment too large/missing; sending without file"
                    )

                first_email = to_emails[0]
                try:
                    recip = User.objects.get(email=first_email)
                    full_name = recip.get_full_name() or recip.username
                    company_name = (
                        getattr(getattr(recip, "employer", None), "name", None)
                        or "Company"
                    )
                except User.DoesNotExist:
                    full_name = "Team"
                    company_name = "Company"

                template_data = {
                    "subject": f"Checklist #{checklist.id} PDF",
                    "name": full_name,
                    "company_name": company_name,
                }

                try:
                    ok_email = create_master_email(
                        to_email=to_emails,  # list OK
                        sendgrid_id=sendgrid_id,
                        template_data=template_data,
                        attachments=attachments,
                        verified_sender=sender_email,
                    )
                    if ok_email:
                        logger.info(
                            "[Checklist Email Task] Email sent to %d recipients in '%s'",
                            len(to_emails),
                            group_name,
                        )
                    else:
                        logger.error(
                            "[Checklist Email Task] SendGrid send returned False"
                        )
                except Exception as e:
                    logger.exception(
                        "[Checklist Email Task] Error sending email: %s", e
                    )
        # ================== /EMAIL CHECKLIST PDF ==================

        pdf_buffer.close()

        if not ok:
            raise RuntimeError(msg or "Upload failed")

        # Persist (unchanged if you have these fields)
        update_fields = []
        if hasattr(checklist, "pdf_key"):
            checklist.pdf_key = full_file_path
            update_fields.append("pdf_key")
        if hasattr(checklist, "pdf_status"):
            checklist.pdf_status = "ready"
            update_fields.append("pdf_status")
        if hasattr(checklist, "pdf_generated_at"):
            checklist.pdf_generated_at = datetime.now()
            update_fields.append("pdf_generated_at")
        if update_fields:
            checklist.save(update_fields=update_fields)

        logger.info(
            "[PDF] done in %.2fs checklist_id=%s", time.time() - t0, checklist_id
        )
        return {"status": "success", "path": full_file_path}

    except Checklist.DoesNotExist:
        logger.error("[PDF] checklist not found id=%s", checklist_id)
        return {"status": "error", "message": f"Checklist {checklist_id} not found"}

    except Exception as e:
        logger.exception("[PDF] error checklist_id=%s err=%s", checklist_id, e)
        try:
            checklist = Checklist.objects.get(pk=checklist_id)
            if hasattr(checklist, "pdf_status"):
                checklist.pdf_status = "error"
                checklist.save(update_fields=["pdf_status"])
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


@app.task(name="generate_fresh_checklist_pdf")
def generate_fresh_checklist_pdf(checklist_id):
    try:
        checklist = (
            Checklist.objects.select_related("created_by", "submitted_by", "store")
            .prefetch_related("items")
            .get(pk=checklist_id)
        )
        logger.info(
            "[PDF] Generating fresh PDF for checklist_id=%s title=%s",
            checklist.id,
            checklist.title,
        )

    except Checklist.DoesNotExist:
        logger.error("[PDF] Checklist id=%s does not exist", checklist_id)
        return None

    def _downscale_for_pdf(http_url: str, max_px=800, jpeg_quality=75):
        """
        Fetch image URL, downscale, upload to TEMP, return signed URL.
        """
        try:
            logger.debug("[PDF] Fetching image from %s", http_url)
            r = requests.get(http_url, timeout=20)
            r.raise_for_status()
            im = Image.open(BytesIO(r.content)).convert("RGB")
            im.thumbnail((max_px, max_px), Image.LANCZOS)

            buf = BytesIO()
            im.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            buf.seek(0)

            tmp_key = f"TEMP/pdf-thumbs/{uuid.uuid4().hex}.jpg"
            upload_to_linode_object_storage(buf, tmp_key)
            buf.close()

            signed_url = get_signed_url_for_key(tmp_key, expires_in=900)
            logger.info("[PDF] Uploaded downscaled image to %s", tmp_key)
            return signed_url
        except Exception as e:
            logger.warning("[PDF] Could not process image %s: %s", http_url, e)
            return None

    # Build items
    items = []
    for it in checklist.items.select_related("action_item").order_by("order", "id"):
        photo_url = None
        if it.photo and it.photo.name:
            orig = get_signed_url_for_key(it.photo.name, expires_in=900)
            photo_url = _downscale_for_pdf(orig, max_px=800, jpeg_quality=75)
        action = it.get_action_item()
        items.append(
            {
                "text": it.text,
                "result": it.result,
                "answer": it.answer,
                "responsibility": it.responsibility,
                "text_value": it.text_value,
                "comment": it.comment,
                "photo_url": photo_url,
                "action": action,
            }
        )
    logger.info(
        "[PDF] Collected %s items for checklist_id=%s", len(items), checklist.id
    )

    # Render template
    html = render_to_string(
        "quiz/checklist_pdf.html",
        {
            "checklist": checklist,
            "items": items,
            "action_items": checklist.action_items.select_related("checklist_item"),
            **store_report_context(checklist.store),
        },
    )
    logger.debug("[PDF] Rendered HTML for checklist_id=%s", checklist.id)

    # Generate PDF
    try:
        options = {
            "enable-local-file-access": None,
            "encoding": "UTF-8",
            "--keep-relative-links": "",
        }
        pdf_bytes = pdfkit.from_string(html, False, options=options)
        logger.info(
            "[PDF] PDF generated successfully for checklist_id=%s", checklist.id
        )
    except Exception as e:
        logger.exception(
            "[PDF] Failed to generate PDF for checklist_id=%s: %s", checklist.id, e
        )
        return None

    pdf_buffer = BytesIO(pdf_bytes)

    # Upload to TEMP folder
    tmp_id = uuid.uuid4().hex
    filename = f"fresh-{slugify(checklist.slug or checklist.title)}-{tmp_id}.pdf"
    folder_path = f"TEMP/{datetime.now().strftime('%Y/%m/%d')}"
    full_path = f"{folder_path}/{filename}"

    try:
        upload_to_linode_object_storage(pdf_buffer, full_path)
        logger.info(
            "[PDF] Uploaded fresh PDF to %s for checklist_id=%s",
            full_path,
            checklist.id,
        )
    except Exception as e:
        logger.exception("[PDF] Upload failed for checklist_id=%s: %s", checklist.id, e)
        return None
    finally:
        pdf_buffer.close()

    return full_path

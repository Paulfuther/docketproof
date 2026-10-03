from __future__ import absolute_import, unicode_literals

from arl.celery import app
from io import BytesIO
from datetime import datetime
import pdfkit
from django.template.loader import render_to_string
from django.utils.text import slugify
from arl.reclose.models import RecClose  # adjust if your model is in another app
from arl.dbox.helpers import upload_to_dropbox
import logging

logger = logging.getLogger(__name__)


@app.task(name="generate_recclose_pdf_task")
def generate_recclose_pdf_task(recclose_id):
    try:
        logger.info(f"🔁 Starting RecClose PDF task for ID: {recclose_id}")
        # Fetch the recclose entry
        entry = RecClose.objects.get(pk=recclose_id)

        # Prepare context for rendering
        context = {"entry": entry}
        html_content = render_to_string("reclose/recclose_pdf_template.html", context)

        # PDF generation options
        pdf_options = {
            "enable-local-file-access": None,
            "--keep-relative-links": "",
            "encoding": "UTF-8",
        }
        pdf = pdfkit.from_string(html_content, False, pdf_options)
        pdf_buffer = BytesIO(pdf)

        # Build file/folder naming
        store_number = slugify(entry.store_number or "unknown-store")
        date_str = entry.timestamp.strftime("%Y-%m-%d") if entry.timestamp else "no-date"
        pdf_filename = f"{store_number}_{slugify(date_str)}_recclose.pdf"

        company_name = slugify(entry.user_employer.name)
        year = datetime.now().strftime("%Y")
        month = datetime.now().strftime("%m-%B")
        folder_path = f"/RECANDCLOSE/{company_name}/{year}/{month}/{store_number}"
        full_file_path = f"{folder_path}/{pdf_filename}"

        logger.info(f"📤 Uploading to Dropbox: {full_file_path}")

        # Upload
        upload_result = upload_to_dropbox(
            pdf_buffer.getvalue(),
            full_file_path,
            employer=entry.user_employer,
            write_mode="add",
        )

        if upload_result[0]:
            return {"status": "success", "path": full_file_path}
        else:
            raise Exception(upload_result[1])

    except RecClose.DoesNotExist:
        error_msg = f"❌ RecClose with ID {recclose_id} does not exist."
        logger.error(error_msg)
        return {"status": "error", "message": error_msg}

    except Exception as e:
        logger.exception(f"🔥 Unexpected error in RecClose task: {str(e)}")
        return {"status": "error", "message": str(e)}
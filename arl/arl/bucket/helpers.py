import re
from datetime import date, datetime
from urllib.parse import quote, unquote
import boto
import boto.s3.connection
from botocore.exceptions import NoCredentialsError
from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.shortcuts import redirect
import logging

logger = logging.getLogger(__name__)


LINODE_ACCESS_KEY = settings.LINODE_ACCESS_KEY
LINODE_SECRET_KEY = settings.LINODE_SECRET_KEY
LINODE_REGION = settings.LINODE_REGION
LINODE_BUCKET_NAME = settings.LINODE_BUCKET_NAME
LINODE_URL = settings.LINODE_URL

conn = None
try:
    conn = boto.connect_s3(
        aws_access_key_id=LINODE_ACCESS_KEY,
        aws_secret_access_key=LINODE_SECRET_KEY,
        host=LINODE_REGION,
        # is_secure=False,               # uncomment if you are not using ssl
        calling_format=boto.s3.connection.OrdinaryCallingFormat(),
    )
except Exception as e:
    print("An error occurred while connecting to S3:", e)


def upload_to_linode_object_storage(file_obj, object_key):
    # Connect to Linode Object Storage
    # Get the bucket
    print("Linode upload to start")
    bucket = conn.get_bucket("paulfuther")
    # Create the S3 key
    key = bucket.new_key(object_key)
    # Set the key's contents from the file object
    key.set_contents_from_file(file_obj)
    # Close the connection
    conn.close()


def get_s3_images_for_incident(image_folder, employer):
    bucket_name = LINODE_BUCKET_NAME
    folder_path = f"SITEINCIDENT/{employer}/{image_folder}/"
    images = []
    try:
        bucket = conn.get_bucket(bucket_name)
        objects = bucket.list(prefix=folder_path)
        for obj in objects:
            if obj.key.endswith(("jpg", "jpeg", "png", "gif")):
                image_key = obj.key
                image_url = conn.generate_url(
                    expires_in=3600, method="GET", bucket=bucket_name, key=image_key
                )
                images.append(image_url)
    except Exception as e:
        print("An error occurred:", e)
    return images


def remove_old_backups():
    bucket_name = settings.LINODE_BUCKET_NAME
    folder_path = "POSTGRES/"
    today = date.today()
    try:
        bucket = conn.get_bucket(bucket_name)
        objects = bucket.list(prefix=folder_path)
        deleted_objects = 0

        for obj in objects:
            if isinstance(obj.last_modified, str):
                try:
                    modified_time = datetime.strptime(
                        obj.last_modified, "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                except ValueError:
                    logger.warning(
                        f"Unable to parse last_modified string: {obj.last_modified}"
                    )
                    continue
            else:
                modified_time = obj.last_modified.date()

            age_in_days = (
                today - modified_time.date()
            ).days  # Extract date for comparison

            if age_in_days > 7:
                obj.delete()
                deleted_objects += 1

        logger.info(
            f"Old backups removed successfully. Total objects deleted: {deleted_objects}"
        )
        return "Old backups removed successfully"

    except Exception as e:
        logger.error(f"Error occurred during backup cleanup: {e}")
        return f"Error occurred during backup cleanup: {e}"


def list_s3_objects(folder_name):
    try:
        bucket = conn.get_bucket(LINODE_BUCKET_NAME)
        folder_prefix = folder_name
        # Retrieve the objects in the folder
        objects = bucket.list(prefix=folder_prefix)
        # Generate links for the files in the folder
        filtered_keys = []
        for obj in objects:
            if not obj.key.endswith("/"):  # Exclude folders
                file_name = unquote(obj.key[len(folder_prefix):].lstrip("/"))
                encoded_key = quote(obj.key, safe="")  # URL-encode the filename

                # Check if the file matches the specified pattern
                if re.match(r"\d{8}_\d{6}_\d{6}_", file_name):
                    # Extract the relevant parts from the file name
                    parts = re.split(r"_", file_name)
                    new_file_name = parts[0] + "_" + "_".join(parts[3:])
                else:
                    new_file_name = file_name

                # Assume it's always a file, create a link for downloading
                link = reverse("download_from_s3", kwargs={"key": encoded_key})
                filtered_keys.append({"name": new_file_name, "link": link})

        return filtered_keys

    except NoCredentialsError:
        # Handle exceptions appropriately (e.g., log the error)
        print("Credentials not available")
        return []


def read_s3_object_bytes(file_path):
    """Read an object from Linode/S3 storage and return its raw bytes."""
    bucket = conn.get_bucket(settings.LINODE_BUCKET_NAME)
    key = bucket.get_key(file_path)
    if not key:
        raise FileNotFoundError(f"File not found in storage: {file_path}")
    return key.get_contents_as_string()


def download_from_s3(request, file_path, custom_filename=None):
    try:
        # 🔒 Connect to the bucket
        bucket = conn.get_bucket(settings.LINODE_BUCKET_NAME)

        # 📂 Get the S3 object using the file path
        key = bucket.get_key(file_path)
        if not key:
            return HttpResponse("File not found in storage.", status=404)

        # 🧼 Use passed custom filename if available
        if not custom_filename:
            filename = key.name.split("/")[-1]
            custom_filename = filename

        filename_encoded = quote(custom_filename, safe="")
        
        # ⏳ Generate a pre-signed URL for 1 hour
        url = key.generate_url(
            expires_in=3600,
            query_auth=True,
            force_http=False,
            response_headers={
                "response-content-disposition": f'attachment; filename="{filename_encoded}"'
            }
        )
        # 🔁 Redirect user directly to the signed S3 URL
        return redirect(url)

    except Exception as e:
        print(f"Error downloading from S3: {str(e)}")
        return HttpResponse("Error downloading from S3", status=500)
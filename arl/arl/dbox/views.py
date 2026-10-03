# your_app_name/views.py

import os
from datetime import datetime
from urllib.parse import quote

import dropbox
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from dropbox import DropboxOAuth2FlowNoRedirect
from dropbox.exceptions import ApiError
from dropbox.files import FolderMetadata, ListFolderResult

from .helpers import generate_new_access_token, upload_to_dropbox

app_key = settings.DROP_BOX_KEY
app_secret = settings.DROP_BOX_SECRET


def _request_employer(request):
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return getattr(user, "employer", None)
    return None


@login_required
def dropbox_auth(request):
    # Replace with your actual app key and secret
    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key, app_secret, token_access_type="offline"
    )

    authorize_url = auth_flow.start()
    # Print the URL or redirect the user to this URL
    print("1. Go to: " + authorize_url)
    print('2. Click "Allow" (you might have to log in first).')
    print("3. After authorization, you will be redirected to the redirect URL.")
    return HttpResponse("Authorization URL printed to console.")


@login_required(login_url="login")
def use_dropbox(request):
    # Replace with your actual app key and secret
    # Use the authorization code to obtain tokens
    authorization_code = settings.DROP_BOX_AUTHORIZATION_CODE  # Replace with the actual
    # authorization code
    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key, app_secret, token_access_type="offline"
    )
    oauth_result = auth_flow.finish(authorization_code)
    # Access token allows access to the user's Dropbox account
    access_token = oauth_result.access_token

    # Refresh token can be used to obtain a new access token when needed
    # Uncomment refresh_token to create a new one should the old be revoked
    # refresh_token = oauth_result.refresh_token
    # print(access_token, refresh_token)

    # Now you can use the access_token and refresh_token for API calls and token management
    dbx = dropbox.Dropbox(oauth2_access_token=access_token)
    try:
        account_info = dbx.users_get_current_account()
        return HttpResponse(
            f"Successfully authenticated as {account_info.name.display_name}."
        )
    except dropbox.exceptions.AuthError as e:
        return HttpResponse("Authentication failed: " + str(e))


@login_required(login_url="login")
def list_folders(request, path=""):
    employer = _request_employer(request)
    new_access_token = generate_new_access_token(employer=employer)
    if new_access_token:
        try:
            # Use the new access token to create the Dropbox client
            dbx = dropbox.Dropbox(new_access_token)

            if request.method == "POST":
                # Handle file upload
                folder_path = request.POST.get("folder_name")
                uploaded_file = request.FILES.get("file")

                if folder_path and uploaded_file:
                    try:
                        # Specify the path where the file will be uploaded
                        upload_path = f"/{folder_path}/{uploaded_file.name}"
                        success, message = upload_to_dropbox(
                            uploaded_file.read(),
                            upload_path,
                            employer=employer,
                            write_mode="add",
                        )
                        if not success:
                            return HttpResponse("Error uploading file: " + message)

                        # Redirect to the same page after successful upload
                        return redirect("list_folders", path=folder_path)
                    except Exception as e:
                        return HttpResponse("Error uploading file: " + str(e))

            # Fetch metadata for files and folders
            folder_list = dbx.files_list_folder(path=path)
            folders = [
                entry
                for entry in folder_list.entries
                if isinstance(entry, dropbox.files.FolderMetadata)
            ]
            files = [
                entry
                for entry in folder_list.entries
                if isinstance(entry, dropbox.files.FileMetadata)
            ]

            # Retrieve last modified dates for files
            file_data = [
                {
                    "metadata": file_metadata,
                    "extension": file_metadata.name.split(".")[-1],
                }
                for file_metadata in files
            ]

            # Calculate an estimated last modified date for folders based on contained files
            folder_last_modified = {}
            for folder in folders:
                folder_files = [
                    entry
                    for entry in folder_list.entries
                    if isinstance(entry, dropbox.files.FileMetadata)
                    and entry.path_lower.startswith(folder.path_lower)
                ]
                last_modified_files = [file.server_modified for file in folder_files]
                if last_modified_files:
                    folder_last_modified[folder.path_lower] = max(last_modified_files)
                else:
                    folder_last_modified[folder.path_lower] = datetime.now().isoformat()

            return render(
                request,
                "dbox/list_folders.html",
                {
                    "folder_list": folder_list,
                    "folders": folders,
                    "files": file_data,
                    "folder_last_modified": folder_last_modified,
                    "current_path": path,
                },
            )
        except dropbox.exceptions.AuthError as e:
            return HttpResponse("API request failed. Authentication error: " + str(e))
        except dropbox.exceptions.ApiError as e:
            return HttpResponse("API request failed. API error: " + str(e))
        except Exception as e:
            return HttpResponse("API request failed. Error: " + str(e))
    else:
        return HttpResponse("Refresh token not found in .env file.", status=500)


@login_required(login_url="login")
def list_files(request, folder_name):
    employer = _request_employer(request)
    new_access_token = generate_new_access_token(employer=employer)
    if new_access_token:
        dbx = dropbox.Dropbox(new_access_token)
        try:
            # Make API request to list files within the specified folder
            folder_path = f"/{folder_name}"
            file_list = dbx.files_list_folder(path=folder_path)
            if isinstance(file_list, dropbox.files.ListFolderResult):
                file_info_list = []
                for entry in file_list.entries:
                    file_name = entry.name
                    if isinstance(entry, dropbox.files.FileMetadata):
                        # Extract file type information, adjust this based on Dropbox API response
                        file_type = (
                            entry.media_info.get_metadata(".tag")
                            if entry.media_info
                            else None
                        )

                        # Append file information to the list
                        file_info = {"name": file_name, "type": file_type}
                        file_info_list.append(file_info)
                if request.method == "POST" and "file" in request.FILES:
                    file_to_upload = request.FILES["file"]
                    upload_path = f"{folder_path}/{file_to_upload.name}"
                    success, message = upload_to_dropbox(
                        file_to_upload.read(),
                        upload_path,
                        employer=employer,
                        write_mode="add",
                    )
                    if not success:
                        return HttpResponse("Error uploading file: " + message)

                return render(
                    request,
                    "list_files.html",
                    {"folder_name": folder_name, "file_names": file_names},
                )
            else:
                return HttpResponse("API request failed. Invalid response format.")
        except dropbox.exceptions.AuthError as e:
            return HttpResponse("API request failed. Authentication error: " + str(e))
        except dropbox.exceptions.ApiError as e:
            return HttpResponse("API request failed. API error: " + str(e))
        except Exception as e:
            return HttpResponse("API request failed. Error: " + str(e))
    else:
        return HttpResponse("Refresh token not found in .env file.", status=500)


@login_required(login_url="login")
def download_file(request):
    file_path = request.GET.get("path", "")

    if not file_path:
        return HttpResponse("File path is missing.", status=400)
    access_token = generate_new_access_token(employer=_request_employer(request))
    try:
        dbx = dropbox.Dropbox(access_token)
        metadata, response = dbx.files_download(file_path)
        file_content = response.content

        # Set up the response as a downloadable file
        http_response = HttpResponse(content_type="application/force-download")
        http_response[
            "Content-Disposition"
        ] = f'attachment; filename="{os.path.basename(file_path)}"'
        http_response.write(file_content)
        return http_response
    except ApiError as e:
        return HttpResponse(f"Failed to download file: {str(e)}", status=500)
    except Exception as e:
        return HttpResponse(f"Failed to download file: {str(e)}", status=500)


@login_required(login_url="login")
def view_folder(request):
    new_access_token = generate_new_access_token(employer=_request_employer(request))
    if new_access_token:
        # Use the new access token to create the Dropbox client
        dbx = dropbox.Dropbox(new_access_token)

        try:
            # Make API request to list the root folder
            folder_list = dbx.files_list_folder(path="")
            if isinstance(folder_list, ListFolderResult):
                folders = [
                    entry
                    for entry in folder_list.entries
                    if isinstance(entry, dropbox.files.FolderMetadata)
                ]
                return render(request, "list_folders.html", {"folders": folders})
            else:
                return HttpResponse("API request failed. Invalid response format.")
        except dropbox.exceptions.AuthError as e:
            return HttpResponse("API request failed. Authentication error: " + str(e))
        except dropbox.exceptions.ApiError as e:
            return HttpResponse("API request failed. API error: " + str(e))
        except Exception as e:
            return HttpResponse("API request failed. Error: " + str(e))
    else:
        return HttpResponse("Refresh token not found in .env file.", status=500)


@login_required(login_url="login")
def list_folder_contents(request, path=""):
    new_access_token = generate_new_access_token(employer=_request_employer(request))
    if new_access_token:
        dbx = dropbox.Dropbox(new_access_token)
        try:
            # encoded_path = quote(path)
            folder_list = dbx.files_list_folder(path)
            if isinstance(folder_list, dropbox.files.ListFolderResult):
                files = [
                    entry
                    for entry in folder_list.entries
                    if isinstance(entry, dropbox.files.FileMetadata)
                ]
                return render(
                    request,
                    "dbox/list_folder_contents.html",
                    {"files": files, "folder_path": path},
                )
            else:
                return HttpResponse("API request failed. Invalid response format.")
        except dropbox.exceptions.AuthError as e:
            return HttpResponse("API request failed. Authentication error: " + str(e))
        except dropbox.exceptions.ApiError as e:
            return HttpResponse("API request failed. API error: " + str(e))
        except Exception as e:
            return HttpResponse("API request failed. Error: " + str(e))
    else:
        return HttpResponse("Refresh token not found in .env file.", status=500)


@login_required(login_url="login")
def upload_file(request):
    employer = _request_employer(request)
    if request.method == "POST":
        try:
            folder_path = request.POST.get("folder_path", "")
            uploaded_file = request.FILES["file"]
            file_path = os.path.join(folder_path, uploaded_file.name)
            success, message = upload_to_dropbox(
                uploaded_file.read(),
                file_path,
                employer=employer,
                write_mode="add",
            )
            if not success:
                return render(request, "error.html", {"error_message": message})
            return redirect("list_folder_contents", path=quote(folder_path))
        except Exception as e:
            return render(request, "error.html", {"error_message": str(e)})
    else:
        new_access_token = generate_new_access_token(employer=employer)
        if new_access_token:
            dbx = dropbox.Dropbox(new_access_token)
            try:
                # Fetch the list of folders from Dropbox
                folder_list = dbx.files_list_folder(path="")
                folders = [
                    entry
                    for entry in folder_list.entries
                    if isinstance(entry, FolderMetadata)
                ]
                return render(request, "dbox/dbx_upload.html", {"folders": folders})
            except ApiError as e:
                return render(request, "error.html", {"error_message": str(e)})
            except Exception as e:
                return render(request, "error.html", {"error_message": str(e)})
        else:
            return render(
                request,
                "error.html",
                {"error_message": "Refresh token not found in .env file."},
            )


@login_required(login_url="login")
def delete_file(request):
    if request.method == "GET":
        try:
            file_path = request.GET.get("path", "")
            new_access_token = generate_new_access_token(
                employer=_request_employer(request)
            )
            if new_access_token:
                dbx = dropbox.Dropbox(new_access_token)
                dbx.files_delete_v2(file_path)
                return redirect("list_folder_contents", path=os.path.dirname(file_path))
            else:
                return render(
                    request,
                    "error.html",
                    {"error_message": "Refresh token not found in .env file."},
                )
        except ApiError as e:
            return render(request, "error.html", {"error_message": str(e)})
        except Exception as e:
            return render(request, "error.html", {"error_message": str(e)})

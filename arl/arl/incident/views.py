import logging
from io import BytesIO

from celery import chain
from django.contrib import messages
from django.contrib.auth.decorators import (
    login_required,
    permission_required,
    user_passes_test,
)
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import ListView
from django.views.generic.edit import CreateView, UpdateView
from PIL import Image

from arl.helpers import get_s3_images_for_incident, upload_to_linode_object_storage
from arl.user.gsa_access import (
    GSAOrPermissionRequiredMixin,
    gsa_preview_blocks_mutation,
    post_form_success_url,
)

from .forms import IncidentForm, MajorIncidentForm
from .models import Incident, MajorIncident
from .tasks import (
    generate_and_send_pdf_task,
    generate_pdf_email_to_user_task,
    generate_pdf_task,
    generate_restricted_incident_pdf_email_task,
    process_new_incident_reports_task,
    save_incident_file,
    send_email_to_group_task,
    generate_significant_security_pdf_email_task
)

incident_updated = Signal()
logger = logging.getLogger(__name__)

INCIDENT_PAGE_SIZE = 10


# Custom decorator to check if the user belongs to abm_incident_report group
def is_abm_incident_pdf(user):
    return user.groups.filter(name="abm_incident_pdf").exists()


def _incident_list_for_user(user, query):
    """Employer-scoped incident rows for the Edit tab, newest first."""
    if not hasattr(user, "employer"):
        qs = Incident.objects.none()
    else:
        qs = (
            Incident.objects.filter(user_employer=user.employer)
            .select_related("store")
            .order_by("-eventdate", "-pk")
        )
    query = (query or "").strip()
    if query:
        qs = qs.annotate(
            store_number_text=Cast("store__number", CharField())
        ).filter(
            Q(brief_description__icontains=query)
            | Q(store__city__icontains=query)
            | Q(store_number_text__icontains=query)
        )
    return qs


def _hub_list_context(request):
    """Shared Start | Edit tab context. Does not build the incident form."""
    user = request.user
    can_add = user.has_perm("incident.add_incident")
    can_view = user.has_perm("incident.view_incident")
    query = (request.GET.get("q") or "").strip()
    page_obj = None
    incident_count = 0
    if can_view:
        paginator = Paginator(
            _incident_list_for_user(user, query), INCIDENT_PAGE_SIZE
        )
        page_obj = paginator.get_page(request.GET.get("page"))
        incident_count = paginator.count
    tab = request.GET.get("tab") or ("start" if can_add else "edit")
    if tab == "create":
        tab = "start"
    if tab not in ("start", "edit"):
        tab = "start" if can_add else "edit"
    if tab == "start" and not can_add:
        tab = "edit"
    if tab == "edit" and not can_view:
        tab = "start"
    return {
        "active_tab": tab,
        "can_add": can_add,
        "can_view": can_view,
        "can_view_pdf": is_abm_incident_pdf(user),
        "page_obj": page_obj,
        "incident_count": incident_count,
        "q": query,
        "form_action": reverse("create_incident"),
    }


@login_required(login_url="/login/")
def incident_edit_list(request):
    """Edit-tab rows for live search. Swapped in as you type, without a full page load."""
    if not request.user.has_perm("incident.view_incident"):
        return render(request, "incident/403.html", status=403)
    context = _hub_list_context(request)
    context["live_search"] = True
    return render(request, "incident/partials/incident_edit_list.html", context)


class IncidentHubView(LoginRequiredMixin, View):
    """One Incidents page: Create a report, or pick an existing one to edit."""

    login_url = "/login/"
    template_name = "incident/incidents.html"

    def get(self, request):
        can_add = request.user.has_perm("incident.add_incident")
        can_view = request.user.has_perm("incident.view_incident")
        if not can_add and not can_view:
            return render(request, "incident/403.html", status=403)
        context = _hub_list_context(request)
        context["existing_images"] = []
        if can_add:
            context["form"] = IncidentForm(user=request.user)
        return render(request, self.template_name, context)


class IncidentCreateView(
    GSAOrPermissionRequiredMixin,
    LoginRequiredMixin,
    CreateView,
):
    model = Incident
    login_url = "/login/"
    permission_required = "incident.add_incident"
    form_class = IncidentForm
    template_name = "incident/incidents.html"
    success_url = reverse_lazy("home")

    def get(self, request, *args, **kwargs):
        return redirect(f"{reverse('incidents')}?tab=start")

    def get_success_url(self):
        return str(post_form_success_url(self.request.user, self.request))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["existing_images"] = []
        context.update(_hub_list_context(self.request))
        context["active_tab"] = "start"
        return context

    def handle_no_permission(self):
        return render(
            self.request,
            "incident/403.html",
            status=403,
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        # Assign tenant ownership server-side.
        form.instance.user_employer = self.request.user.employer

        # Save the Incident. This triggers the post_save receiver.
        self.object = form.save()

        messages.success(
            self.request,
            (
                "The incident was submitted successfully. "
                "The reports are being generated and will be emailed when ready."
            ),
        )

        return redirect(self.get_success_url())

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))


class MajorIncidentCreateView(PermissionRequiredMixin, LoginRequiredMixin, CreateView):
    model = MajorIncident
    login_url = "/login/"
    permission_required = "incident.add_major_incident"
    form_class = MajorIncidentForm
    template_name = "incident/create_major_incident.html"
    success_url = reverse_lazy("home")
    # permission_denied_message = "You are not allowed to add incidents."

    def handle_no_permission(self):
        # Render the custom 403.html template for permission denial
        return render(self.request, "incident/403.html", status=403)

    def dispatch(self, request, *args, **kwargs):
        try:
            # Your print statement for debugging
            print("Dispatch method called.")
            return super().dispatch(request, *args, **kwargs)
        except Exception as e:
            print(f"Exception occurred: {e}")
            raise

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user  # Pass the user to the form
        return kwargs

    def get(self, request, *args, **kwargs):
        form = self.form_class(user=self.request.user)
        return self.render_to_response({"form": form})

    def form_valid(self, form):
        form.instance.user_employer = self.request.user.employer
        form_data = self.serialize_form_data(form.cleaned_data)

        # Trigger the Celery task to save form data
        save_major_incident_file.delay(**form_data)

        messages.success(
            self.request,
            "PDF generation started. Check your email. The file is attached.",
        )
        return redirect("home")

    def form_invalid(self, form):
        return self.render_to_response({"form": form})

    def serialize_form_data(self, form_data):
        # Convert ForeignKey fields to their primary key values
        form_data["store"] = (
            form_data["store"].pk
            if "store" in form_data and form_data["store"] is not None
            else None
        )
        form_data["user_employer"] = (
            form_data["user_employer"].pk
            if "user_employer" in form_data and form_data["user_employer"] is not None
            else None
        )
        return form_data


@receiver(
    post_save,
    sender=Incident,
    dispatch_uid="handle_new_incident_form_creation",
)
def handle_new_incident_form_creation(sender, instance, created, **kwargs):
    if not created:
        return

    Incident.objects.filter(pk=instance.pk).update(queued_for_sending=True)

    process_new_incident_reports_task.delay(instance.pk)


class QueuedIncidentsListView(ListView):
    model = Incident
    template_name = "incident/queued_incidents_list.html"
    context_object_name = "queued_incidents"

    def get_queryset(self):
        queryset = Incident.objects.filter(
            queued_for_sending=True, sent=False, do_not_send=False
        )
        print(queryset)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        logger.debug(f"Context Data: {context}")
        print("Context Data:", context)
        return context


# This route is used to send an incident to anyone
# on the external recipient list
def send_incident_now(request, pk):
    """
    This is part of the queued incidents to external sendrs.
    Process the "Send Now" action for an incident and update its status.
    """
    # Fetch the incident object
    incident = get_object_or_404(Incident, pk=pk)

    try:
        # Mark the incident as processed
        incident.queued_for_sending = False
        incident.save(update_fields=["queued_for_sending"])

        # Trigger the task to generate and send the PDF
        generate_and_send_pdf_task.delay(incident.id)

        # Return an empty response to indicate the row should be removed
        return HttpResponse(status=200)  # HTMX will remove the row with
        # "outerHTML:remove"
    except Exception as e:
        # Return an error response as an HTML row for display
        error_html = f"""
        <tr id="incident-{incident.id}">
            <td colspan="5" class="text-danger">Error: {str(e)}</td>
        </tr>
        """
        return HttpResponse(error_html, status=500, content_type="text/html")


def mark_do_not_send(request, pk):
    """
    Marking do not send for queued incidents.
    Marks the incident as "Do Not Send" and removes it from the queue.
    """
    incident = get_object_or_404(Incident, pk=pk)

    try:
        # Mark the incident as "Do Not Send"
        incident.queued_for_sending = False
        incident.sent = False  # Ensure it's not marked as sent
        incident.do_not_send = True
        incident.save(update_fields=["queued_for_sending", "sent", "do_not_send"])
        print(f"Incident {pk} marked as 'Do Not Send'.")
        return HttpResponse(status=200)  # HTMX will remove the row
    except Exception as e:
        print(f"Error processing 'Do Not Send' for incident {pk}: {e}")
        return HttpResponse(status=500)  # Generic error response


# APPROVED for multi tenant
class IncidentUpdateView(PermissionRequiredMixin, LoginRequiredMixin, UpdateView):
    model = Incident
    login_url = "/login/"
    permission_required = "incident.add_incident"
    form_class = IncidentForm
    template_name = "incident/create_incident.html"

    def get_success_url(self):
        return f"{reverse('incidents')}?tab=edit"

    def handle_no_permission(self):
        # Render the custom 403.html template for permission denial
        return render(self.request, "incident/403.html", status=403)

    def dispatch(self, request, *args, **kwargs):
        try:
            # Your print statement for debugging
            print("Dispatch method called.")
            return super().dispatch(request, *args, **kwargs)
        except Exception as e:
            print(f"Exception occurred: {e}")
            raise

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        user = self.request.user
        employer = user.employer
        existing_images = []

        # Check if image_folder exists and get available images from S3
        if self.object.image_folder:
            existing_images = get_s3_images_for_incident(
                self.object.image_folder, user.employer
            )
        # print(existing_images)
        form = self.form_class(
            instance=self.object, initial={"existing_images": existing_images}
        )
        form.fields["user_employer"].initial = employer

        return self.render_to_response(
            self.get_context_data(
                form=form, existing_images=existing_images, user_employer=employer
            )
        )

    def form_valid(self, form):
        try:
            # Save the updated instance
            response = super().form_valid(form)
            # print("here we go")
            # Trigger the tasks for PDF generation and email
            chain(
                generate_pdf_task.si(self.object.id),  # Generate PDF
                send_email_to_group_task.s(
                    group_name="incident_update_email",
                    employer_id=self.request.user.employer.id,
                ),
            ).apply_async()

            # Add a success message
            messages.success(
                self.request,
                "The incident was updated successfully. A notification has been sent to the relevant group.",
            )

            logger.info(
                f"Tasks triggered for Incident ID {self.object.id} after update."
            )
            return response

        except Exception as e:
            logger.error(
                f"Error triggering tasks for Incident ID {self.object.id}: {e}"
            )
            messages.error(
                self.request,
                "An error occurred while processing the update tasks. Please try again.",
            )
            return super().form_invalid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        return context


@login_required
@permission_required("incident.change_incident", raise_exception=True)
def htmx_edit_incident(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    existing_images = []

    if incident.image_folder:
        existing_images = get_s3_images_for_incident(
            incident.image_folder, request.user.employer
        )

    if request.method == "POST":
        form = IncidentForm(request.POST, request.FILES, instance=incident)
        if form.is_valid():
            form.save()
            return render(
                request,
                "incident/partials/incident_card_content.html",
                {"incident": incident, "updated": True},
            )
    else:
        form = IncidentForm(
            instance=incident, initial={"existing_images": existing_images}
        )
        form.fields["user_employer"].initial = request.user.employer

    return render(
        request,
        "incident/partials/edit_incident_form.html",
        {"form": form, "incident": incident, "existing_images": existing_images},
    )


@login_required
# @permission_required("incident.view_incident", raise_exception=True)
def incident_dashboard(request):
    user = request.user
    form = IncidentForm(request.POST or None, request.FILES or None, user=user)

    if request.method == "POST":
        blocked = gsa_preview_blocks_mutation(request)
        if blocked:
            return blocked
        if form.is_valid():
            form.instance.user_employer = user.employer
            form_data = form.cleaned_data

            # Convert FK fields to IDs
            form_data["store"] = form_data["store"].pk if form_data["store"] else None
            form_data["user_employer"] = user.employer.pk

            # Trigger Celery task
            save_incident_file.delay(**form_data)

            messages.success(request, "PDF generation started. Check your email.")
            return redirect("incident_dashboard")

    # Grab incident list for dashboard tab
    incidents = Incident.objects.filter(user_employer=user.employer).order_by(
        "-eventdate"
    )

    return render(
        request,
        "incident/incident_dashboard.html",
        {
            "form": form,
            "incidents": incidents,
            "existing_images": [],
        },
    )


class MajorIncidentUpdateView(PermissionRequiredMixin, LoginRequiredMixin, UpdateView):
    model = MajorIncident
    login_url = "/login/"
    permission_required = "incident.add_incident"
    form_class = MajorIncidentForm
    template_name = "incident/create_major_incident.html"
    success_url = reverse_lazy("major_incident_list")

    def handle_no_permission(self):
        # Render the custom 403.html template for permission denial
        return render(self.request, "incident/403.html", status=403)

    def dispatch(self, request, *args, **kwargs):
        try:
            # Your print statement for debugging
            print("Dispatch method called.")
            return super().dispatch(request, *args, **kwargs)
        except Exception as e:
            print(f"Exception occurred: {e}")
            raise

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        user = self.request.user
        employer = user.employer
        existing_images = []

        # Check if image_folder exists and get available images from S3
        if self.object.image_folder:
            existing_images = get_s3_images_for_incident(
                self.object.image_folder, user.employer
            )
        # print(existing_images)
        form = self.form_class(
            instance=self.object, initial={"existing_images": existing_images}
        )
        form.fields["user_employer"].initial = employer

        return self.render_to_response(
            self.get_context_data(
                form=form, existing_images=existing_images, user_employer=employer
            )
        )

    def form_valid(self, form):
        form.instance.user_employer = self.request.user.employer
        return super().form_valid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        return context


class ProcessIncidentImagesView(GSAOrPermissionRequiredMixin, LoginRequiredMixin, View):
    login_url = "/login/"
    permission_required = "incident.add_incident"
    raise_exception = True  # Raise exception when no access
    # instead of redirect
    permission_denied_message = "You are not allowed to add incidents."

    def post(self, request, *args, **kwargs):
        if request.method == "POST":
            user = request.user  # Authenticated user
            # print(user.employer)
            image_folder = request.POST.get("image_folder")
            # print(image_folder)
            employer = user.employer
            # Process the uploaded files here
            uploaded_files = request.FILES.getlist(
                "file"
            )  # 'file' is the field name used by Dropzone
            for uploaded_file in uploaded_files:
                # print(uploaded_file.name)
                file = uploaded_file
                folder_name = f"SITEINCIDENT/{employer}/{image_folder}/"
                filename = uploaded_file.name
                employee_key = "{}/{}".format(folder_name, filename)
                # print(employee_key)
                # Open the uploaded image using Pillow
                image = Image.open(file)
                # Resize the image to a larger thumbnail size, e.g.,
                # 1000x1000 pixels
                thumbnail_size = (1500, 1500)
                image.thumbnail(thumbnail_size, Image.LANCZOS)
                # Resize the image to your desired dimensions (e.g., 1000x1000)
                # resized_image = image.resize((500, 500), Image.LANCZOS)
                # print(image)
                # Save the resized image to a temporary BytesIO object
                temp_buffer = BytesIO()
                image.save(temp_buffer, format="JPEG")
                temp_buffer.seek(0)
                # Upload the resized image to Linode Object Storage
                upload_to_linode_object_storage(temp_buffer, employee_key)
                # Close the temporary buffer
                temp_buffer.close()
                # Process and save the files to the desired location
                # You can use the uploaded_files list to iterate through
                # the files and save them

                # Return a JSON response indicating success
            return JsonResponse({"message": "Files uploaded successfully"})
        else:
            # Handle GET request or other methods
            return JsonResponse({"message": "Invalid request method"})


# This route is used to generate a pdf from a newly created
# incident form. It calls a task to upload and email
# the incident form


def generate_pdf(request, incident_id):
    user_email = request.user.email
    generate_pdf_task.delay(incident_id, user_email)
    messages.success(request, "PDF generation started. HR will receive copy")
    return redirect("home")


# This route is used to generate a pdf and
# email it to the user


def generate_incident_pdf_email(request, incident_id):
    user_email = request.user.email
    generate_pdf_email_to_user_task.delay(incident_id, user_email)
    messages.success(
        request, "PDF generation started. Check your email for the attached file."
    )
    return redirect("home")


# this route is used to email a resricted incident report to a user
# who passes the test whichis being a member of the group amb_incident_report
@login_required
@user_passes_test(is_abm_incident_pdf, login_url="home", redirect_field_name=None)
def generate_restricted_incident_pdf_email(request, incident_id):
    """
    Generates a restricted PDF report and emails it to the requesting user.
    """
    user_email = request.user.email

    # Trigger the Celery task
    generate_restricted_incident_pdf_email_task.delay(incident_id, user_email)

    # Notify the user
    messages.success(
        request, "Restricted report is being generated. Check your email shortly."
    )
    return redirect("home")


def generate_pdf_web(request, incident_id):
    # Fetch incident data based on incident_id
    try:
        incident = MajorIncident.objects.get(pk=incident_id)
    except ObjectDoesNotExist:
        raise ValueError("Incident with ID {} does not exist.".format(incident_id))

    images = get_s3_images_for_incident(incident.image_folder, incident.user_employer)
    context = {"incident": incident, "images": images}
    return render(request, "incident/major_incident_form_pdf.html", context)


def generate_restricted_pdf_web(request, incident_id):
    # Fetch incident data based on incident_id
    try:
        incident = Incident.objects.get(pk=incident_id)
    except ObjectDoesNotExist:
        raise ValueError("Incident with ID {} does not exist.".format(incident_id))

    context = {"incident": incident}
    return render(request, "incident/restricted_incident_form_pdf.html", context)


@login_required
@permission_required(
    "incident.view_incident",
    raise_exception=True,
)
def email_significant_security_report(
    request,
    incident_id,
):
    """
    Queue the Significant Security Incident Report for email
    to the current user.
    """
    incident = get_object_or_404(
        Incident,
        pk=incident_id,
        user_employer=request.user.employer,
    )

    if not request.user.email:
        messages.error(
            request,
            "Your account does not have an email address.",
        )

        return redirect(f"{reverse('incidents')}?tab=edit")

    generate_significant_security_pdf_email_task.delay(
        incident.pk,
        request.user.email,
    )

    messages.success(
        request,
        (
            "The Significant Security Incident Report is being "
            "generated and will be emailed to you."
        ),
    )

    return redirect(f"{reverse('incidents')}?tab=edit")


class IncidentListView(LoginRequiredMixin, View):
    """Old Edit Incident URL. Opens the Incidents hub on the Edit tab."""

    login_url = "/login/"

    def get(self, request, *args, **kwargs):
        return redirect(f"{reverse('incidents')}?tab=edit")


class MajorIncidentListView(PermissionRequiredMixin, ListView):
    model = MajorIncident
    template_name = "incident/major_incident_list.html"
    context_object_name = "incidents"
    permission_required = "incident.view_incident"
    raise_exception = True
    permission_denied_message = "You are not allowed to view incidents."

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["defer_render"] = True
        # Pass defer_render as context to the template
        return context


def Permission_Denied_View(request, exception):
    def get(self, request, exception):
        return render(request, "incident/403.html", status=403)


@login_required
def preview_restricted_incident(request):
    incident = Incident.objects.select_related("store").order_by("-id").first()

    if incident is None:
        raise Http404("No incidents found.")

    return render(
        request,
        "incident/restricted_incident_form_pdf.html",
        {
            "incident": incident,
        },
    )


@login_required
def preview_significant_security_report(request):
    incident = (
        Incident.objects
        .filter(user_employer=request.user.employer)
        .select_related("store", "user_employer")
        .order_by("-id")
        .first()
    )
    if incident is None:
        raise Http404("No incident records were found.")
    return render(
        request,
        "incident/significant_security_incident_report_pdf.html",
        {
            "incident": incident,
        },
    )
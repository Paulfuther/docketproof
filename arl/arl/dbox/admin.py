from django import forms
from django.contrib import admin

from .models import DropboxCredentials


class DropboxCredentialsForm(forms.ModelForm):
    refresh_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the existing refresh token.",
    )
    app_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=(
            "Optional. Leave blank to keep the existing value or use "
            "DROP_BOX_KEY from settings."
        ),
    )
    app_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=(
            "Optional. Leave blank to keep the existing value or use "
            "DROP_BOX_SECRET from settings."
        ),
    )

    class Meta:
        model = DropboxCredentials
        fields = (
            "employer",
            "account_email",
            "is_active",
            "refresh_token",
            "app_key",
            "app_secret",
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        refresh_token = self.cleaned_data.get("refresh_token")
        if refresh_token:
            instance.set_refresh_token(refresh_token)
        app_key = self.cleaned_data.get("app_key")
        if app_key:
            instance.set_app_key(app_key)
        app_secret = self.cleaned_data.get("app_secret")
        if app_secret:
            instance.set_app_secret(app_secret)
        if commit:
            instance.save()
        return instance


@admin.register(DropboxCredentials)
class DropboxCredentialsAdmin(admin.ModelAdmin):
    form = DropboxCredentialsForm
    list_display = (
        "employer",
        "account_email",
        "is_active",
        "has_refresh_token",
        "created_at",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("employer__name", "account_email")
    readonly_fields = ("created_at", "updated_at", "has_refresh_token")
    exclude = (
        "encrypted_refresh_token",
        "encrypted_app_key",
        "encrypted_app_secret",
    )

    def has_refresh_token(self, obj):
        return bool(obj.pk and obj.has_refresh_token())

    has_refresh_token.boolean = True
    has_refresh_token.short_description = "Refresh token set"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("employer")

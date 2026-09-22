from django.utils.crypto import get_random_string

from arl.user.models import NewHireInvite
from arl.setup.models import TenantApiKeys
from arl.msg.helpers import create_master_email


def send_new_hire_invite(new_hire_email, new_hire_name, role, start_date, employer):
    """
    Sends an invitation email to a new hire using SendGrid.

    Args:
        new_hire_email (str): The email address of the new hire.
        new_hire_name (str): The name of the new hire.
        role (str): The role assigned to the new hire.
        start_date (str): The start date of the new hire.
        employer (Employer): The employer object sending the invite.
    """
    try:
        # ✅ Ensure the employer has a verified sender
        # ✅ Retrieve verified sender email from TenantApiKeys
        tenant_api_key = TenantApiKeys.objects.filter(employer=employer).first()
        verified_sender = tenant_api_key.verified_sender_email if tenant_api_key else settings.MAIL_DEFAULT_SENDER
        if not verified_sender:
            print(f"❌ Employer {employer.name} does not have a verified sender email.")
            return False
        print("Verified sender email :", verified_sender)
        # ✅ Check if an invite already exists for this email
        invite, created = NewHireInvite.objects.get_or_create(
            employer=employer,
            email=new_hire_email,
            defaults={
                "name": new_hire_name,
                "role": role,
                "token": get_random_string(64),  # Generate a secure token
                "used": False  # Mark as unused
            }
        )

        # ✅ Ensure we have a token in case the invite already existed
        if not created:
            if not invite.token:
                invite.token = get_random_string(64)
                invite.save(update_fields=["token"])
            invite.refresh_expiry()


        # ✅ Generate the correct invite link
        invite_link = invite.get_invite_link()

        # ✅ Prepare dynamic template data
        template_data = {
            "name": new_hire_name,
            "senior_contact_name": employer.senior_contact_name or "HR Team",
            "company_name": employer.name,
            "role": role,
            "start_date": start_date,
            "invite_link": invite_link,
            "sender_contact_name": employer.senior_contact_name or "HR Team",
        }
        print("Template Data :", template_data)
        # ✅ Call the master email function
        return create_master_email(
            to_email=new_hire_email,
            sendgrid_id="d-88bef48e049c477b83f28764b842c7a2",
            template_data=template_data,
            verified_sender=verified_sender  # Optional, if needed
        )
    
    except Exception as e:
        print(f"🚨 Error sending new hire invite: {e}")
        return False


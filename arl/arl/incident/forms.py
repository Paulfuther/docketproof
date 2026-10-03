import random
import string

from django import forms
from django.utils.text import slugify

from arl.user.models import Store

from .models import Incident, MajorIncident


class IncidentForm(forms.ModelForm):
    # user_employer = forms.ChoiceField(choices=[], required=False)
    image_folder = forms.CharField(widget=forms.TextInput(attrs={'style': 'display:none;'}))

    class Meta:
        model = Incident
        fields = "__all__"
        exclude = ['queued_for_sending', 'sent', 'sent_at']
        labels = {
            "contractor_involved": "Contractor Involved in Incident?",
            "contractor_company_name": "Contractor Company Name",
            "syes": "Off Property Impact: Yes",
            "sno": "Off Property Impact: No",
            "scomment": "Comment",
            "othertext": "Comment",
            "ryes": "Regulatory Authorities Called: Yes",
            "rno": "Regulatory Authorities Called: No",
            "rna": "Does Not Apply",
            "rcomment": "Comment",
            "chemcomment": "Comment",
            "actionstaken": "Actions Taken",
            "correctiveactions": "Corrective Actions Taken",
            "volumerelease": "Volume Released",
            "sother": "Other: Yes",
            "s2comment": "Comment",
            "pyes": "Police Called: Yes",
            "pno": "Poice Called: No",
            "pna": "Does Not Apply",
            "pcase": "Police Report Number",
            "stolentransactions": "Stolen Transactions",
            "stoltransactions": "Dollar Amount of Stolen Product",
            "stolencards": "Stolen Cards",
            "stolcards": "Dollar Amount of Stolen Cards",
            "stolentobacco": "Stolen Tobacco",
            "stoltobacco": "Dollar Amount of Stolen Tobacco",
            "stolenlottery": "Stolen Lottery",
            "stollottery": "Dollar Amount of Stolen Lottery",
            "stolenfuel": "Stolen Fuel",
            "stolfuel": "Dollar Amount of Stolen Fuel",
            "stolenother": "Other",
            "stolother": "Description of Other",
            "stolenothervalue": "Dollar Amount of Other",
            "stolenna": "Information Not Available",
            # Significant Security Incident Type
            "security_robbery": "Robbery",
            "security_break_and_enter": "Break & Enter",
            "security_assault": "Assault",
            "security_bomb_threat": "Bomb Threat",
            "security_major_fire_explosion": "Major Fire or Explosion",
            "security_fatality": "Fatality",
            "security_critical_injury": "Critical Injury",

            # Police Information
            "police_attended": "Did Police Attend the Site?",
            "police_agency": "Police Agency",
            "police_officer_name": "Officer Name",
            "police_officer_rank": "Officer Rank",
            "police_officer_badge_number": "Officer Badge Number",

            # GSOC
            "gsoc_called": "Was GSOC Called Once Safe to Do So?",

            # Theft and damage
            "theft_cash": "Cash",
            "theft_cash_value": "Cash Value",
            "damage_value": "Estimated Property Damage Value",

            # Suspect and vehicle
            "suspect_age": "Approximate Age",
            "clothing_description": "Clothing Description",
            "vehicle_year": "Approximate Vehicle Year",
            "vehicle_distinguishing_features": "Vehicle Distinguishing Features",
        }
        widgets = {
            "causalfactors": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "",
                }
            ),
            "determincauses": forms.Textarea(
    attrs={
        "class": "form-control",
        "rows": 4,
        "placeholder": "",
    }
),
           "preventiveactions": forms.Textarea(
    attrs={
        "class": "form-control",
        "rows": 5,
        "placeholder": "",
    }
),
            "eventtimeline": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "What happened? Please provide the event facts in a detailed timeline.",
                    "style": "font-weight:300; font-style:italic; color:#666;",
                }
            ),
        }

    eventdate = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    eventtime = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))

    def generate_random_folder(self):
        return "".join(random.choices(string.ascii_letters + string.digits,
                                      k=10))

    def create_folder(self, instance):
        if not instance.image_folder:
            # Generate a slug from a descriptive field in your model
            # (e.g., title)
            slug = slugify(instance.brief_description)[:50]  # Limit the slug
            # length
            # Generate a random string for additional uniqueness
            random_string = self.generate_random_folder()
            # Combine the slug and random string to create a folder name
            folder_name = f"{slug}-{random_string}"
            # Set the folder name as the initial value for the image_folder
            # field
            instance.image_folder = folder_name

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)  # Get the user from the kwargs
        super().__init__(*args, **kwargs)
        self.fields["causalfactors"].required = True
        self.fields["determincauses"].required = True
        self.fields["preventiveactions"].required = True

        # Check if it's a new form (not an update)
        if self.instance.pk is None:
            # Set the initial value for image_folder
            self.create_folder(self.instance)
            self.fields["image_folder"].initial = self.instance.image_folder

        if user:
            self.fields["user_employer"].initial = self.get_user_employer(user)
            self.fields["user_employer"].disabled = True
            if user.employer_id:
                self.fields["store"].queryset = Store.objects.filter(
                    employer=user.employer
                ).order_by("number")
                self.fields["store"].empty_label = "Select a store…"

    def get_user_employer(self, user):
        employer = user.employer
        return employer


class MajorIncidentForm(forms.ModelForm):
    # user_employer = forms.ChoiceField(choices=[], required=False)
    image_folder = forms.CharField(widget=forms.TextInput(attrs={'style': 'display:none;'}))

    class Meta:
        model = MajorIncident
        fields = "__all__"
        labels = {
            "policeagency": "Police Agency (eg. RCMP, OPP, etc)",
            "officerdetails": "Name Rank Badge etc.",
            "policecalledyes": "Police Called: Yes",
            "policecalledno": "Poice Called: No",
            "policeattendyes": "Police Attend Yes",
            "policeattendno": "Police Attend No",
            "policefilenumber": "Police File Number",
            "gsoccalledyes": "GSOC called Yes",
            "gsoccalledno": "GSOC called no",
            "stolentransactions": "Stolen Transactions",
            "stoltransactions": "Dollar Amount of Stolen Product",
            "stolencards": "Stolen Cards",
            "stolcards": "Dollar Amount of Stolen Cards",
            "stolentobacco": "Stolen Tobacco",
            "stoltobacco": "Dollar Amount of Stolen Tobacco",
            "stolenlottery": "Stolen Lottery",
            "stollottery": "Dollar Amount of Stolen Lottery",
            "stolenfuel": "Stolen Fuel",
            "stolfuel": "Dollar Amount of Stolen Fuel",
            "stolenother": "Other",
            "stolother": "Description and Value of Other",
            "stolenna": "N/A Information Not Available",
            "distinguishablefeatures": "Distinguishable Features",
            "licenceplatenumber": "Licence Plate Number",
            "approximateyearmakemodel": "Approximate Year, Make & Model",
            "direction": "Direction of Travel",
            "bumpersticker": "Bumper Sticker",
            "damagetoproperty": "Damage details and value",
            "eyeeyeglasses": "Eye Colour/ Glasses",
        }

    eventdate = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    eventtime = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))

    def generate_random_folder(self):
        return "".join(random.choices(string.ascii_letters + string.digits,
                                      k=10))

    def create_folder(self, instance):
        if not instance.image_folder:
            # Generate a slug from a descriptive field in your model
            # (e.g., title)
            slug = slugify(instance.brief_description)[:50]  # Limit the slug
            # length
            # Generate a random string for additional uniqueness
            random_string = self.generate_random_folder()
            # Combine the slug and random string to create a folder name
            folder_name = f"{slug}-{random_string}"
            # Set the folder name as the initial value for the image_folder
            # field
            instance.image_folder = folder_name

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)  # Get the user from the kwargs
        super().__init__(*args, **kwargs)

        # Check if it's a new form (not an update)
        if self.instance.pk is None:
            # Set the initial value for image_folder
            self.create_folder(self.instance)
            self.fields["image_folder"].initial = self.instance.image_folder

        if user:
            self.fields["user_employer"].initial = self.get_user_employer(user)
            # Disable the user_employer field and set its initial value
            self.fields['user_employer'].disabled = True
            #self.fields['user_employer'].initial = self.get_user_employer(user)
            #self.fields['user_employer'].widget.attrs['disabled'] = 'disabled'

    def get_user_employer(self, user):
        employer = user.employer
        return employer

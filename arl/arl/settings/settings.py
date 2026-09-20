import os
from pathlib import Path
import logging
from dotenv import load_dotenv
from django.contrib.messages import constants as messages
import socket

load_dotenv()
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
# print("BASE_DIR:", BASE_DIR)

logger = logging.getLogger('django')
# logger.error("TEST LOGGING: This is a test error log.")

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("SECRET_KEY")


# SECURITY WARNING: don't run with debug turned on in production!
# DEBUG = os.environ.get('DEBUG') == 'True'

DEBUG = True

ALLOWED_HOSTS = ["*"]
# Trust the HTTPS origin for CSRF (must include scheme)
CSRF_TRUSTED_ORIGINS = [
    "https://*.ngrok-free.app",
    "https://*.ngrok.app",
    "https://*.ngrok.io",
]
ADMINS = [
    ("Paul Futher", "paul.futher@gmail.com"),
    # Add more admins if needed
]


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',  # Change to INFO or higher (WARNING, ERROR, CRITICAL)
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'django.log'), # Rename for clarity
            'formatter': 'verbose',
        },
        'console': {  # Add a console handler for immediate feedback during development
            'level': 'DEBUG', # Keep console at debug for development
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file', 'console'], # Use both file and console
            'level': 'ERROR', # Change to INFO or higher
            'propagate': True,
        },
            'django.template': {
            'handlers': ['file'],
            'level': 'ERROR',
            'propagate': False,
        },
        '': { # Catch-all logger for other applications
            'handlers': ['file', 'console'],
            'level': 'INFO', # Change to INFO or higher
        },
        'my_app_name': {  # Example for a specific app
            'handlers': ['file', 'console'],
            'level': 'INFO', # Or a more specific level for your app
            'propagate': True,
        },
        'celery': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}


INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "waffle",
    "arl.blog",
    "django_htmx",
    "taggit",
    "arl.user",
    "intl_tel_input",
    "crispy_forms",
    "crispy_bootstrap4",
    "arl.msg",
    "arl.quiz",
    "django_celery_results",
    "django_countries",
    "import_export",
    "flower",
    "django_celery_beat",
    "arl.dsign",
    "arl.dbox",
    "arl.incident",
    "arl.bucket",
    "arl.payroll",
    "arl.carwash",
    "arl.setup",
    "phonenumber_field",
    "arl.helpdesk",
    'django_otp',
    'django_otp.plugins.otp_totp',
    'arl.reclose',
    'arl.utils',
    'arl.stores',
    'arl.documentflow',
    'arl.recruit',
    'arl.sales_targets',
]

PHONENUMBER_DEFAULT_REGION = 'CA'

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "arl.user.middleware.ErrorLoggingMiddleware",
    "waffle.middleware.WaffleMiddleware",
    'django_otp.middleware.OTPMiddleware',
]

ROOT_URLCONF = "arl.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
            "builtins": ["arl.blog.templatetags.tag_cloud"],
        },
    },
]

WSGI_APPLICATION = "arl.wsgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME"),
        "USER": os.environ.get("DB_USER"),
        "PASSWORD": os.environ.get("DB_PASSWORD"),
        "HOST": os.environ.get("DB_HOST"),
        'PORT': os.environ.get("DB_PORT"),
    }
}


# This should match one of the keys in EXPLORER_CONNECTIONS


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "America/New_York"

USE_TZ = True
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000")
# print("site ure :", SITE_URL)
#if socket.gethostname() == "":
#    SITE_URL = "https://www.1553690ontarioinc.com"
#else:
#    SITE_URL = "http://127.0.0.1:8000"  # Local development
# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = "/var/www/static"
# STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
#STATICFILES_DIRS = [BASE_DIR / "static"]
# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATICFILES_DIRS = [BASE_DIR / "static"]

AUTH_USER_MODEL = "user.CustomUser"

CRISPY_TEMPLATE_PACK = "bootstrap4"

CELERY_RESULT_BACKEND = "django-db"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SERIALIZER = 'json'

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "/login/"

BROKER_URL = os.environ.get("CLOUDAMQP_URL")
#BROKER_URL =""

EMAIL_BACKEND = "arl.msg.helpers.SendGridEmailBackend"

DATA_UPLOAD_MAX_MEMORY_SIZE = 50485760  # 10MB in bytes
SECRET_ENCRYPTION_KEY = os.environ.get("SECRET_ENCRYPTION_KEY")

DOCUSIGN_BASE_PATH = os.environ.get("DOCUSIGN_BASE_PATH_DEV")
DOCUSIGN_INTEGRATION_KEY = os.environ.get("DOCUSIGN_INTEGRATION_KEY_DEV")
DOCUSIGN_USER_ID = os.environ.get("DOCUSIGN_USER_ID_DEV")
DOCUSIGN_ACCOUNT_ID = os.environ.get("DOCUSIGN_ACCOUNT_ID_DEV")

DOCUSIGN_API_CLIENT_HOST = os.environ.get("DOCUSIGN_API_CLIENT_HOST_DEV")
DOCUSIGN_TEMPLATE_ID = "e02ea9a2-42d4-453f-bd64-8480b9a2dae4"  # SHORTER TEST DOC
DOCUSIGN_TEMPLATE_ID = "1f4599d9-689a-496d-8a22-24c52529780d"
DOCUSIGN_PRIVATE_KEY = "/Users/paulfuther/Documents/GitHub/Django-arl/privatedev.key"
DOCUSIGN_OAUTH_HOST_NAME = os.environ.get("DOCUSIGN_OAUTH_HOST_NAME_DEV")

# DOCUSIGN_INTEGRATION_KEY = os.environ.get("DOCUSIGN_INTEGRATION_KEY")
# NEW_HIRE_DATA_EMAIL = ["paul.futher@gmail.com", "hr1553690@yahoo.com"]
# DOCUSIGN_ACCOUNT_ID = os.environ.get("DOCUSIGN_ACCOUNT_ID")
# DOCUSIGN_USER_ID = os.environ.get("DOCUSIGN_USER_ID")
# DOCUSIGN_API_CLIENT_HOST = os.environ.get("DOCUSIGN_API_CLIENT_HOST")
# DOCUSIGN_OAUTH_HOST_NAME = os.environ.get("DOCUSIGN_OAUTH_HOST_NAME")
# DOCUSIGN_BASE_PATH = os.environ.get("DOCUSIGN_BASE_PATH")
# DOCUSIGN_TEST_TEMPLATE_ID = "f34f7bd5-c31a-44ae-941c-d16b8ec809c8"
# DOCUSIGN_TEMPLATE_ID = "b8ba5457-9257-491a-9729-d6e66bd78a2e"
# DOCUSIGN_PRIVATE_KEY = "/Users/paulfuther/Documents/GitHub/Django-arl/private.key"


DROP_BOX_SHORT_TOKEN = os.environ.get("DROP_BOX_SHORT_TOKEN")
DROP_BOX_KEY = os.environ.get("DROP_BOX_KEY")
DROP_BOX_SECRET = os.environ.get("DROP_BOX_SECRET")
DROPBOX_REDIRECT_URI = "http://localhost:8000/dropbox/callback/"
DROP_BOX_REFRESH_TOKEN = os.environ.get("DROP_BOX_REFRESH_TOKEN")
DROP_BOX_AUTHORIZATION_CODE = os.environ.get("DROP_BOX_AUTHORIZATION_CODE")

LINODE_ACCESS_KEY = os.environ.get("LINODE_BUCKET_ACCESS_KEY")
LINODE_SECRET_KEY = os.environ.get("LINODE_BUCKET_SECRET_KEY")
LINODE_NAME = os.environ.get("LINODE_BUCKET_NAME")
LINODE_URL = os.environ.get("LINODE_BUCKET_URL")
LINODE_REGION = os.environ.get("LINODE_REGION")
LINODE_BUCKET_NAME = os.environ.get("LINODE_BUCKET_NAME")
LINODE_S3_POSTGRES_FOLDER = os.environ.get("LINODE_S3_POSTGRES_FOLDER")

# twillio variables

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM")
TWILIO_VERIFY_SID = os.environ.get("TWILIO_VERIFY_SID")
TWILIO_NOTIFY_SERVICE_SID = os.environ.get("TWILIO_NOTIFY_SERVICE_SID")
MY_TEST_PHONE_NUMBER = os.environ.get("ADMIN_PHONE_NUMBER")

MESSAGE_SERVICE_SID = os.environ.get("MESSAGE_SERVICE_SID")

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")
SENDGRID_NEWHIRE_ID = os.environ.get("SENDGRID_NEWHIRE_ID")
SENDGRID_NEW_HIRE_FILE_ID = os.environ.get("SENDGRID_NEW_HIRE_FILE_ID")
SENDGRID_SENDER_VERIFICATION_URL = os.environ.get("SENDGRID_SENDER_VERIFICATION_URL")
SENDGRID_EMPLOYER_REGISTER_AS_USER = os.environ.get("SENDGRID_EMPLOYER_REGISTER_AS_USER")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = "smtp.sendgrid.net"
EMAIL_HOST_USER = "apikey"  # this is exactly the value 'apikey'
EMAIL_HOST_PASSWORD = SENDGRID_API_KEY
EMAIL_PORT = 587
EMAIL_USE_TLS = True


BACKUP_FILE_PATH = os.environ.get("BACKUP_FILE_PATH_DEV")
BACKUP_DUMP_PATH = os.environ.get("BACKUP_DUMP_PATH_DEV")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY_DEV")
STRIPE_WEBHOOK_SECRET = "whsec_MLSUcBWyIC20anxyl7wSWarBqjyMCoSH"
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
# STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
# STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")

# BASE URL DEV
BASE_URL = "https://81d2723ffa9e.ngrok-free.app"
SITE_URL = BASE_URL
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")
ADMIN_PHONE_NUMBER = os.environ.get("ADMIN_PHONE_NUMBER")
MESSAGE_TAGS = {
    messages.ERROR: 'danger',  # ✅ this converts 'error' → 'danger' for Bootstrap styling
}

# cryptography

FERNET_PRIMARY_KEY = os.environ.get("FERNET_PRIMARY_KEY")          # 32-byte urlsafe base64
FERNET_OLD_KEYS = [k for k in os.environ.get("FERNET_OLD_KEYS", "").split(",") if k]
SIN_HASH_SALT = os.environ.get("SIN_HASH_SALT")


# print("site url:", SITE_URL)
# print("Twilio Message Service Sid :", MESSAGE_SERVICE_SID)

# settings.py (only for local dev!)
# SESSION_COOKIE_SAMESITE = None
# SESSION_COOKIE_SECURE = False
# CSRF_COOKIE_SAMESITE = None
# CSRF_COOKIE_SECURE = False
# CSRF_TRUSTED_ORIGINS = [
#    "https://731d-2607-fea8-2840-b200-a520-f762-b4dc-93b5.ngrok-free.app",
# ]
# CSRF_TRUSTED_ORIGINS = [
#    "https://*.ngrok-free.app",
# ]

try:
    from .local_settings import *
except ImportError:
    pass

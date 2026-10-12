import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

# Always defaults to false if not explicitly set
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

SECRET_KEY = os.getenv('SECRET_KEY')

# If DEBUG is false, ensure SECRET_KEY is present, not empty, and not the placeholder
if not DEBUG and (not SECRET_KEY or SECRET_KEY == "replace-with-a-long-random-secret"):
    raise RuntimeError("Generate and set a random SECRET_KEY.")


env_allowed_hosts = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

if DEBUG:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"] + env_allowed_hosts
else:
    ALLOWED_HOSTS = env_allowed_hosts



INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Custom Apps
    "travel",
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'travel.middleware.PrivateApiMiddleware',
]


ROOT_URLCONF = 'config.urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]
WSGI_APPLICATION = 'config.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv('DB_PATH', BASE_DIR / 'db.sqlite3'),
        'OPTIONS': {
            'timeout': 20,
        },
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# =============================================================================
# FRONTEND, CSRF & SECURITY CONFIGURATION
# =============================================================================
# Configure frontend integration, trusted CSRF origins, secure cookies,
# HTTP security headers, and request upload limits.
FRONTEND_URL = os.environ['FRONTEND_URL'].rstrip('/')
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ['CSRF_TRUSTED_ORIGINS'].split(',')
    if origin.strip()
]

if not CSRF_TRUSTED_ORIGINS:
    raise ValueError("CSRF_TRUSTED_ORIGINS must contain at least one valid origin.")

CSRF_FAILURE_VIEW = 'travel.views.csrf_failure'


# =============================================================================
# SESSION, COOKIE & HTTP SECURITY
# =============================================================================
# Protect session and CSRF cookies in production, restrict referrer
# information, and rely on Django's defaults for other security headers.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_REFERRER_POLICY = 'no-referrer'



# =============================================================================
# EMAIL NOTIFICATION CONFIGURATION
# =============================================================================
# The application uses the internal Notification Service API to send emails,
# including emails with attachments (e.g., flight tickets and travel documents).
# AWS SQS can optionally be configured for emails without attachments.
#
# Modes: 'console' logs notifications to the terminal; 'live' sends actual emails.
NOTIFICATION_MODE = os.getenv('NOTIFICATION_MODE', 'console')

# When enabled, routes all emails through the Notification Service API and does not involve aws SQS - (defult = false)
NOTIFICATION_USE_API_FOR_ALL_EMAILS = os.getenv('NOTIFICATION_USE_API_FOR_ALL_EMAILS', 'false').strip().lower() == 'true' #

# Internal Notification Service API configuration.
NOTIFICATION_API_URL = os.getenv('NOTIFICATION_API_URL', '').strip().rstrip('/')
NOTIFICATION_API_TOKEN = os.getenv('NOTIFICATION_API_TOKEN', '')


# =============================================================================
# AWS SQS EMAIL NOTIFICATIONS
# =============================================================================
# Ideally, emails without attatchments should use SQS and not sent via the api, thats why NOTIFICATION_USE_API_FOR_ALL_EMAILS is false by default
# The internal Notification Service API is best suited for emails with attachments, although it can also send email without attachments.
# AWS SQS queues email notifications thus best for scalability.

NOTIFICATION_AWS_REGION = os.getenv('AWS_REGION', '')
NOTIFICATION_SQS_QUEUE_NAME = os.getenv('NOTIFICATION_SQS_QUEUE_NAME', 'notifications-email-queue')
NOTIFICATION_SQS_QUEUE_URL = os.getenv('NOTIFICATION_SQS_QUEUE_URL', '')

# Admin email addresses receive travel booking notifications. (to be removed later as the db is the source of truth)
TRAVEL_ADMIN_EMAILS = [address.strip() for address in os.getenv('TRAVEL_ADMIN_EMAILS', '').split(',') if address.strip()]

# Apa Logo
APA_LOGO_URL = 'https://www.apainsurance.org/images/AppResources/Apollo_Logo.png'
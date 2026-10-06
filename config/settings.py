import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
DEBUG = os.getenv('DEBUG', 'true').lower() == 'true'
SECRET_KEY = os.getenv('SECRET_KEY', 'development-only-change-before-deployment')
if not DEBUG and SECRET_KEY == 'development-only-change-before-deployment':
    raise RuntimeError('Set SECRET_KEY before deploying.')
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'travel']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware', 'travel.middleware.PrivateApiMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request',
                          'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.getenv('DB_PATH', BASE_DIR / 'db.sqlite3'), 'OPTIONS': {'timeout': 20}}}
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
                           {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}]
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:5173').rstrip('/')
CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:5174,http://localhost:5174').split(',')
CSRF_FAILURE_VIEW = 'travel.views.csrf_failure'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'no-referrer'
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 100_000
NOTIFICATION_MODE = os.getenv('NOTIFICATION_MODE', 'console')
NOTIFICATION_USE_API_FOR_ALL_EMAILS = os.getenv('NOTIFICATION_USE_API_FOR_ALL_EMAILS', 'false').strip().lower() == 'true'
NOTIFICATION_API_URL = os.getenv('NOTIFICATION_API_URL', 'http://internal-apis-services-alb-1438949379.eu-west-1.elb.amazonaws.com').strip().rstrip('/')
NOTIFICATION_API_TOKEN = os.getenv('NOTIFICATION_API_TOKEN', '')

# Email content is rendered by Django before delivery.
NOTIFICATION_AWS_REGION = os.getenv('AWS_REGION', os.getenv('AWS_DEFAULT_REGION', ''))
NOTIFICATION_SQS_QUEUE_NAME = os.getenv('NOTIFICATION_SQS_QUEUE_NAME', 'notifications-email-queue')
NOTIFICATION_SQS_QUEUE_URL = os.getenv('NOTIFICATION_SQS_QUEUE_URL', '')
TRAVEL_ADMIN_EMAILS = [address.strip() for address in os.getenv('TRAVEL_ADMIN_EMAILS', '').split(',') if address.strip()]
EMAIL_LOGO_URL = os.getenv('EMAIL_LOGO_URL', 'https://www.apainsurance.org/images/AppResources/Apollo_Logo.png')

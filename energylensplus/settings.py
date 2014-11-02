"""
Django settings for energylensplus project.

For more information on this file, see
https://docs.djangoproject.com/en/1.6/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/1.6/ref/settings/
"""

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
import os
BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/1.6/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = '400o5=c!o9^5rynz@!ve9n%qiqii5p2quxy4x61$6jo#d2$6_f'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

TEMPLATE_DEBUG = True

ALLOWED_HOSTS = ['.energy.iiitd.edu.in', '.192.168.1.101', '192.168.20.217']


# Application definition

INSTALLED_APPS = (
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'energylenserver',
)

MIDDLEWARE_CLASSES = (
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
)

ROOT_URLCONF = 'energylensplus.urls'

WSGI_APPLICATION = 'energylensplus.wsgi.application'


# Database
# https://docs.djangoproject.com/en/1.6/ref/settings/#databases

db_host = '192.168.1.38'
db_user = 'manaswi'
db_pass = 'research'

# Local machine settings
# db_user = 'root'
# db_host = '127.0.0.1'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'energylensplus',
        'USER': db_user,
        'HOST': db_host,
        'PASSWORD': db_pass,
        'OPTIONS': {
            'local_infile': 1,
        },
        'CONN_MAX_AGE': None
    }
}

# Celery Settings
CELERY_RESULT_BACKEND = ('db+mysqldb://' + db_user +
                         ':' + db_pass + '@' + db_host + '/celery_results')
CELERY_ACCEPT_CONTENT = ['pickle', 'json']
CELERY_TASK_RESULT_EXPIRES = 7200


'''
from datetime import timedelta
CELERYBEAT_SCHEDULE = {
    'send-report-every-hour': {
        'task': 'tasks.send_validation_report',
        'schedule': timedelta(seconds=60 * 60),
    },
    # 'send-notification-every-hour': {
    #     'task': 'energylenserver.tasks.send_wastage_notification',
    #     'schedule': timedelta(seconds=60 * 45),
    # },
}
'''

# Logger Settings
LOGGING = {
    'version': 1,
    'formatters':
    {
        'verbose': {
            'format': "[%(asctime)s] %(levelname)s [%(module)s:%(lineno)s] %(message)s",
            'datefmt': "%d/%b/%Y %H:%M:%S"
        },
        'simple': {
            'format': '[%(asctime)s] %(levelname)s [%(module)s:%(lineno)s] %(message)s',
            'datefmt': "%d/%b/%Y %H:%M:%S"
        },
    },
    'handlers': {
        'file': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs/django.log'),
            'formatter': 'simple'
        },
        'main_django': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs/energylens.log'),
            'formatter': 'simple'
        },
        'gcmserver': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs/gcmserver.log'),
            'formatter': 'simple'
        },
        'meter_data': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs/meter_data.log'),
            'formatter': 'simple'
        },
        'error': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs/error.log'),
            'formatter': 'simple'
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['file'],
            'propagate': True,
            'level': 'DEBUG',
        },
        'energylensplus_django': {
            'handlers': ['main_django'],
            'propagate': True,
            'level': 'DEBUG',
        },
        'energylensplus_gcm': {
            'handlers': ['gcmserver'],
            'propagate': True,
            'level': 'DEBUG',
        },
        'energylensplus_meterdata': {
            'handlers': ['meter_data'],
            'propagate': True,
            'level': 'DEBUG',
        },
        'energylensplus_error': {
            'handlers': ['error'],
            'level': 'ERROR',
        },
        'energylenserver': {
            'handlers': ['file'],
            'level': 'DEBUG',
        },
    }
}

# For File Handling
MEDIA_ROOT = os.path.join(BASE_DIR, 'energylenserver/tmp/')
# FILE_UPLOAD_HANDLERS = ("django.core.files.uploadhandler.TemporaryFileUploadHandler",)

# Internationalization
# https://docs.djangoproject.com/en/1.6/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kolkata'

# USE_I18N = False

# USE_L10N = True

USE_TZ = False


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.6/howto/static-files/

STATIC_URL = '/static/'

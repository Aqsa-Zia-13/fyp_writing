import os
from pathlib import Path
from urllib.parse import unquote, urlparse

env_file = Path(__file__).resolve().parents[2] / 'fyp_root' / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'writing-development-key')
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '*']
ROOT_URLCONF = 'writing_service.urls'
INSTALLED_APPS = ['writing_service.apps.WritingServiceConfig']
MIDDLEWARE = []
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

DATABASE_URL = os.getenv('DATABASE_URL', '')
if DATABASE_URL:
    database = urlparse(DATABASE_URL)
    if database.scheme not in ('postgres', 'postgresql'):
        raise RuntimeError('DATABASE_URL must be a PostgreSQL URL.')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': unquote(database.path.lstrip('/')),
            'USER': unquote(database.username or ''),
            'PASSWORD': unquote(database.password or ''),
            'HOST': database.hostname,
            'PORT': str(database.port or 5432),
            'OPTIONS': {'sslmode': 'require'},
            'CONN_MAX_AGE': 0,
        }
    }

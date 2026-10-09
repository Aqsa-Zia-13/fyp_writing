import os
from pathlib import Path

env_file = Path(__file__).resolve().parents[2] / 'fyp_root' / '.env'
if not os.environ.get('DATABASE_URL') and env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'writing-development-key')
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '*']
ROOT_URLCONF = 'writing_service.urls'
INSTALLED_APPS = []
MIDDLEWARE = []
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

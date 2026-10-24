"""Database helpers that retain the gateway user's Supabase RLS identity."""
import json
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.db import connection, transaction


@contextmanager
def user_database_context(user_id):
    """Run ORM queries with the authenticated role and claims used by RLS."""
    if not settings.DATABASE_URL:
        raise RuntimeError('DATABASE_URL is not configured for fyp_writing.')
    subject = str(uuid.UUID(str(user_id)))
    with transaction.atomic(using='default'):
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL ROLE authenticated')
            claims = json.dumps({'sub': subject, 'role': 'authenticated'})
            cursor.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", [subject])
            cursor.execute("SELECT set_config('request.jwt.claim.role', %s, true)", ['authenticated'])
            cursor.execute("SELECT set_config('request.jwt.claims', %s, true)", [claims])
            cursor.execute("SELECT set_config('app.user_id', %s, true)", [subject])
        yield

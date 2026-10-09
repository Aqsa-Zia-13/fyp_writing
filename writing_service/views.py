import json
import os
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


def _connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL is not configured for fyp_writing.')
    parsed = urlparse(database_url)
    return psycopg.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        dbname=parsed.path.lstrip('/'), user=parsed.username,
        password=parsed.password, sslmode='require', connect_timeout=5,
    )


def _columns(cursor, table):
    cursor.execute(
        'SELECT column_name FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = %s', (table,)
    )
    return {row[0] for row in cursor.fetchall()}


def _table(cursor, names):
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s)', (list(names),)
    )
    found = {row[0] for row in cursor.fetchall()}
    return next((name for name in names if name in found), None)


def _first(columns, candidates):
    return next((name for name in candidates if name in columns), None)


def _error(message, status=400):
    return JsonResponse({'detail': message}, status=status)


def _database_error():
    return _error('Writing database is unavailable or its tables are not configured.', 503)


@require_GET
def task_detail(request, task_number):
    try:
        with _connection() as connection, connection.cursor() as cursor:
            table = _table(cursor, ('wr_writing_tasks', 'writing_tasks', 'writing_task'))
            if not table:
                return _error('Writing task table was not found.', 503)
            columns = _columns(cursor, table)
            prompt = _first(columns, ('prompt_text', 'prompt', 'task_prompt', 'description'))
            number = _first(columns, ('task_number', 'task_type', 'task_id', 'id'))
            first_set = _first(columns, ('test_id', 'id', 'task_id'))
            if not prompt or not number:
                return _error('writing_tasks needs a task_number and prompt_text column.', 503)
            cursor.execute(
                sql.SQL('SELECT {prompt} FROM {table} WHERE lower({number}::text) IN (%s, %s, %s, %s, %s) ORDER BY {order_column} ASC LIMIT 1').format(
                    prompt=sql.Identifier(prompt), table=sql.Identifier(table),
                    number=sql.Identifier(number), order_column=sql.Identifier(first_set or number)),
                (str(task_number), f'task_{task_number}', f'task {task_number}',
                 f'task{task_number}', f'academic task {task_number}'),
            )
            row = cursor.fetchone()
            if not row:
                return _error('No prompt was found for this writing task.', 404)
            duration = 20 * 60 if task_number == 1 else 40 * 60
            return JsonResponse({'task_number': task_number, 'prompt': row[0], 'duration_seconds': duration})
    except (psycopg.Error, RuntimeError):
        return _database_error()


@csrf_exempt
@require_POST
def submit_response(request):
    try:
        payload = json.loads(request.body or '{}')
    except (ValueError, UnicodeDecodeError):
        return _error('Request body must be valid JSON.')
    task_number = payload.get('task_number')
    response = payload.get('response', '')
    if str(task_number) not in ('1', '2'):
        return _error('task_number must be 1 or 2.')
    if not isinstance(response, str) or not response.strip():
        return _error('Response must not be empty.')
    count = len(response.split())
    if not 150 <= count <= 250:
        return _error('Response must contain between 150 and 250 words.', 422)
    try:
        with _connection() as connection, connection.cursor() as cursor:
            task_table = _table(cursor, ('wr_writing_tasks', 'writing_tasks', 'writing_task'))
            response_table = _table(cursor, ('writing_responses', 'writing_response'))
            if task_table and not response_table:
                cursor.execute(
                    'CREATE TABLE writing_responses ('
                    'id uuid PRIMARY KEY DEFAULT gen_random_uuid(), '
                    'task_id text, user_id text, response_text text NOT NULL, '
                    'word_count integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now())'
                )
                response_table = 'writing_responses'
            tasks = _columns(cursor, task_table) if task_table else set()
            responses = _columns(cursor, response_table) if response_table else set()
            task_number_column = _first(tasks, ('task_number', 'task_type', 'task_id', 'id'))
            task_id_column = _first(tasks, ('id', 'task_id')) or task_number_column
            first_set = _first(tasks, ('test_id', 'id', 'task_id'))
            if not task_number_column or not responses:
                return _error('writing_tasks and writing_responses tables must exist.', 503)
            cursor.execute(
                sql.SQL('SELECT {id} FROM {table} WHERE lower({number}::text) IN (%s, %s, %s, %s, %s) ORDER BY {order_column} ASC LIMIT 1').format(
                    id=sql.Identifier(task_id_column), table=sql.Identifier(task_table),
                    number=sql.Identifier(task_number_column), order_column=sql.Identifier(first_set or task_number_column)),
                (str(task_number), f'task_{task_number}', f'task {task_number}',
                 f'task{task_number}', f'academic task {task_number}'),
            )
            task = cursor.fetchone()
            if not task:
                return _error('No prompt was found for this writing task.', 404)
            values = {}
            for candidates, value in (
                (('task_id', 'writing_task_id'), str(task[0])),
                (('user_id',), request.headers.get('X-User-Id')),
                (('response_text', 'response', 'answer', 'content'), response),
                (('word_count',), count),
            ):
                column = _first(responses, candidates)
                if column:
                    values[column] = value
            if not any(key in values for key in ('response_text', 'response', 'answer', 'content')):
                return _error('writing_responses needs a response_text column.', 503)
            response_id = _first(responses, ('id', 'response_id'))
            if not response_id:
                return _error('writing_responses must have an id or response_id column.', 503)
            statement = sql.SQL('INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING {id}').format(
                table=sql.Identifier(response_table),
                columns=sql.SQL(', ').join(map(sql.Identifier, values)),
                values=sql.SQL(', ').join(sql.Placeholder() for _ in values),
                id=sql.Identifier(response_id),
            )
            cursor.execute(statement, list(values.values()))
            return JsonResponse({'response_id': str(cursor.fetchone()[0]), 'word_count': count, 'status': 'submitted'}, status=201)
    except (psycopg.Error, RuntimeError):
        return _database_error()

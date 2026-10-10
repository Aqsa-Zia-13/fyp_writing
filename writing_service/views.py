import json
import hashlib
import os
import random
import re
from urllib.parse import urlparse
from pathlib import Path

import psycopg
from psycopg import sql
from .scoring import CRITERIA, ScoringError, calculate_overall_band_score, evaluate_response
from .scores import load_saved_evaluation, persist_evaluation
from django.core import signing
from django.http import FileResponse, JsonResponse
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


def _column_types(cursor, table):
    cursor.execute(
        'SELECT column_name, data_type, udt_name FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = %s', (table,)
    )
    return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


def _table(cursor, names):
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s)', (list(names),)
    )
    found = {row[0] for row in cursor.fetchall()}
    return next((name for name in names if name in found), None)


def _first(columns, candidates):
    return next((name for name in candidates if name in columns), None)


def _prompt_id(row, prompt_column, identifier):
    if identifier and row.get(identifier) is not None:
        return str(row[identifier])
    return hashlib.sha256(str(row.get(prompt_column, '')).encode('utf-8')).hexdigest()


def _placeholder_for(column, types):
    data_type, udt_name = types.get(column, ('', ''))
    if udt_name == 'uuid':
        return sql.SQL('%s::uuid')
    return sql.Placeholder()


def _submission_key(task_number, task_token, response, user_id):
    raw = '\0'.join((str(user_id or ''), str(task_number), task_token, response))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _error(message, status=400):
    return JsonResponse({'detail': message}, status=status)


def _database_error():
    return _error('Writing database is unavailable or its tables are not configured.', 503)


def _task_image(values):
    """Match Cambridge IELTS prompt metadata/text to the bundled scan names."""
    text = ' '.join(str(value) for value in values if value is not None)
    match = re.search(r'ielts\D{0,8}(8|9|10|11|12)\D{0,12}test\D{0,4}([1-4])', text, re.I)
    if not match:
        match = re.search(r'(8|9|10|11|12)\D{0,4}test\D{0,4}([1-4])', text, re.I)
    if not match:
        return None
    filename = f'ielts-{match.group(1)}-test-{match.group(2)}.jpg'
    return filename if (Path(__file__).resolve().parents[1] / 'data' / filename).is_file() else None


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
            identifier = _first(columns, ('id', 'task_id', 'test_id'))
            if not prompt or not number:
                return _error('writing_tasks needs a task_number and prompt_text column.', 503)
            cursor.execute(
                sql.SQL('SELECT * FROM {table} WHERE lower({number}::text) IN (%s, %s, %s, %s, %s)').format(
                    table=sql.Identifier(table), number=sql.Identifier(number)),
                (str(task_number), f'task_{task_number}', f'task {task_number}',
                 f'task{task_number}', f'academic task {task_number}'),
            )
            db_rows = cursor.fetchall()
            if not db_rows:
                return _error('No prompt was found for this writing task.', 404)
            fields = [description.name for description in cursor.description]
            rows = [dict(zip(fields, row)) for row in db_rows]
            seen = request.GET.getlist('seen')
            unseen = [row for row in rows if _prompt_id(row, prompt, identifier) not in seen]
            # When the browser has seen every prompt, start a new randomized cycle.
            selected = random.choice(unseen or rows)
            selected_id = _prompt_id(selected, prompt, identifier)
            duration = 20 * 60 if task_number == 1 else 40 * 60
            token = signing.dumps({'task': task_number, 'id': selected_id, 'duration': duration}, salt='writing-task-timer')
            image = _task_image(selected.values()) if task_number == 1 else None
            return JsonResponse({
                'task_number': task_number, 'prompt': selected[prompt], 'prompt_id': selected_id,
                'image_url': f'/api/writing/task-images/{image}/' if image else None,
                'duration_seconds': duration, 'task_token': token,
            })
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError):
        return _database_error()


@require_GET
def task_image(request, filename):
    # Only serve known image files from the writing service's bundled data directory.
    if not re.fullmatch(r'ielts-(?:8|9|10|11|12)-test-[1-4]\.jpg', filename):
        return _error('Task image was not found.', 404)
    path = Path(__file__).resolve().parents[1] / 'data' / filename
    if not path.is_file():
        return _error('Task image was not found.', 404)
    try:
        return FileResponse(path.open('rb'), content_type='image/jpeg')
    except OSError:
        return _error('Task image was not found.', 404)


@csrf_exempt
@require_POST
def submit_response(request):
    try:
        payload = json.loads(request.body or '{}')
    except (ValueError, UnicodeDecodeError):
        return _error('Request body must be valid JSON.')
    if not isinstance(payload, dict):
        return _error('Request body must be a JSON object.')
    task_number = payload.get('task_number')
    response = payload.get('response', '')
    if str(task_number) not in ('1', '2'):
        return _error('task_number must be 1 or 2.')
    if not isinstance(response, str) or not response.strip():
        return _error('Response cannot be empty.')
    count = len(response.split())
    if count < 150:
        return _error('Response cannot be less than 150 words.', 422)
    if count > 250:
        return _error('Response cannot be greater than 250 words.', 422)
    if not isinstance(payload.get('task_token'), str):
        return _error('The writing task session is invalid. Please reload the task.', 403)
    try:
        duration = 20 * 60 if str(task_number) == '1' else 40 * 60
        token_data = signing.loads(payload.get('task_token', ''), salt='writing-task-timer', max_age=24 * 60 * 60)
        if str(token_data.get('task')) != str(task_number) or token_data.get('duration') not in (1200, 2400):
            return _error('The writing task session is invalid. Please reload the task.', 403)
    except signing.SignatureExpired:
        return _error('The writing task session is invalid. Please reload the task.', 403)
    except signing.BadSignature:
        return _error('The writing task session is invalid. Please reload the task.', 403)
    try:
        signing.loads(payload['task_token'], salt='writing-task-timer', max_age=duration)
        task_expired = False
    except signing.SignatureExpired:
        task_expired = True
    except signing.BadSignature:
        return _error('The writing task session is invalid. Please reload the task.', 403)
    user_id = request.headers.get('X-User-Id')
    submission_key = _submission_key(task_number, payload['task_token'], response, user_id)
    try:
        # Idempotent retries read the saved database record and never call Gemini again.
        with _connection() as connection, connection.cursor() as cursor:
            saved = load_saved_evaluation(cursor, submission_key)
            if saved:
                saved['word_count'] = count
                return JsonResponse(saved, status=200)

        # Commit the student's answer before calling the external scoring service.
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
            prompt_column = _first(tasks, ('prompt_text', 'prompt', 'task_prompt', 'description'))
            identifier = _first(tasks, ('id', 'task_id', 'test_id'))
            task_id_column = _first(tasks, ('id', 'task_id')) or task_number_column
            if not task_number_column or not responses:
                return _error('writing_tasks and writing_responses tables must exist.', 503)
            if not prompt_column:
                return _error('Writing task table needs a prompt column.', 503)
            cursor.execute(
                sql.SQL('SELECT * FROM {table} WHERE lower({number}::text) IN (%s, %s, %s, %s, %s)').format(
                    table=sql.Identifier(task_table), number=sql.Identifier(task_number_column)),
                (str(task_number), f'task_{task_number}', f'task {task_number}',
                 f'task{task_number}', f'academic task {task_number}'),
            )
            task_rows = cursor.fetchall()
            if not task_rows:
                return _error('No prompt was found for this writing task.', 404)
            task_fields = [description.name for description in cursor.description]
            task_rows = [dict(zip(task_fields, row)) for row in task_rows]
            task = next((row for row in task_rows
                         if _prompt_id(row, prompt_column, identifier) == token_data.get('id')), None)
            if not task:
                return _error('The writing task session is invalid. Please reload the task.', 403)

            task_ref = task.get(task_id_column)
            response_text_column = _first(responses, ('response_text', 'response', 'answer', 'content'))
            response_id_column = _first(responses, ('id', 'response_id'))
            if not response_text_column:
                return _error('writing_responses needs a response_text column.', 503)
            if not response_id_column:
                return _error('writing_responses must have an id or response_id column.', 503)

            response_task_column = _first(responses, ('task_id', 'writing_task_id'))
            response_user_column = _first(responses, ('user_id', 'student_id'))
            conditions = [sql.SQL('{} = %s').format(sql.Identifier(response_text_column))]
            lookup_values = [response]
            if response_task_column and task_ref is not None:
                conditions.append(sql.SQL('{}::text = %s').format(sql.Identifier(response_task_column)))
                lookup_values.append(str(task_ref))
            if response_user_column and user_id:
                conditions.append(sql.SQL('{}::text = %s').format(sql.Identifier(response_user_column)))
                lookup_values.append(str(user_id))
            cursor.execute(sql.SQL('SELECT {id} FROM {table} WHERE {where} ORDER BY {id} LIMIT 1').format(
                id=sql.Identifier(response_id_column), table=sql.Identifier(response_table),
                where=sql.SQL(' AND ').join(conditions)), lookup_values)
            existing_response = cursor.fetchone()

            if existing_response:
                saved_response_id = str(existing_response[0])
            else:
                if task_expired:
                    return _error('You are out of time. The task cannot be submitted now.', 422)
                response_values = {}
                for candidates, value in (
                    (('task_id', 'writing_task_id'), str(task_ref) if task_ref is not None else None),
                    (('user_id', 'student_id'), user_id),
                    (('response_text', 'response', 'answer', 'content'), response),
                    (('word_count',), count),
                ):
                    column = _first(responses, candidates)
                    if column and (value is not None or column not in ('user_id', 'student_id', 'task_id', 'writing_task_id')):
                        response_values[column] = value
                if not response_values:
                    return _error('Writing response could not be saved.', 503)
                response_types = _column_types(cursor, response_table)
                statement = sql.SQL('INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING {id}').format(
                    table=sql.Identifier(response_table),
                    columns=sql.SQL(', ').join(map(sql.Identifier, response_values)),
                    values=sql.SQL(', ').join(
                        _placeholder_for(column, response_types) for column in response_values),
                    id=sql.Identifier(response_id_column),
                )
                cursor.execute(statement, list(response_values.values()))
                inserted = cursor.fetchone()
                if not inserted:
                    return _error('Writing response could not be saved.', 503)
                saved_response_id = str(inserted[0])
            task_ref = str(task_ref) if task_ref is not None else None
            task_prompt = task[prompt_column]

        evaluation = evaluate_response(task_number, task_prompt, response)
        overall_band_score = calculate_overall_band_score(
            [evaluation[criterion]['score'] for criterion in CRITERIA]
        )
        with _connection() as connection, connection.cursor() as cursor:
            saved = persist_evaluation(
                cursor, response_id=saved_response_id, submission_key=submission_key,
                task_id=task_ref, user_id=user_id, evaluation=evaluation,
                overall_band_score=overall_band_score,
            )
        saved['word_count'] = count
        return JsonResponse(saved, status=201)
    except ScoringError as error:
        return _error(str(error), 503)
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError, KeyError):
        return _database_error()

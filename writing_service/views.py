"""Writing task API. All frontend traffic reaches this service through fyp_root."""
import json
import os
import random
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from django.core import signing
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .scoring import CRITERIA, ScoringError, calculate_overall_band_score, evaluate_response


def _connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL is not configured for fyp_writing.')
    parsed = urlparse(database_url)
    return psycopg.connect(host=parsed.hostname, port=parsed.port or 5432,
                           dbname=parsed.path.lstrip('/'), user=parsed.username,
                           password=parsed.password, sslmode='require', connect_timeout=5)


def _columns(cursor, table):
    cursor.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s', (table,))
    return {row[0] for row in cursor.fetchall()}


def _types(cursor, table):
    cursor.execute('SELECT column_name,data_type,udt_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s', (table,))
    return {name: (data, udt) for name, data, udt in cursor.fetchall()}


def _first(columns, names):
    return next((name for name in names if name in columns), None)


def _error(message, status=400):
    return JsonResponse({'detail': message}, status=status)


def _db_error():
    return _error('Writing database is unavailable or its tables are not configured.', 503)


def _prompt_fields(columns):
    return (_first(columns, ('test_id', 'id')), _first(columns, ('task_text', 'prompt_text', 'task_prompt', 'prompt', 'description')))


def _task_image(test_id):
    """Find the actual bundled extension for a testN image."""
    if isinstance(test_id, bool) or not str(test_id).isdigit():
        return None
    folder = Path(__file__).resolve().parents[1] / 'data'
    for extension in ('.jpg', '.jpeg', '.png', '.webp'):
        name = f'test{int(test_id)}{extension}'
        if (folder / name).is_file():
            return name
    return None


def _table_for_task(task_number):
    return f'writing_task{task_number}'


@require_GET
def task_detail(request, task_number):
    if task_number not in (1, 2):
        return _error('Task number must be 1 or 2.')
    try:
        with _connection() as conn, conn.cursor() as cursor:
            table = _table_for_task(task_number)
            columns = _columns(cursor, table)
            identifier, prompt = _prompt_fields(columns)
            if not identifier or not prompt:
                return _error(f'{table} must contain a prompt ID and a task text column.', 503)
            cursor.execute(sql.SQL('SELECT {id}, {prompt} FROM {table} WHERE {prompt} IS NOT NULL').format(
                id=sql.Identifier(identifier), prompt=sql.Identifier(prompt), table=sql.Identifier(table)))
            rows = cursor.fetchall()
            if not rows:
                return _error('No prompt was found for this writing task.', 404)
            seen = set(request.GET.getlist('seen'))
            unseen = [row for row in rows if str(row[0]) not in seen]
            test_id, task_text = random.choice(unseen or rows)
            duration = 1200 if task_number == 1 else 2400
            token = signing.dumps({'task_number': task_number, 'test_id': str(test_id), 'task_text': task_text, 'duration': duration}, salt='writing-task-session')
            image = _task_image(test_id) if task_number == 1 else None
            return JsonResponse({'task_number': task_number, 'task_type': f'task{task_number}',
                                 'test_id': test_id, 'task_text': task_text, 'prompt': task_text,
                                 'prompt_id': str(test_id),
                                 'image_url': f'/api/writing/task-images/{image}/' if image else None,
                                 'duration_seconds': duration, 'task_token': token})
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()


@require_GET
def task_image(request, filename):
    if not re.fullmatch(r'test\d+\.(?:jpg|jpeg|png|webp)', filename):
        return _error('Task image was not found.', 404)
    path = Path(__file__).resolve().parents[1] / 'data' / filename
    if not path.is_file():
        return _error('Task image was not found.', 404)
    try:
        return FileResponse(path.open('rb'))
    except OSError:
        return _error('Task image was not found.', 404)


def _ensure_response_table(cursor, task_number):
    table = f'writing_response{task_number}'
    if not _columns(cursor, table):
        cursor.execute(sql.SQL('CREATE TABLE {} (response_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), test_id text NOT NULL, task_type text NOT NULL, response_text text NOT NULL, word_count integer NOT NULL, user_id text, created_at timestamptz NOT NULL DEFAULT now())').format(sql.Identifier(table)))
    columns = _columns(cursor, table)
    for name, definition in (('test_id', 'text'), ('task_type', 'text'), ('response_text', 'text'), ('word_count', 'integer'), ('user_id', 'text'), ('created_at', 'timestamptz')):
        if name not in columns:
            if name == 'created_at':
                definition = 'timestamptz NOT NULL DEFAULT now()'
            cursor.execute(sql.SQL('ALTER TABLE {} ADD COLUMN {} {}').format(sql.Identifier(table), sql.Identifier(name), sql.SQL(definition)))
    cursor.execute(sql.SQL('ALTER TABLE {} ALTER COLUMN created_at SET DEFAULT now()').format(sql.Identifier(table)))
    return table


def _ensure_score_table(cursor, task_number):
    table = f'writing_score{task_number}'
    if not _columns(cursor, table):
        cursor.execute(sql.SQL('CREATE TABLE {} (score_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), response_id text NOT NULL, test_id text NOT NULL, task_type text NOT NULL, task_achievement numeric(3,1) NOT NULL, coherence_cohesion numeric(3,1) NOT NULL, lexical_resource numeric(3,1) NOT NULL, grammar_accuracy numeric(3,1) NOT NULL, overall_band_score numeric(3,1) NOT NULL, evaluation_json jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())').format(sql.Identifier(table)))
    columns = _columns(cursor, table)
    definitions = {'response_id': 'text', 'test_id': 'text', 'task_type': 'text',
                   'task_achievement': 'numeric(3,1)', 'coherence_cohesion': 'numeric(3,1)',
                   'lexical_resource': 'numeric(3,1)', 'grammar_accuracy': 'numeric(3,1)',
                   'overall_band_score': 'numeric(3,1)', 'evaluation_json': 'jsonb', 'user_id': 'text', 'created_at': 'timestamptz'}
    for name, definition in definitions.items():
        if name not in columns:
            if name == 'created_at':
                definition = 'timestamptz NOT NULL DEFAULT now()'
            cursor.execute(sql.SQL('ALTER TABLE {} ADD COLUMN {} {}').format(sql.Identifier(table), sql.Identifier(name), sql.SQL(definition)))
    cursor.execute(sql.SQL('ALTER TABLE {} ALTER COLUMN created_at SET DEFAULT now()').format(sql.Identifier(table)))
    return table


@csrf_exempt
@require_POST
def submit_response(request):
    try:
        payload = json.loads(request.body or '{}')
    except (ValueError, UnicodeDecodeError):
        return _error('Request body must be valid JSON.')
    if not isinstance(payload, dict):
        return _error('Request body must be a JSON object.')
    try:
        task_number = int(payload.get('task_number'))
    except (ValueError, TypeError):
        task_number = 0
    response = payload.get('response')
    token = payload.get('task_token')
    if task_number not in (1, 2):
        return _error('task_number must be 1 or 2.')
    if not isinstance(response, str) or not response.strip():
        return _error('Response cannot be empty.')
    word_count = len(response.split())
    if word_count < 150:
        return _error('Response cannot be less than 150 words.', 422)
    if word_count > 250:
        return _error('Response cannot be greater than 250 words.', 422)
    try:
        duration = 20 * 60 if task_number == 1 else 40 * 60
        session = signing.loads(token, salt='writing-task-session', max_age=duration)
        if session.get('task_number') != task_number or not session.get('test_id'):
            return _error('The writing task session is invalid. Please reload the task.', 403)
    except signing.SignatureExpired:
        return _error('You are out of time. The task cannot be submitted now.', 422)
    except (signing.BadSignature, signing.SignatureExpired, TypeError):
        return _error('The writing task session is invalid. Please reload the task.', 403)

    user_id = request.headers.get('X-User-Id')
    try:
        with _connection() as conn, conn.cursor() as cursor:
            task_table = _table_for_task(task_number)
            task_columns = _columns(cursor, task_table)
            id_column, prompt_column = _prompt_fields(task_columns)
            if not id_column or not prompt_column:
                return _error(f'{task_table} must contain a prompt ID and a task text column.', 503)
            cursor.execute(sql.SQL('SELECT {prompt} FROM {table} WHERE {id}::text=%s').format(
                prompt=sql.Identifier(prompt_column), table=sql.Identifier(task_table), id=sql.Identifier(id_column)), (str(session['test_id']),))
            row = cursor.fetchone()
            if not row:
                return _error('The selected writing prompt no longer exists.', 404)
            task_text = row[0]
            if task_text != session.get('task_text'):
                return _error('The writing task session no longer matches its prompt.', 403)
            response_table = _ensure_response_table(cursor, task_number)
            response_columns = _columns(cursor, response_table)
            response_types = _types(cursor, response_table)
            response_id_col = _first(response_columns, ('response_id', 'id'))
            text_col = _first(response_columns, ('response_text', 'response', 'answer', 'content'))
            test_col = _first(response_columns, ('test_id', 'task_id', 'writing_task_id'))
            task_type_col = _first(response_columns, ('task_type', 'task_number'))
            words_col = _first(response_columns, ('word_count',))
            if not response_id_col or not text_col:
                return _error(f'{response_table} needs a response_id and response_text column.', 503)
            mapped = {text_col: response}
            for column in ('test_id', 'task_id', 'writing_task_id'):
                if column in response_columns:
                    mapped[column] = str(session['test_id'])
            for column in ('task_type', 'task_number'):
                if column in response_columns:
                    mapped[column] = f'task{task_number}' if column == 'task_type' else task_number
            for column in ('word_count', 'user_id', 'student_id'):
                if column in response_columns and (column == 'word_count' or user_id is not None):
                    mapped[column] = word_count if column == 'word_count' else user_id
            cursor.execute(sql.SQL('INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING {id}').format(
                table=sql.Identifier(response_table), cols=sql.SQL(',').join(map(sql.Identifier, mapped)),
                vals=sql.SQL(',').join(sql.SQL('%s::uuid') if response_types.get(c, ('',''))[1] == 'uuid' else sql.Placeholder() for c in mapped),
                id=sql.Identifier(response_id_col)), list(mapped.values()))
            response_id = str(cursor.fetchone()[0])
        evaluation = evaluate_response(task_number, task_text, response)
        band = calculate_overall_band_score([evaluation[key]['score'] for key in CRITERIA])
        with _connection() as conn, conn.cursor() as cursor:
            table = _ensure_score_table(cursor, task_number)
            columns = _columns(cursor, table)
            id_col = _first(columns, ('score_id', 'id'))
            response_col = _first(columns, ('response_id', 'writing_response_id'))
            if not id_col or not response_col:
                return _error(f'{table} needs score_id and response_id columns.', 503)
            record = {'response_id': response_id, 'test_id': str(session['test_id']), 'task_type': f'task{task_number}',
                      **{key: evaluation[key]['score'] for key in CRITERIA}, 'overall_band_score': band,
                      'evaluation_json': json.dumps(evaluation, ensure_ascii=False), 'user_id': user_id}
            mapped = {key: value for key, value in record.items() if key in columns}
            for column in ('writing_response_id', 'submission_id'):
                if column in columns:
                    mapped[column] = response_id
            for column in ('task_id', 'writing_task_id'):
                if column in columns:
                    mapped[column] = str(session['test_id'])
            for column in ('task_number',):
                if column in columns:
                    mapped[column] = task_number
            for column in ('task_achievement_score', 'coherence_cohesion_score', 'lexical_resource_score', 'grammar_accuracy_score'):
                criterion = column[:-6]
                if column in columns:
                    mapped[column] = evaluation[criterion]['score']
            for column in ('overall_score', 'overall_band'):
                if column in columns:
                    mapped[column] = band
            for column in ('evaluation', 'llm_evaluation', 'score_details', 'feedback_json'):
                if column in columns:
                    mapped[column] = json.dumps(evaluation, ensure_ascii=False)
            if not any(k in mapped for k in ('evaluation_json', 'evaluation', 'score_details')):
                return _error(f'{table} needs an evaluation_json column to preserve reasoning and feedback.', 503)
            score_types = _types(cursor, table)
            cursor.execute(sql.SQL('INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING {id}').format(
                table=sql.Identifier(table), cols=sql.SQL(',').join(map(sql.Identifier, mapped)),
                vals=sql.SQL(',').join(sql.SQL('%s::json') if score_types.get(k, ('',''))[0] == 'json' else sql.SQL('%s::jsonb') if score_types.get(k, ('',''))[0] == 'jsonb' else sql.SQL('%s::uuid') if score_types.get(k, ('',''))[1] == 'uuid' else sql.Placeholder() for k in mapped),
                id=sql.Identifier(id_col)), list(mapped.values()))
            score_id = str(cursor.fetchone()[0])
            cursor.execute(sql.SQL('SELECT {cols} FROM {table} WHERE {id}=%s').format(
                cols=sql.SQL(',').join(map(sql.Identifier, mapped)), table=sql.Identifier(table), id=sql.Identifier(id_col)), (score_id,))
            saved = dict(zip((d.name for d in cursor.description), cursor.fetchone()))
            details_col = _first(saved.keys(), ('evaluation_json', 'evaluation', 'score_details'))
            details = saved[details_col]
            if isinstance(details, str):
                details = json.loads(details)
            details['overall_band_score'] = float(saved.get('overall_band_score', band))
            details['response_id'] = response_id
            details['test_id'] = str(session['test_id'])
            details['task_number'] = task_number
            details['status'] = 'submitted'
        return JsonResponse(details, status=201)
    except ScoringError as error:
        return _error(str(error), 503)
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError, KeyError, IndexError):
        return _db_error()


@require_GET
def score_detail(request, task_number):
    if task_number not in (1, 2):
        return _error('Task number must be 1 or 2.')
    response_id = request.GET.get('response_id')
    try:
        with _connection() as conn, conn.cursor() as cursor:
            table = _ensure_score_table(cursor, task_number)
            columns = _columns(cursor, table)
            id_col = _first(columns, ('score_id', 'id'))
            response_col = _first(columns, ('response_id', 'writing_response_id'))
            details_col = _first(columns, ('evaluation_json', 'evaluation', 'score_details'))
            if not (id_col and response_col and details_col):
                return _error('Saved score table is incomplete.', 503)
            if response_id:
                where = sql.SQL('{response}::text=%s').format(response=sql.Identifier(response_col))
                params = [response_id]
            else:
                where = sql.SQL('TRUE')
                params = []
            user_id = request.headers.get('X-User-Id')
            if user_id and 'user_id' in columns:
                where += sql.SQL(' AND user_id::text=%s')
                params.append(user_id)
            cursor.execute(sql.SQL('SELECT {details},{band} FROM {table} WHERE {where} ORDER BY {id} DESC LIMIT 1').format(
                details=sql.Identifier(details_col), band=sql.Identifier(_first(columns, ('overall_band_score', 'overall_score', 'overall_band'))),
                table=sql.Identifier(table), where=where, id=sql.Identifier(id_col)), params)
            row = cursor.fetchone()
            if not row:
                return _error('No saved score was found for this task.', 404)
            details = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            details['overall_band_score'] = float(row[1])
            return JsonResponse(details)
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError, KeyError):
        return _db_error()


@require_GET
def combined_score(request):
    result = {}
    try:
        with _connection() as conn, conn.cursor() as cursor:
            for number in (1, 2):
                table = f'writing_score{number}'
                columns = _columns(cursor, table)
                band_col = _first(columns, ('overall_band_score', 'overall_score', 'overall_band'))
                id_col = _first(columns, ('score_id', 'id'))
                if not band_col or not id_col:
                    return _error(f'No saved Task {number} score was found.', 404)
                user_id = request.headers.get('X-User-Id')
                if user_id and 'user_id' in columns:
                    cursor.execute(sql.SQL('SELECT {band} FROM {table} WHERE user_id::text=%s ORDER BY {id} DESC LIMIT 1').format(
                        band=sql.Identifier(band_col), table=sql.Identifier(table), id=sql.Identifier(id_col)), (user_id,))
                else:
                    cursor.execute(sql.SQL('SELECT {band} FROM {table} ORDER BY {id} DESC LIMIT 1').format(
                        band=sql.Identifier(band_col), table=sql.Identifier(table), id=sql.Identifier(id_col)))
                row = cursor.fetchone()
                if not row:
                    return _error(f'No saved Task {number} score was found.', 404)
                result[f'task{number}_band'] = float(row[0])
        weighted = (Decimal(str(result['task1_band'])) + 2 * Decimal(str(result['task2_band']))) / 3
        result['combined_writing_band'] = float((weighted * 2).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 2)
        return JsonResponse(result)
    except (psycopg.Error, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()

"""Writing task API. All frontend traffic reaches this service through fyp_root."""
import hmac
import json
import logging
import os
import random
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from django.core import signing
from django.db import DatabaseError
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .database import user_database_context
from .models import WritingResponse1, WritingResponse2, WritingScore1, WritingScore2, WritingTask1, WritingTask2
from .scoring import CRITERIA, ScoringError, calculate_overall_band_score, evaluate_response

logger = logging.getLogger(__name__)
TASK_MODELS = {1: WritingTask1, 2: WritingTask2}
RESPONSE_MODELS = {1: WritingResponse1, 2: WritingResponse2}
SCORE_MODELS = {1: WritingScore1, 2: WritingScore2}


def _error(message, status=400):
    return JsonResponse({'detail': message}, status=status)


def require_gateway_identity(view):
    """Accept traffic only from fyp_root with its authenticated user identity."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        expected = os.environ.get('INTERNAL_SERVICE_TOKEN', '')
        supplied = request.headers.get('X-Internal-Token', '')
        user_id = request.headers.get('X-User-Id', '')
        if not expected:
            return _error('Writing service internal authentication is not configured.', 503)
        try:
            uuid.UUID(user_id)
        except (ValueError, TypeError, AttributeError):
            return _error('Authentication is required.', 401)
        if not supplied or not hmac.compare_digest(supplied, expected):
            return _error('Authentication is required.', 401)
        return view(request, *args, **kwargs)
    return wrapped


def _db_error():
    return _error('Writing database is unavailable or its tables are not configured.', 503)


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


@require_gateway_identity
@require_GET
def available_tests(request):
    """Return the distinct tests available in the existing prompt tables."""
    tests = {}
    try:
        with user_database_context(request.headers['X-User-Id']):
            for task_number, model in TASK_MODELS.items():
                rows = model.objects.filter(prompt__isnull=False).values_list('id', 'test_number')
                for test_id, test_number in rows:
                    try:
                        number = int(test_number)
                    except (TypeError, ValueError):
                        continue
                    item = tests.setdefault(number, {'test_number': number, 'task1': False, 'task2': False})
                    item[f'task{task_number}'] = True
        return JsonResponse({'tests': [tests[number] for number in sorted(tests)]})
    except (DatabaseError, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()


@require_gateway_identity
@require_GET
def task_detail(request, task_number):
    if task_number not in (1, 2):
        return _error('Task number must be 1 or 2.')
    try:
        model = TASK_MODELS[task_number]
        with user_database_context(request.headers['X-User-Id']):
            queryset = model.objects.filter(prompt__isnull=False)
            requested_number = request.GET.get('test_number')
            if requested_number is None:
                rows = list(queryset.values_list('id', 'prompt', 'test_number'))
                if not rows:
                    return _error('No prompt was found for this writing task.', 404)
                seen = set(request.GET.getlist('seen'))
                unseen = [row for row in rows if str(row[0]) not in seen]
                test_id, task_text, test_number = random.choice(unseen or rows)
            else:
                try:
                    test_number = int(requested_number)
                except ValueError:
                    return _error('The selected test is invalid.')
                row = queryset.filter(test_number=test_number).values_list('id', 'prompt', 'test_number').first()
                if not row:
                    return _error('The selected writing test is unavailable for this task.', 404)
                test_id, task_text, test_number = row
            duration = 1200 if task_number == 1 else 2400
            token = signing.dumps({
                'task_number': task_number, 'test_id': str(test_id), 'test_number': str(test_number),
                'task_text': task_text, 'user_id': request.headers['X-User-Id'], 'duration': duration,
            }, salt='writing-task-session')
            image = _task_image(test_number) if task_number == 1 else None
            return JsonResponse({
                'task_number': task_number, 'task_type': f'task{task_number}',
                'test_id': test_id, 'test_number': test_number, 'task_text': task_text,
                'prompt': task_text, 'prompt_id': str(test_id),
                'image_url': f'/api/writing/task-images/{image}/' if image else None,
                'duration_seconds': duration, 'task_token': token,
            })
    except (DatabaseError, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()


@require_gateway_identity
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


@require_gateway_identity
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
    response_text = payload.get('response')
    token = payload.get('task_token')
    if task_number not in (1, 2):
        return _error('task_number must be 1 or 2.')
    if not isinstance(response_text, str) or not response_text.strip():
        return _error('Response cannot be empty.')
    word_count = len(response_text.split())
    if word_count < 150:
        return _error('Response cannot be less than 150 words.', 422)
    if word_count > 250:
        return _error('Response cannot be greater than 250 words.', 422)
    try:
        duration = 20 * 60 if task_number == 1 else 40 * 60
        session = signing.loads(token, salt='writing-task-session', max_age=duration)
        if (session.get('task_number') != task_number or not session.get('test_id')
                or session.get('user_id') != request.headers['X-User-Id']):
            return _error('The writing task session is invalid. Please reload the task.', 403)
    except signing.SignatureExpired:
        return _error('You are out of time. The task cannot be submitted now.', 422)
    except (signing.BadSignature, TypeError):
        return _error('The writing task session is invalid. Please reload the task.', 403)

    user_id = request.headers['X-User-Id']
    try:
        task_model = TASK_MODELS[task_number]
        response_model = RESPONSE_MODELS[task_number]
        score_model = SCORE_MODELS[task_number]
        with user_database_context(user_id):
            task_text = task_model.objects.filter(pk=session['test_id']).values_list('prompt', flat=True).first()
            if task_text is None:
                return _error('The selected writing prompt no longer exists.', 404)
            if task_text != session.get('task_text'):
                return _error('The writing task session no longer matches its prompt.', 403)
            saved_response = response_model.objects.create(
                test_id=str(session['test_id']), task_type=f'task{task_number}',
                response_text=response_text, word_count=word_count, user_id=user_id,
            )
            response_id = str(saved_response.response_id)

        evaluation = evaluate_response(task_number, task_text, response_text)
        band = calculate_overall_band_score([evaluation[key]['score'] for key in CRITERIA])
        with user_database_context(user_id):
            saved_score = score_model.objects.create(
                response_id=response_id, test_id=str(session['test_id']), task_type=f'task{task_number}',
                task_achievement=evaluation['task_achievement']['score'],
                coherence_cohesion=evaluation['coherence_cohesion']['score'],
                lexical_resource=evaluation['lexical_resource']['score'],
                grammar_accuracy=evaluation['grammar_accuracy']['score'],
                overall_band_score=band, evaluation_json=evaluation, user_id=user_id,
            )
            details = dict(saved_score.evaluation_json)
            details.update({
                'overall_band_score': float(saved_score.overall_band_score), 'response_id': response_id,
                'test_id': str(session['test_id']), 'task_number': task_number, 'status': 'submitted',
            })
        return JsonResponse(details, status=201)
    except ScoringError as error:
        return _error(str(error), 503)
    except (DatabaseError, RuntimeError, OSError, ValueError, TypeError, KeyError, IndexError) as error:
        logger.error('Writing request failed: error_type=%s', type(error).__name__)
        return _db_error()


@require_gateway_identity
@require_GET
def score_detail(request, task_number):
    if task_number not in (1, 2):
        return _error('Task number must be 1 or 2.')
    try:
        score_model = SCORE_MODELS[task_number]
        with user_database_context(request.headers['X-User-Id']):
            queryset = score_model.objects.filter(user_id=request.headers['X-User-Id'])
            response_id = request.GET.get('response_id')
            if response_id:
                queryset = queryset.filter(response_id=response_id)
            saved = queryset.order_by('-score_id').first()
            if not saved:
                return _error('No saved score was found for this task.', 404)
            details = dict(saved.evaluation_json)
            details['overall_band_score'] = float(saved.overall_band_score)
            return JsonResponse(details)
    except (DatabaseError, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()


@require_gateway_identity
@require_GET
def combined_score(request):
    result = {}
    try:
        user_id = request.headers['X-User-Id']
        with user_database_context(user_id):
            for number, model in SCORE_MODELS.items():
                band = model.objects.filter(user_id=user_id).order_by('-score_id').values_list(
                    'overall_band_score', flat=True).first()
                if band is None:
                    return _error(f'No saved Task {number} score was found.', 404)
                result[f'task{number}_band'] = float(band)
        weighted = (Decimal(str(result['task1_band'])) + 2 * Decimal(str(result['task2_band']))) / 3
        result['combined_writing_band'] = float((weighted * 2).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / 2)
        return JsonResponse(result)
    except (DatabaseError, RuntimeError, OSError, ValueError, TypeError):
        return _db_error()

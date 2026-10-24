"""Focused offline tests for IELTS writing scoring and submission behavior."""

import json
import os
import unittest
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'writing_service.settings')
os.environ['INTERNAL_SERVICE_TOKEN'] = 'writing-test-token'

import django
django.setup()

from django.core import signing
from django.test import RequestFactory

from writing_service import scoring, views
from writing_service.models import (
    WritingResponse1, WritingResponse2, WritingScore1, WritingScore2,
    WritingTask1, WritingTask2,
)
from writing_service.scores import ensure_score_table, persist_evaluation

TEST_USER_ID = str(uuid.UUID('f7d5ca85-9a2f-4eeb-aec6-189e18572c51'))


def sample_evaluation():
    return {
        'task_achievement': {'score': 7.0, 'reasoning': 'Relevant overview.', 'feedback': 'Compare the key figures.'},
        'coherence_cohesion': {'score': 7.0, 'reasoning': 'Ideas progress clearly.', 'feedback': 'Vary linking phrases.'},
        'lexical_resource': {'score': 6.5, 'reasoning': 'Vocabulary is suitable.', 'feedback': 'Use more precise collocations.'},
        'grammar_accuracy': {'score': 7.0, 'reasoning': 'Most sentences are accurate.', 'feedback': 'Check article use.'},
        'overall_reasoning': 'A clear response with room to improve precision.',
    }


class ScoringValidationTests(unittest.TestCase):
    def test_half_band_rounding_follows_ielts_ties_up_rule(self):
        self.assertEqual(scoring.calculate_overall_band_score([6, 6, 6, 7]), 6.5)
        self.assertEqual(scoring.calculate_overall_band_score([6.5, 6.5, 7, 7]), 7.0)
        self.assertEqual(scoring.calculate_overall_band_score([9, 9, 9, 9]), 9.0)

    def test_evaluation_json_is_normalized(self):
        result = scoring.validate_evaluation(json.dumps(sample_evaluation()))
        self.assertEqual(result['lexical_resource']['score'], 6.5)
        self.assertIn('overall_reasoning', result)

    def test_rejects_malformed_or_incomplete_json(self):
        for invalid in ('```json\n{}\n```', {'overall_reasoning': 'missing criteria'}):
            with self.subTest(invalid=invalid), self.assertRaises(scoring.ScoringError):
                scoring.validate_evaluation(invalid)

    def test_rejects_out_of_range_and_non_band_increment(self):
        for score in (-0.5, 9.5, 6.2):
            value = sample_evaluation()
            value['task_achievement']['score'] = score
            with self.subTest(score=score), self.assertRaises(scoring.ScoringError):
                scoring.validate_evaluation(value)

    def test_prompt_includes_task_and_response_as_json(self):
        prompt = scoring.build_evaluation_prompt(1, 'Describe this chart.', 'The chart rises.')
        self.assertIn('Task Achievement', prompt)
        self.assertIn('official IELTS Writing Band Descriptors', prompt)
        self.assertIn('"original_task_prompt": "Describe this chart."', prompt)
        self.assertIn('"student_response": "The chart rises."', prompt)

    def test_calls_gemini_with_json_schema_and_validates_result(self):
        model_response = json.dumps({'candidates': [{'content': {'parts': [
            {'text': json.dumps(sample_evaluation())},
        ]}}]}).encode()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = model_response
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}), \
                patch('writing_service.scoring.urlopen', return_value=response) as urlopen:
            result = scoring.evaluate_response(1, 'Describe the chart.', 'The chart rises.')
        self.assertEqual(result['task_achievement']['score'], 7.0)
        request = urlopen.call_args.args[0]
        self.assertIn(scoring.MODEL_ID, request.full_url)
        self.assertEqual(request.get_header('X-goog-api-key'), 'test-key')
        body = json.loads(request.data)
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        self.assertIn('task_achievement', body['generationConfig']['responseSchema']['required'])

    def test_gemini_errors_are_sanitized(self):
        from urllib.error import URLError
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}), \
                patch('writing_service.scoring.urlopen', side_effect=URLError('provider unavailable')):
            with self.assertRaisesRegex(scoring.ScoringError, 'temporarily unavailable'):
                scoring.evaluate_response(2, 'Discuss both views.', 'A response.')


class ScorePersistenceTests(unittest.TestCase):
    def test_existing_score_table_is_extended_additively(self):
        cursor = MagicMock()
        initial = {
            'response_id', 'task_id', 'user_id', 'task_achievement', 'coherence_cohesion',
            'lexical_resource', 'grammar_accuracy', 'overall_band_score',
        }
        types = [(name, 'text', 'text') for name in initial]
        final_types = types + [
            ('evaluation_json', 'jsonb', 'jsonb'),
            ('submission_key', 'text', 'text'),
            ('created_at', 'timestamp with time zone', 'timestamptz'),
        ]
        cursor.fetchall.side_effect = [
            [('writing_scores',)],
            [(name,) for name in initial],
            types,
            final_types,
        ]

        table, fields, column_types = ensure_score_table(cursor)

        self.assertEqual(table, 'writing_scores')
        self.assertEqual(fields['evaluation_json'], 'evaluation_json')
        self.assertEqual(fields['submission_key'], 'submission_key')
        self.assertEqual(column_types['evaluation_json'], ('jsonb', 'jsonb'))
        # table lookup, two schema reads, three additive columns, and the unique key index
        self.assertEqual(cursor.execute.call_count, 8)

    def test_persistence_returns_evaluation_read_back_from_database(self):
        evaluation = sample_evaluation()
        fields = {
            'response_id': 'response_id', 'task_id': 'task_id', 'user_id': 'user_id',
            'submission_key': 'submission_key', 'task_achievement': 'task_achievement',
            'coherence_cohesion': 'coherence_cohesion', 'lexical_resource': 'lexical_resource',
            'grammar_accuracy': 'grammar_accuracy', 'overall_band_score': 'overall_band_score',
            'evaluation_json': 'evaluation_json', 'created_at': 'created_at',
        }
        saved_json = dict(evaluation)
        saved_json['overall_reasoning'] = 'Read back from PostgreSQL.'
        db_values = {
            'response_id': 'saved-response', 'task_id': 'saved-task', 'user_id': 'saved-user',
            'submission_key': 'key', 'task_achievement': 7, 'coherence_cohesion': 7,
            'lexical_resource': 6.5, 'grammar_accuracy': 7, 'overall_band_score': 7,
            'evaluation_json': saved_json, 'created_at': None,
        }
        cursor = MagicMock()

        def describe_select(statement, params=None):
            if len(cursor.execute.call_args_list) >= 1:
                cursor.description = [SimpleNamespace(name=field) for field in db_values]

        cursor.execute.side_effect = describe_select
        cursor.fetchone.return_value = tuple(db_values.values())
        with patch('writing_service.scores.ensure_score_table', return_value=(
                'writing_scores', fields, {name: ('text', 'text') for name in fields.values()})):
            result = persist_evaluation(
                cursor, response_id='new-response', submission_key='key', task_id='new-task',
                user_id='new-user', evaluation=evaluation, overall_band_score=7.0,
            )

        self.assertEqual(cursor.execute.call_count, 2)
        self.assertEqual(result['response_id'], 'saved-response')
        self.assertEqual(result['overall_reasoning'], 'Read back from PostgreSQL.')
        self.assertEqual(result['overall_band_score'], 7.0)
        self.assertEqual(result['lexical_resource']['score'], 6.5)


class ModelMappingTests(unittest.TestCase):
    def test_models_match_existing_table_names_and_are_unmanaged(self):
        expected = {
            WritingTask1: 'writing_task1', WritingTask2: 'writing_task2',
            WritingResponse1: 'writing_response1', WritingResponse2: 'writing_response2',
            WritingScore1: 'writing_score1', WritingScore2: 'writing_score2',
        }
        for model, table in expected.items():
            with self.subTest(table=table):
                self.assertEqual(model._meta.db_table, table)
                self.assertFalse(model._meta.managed)
        self.assertEqual(WritingResponse1._meta.pk.name, 'response_id')
        self.assertEqual(WritingScore2._meta.pk.name, 'score_id')
        self.assertTrue(WritingResponse1._meta.get_field('user_id').null)
        self.assertTrue(WritingScore1._meta.get_field('user_id').null)
        self.assertEqual(WritingScore1._meta.get_field('evaluation_json').get_internal_type(), 'JSONField')
        self.assertFalse(any(field.is_relation for model in expected for field in model._meta.fields))


class SubmitResponseTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.identity = {'HTTP_X_INTERNAL_TOKEN': 'writing-test-token', 'HTTP_X_USER_ID': TEST_USER_ID}

    def post(self, body):
        return self.factory.post('/api/responses/', data=json.dumps(body), content_type='application/json', **self.identity)

    def test_word_count_errors_return_before_database_or_gemini(self):
        response = views.submit_response(self.post({'task_number': 1, 'response': 'too short'}))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.content)['detail'], 'Response cannot be less than 150 words.')

    def test_requests_without_gateway_identity_are_rejected(self):
        response = views.available_tests(self.factory.get('/api/tests/'))
        self.assertEqual(response.status_code, 401)

    def test_invalid_gateway_token_is_rejected_before_database_access(self):
        request = self.factory.get('/api/tests/', HTTP_X_USER_ID=TEST_USER_ID,
                                   HTTP_X_INTERNAL_TOKEN='forged-service-token')
        with patch('writing_service.views.user_database_context') as context:
            response = views.available_tests(request)
        self.assertEqual(response.status_code, 401)
        context.assert_not_called()

    def test_available_tests_are_read_from_both_prompt_tables(self):
        one, two = MagicMock(), MagicMock()
        one.objects.filter.return_value.values_list.return_value = [(9, 1), (10, 2)]
        two.objects.filter.return_value.values_list.return_value = [(11, 1), (12, 3)]
        with patch.dict(views.TASK_MODELS, {1: one, 2: two}), patch('writing_service.views.user_database_context', return_value=nullcontext()):
            response = views.available_tests(self.factory.get('/api/tests/', **self.identity))
        self.assertEqual(json.loads(response.content)['tests'], [
            {'test_number': 1, 'task1': True, 'task2': True},
            {'test_number': 2, 'task1': True, 'task2': False},
            {'test_number': 3, 'task1': False, 'task2': True},
        ])

    def test_selected_test_returns_its_task_prompt(self):
        model = MagicMock()
        model.objects.filter.return_value.filter.return_value.values_list.return_value.first.return_value = (5, 'Describe chart B', 5)
        with patch.dict(views.TASK_MODELS, {1: model}), patch('writing_service.views.user_database_context', return_value=nullcontext()):
            response = views.task_detail(self.factory.get('/api/tasks/1/?test_number=5', **self.identity), 1)
        self.assertEqual(json.loads(response.content)['prompt'], 'Describe chart B')

    def test_score_lookup_filters_by_authenticated_user(self):
        saved = SimpleNamespace(evaluation_json=sample_evaluation(), overall_band_score=7.0)
        model = MagicMock()
        model.objects.filter.return_value.order_by.return_value.first.return_value = saved
        with patch.dict(views.SCORE_MODELS, {1: model}), patch('writing_service.views.user_database_context', return_value=nullcontext()):
            response = views.score_detail(self.factory.get('/api/scores/1/', **self.identity), 1)
        self.assertEqual(response.status_code, 200)
        model.objects.filter.assert_called_once_with(user_id=TEST_USER_ID)

    def test_empty_response_has_specific_error(self):
        response = views.submit_response(self.post({'task_number': 1, 'response': '  '}))
        self.assertEqual(json.loads(response.content)['detail'], 'Response cannot be empty.')

    def test_bad_task_token_is_rejected_before_database_or_gemini(self):
        words = ' '.join(f'word{i}' for i in range(150))
        with patch('writing_service.views.user_database_context') as context, patch('writing_service.views.evaluate_response') as evaluate:
            result = views.submit_response(self.post({'task_number': 1, 'response': words, 'task_token': 'invalid'}))
        self.assertEqual(result.status_code, 403)
        context.assert_not_called()
        evaluate.assert_not_called()

    def test_submission_saves_response_and_score_for_authenticated_user(self):
        task_text = 'Describe the information shown in the chart.'
        response_id = '8f36e55a-019c-49b6-a875-feb04ca28fda'
        token = signing.dumps({'task_number': 1, 'test_id': '5', 'test_number': '5', 'task_text': task_text,
                              'user_id': TEST_USER_ID, 'duration': 1200}, salt='writing-task-session')
        words = ' '.join(f'word{i}' for i in range(150))
        evaluation = sample_evaluation()
        saved_score = SimpleNamespace(evaluation_json=evaluation, overall_band_score=7.0)
        task_model, response_model, score_model = MagicMock(), MagicMock(), MagicMock()
        task_model.objects.filter.return_value.values_list.return_value.first.return_value = task_text
        response_model.objects.create.return_value = SimpleNamespace(response_id=response_id)
        score_model.objects.create.return_value = saved_score
        with patch.dict(views.TASK_MODELS, {1: task_model}), patch.dict(views.RESPONSE_MODELS, {1: response_model}), \
                patch.dict(views.SCORE_MODELS, {1: score_model}), patch('writing_service.views.user_database_context', return_value=nullcontext()), \
                patch('writing_service.views.evaluate_response', return_value=evaluation):
            result = views.submit_response(self.post({'task_number': 1, 'response': words, 'task_token': token}))
        self.assertEqual(result.status_code, 201)
        response_model.objects.create.assert_called_once_with(
            test_id='5', task_type='task1', response_text=words, word_count=150, user_id=TEST_USER_ID)
        score_model.objects.create.assert_called_once()
        self.assertEqual(score_model.objects.create.call_args.kwargs['user_id'], TEST_USER_ID)
        self.assertEqual(score_model.objects.create.call_args.kwargs['response_id'], response_id)
        self.assertEqual(json.loads(result.content)['overall_band_score'], 7.0)

    def test_task_image_matches_test_id_and_real_extension(self):
        self.assertEqual(views._task_image(1), 'test1.jpg')
        self.assertEqual(views._task_image(5), 'test5.png')
        self.assertEqual(views._task_image(13), 'test13.png')
        self.assertIsNone(views._task_image('not-an-id'))
        self.assertIsNone(views._task_image(999))

    def test_random_task_uses_task_specific_table_and_returns_test_id(self):
        model = MagicMock()
        queryset = model.objects.filter.return_value
        queryset.values_list.return_value = [(1, 'Describe chart A', 1), (5, 'Describe chart B', 5)]
        with patch.dict(views.TASK_MODELS, {1: model}), patch('writing_service.views.user_database_context', return_value=nullcontext()), \
                patch('writing_service.views.random.choice', return_value=(5, 'Describe chart B', 5)):
            result = views.task_detail(self.factory.get('/api/tasks/1/?seen=1', **self.identity), 1)
        body = json.loads(result.content)
        self.assertEqual(body['test_id'], 5)
        self.assertEqual(body['task_text'], 'Describe chart B')
        self.assertEqual(body['image_url'], '/api/writing/task-images/test5.png/')

    def test_combined_band_weights_task_two_twice_and_rounds_to_half(self):
        one, two = MagicMock(), MagicMock()
        one.objects.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = 6.0
        two.objects.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = 7.0
        with patch.dict(views.SCORE_MODELS, {1: one, 2: two}), patch('writing_service.views.user_database_context', return_value=nullcontext()):
            result = views.combined_score(self.factory.get('/api/scores/combined/', **self.identity))
        body = json.loads(result.content)
        self.assertEqual(body['combined_writing_band'], 6.5)


if __name__ == '__main__':
    unittest.main()

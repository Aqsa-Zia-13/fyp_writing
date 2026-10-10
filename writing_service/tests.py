"""Focused offline tests for IELTS writing scoring and submission behavior."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'writing_service.settings')

import django
django.setup()

from django.core import signing
from django.test import RequestFactory

from writing_service import scoring, views
from writing_service.scores import ensure_score_table, persist_evaluation


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


class SubmitResponseTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def post(self, body):
        return self.factory.post('/api/responses/', data=json.dumps(body), content_type='application/json')

    def test_word_count_errors_return_before_database_or_gemini(self):
        body = {'task_number': 1, 'response': 'too short'}
        response = views.submit_response(self.post(body))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(response.content)['detail'], 'Response cannot be less than 150 words.')

    def test_empty_response_has_specific_error(self):
        response = views.submit_response(self.post({'task_number': 1, 'response': '  '}))
        self.assertEqual(json.loads(response.content)['detail'], 'Response cannot be empty.')

    def test_success_response_uses_saved_evaluation_and_overall_band(self):
        words = ' '.join(f'word{i}' for i in range(150))
        token = signing.dumps({'task': 1, 'id': 'task-1', 'duration': 1200}, salt='writing-task-timer')
        request = self.post({'task_number': 1, 'response': words, 'task_token': token})
        request.META['HTTP_X_USER_ID'] = 'user-1'
        connections = []
        for _ in range(3):
            connection = MagicMock()
            connection.__enter__.return_value = connection
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            connection.cursor.return_value = cursor
            connections.append(connection)
        response_cursor = connections[1].cursor.return_value.__enter__.return_value
        response_cursor.fetchall.return_value = [('task-1', 1, 'Describe the chart.')]
        response_cursor.description = [
            SimpleNamespace(name='id'), SimpleNamespace(name='task_number'),
            SimpleNamespace(name='prompt_text'),
        ]
        response_cursor.fetchone.side_effect = [None, ('response-1',)]
        saved = sample_evaluation()
        saved.update({'response_id': 'db-response', 'overall_band_score': 7.0, 'status': 'submitted'})
        with patch('writing_service.views._connection', side_effect=connections), \
                patch('writing_service.views.load_saved_evaluation', return_value=None), \
                patch('writing_service.views._table', side_effect=['writing_tasks', 'writing_responses']), \
                patch('writing_service.views._columns', side_effect=[
                    {'id', 'task_number', 'prompt_text'},
                    {'id', 'task_id', 'user_id', 'response_text', 'word_count'},
                ]), \
                patch('writing_service.views._column_types', return_value={}), \
                patch('writing_service.views.evaluate_response', return_value=sample_evaluation()), \
                patch('writing_service.views.persist_evaluation', return_value=saved) as persist:
            result = views.submit_response(request)

        self.assertEqual(result.status_code, 201)
        data = json.loads(result.content)
        self.assertEqual(data['overall_band_score'], 7.0)
        self.assertEqual(data['response_id'], 'db-response')
        self.assertEqual(data['task_achievement']['feedback'], 'Compare the key figures.')
        persist.assert_called_once()


if __name__ == '__main__':
    unittest.main()

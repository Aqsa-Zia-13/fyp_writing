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

    def test_bad_task_token_is_rejected_before_database_or_gemini(self):
        words = ' '.join(f'word{i}' for i in range(150))
        request = self.post({'task_number': 1, 'response': words, 'task_token': 'invalid'})
        with patch('writing_service.views._connection') as connection, patch('writing_service.views.evaluate_response') as evaluate:
            result = views.submit_response(request)
        self.assertEqual(result.status_code, 403)
        connection.assert_not_called()
        evaluate.assert_not_called()

    def test_task_image_matches_test_id_and_real_extension(self):
        self.assertEqual(views._task_image(1), 'test1.jpg')
        self.assertEqual(views._task_image(5), 'test5.png')
        self.assertEqual(views._task_image(13), 'test13.png')
        self.assertIsNone(views._task_image('not-an-id'))
        self.assertIsNone(views._task_image(999))

    def test_random_task_uses_task_specific_table_and_returns_test_id(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [(1, 'Describe chart A'), (5, 'Describe chart B')]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('writing_service.views._connection', return_value=conn), \
                patch('writing_service.views._columns', return_value={'test_id', 'task_text'}), \
                patch('writing_service.views.random.choice', return_value=(5, 'Describe chart B')):
            result = views.task_detail(self.factory.get('/api/tasks/1/?seen=1'), 1)
        body = json.loads(result.content)
        self.assertEqual(body['test_id'], 5)
        self.assertEqual(body['task_text'], 'Describe chart B')
        self.assertEqual(body['image_url'], '/api/writing/task-images/test5.png/')
        self.assertEqual(body['task_number'], 1)

    def test_combined_band_weights_task_two_twice_and_rounds_to_half(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(6.0,), (7.0,)]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor
        with patch('writing_service.views._connection', return_value=conn), \
                patch('writing_service.views._columns', return_value={'score_id', 'overall_band_score'}):
            result = views.combined_score(self.factory.get('/api/scores/combined/'))
        body = json.loads(result.content)
        self.assertEqual(body['combined_writing_band'], 6.5)
        self.assertEqual(body['task1_band'], 6.0)
        self.assertEqual(body['task2_band'], 7.0)


if __name__ == '__main__':
    unittest.main()

"""PostgreSQL persistence for idempotent IELTS writing evaluations."""

import json
from datetime import datetime, timezone

from psycopg import sql

from .scoring import CRITERIA


TABLE_CANDIDATES = ('writing_scores', 'writing_score')
FIELD_CANDIDATES = {
    'response_id': ('response_id', 'writing_response_id', 'submission_id'),
    'task_id': ('task_id', 'writing_task_id'),
    'user_id': ('user_id', 'student_id'),
    'task_achievement': ('task_achievement', 'task_achievement_score'),
    'coherence_cohesion': ('coherence_cohesion', 'coherence_cohesion_score'),
    'lexical_resource': ('lexical_resource', 'lexical_resource_score'),
    'grammar_accuracy': ('grammar_accuracy', 'grammar_accuracy_score'),
    'overall_band_score': ('overall_band_score', 'overall_score', 'overall_band'),
    'evaluation_json': ('evaluation_json', 'evaluation', 'llm_evaluation', 'score_details', 'feedback_json'),
}
ADDITIONS = {
    'response_id': 'text',
    'task_id': 'text',
    'user_id': 'text',
    'task_achievement': 'numeric(3,1)',
    'coherence_cohesion': 'numeric(3,1)',
    'lexical_resource': 'numeric(3,1)',
    'grammar_accuracy': 'numeric(3,1)',
    'overall_band_score': 'numeric(3,1)',
    'evaluation_json': 'jsonb',
    'submission_key': 'text',
    'created_at': 'timestamptz',
}


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


def ensure_score_table(cursor):
    """Reuse a score table when present and add only missing score fields."""
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s)', (list(TABLE_CANDIDATES),)
    )
    found = {row[0] for row in cursor.fetchall()}
    table = next((name for name in TABLE_CANDIDATES if name in found), None)
    if table is None:
        table = 'writing_scores'
        cursor.execute(sql.SQL(
            'CREATE TABLE IF NOT EXISTS {} ('
            'id uuid PRIMARY KEY DEFAULT gen_random_uuid(), '
            'response_id text, submission_key text, task_id text, user_id text, '
            'task_achievement numeric(3,1), coherence_cohesion numeric(3,1), '
            'lexical_resource numeric(3,1), grammar_accuracy numeric(3,1), '
            'overall_band_score numeric(3,1), evaluation_json jsonb NOT NULL, '
            'created_at timestamptz NOT NULL DEFAULT now())'
        ).format(sql.Identifier(table)))

    columns = _columns(cursor, table)
    initial_types = _column_types(cursor, table)
    actual = {}
    for field, alternatives in FIELD_CANDIDATES.items():
        match = next((candidate for candidate in alternatives if candidate in columns), None)
        if match:
            actual[field] = match
    # A stable key prevents duplicate score rows if the same API request is retried.
    existing_key = next((name for name in ('submission_key', 'idempotency_key', 'request_key')
                         if name in columns and initial_types.get(name, ('text', 'text'))[1] in ('text', 'varchar', 'bpchar')), None)
    if existing_key:
        actual['submission_key'] = existing_key
    for field, data_type in ADDITIONS.items():
        if field in actual or field in columns:
            continue
        cursor.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}').format(
            sql.Identifier(table), sql.Identifier(field), sql.SQL(data_type)))
        columns.add(field)
        actual[field] = field
    for field, alternatives in FIELD_CANDIDATES.items():
        if field not in actual:
            # The canonical column was just added where this was missing.
            actual[field] = field
    if 'created_at' in columns:
        actual['created_at'] = 'created_at'

    cursor.execute(sql.SQL('CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})').format(
        sql.Identifier(f'{table}_submission_key_uidx'), sql.Identifier(table),
        sql.Identifier(actual['submission_key'])))
    types = _column_types(cursor, table)
    return table, actual, types


def _placeholder(column, types):
    data_type, udt_name = types.get(column, ('', ''))
    if udt_name == 'uuid':
        return sql.SQL('%s::uuid')
    if data_type == 'jsonb':
        return sql.SQL('%s::jsonb')
    if data_type == 'json':
        return sql.SQL('%s::json')
    return sql.Placeholder()


def persist_evaluation(cursor, *, response_id, submission_key, task_id, user_id,
                       evaluation, overall_band_score):
    table, fields, types = ensure_score_table(cursor)
    payload = {
        'response_id': response_id,
        'submission_key': submission_key,
        'task_id': task_id,
        'user_id': user_id,
        **{criterion: evaluation[criterion]['score'] for criterion in CRITERIA},
        'overall_band_score': overall_band_score,
        'evaluation_json': json.dumps(evaluation, ensure_ascii=False),
        'created_at': datetime.now(timezone.utc),
    }
    selected = [field for field in ADDITIONS if field in payload and fields.get(field)]
    columns = [fields[field] for field in selected]
    values = [payload[field] for field in selected]
    placeholders = [_placeholder(column, types) for column in columns]
    update_fields = [field for field in selected if field not in ('submission_key', 'created_at')]
    assignments = sql.SQL(', ').join(
        sql.SQL('{} = EXCLUDED.{}').format(sql.Identifier(fields[field]), sql.Identifier(fields[field]))
        for field in update_fields
    )
    cursor.execute(sql.SQL(
        'INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}'
    ).format(
        sql.Identifier(table),
        sql.SQL(', ').join(sql.Identifier(column) for column in columns),
        sql.SQL(', ').join(placeholders),
        sql.Identifier(fields['submission_key']),
        assignments,
    ), values)

    persisted_columns = [fields[field] for field in selected]
    cursor.execute(sql.SQL('SELECT {} FROM {} WHERE {} = %s').format(
        sql.SQL(', ').join(sql.Identifier(column) for column in persisted_columns),
        sql.Identifier(table), sql.Identifier(fields['submission_key'])), (submission_key,))
    row = cursor.fetchone()
    if not row:
        raise RuntimeError('Saved writing evaluation could not be read back.')
    saved = dict(zip([description.name for description in cursor.description], row))
    return serialize_saved_evaluation(saved, fields)


def load_saved_evaluation(cursor, submission_key):
    table, fields, _ = ensure_score_table(cursor)
    selected = list(dict.fromkeys(fields[field] for field in (
        'response_id', 'submission_key', 'task_id', 'user_id', *CRITERIA,
        'overall_band_score', 'evaluation_json', 'created_at',
    )))
    cursor.execute(sql.SQL('SELECT {} FROM {} WHERE {} = %s LIMIT 1').format(
        sql.SQL(', ').join(sql.Identifier(column) for column in selected),
        sql.Identifier(table), sql.Identifier(fields['submission_key'])), (submission_key,))
    row = cursor.fetchone()
    if not row:
        return None
    saved = dict(zip([description.name for description in cursor.description], row))
    return serialize_saved_evaluation(saved, fields)


def serialize_saved_evaluation(row, fields):
    evaluation_value = row.get(fields['evaluation_json'])
    if isinstance(evaluation_value, str):
        try:
            evaluation_value = json.loads(evaluation_value)
        except ValueError as error:
            raise RuntimeError('Saved writing evaluation is malformed.') from error
    if not isinstance(evaluation_value, dict):
        raise RuntimeError('Saved writing evaluation is missing.')
    evaluation = dict(evaluation_value)
    for criterion in CRITERIA:
        field = fields[criterion]
        try:
            evaluation[criterion] = {**evaluation[criterion], 'score': float(row[field])}
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError('Saved writing evaluation is incomplete.') from error
    try:
        overall = float(row[fields['overall_band_score']])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('Saved overall writing band score is missing.') from error
    evaluation['overall_band_score'] = overall
    evaluation['response_id'] = str(row[fields['response_id']])
    evaluation['status'] = 'submitted'
    return evaluation

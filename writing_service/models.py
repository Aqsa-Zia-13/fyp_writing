"""Unmanaged mappings for the existing Supabase Writing schema.

No relationships are declared because the live schema has no foreign-key
constraints between prompts, responses, or scores.
"""
from django.db import models
from django.db.models import Func
from django.db.models.functions import Now


class WritingTask1(models.Model):
    id = models.AutoField(primary_key=True)
    test_number = models.IntegerField()
    prompt = models.TextField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = 'writing_task1'
        managed = False


class WritingTask2(models.Model):
    id = models.AutoField(primary_key=True)
    test_number = models.IntegerField()
    prompt = models.TextField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = 'writing_task2'
        managed = False


class _UUIDDatabaseDefault(Func):
    function = 'gen_random_uuid'
    template = '%(function)s()'
    output_field = models.UUIDField()


class WritingResponse1(models.Model):
    response_id = models.UUIDField(primary_key=True, db_default=_UUIDDatabaseDefault())
    test_id = models.TextField()
    task_type = models.TextField()
    response_text = models.TextField()
    word_count = models.IntegerField()
    user_id = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = 'writing_response1'
        managed = False


class WritingResponse2(models.Model):
    response_id = models.UUIDField(primary_key=True, db_default=_UUIDDatabaseDefault())
    test_id = models.TextField()
    task_type = models.TextField()
    response_text = models.TextField()
    word_count = models.IntegerField()
    user_id = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = 'writing_response2'
        managed = False


class _WritingScoreFields(models.Model):
    response_id = models.TextField()
    test_id = models.TextField()
    task_type = models.TextField()
    task_achievement = models.DecimalField(max_digits=3, decimal_places=1)
    coherence_cohesion = models.DecimalField(max_digits=3, decimal_places=1)
    lexical_resource = models.DecimalField(max_digits=3, decimal_places=1)
    grammar_accuracy = models.DecimalField(max_digits=3, decimal_places=1)
    overall_band_score = models.DecimalField(max_digits=3, decimal_places=1)
    evaluation_json = models.JSONField()
    created_at = models.DateTimeField(db_default=Now())
    user_id = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True


class WritingScore1(_WritingScoreFields):
    score_id = models.UUIDField(primary_key=True, db_default=_UUIDDatabaseDefault())

    class Meta:
        db_table = 'writing_score1'
        managed = False


class WritingScore2(_WritingScoreFields):
    score_id = models.UUIDField(primary_key=True, db_default=_UUIDDatabaseDefault())

    class Meta:
        db_table = 'writing_score2'
        managed = False

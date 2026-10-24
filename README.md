# Writing service

The browser calls `fyp_root`; the gateway requires an authenticated account, applies its writing throttle, and forwards the verified user ID with the shared internal service token. The Writing service rejects requests without that token and user identity. Responses and scores are stored with that user's ID and score reads are owner-scoped.

## Run locally

1. From `fyp_writing`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002`.
2. From `fyp_root`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000`.
3. From `fyp_frontend`, run `npm run dev`.

Configure the same `DATABASE_URL` and `INTERNAL_SERVICE_TOKEN` for `fyp_root` and `fyp_writing`, plus `DJANGO_SECRET_KEY` for `fyp_root` and `GEMINI_API_KEY` for `fyp_writing`, in the backend environment or `fyp_root/.env`. `GEMINI_MODEL` is optional and defaults to `gemini-3.5-flash-lite`. Keep all keys on the backend; do not add them to Vite variables. The service loads missing environment values from `fyp_root/.env`.

## Task and image data

Tests are listed from the existing `writing_task1` and `writing_task2` prompt tables and displayed as Test N. Each table must expose a prompt identifier (`test_id` or `id`), a test number (or numeric identifier), and task text (`task_text`, `prompt_text`, `task_prompt`, `prompt`, or `description`). The selected test and exact prompt text are signed into a short-lived task token bound to the authenticated user and rechecked at submission. The response is saved to `writing_response1` or `writing_response2`; its score and complete evaluation JSON are saved separately to `writing_score1` or `writing_score2`. The service creates response and score tables when absent and adds missing compatible columns to existing tables without deleting rows. It does not create or alter the source prompt tables.

For database-side protection, run `sql/writing_rls.sql` in Supabase. It enables and forces RLS on the two prompt tables and all four response/score tables. The service switches each user request to PostgreSQL's non-bypass `authenticated` role and sets transaction-local `request.jwt.claim.sub` from the identity verified and forwarded by `fyp_root`. Policies use `auth.uid()` for owner checks, with restrictive identity guards to prevent broader permissive policies from exposing records. Runtime requests only validate the migrated schema; they do not run DDL under the `authenticated` role.

Task 1 images are resolved as `data/test{test_id}.{extension}`. The service checks the actual `.jpg`, `.jpeg`, `.png`, and `.webp` files and returns no image when one is missing. Image requests also pass through `fyp_root`.

## Scoring and score views

Gemini receives the exact selected task text and submitted response. The existing evaluation prompt, four criterion keys, and validation rules are retained. Backend code calculates each task band from the four criteria, and returns the evaluation read back from the task-specific score table. Task 1 and Task 2 score views fetch saved results. The combined band uses `(Task 1 + 2 × Task 2) / 3`, rounded to the nearest half band with ties rounded up.

## Verification

Run the offline checks from `fyp_writing`:

```powershell
..\.venv\Scripts\python.exe -m unittest writing_service.tests
```

Run the root authentication and gateway checks from `fyp_root`:

```powershell
..\.venv\Scripts\python.exe manage.py test accounts.tests --settings=root_service.settings_test
```

Live Supabase and Gemini checks require outbound network access and valid project credentials. Offline tests do not verify email delivery, live Supabase RLS, or Gemini availability.

# Writing service

The browser calls `fyp_root`; the gateway applies its writing throttle and forwards the request to this service. Writing access is temporarily public for development. The gateway forwards a browser-local development user ID so scores remain separate per browser; authenticated user IDs can replace it when authentication is restored.

## Run locally

1. From `fyp_writing`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002`.
2. From `fyp_root`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000`.
3. From `fyp_frontend`, run `npm run dev`.

Configure `DATABASE_URL`, `DJANGO_SECRET_KEY`, `INTERNAL_SERVICE_TOKEN`, and `GEMINI_API_KEY` in the backend environment or `fyp_root/.env`. `GEMINI_MODEL` is optional and defaults to `gemini-3.5-flash-lite`. Keep all keys on the backend; do not add them to Vite variables. The service loads missing environment values from `fyp_root/.env`.

## Task and image data

Prompts are selected randomly from `writing_task1` and `writing_task2`. Each table must expose a prompt identifier (`test_id` or `id`) and task text (`task_text`, `prompt_text`, `task_prompt`, `prompt`, or `description`). The live Supabase prompt tables currently contain `id`, `test_number`, and `prompt`; their 20 rows have matching `id` and `test_number` values, so `id` serves as the selected prompt's `test_id`. The selected identifier and exact prompt text are signed into a short-lived task token and rechecked at submission. The response is saved to `writing_response1` or `writing_response2`; its score and complete evaluation JSON are saved separately to `writing_score1` or `writing_score2`. The service creates response and score tables when absent and adds missing compatible columns to existing tables without deleting rows. It does not create or alter the source prompt tables.

Task 1 images are resolved as `data/test{test_id}.{extension}`. The service checks the actual `.jpg`, `.jpeg`, `.png`, and `.webp` files and returns no image when one is missing. Image requests also pass through `fyp_root`.

## Scoring and score views

Gemini receives the exact selected task text and submitted response. The existing evaluation prompt, four criterion keys, and validation rules are retained. Backend code calculates each task band from the four criteria, and returns the evaluation read back from the task-specific score table. Task 1 and Task 2 score views fetch saved results. The combined band uses `(Task 1 + 2 × Task 2) / 3`, rounded to the nearest half band with ties rounded up.

## Verification

Run the offline checks from `fyp_writing`:

```powershell
..\.venv\Scripts\python.exe -m unittest writing_service.tests
```

Live Supabase and Gemini checks require outbound network access and valid project credentials. Do not treat offline tests as evidence that live integration is available.

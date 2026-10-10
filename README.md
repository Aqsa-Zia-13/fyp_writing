# Writing service

The frontend calls the writing API through `fyp_root`; it never connects to this service directly.

## Run locally

Use the project's existing Python environment and the Supabase `DATABASE_URL` from `fyp_root/.env`.

1. From `fyp_writing`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002`.
2. From `fyp_root`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000`.
3. From `fyp_frontend`, run `npm run dev`.

The service reads prompts from `wr_writing_tasks`, `writing_tasks`, or `writing_task`. Task tables should expose a task label (`task_number`, `task_type`, or `id`) and a prompt (`prompt_text` or `prompt`). Prompt selection is randomized and avoids prompt IDs already seen in the browser until a new cycle. Task 1 images are served from `data/` through the `fyp_root` gateway. If no response table exists, the service creates `writing_responses` on the first submission; an existing `writing_responses` or `writing_response` table is reused when it has an `id`/`response_id` and a response text column. Optional response columns `task_id`, `user_id`, and `word_count` are populated when present.

Task 1 is timed for 20 minutes and Task 2 for 40 minutes. The API enforces a non-empty response of 150–250 words before inserting it.

## IELTS writing evaluation

Valid submissions are evaluated on the backend with Gemini 3.5 Flash-Lite (`gemini-3.5-flash-lite`). Set `GEMINI_API_KEY` in the environment available to `fyp_writing` (or the shared `fyp_root/.env` loaded by its settings). The frontend sends submissions and retrieves task images through `fyp_root`; it never receives the Gemini key.

The writing service creates `writing_scores` when it is absent. If a compatible `writing_scores` or `writing_score` table already exists, it reuses recognized response, task, user, criterion, overall-score, and evaluation columns, adding only missing score fields and a unique submission key. Scores are idempotent for repeated identical submissions. The API returns the evaluation read back from PostgreSQL. If Gemini is unavailable, the response text remains saved and the API returns a retryable service error.

Run the offline scoring and API unit tests from `fyp_writing` with `..\.venv\Scripts\python.exe -m unittest writing_service.tests`.

# Writing service

The frontend calls the writing API through `fyp_root`; it never connects to this service directly.

## Run locally

Use the project's existing Python environment and the Supabase `DATABASE_URL` from `fyp_root/.env`.

1. From `fyp_writing`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002`.
2. From `fyp_root`, run `..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000`.
3. From `fyp_frontend`, run `npm run dev`.

The service reads prompts from `wr_writing_tasks`, `writing_tasks`, or `writing_task`. Task tables should expose a task label (`task_number`, `task_type`, or `id`) and a prompt (`prompt_text` or `prompt`). For tables with multiple test sets, it uses the lowest `test_id` first. If no response table exists, the service creates `writing_responses` on the first submission; an existing `writing_responses` or `writing_response` table is reused when it has an `id`/`response_id` and a response text column. Optional response columns `task_id`, `user_id`, and `word_count` are populated when present.

Task 1 is timed for 20 minutes and Task 2 for 40 minutes. The API enforces a non-empty response of 150–250 words before inserting it.

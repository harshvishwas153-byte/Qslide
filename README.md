# Qslide
Qslide is an AI-powered quiz platform where learners can upload PPT or PDF files to instantly generate MCQ's quizzes with custom question counts and timers. Tutors can create quizzes, share them with students, track scores, and review which answers students got right or wrong

## Content moderation

Qslide blocks quiz generation or saving when uploaded text, generated quiz output, tutor-created quizzes, or answer-explanation requests contain abusive or offensive language.

To add more blocked words or phrases without changing code, set `BLOCKED_CONTENT_TERMS` as a comma-separated environment variable.

## Supabase deployment

Qslide uses SQLite and local uploads when Supabase variables are missing, which is useful for local development. In production, add these environment variables to use Supabase Postgres and direct-to-storage PPT/PDF uploads:

```env
DATABASE_URL=your_supabase_transaction_pooler_connection_string
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your_anon_public_key
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_STORAGE_BUCKET=uploads
MAX_UPLOAD_BYTES=50000000
COMPRESSED_UPLOAD_TARGET_BYTES=50000000
```

Keep `SUPABASE_SERVICE_ROLE_KEY` only in server-side environment variables. Do not expose it in browser JavaScript.

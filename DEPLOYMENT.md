# Deploying Qslide

GitHub Pages only hosts static HTML/CSS/JS. Qslide is a Flask app, so it needs a Python web service such as Render, Railway, Fly.io, PythonAnywhere, or a VPS.

## Render quick deploy

1. Push this project to GitHub.
2. In Render, create a new **Web Service** from the repository.
3. Use these settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
4. Add environment variables:
   - `GEMINI_API_KEY`: your Gemini API key
   - `SECRET_KEY`: a long random string
   - `GEMINI_MODEL`: optional, for example `gemini-2.5-flash`
5. Deploy.

## Important storage note

The app currently uses SQLite and local file uploads. On hosts with an ephemeral filesystem, data can disappear after restarts or redeploys unless you add persistent storage or move to a managed database.

If your host gives you a persistent disk, set:

- `DATABASE_PATH=/var/data/qslide.db`
- `UPLOAD_FOLDER=/var/data/uploads`

For a production app with real users, move from SQLite/local uploads to a hosted database and object/file storage.

## Security note

Do not commit `.env`, `qslide.db`, `uploads/`, or `__pycache__/` to GitHub. If an API key was already pushed to GitHub, rotate it in the provider dashboard and create a new one.

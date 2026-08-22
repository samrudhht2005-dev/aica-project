AICA Desktop — first-run configuration
======================================

Packaged AICA 1.0.2 uses a local SQLite database by default:

   %AppData%\AICA\aica.db

PostgreSQL is not required to start the desktop app.

Optional overrides (edit %AppData%\AICA\config.env):

1. DATABASE_URL=postgresql://...  — only if you want hosted/web PostgreSQL
2. GEMINI_API_KEY=...            — IRA / OCR / AI Optimization
3. AICA_SECRET_KEY=...           — optional session secret

Do NOT leave USER, PASSWORD, or HOST template words in DATABASE_URL.

Logs:
   %AppData%\AICA\logs\

Web developers continue to use the project .env + uvicorn + PostgreSQL as before.
The desktop build does not replace the web workflow.

Configuration precedence:
  1) Process environment (highest)
  2) %AppData%\AICA\config.env (valid values only)
  3) Packaged desktop SQLite default (%AppData%\AICA\aica.db)
  4) Project .env (development / web)

AICA Desktop — first-run configuration
======================================

1. After install, edit:

   %AppData%\AICA\config.env

2. Set DATABASE_URL to a REAL PostgreSQL connection string.
   Do NOT leave USER, PASSWORD, or HOST template words — AICA ignores those
   and will refuse to start rather than connect to a fake host.

   Development (this PC): use the same URL as your project .env.
   Production (other laptops): use your hosted PostgreSQL URL.

3. Set GEMINI_API_KEY for IRA / OCR / AI features (optional for login/POS).
   Never share this key or commit it to Git.

4. Launch AICA from the Start Menu or Desktop shortcut.

Logs:
   %AppData%\AICA\logs\

Web developers continue to use the project .env + uvicorn as before.
The desktop build does not replace the web workflow.

Configuration precedence:
  1) Process environment (highest)
  2) %AppData%\AICA\config.env (valid values only)
  3) Project .env (development)
  4) No fake DATABASE_URL fallback for desktop

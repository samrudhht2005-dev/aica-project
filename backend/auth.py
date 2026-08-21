import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Optional

from fastapi import Request, Response

SESSION_COOKIE = "aica_session"
UI_MODE_COOKIE = "aica_ui_mode"
REMEMBER_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours
UI_MODE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PBKDF2_ITERATIONS = 390000


def _secret_key() -> str:
    env_key = os.environ.get("AICA_SECRET_KEY")
    if env_key:
        return env_key
    secret_path = os.path.join(os.path.dirname(__file__), "..", "database", ".session_secret")
    secret_path = os.path.abspath(secret_path)
    if os.path.exists(secret_path):
        with open(secret_path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_urlsafe(48)
    os.makedirs(os.path.dirname(secret_path), exist_ok=True)
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters)
        ).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def valid_gstin(gstin: str) -> bool:
    return bool(GSTIN_RE.match((gstin or "").strip().upper()))


def create_session_token(user_id: int, org_id: int, remember: bool) -> str:
    max_age = REMEMBER_MAX_AGE if remember else SESSION_MAX_AGE
    payload = {
        "uid": int(user_id),
        "oid": int(org_id),
        "exp": int(time.time()) + max_age,
        "rm": bool(remember),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret_key().encode("utf-8"), body, hashlib.sha256).hexdigest()
    raw = json.dumps({"p": payload, "s": sig}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def read_session_token(token: str, max_age: int = REMEMBER_MAX_AGE) -> Optional[dict]:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        payload = raw.get("p") or {}
        sig = raw.get("s") or ""
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        expected = hmac.new(_secret_key().encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(sig), expected):
            return None
        exp = int(payload.get("exp") or 0)
        if exp < int(time.time()):
            return None
        if "uid" in payload and "oid" in payload:
            return payload
    except (ValueError, TypeError, json.JSONDecodeError, KeyError):
        return None
    return None


def set_session_cookie(response: Response, user_id: int, org_id: int, remember: bool):
    token = create_session_token(user_id, org_id, remember)
    max_age = REMEMBER_MAX_AGE if remember else SESSION_MAX_AGE
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")


def get_ui_mode(request: Request) -> str | None:
    mode = (request.cookies.get(UI_MODE_COOKIE) or "").strip().lower()
    if mode in ("pos", "org"):
        return mode
    return None


def set_ui_mode_cookie(response: Response, mode: str):
    mode = (mode or "").strip().lower()
    if mode not in ("pos", "org"):
        raise ValueError("ui mode must be pos or org")
    response.set_cookie(
        key=UI_MODE_COOKIE,
        value=mode,
        httponly=False,
        samesite="lax",
        max_age=UI_MODE_MAX_AGE,
        path="/",
    )


def clear_ui_mode_cookie(response: Response):
    response.delete_cookie(UI_MODE_COOKIE, path="/")


def session_from_request(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return read_session_token(token, REMEMBER_MAX_AGE)


def is_public_path(path: str) -> bool:
    if path.startswith("/static"):
        return True
    public = {"/login", "/signup", "/logout", "/favicon.ico"}
    return path in public or path.startswith("/login") or path.startswith("/signup")

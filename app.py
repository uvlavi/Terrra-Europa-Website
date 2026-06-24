#!/usr/bin/env python3
"""
TERRRA Europa — terrra-europa.com
Multi-user auth, admin panel, email composer (Resend), contacts CRM.

Email setup:
  Receive — ImprovMX forwards *@terrra-europa.com to personal Gmail accounts.
  Send    — Resend SMTP from Gmail "Send mail as", or from /compose on the website.
"""
import io
import csv
import os
import json
import hmac
import hashlib
import secrets
import threading
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import i18n
import analytics
import legal_content

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
PUBLIC_DIR     = BASE_DIR / "public"
TEMPLATES_DIR  = BASE_DIR / "templates"
DATA_DIR       = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ACCESS_KEY     = os.environ.get("ACCESS_KEY", "28061972")
SESSION_TTL    = 60 * 60 * 24 * 30
SESSION_COOKIE = "eu_session"

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "resend")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "yuval@terrra-europa.com")
FROM_NAME      = os.environ.get("FROM_NAME", "Yuval Lavi")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "yuval@terrra-europa.com")
GA4_ID         = os.environ.get("GA4_ID", "")
DOMAIN         = "terrra-europa.com"


_file_lock = threading.Lock()


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TERRRA Europa")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR)), name="static")


def render(request: Request, name: str, ctx: dict | None = None):
    """Render a template with i18n context auto-injected.

    Adds: t (translator), lang, lang_labels, supported_langs, ga4_id, sun_cfg.
    """
    lang = i18n.resolve_lang(request)
    base = {
        "t": i18n.translator(lang),
        "lang": lang,
        "lang_labels": i18n.LANG_LABELS,
        "supported_langs": i18n.SUPPORTED,
        "ga4_id": GA4_ID,
        "sun_cfg": _load_sun_config(),
    }
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, name, base)


# ── Hero sun config (live-tunable via /admin/hero-sun) ────────────────────────
SUN_CONFIG_FILE = DATA_DIR / "hero_sun.json"
SUN_DEFAULTS = {
    "size_px":           180,   # diameter of the sun disc
    "image_brightness":  1.50,  # max image-filter brightness at solar noon (steady plateau through midday)
    "sun_brightness":    0.55,  # multiplier on the sun's own halo intensity (filter brightness)
    "noon_neutrality":   0.00,  # multiplier on noon-plateau colour cast (0 = warm sepia kept; 1 = neutral)
    "colour_mix":        1.00,  # 0 = image-only colour shift, 1 = sun-only colour shift, 0.5 = both
    "peak_altitude_vh":  7,     # vertical position at peak — smaller = higher in sky
    "horizon_vh":        72,    # vertical position when sun is below horizon
    "cycle_seconds":     150,   # full day-night cycle duration
}
# Slider bounds are centred ±~50% around each default so the chosen value sits
# mid-slider, leaving headroom for nudges in both directions without re-saving.
SUN_BOUNDS = {
    "size_px":           (60, 300),
    "image_brightness":  (0.8, 2.4),
    "sun_brightness":    (0.4, 1.6),
    "noon_neutrality":   (0.0, 1.5),
    "colour_mix":        (0.0, 1.0),
    "peak_altitude_vh":  (2, 18),
    "horizon_vh":        (55, 95),
    "cycle_seconds":     (60, 240),
}

def _load_sun_config() -> dict:
    if not SUN_CONFIG_FILE.exists():
        return dict(SUN_DEFAULTS)
    try:
        with open(SUN_CONFIG_FILE, "r") as f:
            saved = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(SUN_DEFAULTS)
    cfg = dict(SUN_DEFAULTS)
    for k in SUN_DEFAULTS:
        if k in saved:
            cfg[k] = saved[k]
    return cfg

def _save_sun_config(cfg: dict) -> None:
    clean = {}
    for k, default in SUN_DEFAULTS.items():
        v = cfg.get(k, default)
        try:
            v = float(v) if isinstance(default, float) else int(v)
        except (TypeError, ValueError):
            v = default
        lo, hi = SUN_BOUNDS[k]
        v = max(lo, min(hi, v))
        clean[k] = v
    with open(SUN_CONFIG_FILE, "w") as f:
        json.dump(clean, f, indent=2)


# ── Password helpers ──────────────────────────────────────────────────────────
def _hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return h.hex(), salt

def _verify_password(password: str, hash_hex: str, salt: str) -> bool:
    computed, _ = _hash_password(password, salt)
    return hmac.compare_digest(computed, hash_hex)

def _gen_temp_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))

# ── User helpers ──────────────────────────────────────────────────────────────
USERS_FILE = DATA_DIR / "users.json"

def _load_users() -> list:
    if not USERS_FILE.exists():
        return []
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return []

def _save_users(users: list):
    with _file_lock:
        USERS_FILE.write_text(json.dumps(users, indent=2))

def _get_user(username: str) -> dict | None:
    return next((u for u in _load_users() if u["username"] == username.lower().strip()), None)

def _seed_admin():
    if _load_users():
        return
    ph, salt = _hash_password(ACCESS_KEY)
    alias = FROM_EMAIL.split("@")[0]
    external = os.environ.get("ADMIN_EXTERNAL_EMAIL", NOTIFY_EMAIL)
    _save_users([{
        "username":           alias,
        "display_name":       FROM_NAME,
        "external_email":     external,
        "email_alias":        alias,
        "password_hash":      ph,
        "password_salt":      salt,
        "temp_password_hash": None,
        "temp_password_salt": None,
        "is_admin":           True,
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "last_login":         None,
    }])

# ── Session helpers ───────────────────────────────────────────────────────────
_sessions: dict = {}

def _make_session(user: dict, temp_auth: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username":     user["username"],
        "display_name": user.get("display_name", user["username"]),
        "email_alias":  user.get("email_alias", user["username"]),
        "is_admin":     user.get("is_admin", False),
        "temp_auth":    temp_auth,
        "expires_ts":   datetime.now(timezone.utc).timestamp() + SESSION_TTL,
    }
    return token

def _get_session(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = _sessions.get(token)
    if not s:
        return None
    if s["expires_ts"] < datetime.now(timezone.utc).timestamp():
        del _sessions[token]
        return None
    return s

# ── Email sending ─────────────────────────────────────────────────────────────
async def _send_email(to: str, subject: str, body: str,
                      reply_to: str = None,
                      from_email: str = None, from_name: str = None) -> dict:
    f_email = from_email or FROM_EMAIL
    f_name  = from_name  or FROM_NAME

    if EMAIL_PROVIDER == "resend":
        if not RESEND_API_KEY:
            return {"ok": False, "error": "RESEND_API_KEY not set"}
        payload = {"from": f"{f_name} <{f_email}>", "to": [to],
                   "subject": subject, "text": body}
        if reply_to:
            payload["reply_to"] = reply_to
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=15)
        return {"ok": True} if r.status_code in (200, 201) else {"ok": False, "error": r.text}

    elif EMAIL_PROVIDER == "brevo":
        if not BREVO_API_KEY:
            return {"ok": False, "error": "BREVO_API_KEY not set"}
        payload = {"sender": {"name": f_name, "email": f_email},
                   "to": [{"email": to}], "subject": subject, "textContent": body}
        if reply_to:
            payload["replyTo"] = {"email": reply_to}
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.brevo.com/v3/smtp/email",
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                json=payload, timeout=15)
        return {"ok": True} if r.status_code in (200, 201) else {"ok": False, "error": r.text}

    else:
        import smtplib, ssl
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        if not SMTP_USER or not SMTP_PASS:
            return {"ok": False, "error": "SMTP credentials not set"}
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{f_name} <{f_email}>"
        msg["To"]      = to
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.attach(MIMEText(body, "plain"))
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(f_email, [to], msg.as_string())
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

# ── Send log ──────────────────────────────────────────────────────────────────
def _log_send(sender: str, to: str, subject: str, ok: bool, error: str = ""):
    log_path = DATA_DIR / "send_log.json"
    entries = []
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text())
        except Exception:
            pass
    entries.append({"ts": datetime.now(timezone.utc).isoformat(),
                    "sender": sender, "to": to, "subject": subject,
                    "ok": ok, "error": error})
    log_path.write_text(json.dumps(entries[-500:], indent=2))

# ── Contacts ──────────────────────────────────────────────────────────────────
def _read_contacts() -> list:
    p = DATA_DIR / "contacts.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []

def _log_contact(email: str, role: str, visitor_id: str = "", consent_ts: str = ""):
    p = DATA_DIR / "contacts.json"
    entries = _read_contacts()
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "role": role,
        "visitor_id": visitor_id,
        "consent_ts": consent_ts,  # GDPR Art. 7(1) — proof of consent
    })
    p.write_text(json.dumps(entries, indent=2))

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    _seed_admin()

# ── Public routes ──────────────────────────────────────────────────────────────
VISITOR_COOKIE = "eu_visitor"
VISITOR_TTL    = 60 * 60 * 24 * 365  # 1 year


@app.get("/lang/{code}")
async def set_lang(code: str, request: Request, next: str = "/"):
    code = (code or "").strip().lower()
    if code not in i18n.SUPPORTED:
        code = i18n.DEFAULT
    # Only allow same-site redirects
    if not next.startswith("/"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(i18n.LANG_COOKIE, code, max_age=i18n.LANG_COOKIE_TTL,
                    samesite="lax")
    return resp

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return render(request, "index.html")

@app.get("/impressum", response_class=HTMLResponse)
@app.get("/imprint", response_class=HTMLResponse)
@app.get("/note-legali", response_class=HTMLResponse)
async def impressum_page(request: Request, lang: str = ""):
    return _render_legal(request, "impressum", lang)

@app.get("/datenschutz", response_class=HTMLResponse)
@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy-it", response_class=HTMLResponse)
async def datenschutz_page(request: Request, lang: str = ""):
    return _render_legal(request, "datenschutz", lang)

def _render_legal(request: Request, page: str, lang_override: str):
    # Path-based default: German URLs default to DE, English URLs to EN, etc.
    path = request.url.path
    if lang_override and lang_override in i18n.SUPPORTED:
        active = lang_override
    elif path in ("/impressum", "/datenschutz"):
        active = "de"
    elif path in ("/note-legali", "/privacy-it"):
        active = "it"
    elif path in ("/imprint", "/privacy"):
        active = "en"
    else:
        active = i18n.resolve_lang(request)
    content = legal_content.PAGES[page][active]
    # Shell (cookie banner, footer links, html lang) follows the legal page's
    # own language — not the visitor's saved cookie — so the page is internally
    # consistent regardless of where the user came from.
    return templates.TemplateResponse(request, "legal.html", {
        "page_title":  content["title"],
        "body":        content["body"],
        "active_lang": active,
        "lang":        active,
        "ga4_id":      GA4_ID,
        "t":           i18n.translator(active),
        "lang_labels": i18n.LANG_LABELS,
        "supported_langs": i18n.SUPPORTED,
    })

@app.get("/robots.txt")
async def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /admin/\n"
        "Disallow: /compose\n"
        "Disallow: /contacts\n"
        "Disallow: /change-password\n"
        "Disallow: /forgot\n"
        "Disallow: /login\n"
        f"Sitemap: https://{DOMAIN}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")

@app.get("/sitemap.xml")
async def sitemap():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [
        ("/", "1.0", "weekly"),
        ("/impressum", "0.5", "yearly"),
        ("/imprint", "0.4", "yearly"),
        ("/note-legali", "0.4", "yearly"),
        ("/datenschutz", "0.5", "yearly"),
        ("/privacy", "0.4", "yearly"),
        ("/privacy-it", "0.4", "yearly"),
    ]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    body += 'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
    for path, prio, freq in urls:
        body += f"  <url>\n    <loc>https://{DOMAIN}{path}</loc>\n"
        body += f"    <lastmod>{today}</lastmod>\n"
        body += f"    <changefreq>{freq}</changefreq>\n"
        body += f"    <priority>{prio}</priority>\n"
        if path == "/":
            for code in ("en", "de", "it"):
                body += f'    <xhtml:link rel="alternate" hreflang="{code}" href="https://{DOMAIN}/" />\n'
        body += "  </url>\n"
    body += "</urlset>\n"
    return Response(content=body, media_type="application/xml")

@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {
        "domain": DOMAIN,
        "resend_api_key": RESEND_API_KEY,
    })

@app.post("/contact")
async def contact(request: Request):
    body  = await request.json()
    email = (body.get("email") or "").strip()
    role  = (body.get("role") or "other").strip()
    consent = bool(body.get("consent"))
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Invalid email"}, status_code=400)
    if not consent:
        return JSONResponse({"ok": False, "error": "Consent required"}, status_code=400)
    # Visitor ID is only created at form submission (with explicit consent),
    # never on first page load — TTDSG § 25(1) compliance.
    visitor_id = request.cookies.get(VISITOR_COOKIE) or secrets.token_urlsafe(16)
    _log_contact(email, role, visitor_id, consent_ts=datetime.now(timezone.utc).isoformat())
    role_labels = {"landowner": "Landowner / land manager",
                   "institutional_partner": "Institutional partner",
                   "carbon_buyer": "Institutional partner",
                   "project_developer": "Project developer", "other": "Other"}
    subject = f"TERRRA Europa — new contact: {role_labels.get(role, role)}"
    msg = (f"New contact signup on terrra-europa.com\n\n"
           f"Email: {email}\nRole:  {role_labels.get(role, role)}\n"
           f"Time:  {datetime.now(timezone.utc).isoformat()}\n")
    if RESEND_API_KEY or BREVO_API_KEY or (SMTP_USER and SMTP_PASS):
        await _send_email(to=NOTIFY_EMAIL, subject=subject, body=msg, reply_to=email)
    resp = JSONResponse({"ok": True})
    if not request.cookies.get(VISITOR_COOKIE):
        resp.set_cookie(VISITOR_COOKIE, visitor_id, max_age=VISITOR_TTL, samesite="lax")
    return resp

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    s = _get_session(request)
    if s and not s.get("temp_auth"):
        return RedirectResponse("/compose", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": error})

@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    user = _get_user(username)
    if not user:
        resp = RedirectResponse("/login?error=credentials", status_code=303)
        return resp

    # Check temp password first
    if user.get("temp_password_hash") and user.get("temp_password_salt"):
        if _verify_password(password, user["temp_password_hash"], user["temp_password_salt"]):
            token = _make_session(user, temp_auth=True)
            resp = RedirectResponse("/change-password", status_code=303)
            resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                            httponly=True, samesite="lax")
            return resp

    # Check normal password
    if not _verify_password(password, user["password_hash"], user["password_salt"]):
        return RedirectResponse("/login?error=credentials", status_code=303)

    # Update last_login
    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["last_login"] = datetime.now(timezone.utc).isoformat()
    _save_users(users)

    token = _make_session(user, temp_auth=False)
    resp = RedirectResponse("/compose", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                    httponly=True, samesite="lax")
    return resp

@app.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request, sent: str = "", error: str = ""):
    return templates.TemplateResponse(request, "forgot.html",
                                      {"sent": sent, "error": error})

@app.post("/forgot")
async def forgot_submit(username: str = Form(...)):
    username = username.strip().lower()
    user = _get_user(username)
    # Always redirect with "sent" to avoid user enumeration
    if not user or not user.get("external_email"):
        return RedirectResponse("/forgot?sent=1", status_code=303)

    temp_pw = _gen_temp_password()
    ph, salt = _hash_password(temp_pw)

    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["temp_password_hash"] = ph
            u["temp_password_salt"] = salt
    _save_users(users)

    subject = "TERRRA Europa — your temporary login"
    body = (f"Hi {user.get('display_name', username)},\n\n"
            f"A temporary password was requested for your TERRRA Europa account.\n\n"
            f"Username:          {username}\n"
            f"Temporary password: {temp_pw}\n\n"
            f"Login at: https://{DOMAIN}/login\n\n"
            f"You will be asked to set a new password after logging in.\n\n"
            f"If you did not request this, you can ignore this email.\n\n"
            f"— TERRRA Europa\n")
    await _send_email(to=user["external_email"], subject=subject, body=body)
    return RedirectResponse("/forgot?sent=1", status_code=303)

@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, error: str = ""):
    s = _get_session(request)
    if not s or not s.get("temp_auth"):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "change_password.html",
                                      {"error": error, "display_name": s["display_name"]})

@app.post("/change-password")
async def change_password_submit(request: Request,
                                  new_password: str = Form(...),
                                  confirm_password: str = Form(...)):
    s = _get_session(request)
    if not s or not s.get("temp_auth"):
        return RedirectResponse("/login", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse("/change-password?error=mismatch", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse("/change-password?error=short", status_code=303)

    ph, salt = _hash_password(new_password)
    users = _load_users()
    for u in users:
        if u["username"] == s["username"]:
            u["password_hash"]      = ph
            u["password_salt"]      = salt
            u["temp_password_hash"] = None
            u["temp_password_salt"] = None
    _save_users(users)

    # Upgrade session — clear temp_auth
    token = request.cookies.get(SESSION_COOKIE)
    if token and token in _sessions:
        _sessions[token]["temp_auth"] = False

    return RedirectResponse("/compose", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token and token in _sessions:
        del _sessions[token]
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# ── Protected routes ──────────────────────────────────────────────────────────
@app.get("/compose", response_class=HTMLResponse)
async def compose_page(request: Request):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    if s.get("temp_auth"):
        return RedirectResponse("/change-password", status_code=302)
    alias = s.get("email_alias", s["username"])
    return templates.TemplateResponse(request, "compose.html", {
        "sender_name":  s["display_name"],
        "from_email":   f"{alias}@{DOMAIN}",
        "is_admin":     s.get("is_admin", False),
        "prefill_to":   request.query_params.get("to", ""),
        "prefill_subj": request.query_params.get("subject", ""),
    })

@app.post("/send")
async def send_email_route(request: Request,
                            to: str = Form(...), subject: str = Form(...),
                            body: str = Form(...), reply_to: str = Form(default="")):
    s = _get_session(request)
    if not s or s.get("temp_auth"):
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    if not to or "@" not in to:
        return JSONResponse({"ok": False, "error": "Invalid recipient"})
    if not subject.strip() or not body.strip():
        return JSONResponse({"ok": False, "error": "Subject and body required"})

    alias = s.get("email_alias", s["username"])
    result = await _send_email(
        to=to.strip(), subject=subject.strip(), body=body.strip(),
        reply_to=reply_to.strip() or None,
        from_email=f"{alias}@{DOMAIN}", from_name=s["display_name"]
    )
    _log_send(s["display_name"], to, subject, result["ok"], result.get("error", ""))
    if result["ok"]:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": result.get("error", "Send failed")}, status_code=500)

@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    s = _get_session(request)
    if not s or s.get("temp_auth"):
        return RedirectResponse("/login", status_code=302)
    entries = _read_contacts()
    counts = {}
    visitor_counts = {}
    for e in entries:
        r = e.get("role", "other")
        counts[r] = counts.get(r, 0) + 1
        v = e.get("visitor_id", "")
        if v:
            visitor_counts[v] = visitor_counts.get(v, 0) + 1
    # Annotate entries with repeat count
    for e in entries:
        v = e.get("visitor_id", "")
        e["repeat_count"] = visitor_counts.get(v, 1) if v else 1
    return templates.TemplateResponse(request, "contacts.html", {
        "contacts": entries, "counts": counts,
        "sender_name": s["display_name"],
        "is_admin": s.get("is_admin", False),
    })

@app.get("/contacts/export")
async def contacts_export(request: Request):
    s = _get_session(request)
    if not s or s.get("temp_auth"):
        return RedirectResponse("/login", status_code=302)
    entries = _read_contacts()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ts", "email", "role"])
    writer.writeheader()
    writer.writerows(entries)
    buf.seek(0)
    fname = f"terrra-europa-contacts-{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})

# ── Admin routes ──────────────────────────────────────────────────────────────
def _require_admin(request: Request):
    s = _get_session(request)
    if not s or s.get("temp_auth") or not s.get("is_admin"):
        return None
    return s

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    users = _load_users()
    return templates.TemplateResponse(request, "admin.html", {
        "users": users,
        "sender_name": s["username"],
        "domain": DOMAIN,
        "resend_api_key": RESEND_API_KEY,
    })

@app.post("/admin/users")
async def admin_create_user(request: Request,
                             username: str = Form(...),
                             display_name: str = Form(...),
                             external_email: str = Form(...),
                             email_alias: str = Form(default=""),
                             is_admin: str = Form(default="")):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    username = username.strip().lower()
    if not username or _get_user(username):
        return RedirectResponse("/admin?error=exists", status_code=303)

    temp_pw = _gen_temp_password()
    ph, salt = _hash_password(temp_pw)

    users = _load_users()
    users.append({
        "username":           username,
        "display_name":       display_name.strip(),
        "external_email":     external_email.strip(),
        "email_alias":        username,  # always equals username
        "password_hash":      ph,
        "password_salt":      salt,
        "temp_password_hash": ph,   # force password change on first login
        "temp_password_salt": salt,
        "is_admin":           bool(is_admin),
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "last_login":         None,
    })
    _save_users(users)

    # Welcome email — short, points to /help for Gmail setup
    subject = "Welcome to TERRRA Europa"
    body = f"""Hi {display_name.strip()},

Your TERRRA Europa account is ready.

Log in:    https://{DOMAIN}/login
Username:  {username}
Password:  {temp_pw}

You'll set your own password on first login.

Your email address is {username}@{DOMAIN} — all incoming mail is
automatically forwarded to {external_email.strip()}.

To send mail AS {username}@{DOMAIN} from your Gmail (so replies look
like they come from your TERRRA address), follow the quick setup guide:
  https://{DOMAIN}/help

— TERRRA Europa
"""
    await _send_email(to=external_email.strip(), subject=subject, body=body)
    return RedirectResponse("/admin?created=1", status_code=303)

@app.post("/admin/users/{username}/update")
async def admin_update_user(request: Request, username: str,
                             display_name: str = Form(...),
                             external_email: str = Form(...),
                             email_alias: str = Form(default=""),
                             is_admin: str = Form(default="")):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["display_name"]   = display_name.strip()
            u["external_email"] = external_email.strip()
            u["email_alias"]    = username  # always equals username
            u["is_admin"]       = bool(is_admin)
    _save_users(users)
    return RedirectResponse("/admin?updated=1", status_code=303)

@app.post("/admin/users/{username}/reset")
async def admin_reset_user(request: Request, username: str):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    user = _get_user(username)
    if not user or not user.get("external_email"):
        return RedirectResponse("/admin?error=noemail", status_code=303)

    temp_pw = _gen_temp_password()
    ph, salt = _hash_password(temp_pw)
    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["temp_password_hash"] = ph
            u["temp_password_salt"] = salt
    _save_users(users)

    subject = "TERRRA Europa — new temporary password"
    body = (f"Hi {user.get('display_name', username)},\n\n"
            f"A new temporary password was issued for your account.\n\n"
            f"Username:          {username}\n"
            f"Temporary password: {temp_pw}\n\n"
            f"Login at: https://{DOMAIN}/login\n\n"
            f"— TERRRA Europa\n")
    await _send_email(to=user["external_email"], subject=subject, body=body)
    return RedirectResponse("/admin?reset=1", status_code=303)

@app.post("/admin/users/{username}/send_setup")
async def admin_send_setup(request: Request, username: str):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    user = _get_user(username)
    if not user or not user.get("external_email"):
        return RedirectResponse("/admin?error=noemail", status_code=303)

    alias = user.get("email_alias", username)
    name  = user.get("display_name", username)

    subject = f"TERRRA Europa — email setup for {alias}@{DOMAIN}"
    body = f"""Hi {name},

Reminder: to send mail AS {alias}@{DOMAIN} from your Gmail account,
follow the step-by-step guide here:

  https://{DOMAIN}/help

It takes about 3 minutes — you'll need the SMTP credentials shown on
that page (they're the same for everyone on the team).

Receiving works automatically: any mail sent to {alias}@{DOMAIN} is
forwarded to {user['external_email']} with no setup needed.

— TERRRA Europa
"""
    await _send_email(to=user["external_email"], subject=subject, body=body)
    return RedirectResponse("/admin?setup_sent=1", status_code=303)

@app.get("/admin/analytics", response_class=HTMLResponse)
async def admin_analytics(request: Request):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    data = analytics.summary()
    return templates.TemplateResponse(request, "analytics.html", {
        "data": data,
        "sender_name": s["display_name"],
    })


@app.get("/admin/hero-sun", response_class=HTMLResponse)
async def admin_hero_sun(request: Request, saved: str = ""):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "hero_sun_admin.html", {
        "sun_cfg":      _load_sun_config(),
        "sun_defaults": SUN_DEFAULTS,
        "sun_bounds":   SUN_BOUNDS,
        "sender_name":  s["display_name"],
        "saved":        bool(saved),
    })


@app.post("/admin/hero-sun")
async def admin_hero_sun_save(request: Request,
                              size_px: int = Form(...),
                              image_brightness: float = Form(...),
                              sun_brightness: float = Form(...),
                              noon_neutrality: float = Form(...),
                              colour_mix: float = Form(...),
                              peak_altitude_vh: int = Form(...),
                              horizon_vh: int = Form(...),
                              cycle_seconds: int = Form(...),
                              reset: str = Form(default="")):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    if reset:
        _save_sun_config(dict(SUN_DEFAULTS))
    else:
        _save_sun_config({
            "size_px":          size_px,
            "image_brightness": image_brightness,
            "sun_brightness":   sun_brightness,
            "noon_neutrality":  noon_neutrality,
            "colour_mix":       colour_mix,
            "peak_altitude_vh": peak_altitude_vh,
            "horizon_vh":       horizon_vh,
            "cycle_seconds":    cycle_seconds,
        })
    return RedirectResponse("/admin/hero-sun?saved=1", status_code=303)


@app.post("/admin/users/{username}/delete")
async def admin_delete_user(request: Request, username: str):
    s = _require_admin(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    # Prevent self-deletion
    if username == s["username"]:
        return RedirectResponse("/admin?error=self", status_code=303)
    users = [u for u in _load_users() if u["username"] != username]
    _save_users(users)
    return RedirectResponse("/admin?deleted=1", status_code=303)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8103"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

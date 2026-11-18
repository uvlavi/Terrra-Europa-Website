#!/usr/bin/env python3
"""
Nova Terrra Europa website — parameterised regional entity site.

Every entity-identifying string (domain, brand, Firma, address, HRB, email
addresses, country labels) is env-driven — see ENTITY_CONFIG below. To spin
up a second regional entity clone docker-compose with a different set of env
vars; no code changes needed.

Multi-user auth, admin panel, contacts CRM.

Email: handled entirely by Infomaniak kSuite (MX + kMail). This app sends no
mail of its own; contact-form leads are stored in data/contacts.json.
"""
import io
import csv
import base64
import asyncio
import os
import json
import hmac
import hashlib
import secrets
import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from markupsafe import Markup, escape

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

GA4_ID         = os.environ.get("GA4_ID", "")

# Lead-notification mail, via Infomaniak (the entity's own mail provider). This
# is the ONLY mail the site sends, and only ever to NOTIFY_EMAIL — never to an
# address supplied by a visitor, so the form cannot be used to mail strangers.
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.infomaniak.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

# ── Digital-twin viewer ──────────────────────────────────────────────────────
# Read-only window onto the persistent agent (the "Tuscany digital twin"). The
# agent runs on a separate GPU host; this site never holds its state.
#
# ORGANISM_URL points at the twin's *viewer* service, not at the agent. The
# viewer exposes only read routes, so the port reachable from here has no write
# surface at all — the agent itself stays bound to loopback on its own host and
# is never exposed. The allowlist below is therefore the second of two
# independent checks, not the only one.
ORGANISM_URL   = os.environ.get("ORGANISM_URL", "")
ORGANISM_TOKEN = os.environ.get("ORGANISM_TOKEN", "")

# Only these paths may be fetched, and only with GET. The agent also exposes
# /chat, /consolidate, /genome/*, /actions/* and /research/battery/administer,
# every one of which WRITES to the biography. A viewer that could reach them
# would turn a reader into a participant and contaminate the experiment, so the
# allowlist is positive: unlisted paths are refused rather than filtered.
ORGANISM_READ_PATHS = {
    "/health", "/genesis", "/constitution", "/memory", "/autobiography",
    "/research/state", "/research/ledger", "/research/battery",
    "/sensory/recent", "/audit", "/versions",
    # The epistemic layer, read paths only. Every /epistemic/* WRITE path -- audit, resolve,
    # revise -- is deliberately absent.
    #
    # `/time-zero` is NOT listed even though it has a GET, for the same reason
    # /research/external is not: a GET and a POST share that URL, and the POST declares Time
    # Zero irreversibly. Time Zero status is read from /health instead.
    "/epistemic/propositions",
}
# Deliberately NOT listed: /research/external. It is the only agent path where a
# GET and a POST share the same URL, so allowlisting it would leave the "no
# writes" guarantee resting solely on this proxy always choosing GET. Excluding
# it keeps the guarantee structural instead of behavioural.
# Read paths with a trailing id segment, e.g. /autobiography/1 or
# /research/battery/1/scores. Matched against a regex rather than a bare prefix:
# "/research/battery/1/score" (singular) is a POST that runs the judge, and it
# sits under the same prefix as the GET "/scores". Requiring the full shape to
# match means that path is unreachable even if the method check were bypassed.
ORGANISM_READ_PATTERNS = (
    re.compile(r"^/autobiography/\d+$"),
    re.compile(r"^/epistemic/propositions/\d+$"),
    re.compile(r"^/research/battery/\d+$"),
    re.compile(r"^/research/battery/\d+/scores$"),
    re.compile(r"^/versions/\d+$"),
)


def _organism_path_allowed(path: str) -> bool:
    if path in ORGANISM_READ_PATHS:
        return True
    return any(p.match(path) for p in ORGANISM_READ_PATTERNS)

# ── Regional entity config (parameterised for multi-region reuse) ───────────
# Every string that identifies the entity, its domain, its address, or its
# legal-register data comes from env vars. To spin up a second regional
# entity (e.g. Nova Terrra Africa) clone docker-compose with a different set
# of these values — no code change needed.
DOMAIN               = os.environ.get("DOMAIN",               "nova-terrra.eu")
BRAND_NAME           = os.environ.get("BRAND_NAME",           "Nova Terrra Europa")
LEGAL_ENTITY_NAME    = os.environ.get("LEGAL_ENTITY_NAME",    "Nova Terrra UG (haftungsbeschränkt)")
LEGAL_ENTITY_HRB     = os.environ.get("LEGAL_ENTITY_HRB",     "HRB 200210")
LEGAL_ENTITY_COURT   = os.environ.get("LEGAL_ENTITY_COURT",   "Amtsgericht Hamburg")
LEGAL_ENTITY_DATE    = os.environ.get("LEGAL_ENTITY_DATE",    "09.07.2026")
LEGAL_ENTITY_VAT_ID  = os.environ.get("LEGAL_ENTITY_VAT_ID",  "in Beantragung")
LEGAL_STREET         = os.environ.get("LEGAL_STREET",         "Moorfuhrtweg 4")
LEGAL_POSTCODE       = os.environ.get("LEGAL_POSTCODE",       "22301")
LEGAL_CITY           = os.environ.get("LEGAL_CITY",           "Hamburg")
LEGAL_COUNTRY_DE     = os.environ.get("LEGAL_COUNTRY_DE",     "Deutschland")
LEGAL_COUNTRY_EN     = os.environ.get("LEGAL_COUNTRY_EN",     "Germany")
LEGAL_COUNTRY_IT     = os.environ.get("LEGAL_COUNTRY_IT",     "Germania")

FROM_EMAIL     = os.environ.get("FROM_EMAIL",     f"yuval@{DOMAIN}")
FROM_NAME      = os.environ.get("FROM_NAME",      "Yuval Lavi")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL",   f"yuval@{DOMAIN}")
CONTACT_EMAIL  = os.environ.get("CONTACT_EMAIL",  f"info@{DOMAIN}")

ENTITY_CONFIG = {
    "domain":         DOMAIN,
    "brand":          BRAND_NAME,
    "legal_name":     LEGAL_ENTITY_NAME,
    "hrb":            LEGAL_ENTITY_HRB,
    "court":          LEGAL_ENTITY_COURT,
    "reg_date":       LEGAL_ENTITY_DATE,
    "vat_id":         LEGAL_ENTITY_VAT_ID,
    "street":         LEGAL_STREET,
    "postcode":       LEGAL_POSTCODE,
    "city":           LEGAL_CITY,
    "country_de":     LEGAL_COUNTRY_DE,
    "country_en":     LEGAL_COUNTRY_EN,
    "country_it":     LEGAL_COUNTRY_IT,
    "contact_email":  CONTACT_EMAIL,
    "from_email":     FROM_EMAIL,
    "from_name":      FROM_NAME,
}

# Render legal pages against this entity config at startup — cheap enough
# to do once at import, and it means we fail-fast if a required key is
# missing rather than 500-ing on the first /impressum request.
LEGAL_PAGES = legal_content.build_pages(ENTITY_CONFIG)


_file_lock = threading.Lock()


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TERRRA Europa")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR)), name="static")


def render(request: Request, name: str, ctx: dict | None = None):
    """Render a template with i18n + entity context auto-injected.

    Adds: t (translator), lang, lang_labels, supported_langs, ga4_id, sun_cfg,
    entity (the ENTITY_CONFIG dict), plus flat convenience aliases (brand,
    domain, contact_email, legal_name).
    """
    lang = i18n.resolve_lang(request)
    base = {
        "t": i18n.translator(lang),
        "lang": lang,
        "lang_labels": i18n.LANG_LABELS,
        "supported_langs": i18n.SUPPORTED,
        "ga4_id": GA4_ID,
        "sun_cfg": _load_sun_config(),
        # Entity config — see ENTITY_CONFIG above. Templates should prefer
        # {{ brand }} / {{ domain }} / {{ contact_email }} over hard-coded strings.
        "entity":        ENTITY_CONFIG,
        "brand":         BRAND_NAME,
        "domain":        DOMAIN,
        "legal_name":    LEGAL_ENTITY_NAME,
        "contact_email": CONTACT_EMAIL,
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
    "cycle_seconds":     300,   # full day-night cycle code default (saved override may differ)
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
    "cycle_seconds":     (60, 480),
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
    content = LEGAL_PAGES[page][active]
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
        "entity":        ENTITY_CONFIG,
        "brand":         BRAND_NAME,
        "domain":        DOMAIN,
        "legal_name":    LEGAL_ENTITY_NAME,
        "contact_email": CONTACT_EMAIL,
    })

@app.get("/robots.txt")
async def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /admin/\n"
        "Disallow: /contacts\n"
        "Disallow: /change-password\n"
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

ROLE_LABELS = {"landowner": "Landowner / land manager",
               "institutional_partner": "Institutional partner",
               "carbon_buyer": "Institutional partner",
               "project_developer": "Project developer",
               "other": "Other"}


def _notify_lead(email: str, role: str, ts: str) -> None:
    """Mail NOTIFY_EMAIL about a new lead. Blocking, so callers run it in a thread.

    Reply-To carries the visitor's address so replying goes straight to them —
    the recipient is always NOTIFY_EMAIL, never visitor-supplied input.
    """
    if not (SMTP_USER and SMTP_PASS):
        return
    import smtplib, ssl
    from email.message import EmailMessage

    label = ROLE_LABELS.get(role, role)
    msg = EmailMessage()
    msg["Subject"]  = f"{BRAND_NAME} — new contact: {label}"
    msg["From"]     = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]       = NOTIFY_EMAIL
    msg["Reply-To"] = email
    msg.set_content(f"New contact signup on {DOMAIN}\n\n"
                    f"Email: {email}\n"
                    f"Role:  {label}\n"
                    f"Time:  {ts}\n\n"
                    f"Reply to this message to answer them directly.\n")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        # The lead is already persisted; a mail failure must not surface to the
        # visitor or lose the record.
        print(f"[lead-notify] failed for {email}: {e}", flush=True)


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
    ts = datetime.now(timezone.utc).isoformat()
    _log_contact(email, role, visitor_id, consent_ts=ts)
    # Persisted first, notified second, and off the request path: SMTP latency
    # must not slow the form or let a mail error fail a captured lead.
    asyncio.create_task(asyncio.to_thread(_notify_lead, email, role, ts))
    resp = JSONResponse({"ok": True})
    if not request.cookies.get(VISITOR_COOKIE):
        resp.set_cookie(VISITOR_COOKIE, visitor_id, max_age=VISITOR_TTL, samesite="lax")
    return resp

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    s = _get_session(request)
    if s and not s.get("temp_auth"):
        return RedirectResponse("/contacts", status_code=302)
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
    resp = RedirectResponse("/contacts", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                    httponly=True, samesite="lax")
    return resp

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

    return RedirectResponse("/contacts", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token and token in _sessions:
        del _sessions[token]
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# ── Protected routes ──────────────────────────────────────────────────────────
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
    fname = f"{DOMAIN}-contacts-{datetime.now().strftime('%Y%m%d')}.csv"
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

    # The temp password is shown to the admin to pass on out-of-band; the site
    # no longer sends mail of its own.
    return RedirectResponse(f"/admin?created=1&temp_pw={temp_pw}", status_code=303)

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
    if not user:
        return RedirectResponse("/admin?error=nouser", status_code=303)

    temp_pw = _gen_temp_password()
    ph, salt = _hash_password(temp_pw)
    users = _load_users()
    for u in users:
        if u["username"] == username:
            u["temp_password_hash"] = ph
            u["temp_password_salt"] = salt
    _save_users(users)

    return RedirectResponse(f"/admin?reset=1&temp_pw={temp_pw}", status_code=303)

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


# ── Digital-twin viewer (read-only) ──────────────────────────────────────────
async def _organism_get(path: str) -> tuple[int, dict | list | None, str]:
    """Fetch one read-only path from the agent. Returns (status, data, error).

    The agent's token stays server-side: the browser never receives it, so a
    reader cannot replay it against a write endpoint.
    """
    if not ORGANISM_URL or not ORGANISM_TOKEN:
        return 503, None, "viewer is not configured (ORGANISM_URL / ORGANISM_TOKEN unset)"
    if not _organism_path_allowed(path):
        return 403, None, f"path not on the read-only allowlist: {path}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # /api/ is the viewer's read-only passthrough; it re-checks the
            # allowlist on its side before touching the agent.
            r = await client.get(
                f"{ORGANISM_URL.rstrip('/')}/api{path}",
                headers={"X-Agent-Token": ORGANISM_TOKEN,
                         "X-Operator-User": "viewer"},
            )
    except httpx.RequestError as exc:
        # 0 = no HTTP response at all: Nova could not reach the VIEWER (ORGANISM_URL). This is a
        # DIFFERENT condition from the viewer being up and relaying a 502 because the agent behind
        # it is down, and the page must tell them apart -- so it is a sentinel, not 502.
        return 0, None, f"viewer unreachable: {type(exc).__name__}"
    if r.status_code != 200:
        return r.status_code, None, f"agent returned {r.status_code}"
    try:
        return 200, r.json(), ""
    except ValueError:
        return 502, None, "agent returned a non-JSON body"


# ── UNDERSTANDING ─────────────────────────────────────────────────────────────
# The organism's definition, rendered by SECTION rather than as one wall of text. The document is
# already organised in Fibonacci counts -- Goal (1), Epistemic Test (1), Objectives (2), Concepts
# (3), Architecture (5), Principles (8) -- and that structure is the argument, not decoration:
# each layer is what the previous one makes possible. So the page follows it.
#
# A COPY, not the source. This site runs on a different host from the organism's repository, so
# the definition ships with the site and `scripts/sync-understanding.sh` reports drift. A stale
# copy of the document that defines what the organism IS would be worse than showing nothing,
# which is why the drift check is a script rather than a comment.
#
# Read from disk, never proxied from the agent: this is what the humans defined, and a document
# the organism could serve is a document it could be suspected of shaping.
UNDERSTANDING_PATH = Path(os.environ.get(
    "UNDERSTANDING_PATH", str(Path(__file__).parent / "public" / "docs" / "UNDERSTANDING.md")))

# Short glosses for the section headings, so a reader arriving cold knows why each part exists.
# Keyed on the heading text as written in the document.
_SECTION_NOTES = {
    "The question, and its hierarchy": "What is being asked, and what was asked before. "
                                       "Revised 2026-08-23.",
    "Goal": "What is being attempted, in one statement.",
    "Epistemic Test": "How the question would be answered — and why there is no single test.",
    "Objectives": "The two things that must both be true for the project to mean anything.",
    "Concepts": "The distinctions the whole design rests on. Get these wrong and the rest is noise.",
    "Architecture": "How the concepts become mechanisms.",
    "Constitutional Principles": "Constraints the organism cannot edit, because a self that can "
                                 "rewrite its own limits has none.",
    "Current State": "Where the implementation actually is. This part expires.",
    "Development Plan": "What is built, planned, and merely named.",
    "Developmental Loop": "How a day in its life is meant to go.",
    "Research Position": "What may and may not be claimed from any of this.",
    "Working Method": "Who checks whom.",
    "Genesis Principle": "Why it begins with a record it did not write.",
}


def _inline(text: str) -> Markup:
    """Render `**bold**` and `` `code` `` as markup, and nothing else.

    ESCAPES FIRST, then applies the two patterns. Order matters: the document is a file on disk
    that a person edits, and interpolating it into a page unescaped would make any stray angle
    bracket -- or anything worse -- into live HTML on a page served behind a login. Escaping
    after substitution would defeat the substitution; escaping before means only these two
    deliberate patterns survive.
    """
    safe = escape(text)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(safe))
    safe = re.sub(r"`(.+?)`", r"<code>\1</code>", safe)
    return Markup(safe)


def _parse_understanding(text: str) -> list[dict[str, object]]:
    """Split the document into top-level sections, each with its own subsections.

    Deliberately a small parser rather than a Markdown library: the only structure this page
    needs is `# heading`, `## subsection`, `### term` and paragraphs, and a dependency that
    renders arbitrary HTML into a page served behind a login is a larger surface than the job
    justifies.

    Blank-line-delimited BLOCKS, not lines. The document is hard-wrapped at ~90 columns, so
    treating each line as a paragraph turned every sentence into four stacked fragments and made
    a 23,000-pixel page out of a readable document.
    """
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    sub: dict[str, object] | None = None
    pending: list[str] = []
    pending_kind = "text"

    def flush() -> None:
        """Emit the accumulated lines as one paragraph."""
        nonlocal pending, pending_kind
        if not pending or current is None:
            pending = []
            pending_kind = "text"
            return
        target = sub["body"] if sub is not None else current["intro"]  # type: ignore[index]
        if pending_kind == "bullet":
            items = [x.rstrip(".").rstrip(";") for x in pending if x.strip()]
            if items:
                target.append({"kind": "list", "items": [_inline(x) for x in items]})
        else:
            joined = " ".join(pending).strip()
            if joined:
                target.append({"kind": pending_kind, "text": _inline(joined)})
        pending = []
        pending_kind = "text"

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("# "):
            flush()
            title = stripped[2:].strip()
            # Titles carry their Fibonacci count as "Concepts (3)". Keep the number: it is part
            # of the design's claim about itself, not formatting.
            name, _, count = title.partition(" (")
            current = {"title": title, "name": name.strip(),
                       "count": count.rstrip(")").strip(),
                       "note": _SECTION_NOTES.get(name.strip(), ""),
                       "intro": [], "subsections": []}
            sections.append(current)
            sub = None
        elif stripped.startswith("## ") and current is not None:
            flush()
            sub = {"title": stripped[3:].strip(), "body": []}
            current["subsections"].append(sub)          # type: ignore[union-attr]
        elif stripped.startswith("### ") and current is not None:
            flush()
            # A defined term. Promoted to its own subsection when it appears outside one, so a
            # glossary entry is never orphaned above the section it belongs to.
            if sub is None:
                sub = {"title": stripped[4:].strip(), "body": []}
                current["subsections"].append(sub)      # type: ignore[union-attr]
            else:
                sub["body"].append({"kind": "term", "text": _inline(stripped[4:].strip())})
        elif not stripped or stripped.startswith(("---", "|")):
            # Blank line, rule or table row ends the current paragraph. Tables are skipped
            # rather than mangled: this page is for reading the argument, and STATUS.md is where
            # the tabular detail lives.
            flush()
        elif current is not None:
            if stripped.startswith("- "):
                # A real markdown bullet. Without this the run of them was joined into one
                # paragraph and rendered as a single run-on line of semicolons.
                if pending_kind != "bullet":
                    flush()
                    pending_kind = "bullet"
                pending.append(stripped[2:].strip())
            elif stripped.startswith(">"):
                body = stripped.lstrip(">").strip()
                if not body:                    # a bare '>' is quote spacing, not content
                    flush()
                    continue
                if pending_kind != "quote":
                    flush()
                    pending_kind = "quote"
                pending.append(body)
            else:
                if pending_kind != "text":
                    flush()
                pending.append(stripped)
    flush()

    # Runs of blank-line-separated fragments each ending in ';' are a LIST written as prose --
    # "memory and autobiographical continuity;", "belief formation and revision;" and so on.
    # Rendered as paragraphs they read as a stutter, so consecutive ones are merged into a single
    # list block. Detected by the trailing semicolon rather than by indentation, because that is
    # how the document actually marks them.
    def _fold_lists(blocks: list[dict[str, str]]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        run: list[str] = []
        for b in blocks:
            # A block is either a paragraph-ish one carrying `text` or a list carrying `items`.
            # Reading `text` unconditionally raised KeyError once real dash-bullets started
            # producing list blocks here.
            text = b.get("text", "")
            if b["kind"] == "text" and str(text).endswith((";", ";.")):
                run.append(Markup(str(text).rstrip(".").rstrip(";")))
                continue
            if run:
                out.append({"kind": "list", "items": run})
                run = []
            out.append(b)
        if run:
            out.append({"kind": "list", "items": run})
        return out

    for section in sections:
        section["intro"] = _fold_lists(section["intro"])        # type: ignore[arg-type]
        for subsection in section["subsections"]:               # type: ignore[union-attr]
            subsection["body"] = _fold_lists(subsection["body"])

    # The document's own title block repeats the epigraph shown at the top of the page, so it
    # would appear twice. Drop it rather than render it twice.
    return [s for s in sections if not s["name"].startswith("Fibonacci Organism")]


@app.get("/organism/understanding", response_class=HTMLResponse)
async def organism_understanding(request: Request):
    """The organism's definition. Static: it does not call the agent at all."""
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    try:
        text = UNDERSTANDING_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return templates.TemplateResponse(request, "organism_understanding.html", {
            "display_name": s["display_name"],
            "sections": [],
            "error": f"the definition is not available on this host ({type(exc).__name__})",
        })
    return templates.TemplateResponse(request, "organism_understanding.html", {
        "display_name": s["display_name"],
        "sections": _parse_understanding(text),
        "error": "",
    })


@app.get("/organism", response_class=HTMLResponse)
async def organism_page(request: Request):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    if s.get("temp_auth"):
        return RedirectResponse("/change-password", status_code=302)

    h_status, health, health_err = await _organism_get("/health")
    _, state, state_err = await _organism_get("/research/state")
    _, chapters, chapters_err = await _organism_get("/autobiography")
    _, ledger, ledger_err = await _organism_get("/research/ledger")
    _, memory, memory_err = await _organism_get("/memory")

    entries = (ledger or {}).get("entries", [])

    # Availability is derived ONLY from the /health probe, and lifecycle state is NEVER inferred
    # from a missing /health (operator directive 2026-09-06). Four cases the page tells apart:
    #   online             -- /health answered 200 with a body; time_zero_declared is trusted
    #   viewer_unreachable -- Nova could not reach the viewer at all (sentinel status 0)
    #   auth_or_config     -- the viewer answered 401/403/503 (token, allowlist, unset config)
    #   agent_unavailable  -- the viewer is up but relayed an upstream error (e.g. 502: agent down)
    health_available = h_status == 200 and health is not None
    if health_available:
        availability = "online"
    elif h_status == 0:
        availability = "viewer_unreachable"
    elif h_status in (401, 403, 503):
        availability = "auth_or_config"
    else:
        availability = "agent_unavailable"
    # Prenatal wording is shown ONLY when /health explicitly reports time_zero_declared is False --
    # never because /health was absent. `is False` is deliberate: a missing key (None) is "not
    # known from here", not "prenatal".
    tz = (health or {}).get("time_zero_declared") if health_available else None
    show_prenatal = health_available and tz is False

    return templates.TemplateResponse(request, "organism.html", {
        "display_name":      s["display_name"],
        "availability":      availability,
        "health_available":  health_available,
        # Raw upstream diagnostic, for small technical text only. Never used to infer state.
        "diagnostic":        health_err or state_err or chapters_err or ledger_err or memory_err,
        "show_prenatal":     show_prenatal,
        "positions":         (memory or {}).get("epistemic_positions", []),
        "time_zero_declared": tz,
        "health":            health,
        "snapshot":          (state or {}).get("snapshot", {}),
        "battery_hash":      (state or {}).get("battery_hash", ""),
        "runs":              (state or {}).get("battery_runs", []),
        "entries":           entries[:40],
        "ledger_count":      (ledger or {}).get("count", 0),
        "chapters":          (chapters or {}).get("chapters", []),
        "genesis_integrity": (chapters or {}).get("genesis_integrity"),
    })


@app.get("/organism/chapter/{chapter_no}", response_class=HTMLResponse)
async def organism_chapter(request: Request, chapter_no: int):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    # The agent returns the chapter object itself, not a wrapper.
    status, data, err = await _organism_get(f"/autobiography/{chapter_no}")
    return templates.TemplateResponse(request, "organism_chapter.html", {
        "display_name": s["display_name"],
        "chapter_no":   chapter_no,
        "chapter":      data if isinstance(data, dict) else None,
        "error":        err,
    }, status_code=200 if status == 200 else 404)


@app.get("/organism/genesis", response_class=HTMLResponse)
async def organism_genesis(request: Request):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    _, data, err = await _organism_get("/genesis")
    return templates.TemplateResponse(request, "organism_genesis.html", {
        "display_name": s["display_name"],
        "genesis":      data,
        "error":        err,
    })


# ── Talking to the organism ───────────────────────────────────────────────────
#
# The ONE write path on this site, and the same discipline as the viewer's: a hardcoded target, no
# caller-supplied URL component, and the read allowlist above left untouched. A conversation is a
# write -- it appends an episode and may assert propositions the organism will then hold -- so this
# cannot go through _organism_get, which exists to guarantee the opposite.
#
# Timeout: local inference measured at 62s on the GPU host. The read helper's 20s would abort a
# normal reply while the organism kept generating and still committed the episode, showing an error
# for a turn that really happened.
ORGANISM_CHAT_TIMEOUT = float(os.environ.get("ORGANISM_CHAT_TIMEOUT", "300"))


@app.get("/organism/talk", response_class=HTMLResponse)
async def organism_talk(request: Request):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    status, health, err = await _organism_get("/health")
    return templates.TemplateResponse("talk.html", {
        "request": request,
        "viewer": s["username"],
        "health": health or {},
        "time_zero_declared": bool((health or {}).get("time_zero_declared")),
        "error": err,
    })


# ── chat attachments ──────────────────────────────────────────────────────────────────────
# Attachments are handled ENTIRELY here, at the website layer: each file's text is extracted and
# folded into the message the organism already receives, exactly as if the operator had pasted it.
# The agent's /chat contract is unchanged, so the organism's code -- and therefore its dna_id --
# is untouched. Text files are read as text; PDFs are extracted with pypdf; images are declined
# (the local model reads text only). Sizes are capped so a large upload cannot blow the context
# window or the episode record. Nothing here fetches or executes anything.
ATTACH_MAX_FILES = 12
ATTACH_MAX_CHARS = 60_000        # per attachment, after extraction
ATTACH_TOTAL_CHARS = 200_000     # across all attachments in one turn
ATTACH_MAX_BYTES = 4_000_000     # raw decoded bytes per file


def _extract_attachment_text(name: str, mime: str, b64: str) -> tuple[str, str]:
    """Return (text_to_fold_in, user_facing_note). Text-only; never executes or fetches anything."""
    try:
        data = base64.b64decode(b64 or "", validate=False)
    except Exception:
        return "", f"could not read {name!r}"
    if len(data) > ATTACH_MAX_BYTES:
        return "", f"{name!r} is larger than 4 MB and was skipped"
    low = name.lower()
    if mime == "application/pdf" or low.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
        except Exception as exc:                                             # noqa: BLE001
            return "", f"could not read PDF {name!r} ({type(exc).__name__})"
        if not text:
            return "", f"PDF {name!r} has no extractable text (a scanned image?) and was skipped"
        return text[:ATTACH_MAX_CHARS], ""
    if mime.startswith("image/"):
        return "", f"image {name!r} skipped — the organism reads text only"
    return data.decode("utf-8", errors="replace")[:ATTACH_MAX_CHARS], ""


def _fold_attachments(message: str, attachments: object) -> "tuple[str, list[str]]":
    """Combine the typed message with extracted attachment text, and collect user-facing notes."""
    blocks: "list[str]" = []
    notes: "list[str]" = []
    budget = ATTACH_TOTAL_CHARS
    if isinstance(attachments, list):
        for att in attachments[:ATTACH_MAX_FILES]:
            if not isinstance(att, dict):
                continue
            name = str(att.get("name", "attachment"))[:200]
            mime = str(att.get("mime", ""))[:100]
            text, note = _extract_attachment_text(name, mime, att.get("b64", ""))
            if note:
                notes.append(note)
            if text:
                text = text[:budget]
                budget -= len(text)
                blocks.append(f"\n\n----- attached file: {name} ({mime or 'text'}) -----\n"
                              f"{text}\n----- end of {name} -----")
                if budget <= 0:
                    notes.append("attachment content was truncated to fit the context window")
                    break
    return message + "".join(blocks), notes


@app.post("/organism/talk")
async def organism_talk_send(request: Request):
    s = _get_session(request)
    if not s:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if not ORGANISM_URL or not ORGANISM_TOKEN:
        return JSONResponse({"error": "viewer is not configured"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    message = str(body.get("message", "")).strip()
    # Attachments are folded into the message text here; the agent still receives one {message}.
    combined, attachment_notes = _fold_attachments(message, body.get("attachments"))
    if not combined.strip():
        return JSONResponse({"error": "message is empty"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=ORGANISM_CHAT_TIMEOUT) as client:
            r = await client.post(
                f"{ORGANISM_URL.rstrip('/')}/talk",       # the viewer's own write route, hardcoded
                json={"message": combined},
                headers={"X-Agent-Token": ORGANISM_TOKEN,
                         # Attribute the turn to the logged-in operator, not to "viewer": the
                         # episode records who spoke to it, and that should be a real person.
                         "X-Operator-User": s["username"]},
            )
    except httpx.TimeoutException:
        return JSONResponse({"error": f"no reply within {ORGANISM_CHAT_TIMEOUT:.0f}s. The organism "
                                      "may still be generating, and if it finishes the episode "
                                      "WILL be recorded. Reload before resending, or you may "
                                      "record two turns"}, status_code=504)
    except httpx.RequestError as exc:
        return JSONResponse({"error": f"agent unreachable: {type(exc).__name__}"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": f"agent returned {r.status_code}"}, status_code=r.status_code)
    try:
        payload = r.json()
    except ValueError:
        return JSONResponse({"error": "agent returned a non-JSON body"}, status_code=502)
    if attachment_notes:
        payload["attachment_notes"] = attachment_notes
    return JSONResponse(payload)


@app.get("/organism/api/{path:path}")
async def organism_api(request: Request, path: str):
    """Pass-through for the raw JSON, same allowlist. Handy for inspection."""
    s = _get_session(request)
    if not s:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    status, data, err = await _organism_get("/" + path)
    if err:
        return JSONResponse({"error": err}, status_code=status)
    return JSONResponse(data)


# ── Team routes (session-protected, any logged-in user) ──────────────────────

@app.get("/team/pmo", response_class=HTMLResponse)
async def team_pmo(request: Request):
    s = _get_session(request)
    if not s:
        return RedirectResponse("/login", status_code=302)
    pmo_file = DATA_DIR / "pmo.html"
    if not pmo_file.exists():
        return HTMLResponse("<h1>PMO dashboard not generated yet</h1><p>Run /pmo-refresh to generate.</p>", status_code=404)
    return HTMLResponse(pmo_file.read_text())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8103"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

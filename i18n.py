"""
Tiny i18n layer for terrra-europa.com.

- Source of truth: locales/en.json (canonical English).
- Translations: locales/de.json, locales/it.json — same key shape, values translated.
  Missing keys fall back to English so the site never shows a key id to users.

Workflow for a professional translator:
  1. Send them locales/en.json (and optionally the existing de.json / it.json as a starting point).
  2. They return a file with the same keys, values translated.
  3. Drop the file in locales/ — done.

Use `python tools/i18n_check.py` (or just diff the keys) to verify coverage.
"""
import json
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED = ["en", "de", "it"]
DEFAULT = "en"

# Cookie used by the picker to remember the user's choice.
LANG_COOKIE = "eu_lang"
LANG_COOKIE_TTL = 60 * 60 * 24 * 365  # 1 year

# Display labels for the picker — flag + short label.
LANG_LABELS = {
    "en": {"flag": "🇬🇧", "label": "EN", "name": "English"},
    "de": {"flag": "🇩🇪", "label": "DE", "name": "Deutsch"},
    "it": {"flag": "🇮🇹", "label": "IT", "name": "Italiano"},
}

_cache: dict[str, dict] = {}


def _load(lang: str) -> dict:
    if lang in _cache:
        return _cache[lang]
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        _cache[lang] = {}
        return {}
    try:
        _cache[lang] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _cache[lang] = {}
    return _cache[lang]


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten nested dict so `t('hero.title')` works on nested JSON."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def translator(lang: str):
    """Return a `t(key, **vars)` function bound to a language."""
    if lang not in SUPPORTED:
        lang = DEFAULT
    primary = _flatten(_load(lang))
    fallback = _flatten(_load(DEFAULT)) if lang != DEFAULT else {}

    def t(key: str, **vars) -> str:
        s = primary.get(key) or fallback.get(key) or key
        if vars:
            try:
                s = s.format(**vars)
            except (KeyError, IndexError):
                pass
        return s
    return t


def parse_accept_language(header: str) -> str:
    """Pick the best supported lang from an Accept-Language header."""
    if not header:
        return DEFAULT
    for entry in header.split(","):
        code = entry.split(";")[0].strip().lower()[:2]
        if code in SUPPORTED:
            return code
    return DEFAULT


def resolve_lang(request) -> str:
    """Cookie wins; otherwise sniff Accept-Language; default = en."""
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in SUPPORTED:
        return cookie
    return parse_accept_language(request.headers.get("accept-language", ""))


def reload_cache():
    """Drop in-memory cache — call after editing JSON in dev."""
    _cache.clear()

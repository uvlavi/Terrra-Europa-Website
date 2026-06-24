# Translations

The public landing page at `/` is available in **English (en)**, **German (de)**, and **Italian (it)**.
Team-area pages (login, admin, compose, etc.) remain in English by design.

## How it works

- `en.json` is the **canonical source**. All keys live here first.
- `de.json` and `it.json` mirror the same key shape with translated values.
- Missing keys fall back to English silently — the site never shows a key id to users.
- Language is chosen by:
  1. `eu_lang` cookie (set when the visitor clicks a flag)
  2. otherwise `Accept-Language` header (browser's preferred language)
  3. otherwise English

The flag picker on `/` writes the cookie via `GET /lang/{en|de|it}?next=/`.

## Sending files to a professional translator

1. Send them `en.json` (and optionally the existing draft of `de.json` or `it.json` as a starting point).
2. Ask them to return a file with **exactly the same keys**, values translated.
   - HTML markup inside values like `<strong>...</strong>` should be preserved.
   - Placeholders like `{display_name}` and `{domain}` must be kept verbatim.
3. Drop the returned file into this folder. No other code changes needed.

## Reviewing draft translations

The current `de.json` and `it.json` are **AI-generated drafts** — their `_meta.review_status`
field is set to `DRAFT`. After a native speaker reviews and edits, change that field to
`REVIEWED` and note the reviewer's name + date.

## Checking key parity

Quick coverage check from the project root:

```bash
python3 -c "
import json
def keys(d, p=''):
    out = set()
    for k, v in d.items():
        if k == '_meta': continue
        kk = f'{p}.{k}' if p else k
        if isinstance(v, dict): out |= keys(v, kk)
        else: out.add(kk)
    return out
en = keys(json.load(open('locales/en.json')))
for code in ['de','it']:
    other = keys(json.load(open(f'locales/{code}.json')))
    missing = en - other
    extra = other - en
    print(f'[{code}] missing={len(missing)} extra={len(extra)}')
    for k in sorted(missing): print('  -', k)
    for k in sorted(extra):   print('  +', k)
"
```

Empty output (or "missing=0 extra=0") means the translation is complete.

## Dev workflow

After editing a JSON file, restart the app (Docker rebuild on the server, or just
restart the local dev process) — `i18n.py` caches catalogs in memory.

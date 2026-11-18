# TERRRA Europa

Public landing page + private team site for **TERRRA Europa UG** (Germany).

- **Live**: https://terrra-europa.com
- **Stack**: Python / FastAPI + Jinja2, Caddy reverse proxy, Docker Compose
- **Hosted on**: AWS EC2 t3.micro (eu-central-1), DNS at GoDaddy
- **Email**: ImprovMX (incoming forwarding) + Resend (outbound, used by Gmail Send-As and `/compose`)

## Pages

| URL | What | Auth |
|---|---|---|
| `/` | Public landing — stealth, hero slideshow, contact form | public |
| `/help` | How-to guides (Gmail Send-As setup, DNS reference) | public |
| `/login` `/forgot` `/change-password` | Auth flows | public |
| `/compose` | Send email as `@terrra-europa.com` | session |
| `/contacts` | Contact CRM (table + CSV export) | session |
| `/admin` | User management — single radio + action toolbar | admin |

## Email architecture

```
Incoming    *@terrra-europa.com → ImprovMX → personal Gmail
Outgoing    Gmail "Send mail as" or /compose → Resend SMTP → recipient
```

ImprovMX aliases:
- Personal: each `username@` → that user's Gmail
- Organisation (`info`, `sales`, `receipts`, `contact`, catch-all `*`) → `wetlands.israel@gmail.com`

## Deploy from Mac

```bash
rsync -az --exclude-from=.rsync-exclude \
  -e "ssh -i ~/.ssh/nova-europa.pem" \
  /Users/yuvallavi/dev/terrra-europa/website/ \
  ubuntu@63.182.30.213:~/terrra-europa/

ssh -i ~/.ssh/nova-europa.pem ubuntu@63.182.30.213 \
  "cd ~/terrra-europa && docker compose up -d --build"

# To push pmo.json separately (gitignored):
# rsync -az -e "ssh -i ~/.ssh/nova-europa.pem" data/pmo.json ubuntu@63.182.30.213:~/terrra-europa/data/pmo.json
```

**Always use `--no-cache`** — `restart` and `up -d --build` reuse cached layers and templates don't update.
**Always use `--exclude-from=.rsync-exclude`** — server `.env` and `secrets/` must not be overwritten by local versions.

## Local development

```bash
cd /Users/yuvallavi/dev/terrra-europa
docker compose up -d --build
open http://localhost:8103
```

Default access: username `yuval`, password `28061972` (seeded from `ACCESS_KEY` env var on first boot).

## Data

All state in `data/` as JSON files (no DB):
- `users.json` — user accounts with hashed passwords (PBKDF2-SHA256, salted)
- `contacts.json` — public contact form submissions

## Environment

Copy `.env.example` → `.env`. Required:
- `ACCESS_KEY` — admin password seed
- `FROM_EMAIL`, `FROM_NAME`, `NOTIFY_EMAIL`, `ADMIN_EXTERNAL_EMAIL` — identity strings only; the app sends no mail

## Adding a team member

1. `/admin` → Add new user (username = email local part, plus their personal Gmail)
2. They receive a welcome email with login + link to `/help`
3. **Manually** add their alias in [ImprovMX dashboard](https://app.improvmx.com): `username@terrra-europa.com` → their Gmail
4. They follow the `/help` guide to set up Gmail Send-As (one-time, ~3 min)

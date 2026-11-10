# todobot

A Telegram bot that takes in tasks as text, voice or pictures. This is the skeleton
(`agentify/specs/requirements/skeleton.md`): it keeps every message and does nothing else with it yet.

What it does now:

- `/start` gets the greeting (SK-1).
- Any other private-chat message, other commands included, goes into the sender's `inbox` as `text`, `voice`, `photo`
  or `other`, and the bot answers "Сохранил ✅" (SK-2). Any Telegram user may use it; each user's rows are theirs.
- `GET /healthz` answers 200 `{"status": "ok", "version": …}` when the database answers, else 503 (SK-4).
- Every line on stdout is one JSON object with `level`, `event` and `version`. A handler that fails logs
  `event=handler_error`, apologises in the chat, and the server keeps running (SK-5). What users write stays out
  of the logs: database errors carry no parameters, the message's text and caption are cut out of the
  `handler_error` traceback, and an update that does not validate is one `update_invalid` line with field
  locations and error types only.

Updates come in by webhook (`POST /telegram/webhook`, checked against `TELEGRAM_WEBHOOK_SECRET`). Replies are Bot
API `sendMessage` calls. The app never calls `setWebhook` itself.

## Layout

```
src/todobot/
  __main__.py     python -m todobot migrate | serve
  config.py       environment variables, version fallback
  logs.py         JSON lines on stdout
  web.py          aiohttp app: webhook (aiogram) and /healthz
  bot.py          handlers: /start, inbox, errors
  inbox.py        message kind and storing it
  texts.py        what the bot says (exact, from the requirement)
  db.py, schema.py, migrate.py, migrations/    SQLAlchemy (async, asyncpg) and Alembic
tests/unit/       our tests (real Postgres, no database mocks)
tests/acceptance/ the approved acceptance tests: black box, hash-checked, never edited here
```

## Tests

Needs [uv](https://docs.astral.sh/uv/); it installs Python 3.12 and the locked dependencies itself.

```sh
make ci              # what CI runs: ruff check, ruff format --check, mypy src, pytest (unit + acceptance)
make test-unit
make test-acceptance # starts `uv run python -m todobot migrate|serve` against a fake Bot API
make format          # apply ruff's formatting and fixes
```

Tests that need Postgres start an embedded one (`pgserver`). Set `TEST_DATABASE_URL` to a server where tests may
create databases to use that one instead.

## Running locally

```sh
cp .env.example .env   # then fill it in; .env is git-ignored
make dev               # migrate, then serve on 0.0.0.0:$PORT
```

`make migrate` and `make serve` run each step on its own. Any Postgres works; the commands are
`uv run python -m todobot migrate` and `uv run python -m todobot serve`.

Without a public URL, Telegram cannot reach a local server. You can play Telegram's part yourself:

```sh
curl -s localhost:8080/healthz
curl -s localhost:8080/telegram/webhook \
  -H "X-Telegram-Bot-Api-Secret-Token: $TELEGRAM_WEBHOOK_SECRET" -H 'Content-Type: application/json' \
  -d '{"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "Купить молоко",
       "chat": {"id": 1, "type": "private"}, "from": {"id": 1, "is_bot": false, "first_name": "Me"}}}'
```

The reply then goes to the Bot API at `TELEGRAM_API_BASE`.

## Environment

| Variable | What | Default |
|---|---|---|
| `DATABASE_URL` | `postgresql://user:pass@host:port/db`; `?sslmode=require` (Neon) means TLS | required |
| `TELEGRAM_BOT_TOKEN` | the bot's token from BotFather | required for `serve` |
| `TELEGRAM_WEBHOOK_SECRET` | the secret Telegram sends in `X-Telegram-Bot-Api-Secret-Token` (the `secret_token` given to `setWebhook`) | required for `serve` |
| `TELEGRAM_API_BASE` | the Bot API's base URL; calls go to `{base}/bot{token}/{method}` | `https://api.telegram.org` |
| `PORT` | the HTTP port | `8080` |
| `APP_VERSION` | the release's version, in `/healthz` and every log line | `RAILWAY_GIT_COMMIT_SHA`, else `dev` |

## Deploying (Railway)

- `Dockerfile`: Python 3.12 slim with the locked runtime dependencies (uv), running as a non-root user. Its
  command is `python -m todobot serve` on `$PORT`.
- `railway.json`: builds with the Dockerfile, runs `python -m todobot migrate` before each deploy, and checks
  `/healthz`.

The webhook is registered once, by hand, with the same secret:
`https://api.telegram.org/bot<token>/setWebhook?url=https://<host>/telegram/webhook&secret_token=<secret>`.

## Migrations

`python -m todobot migrate` applies them. To write a new one, change `src/todobot/schema.py`, then run
`DATABASE_URL=… uv run alembic revision --autogenerate -m "what changed"` against a migrated database. Review
what it wrote in `src/todobot/migrations/versions/`.

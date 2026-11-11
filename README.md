# todobot

A Telegram bot that takes in tasks as text, voice or pictures. This is the skeleton
(`agentify/specs/requirements/SK.yaml`, the spec in `agentify/specs/`) with one inbox item per message
(`DUP.yaml`), its errors in PostHog (`ERR.yaml`), an allow list of users (`ACC.yaml`) and recorded tasks
(`CAP.yaml`): it keeps every message once, makes a task of a plain text, and lists the tasks on `/tasks`.

What it does now:

- `/start` gets the greeting (SK-1).
- Any other private-chat message but `/tasks`, other commands included, goes into the sender's `inbox` as `text`,
  `voice`, `photo` or `other`, and the bot answers "Сохранил ✅" (SK-2). Each user's rows are theirs.
- A plain text (one that does not start with `/`) also becomes one task in the table `tasks`, with status
  `recorded` («записана»), its text the message's text, whatever it names, tied to its inbox item by `user_id` and
  `message_id` (CAP#1). It is recorded in the transaction that stores the item, so an update Telegram sends again
  makes no second task; the reply stays "Сохранил ✅". Commands, voice notes, photos (captions too) and anything
  else stay inbox items only. Inbox items stored before CAP are not tasks.
- `/tasks` answers with the sender's tasks, oldest first: "Твои задачи:" and a line `<n>. <text> — записана` for
  each, or "Задач пока нет." when there is none (CAP#2). It is neither an inbox item nor a task. A list longer than
  one Telegram message (4096 UTF-16 code units, as Telegram counts) comes as several messages, split between task
  lines, each going on where the last ended; a single task line longer than that gets messages of its own, cut
  after the last space that fits (mid-word only if none does). The command goes into the bot's menu by hand,
  through BotFather.
- With `TELEGRAM_ALLOWED_USER_IDS` set, only the users it names are served (ACC#1). Anyone else's private-chat
  message, `/start` and `/tasks` included, gets "Извини, бот пока закрыт." and nothing else: nothing of it is stored (no `users`
  row, no `inbox` row), and the bot logs `event=access_denied` with the sender's `user_id` (never what they wrote).
  At start `serve` logs `event=access_restricted` with `allowed`, the number of ids. Without the variable (or with
  it empty) any Telegram user may use the bot, as before, and `serve` logs a `WARNING` `event=access_open` (ACC#2).
  A value that is not ids separated by commas is a `config_error`: `serve` does not start.
- One inbox item per message (DUP#1). A message is its chat and its `message_id` (in a private chat the chat id is
  the user's id), not the `update_id`. When Telegram sends the same update again, the message is already stored:
  nothing is stored, nothing is sent, and the bot logs `event=inbox_repeat`. A new message with the same text is a
  new item.
- `GET /healthz` answers 200 `{"status": "ok", "version": …}` when the database answers, else 503 (SK-4).
- Every line on stdout is one JSON object with `level`, `event` and `version`. A handler that fails logs
  `event=handler_error`, apologises in the chat, and the server keeps running (SK-5). What users write stays out
  of the logs: database errors carry no parameters, the message's text and caption are cut out of the
  `handler_error` traceback (with the user's ids: a rejected row's DETAIL quotes them), and an update that does
  not validate (or a body that is not JSON) is one `update_invalid` line with field locations and error types only.
  A `/tasks` list that cannot be sent is a `TasksNotSentError` with the failing message's number and the cause's
  type (and the Bot API's description), never the cause's traceback, which can quote the list.
- With `POSTHOG_PROJECT_TOKEN` set, every `ERROR` or `CRITICAL` line also goes to PostHog's error tracking, and each
  start sends PostHog `server_started` (ERR); see [Errors in PostHog](#errors-in-posthog).

Updates come in by webhook (`POST /telegram/webhook`, checked against `TELEGRAM_WEBHOOK_SECRET`). The webhook
answers only after the update is handled (DUP#2): the message stored and the reply sent, or, when handling fails,
the apology tried. With the right secret the answer is always 200 `{}`, also when handling fails. An update the bot
does not finish gets no answer, so Telegram sends it again and DUP#1 keeps it once: on SIGTERM, `serve` stops taking
requests, gives the updates in flight up to 5 s, and cancels the rest unanswered. Replies are Bot API
`sendMessage` calls, never the webhook's response body. The app never calls `setWebhook` itself.

The log events (`event`): `server_started`, `server_stopping`, `server_stopped`; `access_restricted` (with
`allowed`) or `access_open` (`WARNING`) at start; `start` (a /start answered); `inbox_saved` and `inbox_repeat` (with
`kind`, `user_id`, `message_id`); `task_recorded` (a plain text became a task, with `user_id`, `message_id`);
`tasks_listed` (a `/tasks` answered, with `user_id`, `tasks` and `messages`, the counts); `access_denied` (a stranger
refused, with `user_id`, `message_id`);
`handler_error`, `apology_failed`, `refusal_failed`, `update_invalid`, `update_failed` (all `ERROR`);
`healthz_failed` and `posthog_failed` (`WARNING`); `http_request`
(aiohttp's access log); `log` (a library's line); and around them `migrate_started`, `migrate_finished`,
`config_error`, `<command>_failed`, `uncaught_exception`, `unraisable_exception`.

## Layout

```
src/todobot/
  __main__.py     python -m todobot migrate | serve
  config.py       environment variables (the allow list among them), version fallback
  logs.py         JSON lines on stdout
  error_tracking.py  ERROR lines to PostHog as $exception, server_started (only with POSTHOG_PROJECT_TOKEN)
  web.py          aiohttp app: the webhook (answers once the update is handled) and /healthz
  bot.py          aiogram dispatcher and handlers: the allow list, /start, /tasks, inbox, errors
  inbox.py        message kind, and storing it once per message, with its task when it is a plain text
  tasks.py        recording a task, the sender's tasks, and the /tasks list as Telegram messages
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
| `TELEGRAM_ALLOWED_USER_IDS` | the only Telegram user ids the bot serves, separated by commas (spaces allowed), e.g. `5812345678, 402117993`; anyone else is refused (ACC). Anything else is a `config_error` and `serve` does not start. In production: the owner's id only | unset: every user is served, with a `WARNING` `access_open` at start |
| `POSTHOG_PROJECT_TOKEN` | the PostHog project's token (project 344747, organization todobot, EU); without it `serve` sends PostHog nothing | unset |
| `POSTHOG_HOST` | PostHog's ingestion host | `https://eu.i.posthog.com` |

## Errors in PostHog

With `POSTHOG_PROJECT_TOKEN` set, `serve` sends PostHog (the official `posthog` SDK, from a background thread):

- `server_started` once the server listens, with `version` (ERR#2). After a release, it shows the way to PostHog
  works.
- One `$exception` for every log line with level `ERROR` or `CRITICAL`, `config_error` and `serve_failed` included
  (ERR#1). It is made from the log line, not from the live exception: `log_event`, `log_level`, `logger`,
  `error_class`, `traceback` (the line's `exc`) and `version`, with PostHog's `$exception_list` (type, message,
  frames) parsed from that traceback.

Every event has `distinct_id` `todobot` and `$process_person_profile` false; GeoIP is off.

What never goes to PostHog: what users write and their ids. Our own lines cut the message's text, caption and the
user's ids out of a traceback before they log it, and PostHog gets that same traceback. The line's other fields
(`message`, `update_id`, `user_id`, …), `INFO` and `WARNING` lines, and anything users do are not sent. Code
variables are never captured, and the SDK's own exception autocapture is off. A crash hook's traceback
(`uncaught_exception`) has no message to cut anything from: it goes as stdout has it.

When PostHog does not take a batch (after 2 retries; 3 s per request), the bot goes on and writes a `WARNING` line
`posthog_failed` (ERR#3). The SDK's own lines (`logger` `posthog`) never go above `WARNING` and never go back to
PostHog. On SIGTERM, `serve` sends what is queued, waiting at most 3 s; what is left then is dropped, with a
`posthog_failed` line.

## Deploying (Railway)

- `Dockerfile`: Python 3.12 slim with the locked runtime dependencies (uv), running as a non-root user. Its
  command is `python -m todobot serve` on `$PORT`.
- `.railway/railway.ts`: the Railway service's settings. It builds with the Dockerfile, runs
  `python -m todobot migrate` before each deploy, checks `/healthz`, runs in one region (`ams`), and deploys a
  push to `main` that touches the app (watch paths) at once; branch protection lets into `main` only pull
  requests whose required checks passed. Railway does not
  read the file on deploys. The `railway-config` workflow plans a change on its pull request and applies it after
  the merge. Locally, with Node 24: `npm ci --ignore-scripts && railway config plan`. The variables (bot token,
  `DATABASE_URL`, webhook secret, PostHog's project token, the allow list) live only in Railway.

The webhook is registered once, by hand, with the same secret:
`https://api.telegram.org/bot<token>/setWebhook?url=https://<host>/telegram/webhook&secret_token=<secret>`.

## Migrations

`python -m todobot migrate` applies them. To write a new one, change `src/todobot/schema.py`, then run
`DATABASE_URL=… uv run alembic revision --autogenerate -m "what changed"` against a migrated database. Review
what it wrote in `src/todobot/migrations/versions/`.

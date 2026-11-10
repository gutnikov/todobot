# SK: the todobot skeleton

- **Status:** approved by the user on 2026-11-10 (hitl-log: H5, task:skeleton)
- **Route:** product · delivery. The oracle is these criteria; `agentify/specs/` is not set up yet.
- **Decided with the approval:** any Telegram user may use the bot, and each user's data is theirs from the start
  (H1); the bot answers in Russian (H1). The bot texts below are exact.

## Acceptance criteria

### SK-1 /start

Any Telegram user sends `/start` in a private chat. Within 5 seconds the bot sends to that chat exactly:

> Привет! Присылай задачи текстом, голосом или картинкой — я их сохраню.

prod: a log line with `event` = `start` and the release's `version`.

### SK-2 Taking in a message

Any Telegram user sends a private-chat message other than `/start` (other commands included). Its `kind`:

| The message has | `kind` | `text` | `file_id` |
|---|---|---|---|
| `text` | `text` | the text | NULL |
| `voice` | `voice` | the caption or NULL | `voice.file_id` |
| `photo` | `photo` | the caption or NULL | `file_id` of the largest `PhotoSize` (the last in the array) |
| anything else (a sticker, a document…) | `other` | the caption or NULL | NULL |

The bot stores one row in the table `inbox` (`user_id` = `from.id`, `kind`, `text`, `file_id`, `received_at`) and
sends to that chat exactly:

> Сохранил ✅

Each row belongs to the user who sent it: messages of two users are stored under their own `user_id`.
Out of scope: recognition, tasks, the calendar, any LLM.

prod: a log line with `event` = `inbox_saved`, `kind` = the kind, and the release's `version`.

### SK-3 (dropped)

Not needed: any Telegram user may use the bot.

### SK-4 GET /healthz

| The database | Status | Body (JSON) |
|---|---|---|
| answers `SELECT 1` | 200 | `{"status": "ok", "version": "<APP_VERSION>"}` |
| does not answer | 503 | `{"status": "error"}` (more fields allowed) |

The server starts and answers `/healthz` even when the database does not answer.

### SK-5 Logs and errors

- Every line `serve` writes to stdout is one JSON object with at least `level`, `event` and `version`.
- An unhandled exception in an update handler writes a line with `level` = `ERROR` and `event` = `handler_error`,
  and the bot sends to that chat exactly:

  > Что-то пошло не так, попробуй ещё раз.

- The process keeps running: the next update is handled and `/healthz` answers.

### Confirmation in production

After a release: `/healthz` returns the new `version`; lines with `event` = `start` and `event` = `inbox_saved` appear
with that version; no line with `level` = `ERROR` and that version for 10 minutes. Window: at least 10 minutes and at
least one `start` and one `inbox_saved`, cap 60 minutes; below that at the cap, `inconclusive`, and the owner sends
`/start` and one message as a synthetic run.

## Test seams

The contract both the test writer and the worker build to. Tests are black-box: they start the app as a process
and talk to it over HTTP.

- **Python 3.12** (`requires-python = ">=3.12,<3.13"`, `.python-version` = `3.12`), uv. 3.12 because the embedded
  Postgres used in tests (`pgserver`) has no wheels for 3.13.
- **Commands**, run from the repository root:
  - `uv run python -m todobot migrate`: applies the migrations to `DATABASE_URL`, exits 0.
  - `uv run python -m todobot serve`: the HTTP server on `PORT`. It does not migrate and does not call
    `setWebhook`.
- **Environment:**

  | Variable | What | Default |
  |---|---|---|
  | `DATABASE_URL` | `postgresql://user:pass@host:port/db` (the app converts the scheme for its driver) | required |
  | `TELEGRAM_BOT_TOKEN` | the bot's token | required |
  | `TELEGRAM_WEBHOOK_SECRET` | the secret Telegram sends in `X-Telegram-Bot-Api-Secret-Token` | required |
  | `TELEGRAM_API_BASE` | the Bot API's base URL; calls go to `{base}/bot{token}/{method}` | `https://api.telegram.org` |
  | `PORT` | the HTTP port | `8080` |
  | `APP_VERSION` | the release's version, in `/healthz` and every log line | `dev` |

- **Updates in:** `POST /telegram/webhook` with a Telegram `Update` as the JSON body and the header
  `X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>`; the server answers 200. A request without the right
  secret is not handled (the worker's own tests cover it).
- **Replies out:** every reply is an HTTP call to the Bot API's `sendMessage` with `chat_id` and `text` (JSON or
  form-encoded), never the body of the webhook's response. Tests run a fake Bot API that answers every
  `/bot<token>/<method>` with `{"ok": true, "result": …}`: a valid `Message` (`message_id`, `date`,
  `chat: {id, type: "private"}`, `text`) for `sendMessage`, `true` for anything else.
- **Database:** tests read the table `inbox` with the columns `user_id` (bigint), `kind` (text), `text` (text, null),
  `file_id` (text, null), `received_at` (timestamptz). Each test gets a fresh database: `TEST_DATABASE_URL` (a server
  where tests may create databases) when it is set, otherwise an embedded Postgres started by `pgserver`.
- **SK-5's trigger:** `serve` against a fresh database where `migrate` has not run: storing a message fails.
- **Logs:** tests read the stdout of the `serve` process.
- **Tags:** each acceptance test names its requirement, `test_sk1_…`, `test_sk2_…`; they live in `tests/acceptance/`.

Settled after the test writer's questions (2026-11-10), seams only, no criterion changed:

- `serve` listens on `0.0.0.0:PORT`.
- `serve` calls the Bot API only to reply: no `getMe`, no `setWebhook` at startup (the fake answers `true` to every
  method but `sendMessage`).
- Every webhook POST with the right secret gets 200, also when its handler fails.
- `received_at` is the server's time or the message's `date`, with a time zone; tests accept either.
- `APP_VERSION`, when unset, falls back to `RAILWAY_GIT_COMMIT_SHA`, then to `dev`.
- Production's `DATABASE_URL` is Neon's: `postgresql://…/neondb?sslmode=require`; the app connects over TLS with it.

## Approved tests

Written by a test writer with a fresh context from this file; red before the work: `uv run pytest tests/acceptance`
→ `19 failed` (`No module named todobot`), 2026-11-10. The worker does not change them; the gate compares:

```
c41ffbfcdc5cea1ed81077e40d79b3506eefe8a20005e2fed665173c93acfcad  tests/acceptance/conftest.py
49ce070de8c412bd287ae7fa6691d6a4d085654b2b5e8ffb99e5b28926a18b58  tests/acceptance/harness.py
766583c6908d8d066a2ba0ec50a94aefde31b914e953272b20e5e2da958e6137  tests/acceptance/test_sk1_start.py
3058864ab953d89eae8aa5d44b2f4344908c02f6547295e945a22a60bcb4b855  tests/acceptance/test_sk2_inbox.py
800d724933962f6fd0ff7100cb3c482fd2b5d83507622332c14220f7c57f318b  tests/acceptance/test_sk4_healthz.py
28bab435412c5e84dcc3a639fa9b97e9b4105f087baa9333730b6e1579e9db47  tests/acceptance/test_sk5_logs_and_errors.py
```

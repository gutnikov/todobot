# todobot

A Telegram bot that takes in tasks as text, voice or pictures. This is the skeleton
(`agentify/specs/requirements/SK.yaml`, the spec in `agentify/specs/`) with one inbox item per message
(`DUP.yaml`), its errors in PostHog (`ERR.yaml`), an allow list of users (`ACC.yaml`), recorded tasks
(`CAP.yaml`) and tasks in Trello (`TRL.yaml`): it keeps every message once, makes a task of a plain text, puts it on
the user's Trello board as a card, and lists the tasks on `/tasks`.

What it does now:

- `/start` gets the greeting (SK-1).
- Any other private-chat message but `/tasks` (and `/trello` with Trello set up), other commands included, goes into
  the sender's `inbox` as `text`, `voice`, `photo` or `other`, and the bot answers "Сохранил ✅" (SK-2). Each user's
  rows are theirs.
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
- With Trello set up (`TRELLO_API_KEY`, see [Tasks in Trello](#tasks-in-trello)), each user connects their own
  Trello with `/trello` (TRL#1), every task becomes a card on their board «todobot» (TRL#2, TRL#3), and `/tasks`
  reads the board (TRL#4). Without `TRELLO_API_KEY` there is no Trello at all: `/trello` is a command like any other
  (an inbox item, "Сохранил ✅"), and `/tasks` is as above.
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
(aiohttp's access log: `method`, `path`, `status`, `size`, `seconds`; never the query string, the Referer or the
caller's address); `log` (a library's line); the Trello events (see [Tasks in Trello](#tasks-in-trello)); and around
them `migrate_started`, `migrate_finished`, `config_error`, `<command>_failed`, `uncaught_exception`,
`unraisable_exception`.

## Layout

```
src/todobot/
  __main__.py     python -m todobot migrate | serve
  config.py       environment variables (the allow list among them), version fallback
  logs.py         JSON lines on stdout
  error_tracking.py  ERROR lines to PostHog as $exception, server_started (only with POSTHOG_PROJECT_TOKEN)
  web.py          aiohttp app: the webhook (answers once the update is handled) and /healthz
  bot.py          aiogram dispatcher and handlers: the allow list, /start, /tasks, /trello, inbox, errors
  inbox.py        message kind, and storing it once per message, with its task when it is a plain text
  tasks.py        recording a task, the sender's tasks, and the /tasks list as Telegram messages
  texts.py        what the bot says (exact, from the requirement)
  crypto.py       secrets sealed for the database with APP_SECRET_KEY (Fernet)
  trello/         tasks in Trello (only with TRELLO_API_KEY): api.py (OAuth 1.0a and REST), connect.py (/trello and
                  the callback), board.py (the board, a card, the board read for /tasks), sync.py (the loop that
                  makes the cards), store.py (its tables)
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
| `TRELLO_API_KEY` | the bot's Trello app's API key; without it there is no Trello at all (TRL) | unset |
| `TRELLO_API_SECRET` | the app's OAuth secret | required with `TRELLO_API_KEY` |
| `PUBLIC_BASE_URL` | the bot's own `https://` address; Trello sends the browser back to `{PUBLIC_BASE_URL}/trello/callback` | required with `TRELLO_API_KEY` |
| `APP_SECRET_KEY` | encrypts the users' Trello tokens in the database: a Fernet key, 32 random bytes in url-safe base64 (44 characters), made with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Anything else is a `config_error`. Changing it makes the stored tokens unreadable: users then connect again with `/trello` | required with `TRELLO_API_KEY` |
| `TRELLO_API_BASE` | Trello's REST API | `https://api.trello.com` |
| `TRELLO_AUTH_BASE` | Trello's OAuth pages and endpoints | `https://trello.com` |
| `TRELLO_RETRY_SECONDS` | how often the loop tries again to make the cards Trello did not take (TRL#3) | `60` |

## Tasks in Trello

Only with `TRELLO_API_KEY` set (TRL). Each user connects their own Trello; the bot keeps their tasks there as cards.

- `/trello` asks Trello (OAuth 1.0a, `POST {TRELLO_AUTH_BASE}/1/OAuthGetRequestToken`, the callback
  `{PUBLIC_BASE_URL}/trello/callback`) for a request token, keeps it for the user who asked, and replies «Чтобы
  подключить Trello, открой ссылку и разреши доступ: <link>», the link being Trello's authorize page asking for read
  and write access with no end date (`scope=read,write`, `expiration=never`, `name=todobot`). The link works once,
  for 30 minutes; `/trello` again makes a new one and the old one stops working. `/trello` is not an inbox item.
- `GET /trello/callback` is where Trello sends the browser back. The user is found by the request token Trello brings
  back (issued at random to the bot, for that user only), never by anything else in the URL. Allowed: the bot gets
  the user's access token (`/1/OAuthGetAccessToken`), stores it encrypted with `APP_SECRET_KEY`, the page says
  «Trello подключён, можно возвращаться в Telegram.» and the chat gets «Trello подключён ✅ Задачи будут на доске
  «todobot».». Refused (no verifier): the page says «Trello не подключён.» and the chat «Trello не подключён: доступ не
  разрешён.». An unknown, used or expired request token: «Trello не подключён.» (400), nothing in any chat. Trello not
  giving the access token: «Trello не подключён.» (502), and the chat gets the usual apology. A new connection
  replaces the user's earlier one.
- The board (TRL#2): the user's open board «todobot», made (with `defaultLists=false`) when there is none, with the
  lists «Записана», «Исследуется», «Ждёт ответов», «План готов», «Сделана»; a missing one is added after the status
  list before it, and nothing else on the board is changed.
- Cards (TRL#3): every task becomes a card in «Записана»: a one-line text is the card's name, with no description;
  several lines give the first line as the name and the whole text as the description. The reply stays «Сохранил
  ✅» at once: the cards are made by a loop in `serve`, outside the webhook, at start, when a task is recorded or
  Trello connected, and every `TRELLO_RETRY_SECONDS`, for every task without a card whose user is connected (tasks
  recorded before connecting included). A task never gets a second card: its card id is stored; before Trello is
  asked for a card the task is marked, so after a crash or a lost answer the next round looks for that card on the
  board (same name, made since the mark, no task's yet) before it makes one; one process at a time runs a round (a
  Postgres advisory lock). Left over: a card renamed, moved off the board or deleted before the next round finds it
  gets a successor. When Trello says the list or the board is gone, the board is set up again.
- `/tasks` (TRL#4), connected: each task as its card is now, `<n>. <card's name> — <its list's name in lower case>`;
  an archived or deleted card is not shown; a task with no card yet is `<text> — ещё не в Trello`. When Trello does
  not answer: exactly «Trello сейчас не отвечает, попробуй позже.». A user who has not connected sees each task as
  `<text> — ещё не в Trello`. The header, the order, «Задач пока нет.» and the splitting are CAP#2's. A card is on
  the board as soon as Trello makes it, but its id is stored only once Trello's answer is back: `/tasks` first waits
  (at most 10 s) for a card of the user's being made right now, so a card already on the board is always known as
  its task's.
- Every call to Trello is OAuth 1.0a signed (HMAC-SHA1, `oauthlib`) in the `Authorization` header: no key or token
  in a URL. A card's text goes in a JSON body. One call waits at most 10 s.

Never logged, never sent to PostHog: the link `/trello` sends (it holds the request token), the request token, the
verifier, the access token, any secret, the tasks' or cards' texts. The callback's access line has no query string.

Its events: `trello_link_sent` (with `user_id`); `trello_connected`, `trello_declined` (with `user_id`);
`trello_callback_unknown` (`WARNING`, with `reason`: `unknown`, `expired` or `unreadable`); `board_ready` (with
`user_id`, `board_created`, `lists_added`); `card_created` and `card_found` (a card a lost answer had made, taken;
with `user_id`, `task_id`); `card_failed`, `board_failed`, `board_gone`, `trello_unavailable` (a `/tasks` Trello did
not answer), `trello_access_unreadable` (`APP_SECRET_KEY` changed) (all `WARNING`, with `user_id` and the call and
Trello's status as `error`); `trello_connect_failed`, `trello_callback_failed`, `trello_reply_failed`,
`trello_sync_failed` (`ERROR`, with the error's type or the call and status only).

Setting it up:

1. Create the bot's Trello app: in Trello's Power-Up admin portal (`https://trello.com/power-ups/admin`) make a new
   app (a Power-Up) in your workspace, then generate its API key. The key is `TRELLO_API_KEY`; the OAuth secret shown
   with it is `TRELLO_API_SECRET`. Add the bot's address (`PUBLIC_BASE_URL`, e.g.
   `https://todobot-production.up.railway.app`) to the app's allowed origins, so Trello sends the browser back to it.
2. Make a key for `APP_SECRET_KEY` (see [Environment](#environment)).
3. Put the variables into Railway (`TRELLO_API_KEY`, `TRELLO_API_SECRET`, `PUBLIC_BASE_URL`, `APP_SECRET_KEY`); the
   defaults serve the rest. Secrets live only there.
4. Add `/trello` to the bot's menu by hand, through BotFather, and send it.

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
  `DATABASE_URL`, webhook secret, PostHog's project token, the allow list, Trello's key and secret, `PUBLIC_BASE_URL`,
  `APP_SECRET_KEY`) live only in Railway.

The webhook is registered once, by hand, with the same secret:
`https://api.telegram.org/bot<token>/setWebhook?url=https://<host>/telegram/webhook&secret_token=<secret>`.

## Migrations

`python -m todobot migrate` applies them. To write a new one, change `src/todobot/schema.py`, then run
`DATABASE_URL=… uv run alembic revision --autogenerate -m "what changed"` against a migrated database. Review
what it wrote in `src/todobot/migrations/versions/`.

"""What the bot says. Exact, byte for byte, from the requirements (agentify/specs/requirements/: SK, ACC, CAP, TRL)."""

# SK-1: the reply to /start
START = "Привет! Присылай задачи текстом, голосом или картинкой — я их сохраню."

# SK-2: the reply to any other message, once it is stored (CAP#1: also when it became a task)
SAVED = "Сохранил ✅"

# SK-5: the reply when handling a message failed
ERROR = "Что-то пошло не так, попробуй ещё раз."

# ACC#1: the only reply to a user the allow list does not name, /start included
REFUSED = "Извини, бот пока закрыт."

# CAP#2: the reply to /tasks: the header, then one line per task, oldest first ...
TASKS_HEADER = "Твои задачи:"
TASK_LINE = "{number}. {text} — {status}"
# ... or, when the sender has no task, this alone
NO_TASKS = "Задач пока нет."

# CAP#1: a task's status (as the table `tasks` keeps it) and its label in the list
STATUS_LABELS = {"recorded": "записана"}

# TRL#1: the reply to /trello, then the page Trello sends the browser back to and the message in the chat, when the
# user allowed access ...
TRELLO_LINK = "Чтобы подключить Trello, открой ссылку и разреши доступ: {link}"
TRELLO_CONNECTED_PAGE = "Trello подключён, можно возвращаться в Telegram."
TRELLO_CONNECTED = "Trello подключён ✅ Задачи будут на доске «todobot»."
# ... and when they refused it
TRELLO_DECLINED_PAGE = "Trello не подключён."
TRELLO_DECLINED = "Trello не подключён: доступ не разрешён."

# TRL#2: the board and its lists, one per status, in this order; TRL#3: a task's card goes into the first
BOARD_NAME = "todobot"
STATUS_LISTS = ("Записана", "Исследуется", "Ждёт ответов", "План готов", "Сделана")

# TRL#4: /tasks with Trello connected. A task's label is its card's list's name in lower case, or this before it has a
# card; when Trello does not answer, the reply is this alone.
NOT_IN_TRELLO = "ещё не в Trello"
TRELLO_DOWN = "Trello сейчас не отвечает, попробуй позже."

"""What the bot says. Exact, byte for byte, from the requirements (agentify/specs/requirements/: SK, ACC, CAP)."""

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

"""What the bot says. Exact, byte for byte, from the requirements (agentify/specs/requirements/: SK, ACC)."""

# SK-1: the reply to /start
START = "Привет! Присылай задачи текстом, голосом или картинкой — я их сохраню."

# SK-2: the reply to any other message, once it is stored
SAVED = "Сохранил ✅"

# SK-5: the reply when handling a message failed
ERROR = "Что-то пошло не так, попробуй ещё раз."

# ACC#1: the only reply to a user the allow list does not name, /start included
REFUSED = "Извини, бот пока закрыт."

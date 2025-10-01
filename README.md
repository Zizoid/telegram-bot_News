# Telegram News Relay Bot

Это Telegram-бот, который отслеживает один или несколько каналов и пересылает их сообщения без изменений в ваш канал.

## Возможности

- Подписка на список каналов-источников (указывается в `.env`).
- Периодический опрос веб-версии Telegram (`https://t.me/s/<channel>`).
- Игнорирование уже пересланных сообщений (хранение в локальной SQLite базе).
- Публикация текста и медиа «один в один».
- Команды `/start`, `/help`, `/status` (статус доступен только админу).
- Логирование в файл `bot.log`.

## Требования

- Python 3.11+.
- Telegram Bot Token.

## Установка

```bash
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

## Настройка

Создайте файл `.env` в корне проекта:

```env
BOT_TOKEN=123456:ABC
PUBLISHER_CHANNEL_ID=@your_channel
ADMIN_CHAT_ID=123456789
SOURCE_CHANNELS=channel1,channel2
UPDATE_INTERVAL=300
FETCH_LIMIT=20
```

- `SOURCE_CHANNELS` — список каналов без `@`.
- `UPDATE_INTERVAL` — пауза между циклами проверки (в секундах).
- `FETCH_LIMIT` — максимальное количество сообщений, загружаемых за один проход с каждого канала.

## Запуск

```bash
venv\Scripts\python bot.py
```

## Лицензия

MIT

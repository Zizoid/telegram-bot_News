import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from telegram.error import TelegramError, BadRequest
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()
# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = "-1002700165816"   # ID канала как строка

async def start(update: Update, context: CallbackContext):
    """Обработчик команды /start"""
    try:
        await update.message.reply_text(
            "📷 Отправь фото брака\n"
            "Просто пришли мне фотографию или текст, и я перешлю её в канал!"
        )
    except Exception as e:
        logger.error(f"Ошибка в команде /start: {e}")

async def handle_content(update: Update, context: CallbackContext):
    """Обработчик фото и текстовых сообщений"""
    try:
        # Проверяем доступ к каналу
        try:
            chat = await context.bot.get_chat(CHANNEL_ID)
            logger.info(f"Канал доступен: {chat.title}")
        except BadRequest as e:
            error_msg = f"❌ Ошибка доступа к каналу: {e.message}"
            logger.error(error_msg)
            await update.message.reply_text(error_msg)
            return

        # Обработка фото
        if update.message.photo:
            photo = update.message.photo[-1]
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo.file_id,
                caption="",  # Без подписи
                disable_notification=True
            )
            await update.message.reply_text("✅ Фото отправлено в канал!")

        # Обработка текста
        elif update.message.text:
            text = update.message.text
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                disable_notification=True
            )
            await update.message.reply_text("✅ Текст отправлен в канал!")

        else:
            await update.message.reply_text("❌ Отправьте фото или текст")

    except TelegramError as e:
        logger.error(f"Ошибка Telegram: {e}")
        await update.message.reply_text("⚠️ Ошибка сети. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        await update.message.reply_text("⛔ Произошла непредвиденная ошибка")

def main():
    """Запуск бота"""
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        # Обработчики команд
        app.add_handler(CommandHandler("start", start))

        # Обработчики сообщений
        app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle_content))

        logger.info("Бот запущен и ожидает сообщений...")
        app.run_polling()

    except Exception as e:
        logger.critical(f"Фатальная ошибка запуска: {e}", exc_info=True)

if __name__ == "__main__":
    main()

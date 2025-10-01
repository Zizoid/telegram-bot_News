import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape as escape_html
from typing import Dict, List, Optional, Tuple

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Compatibility fixes for python-telegram-bot on Python 3.13
# ---------------------------------------------------------------------------

try:  # pragma: no cover - defensive compatibility patch
    from telegram.ext._updater import Updater as _Updater

    if hasattr(_Updater, "__slots__") and "_Updater__polling_cleanup_cb" not in _Updater.__slots__:
        _Updater.__slots__ = tuple(_Updater.__slots__) + ("_Updater__polling_cleanup_cb",)
except Exception:  # pylint: disable=broad-except
    pass


# ---------------------------------------------------------------------------
# Logging & configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

logger = logging.getLogger(__name__)


load_dotenv()


def require_env(var: str) -> str:
    value = os.getenv(var)
    if not value:
        raise ValueError(f"Environment variable {var} is required")
    return value


def load_config() -> Dict[str, object]:
    bot_token = require_env("BOT_TOKEN")
    publisher = require_env("PUBLISHER_CHANNEL_ID")

    channels_raw = os.getenv("SOURCE_CHANNELS", "").strip()
    if not channels_raw:
        raise ValueError("SOURCE_CHANNELS must contain at least one channel name")

    source_channels = [normalize_channel_name(item) for item in channels_raw.split(",") if item.strip()]
    if not source_channels:
        raise ValueError("SOURCE_CHANNELS must contain valid channel names")

    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    update_interval = int(os.getenv("UPDATE_INTERVAL", "600"))
    fetch_limit = int(os.getenv("FETCH_LIMIT", "20"))

    return {
        "bot_token": bot_token,
        "publisher_channel_id": publisher,
        "source_channels": source_channels,
        "admin_chat_id": admin_chat_id,
        "update_interval": update_interval,
        "fetch_limit": fetch_limit,
    }


def normalize_channel_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^https?://t\.me/(s/)?", "", name)
    name = name.lstrip("@")
    return name


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

DB_NAME = "posted_messages.db"


async def init_db() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_messages (
                message_id INTEGER NOT NULL,
                channel_username TEXT NOT NULL,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, channel_username)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel ON posted_messages (channel_username)"
        )
        await db.commit()


async def is_message_posted(message_id: int, channel_username: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT 1 FROM posted_messages WHERE message_id = ? AND channel_username = ?",
            (message_id, channel_username),
        )
        return (await cursor.fetchone()) is not None


async def add_posted_message(message_id: int, channel_username: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO posted_messages (message_id, channel_username) VALUES (?, ?)",
            (message_id, channel_username),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Telegram web parser
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    channel: str
    message_id: int
    text_html: str
    plain_text: str
    media_url: Optional[str]
    published_at: datetime


class TelegramChannelParser:
    TELEGRAM_WEB_TEMPLATE = "https://t.me/s/{channel}"

    def __init__(self, session: aiohttp.ClientSession, fetch_limit: int) -> None:
        self.session = session
        self.fetch_limit = fetch_limit

    async def fetch_messages(self, channel: str) -> List[ParsedMessage]:
        url = self.TELEGRAM_WEB_TEMPLATE.format(channel=channel)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/118.0 Safari/537.36"
            )
        }

        async with self.session.get(url, headers=headers) as response:
            if response.status != 200:
                logger.error("Failed to load channel %s: HTTP %s", channel, response.status)
                return []
            html = await response.text()

        soup = BeautifulSoup(html, "html.parser")
        container = soup.find_all("div", class_="tgme_widget_message")
        messages: List[ParsedMessage] = []

        for element in container:
            try:
                message_id = self._extract_message_id(element)
                if message_id is None:
                    continue

                text_html, plain_text = self._extract_text(element)
                media_url = self._extract_media(element)

                messages.append(
                    ParsedMessage(
                        channel=channel,
                        message_id=message_id,
                        text_html=text_html,
                        plain_text=plain_text,
                        media_url=media_url,
                        published_at=datetime.utcnow(),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Failed to parse message in %s: %s", channel, exc)

        messages.sort(key=lambda item: item.message_id)
        return messages[: self.fetch_limit]

    @staticmethod
    def _extract_message_id(element: Tag) -> Optional[int]:
        data_post = element.get("data-post")
        if not data_post:
            return None
        try:
            return int(data_post.split("/")[-1])
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _extract_text(element: Tag) -> Tuple[str, str]:
        text_block = element.find("div", class_="tgme_widget_message_text")
        if not text_block:
            return "", ""

        html_content = html_from_bs(text_block)
        plain_text = text_block.get_text("\n", strip=True)
        return html_content, plain_text

    @staticmethod
    def _extract_media(element: Tag) -> Optional[str]:
        photo = element.find("a", class_="tgme_widget_message_photo_wrap")
        if photo:
            style = photo.get("style", "")
            match = re.search(r"background-image:url\('([^']+)'\)", style)
            if match:
                return match.group(1)

        photo_view = element.find("a", class_="tgme_widget_message_photo_view")
        if photo_view and photo_view.has_attr("href"):
            return photo_view["href"]

        return None


# ---------------------------------------------------------------------------
# HTML conversion helpers
# ---------------------------------------------------------------------------

ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "a",
    "blockquote",
    "span",
    "tg-emoji",
}

TAG_MAP = {
    "strong": "b",
    "em": "i",
    "ins": "u",
    "strike": "s",
    "del": "s",
}


def html_from_bs(node: Tag) -> str:
    parts = [render_node(child) for child in node.children]
    html = "".join(parts)
    html = html.replace("\xa0", " ")
    return html


def render_node(node) -> str:
    if isinstance(node, NavigableString):
        return escape_html(str(node))

    if isinstance(node, Tag):
        name = node.name.lower()
        if name == "br":
            return "\n"

        if name not in ALLOWED_TAGS:
            return "".join(render_node(child) for child in node.children)

        mapped_name = TAG_MAP.get(name, name)

        inner = "".join(render_node(child) for child in node.children)

        if mapped_name == "span" or mapped_name == "tg-emoji":
            return inner

        if mapped_name == "a":
            href = node.get("href", "")
            if not href:
                return inner
            safe_href = escape_attribute(href)
            return f'<a href="{safe_href}">{inner}</a>'

        return f"<{mapped_name}>{inner}</{mapped_name}>"

    return ""


def escape_attribute(value: str) -> str:
    return escape_html(value, quote=True)


# ---------------------------------------------------------------------------
# News relay core
# ---------------------------------------------------------------------------


class NewsRelay:
    def __init__(
        self,
        app: Application,
        parser: TelegramChannelParser,
        publisher_channel_id: str,
        source_channels: List[str],
        update_interval: int,
    ) -> None:
        self.app = app
        self.parser = parser
        self.publisher_channel_id = publisher_channel_id
        self.source_channels = source_channels
        self.update_interval = update_interval
        self.update_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.stats: Dict[str, object] = {
            "last_run": None,
            "last_messages": 0,
            "total_messages": 0,
            "errors": [],
        }

    async def start(self) -> None:
        if self.update_task:
            return
        self.update_task = asyncio.create_task(self._runner())

    async def stop(self) -> None:
        if self.update_task:
            self.update_task.cancel()
            try:
                await self.update_task
            except asyncio.CancelledError:
                pass
            self.update_task = None

    async def _runner(self) -> None:
        while True:
            try:
                await self._process_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Cycle failed: %s", exc)
                self.stats["errors"].append(str(exc))
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(self.update_interval)

    async def _process_cycle(self) -> None:
        async with self.lock:
            self.stats["last_run"] = datetime.utcnow()
            self.stats["last_messages"] = 0

            for channel in self.source_channels:
                messages = await self.parser.fetch_messages(channel)
                for message in messages:
                    if await is_message_posted(message.message_id, channel):
                        continue

                    if await self._publish(message):
                        await add_posted_message(message.message_id, channel)
                        self.stats["last_messages"] += 1
                        self.stats["total_messages"] += 1
                        await asyncio.sleep(5)

            if self.stats["last_messages"]:
                logger.info("Published %s messages", self.stats["last_messages"])
            else:
                logger.info("No new messages")

    async def _publish(self, message: ParsedMessage) -> bool:
        text_html = message.text_html or message.plain_text

        if message.media_url:
            try:
                await self.app.bot.send_photo(
                    chat_id=self.publisher_channel_id,
                    photo=message.media_url,
                    caption=text_html if text_html else None,
                    parse_mode="HTML" if text_html else None,
                )
                logger.info("Forwarded photo message %s from %s", message.message_id, message.channel)
                return True
            except Exception as exc:
                logger.warning(
                    "Failed to send photo message %s from %s: %s",
                    message.message_id,
                    message.channel,
                    exc,
                )

        if text_html:
            try:
                await self.app.bot.send_message(
                    chat_id=self.publisher_channel_id,
                    text=text_html,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                logger.info("Forwarded text message %s from %s", message.message_id, message.channel)
                return True
            except Exception as exc:
                logger.error("Failed to send text message %s from %s: %s", message.message_id, message.channel, exc)
                return False

        logger.info("Skipping empty message %s from %s", message.message_id, message.channel)
        return False


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source_channels = ", ".join(context.bot_data.get("source_channels", []))
    if update.effective_message:
        await update.effective_message.reply_text(
            "Бот запущен. Отслеживаем каналы: {channels}".format(channels=source_channels)
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source_channels = ", ".join(context.bot_data.get("source_channels", []))
    if update.effective_message:
        await update.effective_message.reply_text(
            "Этот бот копирует новости из каналов: {channels}\n"
            "Команды:\n"
            "/start — статус\n"
            "/help — помощь\n"
            "/status — статистика (для администратора)".format(channels=source_channels)
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_chat_id = context.bot_data.get("admin_chat_id")
    relay: Optional[NewsRelay] = context.bot_data.get("relay")
    if not admin_chat_id or str(update.effective_user.id) != str(admin_chat_id):
        if update.effective_message:
            await update.effective_message.reply_text("Доступ запрещен")
        return

    if not relay:
        if update.effective_message:
            await update.effective_message.reply_text("Релэй не запущен")
        return

    last_run = relay.stats.get("last_run")
    last_run_text = last_run.strftime("%Y-%m-%d %H:%M:%S") if last_run else "еще ни разу"
    response = (
        "Последний проход: {last_run}\n"
        "Сообщений в последнем проходе: {last_messages}\n"
        "Всего переслано: {total}".format(
            last_run=last_run_text,
            last_messages=relay.stats.get("last_messages", 0),
            total=relay.stats.get("total_messages", 0),
        )
    )

    errors = relay.stats.get("errors", [])
    if errors:
        response += "\nОшибки:\n" + "\n".join(errors[-5:])

    if update.effective_message:
        await update.effective_message.reply_text(response)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app: Application) -> None:
    config = app.bot_data["config"]
    await init_db()

    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    parser = TelegramChannelParser(session=session, fetch_limit=config["fetch_limit"])
    relay = NewsRelay(
        app=app,
        parser=parser,
        publisher_channel_id=config["publisher_channel_id"],
        source_channels=config["source_channels"],
        update_interval=config["update_interval"],
    )

    app.bot_data["aiohttp_session"] = session
    app.bot_data["parser"] = parser
    app.bot_data["relay"] = relay

    await relay.start()
    logger.info("News relay started")


async def on_shutdown(app: Application) -> None:
    relay: Optional[NewsRelay] = app.bot_data.get("relay")
    if relay:
        await relay.stop()

    session: Optional[aiohttp.ClientSession] = app.bot_data.get("aiohttp_session")
    if session and not session.closed:
        await session.close()

    logger.info("Bot stopped")


def build_application(config: Dict[str, object]) -> Application:
    application = Application.builder().token(config["bot_token"]).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    if config.get("admin_chat_id"):
        application.add_handler(CommandHandler("status", status_command))

    application.bot_data["config"] = config
    application.bot_data["source_channels"] = config["source_channels"]
    application.bot_data["admin_chat_id"] = config.get("admin_chat_id")

    application.post_init = on_startup
    application.post_shutdown = on_shutdown

    return application


def main() -> None:
    try:
        config = load_config()
    except Exception as exc:  # pragma: no cover - startup validation
        logger.critical("Failed to load configuration: %s", exc)
        return

    logger.info(
        "Starting bot. Publisher=%s, sources=%s",
        config["publisher_channel_id"],
        ", ".join(config["source_channels"]),
    )

    application = build_application(config)

    try:
        application.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        logger.info("Bot stopped by user")


if __name__ == "__main__":
    main()

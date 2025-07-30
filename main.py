import logging
import asyncio
import aiohttp
import feedparser
import os
import html
import hashlib
import re
import json
import time
import shutil
from datetime import datetime, timedelta
from textwrap import dedent
from bs4 import BeautifulSoup
from telegram import InputMediaPhoto
from telegram.ext import Application
from typing import Optional, Dict, List
from logging.handlers import RotatingFileHandler

# Настройка логирования с ротацией
log_handler = RotatingFileHandler(
    'news_bot.log', 
    maxBytes=5*1024*1024,  # 5 MB
    backupCount=3
)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        log_handler,
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN') 
CHANNEL_ID = os.getenv('CHANNEL_ID') 
ADMIN_ID = os.getenv('ADMIN_ID')  # ID администратора для уведомлений
NEWS_SOURCES = {
    "Habr": "https://habr.com/ru/rss/articles/",
    "3DNews": "https://3dnews.ru/news/rss//",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index/",
    "Engadget": "https://www.engadget.com/rss.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "TechNode": "https://technode.com/feed/",
    "South China Morning Post (Tech)": "https://www.scmp.com/rss/92/feed/",
    "Nikkei Asia (Tech)": "https://www.techinasia.com/japan/feed",
    "Tech in Asia (Japan) ": "https://www.techinasia.com/japan/feed",
    "Japan Times (Tech News)": "https://www.japantimes.co.jp/search?query=feed",
}
UPDATE_INTERVAL = 300  # 5 минут
DEEPLX_API_URL = "https://deeplx.vercel.app/translate"
MYMEMORY_API_URL = "https://api.mymemory.translated.net/get"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}
STATE_FILE = "news_bot_state.json"  # Файл для сохранения состояния
BACKUP_DIR = "state_backups"  # Директория для резервных копий состояния
MAX_HISTORY_SIZE = 1000  # Максимальное количество хранимых записей
MAX_CACHE_SIZE = 1000  # Максимальный размер кэша переводов

# Резервные RSS-источники
BACKUP_SOURCES = {
    "Reuters": "https://feeds.reuters.com/Reuters/worldNews",
    "CNN": "http://rss.cnn.com/rss/edition_world.rss",
}

# Настройки исследовательского агента
RESEARCH_AGENT_ENABLED = True  # Включить/выключить исследовательского агента
RESEARCH_KEYWORDS = [  # Ключевые слова для активации исследования
    "AI", "искусственный интеллект", "машинное обучение", 
    "нейросети", "deep learning", "LLM", "GPT",
    "квантовые вычисления", "quantum computing",
    "криптовалюта", "blockchain", "Web3",
    "кибербезопасность", "cybersecurity"
]
RESEARCH_MAX_LENGTH = 2000  # Максимальная длина исследовательского отчета
RESEARCH_MIN_LENGTH = 800   # Минимальная длина для активации исследования
RESEARCH_CACHE_FILE = "research_cache.json"  # Кэш исследовательских отчетов

class ResearchAgent:
    """Исследовательский агент для углубленного анализа новостей"""
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.cache = self.load_cache()
        self.last_cache_clear = datetime.now()
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
    
    def load_cache(self) -> Dict[str, str]:
        """Загрузка кэша исследований"""
        try:
            if os.path.exists(RESEARCH_CACHE_FILE):
                with open(RESEARCH_CACHE_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки кэша исследований: {e}")
        return {}
    
    def save_cache(self):
        """Сохранение кэша исследований"""
        try:
            with open(RESEARCH_CACHE_FILE, 'w') as f:
                json.dump(self.cache, f)
        except Exception as e:
            logger.error(f"Ошибка сохранения кэша исследований: {e}")
    
    def should_research(self, title: str, description: str) -> bool:
        """Определяет, нужно ли проводить исследование по новости"""
        if not RESEARCH_AGENT_ENABLED:
            return False
        
        # Проверка по длине описания
        if len(description) < RESEARCH_MIN_LENGTH:
            return False
        
        # Проверка по ключевым словам
        content = f"{title} {description}".lower()
        return any(keyword.lower() in content for keyword in RESEARCH_KEYWORDS)
    
    async def research_topic(self, topic: str, max_length: int = RESEARCH_MAX_LENGTH) -> str:
        """Проводит исследование по теме с использованием DeepSeek API"""
        start_time = time.time()
        try:
            # Проверка кэша
            cache_key = hashlib.md5(topic.encode()).hexdigest()
            if cache_key in self.cache:
                return self.cache[cache_key]
            
            # Системный промпт для исследования
            system_prompt = dedent("""\
                Ты — ведущий журналист-аналитик с опытом работы в The New York Times. 
                Проведи углубленное исследование по теме, соблюдая профессиональные стандарты журналистики.
                
                Структура отчета:
                1. Ключевые тезисы (основные выводы)
                2. Контекст и предыстория
                3. Анализ доказательств и данных
                4. Мнения экспертов (если доступны)
                5. Прогнозы и будущие последствия
                6. Рекомендации для дальнейшего изучения
                
                Требования:
                - Используй только проверенные факты
                - Сохраняй нейтральный тон
                - Адаптируй сложные концепции для широкой аудитории
                - Ограничь отчет {} символами
            """).format(max_length)
            
            # Запрос к DeepSeek API
            headers = {
                "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": topic}
                ],
                "max_tokens": max_length,
                "temperature": 0.3
            }
            
            async with self.session.post(self.api_url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    report = data['choices'][0]['message']['content']
                    
                    # Сохраняем в кэш
                    self.cache[cache_key] = report
                    self.save_cache()
                    
                    logger.info(f"Исследование DeepSeek завершено за {time.time() - start_time:.2f} сек")
                    return report
                else:
                    error = await response.text()
                    logger.error(f"Ошибка DeepSeek API: {response.status} - {error}")
                    return f"⚠️ Не удалось провести исследование. Ошибка API: {response.status}"
        
        except Exception as e:
            logger.error(f"Ошибка исследования: {str(e)}")
            return f"⚠️ Ошибка исследования: {str(e)}"

class NewsBot:
    def __init__(self):
        self.posted_links = set()
        self.content_checks = set()
        self.session = aiohttp.ClientSession(headers=HEADERS)
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.translation_cache = {}
        self.running = False
        self.update_task = None
        self.research_agent = ResearchAgent(self.session)
        self.last_backup = datetime.now()
        self.last_cache_clear = datetime.now()
        self.load_state()

    async def send_admin_alert(self, message: str):
        """Отправка уведомления администратору"""
        if not ADMIN_ID:
            return
            
        try:
            await self.app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 Ошибка бота: {message}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления администратору: {e}")

    def load_state(self):
        """Загрузка состояния из файла"""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    self.posted_links = set(data.get('posted_links', []))
                    self.content_checks = set(data.get('content_checks', []))
                    logger.info(f"Загружено состояние: {len(self.posted_links)} ссылок, {len(self.content_checks)} хешей")
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {e}")
            self.send_admin_alert(f"Ошибка загрузки состояния: {e}")

    def save_state(self):
        """Сохранение состояния в файл"""
        try:
            # Ограничиваем размер истории
            if len(self.posted_links) > MAX_HISTORY_SIZE:
                self.posted_links = set(list(self.posted_links)[-MAX_HISTORY_SIZE:])
            if len(self.content_checks) > MAX_HISTORY_SIZE:
                self.content_checks = set(list(self.content_checks)[-MAX_HISTORY_SIZE:])

            data = {
                'posted_links': list(self.posted_links),
                'content_checks': list(self.content_checks)
            }
            with open(STATE_FILE, 'w') as f:
                json.dump(data, f)
            logger.debug("Состояние сохранено")
            
            # Создание резервной копии раз в сутки
            if datetime.now() - self.last_backup > timedelta(days=1):
                self.create_state_backup()
                self.last_backup = datetime.now()
                
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния: {e}")
            self.send_admin_alert(f"Ошибка сохранения состояния: {e}")

    def create_state_backup(self):
        """Создание резервной копии состояния"""
        try:
            if not os.path.exists(BACKUP_DIR):
                os.makedirs(BACKUP_DIR)
                
            backup_file = os.path.join(BACKUP_DIR, f"state_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
            shutil.copy2(STATE_FILE, backup_file)
            
            # Удаляем старые бэкапы (>7 дней)
            for f in os.listdir(BACKUP_DIR):
                file_path = os.path.join(BACKUP_DIR, f)
                if os.path.isfile(file_path) and os.stat(file_path).st_mtime < (time.time() - 7 * 86400):
                    os.remove(file_path)
                    
            logger.info(f"Создана резервная копия состояния: {backup_file}")
        except Exception as e:
            logger.error(f"Ошибка создания резервной копии: {e}")

    def clear_caches(self):
        """Очистка кэшей при превышении размера"""
        try:
            # Очистка кэша переводов
            if len(self.translation_cache) > MAX_CACHE_SIZE:
                self.translation_cache.clear()
                logger.info("Кэш переводов очищен")
                
            # Очистка кэша исследований раз в час
            if datetime.now() - self.last_cache_clear > timedelta(hours=1):
                self.research_agent.cache.clear()
                self.research_agent.save_cache()
                self.last_cache_clear = datetime.now()
                logger.info("Кэш исследований очищен")
                
        except Exception as e:
            logger.error(f"Ошибка очистки кэшей: {e}")

    def escape_markdown(self, text: str) -> str:
        """Экранирование всех специальных символов MarkdownV2"""
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

    def clean_html_content(self, text: str) -> str:
        """Очистка HTML-контента от ненужных элементов"""
        if not text:
            return ""

        # Удаление всех HTML-тегов
        text = re.sub(r'<[^>]+>', '', text)

        # Удаление видео-блоков и рекламы
        text = re.sub(r'Video Ad Feedback', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Now playing - Source:.*?$', '', text)
        text = re.sub(r'\.\.\.', '', text)
        text = re.sub(r' - CNN$', '', text)

        # Удаление временных меток
        text = re.sub(r'\d{1,2}:\d{2}', '', text)

        return text

    async def fetch_article_text(self, url: str) -> Optional[str]:
        """Получение основного текста статьи по URL"""
        start_time = time.time()
        try:
            async with self.session.get(url, timeout=20) as response:
                if response.status == 200:
                    html_content = await response.text()

                    # Очистка контента
                    cleaned_content = self.clean_html_content(html_content)

                    # Получаем текст без HTML тегов
                    result = ' '.join(cleaned_content.split())[:500]
                    logger.info(f"Статья получена за {time.time() - start_time:.2f} сек: {url}")
                    return result
        except Exception as e:
            logger.error(f"Ошибка получения статьи {url}: {e}")
        return None

    def is_russian(self, text: str) -> bool:
        """Проверяет, содержит ли текст русские символы"""
        russian_letters = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        return any(char.lower() in russian_letters for char in text)

    async def translate_with_mymemory(self, text: str, target_lang: str = "RU") -> str:
        """Резервный перевод через MyMemory API"""
        if not text:
            return text

        start_time = time.time()
        try:
            params = {
                'q': text,
                'langpair': f'auto|{target_lang.lower()}'
            }
            async with self.session.get(MYMEMORY_API_URL, params=params, timeout=20) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get('responseStatus') == 200:
                        translated = result['responseData']['translatedText']
                        # Корректировка регистра
                        if translated.isupper():
                            translated = translated.capitalize()
                        logger.info(f"MyMemory перевод за {time.time() - start_time:.2f} сек")
                        return translated
        except Exception as e:
            logger.error(f"Ошибка перевода через MyMemory: {e}")

        return text

    async def translate_text(self, text: str, target_lang: str = "RU") -> str:
        """Перевод текста с использованием основного и резервного сервисов"""
        if not text:
            return text

        # Проверяем, не русский ли уже текст
        if self.is_russian(text):
            return text

        cache_key = hashlib.md5(f"{text}_{target_lang}".encode()).hexdigest()
        if cache_key in self.translation_cache:
            return self.translation_cache[cache_key]

        start_time = time.time()
        
        # Сначала пробуем DeepLX
        for attempt in range(2):
            try:
                payload = {
                    "text": text,
                    "source_lang": "auto",
                    "target_lang": target_lang
                }
                async with self.session.post(
                    DEEPLX_API_URL, 
                    json=payload, 
                    timeout=20
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get('code') == 200:
                            translated = result.get('data', text)
                            # Проверяем, что перевод на русском
                            if self.is_russian(translated):
                                self.translation_cache[cache_key] = translated
                                logger.info(f"DeepLX перевод за {time.time() - start_time:.2f} сек")
                                return translated
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Ошибка DeepLX (попытка {attempt+1}): {e}")
                await asyncio.sleep(1)

        # Если DeepLX не сработал, пробуем MyMemory
        translated = await self.translate_with_mymemory(text, target_lang)
        if self.is_russian(translated):
            self.translation_cache[cache_key] = translated
            return translated

        return text  # Возвращаем оригинальный текст

    def clean_content(self, text: str, max_length: int = 300) -> str:
        """Очистка и нормализация текста"""
        if not text:
            return ""

        # Удаляем все HTML-теги
        text = re.sub(r'<[^>]+>', '', text)

        # Декодируем HTML-сущности
        text = html.unescape(text)

        # Удаление технических меток
        text = re.sub(r'Video Ad Feedback', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Now playing - Source:.*?$', '', text)
        text = re.sub(r'\d{1,2}:\d{2}', '', text)
        text = re.sub(r'\.\.\.', '', text)
        text = re.sub(r' - CNN$', '', text)

        # Удаление лишних пробелов и переносов
        text = ' '.join(text.split())

        # Ограничение длины
        return text[:max_length].strip()

    async def get_best_image(self, entry) -> Optional[str]:
        """Поиск лучшего изображения для новости"""
        start_time = time.time()
        try:
            # Проверяем разные источники изображений в порядке приоритета
            sources = [
                lambda: next((media['url'] for media in entry.get('media_content', []) 
                             if media.get('type', '').startswith('image/')), None),
                lambda: next((enc['href'] for enc in entry.get('enclosures', [])
                             if enc.get('type', '').startswith('image/')), None),
                lambda: entry.get('image', {}).get('href') if isinstance(entry.get('image'), dict) else None,
                lambda: entry.get('image') if isinstance(entry.get('image'), str) else None,
                lambda: entry.get('media_thumbnail', {}).get('url')
            ]

            for source in sources:
                image_url = source()
                if image_url and image_url.startswith('http'):
                    return image_url

            # Если в RSS нет, парсим страницу
            link = entry.get('link')
            if link:
                async with self.session.get(link, timeout=20) as response:
                    if response.status == 200:
                        html_content = await response.text()

                        # Ищем Open Graph изображение
                        og_image_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_content)
                        if og_image_match:
                            return og_image_match.group(1)

                        # Ищем Twitter изображение
                        twitter_image_match = re.search(r'<meta\s+property="twitter:image"\s+content="([^"]+)"', html_content)
                        if twitter_image_match:
                            return twitter_image_match.group(1)

                        # Ищем первое подходящее изображение в статье
                        img_match = re.search(r'<img[^>]+src="([^"]+)"', html_content)
                        if img_match:
                            return img_match.group(1)
        except Exception as e:
            logger.error(f"Ошибка поиска изображения: {e}")
        finally:
            logger.info(f"Поиск изображения занял {time.time() - start_time:.2f} сек")
        return None

    async def prepare_news_message(self, entry: Dict) -> Optional[Dict]:
        """Подготовка новостного сообщения с возможностью исследования"""
        start_time = time.time()
        try:
            # Получаем оригинальные данные
            original_title = self.clean_content(entry.get('title', ''), 120)
            if not original_title:
                logger.debug("Пропуск новости без заголовка")
                return None

            link = entry.get('link', '')
            if not link:
                logger.debug("Новость без ссылки")
                return None

            # Проверка по URL
            if link in self.posted_links:
                logger.debug(f"Ссылка уже публиковалась: {link}")
                return None

            # Получаем описание
            original_description = self.clean_content(entry.get('description', ''), 500)
            if not original_description:
                # Если описания нет, получаем начало статьи
                article_text = await self.fetch_article_text(link)
                if article_text:
                    original_description = article_text
                else:
                    logger.debug(f"Не удалось получить текст для статьи: {link}")
                    return None

            # Генерируем хеш для проверки дублирования
            content_hash = hashlib.md5(f"{original_title}{original_description}".encode()).hexdigest()

            # Проверка по хешу контента
            if content_hash in self.content_checks:
                logger.debug(f"Дубликат контента: {original_title}")
                return None

            # Переводим заголовок
            translated_title = await self.translate_text(original_title)

            # Определяем, нужно ли проводить исследование
            research_report = None
            if self.research_agent.should_research(original_title, original_description):
                logger.info(f"Активация исследовательского агента для: {original_title}")
                research_topic = f"{original_title}\n\n{original_description}"
                research_report = await self.research_agent.research_topic(research_topic)
                # Если отчет слишком длинный, сокращаем
                if research_report and len(research_report) > RESEARCH_MAX_LENGTH:
                    research_report = research_report[:RESEARCH_MAX_LENGTH] + "..."
            
            # Переводим описание или используем отчет
            if research_report:
                translated_description = research_report
            else:
                translated_description = await self.translate_text(original_description)

            # Получаем изображение
            image_url = await self.get_best_image(entry)

            # Получаем категорию
            original_category = "Общее"
            if entry.get('tags'):
                if isinstance(entry['tags'], list) and len(entry['tags']) > 0:
                    first_tag = entry['tags'][0]
                    if isinstance(first_tag, dict) and 'term' in first_tag:
                        original_category = first_tag['term']
                    elif isinstance(first_tag, str):
                        original_category = first_tag

            translated_category = await self.translate_text(original_category)

            # Определяем источник
            source = "Новости"
            for src_name, src_url in NEWS_SOURCES.items():
                if src_url in entry.get('id', '') or src_url in link:
                    source = src_name
                    break

            # Экранируем все специальные символы MarkdownV2
            escaped_title = self.escape_markdown(translated_title)
            escaped_description = self.escape_markdown(translated_description)
            escaped_category = self.escape_markdown(translated_category)
            escaped_source = self.escape_markdown(source.replace(' ', ''))

            # Формируем сообщение
            message = (
                f"📰 *{escaped_title}*\n\n"
                f"{escaped_description}\n\n"
            )
            
            # Добавляем метки только если не исследовательский отчет
            if not research_report:
                message += (
                    f"\\#{escaped_source} \\#{escaped_category}\n"
                    f"🔗 [Читать полностью]({link})"
                )
            else:
                message += (
                    f"🔍 *Исследовательский отчет* \\#{escaped_source}\n"
                    f"🔗 [Исходная статья]({link})"
                )

            logger.info(f"Подготовка новости заняла {time.time() - start_time:.2f} сек")
            return {
                'message': message,
                'image_url': image_url,
                'link': link,
                'content_hash': content_hash,
                'is_research': bool(research_report)
            }

        except Exception as e:
            logger.error(f"Ошибка подготовки новости: {e}", exc_info=True)
            return None

    async def send_news_item(self, news_item: Dict) -> bool:
        """Отправка новости в Telegram канал"""
        start_time = time.time()
        try:
            # Для исследовательских отчетов отправляем без превью
            disable_preview = news_item.get('is_research', False)
            
            # Если есть изображение
            if news_item['image_url'] and not disable_preview:
                media = InputMediaPhoto(
                    media=news_item['image_url'],
                    caption=news_item['message'],
                    parse_mode='MarkdownV2'
                )
                await self.app.bot.send_media_group(
                    chat_id=CHANNEL_ID,
                    media=[media]
                )
            else:
                await self.app.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=news_item['message'],
                    parse_mode='MarkdownV2',
                    disable_web_page_preview=disable_preview
                )

            # Добавляем в опубликованные
            self.posted_links.add(news_item['link'])
            self.content_checks.add(news_item['content_hash'])
            self.save_state()

            logger.info(f"Опубликовано за {time.time() - start_time:.2f} сек: {news_item['link']}")
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки новости: {e}")
            if 'content_hash' in news_item:
                self.content_checks.discard(news_item['content_hash'])
            return False

    async def fetch_feed(self, url: str) -> Optional[Dict]:
        """Получение RSS-ленты с повторными попытками и резервными источниками"""
        start_time = time.time()
        try:
            async with self.session.get(url, timeout=25) as response:
                if response.status == 200:
                    feed_content = await response.text()
                    result = feedparser.parse(feed_content)
                    logger.info(f"RSS получен за {time.time() - start_time:.2f} сек: {url}")
                    return result
        except Exception as e:
            logger.warning(f"Ошибка получения RSS ({url}): {e}")

            # Пробуем резервный источник
            for name, source_url in NEWS_SOURCES.items():
                if source_url == url and name in BACKUP_SOURCES:
                    backup_url = BACKUP_SOURCES[name]
                    logger.info(f"Пробую резервный источник для {name}: {backup_url}")
                    try:
                        async with self.session.get(backup_url, timeout=25) as resp:
                            if resp.status == 200:
                                feed_content = await resp.text()
                                result = feedparser.parse(feed_content)
                                logger.info(f"Резервный RSS получен за {time.time() - start_time:.2f} сек: {backup_url}")
                                return result
                    except Exception as e2:
                        logger.error(f"Ошибка резервного источника {backup_url}: {e2}")

            logger.error(f"Все источники для {url} недоступны")
            return None

    async def fetch_and_publish_news(self):
        """Получение и публикация новостей"""
        while self.running:
            try:
                logger.info(f"Начало проверки {len(NEWS_SOURCES)} источников...")
                start_cycle_time = time.time()

                for source_name, source_url in NEWS_SOURCES.items():
                    try:
                        logger.info(f"Проверка источника: {source_name}")
                        feed = await self.fetch_feed(source_url)
                        if not feed or not feed.entries:
                            logger.warning(f"Нет новостей в источнике: {source_name}")
                            continue

                        logger.info(f"Найдено {len(feed.entries)} новостей в {source_name}")

                        # Берем только 3 последних новостей
                        for entry in feed.entries[:3]:
                            try:
                                news_item = await self.prepare_news_message(entry)
                                if news_item:
                                    logger.info(f"Подготовлено: {news_item.get('content_hash', '')}")
                                    success = await self.send_news_item(news_item)
                                    if success:
                                        logger.info(f"Успешно опубликовано: {source_name}")
                                    else:
                                        logger.warning(f"Ошибка публикации: {source_name}")

                                    # Задержка между отправкой новостей
                                    await asyncio.sleep(25)
                                else:
                                    logger.debug("Пропуск новости (не подготовлено)")
                            except Exception as e:
                                logger.error(f"Ошибка обработки новости: {e}")
                    except Exception as e:
                        logger.error(f"Ошибка обработки источника {source_name}: {e}")

                # Очистка кэшей
                self.clear_caches()
                
                cycle_duration = time.time() - start_cycle_time
                logger.info(f"Цикл проверки завершен за {cycle_duration:.2f} сек. Ожидание {UPDATE_INTERVAL} сек.")
                await asyncio.sleep(UPDATE_INTERVAL)
            except Exception as e:
                logger.error(f"Ошибка в основном цикле публикации новостей: {e}", exc_info=True)
                await self.send_admin_alert(f"Ошибка в основном цикле: {str(e)}")
                await asyncio.sleep(60)

    async def run(self):
        """Запуск бота"""
        self.running = True
        await self.app.initialize()
        await self.app.start()
        logger.info(f"Бот запущен. Канал: {CHANNEL_ID}")
        self.update_task = asyncio.create_task(self.fetch_and_publish_news())
        logger.info("Фоновый процесс публикации новостей запущен")

    async def stop(self):
        """Остановка бота"""
        logger.info("Остановка бота...")
        self.running = False

        if self.update_task:
            self.update_task.cancel()
            try:
                await self.update_task
            except asyncio.CancelledError:
                logger.info("Фоновый процесс публикации остановлен")

        self.save_state()
        await self.app.stop()
        await self.app.shutdown()
        await self.session.close()
        logger.info("Бот полностью остановлен")

async def main():
    bot = NewsBot()
    try:
        await bot.run()
        while bot.running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}", exc_info=True)
        await bot.send_admin_alert(f"Фатальная ошибка: {str(e)}")
    finally:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())

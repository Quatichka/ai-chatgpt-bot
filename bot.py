"""Telegram-бот в стиле ChatGPT: отвечает, рисует картинки, создаёт файлы.

Работает с любым OpenAI-совместимым API (AgentRouter, OpenAI, OpenRouter и т.п.).
Настройки — через переменные окружения (см. .env.example).
"""
import asyncio
import base64
import json
import logging
import os
from collections import defaultdict, deque

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message
from openai import AsyncOpenAI

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# bothost может передавать токен под разными именами
TELEGRAM_BOT_TOKEN = next(
    v for v in (os.getenv(k) for k in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TOKEN", "TELEGRAM_TOKEN")) if v
)
API_KEY = os.environ["API_KEY"]
API_BASE_URL = os.getenv("API_BASE_URL", "https://agentrouter.org/v1")
CHAT_MODEL = os.getenv("CHAT_MODEL", "")  # пусто = взять первую доступную модель
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
# Через запятую: Telegram user id, кому разрешено пользоваться. Пусто = всем.
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный ассистент в Telegram, как ChatGPT. Отвечай на языке пользователя, "
    "понятно и по делу. Если пользователь просит нарисовать/сгенерировать изображение — "
    "вызови инструмент generate_image. Если просит создать файл (код, документ, таблицу CSV, "
    "текст и т.п.) — вызови инструмент create_file с полным содержимым файла. "
    "Не используй Markdown-разметку со звёздочками, пиши обычным текстом.",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Сгенерировать изображение по текстовому описанию и отправить пользователю.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Подробное описание картинки (лучше на английском)."},
                    "size": {"type": "string", "enum": ["1024x1024", "1024x1536", "1536x1024"]},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Создать файл и отправить его пользователю документом.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Имя файла с расширением, например report.md"},
                    "content": {"type": "string", "description": "Полное текстовое содержимое файла."},
                },
                "required": ["filename", "content"],
            },
        },
    },
]

client = AsyncOpenAI(api_key=API_KEY, base_url=API_BASE_URL)
dp = Dispatcher()
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
user_model: dict[int, str] = {}
state = {"default_model": CHAT_MODEL}

TEXT_EXT = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".css", ".xml", ".yaml",
    ".yml", ".ini", ".cfg", ".log", ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".php",
    ".sh", ".bat", ".ps1", ".toml", ".kt", ".swift", ".rb",
}


def allowed(message: Message) -> bool:
    return not ALLOWED_USERS or (message.from_user and message.from_user.id in ALLOWED_USERS)


async def list_models() -> list[str]:
    models = await client.models.list()
    return sorted(m.id for m in models.data)


async def get_model(chat_id: int) -> str:
    if chat_id in user_model:
        return user_model[chat_id]
    if not state["default_model"]:
        ids = await list_models()
        if not ids:
            raise RuntimeError("API не вернул ни одной модели — задайте CHAT_MODEL")
        state["default_model"] = ids[0]
        log.info("CHAT_MODEL не задан, использую %s", ids[0])
    return state["default_model"]


async def send_long(message: Message, text: str) -> None:
    text = text.strip() or "(пустой ответ)"
    for i in range(0, len(text), 4000):
        await message.answer(text[i : i + 4000])


async def keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def make_image(prompt: str, size: str = "1024x1024") -> bytes:
    resp = await client.images.generate(model=IMAGE_MODEL, prompt=prompt, size=size, n=1)
    item = resp.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    async with httpx.AsyncClient(timeout=120) as http:
        r = await http.get(item.url)
        r.raise_for_status()
        return r.content


async def run_tool(message: Message, name: str, args: dict) -> str:
    if name == "generate_image":
        prompt = args.get("prompt", "")
        try:
            data = await make_image(prompt, args.get("size") or "1024x1024")
        except Exception as e:
            log.exception("Ошибка генерации картинки")
            return f"Ошибка генерации изображения: {e}"
        await message.answer_photo(BufferedInputFile(data, "image.png"), caption=prompt[:1000])
        return "Изображение сгенерировано и отправлено пользователю."
    if name == "create_file":
        filename = os.path.basename(args.get("filename") or "file.txt")
        content = args.get("content", "")
        await message.answer_document(BufferedInputFile(content.encode("utf-8"), filename))
        return f"Файл {filename} отправлен пользователю."
    return f"Неизвестный инструмент {name}"


async def ask_ai(message: Message, user_content) -> None:
    chat_id = message.chat.id
    hist = history[chat_id]
    hist.append({"role": "user", "content": user_content})
    model = await get_model(chat_id)

    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(message.bot, chat_id, stop))
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *hist]
        for _ in range(5):  # максимум 5 раундов вызова инструментов
            resp = await client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                answer = msg.content or ""
                hist.append({"role": "assistant", "content": answer})
                await send_long(message, answer)
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await run_tool(message, tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                hist.append({"role": "assistant", "content": f"[{tc.function.name}] {result}"})
        await message.answer("Слишком много шагов, попробуйте переформулировать запрос.")
    finally:
        stop.set()
        await typing


# ---------- команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я ИИ-ассистент.\n\n"
        "Просто пиши — отвечу. Попроси «нарисуй ...» — сгенерирую картинку, "
        "«сделай файл ...» — пришлю файл. Можно присылать фото и текстовые файлы.\n\n"
        "/image <описание> — сразу нарисовать\n"
        "/reset — забыть историю диалога\n"
        "/models — список моделей\n"
        "/model <id> — выбрать модель"
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    history.pop(message.chat.id, None)
    await message.answer("История очищена.")


@dp.message(Command("models"))
async def cmd_models(message: Message):
    if not allowed(message):
        return
    try:
        ids = await list_models()
    except Exception as e:
        await message.answer(f"Не удалось получить список моделей: {e}")
        return
    current = await get_model(message.chat.id)
    await send_long(message, f"Текущая: {current}\n\nДоступные:\n" + "\n".join(ids))


@dp.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject):
    if not allowed(message):
        return
    if not command.args:
        await message.answer(f"Текущая модель: {await get_model(message.chat.id)}\nИспользование: /model <id>")
        return
    user_model[message.chat.id] = command.args.strip()
    await message.answer(f"Модель переключена на {command.args.strip()}")


@dp.message(Command("image"))
async def cmd_image(message: Message, command: CommandObject):
    if not allowed(message):
        return
    if not command.args:
        await message.answer("Использование: /image кот в скафандре на Луне")
        return
    await message.bot.send_chat_action(message.chat.id, "upload_photo")
    try:
        data = await make_image(command.args)
        await message.answer_photo(BufferedInputFile(data, "image.png"), caption=command.args[:1000])
    except Exception as e:
        log.exception("Ошибка /image")
        await message.answer(f"Не получилось сгенерировать картинку: {e}")


# ---------- сообщения ----------

@dp.message(F.photo)
async def on_photo(message: Message):
    if not allowed(message):
        return
    file = await message.bot.download(message.photo[-1])
    b64 = base64.b64encode(file.read()).decode()
    content = [
        {"type": "text", "text": message.caption or "Что на этом изображении?"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    await safe_ask(message, content)


@dp.message(F.document)
async def on_document(message: Message):
    if not allowed(message):
        return
    doc = message.document
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in TEXT_EXT or (doc.file_size or 0) > 1_000_000:
        await message.answer("Пока умею читать только текстовые файлы до 1 МБ (txt, md, py, csv, json и т.п.).")
        return
    file = await message.bot.download(doc)
    text = file.read().decode("utf-8", errors="replace")
    prompt = (message.caption or "Разбери этот файл.") + f"\n\nФайл {doc.file_name}:\n```\n{text}\n```"
    await safe_ask(message, prompt)


@dp.message(F.text)
async def on_text(message: Message):
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    await safe_ask(message, message.text)


async def safe_ask(message: Message, content) -> None:
    try:
        await ask_ai(message, content)
    except Exception as e:
        log.exception("Ошибка запроса к ИИ")
        await message.answer(f"Ошибка при обращении к ИИ: {e}")


async def main():
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    log.info("Бот запущен. API: %s", API_BASE_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

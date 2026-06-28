"""
Telegram webhook handler для Vercel (serverless).
Каждый апдейт от Telegram — отдельный HTTP POST на /api/webhook.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart

log = logging.getLogger(__name__)

# ─────────────── конфиг ───────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
# Супер-админ: полный доступ (удаление, управление менеджерами и т.д.)
SUPER_ADMIN_IDS = [int(x) for x in os.getenv("SUPER_ADMIN_IDS", "").split(",") if x.strip()]
# Менеджер: рассылки + статистика, без управления пользователями
MANAGER_IDS     = [int(x) for x in os.getenv("MANAGER_IDS", "").split(",") if x.strip()]
# Все с правами на рассылку/статистику
ADMIN_IDS       = SUPER_ADMIN_IDS + MANAGER_IDS
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
TEST_URL        = os.getenv("TEST_URL", "https://tochka-a.vercel.app")
USERS_TABLE     = os.getenv("USERS_TABLE", "bot_users")


# ─────────────── Supabase ───────────────
class Supabase:
    def __init__(self):
        self.base = SUPABASE_URL.rstrip("/")
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    async def upsert(self, table: str, data: dict):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.base}/rest/v1/{table}",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=data, timeout=8,
            )
            r.raise_for_status()

    async def update(self, table: str, match: dict, data: dict):
        params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
        async with httpx.AsyncClient() as c:
            r = await c.patch(
                f"{self.base}/rest/v1/{table}?{params}",
                headers={**self.headers, "Prefer": "return=minimal"},
                json=data, timeout=8,
            )
            r.raise_for_status()

    async def select(self, table: str, filters: dict | None = None, cols: str = "*") -> list[dict]:
        params = f"select={cols}"
        if filters:
            params += "&" + "&".join(f"{k}=eq.{v}" for k, v in filters.items())
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.base}/rest/v1/{table}?{params}",
                headers=self.headers, timeout=8,
            )
            r.raise_for_status()
            return r.json()

db = Supabase()


# ─────────────── утилиты ───────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def make_test_url(tg_id: int, tg_user: str, tg_name: str) -> str:
    return (
        f"{TEST_URL}"
        f"?tg_id={tg_id}"
        f"&tg_user={tg_user or ''}"
        f"&tg_name={tg_name or ''}"
        f"&ref=bot"
    )

def test_keyboard(url: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📊 Пройти тест →", url=url)
    ]])


# ─────────────── хэндлеры ───────────────
async def cmd_start(message: types.Message):
    user    = message.from_user
    tg_id   = user.id
    tg_user = user.username or ""
    tg_name = (user.full_name or "").strip()
    args    = message.text.split(maxsplit=1)[1] if " " in (message.text or "") else ""

    await db.upsert(USERS_TABLE, {
        "tg_id":         str(tg_id),
        "tg_user":       tg_user,
        "tg_name":       tg_name,
        "first_seen":    now_iso(),
        "is_subscribed": True,
    })

    if args.startswith("passed_") and args.replace("passed_", "", 1) == str(tg_id):
        await db.update(USERS_TABLE, {"tg_id": str(tg_id)}, {"test_passed_at": now_iso()})
        await message.answer(
            "🎉 *Тест пройден!*\n\n"
            "Я получил твои результаты. Скоро напишу тебе — разберём твою точку А "
            "и какая одна зона прямо сейчас тормозит твой доход.",
            parse_mode="Markdown",
        )
        return

    url = make_test_url(tg_id, tg_user, tg_name)
    await message.answer(
        f"Привет, {tg_name or 'друг'}! 👋\n\n"
        "Пройди бесплатный тест «Точка А» — за 4 минуты узнаешь, "
        "какая одна зона сейчас тормозит твой доход. "
        "И заберёшь персональный разбор в подарок.\n\n"
        "👇 Нажми кнопку, чтобы начать:",
        reply_markup=test_keyboard(url),
    )
    await db.update(USERS_TABLE, {"tg_id": str(tg_id)}, {"test_opened_at": now_iso()})


async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users  = await db.select(USERS_TABLE)
    total  = len(users)
    opened = sum(1 for u in users if u.get("test_opened_at"))
    passed = sum(1 for u in users if u.get("test_passed_at"))
    subbed = sum(1 for u in users if u.get("is_subscribed"))
    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"Всего юзеров: {total}\n"
        f"Открыли тест: {opened}\n"
        f"Прошли тест: {passed}\n"
        f"Подписаны: {subbed}",
        parse_mode="Markdown",
    )


async def cmd_broadcast(message: types.Message):
    """
    /broadcast all Текст    — всем подписанным
    /broadcast passed Текст — только прошедшим тест
    """
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат:\n"
            "`/broadcast all Привет всем!`\n"
            "`/broadcast passed Привет тем, кто прошёл тест!`",
            parse_mode="Markdown",
        )
        return

    mode, text = parts[1].lower(), parts[2]
    users = await db.select(USERS_TABLE, cols="tg_id,test_passed_at,is_subscribed")

    if mode == "passed":
        targets = [u for u in users if u.get("test_passed_at") and u.get("is_subscribed")]
    else:
        targets = [u for u in users if u.get("is_subscribed")]

    if not targets:
        await message.answer("Нет пользователей для рассылки.")
        return

    # На Vercel free — лимит ~10с, поэтому рассылаем пакетами
    # Для большой базы (500+) используй /broadcast_local на своём компьютере
    bot = message.bot
    ok = fail = 0
    for u in targets:
        try:
            await bot.send_message(int(u["tg_id"]), text, parse_mode="Markdown")
            ok += 1
        except Exception as e:
            if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                await db.update(USERS_TABLE, {"tg_id": u["tg_id"]}, {"is_subscribed": False})
            fail += 1
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Рассылка завершена.\nДоставлено: {ok} | Ошибки: {fail}")


async def cmd_unsubscribe(message: types.Message):
    await db.update(USERS_TABLE, {"tg_id": str(message.from_user.id)}, {"is_subscribed": False})
    await message.answer("Ты отписан от рассылки. Чтобы вернуться — /start")


# ─────────────── диспетчер ───────────────
def make_dispatcher() -> Dispatcher:
    d = Dispatcher()
    d.message.register(cmd_start,       CommandStart())
    d.message.register(cmd_stats,       Command("stats"))
    d.message.register(cmd_broadcast,   Command("broadcast"))
    d.message.register(cmd_unsubscribe, Command("unsubscribe"))
    return d


# ─────────────── обработка апдейта ───────────────
async def process(body: dict):
    bot = Bot(token=BOT_TOKEN)
    dp  = make_dispatcher()
    update = types.Update.model_validate(body)
    await dp.feed_update(bot, update)
    await bot.session.close()


# ─────────────── Vercel handler ───────────────
class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # заглушить стандартные логи Vercel

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        try:
            asyncio.run(process(body))
        except Exception as e:
            log.error("update error: %s", e)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot webhook is active")

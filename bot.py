"""
Telegram-бот «Точка А» — трекинг прохождения теста + рассылка.

Запуск:
  pip install -r requirements.txt
  cp .env.example .env   # заполнить переменные
  python bot.py
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────── конфиг ───────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPABASE_URL   = os.environ["SUPABASE_URL"]      # https://xxx.supabase.co
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]      # service_role ключ (не anon!)
TEST_URL       = os.getenv("TEST_URL", "https://tochka-a.vercel.app")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")   # без @, нужен для deeplink
USERS_TABLE    = os.getenv("USERS_TABLE", "bot_users")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ─────────────── Supabase helper ───────────────
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
                json=data,
                timeout=10,
            )
            r.raise_for_status()

    async def update(self, table: str, match: dict, data: dict):
        """PATCH строки, подходящие под match."""
        params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
        async with httpx.AsyncClient() as c:
            r = await c.patch(
                f"{self.base}/rest/v1/{table}?{params}",
                headers={**self.headers, "Prefer": "return=minimal"},
                json=data,
                timeout=10,
            )
            r.raise_for_status()

    async def select(self, table: str, filters: dict | None = None, cols: str = "*") -> list[dict]:
        params = f"select={cols}"
        if filters:
            params += "&" + "&".join(f"{k}=eq.{v}" for k, v in filters.items())
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.base}/rest/v1/{table}?{params}",
                headers=self.headers,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()

db = Supabase()


# ─────────────── утилиты ───────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def make_test_url(tg_id: int, tg_user: str, tg_name: str) -> str:
    user = tg_user or ""
    name = tg_name or ""
    return (
        f"{TEST_URL}"
        f"?tg_id={tg_id}"
        f"&tg_user={user}"
        f"&tg_name={name}"
        f"&ref=bot"
    )

def test_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Пройти тест →", url=url)
    ]])


# ─────────────── /start ───────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user    = message.from_user
    tg_id   = user.id
    tg_user = user.username or ""
    tg_name = (user.full_name or "").strip()
    args    = message.text.split(maxsplit=1)[1] if " " in (message.text or "") else ""

    # 1. Сохранить/обновить юзера
    await db.upsert(USERS_TABLE, {
        "tg_id":      str(tg_id),
        "tg_user":    tg_user,
        "tg_name":    tg_name,
        "first_seen": now_iso(),
        "is_subscribed": True,
    })

    # 2. Если вернулся из теста с payload "passed_<tg_id>"
    if args.startswith("passed_"):
        passed_id = args.replace("passed_", "", 1)
        if passed_id == str(tg_id):
            await db.update(USERS_TABLE, {"tg_id": str(tg_id)}, {
                "test_passed_at": now_iso()
            })
            await message.answer(
                "🎉 *Тест пройден!*\n\n"
                "Я получил твои результаты. Скоро напишу тебе — разберём твою точку А "
                "и какая одна зона прямо сейчас тормозит твой доход.",
                parse_mode="Markdown",
            )
            return

    # 3. Обычный /start — показать кнопку
    url = make_test_url(tg_id, tg_user, tg_name)
    await message.answer(
        f"Привет, {tg_name or 'друг'}! 👋\n\n"
        "Пройди бесплатный тест «Точка А» — за 4 минуты узнаешь, "
        "какая одна зона сейчас тормозит твой доход. "
        "И заберёшь персональный разбор в подарок.\n\n"
        "👇 Нажми кнопку, чтобы начать:",
        reply_markup=test_keyboard(url),
    )

    # Отметить, что пользователь перешёл к тесту
    await db.update(USERS_TABLE, {"tg_id": str(tg_id)}, {
        "test_opened_at": now_iso()
    })


# ─────────────── /broadcast ───────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Формат: /broadcast [all|passed] Текст сообщения
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат: /broadcast [all|passed] Текст\n\n"
            "`/broadcast all Привет всем!`\n"
            "`/broadcast passed Привет тем, кто прошёл тест!`",
            parse_mode="Markdown",
        )
        return

    mode, text = parts[1].lower(), parts[2]

    if mode == "passed":
        # Только те, кто завершил тест
        users = await db.select(
            USERS_TABLE,
            cols="tg_id",
        )
        users = [u for u in users if u.get("test_passed_at") and u.get("is_subscribed")]
    else:
        # Все подписанные
        users = await db.select(USERS_TABLE, filters={"is_subscribed": "true"}, cols="tg_id")

    if not users:
        await message.answer("Нет пользователей для рассылки.")
        return

    status = await message.answer(f"Рассылка: 0 / {len(users)}...")
    ok = fail = 0

    for i, u in enumerate(users, 1):
        try:
            await bot.send_message(int(u["tg_id"]), text, parse_mode="Markdown")
            ok += 1
        except Exception as e:
            log.warning("Не смог отправить %s: %s", u["tg_id"], e)
            # Если бот заблокирован — снять подписку
            if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                await db.update(USERS_TABLE, {"tg_id": u["tg_id"]}, {"is_subscribed": False})
            fail += 1

        # Прогресс каждые 20 сообщений
        if i % 20 == 0:
            try:
                await status.edit_text(f"Рассылка: {i} / {len(users)}...")
            except Exception:
                pass

        await asyncio.sleep(0.05)  # 20 msg/s — лимит Telegram

    await status.edit_text(
        f"✅ Рассылка завершена.\n"
        f"Доставлено: {ok} | Ошибки: {fail}"
    )


# ─────────────── /stats ───────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
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


# ─────────────── /unsubscribe ───────────────
@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    await db.update(USERS_TABLE, {"tg_id": str(message.from_user.id)}, {"is_subscribed": False})
    await message.answer("Ты отписан от рассылки. Чтобы вернуться — /start")


# ─────────────── запуск ───────────────
async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())

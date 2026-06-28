"""
POST /api/lead — принимает результаты теста от квиза,
сохраняет в Supabase и шлёт уведомление с результатами в группу.
"""

import json
import os
from http.server import BaseHTTPRequestHandler

import httpx

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
BOT_TOKEN       = os.environ["BOT_TOKEN"]
NOTIFY_GROUP_ID = os.getenv("NOTIFY_GROUP_ID", "")
SUPER_ADMIN_IDS = [x for x in os.getenv("SUPER_ADMIN_IDS", "").split(",") if x.strip()]
MANAGER_IDS     = [x for x in os.getenv("MANAGER_IDS", "").split(",") if x.strip()]
LEADS_TABLE     = "leads_tochka_a"

SPHERE_NAMES = {
    "money":   "💰 Деньги",
    "skill":   "🎯 Навык",
    "clients": "🧲 Клиенты",
    "self":    "🪞 Я",
    "circle":  "👥 Окружение",
    "habits":  "🔄 Привычки",
    "vibe":    "🌴 Кайф от жизни",
}

INTENT_LABELS = {
    "hot":  "🔥 Хочет начать прямо сейчас",
    "warm": "⚡️ Скоро, если увидит результат",
    "cool": "👀 Присматривается",
    "cold": "💤 Просто интересно",
}

ROUTE_LABELS = {
    "newbie":     "🟢 С нуля в онлайн",
    "freelancer": "🔵 Уже делает, хочет клиентов",
    "producer":   "🟣 Хочет строить/продюсировать",
}


def save_lead(lead: dict):
    with httpx.Client() as c:
        r = c.post(
            f"{SUPABASE_URL}/rest/v1/{LEADS_TABLE}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=lead,
            timeout=8,
        )
        r.raise_for_status()


def send_telegram(chat_id: str, text: str):
    with httpx.Client() as c:
        c.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=8,
        )


def build_notification(lead: dict) -> str:
    name      = lead.get("name") or "—"
    contact   = lead.get("contact") or "—"
    tg_id     = lead.get("tg_id") or ""
    tg_user   = lead.get("tg_user") or ""
    avg       = lead.get("avg") or 0
    weakest   = lead.get("weakest") or "—"
    intent    = lead.get("intent") or ""
    route     = lead.get("route") or ""
    scores    = lead.get("scores") or {}

    contact_line = f"@{tg_user}" if tg_user else (f"[написать](tg://user?id={tg_id})" if tg_id else contact)

    # Баллы по сферам — от худшего к лучшему
    sorted_scores = sorted(scores.items(), key=lambda x: x[1]) if scores else []
    scores_lines = "\n".join(
        f"  {SPHERE_NAMES.get(k, k)}: *{v}/10*"
        for k, v in sorted_scores
    )

    lines = [
        "📋 *Новый результат теста!*\n",
        f"👤 Имя: {name}",
        f"📲 Контакт: {contact_line}",
    ]
    if tg_id:
        lines.append(f"🆔 ID: `{tg_id}`")

    lines.append(f"\n📊 Средний балл: *{avg}/10*")
    lines.append(f"🔴 Точка роста: *{SPHERE_NAMES.get(weakest, weakest)}*")

    if scores_lines:
        lines.append(f"\n*Баллы по сферам:*\n{scores_lines}")

    if intent:
        lines.append(f"\n🎯 Намерение: {INTENT_LABELS.get(intent, intent)}")
    if route:
        lines.append(f"🧭 Маршрут: {ROUTE_LABELS.get(route, route)}")

    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        # CORS
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            length = int(self.headers.get("Content-Length", 0))
            lead   = json.loads(self.rfile.read(length) or b"{}")

            # Сохранить в Supabase
            save_lead(lead)

            # Уведомление с результатами
            text    = build_notification(lead)
            targets = ([NOTIFY_GROUP_ID] if NOTIFY_GROUP_ID else SUPER_ADMIN_IDS + MANAGER_IDS)
            for chat_id in targets:
                send_telegram(chat_id, text)

            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

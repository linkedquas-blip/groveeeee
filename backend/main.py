"""
Luminis — облегчённый backend без Rust/C-зависимостей (для Termux/телефона).
Flask (без pydantic v2) + pyTelegramBotAPI (без aiohttp/pydantic).
Тот же функционал, что и в версии на FastAPI+aiogram, просто легче собирается.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from urllib.parse import parse_qsl

import telebot
from telebot import types
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "game.db")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ---- Настройки баланса игры ----
GROW_SECONDS = 60
HARVEST_REWARD = 25
START_BALANCE = 0

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            currency INTEGER NOT NULL DEFAULT 0,
            plot_state TEXT NOT NULL DEFAULT 'empty',
            planted_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(telegram_id, username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (telegram_id, username, currency, plot_state) VALUES (?, ?, ?, 'empty')",
            (telegram_id, username, START_BALANCE),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row)


def update_user(telegram_id, **fields):
    conn = get_db()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE users SET {cols} WHERE telegram_id = ?", (*fields.values(), telegram_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Проверка initData от Telegram Web App (защита от подделки user_id)
# ---------------------------------------------------------------------------
def validate_init_data(init_data, bot_token):
    if not init_data:
        return None, ("Нет initData", 401)
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None, ("Нет hash в initData", 401)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if calculated_hash != received_hash:
        return None, ("Неверная подпись initData", 401)
    user_json = parsed.get("user")
    if not user_json:
        return None, ("Нет данных пользователя", 401)
    return json.loads(user_json), None


def extract_user_or_error(init_data):
    if not BOT_TOKEN:
        return None, ("BOT_TOKEN не настроен на сервере", 500)
    return validate_init_data(init_data, BOT_TOKEN)


# ---------------------------------------------------------------------------
# Игровая логика
# ---------------------------------------------------------------------------
def compute_state(user_row):
    state = user_row["plot_state"]
    remaining = 0
    if state == "growing":
        elapsed = time.time() - user_row["planted_at"]
        remaining = max(0, GROW_SECONDS - elapsed)
        if remaining <= 0:
            state = "ready"
            update_user(user_row["telegram_id"], plot_state="ready")
    return {
        "currency": user_row["currency"],
        "plot_state": state,
        "grow_seconds": GROW_SECONDS,
        "seconds_remaining": round(remaining),
    }


# ---------------------------------------------------------------------------
# API-эндпоинты (те же пути, что и раньше — фронтенд не меняется)
# ---------------------------------------------------------------------------
@app.get("/api/state")
def api_state():
    init_data = request.args.get("init_data", "")
    tg_user, err = extract_user_or_error(init_data)
    if err:
        return jsonify({"detail": err[0]}), err[1]
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return jsonify(compute_state(row))


@app.post("/api/plant")
def api_plant():
    body = request.get_json(force=True)
    tg_user, err = extract_user_or_error(body.get("init_data", ""))
    if err:
        return jsonify({"detail": err[0]}), err[1]
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    if row["plot_state"] != "empty":
        return jsonify({"detail": "Горшок уже занят"}), 400
    update_user(row["telegram_id"], plot_state="growing", planted_at=time.time())
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return jsonify(compute_state(row))


@app.post("/api/harvest")
def api_harvest():
    body = request.get_json(force=True)
    tg_user, err = extract_user_or_error(body.get("init_data", ""))
    if err:
        return jsonify({"detail": err[0]}), err[1]
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    state = compute_state(row)
    if state["plot_state"] != "ready":
        return jsonify({"detail": "Урожай ещё не готов"}), 400
    new_balance = row["currency"] + HARVEST_REWARD
    update_user(row["telegram_id"], plot_state="empty", currency=new_balance, planted_at=None)
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return jsonify(compute_state(row))


# ---------------------------------------------------------------------------
# Telegram-бот (pyTelegramBotAPI), крутится в отдельном потоке
# ---------------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

if bot:
    @bot.message_handler(commands=["start"])
    def handle_start(message):
        if not WEBAPP_URL:
            bot.send_message(message.chat.id, "WEBAPP_URL ещё не настроен на сервере.")
            return
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                text="🌿 Открыть Luminis",
                web_app=types.WebAppInfo(url=WEBAPP_URL),
            )
        )
        bot.send_message(
            message.chat.id,
            "Добро пожаловать в подпольную теплицу Luminis 🌱\n"
            "Выращивай, собирай, продавай — и не попадись.",
            reply_markup=markup,
        )

    def run_bot_polling():
        bot.infinity_polling(skip_pending=True)

    threading.Thread(target=run_bot_polling, daemon=True).start()
else:
    print("BOT_TOKEN не задан — бот не запущен (только API/фронтенд).")


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

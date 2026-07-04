"""
Luminis — MVP бэкенда телеграм веб-игры.

Что тут есть:
- FastAPI сервер, который отдаёт фронтенд (frontend/index.html) и API
- SQLite база (файл game.db), создаётся автоматически при первом запуске
- aiogram-бот, который запускается в фоне вместе с сервером и присылает
  кнопку "Открыть игру" в чат

Как запустить локально:
    pip install -r requirements.txt
    cp .env.example .env      # и вписать туда свой BOT_TOKEN и WEBAPP_URL
    uvicorn main:app --reload

Подробности деплоя — в README.md в корне проекта.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "game.db")

# ---- Настройки баланса игры (крути эти цифры, чтобы менять сложность) ----
GROW_SECONDS = 60          # сколько секунд растёт куст (для теста — 1 минута)
HARVEST_REWARD = 25        # сколько "Спор" даёт один урожай
START_BALANCE = 0


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
            plot_state TEXT NOT NULL DEFAULT 'empty',  -- empty | growing | ready
            planted_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(telegram_id: int, username: str | None):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (telegram_id, username, currency, plot_state) VALUES (?, ?, ?, 'empty')",
            (telegram_id, username, START_BALANCE),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    conn.close()
    return dict(row)


def update_user(telegram_id: int, **fields):
    conn = get_db()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE users SET {cols} WHERE telegram_id = ?",
        (*fields.values(), telegram_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Проверка данных, присланных Telegram Web App (initData)
# Это защищает от подделки user_id с клиента.
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
# ---------------------------------------------------------------------------
def validate_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Нет initData")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Нет hash в initData")

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if calculated_hash != received_hash:
        raise HTTPException(status_code=401, detail="Неверная подпись initData")

    user_json = parsed.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="Нет данных пользователя")

    return json.loads(user_json)


def extract_user(init_data: str) -> dict:
    # На локальной разработке без токена/HTTPS можно временно упростить проверку —
    # но для прода оставляй validate_init_data включённой.
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен на сервере")
    return validate_init_data(init_data, BOT_TOKEN)


# ---------------------------------------------------------------------------
# Игровая логика
# ---------------------------------------------------------------------------
def compute_state(user_row: dict) -> dict:
    """Пересчитывает, не созрел ли куст, и возвращает состояние для фронта."""
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
# aiogram-бот
# ---------------------------------------------------------------------------
bot: Bot | None = None
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not WEBAPP_URL:
        await message.answer(
            "Игра почти готова, но WEBAPP_URL ещё не настроен на сервере."
        )
        return
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🌿 Открыть Luminis",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )
    await message.answer(
        "Добро пожаловать в подпольную теплицу Luminis 🌱\n"
        "Выращивай, собирай, продавай — и не попадись.",
        reply_markup=keyboard,
    )


async def run_bot():
    global bot
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан — бот не запущен (только API/фронтенд).")
        return
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(run_bot())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/state")
async def api_state(init_data: str):
    tg_user = extract_user(init_data)
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return compute_state(row)


@app.post("/api/plant")
async def api_plant(request: Request):
    body = await request.json()
    tg_user = extract_user(body.get("init_data", ""))
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    if row["plot_state"] != "empty":
        raise HTTPException(status_code=400, detail="Горшок уже занят")
    update_user(row["telegram_id"], plot_state="growing", planted_at=time.time())
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return compute_state(row)


@app.post("/api/harvest")
async def api_harvest(request: Request):
    body = await request.json()
    tg_user = extract_user(body.get("init_data", ""))
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    state = compute_state(row)  # пересчитает, если готово
    if state["plot_state"] != "ready":
        raise HTTPException(status_code=400, detail="Урожай ещё не готов")
    new_balance = row["currency"] + HARVEST_REWARD
    update_user(row["telegram_id"], plot_state="empty", currency=new_balance, planted_at=None)
    row = get_or_create_user(tg_user["id"], tg_user.get("username"))
    return compute_state(row)


# Отдаём фронтенд как статику (index.html и всё, что рядом с ним)
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

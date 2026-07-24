import re
import logging
import os
import asyncio
from typing import Dict, Optional, List

import aiosqlite
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler
)

load_dotenv()

# ========================= КОНФИГ И НАСТРОЙКИ =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SOURCE_CHAT_ID = -1003469691743
CHECK_RANGE = 3  # Основная игра + 3 догона
MAX_GAMES_PER_DAY = 1440

# Смещение для быстрых игр (по умолчанию +2)
SUIT_OFFSET = int(os.environ.get("SUIT_OFFSET", 2))

# Контакты владельца
OWNER_NAME = "Abramovich"
OWNER_USERNAME = "@Ol1garxxxx , https://t.me/creativebaccarat "  # Укажите ваш контактный Telegram username

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_database.db")
LOG_FILE = os.path.join(DATA_DIR, "pro_predictor.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ========================= МАТРИЦА МАСТЕЙ =========================
SUIT_MATRIX = {
    ('♣', '♦'): '♠', ('♣', '♠'): '♣', ('♣', '♣'): '♠', ('♥', '♠'): '♥',
    ('♥', '♣'): '♠', ('♥', '♦'): '♦', ('♥', '♥'): '♦', ('♣', '♥'): '♠',
    ('♠', '♥'): '♦', ('♠', '♣'): '♦', ('♠', '♦'): '♥', ('♠', '♠'): '♥',
    ('♦', '♠'): '♠', ('♦', '♣'): '♣', ('♦', '♥'): '♦', ('♦', '♦'): '♥'
}

game_history: List[Dict] = []

# ========================= БАЗА ДАННЫХ =========================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                selected_mode TEXT DEFAULT 'off',
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                mode TEXT PRIMARY KEY,
                success INTEGER DEFAULT 0,
                fail INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_predictions (
                user_id INTEGER PRIMARY KEY,
                target_raw INTEGER,
                mode TEXT,
                title TEXT,
                target_suit TEXT,
                msg_id INTEGER
            )
        """)
        await db.commit()

async def get_user(user_id: int) -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            
            await db.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            await db.commit()
            
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor2:
                return dict(await cursor2.fetchone())

async def update_user(user_id: int, **kwargs):
    fields = ", ".join([f"{k} = ?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)
        await db.commit()

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM stats") as cursor:
            rows = await cursor.fetchall()
            return {r['mode']: {'success': r['success'], 'fail': r['fail']} for r in rows}

async def update_stat(mode: str, is_success: bool):
    field = "success" if is_success else "fail"
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"""
            INSERT INTO stats (mode, {field}) VALUES (?, 1)
            ON CONFLICT(mode) DO UPDATE SET {field} = {field} + 1
        """, (mode,))
        await db.commit()

# ========================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========================
def get_target_game_id(current_id: int, offset: int = 0) -> int:
    res = (current_id + offset) % MAX_GAMES_PER_DAY
    return MAX_GAMES_PER_DAY if res == 0 else res

def extract_ranks_and_suits(cards_str: str):
    cleaned = re.sub(r'[🔰✅🟩]', '', cards_str)
    matches = re.findall(r'([A-Z\d]+)\s*([♣♦♥♠])', cleaned)
    return [m[0] for m in matches], [m[1] for m in matches]

def parse_game(text: str) -> Optional[Dict]:
    if not text or '#N' not in text:
        return None

    text_clean = re.sub(r'^\[\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\]\s*[^:]+:\s*', '', text)
    pattern = r'#N(\d+)\.\s*(?:✅|🔰)?\s*(\d+)\s*\(([^)]+)\)\s*(?:✅|🔰)?\s*(\d+)\s*\(([^)]+)\)'
    m = re.search(pattern, text_clean)
    
    if m:
        raw_id = int(m.group(1))
        p_score, p_str = int(m.group(2)), m.group(3)
        b_score, b_str = int(m.group(4)), m.group(5)

        p_ranks, p_suits = extract_ranks_and_suits(p_str)
        b_ranks, b_suits = extract_ranks_and_suits(b_str)

        return {
            "raw_id": raw_id,
            "player_score": p_score,
            "banker_score": b_score,
            "player_ranks": p_ranks,
            "player_suits": p_suits,
            "banker_ranks": b_ranks,
            "banker_suits": b_suits,
            "player_count": len(p_ranks),
            "banker_count": len(b_ranks)
        }
    return None

def get_last_two_suits(suits_list: List[str]) -> Optional[tuple]:
    if len(suits_list) >= 3:
        return (suits_list[1], suits_list[2])
    elif len(suits_list) == 2:
        return (suits_list[0], suits_list[1])
    return None

# ========================= КЛАВИАТУРЫ =========================
def main_menu(current_mode: str = "off"):
    def mark(mode_name):
        return " ⚡️ [АКТИВЕН]" if current_mode == mode_name else ""

    keyboard = [
        [InlineKeyboardButton(f"🎴 Масть Игрока{mark('suit_p')}", callback_data="select_suit_p")],
        [InlineKeyboardButton(f"🎴 Масть Банкира{mark('suit_b')}", callback_data="select_suit_b")],
        [InlineKeyboardButton("🛑 Стоп Сигналы", callback_data="stop_mode")],
        [
            InlineKeyboardButton("📈 Статистика", callback_data="stats"),
            InlineKeyboardButton("👑 Владелец", callback_data="owner")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========================= ЛОГИКА СИГНАЛОВ И ОФОРМЛЕНИЕ =========================
async def handle_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.edited_channel_post or update.message or update.edited_message
    if not msg or msg.chat.id != SOURCE_CHAT_ID or not msg.text:
        return

    game = parse_game(msg.text)
    if not game:
        return

    raw_id = game["raw_id"]
    if game_history and game_history[-1]["raw_id"] == raw_id:
        return

    # === 1. ПРОВЕРКА АКТИВНЫХ СИГНАЛОВ И ОБНОВЛЕНИЕ СТАТУСА ===
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM active_predictions") as cursor:
            active_preds = [dict(r) for r in await cursor.fetchall()]

    for pred in active_preds:
        uid = pred["user_id"]
        target_raw = pred["target_raw"]
        offset = (raw_id - target_raw) % MAX_GAMES_PER_DAY

        if 0 <= offset < CHECK_RANGE:
            p_type = pred["mode"]
            target_suit = pred.get("target_suit")
            is_success = False

            if p_type == "suit_p" and target_suit in game["player_suits"]:
                is_success = True
            elif p_type == "suit_b" and target_suit in game["banker_suits"]:
                is_success = True

            step_str = "Основная игра" if offset == 0 else f"{offset}-й догон"

            if is_success:
                await update_stat(p_type, True)
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM active_predictions WHERE user_id = ?", (uid,))
                    await db.commit()

                # Вариант 2: Стильный моноширинный блок
                win_text = (
                    f"```text\n"
                    f"💎 #{target_raw} ➔ {pred['title']}\n"
                    f"✅ Зашел на #{raw_id} [{step_str} ⚡️]\n"
                    f"```"
                )

                try:
                    await context.bot.edit_message_text(
                        chat_id=uid, message_id=pred["msg_id"],
                        text=win_text, parse_mode='Markdown'
                    )
                except TelegramError: pass

            elif offset == CHECK_RANGE - 1:
                await update_stat(p_type, False)
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM active_predictions WHERE user_id = ?", (uid,))
                    await db.commit()

                loss_text = (
                    f"```text\n"
                    f"💥 #{target_raw} ➔ {pred['title']}\n"
                    f"❌ Незаход [Лимит догонов]\n"
                    f"```"
                )

                try:
                    await context.bot.edit_message_text(
                        chat_id=uid, message_id=pred["msg_id"],
                        text=loss_text, parse_mode='Markdown'
                    )
                except TelegramError: pass

    # === 2. ОБНОВЛЕНИЕ ИСТОРИИ ===
    game_history.append(game)
    if len(game_history) > 15: game_history.pop(0)

    # === 3. РАСЧЕТ И ВЫДАЧА НОВОГО СИГНАЛА ===
    b_last_suits = get_last_two_suits(game["banker_suits"])
    pred_suit_p = SUIT_MATRIX.get(b_last_suits) if b_last_suits else None

    p_last_suits = get_last_two_suits(game["player_suits"])
    pred_suit_b = SUIT_MATRIX.get(p_last_suits) if p_last_suits else None

    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE selected_mode != 'off' AND is_active = 1") as cursor:
            active_users = [dict(r) for r in await cursor.fetchall()]

    async def send_signal(user):
        uid = user["user_id"]
        mode = user["selected_mode"]

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT 1 FROM active_predictions WHERE user_id = ?", (uid,)) as cursor:
                if await cursor.fetchone(): return

        signal_matched = False
        title, target_suit = "", None
        target_raw = get_target_game_id(raw_id, SUIT_OFFSET)

        if mode == "suit_p" and pred_suit_p:
            signal_matched, target_suit = True, pred_suit_p
            title = f"{pred_suit_p} ИГРОК"
        elif mode == "suit_b" and pred_suit_b:
            signal_matched, target_suit = True, pred_suit_b
            title = f"{pred_suit_b} БАНКИР"

        if signal_matched:
            game_range_end = get_target_game_id(target_raw, CHECK_RANGE - 1)
            
            # Вариант 2: Выдача сигнала в аналогичном формате
            signal_text = (
                f"```text\n"
                f"⚡️ СИГНАЛ #{target_raw} ➔ {title}\n"
                f"⏳ Диапазон: #{target_raw} - #{game_range_end}\n"
                f"```"
            )

            try:
                sent_msg = await context.bot.send_message(uid, signal_text, parse_mode='Markdown')

                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("""
                        INSERT OR REPLACE INTO active_predictions (user_id, target_raw, mode, title, target_suit, msg_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (uid, target_raw, mode, title, target_suit, sent_msg.message_id))
                    await db.commit()

            except TelegramError as e:
                if "Forbidden" in str(e):
                    await update_user(uid, selected_mode="off", is_active=0)

    if active_users:
        await asyncio.gather(*(send_signal(u) for u in active_users), return_exceptions=True)

# ========================= КОМАНДЫ И ОБРАБОТКА МЕНЮ =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = await get_user(user_id)

    welcome_text = (
        f"👑 **VIP PREDICTOR BOT**\n"
        f"───────────────\n"
        f"Бот автоматически анализирует математические матрицы мастей "
        f"и выдает сигналы в реальном времени.\n\n"
        f"👤 **Владелец:** {OWNER_NAME}\n"
        f"👇 **Выберите алгоритм работы:**"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=main_menu(user["selected_mode"]),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = await get_user(user_id)

    try:
        if query.data == "stop_mode":
            await update_user(user_id, selected_mode="off")
            await query.edit_message_text(
                "🛑 **Выдача сигналов остановлена.**\nВыберите нужный режим ниже для повторного запуска.",
                reply_markup=main_menu("off"), parse_mode='Markdown'
            )

        elif query.data in ["select_suit_p", "select_suit_b"]:
            mode_map = {
                "select_suit_p": ("suit_p", "Масть Игрока"),
                "select_suit_b": ("suit_b", "Масть Банкира")
            }
            mode_code, mode_title = mode_map[query.data]

            await update_user(user_id, selected_mode=mode_code)
            
            text = (
                f"⚡️ **АВТО-РЕЖИМ ЗАПУЩЕН**\n"
                f"───────────────\n"
                f"🎯 **Выбран алгоритм:** `{mode_title}`\n"
                f"📡 Ожидайте сигналы..."
            )
            await query.edit_message_text(text, reply_markup=main_menu(mode_code), parse_mode='Markdown')

        elif query.data == "stats":
            stats = await get_stats()
            
            titles = {
                "suit_p": "🎴 Масть Игрока", 
                "suit_b": "🎴 Масть Банкира"
            }
            
            stat_lines = []
            for k, name in titles.items():
                st = stats.get(k, {"success": 0, "fail": 0})
                tot = st["success"] + st["fail"]
                rate = (st["success"] / tot * 100) if tot > 0 else 0
                stat_lines.append(f"▪️ **{name}:** `{st['success']}/{tot}` — `{rate:.1f}%` Winrate")

            stats_text = (
                f"📈 **ОФИЦИАЛЬНАЯ СТАТИСТИКА**\n"
                f"───────────────\n" +
                "\n".join(stat_lines) +
                f"\n───────────────\n"
                f"👑 *Статистика обновляется автоматически*"
            )

            await query.edit_message_text(stats_text, parse_mode='Markdown', reply_markup=main_menu(user["selected_mode"]))

        elif query.data == "owner":
            owner_text = (
                f"👑 **ВЛАДЕЛЕЦ И РАЗРАБОТЧИК**\n"
                f"───────────────\n"
                f"👤 **Имя:** `{OWNER_NAME}`\n"
                f"💎 **Статус:** `Автор алгоритма`\n\n"
                f"💬 **Контакты для связи:**\n"
                f"По всем вопросам обращаться напрямую к владельцу:\n"
                f"👉 {OWNER_USERNAME}\n"
                f"───────────────"
            )
            await query.edit_message_text(owner_text, parse_mode='Markdown', reply_markup=main_menu(user["selected_mode"]))

    except TelegramError as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Ошибка меню: {e}")

# ========================= ТОЧКА ВХОДА =========================
def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден!")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler((filters.Chat(SOURCE_CHAT_ID) & filters.TEXT) | filters.UpdateType.EDITED_CHANNEL_POST, handle_game))

    logger.info("🚀 VIP Predictor Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

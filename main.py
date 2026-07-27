import re
import logging
import os
import asyncio
from typing import Dict, Optional, List

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
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", -1003469691743))
CHECK_RANGE = 4  # 0 (основная), 1, 2, 3 (до 3-го догона включительно)
MAX_GAMES_PER_DAY = 1440
SUIT_OFFSET = int(os.environ.get("SUIT_OFFSET", 1))

# ID канала, куда дублировать прогноз, если предыдущий зашел на 3-м или 4-м шаге (offset 2 или 3)
DOGON_CHANNEL_ID = int(os.environ.get("DOGON_CHANNEL_ID", 0))

OWNER_NAME = "Abramovich"
OWNER_USERNAME = "@Ol1garxxxx, https://t.me/creativebaccarat"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========================= МАТРИЦА МАСТЕЙ =========================
SUIT_MATRIX = {
    ('♣', '♦'): '♠', ('♣', '♠'): '♣', ('♣', '♣'): '♠', ('♥', '♠'): '♥',
    ('♥', '♣'): '♠', ('♥', '♦'): '♦', ('♥', '♥'): '♦', ('♣', '♥'): '♠',
    ('♠', '♥'): '♦', ('♠', '♣'): '♦', ('♠', '♦'): '♥', ('♠', '♠'): '♥',
    ('♦', '♠'): '♠', ('♦', '♣'): '♣', ('♦', '♥'): '♦', ('♦', '♦'): '♥'
}

# ========================= ХРАНИЛИЩЕ В ПАМЯТИ =========================
USERS_DATA: Dict[int, Dict] = {}
ACTIVE_PREDICTIONS: Dict[int, Dict] = {}
STATS_DATA: Dict[str, Dict[str, int]] = {
    "suit_p": {"success": 0, "fail": 0},
    "suit_b": {"success": 0, "fail": 0}
}
game_history: List[Dict] = []

def get_or_create_user(user_id: int) -> dict:
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {
            "selected_mode": "off",
            "is_active": True,
            "last_was_dogon": False
        }
    return USERS_DATA[user_id]

def update_stat(mode: str, is_success: bool):
    if mode not in STATS_DATA:
        STATS_DATA[mode] = {"success": 0, "fail": 0}
    if is_success:
        STATS_DATA[mode]["success"] += 1
    else:
        STATS_DATA[mode]["fail"] += 1

STEP_EMOJIS = {
    0: "0️⃣",  # Основная игра
    1: "1️⃣",  # 1-й догон
    2: "2️⃣",  # 2-й догон
    3: "3️⃣"   # 3-й догон
}

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

def get_first_two_suits(suits_list: List[str]) -> Optional[tuple]:
    if len(suits_list) >= 2:
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
        [InlineKeyboardButton("📈 Статистика", callback_data="stats"),
         InlineKeyboardButton("👑 Владелец", callback_data="owner")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========================= ЛОГИКА СИГНАЛОВ =========================
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
    users_to_remove = []

    for uid, pred in list(ACTIVE_PREDICTIONS.items()):
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

            if is_success:
                update_stat(p_type, True)
                users_to_remove.append(uid)

                # Если зашло на 3-м или 4-м шаге (offset 2 или 3)
                if offset in (2, 3):
                    USERS_DATA[uid]["last_was_dogon"] = True

                step_emoji = STEP_EMOJIS.get(offset, "")
                result_text = f"✅ #{target_raw} ➔ {pred['title']}{step_emoji}"

                # Редактируем сообщение у пользователя
                try:
                    await context.bot.edit_message_text(chat_id=uid, message_id=pred["msg_id"], text=result_text)
                except TelegramError:
                    pass

                # Редактируем сообщение в канале (если оно было отправлено)
                if pred.get("channel_msg_id") and DOGON_CHANNEL_ID != 0:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=DOGON_CHANNEL_ID, 
                            message_id=pred["channel_msg_id"], 
                            text=result_text
                        )
                    except TelegramError:
                        pass

            elif offset == CHECK_RANGE - 1:
                update_stat(p_type, False)
                users_to_remove.append(uid)
                result_text = f"❌ #{target_raw} ➔ {pred['title']} "

                # Редактируем сообщение у пользователя
                try:
                    await context.bot.edit_message_text(chat_id=uid, message_id=pred["msg_id"], text=result_text)
                except TelegramError:
                    pass

                # Редактируем сообщение в канале (если оно было отправлено)
                if pred.get("channel_msg_id") and DOGON_CHANNEL_ID != 0:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=DOGON_CHANNEL_ID, 
                            message_id=pred["channel_msg_id"], 
                            text=result_text
                        )
                    except TelegramError:
                        pass

    for uid in users_to_remove:
        ACTIVE_PREDICTIONS.pop(uid, None)

    # === 2. ОБНОВЛЕНИЕ ИСТОРИИ ===
    game_history.append(game)
    if len(game_history) > 15: 
        game_history.pop(0)

    # === 3. РАСЧЕТ И ВЫДАЧА НОВОГО СИГНАЛА ===
    b_last_suits = get_last_two_suits(game["banker_suits"])
    pred_suit_p = SUIT_MATRIX.get(b_last_suits) if b_last_suits else None

    p_first_suits = get_first_two_suits(game["player_suits"])
    pred_suit_b = SUIT_MATRIX.get(p_first_suits) if p_first_suits else None

    active_users = [
        (uid, udata) for uid, udata in USERS_DATA.items() 
        if udata.get("selected_mode") != "off" and udata.get("is_active", True)
    ]

    async def send_signal(user_item):
        uid, user = user_item
        mode = user["selected_mode"]

        if uid in ACTIVE_PREDICTIONS:
            return

        signal_matched = False
        title, target_suit = "", None
        target_raw = get_target_game_id(raw_id, SUIT_OFFSET)

        if mode == "suit_p" and pred_suit_p:
            signal_matched, target_suit = True, pred_suit_p
            title = f"{pred_suit_p} Игрок"
        elif mode == "suit_b" and pred_suit_b:
            signal_matched, target_suit = True, pred_suit_b
            title = f"{pred_suit_b} Банкир"

        if signal_matched:
            # Формат строго: номер игры и масть
            signal_text = f"⚡️ #{target_raw} ➔ {title}"
            
            is_dogon_follow_up = user.get("last_was_dogon", False)
            user["last_was_dogon"] = False  # Сразу сбрасываем флаг

            try:
                # 1. Отправляем пользователю
                sent_msg = await context.bot.send_message(uid, signal_text)
                
                channel_msg_id = None
                # 2. Если предыдущий зашел с догоном, отправляем ТОЧНО ТАКОЙ ЖЕ текст в канал
                if is_dogon_follow_up and DOGON_CHANNEL_ID != 0:
                    channel_msg = await context.bot.send_message(
                        chat_id=DOGON_CHANNEL_ID, 
                        text=signal_text
                    )
                    channel_msg_id = channel_msg.message_id

                ACTIVE_PREDICTIONS[uid] = {
                    "target_raw": target_raw,
                    "mode": mode,
                    "title": title,
                    "target_suit": target_suit,
                    "msg_id": sent_msg.message_id,
                    "channel_msg_id": channel_msg_id  # Сохраняем ID для последующего редактирования
                }

            except TelegramError as e:
                if "Forbidden" in str(e):
                    USERS_DATA[uid]["selected_mode"] = "off"
                    USERS_DATA[uid]["is_active"] = False

    if active_users:
        await asyncio.gather(*(send_signal(u) for u in active_users), return_exceptions=True)

# ========================= КОМАНДЫ И ОБРАБОТКА МЕНЮ =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_or_create_user(user_id)
    welcome_text = (
        f"👑 **VIP PREDICTOR BOT**\n"
        f"───────────────\n"
        f"Бот автоматически анализирует математические матрицы мастей "
        f"и выдает сигналы в реальном времени.\n\n"
        f"👤 **Владелец:** {OWNER_NAME}\n"
        f"👇 **Выберите алгоритм работы:**"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu(user["selected_mode"]), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_or_create_user(user_id)

    try:
        if query.data == "stop_mode":
            user["selected_mode"] = "off"
            await query.edit_message_text("🛑 **Выдача сигналов остановлена.**\nВыберите нужный режим ниже для повторного запуска.", reply_markup=main_menu("off"), parse_mode='Markdown')

        elif query.data in ["select_suit_p", "select_suit_b"]:
            mode_map = {"select_suit_p": ("suit_p", "Масть Игрока"), "select_suit_b": ("suit_b", "Масть Банкира")}
            mode_code, mode_title = mode_map[query.data]
            user["selected_mode"] = mode_code
            text = f"⚡️ **АВТО-РЕЖИМ ЗАПУЩЕН**\n───────────────\n🎯 **Выбран алгоритм:** `{mode_title}`\n📡 Ожидайте сигналы..."
            await query.edit_message_text(text, reply_markup=main_menu(mode_code), parse_mode='Markdown')

        elif query.data == "stats":
            titles = {"suit_p": "🎴 Масть Игрока", "suit_b": "🎴 Масть Банкира"}
            stat_lines = []
            for k, name in titles.items():
                st = STATS_DATA.get(k, {"success": 0, "fail": 0})
                tot = st["success"] + st["fail"]
                rate = (st["success"] / tot * 100) if tot > 0 else 0
                stat_lines.append(f"▪️ **{name}:** `{st['success']}/{tot}` — `{rate:.1f}%` Winrate")
            stats_text = f"📈 **ОФИЦИАЛЬНАЯ СТАТИСТИКА**\n───────────────\n" + "\n".join(stat_lines) + f"\n───────────────\n👑 *Статистика обновляется автоматически*"
            await query.edit_message_text(stats_text, parse_mode='Markdown', reply_markup=main_menu(user["selected_mode"]))

        elif query.data == "owner":
            owner_text = f"👑 **ВЛАДЕЛЕЦ И РАЗРАБОТЧИК**\n───────────────\n👤 **Имя:** `{OWNER_NAME}`\n💎 **Статус:** `Автор алгоритма`\n\n💬 **Контакты для связи:**\nПо всем вопросам обращаться напрямую к владельцу:\n👉 {OWNER_USERNAME}\n───────────────"
            await query.edit_message_text(owner_text, parse_mode='Markdown', reply_markup=main_menu(user["selected_mode"]))

    except TelegramError as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Ошибка меню: {e}")

# ========================= ТОЧКА ВХОДА =========================
def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler((filters.Chat(SOURCE_CHAT_ID) & filters.TEXT) | filters.UpdateType.EDITED_CHANNEL_POST, handle_game))

    logger.info("🚀 VIP Predictor Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

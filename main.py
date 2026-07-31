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

DOGON_CHANNEL_ID = int(os.environ.get("DOGON_CHANNEL_ID", 0))
MIRROR_CHANNEL_ID = int(os.environ.get("MIRROR_CHANNEL_ID", 0))
SPECIAL_CHANNEL_ID = int(os.environ.get("SPECIAL_CHANNEL_ID", 0))  # <-- ДОБАВЛЕНО: ID 3-го канала для спец. сигналов

OWNER_NAME = "Abramovich"
OWNER_USERNAME = "@Ol1garxxxx, https://t.me/creativebaccarat"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========================= МАТРИЦЫ МАСТЕЙ =========================
SUIT_MATRIX = {
    ('♣', '♦'): '♠', ('♣', '♠'): '♣', ('♣', '♣'): '♠', ('♥', '♠'): '♥',
    ('♥', '♣'): '♠', ('♥', '♦'): '♦', ('♥', '♥'): '♦', ('♣', '♥'): '♠',
    ('♠', '♥'): '♦', ('♠', '♣'): '♦', ('♠', '♦'): '♥', ('♠', '♠'): '♥',
    ('♦', '♠'): '♠', ('♦', '♣'): '♣', ('♦', '♥'): '♦', ('♦', '♦'): '♥'
}

MIRROR_SUIT_MATRIX = {
    '♣': '♦',
    '♦': '♣',
    '♥': '♠',
    '♠': '♥'
}

# ========================= ХРАНИЛИЩЕ В ПАМЯТИ =========================
USERS_DATA: Dict[int, Dict] = {}
ACTIVE_PREDICTIONS: Dict[int, Dict] = {}
STATS_DATA: Dict[str, Dict[str, int]] = {
    "suit_p": {"success": 0, "fail": 0},
    "suit_b": {"success": 0, "fail": 0}
}
game_history: List[Dict] = []
consecutive_non_zero_wins = 0  # <-- ДОБАВЛЕНО: Счетчик подряд идущих побед не на 0 шаге

STEP_EMOJIS = {
    0: "0️⃣",
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣"
}

def get_or_create_user(user_id: int) -> dict:
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {
            "selected_mode": "off",
            "is_active": True,
            "last_was_dogon": False,
            "consecutive_zero_wins": 0
        }
    else:
        if "consecutive_zero_wins" not in USERS_DATA[user_id]:
            USERS_DATA[user_id]["consecutive_zero_wins"] = 0
        if "last_was_dogon" not in USERS_DATA[user_id]:
            USERS_DATA[user_id]["last_was_dogon"] = False
    return USERS_DATA[user_id]

def update_stat(mode: str, is_success: bool):
    if mode not in STATS_DATA:
        STATS_DATA[mode] = {"success": 0, "fail": 0}
    if is_success:
        STATS_DATA[mode]["success"] += 1
    else:
        STATS_DATA[mode]["fail"] += 1

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

def get_mirror_suit(suit: str) -> str:
    return MIRROR_SUIT_MATRIX.get(suit, suit)

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
    global consecutive_non_zero_wins  # <-- ДОБАВЛЕНО
    
    # Для счетчика берем только новые сообщения, чтобы избежать двойного срабатывания при редактировании
    new_msg = update.channel_post or update.message
    # Для парсинга игры берем также edited, если вдруг игра обновилась
    msg = update.channel_post or update.edited_channel_post or update.message or update.edited_message
    
    if not msg or msg.chat.id != SOURCE_CHAT_ID or not msg.text:
        return

    # === 0. ПРОВЕРКА ТРИГГЕРОВ ДЛЯ 3-го КАНАЛА (СЧИТАЕМ ПОДРЯД ИДУЩИЕ НЕ-НУЛЕВЫЕ ПОБЕДЫ) ===
    if new_msg:
        text = new_msg.text
        if "✅" in text and ("Игрок" in text or "Банкир" in text):
            if "0️⃣" in text:
                consecutive_non_zero_wins = 0
            elif "1️⃣" in text or "2️⃣" in text or "3️⃣" in text:
                consecutive_non_zero_wins += 1
        elif "❌" in text and ("Игрок" in text or "Банкир" in text):
            consecutive_non_zero_wins = 0

    game = parse_game(msg.text)
    if not game:
        return

    raw_id = game["raw_id"]
    if game_history and game_history[-1]["raw_id"] == raw_id:
        return

    # === 1. ПРОВЕРКА АКТИВНЫХ СИГНАЛОВ (РАЗДЕЛЬНАЯ ЛОГИКА) ===
    users_to_remove = []

    for uid, pred in list(ACTIVE_PREDICTIONS.items()):
        target_raw = pred["target_raw"]
        offset = (raw_id - target_raw) % MAX_GAMES_PER_DAY

        if 0 <= offset < CHECK_RANGE:
            p_type = pred["mode"]
            target_suit = pred["target_suit"]
            mirror_suit = pred["mirror_suit"]
            
            check_suits = game["player_suits"] if p_type == "suit_p" else game["banker_suits"]

            # --- ПРОВЕРКА ОСНОВНОГО ПРОГНОЗА ---
            if not pred["main_closed"]:
                if target_suit in check_suits:
                    pred["main_closed"] = True
                    pred["main_win_step"] = offset
                    update_stat(p_type, True)
                    
                    if offset == 0:
                        USERS_DATA[uid]["consecutive_zero_wins"] = USERS_DATA[uid].get("consecutive_zero_wins", 0) + 1
                        if USERS_DATA[uid]["consecutive_zero_wins"] >= 2:
                            USERS_DATA[uid]["last_was_dogon"] = True
                    else:
                        USERS_DATA[uid]["consecutive_zero_wins"] = 0
                        if offset in (2, 3):
                            USERS_DATA[uid]["last_was_dogon"] = True

                    step_emoji = STEP_EMOJIS.get(offset, "")
                    result_text = f"✅ #{target_raw} ➔ {pred['title']}{step_emoji}"
                    
                    try:
                        await context.bot.edit_message_text(chat_id=uid, message_id=pred["msg_id"], text=result_text)
                    except TelegramError:
                        pass

                    if pred.get("channel_msg_id") and DOGON_CHANNEL_ID != 0:
                        try:
                            await context.bot.edit_message_text(chat_id=DOGON_CHANNEL_ID, message_id=pred["channel_msg_id"], text=result_text)
                        except TelegramError:
                            pass

                elif offset == CHECK_RANGE - 1:
                    pred["main_closed"] = True
                    update_stat(p_type, False)
                    USERS_DATA[uid]["consecutive_zero_wins"] = 0
                    
                    result_text = f"❌ #{target_raw} ➔ {pred['title']} "
                    try:
                        await context.bot.edit_message_text(chat_id=uid, message_id=pred["msg_id"], text=result_text)
                    except TelegramError:
                        pass

                    if pred.get("channel_msg_id") and DOGON_CHANNEL_ID != 0:
                        try:
                            await context.bot.edit_message_text(chat_id=DOGON_CHANNEL_ID, message_id=pred["channel_msg_id"], text=result_text)
                        except TelegramError:
                            pass

            # --- ПРОВЕРКА ЗЕРКАЛЬНОГО ПРОГНОЗА ---
            if not pred["mirror_closed"]:
                if mirror_suit in check_suits:
                    pred["mirror_closed"] = True
                    pred["mirror_win_step"] = offset

                    step_emoji = STEP_EMOJIS.get(offset, "")
                    mirror_result_text = f"✅ #{target_raw} ➔ {pred['mirror_title']}{step_emoji}"

                    if pred.get("mirror_channel_msg_id") and MIRROR_CHANNEL_ID != 0:
                        try:
                            await context.bot.edit_message_text(chat_id=MIRROR_CHANNEL_ID, message_id=pred["mirror_channel_msg_id"], text=mirror_result_text)
                        except TelegramError:
                            pass

                elif offset == CHECK_RANGE - 1:
                    pred["mirror_closed"] = True
                    mirror_result_text = f"❌ #{target_raw} ➔ {pred['mirror_title']} "

                    if pred.get("mirror_channel_msg_id") and MIRROR_CHANNEL_ID != 0:
                        try:
                            await context.bot.edit_message_text(chat_id=MIRROR_CHANNEL_ID, message_id=pred["mirror_channel_msg_id"], text=mirror_result_text)
                        except TelegramError:
                            pass

            if pred["main_closed"] and pred["mirror_closed"]:
                users_to_remove.append(uid)

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
        global consecutive_non_zero_wins  # <-- ДОБАВЛЕНО
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
            # === НОВАЯ ЛОГИКА ДЛЯ 3-го КАНАЛА ===
            is_special_signal = consecutive_non_zero_wins >= 2
            
            if is_special_signal:
                consecutive_non_zero_wins = 0  # Сбрасываем счетчик после активации
            
            # Формируем текст сигнала
            if is_special_signal:
                signal_text = f"💸 #{target_raw} ➔ {title}"
            else:
                signal_text = f"⚡️ #{target_raw} ➔ {title}"
            
            mirror_suit = get_mirror_suit(target_suit)
            role = "Игрок" if "Игрок" in title else "Банкир"
            mirror_title = f"{mirror_suit} {role}"
            mirror_signal_text = f"💸 #{target_raw} ➔ {mirror_title}" if is_special_signal else f"⚡️ #{target_raw} ➔ {mirror_title}"
            
            is_dogon_follow_up = user.get("last_was_dogon", False)
            user["last_was_dogon"] = False

            try:
                # 1. Отправка пользователю
                sent_msg = await context.bot.send_message(uid, signal_text)
                
                channel_msg_id = None
                mirror_channel_msg_id = None
                special_channel_msg_id = None
                
                # 2. Публикация в каналы при выполнении условий
                if is_dogon_follow_up:
                    if DOGON_CHANNEL_ID != 0:
                        channel_msg = await context.bot.send_message(chat_id=DOGON_CHANNEL_ID, text=signal_text)
                        channel_msg_id = channel_msg.message_id
                    
                    if MIRROR_CHANNEL_ID != 0:
                        mirror_channel_msg = await context.bot.send_message(chat_id=MIRROR_CHANNEL_ID, text=mirror_signal_text)
                        mirror_channel_msg_id = mirror_channel_msg.message_id

                # 3. Публикация в 3-й (специальный) канал, если сработал триггер
                if is_special_signal and SPECIAL_CHANNEL_ID != 0:
                    special_msg = await context.bot.send_message(chat_id=SPECIAL_CHANNEL_ID, text=signal_text)
                    special_channel_msg_id = special_msg.message_id

                # Инициализация структуры раздельной проверки
                ACTIVE_PREDICTIONS[uid] = {
                    "target_raw": target_raw,
                    "mode": mode,
                    "title": title,
                    "target_suit": target_suit,
                    "mirror_title": mirror_title,
                    "mirror_suit": mirror_suit,
                    "msg_id": sent_msg.message_id,
                    "channel_msg_id": channel_msg_id,
                    "mirror_channel_msg_id": mirror_channel_msg_id,
                    "special_channel_msg_id": special_channel_msg_id,
                    # Флаги раздельного отслеживания
                    "main_closed": False,
                    "mirror_closed": False,
                    "main_win_step": None,
                    "mirror_win_step": None
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
            user["consecutive_zero_wins"] = 0
            user["last_was_dogon"] = False
            await query.edit_message_text("🛑 **Выдача сигналов остановлена.**\nВыберите нужный режим ниже для повторного запуска.", reply_markup=main_menu("off"), parse_mode='Markdown')

        elif query.data in ["select_suit_p", "select_suit_b"]:
            mode_map = {"select_suit_p": ("suit_p", "Масть Игрока"), "select_suit_b": ("suit_b", "Масть Банкира")}
            mode_code, mode_title = mode_map[query.data]
            user["selected_mode"] = mode_code
            user["consecutive_zero_wins"] = 0
            user["last_was_dogon"] = False
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

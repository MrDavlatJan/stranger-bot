import os
import logging
import asyncio
import random
import time
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)
from groq import Groq

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPPORT_GROUP_ID = int(os.environ.get("SUPPORT_GROUP_ID", "0"))

client = Groq(api_key=GROQ_API_KEY)

users = {}
waiting = []
active_chats = {}
ai_chats = {}
past_partners = {}
banned_users = set()
searching = set()
likes = {}
pending_likes = {}
last_chat_partner = {}
repeat_requests = set()
support_mode = set()
support_topics = {}

def get_user(uid, name=""):
    if uid not in users:
        users[uid] = {"name": name, "coins": 10, "total_spent": 0, "chats": 0, "gender": None, "looking_for": None, "setup_step": None}
    return users[uid]

def is_admin(uid):
    return uid == ADMIN_ID

def get_ai_system(looking_for):
    if looking_for == "girl":
        persona = "Ты девушка 20-25 лет. Общаешься легко и немного игриво."
    elif looking_for == "guy":
        persona = "Ты парень 20-25 лет. Общаешься уверенно и с юмором."
    else:
        persona = "Ты молодой человек 20-25 лет."
    return (f"{persona} Ты анонимный собеседник в чате. Никогда не признавайся что ты ИИ. "
            "Отвечай ОЧЕНЬ коротко — максимум 1-2 предложения. Без восклицаний и детского восторга. "
            "Говори естественно. Если написали одно слово — отвечай коротко.")

def get_typing_delay(text):
    words = len(text.split())
    return random.uniform(1.5, 3.0) + min(words * 0.1, 2.0)

def get_like_cost(sender_uid, receiver_uid):
    return 5 if users.get(sender_uid, {}).get("gender") == "girl" else 10

def gender_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("👦 Я парень"), KeyboardButton("👧 Я девушка")]], resize_keyboard=True, one_time_keyboard=True)

def looking_for_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("👧 Ищу девушку"), KeyboardButton("👦 Ищу парня"), KeyboardButton("🤷 Без разницы")]], resize_keyboard=True, one_time_keyboard=True)

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔍 Найти"), KeyboardButton("💰 Баланс")],
        [KeyboardButton("⭐ Купить монеты"), KeyboardButton("❓ Помощь")],
        [KeyboardButton("🆘 Поддержка")]
    ], resize_keyboard=True)

def chat_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("🚫 Завершить чат")]], resize_keyboard=True)

def support_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Выйти из поддержки")]], resize_keyboard=True)

def after_chat_keyboard(uid):
    partner_uid = last_chat_partner.get(uid)
    buttons = [[KeyboardButton("🔍 Найти нового")]]
    if partner_uid:
        cost = get_like_cost(uid, partner_uid)
        if uid in pending_likes:
            buttons.insert(0, [KeyboardButton("❤️ Лайк (бесплатно)"), KeyboardButton("👎 Дизлайк")])
        else:
            buttons.insert(0, [KeyboardButton(f"❤️ Лайк ({cost} монет)"), KeyboardButton("🔄 Повторный чат (20 монет)")])
        buttons.insert(1, [KeyboardButton("💌 Анонимное сообщение (15 монет)")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def ban_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🆘 Поддержка", callback_data="open_support"),
        InlineKeyboardButton("💫 Разбан — 250 звёзд", callback_data="unban_pay")
    ]])

def like_response_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Лайкнуть в ответ", callback_data="like_back"),
        InlineKeyboardButton("👎 Дизлайк", callback_data="dislike")
    ]])

# ─── /start ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)
    support_mode.discard(uid)
    u["setup_step"] = "gender"
    await update.message.reply_text("👋 Добро пожаловать в <b>MyStranger</b>!\n\nКто ты?", parse_mode="HTML", reply_markup=gender_keyboard())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    support_mode.discard(uid)
    await update.message.reply_text("Главное меню:", reply_markup=main_keyboard())

async def handle_setup(update, context, uid, text, u):
    if u["setup_step"] == "gender":
        if "парень" in text.lower():
            u["gender"] = "guy"
        elif "девушка" in text.lower():
            u["gender"] = "girl"
        else:
            await update.message.reply_text("Выбери кто ты 👇", reply_markup=gender_keyboard())
            return
        u["setup_step"] = "looking_for"
        await update.message.reply_text("Кого ищешь?", reply_markup=looking_for_keyboard())
    elif u["setup_step"] == "looking_for":
        if "девушку" in text.lower():
            u["looking_for"] = "girl"
        elif "парня" in text.lower():
            u["looking_for"] = "guy"
        elif "без разниц" in text.lower() or "разниц" in text.lower():
            u["looking_for"] = "any"
        else:
            await update.message.reply_text("Выбери кого ищешь 👇", reply_markup=looking_for_keyboard())
            return
        u["setup_step"] = None
        await update.message.reply_text(f"✅ Профиль создан!\n\n💰 У тебя <b>{u['coins']} монет</b>\n\nНажми <b>🔍 Найти</b> чтобы начать.", parse_mode="HTML", reply_markup=main_keyboard())

# ─── Поддержка ────────────────────────────────────────────
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    get_user(uid, name)
    # Останавливаем поиск если искал
    searching.discard(uid)
    for w in list(waiting):
        if w["uid"] == uid:
            waiting.remove(w)
            break
    support_mode.add(uid)
    await update.message.reply_text(
        "🆘 <b>Поддержка</b>\n\nПиши своё сообщение — модераторы ответят как можно скорее.\n\n"
        "Нажми ❌ Выйти из поддержки чтобы вернуться в меню.",
        parse_mode="HTML", reply_markup=support_keyboard()
    )

async def handle_support_message(uid, name, update, context):
    if not SUPPORT_GROUP_ID:
        return
    # Создаём топик если его нет
    if uid not in support_topics:
        try:
            topic = await context.bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=f"👤 {name} ({uid})")
            support_topics[uid] = topic.message_thread_id
            u = users.get(uid, {})
            gender = "👦 Парень" if u.get("gender") == "guy" else "👧 Девушка" if u.get("gender") == "girl" else "❓"
            await context.bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                message_thread_id=support_topics[uid],
                text=f"🆕 Новый тикет\n\n👤 {name}\n🆔 <code>{uid}</code>\n{gender}\n💰 Монет: {u.get('coins', 0)}",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            return
    # Пересылаем оригинальное сообщение юзера (forward)
    try:
        await context.bot.forward_message(
            chat_id=SUPPORT_GROUP_ID,
            from_chat_id=uid,
            message_id=update.message.message_id,
            message_thread_id=support_topics[uid]
        )
    except Exception as e:
        logging.error(f"Ошибка пересылки: {e}")

async def cancel_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    support_mode.discard(uid)
    await update.message.reply_text("✅ Вышел из поддержки.", reply_markup=main_keyboard())

# ─── Ответ модера из группы → юзеру (копирует наш текст) ─
async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.chat.id != SUPPORT_GROUP_ID:
        return
    # Игнорируем сообщения ботов
    if msg.from_user and msg.from_user.is_bot:
        return
    thread_id = msg.message_thread_id
    if not thread_id:
        return
    # Ищем юзера по topic_id
    target_uid = None
    for uid, tid in support_topics.items():
        if tid == thread_id:
            target_uid = uid
            break
    if not target_uid:
        return
    # Копируем текст модера юзеру
    try:
        await context.bot.send_message(target_uid, f"💬 <b>Поддержка:</b> {msg.text}", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка ответа юзеру: {e}")

# ─── Поиск ────────────────────────────────────────────────
async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)

    if uid in banned_users:
        await update.message.reply_text("🚫 Ты заблокирован.", reply_markup=ban_keyboard())
        return
    if u.get("setup_step"):
        await update.message.reply_text("Сначала заполни профиль. Напиши /start")
        return
    if u["gender"] is None:
        u["setup_step"] = "gender"
        await update.message.reply_text("Кто ты?", reply_markup=gender_keyboard())
        return
    if uid in active_chats:
        await update.message.reply_text("Ты уже в чате! Нажми 🚫 Завершить чат.")
        return
    if uid in ai_chats and not ai_chats[uid].get("ended"):
        await update.message.reply_text("Ты уже в чате! Нажми 🚫 Завершить чат.")
        return
    if uid in searching:
        await update.message.reply_text("Уже ищем... Подожди.")
        return

    searching.add(uid)
    await update.message.reply_text("🔍 Ищем собеседника...", reply_markup=ReplyKeyboardRemove())
    asyncio.create_task(do_find(uid, update, context))

async def do_find(uid, update, context):
    u = users[uid]
    looking = u.get("looking_for", "any")
    await asyncio.sleep(random.uniform(5, 12))
    if uid not in searching:
        return

    past = past_partners.get(uid, set())

    # Повторный чат
    if uid in repeat_requests:
        partner_uid = last_chat_partner.get(uid)
        if partner_uid and partner_uid in repeat_requests:
            repeat_requests.discard(uid)
            repeat_requests.discard(partner_uid)
            searching.discard(uid)
            searching.discard(partner_uid)
            active_chats[uid] = partner_uid
            active_chats[partner_uid] = uid
            try:
                await context.bot.send_message(uid, "🔄 Повторный чат начат!", reply_markup=chat_keyboard())
                await context.bot.send_message(partner_uid, "🔄 Повторный чат начат!", reply_markup=chat_keyboard())
            except:
                pass
            return

    # Ищем реального
    candidates = []
    for w in waiting:
        wuid = w["uid"]
        if wuid == uid or wuid in past:
            continue
        their_looking = w.get("looking_for", "any")
        my_gender = u.get("gender", "any")
        their_gender = users.get(wuid, {}).get("gender", "any")
        gender_match = (looking == "any" or their_gender == looking or their_looking == "any" or their_looking == my_gender)
        if gender_match:
            candidates.append(w)

    if candidates:
        partner = candidates[0]
        partner_uid = partner["uid"]
        waiting.remove(partner)
        searching.discard(uid)
        searching.discard(partner_uid)
        active_chats[uid] = partner_uid
        active_chats[partner_uid] = uid
        last_chat_partner[uid] = partner_uid
        last_chat_partner[partner_uid] = uid
        past_partners.setdefault(uid, set()).add(partner_uid)
        past_partners.setdefault(partner_uid, set()).add(uid)
        u["chats"] += 1
        users[partner_uid]["chats"] += 1
        try:
            await context.bot.send_message(uid, "✅ Собеседник найден! Начинайте общаться.", reply_markup=chat_keyboard())
            await context.bot.send_message(partner_uid, "✅ Собеседник найден! Начинайте общаться.", reply_markup=chat_keyboard())
        except:
            pass
        return

    # Ждём в очереди
    waiting_entry = {"uid": uid, "looking_for": looking, "gender": u.get("gender")}
    waiting.append(waiting_entry)
    await asyncio.sleep(90)

    if uid in active_chats or uid not in searching:
        return

    searching.discard(uid)
    if waiting_entry in waiting:
        waiting.remove(waiting_entry)
    if uid in banned_users:
        return

    # Даём ИИ
    ai_gender = looking if looking != "any" else random.choice(["girl", "guy"])
    ai_chats[uid] = {"history": [], "start_time": time.time(), "ended": False, "ai_gender": ai_gender}
    u["chats"] += 1
    try:
        await context.bot.send_message(uid, "✅ Собеседник найден!", reply_markup=chat_keyboard())
    except:
        pass
    asyncio.create_task(ai_chat_timer(uid, context))

async def ai_chat_timer(uid, context):
    await asyncio.sleep(180)
    if uid in ai_chats and not ai_chats[uid].get("ended"):
        ai_chats[uid]["ended"] = True
        endings = ["ладно мне пора 👋", "слушай, пока! было норм", "ой всё, до связи 😄", "мне надо идти, пока"]
        try:
            ending = random.choice(endings)
            await context.bot.send_chat_action(uid, "typing")
            await asyncio.sleep(get_typing_delay(ending))
            await context.bot.send_message(uid, ending)
            await asyncio.sleep(2)
            await context.bot.send_message(uid, "🔚 Собеседник ушёл.", reply_markup=after_chat_keyboard(uid))
        except:
            pass

# ─── /stop ────────────────────────────────────────────────
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in active_chats:
        partner_uid = active_chats.pop(uid)
        active_chats.pop(partner_uid, None)
        await update.message.reply_text("🔚 Чат завершён.", reply_markup=after_chat_keyboard(uid))
        try:
            await context.bot.send_message(partner_uid, "🔚 Собеседник завершил чат.", reply_markup=after_chat_keyboard(partner_uid))
        except:
            pass
        return
    if uid in ai_chats:
        ai_chats[uid]["ended"] = True
        del ai_chats[uid]
        await update.message.reply_text("🔚 Чат завершён.", reply_markup=after_chat_keyboard(uid))
        return
    if uid in searching:
        searching.discard(uid)
        for w in list(waiting):
            if w["uid"] == uid:
                waiting.remove(w)
                break
        await update.message.reply_text("❌ Поиск отменён.", reply_markup=main_keyboard())
        return
    await update.message.reply_text("Ты не в чате.", reply_markup=main_keyboard())

# ─── Лайк ─────────────────────────────────────────────────
async def handle_like(uid, context, free=False):
    partner_uid = last_chat_partner.get(uid)
    if not partner_uid:
        return False, "Нет недавнего собеседника."
    u = users.get(uid, {})
    cost = 0 if free else get_like_cost(uid, partner_uid)
    if not free and u.get("coins", 0) < cost:
        return False, f"Недостаточно монет. Нужно {cost} монет."
    if not free:
        u["coins"] -= cost
    likes.setdefault(uid, set()).add(partner_uid)
    if partner_uid in likes and uid in likes.get(partner_uid, set()):
        try:
            await context.bot.send_message(uid, "💕 Взаимный лайк! Нажми 🔄 Повторный чат чтобы снова пообщаться.")
            await context.bot.send_message(partner_uid, "💕 Взаимный лайк! Нажми 🔄 Повторный чат чтобы снова пообщаться.")
        except:
            pass
        return True, "💕 Взаимный лайк!"
    pending_likes[partner_uid] = uid
    try:
        await context.bot.send_message(partner_uid, "❤️ Тебя лайкнули! Хочешь ответить?", reply_markup=like_response_keyboard())
    except:
        pass
    return True, (f"❤️ Лайк отправлен! (-{cost} монет)" if not free else "❤️ Лайк отправлен!")

# ─── /balance ─────────────────────────────────────────────
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid, update.effective_user.full_name)
    online = len(searching) + len(active_chats) + len([x for x in ai_chats.values() if not x["ended"]])
    await update.message.reply_text(
        f"💰 Баланс: <b>{u['coins']} монет</b>\n💸 Потрачено: {u['total_spent']} монет\n💬 Чатов: {u['chats']}\n👥 Онлайн: {online}",
        parse_mode="HTML"
    )

# ─── /buy ─────────────────────────────────────────────────
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⭐ 50 монет — 50 звёзд", callback_data="buy_50")],
        [InlineKeyboardButton("⭐ 150 монет — 130 звёзд", callback_data="buy_150")],
        [InlineKeyboardButton("⭐ 350 монет — 280 звёзд", callback_data="buy_350")],
    ]
    await update.message.reply_text(
        "💰 <b>Купить монеты:</b>\n\n❤️ Лайк — 5-10 монет\n🔄 Повторный чат — 20 монет\n💌 Анонимное сообщение — 15 монет\n👥 Онлайн счётчик — 20 монет",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "open_support":
        support_mode.add(uid)
        await context.bot.send_message(uid, "🆘 <b>Поддержка</b>\n\nПиши своё сообщение — модераторы ответят.\n\nНажми ❌ Выйти из поддержки чтобы вернуться.", parse_mode="HTML", reply_markup=support_keyboard())
        return
    if query.data == "unban_pay":
        await context.bot.send_invoice(chat_id=uid, title="MyStranger — Разбан", description="Снятие блокировки", payload="unban", currency="XTR", prices=[LabeledPrice(label="Разбан", amount=250)])
        return
    if query.data == "like_back":
        if uid in pending_likes:
            success, msg = await handle_like(uid, context, free=True)
            pending_likes.pop(uid, None)
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(uid, msg)
        return
    if query.data == "dislike":
        pending_likes.pop(uid, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(uid, "👎 Дизлайк поставлен.")
        return

    packages = {"buy_50": (50, 50, "50 монет"), "buy_150": (150, 130, "150 монет"), "buy_350": (350, 280, "350 монет")}
    if query.data not in packages:
        return
    coins, stars, label = packages[query.data]
    await context.bot.send_invoice(chat_id=uid, title=f"MyStranger — {label}", description=f"{coins} монет", payload=f"coins_{coins}", currency="XTR", prices=[LabeledPrice(label=label, amount=stars)])

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload == "unban":
        banned_users.discard(uid)
        await update.message.reply_text("✅ Блокировка снята! Можешь снова общаться.", reply_markup=main_keyboard())
        return
    coins = int(payload.split("_")[1])
    u = get_user(uid, update.effective_user.full_name)
    u["coins"] += coins
    u["total_spent"] += coins
    await update.message.reply_text(f"✅ Начислено <b>{coins} монет</b>!\n💰 Баланс: <b>{u['coins']}</b>", parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды:</b>\n\n🔍 Найти — поиск собеседника\n🚫 Завершить чат — выйти\n"
        "❤️ Лайк — понравился собеседник\n🔄 Повторный чат — снова с тем же\n"
        "💌 Анонимное сообщение\n🆘 Поддержка — написать модераторам\n"
        "💰 Баланс\n⭐ Купить монеты",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

# ─── Главный обработчик ───────────────────────────────────
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    text = update.message.text
    chat_id = update.effective_chat.id

    # Сообщения из группы поддержки — только копируем ответы модеров
    if chat_id == SUPPORT_GROUP_ID:
        await handle_group_reply(update, context)
        return

    u = get_user(uid, name)

    if uid in banned_users:
        await update.message.reply_text("🚫 Ты заблокирован.", reply_markup=ban_keyboard())
        return

    # Кнопки меню
    if text == "🔍 Найти" or text == "🔍 Найти нового":
        await find(update, context)
        return
    elif text == "🚫 Завершить чат":
        await stop(update, context)
        return
    elif text == "💰 Баланс":
        await balance(update, context)
        return
    elif text == "⭐ Купить монеты":
        await buy(update, context)
        return
    elif text == "❓ Помощь":
        await help_cmd(update, context)
        return
    elif text == "🆘 Поддержка":
        await support(update, context)
        return
    elif text == "❌ Выйти из поддержки":
        await cancel_support(update, context)
        return

    # ── Режим поддержки — ПРИОРИТЕТ ──
    if uid in support_mode:
        await handle_support_message(uid, name, update, context)
        await update.message.reply_text("✅ Сообщение отправлено модераторам. Ожидай ответа.")
        return

    # Лайк / дизлайк / повторный чат / анонимное
    if "лайк" in text.lower() and "бесплатно" in text.lower():
        success, msg = await handle_like(uid, context, free=True)
        pending_likes.pop(uid, None)
        await update.message.reply_text(msg)
        return
    elif "лайк" in text.lower():
        success, msg = await handle_like(uid, context, free=False)
        await update.message.reply_text(msg)
        return
    elif "дизлайк" in text.lower():
        pending_likes.pop(uid, None)
        await update.message.reply_text("👎 Ок.")
        return
    elif "повторный чат" in text.lower():
        partner_uid = last_chat_partner.get(uid)
        if not partner_uid:
            await update.message.reply_text("Нет недавнего собеседника.")
            return
        if u.get("coins", 0) < 20:
            await update.message.reply_text("Недостаточно монет. Нужно 20 монет.")
            return
        u["coins"] -= 20
        repeat_requests.add(uid)
        await update.message.reply_text("🔄 Запрос отправлен!", reply_markup=ReplyKeyboardRemove())
        asyncio.create_task(do_find(uid, update, context))
        return
    elif "анонимное сообщение" in text.lower():
        partner_uid = last_chat_partner.get(uid)
        if not partner_uid:
            await update.message.reply_text("Нет недавнего собеседника.")
            return
        if u.get("coins", 0) < 15:
            await update.message.reply_text("Недостаточно монет. Нужно 15 монет.")
            return
        u["coins"] -= 15
        context.user_data["sending_anon"] = partner_uid
        await update.message.reply_text("💌 Напиши сообщение — отправлю анонимно:", reply_markup=ReplyKeyboardRemove())
        return

    # Настройка профиля
    if u.get("setup_step"):
        await handle_setup(update, context, uid, text, u)
        return

    # Анонимное сообщение
    if context.user_data.get("sending_anon"):
        partner_uid = context.user_data.pop("sending_anon")
        try:
            await context.bot.send_message(partner_uid, f"💌 Анонимное сообщение: {text}")
            await update.message.reply_text("✅ Отправлено!", reply_markup=main_keyboard())
        except:
            await update.message.reply_text("❌ Не удалось.", reply_markup=main_keyboard())
        return

    # Реальный чат
    if uid in active_chats:
        partner_uid = active_chats[uid]
        await context.bot.send_chat_action(partner_uid, "typing")
        await asyncio.sleep(random.uniform(0.3, 1.0))
        try:
            await context.bot.send_message(partner_uid, text)
        except:
            pass
        return

    # ИИ чат
    if uid in ai_chats and not ai_chats[uid]["ended"]:
        history = ai_chats[uid]["history"]
        ai_gender = ai_chats[uid].get("ai_gender", "any")
        history.append({"role": "user", "content": text})
        if len(history) > 10:
            history = history[-10:]
            ai_chats[uid]["history"] = history
        await context.bot.send_chat_action(uid, "typing")
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": get_ai_system(ai_gender)}] + history,
                max_tokens=80
            )
            reply = response.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": reply})
            await asyncio.sleep(get_typing_delay(reply))
            await context.bot.send_chat_action(uid, "typing")
            await asyncio.sleep(0.5)
            await update.message.reply_text(reply)
        except Exception as e:
            logging.error(f"Groq error: {e}")
        return

    if u["gender"] is None:
        u["setup_step"] = "gender"
        await update.message.reply_text("Кто ты?", reply_markup=gender_keyboard())
        return

    await update.message.reply_text("Нажми 🔍 Найти чтобы начать.", reply_markup=main_keyboard())

# ─── Онлайн ───────────────────────────────────────────────
async def online_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    if u.get("coins", 0) < 20:
        await update.message.reply_text("Недостаточно монет. Нужно 20 монет.")
        return
    u["coins"] -= 20
    online = len(searching) + len(active_chats) + len([x for x in ai_chats.values() if not x["ended"]])
    await update.message.reply_text(f"👥 Сейчас онлайн: <b>{online} человек</b>", parse_mode="HTML")

# ─── Админ ────────────────────────────────────────────────
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        f"📊 <b>Статистика:</b>\n\n👥 Всего: {len(users)}\n💬 Чатов: {len(active_chats) // 2}\n"
        f"🤖 ИИ: {len([u for u in ai_chats.values() if not u['ended']])}\n"
        f"🔍 Ищут: {len(searching)}\n⏳ Очередь: {len(waiting)}\n"
        f"🆘 Поддержка: {len(support_mode)}\n🚫 Банов: {len(banned_users)}",
        parse_mode="HTML"
    )

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Использование: /ban [id]")
        return
    uid = int(context.args[0])
    banned_users.add(uid)
    await update.message.reply_text(f"🔨 {uid} забанен.")
    try:
        await context.bot.send_message(uid, "🚫 Ты заблокирован.\n\nЕсли считаешь это ошибкой — напиши в поддержку или оплати разбан:", reply_markup=ban_keyboard())
    except:
        pass

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Использование: /unban [id]")
        return
    uid = int(context.args[0])
    banned_users.discard(uid)
    await update.message.reply_text(f"✅ {uid} разбанен.")
    try:
        await context.bot.send_message(uid, "✅ Блокировка снята! Можешь снова общаться.", reply_markup=main_keyboard())
    except:
        pass

async def admin_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /give [id] [монеты]")
        return
    uid = int(context.args[0])
    coins = int(context.args[1])
    get_user(uid)
    users[uid]["coins"] += coins
    users[uid]["total_spent"] += coins
    await update.message.reply_text(f"✅ Выдано {coins} монет пользователю {uid}.")

async def admin_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /take [id] [монеты]")
        return
    uid = int(context.args[0])
    coins = int(context.args[1])
    if uid not in users:
        await update.message.reply_text("Пользователь не найден.")
        return
    users[uid]["coins"] = max(0, users[uid]["coins"] - coins)
    await update.message.reply_text(f"✅ Забрано {coins} монет у {uid}. Баланс: {users[uid]['coins']}.")

# ─── Запуск ───────────────────────────────────────────────
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("menu", menu_cmd))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("find", find))
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("online", online_count))
app.add_handler(CommandHandler("support", support))
app.add_handler(CommandHandler("cancel", cancel_support))
app.add_handler(CommandHandler("stats", admin_stats))
app.add_handler(CommandHandler("ban", admin_ban))
app.add_handler(CommandHandler("unban", admin_unban))
app.add_handler(CommandHandler("give", admin_give))
app.add_handler(CommandHandler("take", admin_take))
app.add_handler(CallbackQueryHandler(buy_callback))
app.add_handler(PreCheckoutQueryHandler(precheckout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
app.run_polling()

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

client = Groq(api_key=GROQ_API_KEY)

# ─── Хранилище ────────────────────────────────────────────
users = {}          # uid -> {name, coins, total_spent, chats, gender, looking_for, setup_step}
waiting = []        # [{uid, gender, looking_for}]
active_chats = {}   # uid -> uid
ai_chats = {}       # uid -> {history, start_time, ended, ai_gender}
past_partners = {}  # uid -> set of uids
banned_users = set()
searching = set()   # uid -> ищет прямо сейчас

def get_user(uid, name=""):
    if uid not in users:
        users[uid] = {
            "name": name,
            "coins": 10,
            "total_spent": 0,
            "chats": 0,
            "gender": None,
            "looking_for": None,
            "setup_step": None
        }
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

    return (
        f"{persona} Ты анонимный собеседник в чате. "
        "Никогда не признавайся что ты ИИ или бот — ты живой человек. "
        "Отвечай ОЧЕНЬ коротко — максимум 1-2 предложения. Без восклицаний и детского восторга. "
        "Говори естественно как живой человек в переписке. Иногда задавай вопросы. "
        "Если написали одно слово — отвечай одним-двумя словами или коротко."
    )

def get_typing_delay(text):
    words = len(text.split())
    base = random.uniform(1.5, 3.0)
    extra = min(words * 0.1, 2.0)
    return base + extra

# ─── Клавиатуры ───────────────────────────────────────────
def gender_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👦 Я парень"), KeyboardButton("👧 Я девушка")]],
        resize_keyboard=True, one_time_keyboard=True
    )

def looking_for_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👧 Ищу девушку"), KeyboardButton("👦 Ищу парня"), KeyboardButton("🤷 Без разницы")]],
        resize_keyboard=True, one_time_keyboard=True
    )

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔍 Найти"), KeyboardButton("💰 Баланс")],
         [KeyboardButton("⭐ Купить монеты"), KeyboardButton("❓ Помощь")]],
        resize_keyboard=True
    )

def chat_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🚫 Завершить чат")]],
        resize_keyboard=True
    )

# ─── /start ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)
    u["setup_step"] = "gender"
    await update.message.reply_text(
        "👋 Добро пожаловать в <b>MyStranger</b>!\n\nКто ты?",
        parse_mode="HTML",
        reply_markup=gender_keyboard()
    )

# ─── Настройка профиля ────────────────────────────────────
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
        await update.message.reply_text(
            f"✅ Профиль создан!\n\n💰 У тебя <b>{u['coins']} монет</b>\n\nНажми <b>🔍 Найти</b> чтобы начать.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

# ─── Поиск (параллельный) ─────────────────────────────────
async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)

    if uid in banned_users:
        await update.message.reply_text("🚫 Ты заблокирован.")
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

    await update.message.reply_text("🔍 Ищем собеседника...", reply_markup=ReplyKeyboardRemove())
    asyncio.create_task(do_find(uid, name, update, context))

async def do_find(uid, name, update, context):
    searching.add(uid)
    u = users[uid]
    looking = u.get("looking_for", "any")

    # Рандомная задержка
    delay = random.uniform(5, 12)
    await asyncio.sleep(delay)

    # Проверяем не передумал ли пользователь
    if uid not in searching:
        return

    searching.discard(uid)

    # Ищем реального человека в очереди
    past = past_partners.get(uid, set())
    candidates = []
    for w in waiting:
        wuid = w["uid"]
        if wuid == uid or wuid in past:
            continue
        their_looking = w.get("looking_for", "any")
        my_gender = u.get("gender", "any")
        their_gender = users.get(wuid, {}).get("gender", "any")
        gender_match = (
            looking == "any" or their_gender == looking or
            their_looking == "any" or their_looking == my_gender
        )
        if gender_match:
            candidates.append(w)

    if candidates:
        partner = candidates[0]
        partner_uid = partner["uid"]
        waiting.remove(partner)

        active_chats[uid] = partner_uid
        active_chats[partner_uid] = uid

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

    # Нет реального — добавляем в очередь на 60 секунд
    waiting.append({"uid": uid, "looking_for": looking, "gender": u.get("gender")})
    try:
        await context.bot.send_message(uid, "⏳ Реальных собеседников пока нет, ждём ещё немного...\n/stop — отменить")
    except:
        pass

    await asyncio.sleep(60)

    # Проверяем — вдруг нашли пока ждали
    if uid in active_chats:
        return

    # Убираем из очереди и даём ИИ
    for w in waiting:
        if w["uid"] == uid:
            waiting.remove(w)
            break

    if uid in banned_users:
        return

    ai_gender = looking if looking != "any" else random.choice(["girl", "guy"])
    ai_chats[uid] = {
        "history": [],
        "start_time": time.time(),
        "ended": False,
        "ai_gender": ai_gender
    }
    u["chats"] += 1

    try:
        await context.bot.send_message(
            uid,
            "✅ Собеседник найден!\n\n💡 <i>Купи монеты чтобы находить людей быстрее</i>",
            parse_mode="HTML",
            reply_markup=chat_keyboard()
        )
    except:
        pass

    asyncio.create_task(ai_chat_timer(uid, context))

async def ai_chat_timer(uid, context):
    await asyncio.sleep(180)
    if uid in ai_chats and not ai_chats[uid].get("ended"):
        ai_chats[uid]["ended"] = True
        endings = [
            "ладно мне пора 👋",
            "слушай, пока! было норм",
            "ой всё, до связи 😄",
            "мне надо идти, пока",
        ]
        try:
            ending = random.choice(endings)
            await context.bot.send_chat_action(uid, "typing")
            await asyncio.sleep(get_typing_delay(ending))
            await context.bot.send_message(uid, ending)
            await asyncio.sleep(2)
            await context.bot.send_message(
                uid,
                "🔚 Собеседник ушёл.\n\n/find — найти нового",
                reply_markup=main_keyboard()
            )
        except:
            pass

# ─── /stop ────────────────────────────────────────────────
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in active_chats:
        partner_uid = active_chats.pop(uid)
        active_chats.pop(partner_uid, None)
        await update.message.reply_text("🔚 Чат завершён.\n\n/find — найти нового", reply_markup=main_keyboard())
        try:
            await context.bot.send_message(partner_uid, "🔚 Собеседник завершил чат.\n\n/find — найти нового", reply_markup=main_keyboard())
        except:
            pass
        return

    if uid in ai_chats:
        ai_chats[uid]["ended"] = True
        del ai_chats[uid]
        await update.message.reply_text("🔚 Чат завершён.\n\n/find — найти нового", reply_markup=main_keyboard())
        return

    if uid in searching:
        searching.discard(uid)
        for w in waiting:
            if w["uid"] == uid:
                waiting.remove(w)
                break
        await update.message.reply_text("❌ Поиск отменён.", reply_markup=main_keyboard())
        return

    await update.message.reply_text("Ты не в чате. /find — найти собеседника", reply_markup=main_keyboard())

# ─── /balance ─────────────────────────────────────────────
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid, update.effective_user.full_name)
    await update.message.reply_text(
        f"💰 Баланс: <b>{u['coins']} монет</b>\n"
        f"💸 Потрачено: {u['total_spent']} монет\n"
        f"💬 Чатов: {u['chats']}",
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
        "💰 <b>Купить монеты:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    packages = {
        "buy_50": (50, 50, "50 монет"),
        "buy_150": (150, 130, "150 монет"),
        "buy_350": (350, 280, "350 монет"),
    }
    if query.data not in packages:
        return
    coins, stars, label = packages[query.data]
    await context.bot.send_invoice(
        chat_id=uid,
        title=f"MyStranger — {label}",
        description=f"{coins} монет для MyStranger",
        payload=f"coins_{coins}",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=stars)],
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    coins = int(payload.split("_")[1])
    u = get_user(uid, update.effective_user.full_name)
    u["coins"] += coins
    u["total_spent"] += coins
    await update.message.reply_text(
        f"✅ Начислено <b>{coins} монет</b>!\n💰 Баланс: <b>{u['coins']}</b>",
        parse_mode="HTML"
    )

# ─── /help ────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Как это работает:</b>\n\n"
        "🔍 Найти — поиск собеседника\n"
        "🚫 Завершить чат — выйти из чата\n"
        "💰 Баланс — твои монеты\n"
        "⭐ Купить монеты — поддержать бота\n\n"
        "👤 Бот ищет реального человека\n"
        "🤖 Если никого нет — подключает ИИ",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# ─── Главный обработчик ───────────────────────────────────
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    text = update.message.text
    u = get_user(uid, name)

    if uid in banned_users:
        return

    # Кнопки меню
    if text == "🔍 Найти":
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

    # Настройка профиля
    if u.get("setup_step"):
        await handle_setup(update, context, uid, text, u)
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
            delay = get_typing_delay(reply)
            await asyncio.sleep(delay)
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

# ─── Админ ────────────────────────────────────────────────
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        f"📊 <b>Статистика:</b>\n\n"
        f"👥 Всего: {len(users)}\n"
        f"💬 Активных чатов: {len(active_chats) // 2}\n"
        f"🤖 ИИ чатов: {len([u for u in ai_chats.values() if not u['ended']])}\n"
        f"🔍 Ищут: {len(searching)}\n"
        f"⏳ В очереди: {len(waiting)}\n"
        f"🚫 Банов: {len(banned_users)}",
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

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Использование: /unban [id]")
        return
    uid = int(context.args[0])
    banned_users.discard(uid)
    await update.message.reply_text(f"✅ {uid} разбанен.")

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

# ─── Запуск ───────────────────────────────────────────────
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("find", find))
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("stats", admin_stats))
app.add_handler(CommandHandler("ban", admin_ban))
app.add_handler(CommandHandler("unban", admin_unban))
app.add_handler(CommandHandler("give", admin_give))
app.add_handler(CallbackQueryHandler(buy_callback))
app.add_handler(PreCheckoutQueryHandler(precheckout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
app.run_polling()

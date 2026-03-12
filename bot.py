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

REAL_CHAT_THRESHOLD = 50

client = Groq(api_key=GROQ_API_KEY)

# ─── Хранилище ────────────────────────────────────────────
users = {}          # uid -> {name, coins, total_spent, chats, gender, looking_for, setup_step}
waiting = []        # очередь {uid, gender, looking_for}
active_chats = {}   # uid -> uid
ai_chats = {}       # uid -> {history, start_time, ended, ai_gender}
past_partners = {}  # uid -> set of uids
banned_users = set()

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

def has_real_access(uid):
    return users.get(uid, {}).get("total_spent", 0) >= REAL_CHAT_THRESHOLD

def get_ai_system(looking_for):
    if looking_for == "girl":
        persona = "Ты девушка 20-25 лет. Общаешься легко и игриво."
    elif looking_for == "guy":
        persona = "Ты парень 20-25 лет. Общаешься уверенно и с юмором."
    else:
        persona = "Ты молодой человек 20-25 лет, пол не важен."

    return (
        f"{persona} Ты анонимный собеседник в чате. "
        "Никогда не признавайся что ты ИИ или бот — ты живой человек. "
        "Отвечай ОЧЕНЬ коротко — максимум 1-2 предложения. Без восклицаний и детского восторга. "
        "Говори естественно как живой человек в переписке. Иногда задавай вопросы. "
        "Не пиши длинные тексты. Если написали одно слово — отвечай одним-двумя словами или коротко."
    )

def get_typing_delay(text):
    words = len(text.split())
    base = random.uniform(2.5, 4.0)
    extra = min(words * 0.15, 3.0)
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

# ─── Обработка настройки профиля ──────────────────────────
async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, uid, text, u):
    if u["setup_step"] == "gender":
        if "парень" in text.lower():
            u["gender"] = "guy"
        elif "девушка" in text.lower() or "девушку" in text.lower():
            u["gender"] = "girl"
        else:
            await update.message.reply_text("Выбери кто ты 👇", reply_markup=gender_keyboard())
            return

        u["setup_step"] = "looking_for"
        await update.message.reply_text("Кого ищешь?", reply_markup=looking_for_keyboard())

    elif u["setup_step"] == "looking_for":
        if "девушку" in text.lower() or "девушка" in text.lower():
            u["looking_for"] = "girl"
        elif "парня" in text.lower() or "парень" in text.lower():
            u["looking_for"] = "guy"
        elif "без разницы" in text.lower() or "разниц" in text.lower():
            u["looking_for"] = "any"
        else:
            await update.message.reply_text("Выбери кого ищешь 👇", reply_markup=looking_for_keyboard())
            return

        u["setup_step"] = None
        await update.message.reply_text(
            f"✅ Профиль создан!\n\n💰 У тебя <b>{u['coins']} монет</b>\n\nНажми <b>🔍 Найти</b> чтобы начать общение.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

# ─── /find ────────────────────────────────────────────────
async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)

    if uid in banned_users:
        await update.message.reply_text("🚫 Ты заблокирован.")
        return

    if u["setup_step"]:
        await update.message.reply_text("Сначала заполни профиль. Напиши /start")
        return

    if u["gender"] is None:
        u["setup_step"] = "gender"
        await update.message.reply_text("Сначала заполни профиль. Кто ты?", reply_markup=gender_keyboard())
        return

    if uid in active_chats:
        await update.message.reply_text("Ты уже в чате! Напиши /stop чтобы завершить.")
        return

    if uid in ai_chats and not ai_chats[uid].get("ended"):
        await update.message.reply_text("Ты уже в чате! Напиши /stop чтобы завершить.")
        return

    if uid in [w["uid"] for w in waiting]:
        await update.message.reply_text("Уже ищем... Подожди немного.")
        return

    await update.message.reply_text("🔍 Ищем собеседника...", reply_markup=ReplyKeyboardRemove())

    # Рандомная задержка 5-12 секунд
    delay = random.uniform(5, 12)
    await asyncio.sleep(delay)

    looking = u.get("looking_for", "any")

    # Ищем реального человека сначала
    if has_real_access(uid):
        past = past_partners.get(uid, set())
        candidates = []
        for w in waiting:
            wuid = w["uid"]
            if wuid == uid or wuid in past:
                continue
            # Проверяем совместимость по полу
            their_looking = w.get("looking_for", "any")
            my_gender = u.get("gender", "any")
            their_gender = users.get(wuid, {}).get("gender", "any")

            gender_match = (
                looking == "any" or
                their_gender == looking or
                their_looking == "any" or
                their_looking == my_gender
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

            await update.message.reply_text(
                "✅ Собеседник найден! Начинайте общаться.\n/stop — завершить чат",
                reply_markup=main_keyboard()
            )
            await context.bot.send_message(
                partner_uid,
                "✅ Собеседник найден! Начинайте общаться.\n/stop — завершить чат",
                reply_markup=main_keyboard()
            )
            return

        # Нет реального — добавляем в очередь
        waiting.append({"uid": uid, "looking_for": looking, "gender": u.get("gender")})
        await update.message.reply_text(
            "⏳ Реальных собеседников пока нет. Ищем...\n/stop — отменить поиск",
            reply_markup=main_keyboard()
        )
        return

    # ИИ чат
    ai_gender = looking if looking != "any" else random.choice(["girl", "guy"])
    ai_chats[uid] = {
        "history": [],
        "start_time": time.time(),
        "ended": False,
        "ai_gender": ai_gender
    }
    u["chats"] += 1

    await update.message.reply_text(
        "✅ Собеседник найден!\n\n💡 <i>Купи монеты чтобы общаться с реальными людьми</i>",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

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
            delay = get_typing_delay(ending)
            await context.bot.send_chat_action(uid, "typing")
            await asyncio.sleep(delay)
            await context.bot.send_message(uid, ending)
            await asyncio.sleep(2)
            await context.bot.send_message(
                uid,
                "🔚 Собеседник ушёл.\n\n💰 Купи монеты для общения с реальными людьми!\n/buy"
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

    for w in waiting:
        if w["uid"] == uid:
            waiting.remove(w)
            await update.message.reply_text("❌ Поиск отменён.", reply_markup=main_keyboard())
            return

    await update.message.reply_text("Ты не в чате. /find — найти собеседника", reply_markup=main_keyboard())

# ─── /balance ─────────────────────────────────────────────
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)
    access = "👤 Реальные люди" if has_real_access(uid) else "🤖 ИИ-собеседник"
    need = max(0, REAL_CHAT_THRESHOLD - u["total_spent"])
    text = (
        f"💰 Баланс: <b>{u['coins']} монет</b>\n"
        f"💸 Потрачено: {u['total_spent']} монет\n"
        f"💬 Чатов: {u['chats']}\n"
        f"🔓 Режим: {access}\n"
    )
    if need > 0:
        text += f"\nДо реальных людей: <b>{need} монет</b>"
    await update.message.reply_text(text, parse_mode="HTML")

# ─── /buy ─────────────────────────────────────────────────
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⭐ 50 монет — 50 звёзд", callback_data="buy_50")],
        [InlineKeyboardButton("⭐ 150 монет — 130 звёзд", callback_data="buy_150")],
        [InlineKeyboardButton("⭐ 350 монет — 280 звёзд", callback_data="buy_350")],
    ]
    await update.message.reply_text(
        "💰 <b>Купить монеты:</b>\n\nМонеты открывают доступ к реальным собеседникам.",
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
        description=f"Получи {coins} монет для общения с реальными людьми",
        payload=f"coins_{coins}",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=stars)],
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    payload = update.message.successful_payment.invoice_payload
    coins = int(payload.split("_")[1])
    u = get_user(uid, name)
    u["coins"] += coins
    u["total_spent"] += coins
    unlocked = "\n\n🎉 <b>Теперь тебе доступны реальные собеседники!</b>" if has_real_access(uid) else ""
    await update.message.reply_text(
        f"✅ Начислено <b>{coins} монет</b>!\n💰 Баланс: <b>{u['coins']}</b>{unlocked}",
        parse_mode="HTML"
    )

# ─── Главный обработчик ───────────────────────────────────
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    text = update.message.text
    u = get_user(uid, name)

    if uid in banned_users:
        return

    # Кнопки главного меню
    if text == "🔍 Найти":
        await find(update, context)
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
        await asyncio.sleep(random.uniform(0.5, 1.5))
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
            await asyncio.sleep(1)
            await update.message.reply_text(reply)
        except Exception as e:
            logging.error(f"Groq error: {e}")
        return

    # Не в чате — если профиль не заполнен
    if u["gender"] is None:
        u["setup_step"] = "gender"
        await update.message.reply_text("Кто ты?", reply_markup=gender_keyboard())
        return

    await update.message.reply_text("Нажми 🔍 Найти чтобы начать общение.", reply_markup=main_keyboard())

# ─── /help ────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Как это работает:</b>\n\n"
        "🔍 Найти — поиск собеседника\n"
        "/stop — завершить чат\n"
        "💰 Баланс — твои монеты\n"
        "⭐ Купить монеты — доступ к реальным людям\n\n"
        "🤖 Новые пользователи общаются с ИИ\n"
        "👤 После покупки монет — реальные люди",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# ─── Админ ────────────────────────────────────────────────
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total = len(users)
    real = sum(1 for u in users.values() if u["total_spent"] >= REAL_CHAT_THRESHOLD)
    active = len(active_chats) // 2
    ai_active = len([u for u in ai_chats.values() if not u["ended"]])
    await update.message.reply_text(
        f"📊 <b>Статистика:</b>\n\n"
        f"👥 Всего: {total}\n"
        f"💎 Премиум: {real}\n"
        f"💬 Активных чатов: {active}\n"
        f"🤖 ИИ чатов: {ai_active}\n"
        f"🔍 В очереди: {len(waiting)}\n"
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

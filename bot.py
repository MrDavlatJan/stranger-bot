import os
import logging
import asyncio
import random
import time
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)
from groq import Groq

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# Порог монет для реального чата
REAL_CHAT_THRESHOLD = 50

client = Groq(api_key=GROQ_API_KEY)

# ─── Хранилище ────────────────────────────────────────────
users = {}          # uid -> {name, coins, total_spent, chats}
waiting = []        # очередь ожидающих реального чата
active_chats = {}   # uid -> uid (пары)
ai_chats = {}       # uid -> {history, start_time, ended}
past_partners = {}  # uid -> set of uids (кого уже встречал)
banned_users = set()

# ─── AI персонаж ──────────────────────────────────────────
AI_SYSTEM = """Ты анонимный собеседник в чате. Ты живой человек — никогда не признавайся что ты ИИ. 
Общайся естественно, с юмором, немного флиртуй если уместно. Будь интересным и вовлекающим.
Отвечай коротко — 1-3 предложения максимум. Говори на том языке на котором пишет собеседник.
Не будь слишком навязчивым. Иногда задавай вопросы. Будь живым и непредсказуемым."""

def get_user(uid, name=""):
    if uid not in users:
        users[uid] = {"name": name, "coins": 10, "total_spent": 0, "chats": 0}
    return users[uid]

def is_admin(uid):
    return uid == ADMIN_ID

def has_real_access(uid):
    return users.get(uid, {}).get("total_spent", 0) >= REAL_CHAT_THRESHOLD

# ─── /start ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    get_user(uid, name)

    text = (
        "👋 Добро пожаловать в <b>MyStranger</b>!\n\n"
        "Здесь ты можешь анонимно общаться с незнакомцами.\n\n"
        "👤 Реальные люди доступны после накопления монет.\n\n"
        f"💰 Твой баланс: <b>{users[uid]['coins']} монет</b>\n\n"
        "/find — найти собеседника\n"
        "/stop — завершить чат\n"
        "/balance — мой баланс\n"
        "/buy — купить монеты\n"
        "/help — помощь"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ─── /help ────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Команды:</b>\n\n"
        "/find — найти собеседника\n"
        "/stop — завершить чат\n"
        "/balance — мой баланс и статистика\n"
        "/buy — купить монеты за звёзды\n\n"
        "💡 Чем больше монет — тем быстрее находишь реальных людей."
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ─── /balance ─────────────────────────────────────────────
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)
    access = "👤 Реальные люди" if has_real_access(uid) else "🤖 ИИ-собеседник"
    text = (
        f"💰 <b>Твой баланс:</b> {u['coins']} монет\n"
        f"💸 Потрачено всего: {u['total_spent']} монет\n"
        f"💬 Чатов проведено: {u['chats']}\n"
        f"🔓 Режим: {access}\n\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ─── /buy ─────────────────────────────────────────────────
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⭐ 50 монет — 50 звёзд", callback_data="buy_50")],
        [InlineKeyboardButton("⭐ 150 монет — 130 звёзд", callback_data="buy_150")],
        [InlineKeyboardButton("⭐ 350 монет — 280 звёзд", callback_data="buy_350")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "💰 <b>Купить монеты:</b>\n\nМонеты ускоряют поиск.",
        parse_mode="HTML",
        reply_markup=reply_markup
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
        description=f"Получи {coins} монет для буста профиля",
        payload=f"coins_{coins}",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=stars)],
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    payload = update.message.successful_payment.invoice_payload

    coins = int(payload.split("_")[1])
    u = get_user(uid, name)
    u["coins"] += coins
    u["total_spent"] += coins

    unlocked = ""
    if has_real_access(uid):
        unlocked = "\n\n🎉 <b>Поздравляем! Твой профил будет показывтся чаще!</b>"

    await update.message.reply_text(
        f"✅ Оплата прошла! Начислено <b>{coins} монет</b>.\n"
        f"💰 Баланс: <b>{u['coins']} монет</b>{unlocked}",
        parse_mode="HTML"
    )

# ─── /find ────────────────────────────────────────────────
async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    u = get_user(uid, name)

    if uid in banned_users:
        await update.message.reply_text("🚫 Ты заблокирован.")
        return

    if uid in active_chats:
        await update.message.reply_text("Ты уже в чате! Напиши /stop чтобы завершить.")
        return

    if uid in ai_chats and not ai_chats[uid].get("ended"):
        await update.message.reply_text("Ты уже общаешься! Напиши /stop чтобы завершить.")
        return

    # Если у юзера достаточно монет — ищем реального
    if has_real_access(uid):
        past = past_partners.get(uid, set())
        candidates = [w for w in waiting if w != uid and w not in past]

        if candidates:
            partner_uid = candidates[0]
            waiting.remove(partner_uid)

            active_chats[uid] = partner_uid
            active_chats[partner_uid] = uid

            if uid not in past_partners:
                past_partners[uid] = set()
            if partner_uid not in past_partners:
                past_partners[partner_uid] = set()

            past_partners[uid].add(partner_uid)
            past_partners[partner_uid].add(uid)

            u["chats"] += 1
            users[partner_uid]["chats"] += 1

            await update.message.reply_text("✅ Собеседник найден! Начинайте общаться.\nНапишите /stop чтобы завершить.")
            await context.bot.send_message(partner_uid, "✅ Собеседник найден! Начинайте общаться.\nНапишите /stop чтобы завершить.")
            return
        else:
            waiting.append(uid)
            await update.message.reply_text("🔍 Ищем собеседника... Ожидайте.\nНапишите /stop чтобы отменить поиск.")
            return

    # Иначе — ИИ чат
    ai_chats[uid] = {
        "history": [],
        "start_time": time.time(),
        "ended": False
    }
    u["chats"] += 1

    await update.message.reply_text(
        "✅ Собеседник найден!\n\n"
        "💡 <i>Совет: покупай монеты чтобы бустить свой профиль!</i>",
        parse_mode="HTML"
    )

    # Запускаем таймер на 3 минуты
    asyncio.create_task(ai_chat_timer(uid, context))

async def ai_chat_timer(uid, context):
    await asyncio.sleep(180)  # 3 минуты
    if uid in ai_chats and not ai_chats[uid]["ended"]:
        ai_chats[uid]["ended"] = True
        endings = [
            "Хм, мне надо идти 👋 было приятно поболтать!",
            "Слушай, ладно, удачи тебе 👋",
            "Мне пора! Было весело 😄",
        ]
        try:
            await context.bot.send_message(uid, random.choice(endings))
            await asyncio.sleep(2)
            await context.bot.send_message(
                uid,
                "🔚 Собеседник завершил чат.\n\n"
                "💰 Купи монеты чтобы общаться с реальными людьми дольше!\n/buy"
            )
        except:
            pass

# ─── /stop ────────────────────────────────────────────────
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Реальный чат
    if uid in active_chats:
        partner_uid = active_chats[uid]
        del active_chats[uid]
        if partner_uid in active_chats:
            del active_chats[partner_uid]
        await update.message.reply_text("🔚 Чат завершён. Напиши /find чтобы найти нового собеседника.")
        try:
            await context.bot.send_message(partner_uid, "🔚 Собеседник завершил чат. Напиши /find чтобы найти нового.")
        except:
            pass
        return

    # ИИ чат
    if uid in ai_chats:
        ai_chats[uid]["ended"] = True
        del ai_chats[uid]
        await update.message.reply_text("🔚 Чат завершён. Напиши /find чтобы найти нового собеседника.")
        return

    # Очередь
    if uid in waiting:
        waiting.remove(uid)
        await update.message.reply_text("❌ Поиск отменён.")
        return

    await update.message.reply_text("Ты сейчас не в чате. Напиши /find чтобы найти собеседника.")

# ─── Обработка сообщений ──────────────────────────────────
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    text = update.message.text
    get_user(uid, name)

    if uid in banned_users:
        return

    # Реальный чат — пересылаем партнёру
    if uid in active_chats:
        partner_uid = active_chats[uid]
        try:
            await context.bot.send_message(partner_uid, f"👤 {text}")
        except:
            pass
        return

    # ИИ чат
    if uid in ai_chats and not ai_chats[uid]["ended"]:
        history = ai_chats[uid]["history"]
        history.append({"role": "user", "content": text})

        if len(history) > 10:
            history = history[-10:]
            ai_chats[uid]["history"] = history

        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": AI_SYSTEM}] + history,
                max_tokens=150
            )
            reply = response.choices[0].message.content
            history.append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply)
        except Exception as e:
            logging.error(f"Groq error: {e}")
        return

    # Не в чате
    await update.message.reply_text("Напиши /find чтобы найти собеседника 👀")

# ─── Админ команды ────────────────────────────────────────
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total = len(users)
    real = sum(1 for u in users.values() if u["total_spent"] >= REAL_CHAT_THRESHOLD)
    active = len(active_chats) // 2
    ai_active = len([u for u in ai_chats.values() if not u["ended"]])
    text = (
        f"📊 <b>Статистика:</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💎 Премиум (реальный чат): {real}\n"
        f"💬 Активных реальных чатов: {active}\n"
        f"🤖 Активных ИИ чатов: {ai_active}\n"
        f"🔍 В очереди: {len(waiting)}\n"
        f"🚫 Забанено: {len(banned_users)}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

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

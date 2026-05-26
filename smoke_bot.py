"""
Телеграм-бот для перекуров в групповом чате.

Установка:
    pip install python-telegram-bot

Запуск (Mac/Linux):
    BOT_TOKEN="твой_токен" python3 smoke_bot.py

Команды:
    /smoke          — начать перекур на 10 минут
    /smoke 15       — начать перекур на 15 минут
    /vote           — запустить голосование (нужно 2 из 4)
    /vote 15        — голосование за 15-минутный перекур
    /stop           — остановить текущий перекур
    /next_smoke     — таймер обратного отсчёта до следующего перекура
    /stats          — статистика перекуров
"""

import asyncio
import os
import json
import logging
import re
from datetime import datetime, timedelta, date
from collections import defaultdict
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import BadRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

chat_states: dict[int, dict] = {}

DEFAULT_BREAK_MINUTES = 10
VOTES_NEEDED = 2
VOTE_TIMEOUT_SECONDS = 180

STATS_FILE = Path(__file__).parent / "smoke_stats.json"


# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def md_escape(text: str) -> str:
    """Экранировать спецсимволы Markdown v1: * _ ` ["""
    return re.sub(r'([*_`\[])', r'\\\1', str(text))


def fmt_delta(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _cancel_task(state: dict, key: str):
    task = state.get(key)
    if task and not task.done():
        task.cancel()
    state[key] = None


# ─── СОХРАНЕНИЕ / ЗАГРУЗКА ────────────────────────────────────────────────────

def _load_stats() -> dict:
    if not STATS_FILE.exists():
        return {}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception as e:
        logger.warning(f"Не удалось загрузить статистику: {e}")
        return {}


def _save_stats():
    data = {}
    for chat_id, state in chat_states.items():
        data[str(chat_id)] = {
            "stats_total_breaks": state["stats_total_breaks"],
            "stats_total_minutes": state["stats_total_minutes"],
            "stats_initiated": dict(state["stats_initiated"]),
            "stats_votes_won": state["stats_votes_won"],
            "stats_votes_lost": state["stats_votes_lost"],
            "stats_today_breaks": state["stats_today_breaks"],
            "stats_today_date": state["stats_today_date"].isoformat(),
        }
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить статистику: {e}")


def _init_stats_from_saved(state: dict, saved: dict):
    state["stats_total_breaks"] = saved.get("stats_total_breaks", 0)
    state["stats_total_minutes"] = saved.get("stats_total_minutes", 0)
    state["stats_initiated"] = defaultdict(int, saved.get("stats_initiated", {}))
    state["stats_votes_won"] = saved.get("stats_votes_won", 0)
    state["stats_votes_lost"] = saved.get("stats_votes_lost", 0)
    state["stats_today_breaks"] = saved.get("stats_today_breaks", 0)
    today_str = saved.get("stats_today_date")
    if today_str:
        try:
            saved_date = date.fromisoformat(today_str)
            if saved_date != datetime.now().date():
                state["stats_today_breaks"] = 0
                state["stats_today_date"] = datetime.now().date()
            else:
                state["stats_today_date"] = saved_date
        except ValueError:
            state["stats_today_date"] = datetime.now().date()
    else:
        state["stats_today_date"] = datetime.now().date()


def _record_break_end(state: dict):
    today = datetime.now().date()
    if state["stats_today_date"] != today:
        state["stats_today_date"] = today
        state["stats_today_breaks"] = 0
    state["stats_total_breaks"] += 1
    state["stats_today_breaks"] += 1
    state["stats_total_minutes"] += state["break_duration"]
    _save_stats()


_saved_stats = _load_stats()


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_states:
        state = {
            # перекур
            "break_active": False,
            "break_end": None,
            "next_smoke": None,
            "break_duration": DEFAULT_BREAK_MINUTES,
            "end_task": None,
            "countdown_task": None,
            "countdown_msg_id": None,
            # голосование
            "vote_active": False,
            "vote_yes": set(),
            "vote_no": set(),
            "vote_msg_id": None,
            "vote_task": None,
            "vote_duration": DEFAULT_BREAK_MINUTES,
            "vote_proposer": "Кто-то",  # всегда присутствует
            # статистика
            "stats_total_breaks": 0,
            "stats_total_minutes": 0,
            "stats_initiated": defaultdict(int),
            "stats_votes_won": 0,
            "stats_votes_lost": 0,
            "stats_today_breaks": 0,
            "stats_today_date": datetime.now().date(),
        }
        if chat_id in _saved_stats:
            _init_stats_from_saved(state, _saved_stats[chat_id])
        chat_states[chat_id] = state
    return chat_states[chat_id]


# ─── ГОЛОСОВАНИЕ — тексты и клавиатура ────────────────────────────────────────

def _build_vote_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚬 Лес гооу", callback_data="vote_yes"),
        InlineKeyboardButton("😞 Не хочу (((", callback_data="vote_no"),
    ]])


def _vote_text(proposer: str, duration: int, yes: int, no: int) -> str:
    return (
        f"🗳 *{md_escape(proposer)}* предлагает перекур на *{duration} мин*!\n\n"
        f"Нужно *{VOTES_NEEDED} голоса* «За».\n"
        f"✅ За: *{yes}*   ❌ Против: *{no}*\n\n"
        f"⏳ Голосование закроется через 3 минуты."
    )


# ─── РУЧНОЙ ЗАПУСК ────────────────────────────────────────────────────────────

async def smoke_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    user = update.effective_user.first_name

    if state["break_active"]:
        await update.message.reply_text("🚬 Перекур уже идёт!")
        return

    duration = DEFAULT_BREAK_MINUTES
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Укажи целое число минут, например: /smoke 15")
            return

    async def reply(text, **kw):
        await update.message.reply_text(text, **kw)

    await _start_break(context, chat_id, duration, started_by=user, reply_func=reply)

    state["stats_initiated"][user] += 1
    _save_stats()


async def _start_break(context, chat_id: int, duration: int, started_by: str, reply_func):
    state = get_state(chat_id)

    _cancel_task(state, "countdown_task")
    _cancel_task(state, "end_task")

    now = datetime.now()
    state["break_active"] = True
    state["break_end"] = now + timedelta(minutes=duration)
    state["break_duration"] = duration

    await reply_func(
        f"🚬 *{md_escape(started_by)}* объявил перекур!\n"
        f"⏱ Длительность: *{duration} мин*\n"
        f"🏁 Конец в: *{state['break_end'].strftime('%H:%M:%S')}*\n\n"
        f"Остановить досрочно: /stop",
        parse_mode="Markdown",
    )

    state["end_task"] = asyncio.create_task(
        _remind_end(context, chat_id, duration)
    )


async def _remind_end(context, chat_id: int, minutes: int):
    await asyncio.sleep(minutes * 60)
    state = get_state(chat_id)
    if not state["break_active"]:
        return

    state["break_active"] = False
    state["break_end"] = None
    state["next_smoke"] = datetime.now() + timedelta(hours=1)
    _record_break_end(state)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⏰ Перекур закончился! Все за работу! 💼\n\n"
            "⏭ Следующий через 1 час — смотри таймер: /next_smoke"
        ),
    )


# ─── ГОЛОСОВАНИЕ ──────────────────────────────────────────────────────────────

async def vote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    user = update.effective_user.first_name

    if state["break_active"]:
        await update.message.reply_text("🚬 Перекур уже идёт!")
        return

    if state["vote_active"]:
        await update.message.reply_text("🗳 Голосование уже идёт!")
        return

    duration = DEFAULT_BREAK_MINUTES
    if context.args:
        try:
            duration = int(context.args[0])
            if duration <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Укажи целое число минут, например: /vote 15")
            return

    state["vote_active"] = True
    state["vote_yes"] = set()
    state["vote_no"] = set()
    state["vote_duration"] = duration
    state["vote_proposer"] = user

    msg = await update.message.reply_text(
        _vote_text(user, duration, 0, 0),
        parse_mode="Markdown",
        reply_markup=_build_vote_keyboard(),
    )
    state["vote_msg_id"] = msg.message_id

    state["vote_task"] = asyncio.create_task(
        _vote_timeout(context, chat_id, msg.message_id)
    )


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    state = get_state(chat_id)

    # Сначала проверяем — если голосование уже закрыто, отвечаем с алертом и выходим
    if not state["vote_active"]:
        await query.answer("Голосование уже закончилось.", show_alert=True)
        return

    # Снять предыдущий голос (можно переголосовать)
    state["vote_yes"].discard(user_id)
    state["vote_no"].discard(user_id)

    if query.data == "vote_yes":
        state["vote_yes"].add(user_id)
    else:
        state["vote_no"].add(user_id)

    await query.answer()  # подтверждаем нажатие без алерта

    yes = len(state["vote_yes"])
    no = len(state["vote_no"])
    duration = state["vote_duration"]
    proposer = state["vote_proposer"]

    # Победа «за»
    if yes >= VOTES_NEEDED:
        state["vote_active"] = False
        _cancel_task(state, "vote_task")
        state["stats_votes_won"] += 1
        _save_stats()

        await query.edit_message_text(
            f"✅ *{yes} из {VOTES_NEEDED}* проголосовали «За» — перекур одобрен!\n"
            f"🚬 Стартуем на *{duration} мин*!",
            parse_mode="Markdown",
        )

        async def send(text, **kw):
            await context.bot.send_message(chat_id=chat_id, text=text, **kw)

        await _start_break(context, chat_id, duration,
                           started_by="Голосование", reply_func=send)
        return

    # Победа «против»
    if no >= VOTES_NEEDED:
        state["vote_active"] = False
        _cancel_task(state, "vote_task")
        state["stats_votes_lost"] += 1
        _save_stats()

        await query.edit_message_text(
            f"🚫 *{no}* проголосовали «Против» — перекур отменён! Работаем 💪",
            parse_mode="Markdown",
        )
        return

    # Голосование продолжается — обновить счётчик
    await query.edit_message_text(
        _vote_text(proposer, duration, yes, no),
        parse_mode="Markdown",
        reply_markup=_build_vote_keyboard(),
    )


async def _vote_timeout(context, chat_id: int, msg_id: int):
    await asyncio.sleep(VOTE_TIMEOUT_SECONDS)
    state = get_state(chat_id)
    if not state["vote_active"]:
        return

    state["vote_active"] = False
    yes = len(state["vote_yes"])
    no = len(state["vote_no"])
    state["stats_votes_lost"] += 1
    _save_stats()

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"⏰ Время вышло. За: *{yes}*, Против: *{no}* — "
                f"перекур не состоялся 😔"
            ),
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning(f"Vote timeout edit failed: {e}")


# ─── СТОП ─────────────────────────────────────────────────────────────────────

async def smoke_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state["break_active"]:
        await update.message.reply_text("Нет активного перекура.")
        return

    _cancel_task(state, "end_task")
    state["break_active"] = False
    state["break_end"] = None
    state["next_smoke"] = datetime.now() + timedelta(hours=1)
    _record_break_end(state)

    await update.message.reply_text(
        "🛑 Перекур остановлен досрочно! Все за работу! 💪\n\n"
        "⏭ Следующий через 1 час — /next_smoke"
    )


# ─── ОБРАТНЫЙ ОТСЧЁТ ──────────────────────────────────────────────────────────

async def next_smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if state["break_active"]:
        if state["break_end"]:
            remaining = state["break_end"] - datetime.now()
            await update.message.reply_text(
                f"🚬 Сейчас идёт перекур! Осталось *{fmt_delta(remaining)}*\n"
                f"Остановить: /stop",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("🚬 Сейчас идёт перекур!")
        return

    if not state["next_smoke"] or state["next_smoke"] <= datetime.now():
        await update.message.reply_text(
            "❓ Следующий перекур не запланирован.\n"
            "Запусти /smoke или /vote"
        )
        return

    _cancel_task(state, "countdown_task")

    remaining = state["next_smoke"] - datetime.now()
    msg = await update.message.reply_text(
        f"⏭ *До следующего перекура:*\n\n"
        f"⏱ `{fmt_delta(remaining)}`",
        parse_mode="Markdown",
    )
    state["countdown_msg_id"] = msg.message_id

    state["countdown_task"] = asyncio.create_task(
        _countdown_ticker(context, chat_id, msg.message_id)
    )


async def _countdown_ticker(context, chat_id: int, msg_id: int):
    state = get_state(chat_id)
    last_text = ""
    try:
        while True:
            await asyncio.sleep(5)
            now = datetime.now()

            if state["break_active"]:
                new_text = "🚬 *Перекур начался!*"
                if new_text != last_text:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=new_text,
                            parse_mode="Markdown",
                        )
                    except BadRequest as e:
                        logger.warning(f"Countdown final edit failed: {e}")
                return

            if not state["next_smoke"] or state["next_smoke"] <= now:
                new_text = "🔔 *Время перекура!* Запускай /smoke или /vote"
                if new_text != last_text:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=new_text,
                            parse_mode="Markdown",
                        )
                    except BadRequest as e:
                        logger.warning(f"Countdown final edit failed: {e}")
                return

            remaining = state["next_smoke"] - now
            new_text = f"⏭ *До следующего перекура:*\n\n⏱ `{fmt_delta(remaining)}`"
            if new_text != last_text:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=new_text,
                        parse_mode="Markdown",
                    )
                    last_text = new_text
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        last_text = new_text  # текст уже такой, просто обновим кэш
                    else:
                        logger.warning(f"Countdown edit failed: {e}")
                        return

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Countdown error: {e}")


# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    total = state["stats_total_breaks"]
    total_min = state["stats_total_minutes"]
    today = state["stats_today_breaks"]
    won = state["stats_votes_won"]
    lost = state["stats_votes_lost"]

    initiated = {k: v for k, v in state["stats_initiated"].items() if v > 0}

    if initiated:
        top = sorted(initiated.items(), key=lambda x: x[1], reverse=True)
        top_lines = (
            "\n🏆 *Кто чаще всех объявлял перекур:*\n" +
            "\n".join(
                f"  {'🥇' if i == 0 else '🥈' if i == 1 else '🥉' if i == 2 else '•'} "
                f"{md_escape(name)} — {count} раз"
                for i, (name, count) in enumerate(top[:5])
            )
        )
    else:
        top_lines = ""

    text = (
        f"📊 *Статистика перекуров*\n\n"
        f"Сегодня: *{today}* перекуров\n"
        f"Всего: *{total}* перекуров\n"
        f"Суммарно покурено: *{total_min} мин*\n\n"
        f"🗳 Голосований выиграно: *{won}*\n"
        f"🗳 Голосований провалено: *{lost}*\n"
        f"{top_lines}"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


# ─── ПОМОЩЬ ───────────────────────────────────────────────────────────────────

async def smoke_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚬 Бот перекуров — команды:\n\n"
        "/smoke — перекур на 10 мин\n"
        "/smoke 15 — перекур на 15 мин\n"
        "/vote — голосование за перекур (нужно 2 из 4)\n"
        "/vote 15 — голосование за 15-минутный перекур\n"
        "/stop — остановить перекур досрочно\n"
        "/next_smoke — таймер до следующего перекура\n"
        "/stats — статистика перекуров",
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Укажи токен бота!\n"
            "  Mac/Linux: BOT_TOKEN='токен' python3 smoke_bot.py"
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("smoke", smoke_start))
    app.add_handler(CommandHandler("vote", vote_start))
    app.add_handler(CommandHandler("stop", smoke_stop))
    app.add_handler(CommandHandler("next_smoke", next_smoke))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", smoke_help))
    app.add_handler(CommandHandler("help", smoke_help))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern="^vote_(yes|no)$"))

    logger.info("Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()

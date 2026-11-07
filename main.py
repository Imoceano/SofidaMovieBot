# ==========================================
# 1. ИМПОРТЫ И ИНИЦИАЛИЗАЦИЯ БОТА
# ==========================================
import asyncio
import logging
import json
import os
import re
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, InputMediaPhoto, InputMediaVideo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TOKEN = "9245319564:AAH206rvLw-gxRAmHbpxyVWR5g40R-6KhzI"
EVENT_CHAT_ID = "-5531382567"  # Точный ID вашей группы
ADMIN_ID = 600968408  # Telegram ID администратора

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_FILE = "events_db.json"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")


# ==========================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (JSON)
# ==========================================
def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("events", {}), data.get("history", [])
        except Exception as e:
            logging.error(f"Ошибка загрузки базы данных: {e}")
    return {}, []

def save_database():
    try:
        data = {
            "events": events_db,
            "history": posts_history
        }
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Ошибка сохранения базы данных: {e}")

events_db, posts_history = load_database()
MAX_PLACES = 20


# ==========================================
# 3. УМНЫЙ ПАРСЕР И НОРМАЛИЗАЦИЯ ДАТ
# ==========================================
MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "юн": 6,
    "юл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12
}

def parse_and_clean_schedule(raw_str: str):
    now = datetime.now(MOSCOW_TZ)
    text = raw_str.strip()
    
    time_match = re.search(r'(\d{1,2})[:\.](\d{2})', text)
    hour, minute = 18, 0
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        text = text.replace(time_match.group(0), "").strip()

    day, month, year = None, None, None

    dot_match = re.search(r'\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b', text)
    if dot_match:
        day = int(dot_match.group(1))
        month = int(dot_match.group(2))
        if dot_match.group(3):
            y_str = dot_match.group(3)
            year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
        text = text.replace(dot_match.group(0), "").strip()
    else:
        for m_key, m_val in MONTHS_RU.items():
            if m_key in text.lower():
                month = m_val
                day_match = re.search(r'\b(\d{1,2})\b', text)
                if day_match:
                    day = int(day_match.group(1))
                    text = text.replace(day_match.group(0), "").strip()
                text = re.sub(m_key + r'[а-я]*', '', text, flags=re.IGNORECASE).strip()
                break

    if not day or not month:
        return None, "", ""

    if not year:
        year = now.year
        try:
            candidate = MOSCOW_TZ.localize(datetime(year, month, day, hour, minute))
            if candidate < now:
                year += 1
        except ValueError:
            return None, "", ""

    try:
        dt = MOSCOW_TZ.localize(datetime(year, month, day, hour, minute))
    except ValueError:
        return None, "", ""

    movie_title = re.sub(r'^[\-\—\–\s]+', '', text).strip()
    if not movie_title:
        movie_title = "Киновечер"

    months_names = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    formatted_date = f"{dt.day} {months_names[dt.month]}, {dt.strftime('%H:%M')}"

    return dt, movie_title, formatted_date


# ==========================================
# 4. СОСТОЯНИЯ FSM (МАШИНА СОСТОЯНИЙ)
# ==========================================
class EventCreation(StatesGroup):
    waiting_for_input = State()  # Ввод расписания
    waiting_for_media = State()  # Загрузка фото/видео (карусель) с возможностью пропуска
    waiting_for_audio = State()  # Пересылка трека из ShazamBot с возможностью пропуска
    waiting_for_text = State()   # Текст анонса
    confirming = State()         # Превью и подтверждение
    editing_text = State()       # Редактирование текста

class PostGeneration(StatesGroup):
    waiting_for_approval = State()


# ==========================================
# 5. АДМИН-ПАНЕЛЬ И ГЛАВНОЕ МЕНЮ
# ==========================================
async def is_user_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        chat_member = await bot.get_chat_member(chat_id=EVENT_CHAT_ID, user_id=user_id)
        if chat_member.status in ["creator", "administrator"]:
            return True
    except TelegramAPIError as e:
        logging.error(f"Ошибка проверки прав: {e}")
    return False

async def show_main_menu(message: Message, user_id: int, user_first_name: str, edit_mode: bool = False):
    builder = InlineKeyboardBuilder()
    is_admin = await is_user_admin(user_id)
    
    if is_admin:
        builder.row(InlineKeyboardButton(text="👑 Панель администратора", callback_data="admin_menu_main"))
        builder.row(InlineKeyboardButton(text="➕ Создать киновечер", callback_data="admin_create_start"))
        builder.row(InlineKeyboardButton(text="📅 Список мероприятий", callback_data="admin_list_events"))
        builder.row(InlineKeyboardButton(text="👥 Список записавшихся", callback_data="admin_list_events"))
        text = f"Привет, организатор {user_first_name}! 🤎 Панель управления киновечерами:"
    else:
        builder.row(InlineKeyboardButton(text="🎬 Записаться на киновечер", callback_data="user_choose_event"))
        builder.row(InlineKeyboardButton(text="📋 Мои записи / Отменить", callback_data="user_my_events"))
        text = f"Привет, {user_first_name}! 🤎 Добро пожаловать на наши киновечера.\nВыберите нужное действие ниже:"
    
    try:
        if edit_mode and message.text != text:
            await message.edit_text(text, reply_markup=builder.as_markup())
        elif not edit_mode:
            await message.answer(text, reply_markup=builder.as_markup())
    except TelegramAPIError:
        pass

async def show_admin_menu(message: Message, edit_mode: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать киновечер", callback_data="admin_create_start"))
    builder.row(InlineKeyboardButton(text="🤖 Создать через ИИ-черновик", callback_data="admin_ai_draft"))
    builder.row(InlineKeyboardButton(text="📅 Список мероприятий", callback_data="admin_list_events"))
    builder.row(InlineKeyboardButton(text="👥 Список записавшихся", callback_data="admin_list_events"))
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_back_to_user_menu"))
    
    text = "👑 <b>Панель администратора киновечеров:</b>\nВыберите действие с помощью кнопок ниже:"
    
    try:
        if edit_mode:
            await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except TelegramAPIError:
        pass

@router.callback_query(F.data == "admin_menu_main")
async def cb_admin_menu_main(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    if callback.message and isinstance(callback.message, Message):
        await show_admin_menu(callback.message, edit_mode=True)

@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if not message.from_user or not await is_user_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора.")
        return
    await show_admin_menu(message, edit_mode=False)

@router.callback_query(F.data == "admin_back_to_menu")
async def admin_back_to_menu(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    if callback.message and isinstance(callback.message, Message):
        await show_admin_menu(callback.message, edit_mode=True)

@router.callback_query(F.data == "admin_back_to_user_menu")
async def admin_back_to_user_menu(callback: CallbackQuery):
    if not callback.from_user:
        return
    if callback.message and isinstance(callback.message, Message):
        await show_main_menu(callback.message, callback.from_user.id, callback.from_user.first_name, edit_mode=True)


# ==========================================
# 6. ПОШАГОВЫЙ СЦЕНАРИЙ СОЗДАНИЯ МЕРОПРИЯТИЙ
# ==========================================

# ИИ-черновик
@router.callback_query(F.data == "admin_ai_draft")
async def cb_generate_draft(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
        
    if not posts_history:
        await callback.answer("😔 У меня пока нет истории прошлых постов. Проведите и опубликуйте хотя бы один киновечер!", show_alert=True)
        return
        
    draft_text = posts_history[-1]
    await state.update_data(generated_draft=draft_text)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Принять и использовать", callback_data="accept_draft"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_draft")
    )
    
    explanation = (
        "🤖 <b>Помощник ИИ:</b> Я подготовил текст на основе ваших прошлых успешных анонсов.\n\n"
        f"-----------------------------------\n{draft_text}\n-----------------------------------"
    )
    
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(explanation, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramAPIError:
            pass
    await state.set_state(PostGeneration.waiting_for_approval)

@router.callback_query(PostGeneration.waiting_for_approval, F.data == "accept_draft")
async def accept_generated_draft(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft_text = data.get("generated_draft")
    
    await state.update_data(event_text=draft_text)
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                "✅ Черновик принят!\n\n🎬 Введите расписание сеансов (например: <i>8 декабря 18:00 Зеленый слоник</i>):",
                parse_mode="HTML"
            )
        except TelegramAPIError:
            pass
    await state.set_state(EventCreation.waiting_for_input)

@router.callback_query(PostGeneration.waiting_for_approval, F.data == "cancel_draft")
async def cancel_generated_draft(callback: CallbackQuery, state: FSMContext):
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("🚫 Генерация отменена.")
        except TelegramAPIError:
            pass
    await state.clear()
    if callback.message and isinstance(callback.message, Message):
        await show_admin_menu(callback.message, edit_mode=False)


# Шаг 1: Ввод расписания
@router.callback_query(F.data == "admin_create_start")
async def cb_create_event_start(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                "🎬 <b>Шаг 1 из 4: Введите расписание сеансов</b>\n\n"
                "• Для одного сеанса отправьте строку:\n<code>8 декабря 18:00 Зеленый слоник</code>\n\n"
                "• Для нескольких сеансов используйте нумерованный список:\n"
                "<code>1. 8 декабря 18:00 Зеленый слоник</code>\n"
                "<code>2. 9 декабря 15:00 Пять бутылок водки</code>",
                parse_mode="HTML"
            )
        except TelegramAPIError:
            pass
    await state.set_state(EventCreation.waiting_for_input)

@router.message(Command("create_event"))
async def cmd_create_event(message: Message, state: FSMContext):
    if not message.from_user or not await is_user_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора.")
        return
    await message.answer(
        "🎬 <b>Шаг 1 из 4: Введите расписание сеансов</b>\n\n"
        "• Для одного сеанса:\n<code>8 декабря 18:00 Зеленый слоник</code>\n\n"
        "• Для нескольких сеансов списком:\n"
        "<code>1. 8 декабря 18:00 Зеленый слоник</code>\n"
        "<code>2. 9 декабря 15:00 Пять бутылок водки</code>",
        parse_mode="HTML"
    )
    await state.set_state(EventCreation.waiting_for_input)

@router.message(EventCreation.waiting_for_input, F.text)
async def process_event_input(message: Message, state: FSMContext):
    if not message.text:
        return
    
    raw_text = message.text.strip()
    parsed_events = []
    
    lines = raw_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line[0].isdigit() and (len(line) > 1 and line[1] in [".", ")", "-"]):
            clean_line = line[2:].strip()
        else:
            clean_line = line
            
        dt, movie_title, nice_date_str = parse_and_clean_schedule(clean_line)
        if not dt:
            await message.answer(f"⚠️ Не удалось распознать дату/время в строке:\n<code>{line}</code>\nПожалуйста, укажите дату понятнее (например: <i>8 декабря 18:00 Название</i>).", parse_mode="HTML")
            return
            
        standard_desc = f"{nice_date_str} — {movie_title}"
        
        parsed_events.append({
            "raw": clean_line,
            "dt_iso": dt.isoformat(),
            "timestamp": dt.timestamp(),
            "formatted": nice_date_str,
            "title_desc": standard_desc
        })
            
    if not parsed_events:
        await message.answer("⚠️ Не удалось распознать сеансы. Пожалуйста, введите расписание еще раз.")
        return
        
    existing_timestamps = [ev_data.get("timestamp") for ev_data in events_db.values()]
    duplicates = [ev for ev in parsed_events if ev["timestamp"] in existing_timestamps]
    
    if duplicates:
        dup_str = "\n".join([f"• <code>{d['title_desc']}</code>" for d in duplicates])
        await message.answer(
            f"❌ <b>Ошибка! Мероприятие на это время уже существует:</b>\n{dup_str}\n\n"
            f"⚠️ На данную дату и время уже есть опубликованное мероприятие. Измените время или дату.",
            parse_mode="HTML"
        )
        return

    await state.update_data(parsed_events=parsed_events, media_files=[], audio_info=None)
    await state.set_state(EventCreation.waiting_for_media)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏭ Пропустить шаг (без медиа)", callback_data="finish_media"))
    
    await message.answer(
        f"🖼 <b>Шаг 2 из 4:</b> Распознано сеансов: <b>{len(parsed_events)}</b> без пересечений.\n"
        f"Отправляйте фото или видеофайлы для карусели (можно смешивать).\n"
        f"<i>Когда закончите, нажмите кнопку ниже:</i>", 
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# Шаг 2: Прием медиа (фото или видео в карусель) с возможностью пропуска
@router.message(EventCreation.waiting_for_media, F.photo | F.video)
async def process_event_media(message: Message, state: FSMContext):
    data = await state.get_data()
    media_files = data.get("media_files", [])
    
    if message.photo:
        media_files.append(("photo", message.photo[-1].file_id))
    elif message.video:
        media_files.append(("video", message.video.file_id))
        
    await state.update_data(media_files=media_files)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Готово, перейти к Shazam-треку", callback_data="finish_media"))
    
    await message.answer(
        f"✅ Медиафайл принят! Всего в карусели: <b>{len(media_files)}</b>.\n"
        f"Можете отправить еще фото/видео или нажать кнопку ниже:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.callback_query(EventCreation.waiting_for_media, F.data == "finish_media")
async def finish_media_collection(callback: CallbackQuery, state: FSMContext):
    if callback.message and isinstance(callback.message, Message):
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="⏭ Пропустить шаг (без трека)", callback_data="skip_audio"))
        try:
            await callback.message.edit_text(
                "🎵 <b>Шаг 3 из 4: Музыка из фильма (Shazam)</b>\n\n"
                "Перешлите сюда карточку трека или аудиосообщение из бота <b>@ShazamBot</b>, чтобы добавить музыкальное сопровождение к анонсу:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except TelegramAPIError:
            pass
            
    await state.set_state(EventCreation.waiting_for_audio)


# Шаг 3: Прием аудио из ShazamBot или пропуск
@router.message(EventCreation.waiting_for_audio, F.audio | F.voice | F.text)
async def process_shazam_audio(message: Message, state: FSMContext):
    audio_data = None
    
    if message.audio:
        audio_data = {"type": "audio", "file_id": message.audio.file_id, "caption": message.caption or message.text}
    elif message.voice:
        audio_data = {"type": "voice", "file_id": message.voice.file_id, "caption": message.caption or message.text}
    elif message.text:
        audio_data = {"type": "text", "text": message.text}
        
    await state.update_data(audio_info=audio_data)
    await state.set_state(EventCreation.waiting_for_text)
    await message.answer("📝 <b>Шаг 4 из 4:</b> Теперь отправьте текст общего поста-анонса:", parse_mode="HTML")

@router.callback_query(EventCreation.waiting_for_audio, F.data == "skip_audio")
async def skip_shazam_audio(callback: CallbackQuery, state: FSMContext):
    await state.update_data(audio_info=None)
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("⏭ Шаг с Shazam-треком пропущен.\n\n📝 <b>Шаг 4 из 4:</b> Теперь отправьте текст общего поста-анонса:", parse_mode="HTML")
        except TelegramAPIError:
            pass
    await state.set_state(EventCreation.waiting_for_text)


# Шаг 4: Ввод текста и превью
@router.message(EventCreation.waiting_for_text, F.text)
async def process_event_text(message: Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(event_text=message.text)
    await show_preview(message, state)

async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    media_files = data.get("media_files", [])
    event_text = data.get("event_text")
    parsed_events = data.get("parsed_events", [])
    audio_info = data.get("audio_info")
    
    events_list_str = "\n".join([f"• {ev['title_desc']}" for ev in parsed_events])
    audio_status = "🎵 С треком Shazam" if audio_info else "❌ Без музыки"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🚀 Опубликовать в группу", callback_data="publish_planned_event"))
    builder.row(InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="edit_preview_text"))
    builder.row(InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_creation"))
    
    preview_caption = (
        f"<b>👀 Превью для группы (Сеансов: {len(parsed_events)}, Медиа в карусели: {len(media_files)}, {audio_status}):</b>\n\n"
        f"{events_list_str}\n\n{event_text}\n\n"
        f"⚙️ <i>Проверьте текст и опубликуйте.</i>"
    )
    
    if media_files:
        m_type, m_id = media_files[0]
        if m_type == "photo":
            await message.answer_photo(photo=m_id, caption=preview_caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await message.answer_video(video=m_id, caption=preview_caption, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(preview_caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        
    await state.set_state(EventCreation.confirming)

@router.callback_query(EventCreation.confirming, F.data == "edit_preview_text")
async def cb_edit_preview_text(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_text = data.get("event_text", "")
    
    if callback.message and isinstance(callback.message, Message):
        await callback.message.answer(
            f"✏️ <b>Редактирование текста анонса:</b>\n\n"
            f"Текущий текст:\n<i>{current_text}</i>\n\n"
            f"👇 Отправьте новый исправленный вариант сообщения следующим сообщением:",
            parse_mode="HTML"
        )
    await state.set_state(EventCreation.editing_text)

@router.message(EventCreation.editing_text, F.text)
async def process_edited_text(message: Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(event_text=message.text)
    await message.answer("✅ Текст успешно обновлен! Вот новое превью:")
    await show_preview(message, state)


# Публикация в группу (смешанная карусель фото+видео)
@router.callback_query(EventCreation.confirming, F.data == "publish_planned_event")
async def publish_planned_event(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parsed_events = data.get("parsed_events", [])
    event_text = data.get("event_text")
    media_files = data.get("media_files", [])
    audio_info = data.get("audio_info")
    
    bot_info = await bot.get_me()
    registration_link = f"https://t.me/{bot_info.username}?start=register"
    
    group_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📝 Записаться через бота", url=registration_link)]]
    )
    
    events_str_group = "\n".join([f"🎬 {ev['title_desc']}" for ev in parsed_events])
    post_content = f"<b>Список запланированных сеансов:</b>\n{events_str_group}\n\n{event_text}"
    
    if audio_info and audio_info["type"] == "text":
        post_content += f"\n\n🎵 <b>Саундтрек вечера:</b>\n{audio_info['text']}"
    
    try:
        group_msg_id = None
        if media_files:
            if len(media_files) == 1:
                m_type, m_id = media_files[0]
                if m_type == "photo":
                    sent_msg = await bot.send_photo(chat_id=EVENT_CHAT_ID, photo=m_id, caption=post_content, reply_markup=group_keyboard, parse_mode="HTML")
                else:
                    sent_msg = await bot.send_video(chat_id=EVENT_CHAT_ID, video=m_id, caption=post_content, reply_markup=group_keyboard, parse_mode="HTML")
                group_msg_id = sent_msg.message_id
            else:
                media_group = []
                for idx, (m_type, m_id) in enumerate(media_files):
                    caption_text = post_content if idx == 0 else None
                    parse_mode_val = "HTML" if idx == 0 else None
                    
                    if m_type == "photo":
                        media_group.append(InputMediaPhoto(media=m_id, caption=caption_text, parse_mode=parse_mode_val))
                    else:
                        media_group.append(InputMediaVideo(media=m_id, caption=caption_text, parse_mode=parse_mode_val))
                
                sent_messages = await bot.send_media_group(chat_id=EVENT_CHAT_ID, media=media_group)
                group_msg_id = sent_messages[0].message_id
                await bot.send_message(chat_id=EVENT_CHAT_ID, text="👇 Нажмите для записи на сеанс:", reply_markup=group_keyboard)
        else:
            sent_msg = await bot.send_message(chat_id=EVENT_CHAT_ID, text=post_content, reply_markup=group_keyboard, parse_mode="HTML")
            group_msg_id = sent_msg.message_id

        if audio_info and audio_info["type"] in ["audio", "voice"]:
            if audio_info["type"] == "audio":
                await bot.send_audio(chat_id=EVENT_CHAT_ID, audio=audio_info["file_id"], caption="🎵 Саундтрек вечера")
            else:
                await bot.send_voice(chat_id=EVENT_CHAT_ID, voice=audio_info["file_id"], caption="🎵 Саундтрек вечера")

        for idx, ev in enumerate(parsed_events):
            event_id = f"ev_{abs(hash(ev['title_desc']))}_{idx}"
            events_db[event_id] = {
                "title_desc": ev["title_desc"],
                "timestamp": ev["timestamp"],
                "text": event_text,
                "group_message_id": group_msg_id,
                "participants": [],
                "waitlist": []
            }
            
        posts_history.append(event_text)
        save_database()

        if callback.message and isinstance(callback.message, Message):
            await callback.message.answer(f"✅ Успешно! Опубликовано сеансов: {len(parsed_events)}.")
    except TelegramAPIError as e:
        if callback.message and isinstance(callback.message, Message):
            try:
                await callback.message.answer(f"❌ Ошибка публикации: {e}")
            except TelegramAPIError:
                pass
        
    await state.clear()
    if callback.message and isinstance(callback.message, Message):
        await show_admin_menu(callback.message, edit_mode=False)

@router.callback_query(EventCreation.confirming, F.data == "cancel_creation")
async def cancel_creation(callback: CallbackQuery, state: FSMContext):
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("🚫 Создание отменено.")
        except TelegramAPIError:
            pass
            
    await state.clear()
    if callback.message and isinstance(callback.message, Message):
        await show_admin_menu(callback.message, edit_mode=False)


# ==========================================
# 7. УПРАВЛЕНИЕ МЕРОПРИЯТИЯМИ И УЧАСТНИКАМИ
# ==========================================
@router.callback_query(F.data == "admin_list_events")
async def cb_admin_list_events(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
        
    if not events_db:
        if callback.message and isinstance(callback.message, Message):
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="⬅️ Назад в админ-меню", callback_data="admin_back_to_menu"))
            try:
                await callback.message.edit_text("ℹ️ Сейчас нет активных мероприятий.", reply_markup=builder.as_markup())
            except TelegramAPIError:
                pass
        return

    builder = InlineKeyboardBuilder()
    text = "📅 <b>Список сеансов и участников:</b>\nВыберите сеанс:"
    
    for ev_id, ev_data in events_db.items():
        count = len(ev_data["participants"])
        wait_count = len(ev_data["waitlist"])
        desc = ev_data.get('title_desc', 'Киновечер')
        builder.row(
            InlineKeyboardButton(text=f"🎬 {desc} — {count}/{MAX_PLACES} [Резерв: {wait_count}]", callback_data=f"list_{ev_id}")
        )
        
    builder.row(InlineKeyboardButton(text="⬅️ Назад в админ-меню", callback_data="admin_back_to_menu"))
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramAPIError:
            pass

@router.callback_query(F.data.startswith("list_"))
async def show_event_participants(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    ev_id = callback.data.replace("list_", "") if callback.data else ""
    ev = events_db.get(ev_id)
    
    if not ev:
        await callback.answer("Сеанс не найден.", show_alert=True)
        return
        
    desc = ev.get('title_desc', 'Киновечер')
    text = f"👥 <b>Участники сеанса:</b>\n{desc}\nОсновной список: {len(ev['participants'])}/{MAX_PLACES} | Резерв: {len(ev['waitlist'])}\n\n"
    builder = InlineKeyboardBuilder()
    
    if not ev["participants"] and not ev["waitlist"]:
        text += "Пока никого нет."
    else:
        text += "<b>Основной состав:</b>\n"
        for p in ev["participants"]:
            uname = f" (@{p['username']})" if p['username'] else ""
            builder.row(
                InlineKeyboardButton(text=f"👤 {p['name']}{uname}", callback_data="none"),
                InlineKeyboardButton(text="❌ Удалить", callback_data=f"kick_{ev_id}_{p['id']}")
            )
        if ev["waitlist"]:
            text += "\n<b>Лист ожидания (резерв):</b>\n"
            for p in ev["waitlist"]:
                builder.row(
                    InlineKeyboardButton(text=f"⏳ [Резерв] {p['name']}", callback_data="none"),
                    InlineKeyboardButton(text="❌ Удалить", callback_data=f"kick_wait_{ev_id}_{p['id']}")
                )
            
    builder.row(InlineKeyboardButton(text="❌ Отменить этот сеанс", callback_data=f"cancel_ev_{ev_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ К списку сеансов", callback_data="admin_list_events"))
    
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except TelegramAPIError:
            pass

@router.callback_query(F.data.startswith("kick_") & ~F.data.startswith("kick_wait_"))
async def kick_participant(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    parts = callback.data.split("_") if callback.data else []
    if len(parts) < 4:
        return
    ev_id = f"{parts[1]}_{parts[2]}_{parts[3]}" if len(parts) > 3 and parts[2].isdigit() else f"{parts[1]}_{parts[2]}"
    try:
        user_id = int(parts[-1])
    except ValueError:
        return
    
    ev = events_db.get(ev_id)
    if ev:
        ev["participants"] = [p for p in ev["participants"] if p["id"] != user_id]
        
        promoted_user = None
        if ev["waitlist"]:
            promoted_user = ev["waitlist"].pop(0)
            ev["participants"].append(promoted_user)
            try:
                success_text = (
                    f"🎉 <b>Освободилось место на сеанс «{ev.get('title_desc')}», и вы перенесены из резерва!</b>\n\n"
                    "Вы записаны! 🤎 Буду рада видеть вас.\n\n"
                    "📍 <b>Адрес:</b> бульвар Чавайна, 36, Дом быта «Смена», 3 этаж, кабинет 304.\n"
                    "Приходите за 5–10 минут до начала."
                )
                await bot.send_message(promoted_user["id"], success_text, parse_mode="HTML")
            except TelegramAPIError:
                pass
                
        save_database()
        await callback.answer("Участник удален, резерв проверен!", show_alert=True)
        try:
            await bot.send_message(user_id, "⚠️ Организатор исключил вас из списка участников сеанса.")
        except TelegramAPIError:
            pass
            
    callback.data = f"list_{ev_id}"
    await show_event_participants(callback)

@router.callback_query(F.data.startswith("kick_wait_"))
async def kick_waitlist_participant(callback: CallbackQuery):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    parts = callback.data.split("_") if callback.data else []
    if len(parts) < 5:
        return
    ev_id = f"{parts[2]}_{parts[3]}_{parts[4]}" if len(parts) > 4 and parts[3].isdigit() else f"{parts[2]}_{parts[3]}"
    try:
        user_id = int(parts[-1])
    except ValueError:
        return
    
    ev = events_db.get(ev_id)
    if ev:
        ev["waitlist"] = [p for p in ev["waitlist"] if p["id"] != user_id]
        save_database()
        await callback.answer("Участник удален из резерва.", show_alert=True)
        
    callback.data = f"list_{ev_id}"
    await show_event_participants(callback)

@router.callback_query(F.data.startswith("cancel_ev_"))
async def cancel_event_handler(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or not await is_user_admin(callback.from_user.id):
        return
    ev_id = callback.data.replace("cancel_ev_", "") if callback.data else ""
    ev = events_db.get(ev_id)
    
    if not ev:
        await callback.answer("Сеанс не найден.", show_alert=True)
        return
        
    all_users = ev["participants"] + ev["waitlist"]
    desc = ev.get('title_desc', 'Киновечер')
    
    if len(all_users) > 0:
        canceled_count = 0
        for p in all_users:
            try:
                await bot.send_message(p["id"], f"❌ К сожалению, сеанс «<b>{desc}</b>» отменен.", parse_mode="HTML")
                canceled_count += 1
            except TelegramAPIError:
                pass
                
        events_db.pop(ev_id, None)
        save_database()
        if callback.message and isinstance(callback.message, Message):
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="👑 В админ-меню", callback_data="admin_back_to_menu"))
            try:
                await callback.message.edit_text(
                    f"✅ Сеанс отменен. Уведомления отправлены <b>{canceled_count}</b> участникам.",
                    reply_markup=builder.as_markup(), parse_mode="HTML"
                )
            except TelegramAPIError:
                pass
    else:
        events_db.pop(ev_id, None)
        save_database()
        if callback.message and isinstance(callback.message, Message):
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="👑 В админ-меню", callback_data="admin_back_to_menu"))
            try:
                await callback.message.edit_text(
                    f"✅ Сеанс «<b>{desc}</b>» успешно удален (на него никто не был записан).",
                    reply_markup=builder.as_markup(), parse_mode="HTML"
                )
            except TelegramAPIError:
                pass


# ==========================================
# 8. ПОЛЬЗОВАТЕЛЬСКИЙ СЦЕНАРИЙ (ЗАПИСЬ И ОТМЕНА)
# ==========================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    if not message.from_user:
        return
    await show_main_menu(message, message.from_user.id, message.from_user.first_name, edit_mode=False)

@router.callback_query(F.data == "user_choose_event")
async def user_choose_event(callback: CallbackQuery):
    if not events_db:
        if callback.message and callback.from_user and isinstance(callback.message, Message):
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="🎬 Записаться на киновечер", callback_data="user_choose_event"))
            builder.row(InlineKeyboardButton(text="📋 Мои записи / Отменить", callback_data="user_my_events"))
            try:
                await callback.message.edit_text(
                    f"😔 На данный момент нет активных сеансов для записи.\n\n"
                    f"Привет, {callback.from_user.first_name}! 🤎 Добро пожаловать на наши киновечера.\nВыберите нужное действие ниже:",
                    reply_markup=builder.as_markup()
                )
            except TelegramAPIError:
                pass
        return
        
    builder = InlineKeyboardBuilder()
    for ev_id, ev_data in events_db.items():
        count = len(ev_data["participants"])
        wait_count = len(ev_data["waitlist"])
        desc = ev_data.get('title_desc', 'Киновечер')
        label = f"🎬 {desc} ({count}/{MAX_PLACES})"
        if wait_count > 0:
            label += f" [Резерв: {wait_count}]"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"book_{ev_id}"))
        
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start"))
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("Выберите сеанс для записи:", reply_markup=builder.as_markup())
        except TelegramAPIError:
            pass

@router.callback_query(F.data.startswith("book_"))
async def process_booking(callback: CallbackQuery):
    ev_id = callback.data.replace("book_", "") if callback.data else ""
    ev = events_db.get(ev_id)
    
    if not ev:
        await callback.answer("Сеанс недоступен.", show_alert=True)
        return
        
    if not callback.from_user:
        return
    user = callback.from_user
    
    if any(p["id"] == user.id for p in ev["participants"]) or any(p["id"] == user.id for p in ev["waitlist"]):
        await callback.answer("Вы уже записаны или находитесь в резерве на этот сеанс! 🤎", show_alert=True)
        return
        
    user_data = {"id": user.id, "name": user.full_name, "username": user.username}
    desc = ev.get('title_desc', 'Киновечер')
    cal_url = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text=Киновечер:+{desc}&details=Киновечер+на+бульваре+Чавайна+36"

    builder = InlineKeyboardBuilder()
    is_waitlist = False
    
    if len(ev["participants"]) >= MAX_PLACES:
        ev["waitlist"].append(user_data)
        wait_pos = len(ev["waitlist"])
        is_waitlist = True
        builder.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_start"))
        if callback.message and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    f"⏳ Свободных мест на «<b>{desc}</b>» нет, но вы добавлены в <b>лист ожидания (резерв) под номером {wait_pos}</b>!\n"
                    f"Если кто-то отменит запись, вы автоматически попадете на сеанс. 🤎",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            except TelegramAPIError:
                pass
    else:
        ev["participants"].append(user_data)
        builder.row(InlineKeyboardButton(text="📅 Добавить в календарь", url=cal_url))
        builder.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_start"))
        
        success_text = (
            f"Вы записаны на сеанс: <b>{desc}</b>! 🤎 Буду рада видеть вас.\n\n"
            "📍 <b>Адрес:</b> бульвар Чавайна, 36, Дом быта «Смена», 3 этаж, кабинет 304.\n"
            "Приходите за 5–10 минут до начала.\n\n"
            "🍂 Будет здорово, если захватите с собой что-нибудь вкусное к чаю!\n"
            "Оплата — донейшн.\n\n"
            "До встречи! 🎬"
        )
        if callback.message and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(success_text, reply_markup=builder.as_markup(), parse_mode="HTML")
            except TelegramAPIError:
                pass

    save_database()

    if ADMIN_ID:
        username_str = f" (@{user.username})" if user.username else ""
        status_label = "⏳ <b>Новая запись в резерв!</b>" if is_waitlist else "👤 <b>Новая запись в основной список!</b>"
        count_info = f"Основных мест: <b>{len(ev['participants'])}/{MAX_PLACES}</b>"
        if is_waitlist:
            count_info += f" | Резерв: <b>{len(ev['waitlist'])}</b>"
            
        admin_msg = (
            f"{status_label}\n"
            f"• Кто: {user.full_name}{username_str}\n"
            f"• Сеанс: {desc}\n"
            f"• {count_info}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="HTML")
        except TelegramAPIError:
            pass

@router.callback_query(F.data == "user_my_events")
async def user_my_events(callback: CallbackQuery):
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    user_events = []
    
    for ev_id, ev_data in events_db.items():
        if any(p["id"] == user_id for p in ev_data["participants"]):
            user_events.append((ev_id, ev_data.get("title_desc", "Киновечер"), "основной"))
        elif any(p["id"] == user_id for p in ev_data["waitlist"]):
            user_events.append((ev_id, ev_data.get("title_desc", "Киновечер"), "резерв"))
            
    if not user_events:
        if callback.message and callback.from_user and isinstance(callback.message, Message):
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="🎬 Записаться на киновечер", callback_data="user_choose_event"))
            builder.row(InlineKeyboardButton(text="📋 Мои записи / Отменить", callback_data="user_my_events"))
            try:
                await callback.message.edit_text(
                    f"У вас нет активных записей.\n\n"
                    f"Привет, {callback.from_user.first_name}! 🤎 Добро пожаловать на наши киновечера.\nВыберите нужное действие ниже:",
                    reply_markup=builder.as_markup()
                )
            except TelegramAPIError:
                pass
        return
        
    builder = InlineKeyboardBuilder()
    for ev_id, desc, status in user_events:
        prefix = "[Резерв] " if status == "резерв" else ""
        builder.row(InlineKeyboardButton(text=f"❌ Отменить ({prefix}{desc})", callback_data=f"unbook_{ev_id}"))
        
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start"))
    if callback.message and isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("Выберите запись для отмены:", reply_markup=builder.as_markup())
        except TelegramAPIError:
            pass

@router.callback_query(F.data.startswith("unbook_"))
async def process_unbooking(callback: CallbackQuery):
    ev_id = callback.data.replace("unbook_", "") if callback.data else ""
    ev = events_db.get(ev_id)
    if not callback.from_user:
        return
    user = callback.from_user
    
    if ev:
        was_in_participants = any(p["id"] == user.id for p in ev["participants"])
        ev["participants"] = [p for p in ev["participants"] if p["id"] != user.id]
        ev["waitlist"] = [p for p in ev["waitlist"] if p["id"] != user.id]
        desc = ev.get('title_desc', 'Киновечер')
        
        promoted_user = None
        if was_in_participants and ev["waitlist"]:
            promoted_user = ev["waitlist"].pop(0)
            ev["participants"].append(promoted_user)
            try:
                await bot.send_message(
                    promoted_user["id"],
                    f"🎉 Место на «{desc}» освободилось, и вы перенесены из резерва в основной список! Адрес: бульвар Чавайна, 36.",
                    parse_mode="HTML"
                )
            except TelegramAPIError:
                pass

        save_database()

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_start"))
        if callback.message and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "Хорошо, спасибо, что предупредили! 🤎 Надеюсь, увидимся на киновечере.",
                    reply_markup=builder.as_markup()
                )
            except TelegramAPIError:
                pass
        
        if ADMIN_ID:
            username_str = f" (@{user.username})" if user.username else ""
            admin_msg = (
                f"👤 <b>Отмена записи!</b>\n"
                f"• Кто: {user.full_name}{username_str}\n"
                f"• Сеанс: {desc}\n"
                f"• Основных мест: <b>{len(ev['participants'])}/{MAX_PLACES}</b>"
            )
            if promoted_user:
                admin_msg += f"\n• <i>Автоматически поднят из резерва: {promoted_user['name']}</i>"
            try:
                await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="HTML")
            except TelegramAPIError:
                pass
    else:
        if callback.message and isinstance(callback.message, Message):
            try:
                await callback.message.edit_text("Сеанс неактивен.")
            except TelegramAPIError:
                pass

@router.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery):
    if not callback.from_user:
        return
    if callback.message and isinstance(callback.message, Message):
        await show_main_menu(callback.message, callback.from_user.id, callback.from_user.first_name, edit_mode=True)


# ==========================================
# 9. ПЛАНОВЩИК ЗАДАЧ (НАПОМИНАНИЯ И ОТЗЫВЫ)
# ==========================================
async def send_daily_reminders():
    for ev in events_db.values():
        if ev["participants"]:
            desc = ev.get('title_desc', 'Киновечер')
            reminder_text = (
                f"🎬 <b>Напоминание о киновечере!</b>\n\n"
                f"Напоминаем, что сегодня состоится сеанс: <b>{desc}</b>!\n"
                f"📍 Адрес: бульвар Чавайна, 36, Дом быта «Смена», 3 этаж, кабинет 304.\n\n"
                f"Если планы поменялись, пожалуйста, отмените запись через бота! 🤎"
            )
            for p in ev["participants"]:
                try:
                    await bot.send_message(p["id"], reminder_text, parse_mode="HTML")
                except TelegramAPIError:
                    pass

async def send_feedback_loop():
    for ev in events_db.values():
        desc = ev.get('title_desc', 'Киновечер')
        feedback_text = f"Привет! 🤎 Как вам вчерашний киновечер ({desc})? Поделитесь впечатлениями, нам очень важно ваше мнение!"
        for p in ev["participants"]:
            try:
                await bot.send_message(p["id"], feedback_text)
            except TelegramAPIError:
                pass


# ==========================================
# 10. ТОЧКА ВХОДА (ЗАПУСК БОТА)
# ==========================================
async def main():
    logging.basicConfig(level=logging.INFO)
    
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="admin", description="👑 Панель администратора"),
        ])
    except TelegramAPIError:
        pass
    
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_daily_reminders, 'cron', hour=13, minute=0)
    scheduler.add_job(send_feedback_loop, 'cron', hour=12, minute=0)
    scheduler.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
import os
import re
import time
import random
import logging

import fitz
import telebot
from telebot import types
from dotenv import load_dotenv

from керне import obp

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv('BOT_TOKEN')
ADMIN_ID    = int(os.getenv('ADMIN_ID', 0))
LOG_CHAT_ID = int(os.getenv('LOG_CHAT_ID', 0))
PDF_PATH    = os.getenv('PDF_PATH', 'Микра тесты.pdf')
DB_PATH     = os.getenv('DB_PATH', 'база данных.txt')

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# user state: {user_id: {count, col, current_ball, list_mistakes, work_for_mistakes, order, obp, list_true}}
users: dict = {}
# parsed questions per user
questions: dict = {}

GREEN = (0.141, 1.0, 0.376)


# ─── PDF parsing ────────────────────────────────────────────────────────────

def _is_green(color) -> bool:
    if not color:
        return False
    return all(abs(color[i] - GREEN[i]) < 0.05 for i in range(3))


def _green_texts(page) -> dict:
    """y0 → text for green-highlighted spans"""
    green_rects = [d['rect'] for d in page.get_drawings() if _is_green(d.get('fill'))]
    words = page.get_text('words')
    result = {}
    for rect in green_rects:
        collected = sorted(
            [(w[1], w[4]) for w in words if rect.intersects(fitz.Rect(w[:4]))],
        )
        if collected:
            result[round(rect.y0)] = ' '.join(t for _, t in collected)
    return result


def parse_pdf(path: str, uid: int) -> None:
    """Parse PDF and fill questions[uid]"""
    questions[uid] = []
    doc = fitz.open(path)

    for page in doc:
        green = _green_texts(page)
        lines = []
        for block in page.get_text('dict')['blocks']:
            if block['type'] != 0:
                continue
            for line in block['lines']:
                text = ' '.join(s['text'] for s in line['spans']).strip()
                if text:
                    lines.append({'text': text, 'y': round(line['bbox'][1])})

        i = 0
        while i < len(lines):
            if not re.match(r'^#\d+$', lines[i]['text']):
                i += 1
                continue

            i += 1
            if i >= len(lines):
                break
            q_text = lines[i]['text'].strip()
            i += 1

            options, answer = [], ''
            while i < len(lines) and not re.match(r'^#\d+$', lines[i]['text']):
                opt = lines[i]['text'].strip()
                if opt:
                    options.append(opt)
                    if any(abs(gy - lines[i]['y']) < 20 for gy in green):
                        answer = opt
                i += 1

            if q_text and options:
                questions[uid].append({
                    'question': q_text,
                    'options':  options,
                    'answers':  [answer] if answer else [],
                })


# ─── Helpers ────────────────────────────────────────────────────────────────

def _init_user(uid: int) -> None:
    users[uid] = {
        'count': 0, 'col': 0, 'current_ball': 0,
        'list_mistakes': [], 'list_true': [],
        'work_for_mistakes': False, 'order': False, 'obp': False,
    }
    questions[uid] = []


def _require_user(uid: int) -> bool:
    """Return False and notify if user not initialised"""
    if uid not in users:
        bot.send_message(uid, 'Нажми /start')
        return False
    return True


def _build_markup(q: dict) -> tuple[types.InlineKeyboardMarkup, str]:
    """Build inline keyboard + option text for a question"""
    markup = types.InlineKeyboardMarkup()
    opt_text = ''
    for i, option in enumerate(q['options']):
        opt_text += f'\n{i + 1}) {option}'
        markup.add(types.InlineKeyboardButton(text=str(i + 1), callback_data=str(i)))
    return markup, opt_text


def _send_question(uid: int, msg_id: int | None = None, edit: bool = True) -> None:
    u = users[uid]
    qs = questions[uid]

    if u['count'] >= len(qs):
        _finish(uid, msg_id)
        return

    q = qs[u['count']]
    markup, opt = _build_markup(q)
    text = (
        f"{u['count'] + 1}) <b>{q['question']}</b>\n"
        f"<i>Варианты ответов:</i>\n<u>{opt}</u>\n"
        f"<pre>Правильных ответов: [{len(q['answers'])}]</pre>"
    )

    if edit and msg_id:
        try:
            bot.edit_message_text(text, uid, msg_id, parse_mode='HTML', reply_markup=markup)
        except Exception as e:
            log.warning('edit failed: %s', e)
    else:
        bot.send_message(uid, text, parse_mode='HTML', reply_markup=markup)


def _finish(uid: int, msg_id: int | None) -> None:
    u = users[uid]
    qs = questions[uid]
    pct = (u['current_ball'] / len(qs) * 100) if qs else 0

    if msg_id:
        try:
            bot.delete_message(uid, msg_id)
        except Exception:
            pass

    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton('работа над ошибками'), types.KeyboardButton('назад'))
    bot.send_message(uid, f'<pre>Тест завершён\nРезультат: {pct:.2f}%</pre>',
                     parse_mode='HTML', reply_markup=kb)

    username = ''  # имя берётся при callback, здесь недоступно
    summary = f'{uid}: результат {pct:.2f}%'
    log.info(summary)

    try:
        with open(DB_PATH, 'a', encoding='UTF-8') as f:
            f.write(f'\n{summary}')
        if LOG_CHAT_ID:
            bot.send_message(LOG_CHAT_ID, summary)
    except Exception as e:
        log.error('finish log error: %s', e)


def _main_keyboard(uid: int) -> None:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add('ЗОЖ', 'Английский', 'База данных', 'ОБП (блок А)')
    bot.send_message(uid, 'Выбери предмет:', reply_markup=kb)


def _format_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add('60 вопросов', 'Все вопросы', 'По порядку', 'С конца', 'назад')
    return kb


def _waiting(uid: int) -> int:
    msg = bot.send_message(uid, '⏳')
    time.sleep(1.5)
    bot.delete_message(uid, msg.message_id)
    return msg.message_id


# ─── Handlers ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.chat.id
    _init_user(uid)

    if uid != ADMIN_ID and LOG_CHAT_ID:
        bot.send_message(LOG_CHAT_ID,
                         f'@{message.from_user.username} нажал /start')

    inline = types.InlineKeyboardMarkup()
    inline.add(types.InlineKeyboardButton(
        'Поделиться ботом', switch_inline_query='тестовый бот ДГМУ'))
    bot.send_message(uid, 'Поделись ботом:', reply_markup=inline)

    _main_keyboard(uid)


@bot.message_handler(content_types=['text'])
def on_text(message):
    uid  = message.chat.id
    text = message.text.strip()

    if not _require_user(uid):
        return

    # ── Главное меню ────────────────────────────────────────────────────────
    if text == 'База данных':
        try:
            _waiting(uid)
            with open(DB_PATH, encoding='UTF-8') as f:
                bot.send_document(uid, f)
        except Exception as e:
            log.error(e)
            bot.send_message(uid, 'Файл недоступен')

    elif text in ('ОБП (блок А)', 'Английский'):
        _init_user(uid)
        if text == 'ОБП (блок А)':
            parse_pdf(PDF_PATH, uid)
        # Английский — пока без источника, вопросы остаются пустыми
        bot.send_message(uid, 'Выбери формат:', reply_markup=_format_keyboard())

    elif text == 'ЗОЖ':
        bot.send_message(uid, 'В разработке...')

    elif text == 'назад':
        _main_keyboard(uid)

    # ── Форматы теста ────────────────────────────────────────────────────────
    elif text == '60 вопросов':
        qs = questions[uid]
        if not qs:
            bot.send_message(uid, 'Нет вопросов'); return
        questions[uid] = random.sample(qs, min(60, len(qs)))
        users[uid]['count'] = 0
        _waiting(uid)
        _send_question(uid, edit=False)

    elif text == 'Все вопросы':
        qs = questions[uid]
        if not qs:
            bot.send_message(uid, 'Нет вопросов'); return
        questions[uid] = random.sample(qs, len(qs))
        users[uid]['count'] = 0
        _waiting(uid)
        _send_question(uid, edit=False)

    elif text == 'По порядку':
        if not questions[uid]:
            bot.send_message(uid, 'Нет вопросов'); return
        users[uid]['count'] = 0
        _send_question(uid, edit=False)

    elif text == 'С конца':
        if not questions[uid]:
            bot.send_message(uid, 'Нет вопросов'); return
        questions[uid] = list(reversed(questions[uid]))
        users[uid]['count'] = 0
        _send_question(uid, edit=False)

    elif text == 'работа над ошибками':
        mistakes = users[uid]['list_mistakes']
        if not mistakes:
            bot.send_message(uid, 'Ошибок нет 🎉'); return
        questions[uid] = mistakes[:]
        users[uid].update({'count': 0, 'work_for_mistakes': True})
        _send_question(uid, edit=False)

    # используем OBP из внешнего модуля
    # (вызывается через 'ОБП (блок А)' выше, но можно расширить)


@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    uid = call.message.chat.id
    mid = call.message.message_id

    if not _require_user(uid):
        return

    u  = users[uid]
    qs = questions[uid]

    if not qs or u['count'] >= len(qs):
        return

    q   = qs[u['count']]
    idx = int(call.data)

    if idx >= len(q['options']):
        return

    selected = q['options'][idx]
    correct  = q['answers']

    # ── Работа над ошибками (просто листаем) ────────────────────────────────
    if u['work_for_mistakes']:
        u['count'] += 1
        _send_question(uid, mid)
        return

    # ── Обычный режим ────────────────────────────────────────────────────────
    if len(correct) == 0:
        # нет правильного ответа — пропускаем
        u['count'] += 1
        _send_question(uid, mid)
        return

    if len(correct) == 1:
        if selected in correct:
            u['current_ball'] += 1
            bot.answer_callback_query(call.id, '✅ Правильно!')
        else:
            u['list_mistakes'].append(q)
            bot.answer_callback_query(call.id, f'❌ Правильно: {correct[0]}', cache_time=4)
        u['count'] += 1
        time.sleep(0.8)
        _send_question(uid, mid)

    else:
        # несколько правильных ответов
        u['list_true'].append(selected)
        u['col'] += 1

        if selected in correct:
            bot.answer_callback_query(call.id, '✅')
        else:
            bot.answer_callback_query(call.id, f'❌ Правильно: {correct}', cache_time=4)

        if u['col'] >= len(correct):
            chosen = u['list_true']
            if set(chosen) == set(correct):
                u['current_ball'] += 1
            elif any(c in correct for c in chosen):
                u['current_ball'] += 0.5
            else:
                u['list_mistakes'].append(q)

            u.update({'count': u['count'] + 1, 'col': 0, 'list_true': []})
            time.sleep(0.8)
            _send_question(uid, mid)


# ─── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    while True:
        try:
            log.info('Бот запущен')
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            log.error('Polling error: %s', e)
            time.sleep(5)

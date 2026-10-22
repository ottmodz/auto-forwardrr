
import os
import time
import html
import sqlite3
import logging
import threading
from collections import defaultdict

import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio
)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = 5989928539
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "users.db")

ADD_CHANNEL_URL = (
    "https://t.me/autoforwardrrbot"
    "?startchannel=true"
    "&admin=post_messages+delete_messages+edit_messages+invite_users+promote_members"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

# ================= RAM-ONLY SETTINGS =================
# Channel settings are intentionally NOT saved to disk.
# They will reset when the process restarts.
sources = {}       # {user_id: {chat_id: title}}
destinations = {}  # {user_id: {chat_id: title}}

pending = {}
state_lock = threading.RLock()


# ================= USER-ID DATABASE =================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


def save_user(user):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO users(user_id, first_name)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name
        """, (user.id, user.first_name or "User"))
        conn.commit()
    finally:
        conn.close()


def get_all_users():
    conn = get_db()
    try:
        return [
            row[0] for row in conn.execute(
                "SELECT user_id FROM users"
            ).fetchall()
        ]
    finally:
        conn.close()


# ================= CHANNEL SETTINGS =================
def add_chat(table, user_id, chat_id, title):
    store = sources if table == "sources" else destinations
    with state_lock:
        store.setdefault(user_id, {})[chat_id] = title or str(chat_id)


def remove_chat(table, user_id, chat_id):
    store = sources if table == "sources" else destinations
    with state_lock:
        return store.get(user_id, {}).pop(chat_id, None) is not None


def get_chats(table, user_id):
    store = sources if table == "sources" else destinations
    with state_lock:
        return list(store.get(user_id, {}).items())


def get_source_owners(chat_id):
    with state_lock:
        return [
            uid for uid, chats in sources.items()
            if chat_id in chats
        ]


def set_pending(user_id, action):
    with state_lock:
        pending[user_id] = action


def get_pending(user_id):
    with state_lock:
        return pending.get(user_id)


def clear_pending(user_id):
    with state_lock:
        pending.pop(user_id, None)


# ================= MENUS =================
def main_menu(user_id):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "➕ Add Source Channel",
            url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "➕ Add Forward Channel",
            url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "⚙️ Manage Channels",
            callback_data="manage"
        ),
        InlineKeyboardButton(
            "📋 My Channel List",
            callback_data="mylist"
        )
    )

    if user_id == ADMIN_ID:
        kb.add(
            InlineKeyboardButton(
                "📣 Admin Broadcast",
                callback_data="broadcast"
            )
        )
    return kb


def manage_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📥 Add Source by ID", callback_data="source_add"),
        InlineKeyboardButton("📤 Add Forward by ID", callback_data="dest_add"),
        InlineKeyboardButton("🗑 Remove Source by ID", callback_data="source_remove"),
        InlineKeyboardButton("🗑 Remove Forward by ID", callback_data="dest_remove"),
        InlineKeyboardButton("⬅️ Main Menu", callback_data="home")
    )
    return kb


# ================= START =================
@bot.message_handler(commands=["start"])
def start(message):
    save_user(message.from_user)

    bot.send_message(
        message.chat.id,
        "🤖 <b>Multi-User Auto Forward Bot</b>\n\n"
        "📥 Add Source Channel — जहाँ से पोस्ट आएगी\n"
        "📤 Add Forward Channel — जहाँ पोस्ट कॉपी होगी\n\n"
        "⚙️ Manage Channels से channel IDs add/remove करें.\n"
        "हर यूज़र की channel settings अलग हैं.\n\n"
        "⚠️ Channel settings RAM में हैं और restart पर reset हो जाएँगी.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    clear_pending(message.from_user.id)
    bot.reply_to(message, "❎ Cancelled.")


# ================= CALLBACKS =================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid = call.from_user.id
    data = call.data

    if data == "home":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏠 Main Menu",
            reply_markup=main_menu(uid)
        )

    elif data == "manage":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⚙️ <b>Manage Channels</b>",
            reply_markup=manage_menu(),
            parse_mode="HTML"
        )

    elif data in ("source_add", "dest_add", "source_remove", "dest_remove"):
        bot.answer_callback_query(call.id)
        set_pending(uid, data)

        bot.send_message(
            call.message.chat.id,
            "अपना Telegram chat ID भेजें.\n\n"
            "Example: <code>-1001234567890</code>\n\n"
            "Source के लिए bot को channel admin बनाएं.\n"
            "Destination के लिए bot को पोस्ट भेजने की permission दें.\n\n"
            "Cancel: /cancel",
            parse_mode="HTML"
        )

    elif data == "mylist":
        bot.answer_callback_query(call.id)
        src = get_chats("sources", uid)
        dst = get_chats("destinations", uid)

        lines = ["📋 <b>Your Channel Settings</b>", "", "📥 <b>Sources</b>"]
        lines.extend(
            f"• {html.escape(title)} — <code>{cid}</code>"
            for cid, title in src
        )
        if not src:
            lines.append("• None")

        lines += ["", "📤 <b>Forward Channels</b>"]
        lines.extend(
            f"• {html.escape(title)} — <code>{cid}</code>"
            for cid, title in dst
        )
        if not dst:
            lines.append("• None")

        bot.send_message(
            call.message.chat.id,
            "\n".join(lines),
            parse_mode="HTML"
        )

    elif data == "broadcast":
        bot.answer_callback_query(call.id)

        if uid != ADMIN_ID:
            bot.send_message(call.message.chat.id, "⛔ Unauthorized.")
            return

        set_pending(uid, "broadcast")
        bot.send_message(
            call.message.chat.id,
            "📣 <b>Admin Broadcast</b>\n\n"
            "Text, photo, video, document या supported media भेजें.\n"
            "यह आपके registered bot users को copy किया जाएगा.\n"
            "Cancel: /cancel",
            parse_mode="HTML"
        )

    else:
        bot.answer_callback_query(call.id, "Unknown option")


# ================= ALBUM BUFFER =================
album_buffer = defaultdict(list)
album_timers = {}
album_lock = threading.Lock()


def media_kind(message):
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    return None, None


def album_caption(message):
    return message.caption or None


def schedule_album_flush(key):
    old = album_timers.get(key)
    if old:
        old.cancel()

    timer = threading.Timer(1.2, flush_album, args=(key,))
    timer.daemon = True
    album_timers[key] = timer
    timer.start()


def flush_album(key):
    with album_lock:
        messages = album_buffer.pop(key, [])
        album_timers.pop(key, None)

    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    source_id = messages[0].chat.id

    # Telegram media groups contain 2–10 media items.
    # Copying albums as an album requires rebuilding media references.
    media = []
    for index, msg in enumerate(messages[:10]):
        kind, file_id = media_kind(msg)
        if not kind:
            continue

        caption = album_caption(msg)
        if index == 0 and caption:
            caption = caption[:1024]

        if kind == "photo":
            item = InputMediaPhoto(
                media=file_id,
                caption=caption
            )
        elif kind == "video":
            item = InputMediaVideo(
                media=file_id,
                caption=caption
            )
        elif kind == "document":
            item = InputMediaDocument(
                media=file_id,
                caption=caption
            )
        elif kind == "audio":
            item = InputMediaAudio(
                media=file_id,
                caption=caption
            )
        else:
            continue

        media.append(item)

    if len(media) < 2:
        # Not enough compatible items to create an album.
        for msg in messages:
            forward_single(msg)
        return

    owners = get_source_owners(source_id)

    for owner_id in owners:
        for dest_id, _title in get_chats("destinations", owner_id):
            try:
                # Rebuild media for every destination because API objects
                # may be modified by the Telegram library.
                outgoing = []
                for index, msg in enumerate(messages[:10]):
                    kind, file_id = media_kind(msg)
                    if not kind:
                        continue

                    caption = album_caption(msg) if index == 0 else None

                    if kind == "photo":
                        outgoing.append(InputMediaPhoto(media=file_id, caption=caption))
                    elif kind == "video":
                        outgoing.append(InputMediaVideo(media=file_id, caption=caption))
                    elif kind == "document":
                        outgoing.append(InputMediaDocument(media=file_id, caption=caption))
                    elif kind == "audio":
                        outgoing.append(InputMediaAudio(media=file_id, caption=caption))

                if len(outgoing) >= 2:
                    bot.send_media_group(
                        chat_id=dest_id,
                        media=outgoing
                    )
                else:
                    for msg in messages:
                        forward_single_to(msg, dest_id)

            except Exception:
                logging.warning(
                    "Album send failed to %s",
                    dest_id,
                    exc_info=True
                )


# ================= FORWARD SINGLE POST =================
def forward_single_to(message, dest_id):
    try:
        bot.copy_message(
            chat_id=dest_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception:
        logging.warning("Copy failed to %s", dest_id, exc_info=True)


def forward_single(message):
    owners = get_source_owners(message.chat.id)

    for owner_id in owners:
        for dest_id, _title in get_chats("destinations", owner_id):
            forward_single_to(message, dest_id)


# ================= CHANNEL POSTS =================
@bot.channel_post_handler(
    content_types=[
        "text", "photo", "video", "document", "audio",
        "animation", "voice", "video_note", "sticker"
    ]
)
def handle_channel_post(message):
    # Group albums are delivered as multiple channel_post updates.
    group_id = getattr(message, "media_group_id", None)

    if group_id and media_kind(message)[0]:
        key = (message.chat.id, group_id)

        with album_lock:
            album_buffer[key].append(message)
            schedule_album_flush(key)
        return

    forward_single(message)


# ================= GROUP POSTS =================
@bot.message_handler(
    content_types=[
        "text", "photo", "video", "document", "audio",
        "animation", "voice", "video_note", "sticker"
    ],
    func=lambda m: m.chat.type in ("group", "supergroup")
)
def handle_group_post(message):
    # Ignore commands and messages sent by bots.
    if message.from_user and message.from_user.is_bot:
        return

    group_id = getattr(message, "media_group_id", None)

    if group_id and media_kind(message)[0]:
        key = (message.chat.id, group_id)

        with album_lock:
            album_buffer[key].append(message)
            schedule_album_flush(key)
        return

    forward_single(message)


# ================= ID ENTRY + ADMIN BROADCAST =================
@bot.message_handler(
    content_types=[
        "text", "photo", "video", "document", "audio",
        "animation", "voice", "video_note", "sticker"
    ]
)
def handle_pending(message):
    uid = message.from_user.id
    action = get_pending(uid)

    if not action:
        return

    # Admin broadcast
    if action == "broadcast":
        if uid != ADMIN_ID:
            clear_pending(uid)
            return

        if message.content_type == "text" and (message.text or "").startswith("/"):
            return

        clear_pending(uid)
        sent = 0
        failed = 0

        for target_id in get_all_users():
            try:
                bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except Exception:
                failed += 1
            time.sleep(0.04)

        bot.send_message(
            message.chat.id,
            f"📣 Broadcast complete\n\n"
            f"✅ Sent: {sent}\n"
            f"⚠️ Failed: {failed}"
        )
        return

    if message.content_type != "text":
        bot.reply_to(message, "⚠️ Please send a numeric chat ID.")
        return

    try:
        chat_id = int(message.text.strip())
    except (ValueError, TypeError):
        bot.reply_to(message, "⚠️ Invalid ID. Numeric ID bhejein.")
        return

    if action in ("source_add", "dest_add"):
        try:
            chat = bot.get_chat(chat_id)
            member = bot.get_chat_member(chat_id, bot.get_me().id)

            if member.status not in ("administrator", "creator"):
                bot.reply_to(
                    message,
                    "⚠️ Bot ko pehle is chat mein admin banayein."
                )
                return

            if action == "source_add":
                add_chat("sources", uid, chat_id, chat.title or str(chat_id))
                response = "✅ Source channel added."
            else:
                add_chat("destinations", uid, chat_id, chat.title or str(chat_id))
                response = "✅ Forward channel added."

            clear_pending(uid)
            bot.reply_to(message, response)

        except Exception:
            logging.warning("Chat verification failed", exc_info=True)
            bot.reply_to(
                message,
                "❌ Chat verify nahi hua. ID aur bot permissions check karein."
            )

    elif action in ("source_remove", "dest_remove"):
        table = "sources" if action == "source_remove" else "destinations"
        removed = remove_chat(table, uid, chat_id)
        clear_pending(uid)
        bot.reply_to(
            message,
            "✅ Removed." if removed else "⚠️ ID list mein nahi mila."
        )


# ================= HEALTH SERVER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        body = b"Auto Forward Bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )
    thread.start()


# ================= MAIN =================
def main():
    init_db()
    start_health_server()

    logging.warning("Auto Forward Bot started")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                allowed_updates=[
                    "message",
                    "channel_post",
                    "callback_query"
                ],
                skip_pending=False
            )
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception("Polling stopped; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
    

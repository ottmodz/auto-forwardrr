import os
import re
import time
import html
import sqlite3
import logging
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo,
    InputMediaDocument, InputMediaAudio
)

# =============== CONFIG ===============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = 5989928539
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "users.db")

ADD_CHANNEL_URL = (
    "https://t.me/autoforwardrrbot?startchannel=true"
    "&admin=post_messages+delete_messages+edit_messages+invite_users+promote_members"
)

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render Environment Variables.")

logging.basicConfig(level=logging.WARNING)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

# Channel settings are RAM-only and reset after restart.
sources = {}
destinations = {}
pending = {}
state_lock = threading.RLock()

# =============== USER DATABASE ===============
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
            r[0] for r in
            conn.execute("SELECT user_id FROM users").fetchall()
        ]
    finally:
        conn.close()

# =============== CHANNEL HELPERS ===============
def get_chats(table, user_id):
    store = sources if table == "sources" else destinations
    with state_lock:
        return list(store.get(user_id, {}).items())

def remove_chat(table, user_id, chat_id):
    store = sources if table == "sources" else destinations
    with state_lock:
        return store.get(user_id, {}).pop(chat_id, None) is not None

def set_pending(user_id, action):
    with state_lock:
        pending[user_id] = action

def get_pending(user_id):
    with state_lock:
        return pending.get(user_id)

def clear_pending(user_id):
    with state_lock:
        pending.pop(user_id, None)

def is_id_registered(chat_id):
    """Check if the ID is already registered globally by ANY user as a Source or Destination."""
    with state_lock:
        return (
            any(chat_id in x for x in sources.values())
            or any(chat_id in x for x in destinations.values())
        )

def add_source(user_id, chat_id, title):
    # Source IDs can be freely reused/added as sources by any user.
    with state_lock:
        sources.setdefault(user_id, {})[chat_id] = title or str(chat_id)

def add_destination(user_id, chat_id, title):
    # Any ID registered globally cannot be added as a destination.
    with state_lock:
        if is_id_registered(chat_id):
            return False
        destinations.setdefault(user_id, {})[chat_id] = title or str(chat_id)
        return True

# =============== MENUS ===============
def main_menu(user_id):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Add Source Channel", url=ADD_CHANNEL_URL),
        InlineKeyboardButton("➕ Add Forward Channel", url=ADD_CHANNEL_URL),
        InlineKeyboardButton("⚙️ Manage Channels", callback_data="manage"),
        InlineKeyboardButton("📋 My Channel List", callback_data="mylist"),
    )
    if user_id == ADMIN_ID:
        kb.add(
            InlineKeyboardButton(
                "📣 Admin Broadcast", callback_data="broadcast"
            )
        )
    return kb

def manage_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "➕ Add Source Channel ID", callback_data="source_add"
        ),
        InlineKeyboardButton(
            "➕ Add Forward Channel ID", callback_data="dest_add"
        ),
        InlineKeyboardButton(
            "🗑 Delete Source Channel ID", callback_data="source_remove"
        ),
        InlineKeyboardButton(
            "🗑 Delete Forward Channel ID", callback_data="dest_remove"
        ),
        InlineKeyboardButton("⬅️ Main Menu", callback_data="home"),
    )
    return kb

# =============== START / CANCEL ===============
@bot.message_handler(commands=["start"])
def start(message):
    if not message.from_user:
        return
    save_user(message.from_user)
    bot.send_message(
        message.chat.id,
        "🤖 <b>Multi-User Auto Forward Bot</b>\n\n"
        "📥 Add Source Channel — receive posts from a source.\n"
        "📤 Add Forward Channel — send copied posts to a destination.\n\n"
        "Use <b>Manage Channels</b> to add or remove channel IDs.\n"
        "Send multiple IDs separated by spaces or new lines.\n\n"
        "⚠️ Channel settings reset after a bot restart.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML",
    )

@bot.message_handler(commands=["cancel"])
def cancel(message):
    clear_pending(message.from_user.id)
    bot.reply_to(message, "❎ Cancelled.")

def channel_list_text(user_id):
    src = get_chats("sources", user_id)
    dst = get_chats("destinations", user_id)
    lines = [
        "📋 <b>Your Channel Settings</b>",
        "",
        "📥 <b>Source Channels</b>",
    ]
    lines += [
        f"• {html.escape(title)} — <code>{cid}</code>"
        for cid, title in src
    ] or ["• None"]
    lines += ["", "📤 <b>Forward Channels</b>"]
    lines += [
        f"• {html.escape(title)} — <code>{cid}</code>"
        for cid, title in dst
    ] or ["• None"]
    return "\n".join(lines)

# =============== CALLBACKS ===============
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid, data = call.from_user.id, call.data

    if data == "home":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id, "🏠 Main Menu",
            reply_markup=main_menu(uid)
        )
        return

    if data == "manage":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id, "⚙️ <b>Manage Channels</b>",
            reply_markup=manage_menu(), parse_mode="HTML"
        )
        return

    if data == "mylist":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id, channel_list_text(uid),
            parse_mode="HTML"
        )
        return

    if data in (
        "source_add", "dest_add",
        "source_remove", "dest_remove"
    ):
        bot.answer_callback_query(call.id)
        set_pending(uid, data)
        prompts = {
            "source_add":
                "➕ <b>Add Source Channel ID</b>\n\n"
                "Send one or more source channel IDs. "
                "The bot must be an administrator in each source.\n\n"
                "Example:\n<code>-1004480271980\n-1005880271980</code>\n\n"
                "Separate IDs with spaces or new lines. Cancel: /cancel",

            "dest_add":
                "➕ <b>Add Forward Channel ID</b>\n\n"
                "Send one or more destination channel IDs. "
                "The bot must have posting permission.\n\n"
                "Example:\n<code>-1004480271980\n-1005880271980</code>\n\n"
                "Any ID already registered globally as a source or destination "
                "cannot be added as a destination. Cancel: /cancel",

            "source_remove":
                "🗑 <b>Delete Source Channel ID</b>\n\n"
                "Send IDs separated by spaces or new lines. Cancel: /cancel",

            "dest_remove":
                "🗑 <b>Delete Forward Channel ID</b>\n\n"
                "Send IDs separated by spaces or new lines. Cancel: /cancel",
        }
        bot.send_message(
            call.message.chat.id, prompts[data], parse_mode="HTML"
        )
        return

    if data == "broadcast":
        bot.answer_callback_query(call.id)
        if uid != ADMIN_ID:
            bot.send_message(call.message.chat.id, "⛔ Unauthorized.")
            return
        set_pending(uid, "broadcast")
        bot.send_message(
            call.message.chat.id,
            "📣 <b>Admin Broadcast</b>\n\n"
            "Send text, photo, video, document, or supported media.\n"
            "It will be copied to registered users and configured "
            "destination chats.\n\nCancel: /cancel",
            parse_mode="HTML",
        )
        return

    bot.answer_callback_query(call.id, "Unknown option")

# =============== ID PARSER ===============
def parse_chat_ids(text):
    parts = [
        p for p in re.split(r"[\s,]+", (text or "").strip()) if p
    ]
    if not parts or any(not re.fullmatch(r"-?\d+", p) for p in parts):
        return None
    return list(dict.fromkeys(int(p) for p in parts))

def verify_chat(chat_id, action):
    chat = bot.get_chat(chat_id)
    member = bot.get_chat_member(chat_id, bot.get_me().id)

    if action == "source_add":
        if member.status not in ("administrator", "creator"):
            raise ValueError("Bot must be admin in source.")
    elif chat.type == "channel":
        if member.status not in ("administrator", "creator"):
            raise ValueError("Bot must be admin in destination.")
        if getattr(member, "can_post_messages", None) is False:
            raise ValueError("Bot cannot post to destination.")
    elif member.status not in ("administrator", "creator", "member"):
        raise ValueError("Bot cannot post to this chat.")
    return chat

def process_id_action(message, action):
    uid = message.from_user.id
    ids = parse_chat_ids(message.text)

    if not ids:
        bot.reply_to(
            message,
            "❌ Invalid format. Send numeric channel IDs separated by "
            "spaces or new lines.\n\nExample:\n"
            "<code>-1004480271980\n-1005880271980</code>",
            parse_mode="HTML",
        )
        return

    added, skipped, failed = [], [], []
    for chat_id in ids:
        if action in ("source_add", "dest_add"):
            try:
                chat = verify_chat(chat_id, action)
                title = getattr(chat, "title", None) or str(chat_id)
                if action == "source_add":
                    add_source(uid, chat_id, title)
                    added.append(f"{chat_id} — Source")
                elif add_destination(uid, chat_id, title):
                    added.append(f"{chat_id} — Forward")
                else:
                    skipped.append(
                        f"{chat_id} — Already registered globally; cannot add as Forward"
                    )
            except Exception:
                logging.warning("Verification failed for %s", chat_id,
                                exc_info=True)
                failed.append(f"{chat_id} — Verification/permission failed")
        else:
            table = "sources" if action == "source_remove" else "destinations"
            if remove_chat(table, uid, chat_id):
                skipped.append(f"{chat_id} — Deleted")
            else:
                skipped.append(f"{chat_id} — Not found in your list")

    clear_pending(uid)
    lines = ["📋 <b>Channel ID Results</b>", ""]
    if added:
        lines += ["✅ <b>Added:</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in added
        ]
    if skipped:
        lines += ["", "ℹ️ <b>Skipped / Removed:</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in skipped
        ]
    if failed:
        lines += ["", "❌ <b>Failed:</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in failed
        ]
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
                              # =============== MEDIA / ALBUM HANDLING ===============
CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio",
    "animation", "voice", "video_note", "sticker",
    "contact", "location", "venue", "poll", "dice"
]

album_buffer = defaultdict(list)
album_timers = {}
album_lock = threading.Lock()

def media_kind(message):
    if message.content_type == "photo" and message.photo:
        return "photo", message.photo[-1].file_id
    if message.content_type == "video" and message.video:
        return "video", message.video.file_id
    if message.content_type == "document" and message.document:
        return "document", message.document.file_id
    if message.content_type == "audio" and message.audio:
        return "audio", message.audio.file_id
    return None, None

def build_media_group(messages):
    messages = sorted(messages, key=lambda m: m.message_id)
    kinds = [media_kind(m)[0] for m in messages]
    kind_set = set(kinds)

    compatible = (
        kind_set.issubset({"photo", "video"})
        or kind_set == {"document"}
        or kind_set == {"audio"}
    )
    if not compatible or not 2 <= len(messages) <= 10:
        return None

    caption_message = next(
        (m for m in messages if getattr(m, "caption", None)), None
    )
    media = []

    for msg in messages:
        kind, file_id = media_kind(msg)
        if not kind:
            return None

        caption = (
            caption_message.caption
            if caption_message and
            msg.message_id == caption_message.message_id
            else None
        )

        if kind == "photo":
            media.append(InputMediaPhoto(file_id, caption=caption))
        elif kind == "video":
            media.append(InputMediaVideo(file_id, caption=caption))
        elif kind == "document":
            media.append(InputMediaDocument(file_id, caption=caption))
        elif kind == "audio":
            media.append(InputMediaAudio(file_id, caption=caption))

    return media

def get_destinations_for_source(source_id):
    result = set()
    with state_lock:
        for owner_id, chat_map in sources.items():
            if source_id in chat_map:
                result.update(destinations.get(owner_id, {}).keys())
    return result

def forward_to_downstream(source_id, message_id):
    """Automatically forward messages further if a destination channel is also used as a source by someone else."""
    downstream_destinations = set()
    with state_lock:
        for owner_id, chat_map in sources.items():
            if source_id in chat_map:
                downstream_destinations.update(destinations.get(owner_id, {}).keys())
    
    for dest_id in downstream_destinations:
        if dest_id == source_id:
            continue
        try:
            sent = bot.copy_message(
                chat_id=dest_id,
                from_chat_id=source_id,
                message_id=message_id
            )
            forward_to_downstream(dest_id, sent.message_id)
        except Exception:
            logging.warning("Downstream copy failed to %s", dest_id, exc_info=True)

def forward_single_to(message, destination_id):
    try:
        sent = bot.copy_message(
            chat_id=destination_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        forward_to_downstream(destination_id, sent.message_id)
    except Exception:
        logging.warning(
            "Copy failed to %s", destination_id, exc_info=True
        )

def forward_single(message):
    for destination_id in get_destinations_for_source(message.chat.id):
        forward_single_to(message, destination_id)

def flush_album(key):
    with album_lock:
        messages = album_buffer.pop(key, [])
        album_timers.pop(key, None)

    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    original_chat_id = messages[0].chat.id
    for destination_id in get_destinations_for_source(original_chat_id):
        try:
            media = build_media_group(messages)
            if media:
                sent_msgs = bot.send_media_group(
                    chat_id=destination_id, media=media
                )
                if sent_msgs:
                    for sm in sent_msgs:
                        forward_to_downstream(destination_id, sm.message_id)
            else:
                for msg in messages:
                    forward_single_to(msg, destination_id)
        except Exception:
            logging.warning(
                "Album forwarding failed to %s",
                destination_id, exc_info=True
            )

def handle_album(message):
    key = (message.chat.id, message.media_group_id)
    with album_lock:
        album_buffer[key].append(message)
        old_timer = album_timers.get(key)
        if old_timer:
            old_timer.cancel()

        timer = threading.Timer(1.3, flush_album, args=(key,))
        timer.daemon = True
        album_timers[key] = timer
        timer.start()

# =============== SOURCE CHANNEL POSTS ===============
@bot.channel_post_handler(content_types=CONTENT_TYPES)
def handle_channel_post(message):
    if getattr(message, "media_group_id", None) and media_kind(message)[0]:
        handle_album(message)
    else:
        forward_single(message)

# =============== SOURCE GROUP POSTS ===============
@bot.message_handler(
    content_types=CONTENT_TYPES,
    func=lambda m: m.chat.type in ("group", "supergroup")
)
def handle_group_post(message):
    if message.from_user and message.from_user.is_bot:
        return

    if message.content_type == "text" and (
        message.text or ""
    ).startswith("/"):
        return

    if getattr(message, "media_group_id", None) and media_kind(message)[0]:
        handle_album(message)
    else:
        forward_single(message)

# =============== ADMIN BROADCAST ===============
def broadcast_message(message):
    targets = set(get_all_users())

    with state_lock:
        for chat_map in destinations.values():
            targets.update(chat_map.keys())

    targets.discard(ADMIN_ID)
    sent = 0
    failed = 0

    for target in targets:
        try:
            bot.copy_message(
                chat_id=target,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.04)

    bot.send_message(
        message.chat.id,
        "📣 <b>Broadcast Complete</b>\n\n"
        f"✅ Sent: <code>{sent}</code>\n"
        f"❌ Failed: <code>{failed}</code>",
        parse_mode="HTML"
    )

# =============== PRIVATE USER INPUT ===============
@bot.message_handler(
    content_types=CONTENT_TYPES,
    func=lambda m: m.chat.type == "private"
)
def handle_pending(message):
    if not message.from_user:
        return

    uid = message.from_user.id
    action = get_pending(uid)

    if not action:
        return

    if message.content_type == "text" and (
        message.text or ""
    ).startswith("/"):
        return

    if action == "broadcast":
        if uid != ADMIN_ID:
            clear_pending(uid)
            return
        clear_pending(uid)
        broadcast_message(message)
        return

    if action in (
        "source_add", "dest_add",
        "source_remove", "dest_remove"
    ):
        if message.content_type != "text":
            bot.reply_to(
                message,
                "❌ Please send channel IDs as text."
            )
            return

        process_id_action(message, action)

# =============== RENDER HEALTH SERVER ===============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT), HealthHandler
    )
    server.serve_forever()

# =============== START BOT ===============
if __name__ == "__main__":
    init_db()

    threading.Thread(
        target=run_health_server,
        daemon=True
    ).start()

    logging.warning("Auto Forward Bot started.")

    while True:
        try:
            bot.infinity_polling(
                skip_pending=False,
                timeout=30,
                long_polling_timeout=30
            )
        except Exception:
            logging.exception("Polling stopped; restarting in 5 seconds.")
            time.sleep(5)

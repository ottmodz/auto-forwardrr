
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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio
)

# =============== CONFIGURATION ===============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = 5989928539
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "users.db")

ADD_CHANNEL_URL = (
    "https://t.me/autoforwardrrbot?startchannel=true"
    "&admin=post_messages+delete_messages+edit_messages+invite_users+promote_members"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

state_lock = threading.RLock()
sources = {}
destinations = {}
pending = {}


# =============== DATABASE ===============
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL
                    CHECK(kind IN ('source', 'destination')),
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, chat_id, kind)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_settings_chat
            ON channel_settings(chat_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                user_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()

    load_settings()


def load_settings():
    new_sources = defaultdict(dict)
    new_destinations = defaultdict(dict)
    new_pending = {}

    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT user_id, chat_id, kind, title
            FROM channel_settings
        """).fetchall()

        for user_id, chat_id, kind, title in rows:
            target = (
                new_sources if kind == "source"
                else new_destinations
            )
            target[user_id][chat_id] = title

        rows = conn.execute(
            "SELECT user_id, action FROM pending_actions"
        ).fetchall()

        for user_id, action in rows:
            new_pending[user_id] = action
    finally:
        conn.close()

    with state_lock:
        sources.clear()
        sources.update(dict(new_sources))

        destinations.clear()
        destinations.update(dict(new_destinations))

        pending.clear()
        pending.update(new_pending)

    logging.info("Persistent channel settings loaded.")


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


def save_channel(user_id, chat_id, kind, title):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO channel_settings(user_id, chat_id, kind, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id, kind)
            DO UPDATE SET title=excluded.title
        """, (user_id, chat_id, kind, title or str(chat_id)))
        conn.commit()
    finally:
        conn.close()


def delete_channel(user_id, chat_id, kind):
    conn = get_db()
    try:
        cur = conn.execute("""
            DELETE FROM channel_settings
            WHERE user_id=? AND chat_id=? AND kind=?
        """, (user_id, chat_id, kind))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# =============== PENDING ACTIONS ===============
def set_pending(user_id, action):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pending_actions(user_id, action)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET action=excluded.action
        """, (user_id, action))
        conn.commit()
    finally:
        conn.close()

    with state_lock:
        pending[user_id] = action


def get_pending(user_id):
    with state_lock:
        return pending.get(user_id)


def clear_pending(user_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM pending_actions WHERE user_id=?",
            (user_id,)
        )
        conn.commit()
    finally:
        conn.close()

    with state_lock:
        pending.pop(user_id, None)


# =============== CHANNEL OPERATIONS ===============
def get_chats(table, user_id):
    store = sources if table == "sources" else destinations
    with state_lock:
        return list(store.get(user_id, {}).items())


def add_source(user_id, chat_id, title):
    # Multiple users may use the same channel as a source.
    with state_lock:
        save_channel(user_id, chat_id, "source", title)
        sources.setdefault(user_id, {})[chat_id] = title or str(chat_id)


def add_destination(user_id, chat_id, title):
    # A destination ID cannot already exist as a source/destination.
    with state_lock:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")

            exists = conn.execute("""
                SELECT 1 FROM channel_settings
                WHERE chat_id=?
                LIMIT 1
            """, (chat_id,)).fetchone()

            if exists:
                conn.rollback()
                return False

            conn.execute("""
                INSERT INTO channel_settings(
                    user_id, chat_id, kind, title
                ) VALUES (?, ?, 'destination', ?)
            """, (user_id, chat_id, title or str(chat_id)))

            conn.commit()
            destinations.setdefault(user_id, {})[chat_id] = (
                title or str(chat_id)
            )
            return True
        finally:
            conn.close()


def remove_chat(table, user_id, chat_id):
    kind = "source" if table == "sources" else "destination"
    store = sources if kind == "source" else destinations

    with state_lock:
        if chat_id not in store.get(user_id, {}):
            return False

        if not delete_channel(user_id, chat_id, kind):
            return False

        store[user_id].pop(chat_id, None)

        if not store[user_id]:
            store.pop(user_id, None)

        return True


# =============== MENUS ===============
def main_menu(user_id):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "➕ Add Source Channel", url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "➕ Add Forward Channel", url=ADD_CHANNEL_URL
        ),
        InlineKeyboardButton(
            "⚙️ Manage Channels", callback_data="manage"
        ),
        InlineKeyboardButton(
            "📋 My Channel List", callback_data="mylist"
        )
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
            "➕ Add Source ID", callback_data="source_add"
        ),
        InlineKeyboardButton(
            "➕ Add Forward ID", callback_data="dest_add"
        ),
        InlineKeyboardButton(
            "🗑 Delete Source ID", callback_data="source_remove"
        ),
        InlineKeyboardButton(
            "🗑 Delete Forward ID", callback_data="dest_remove"
        ),
        InlineKeyboardButton(
            "⬅️ Main Menu", callback_data="home"
        )
    )
    return kb


def channel_list_text(user_id):
    src = get_chats("sources", user_id)
    dst = get_chats("destinations", user_id)

    lines = [
        "📋 <b>Your Channel Settings</b>",
        "",
        "📥 <b>Source Channels</b>"
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
        "📤 Add Forward Channel — copy posts to a destination.\n\n"
        "⚙️ Manage Channels to add or remove IDs.\n"
        "💾 Channel settings are saved in the database.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    if message.from_user:
        clear_pending(message.from_user.id)

    bot.reply_to(message, "❎ Cancelled.")


# =============== CALLBACK HANDLER ===============
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid = call.from_user.id
    data = call.data
    chat_id = call.message.chat.id if call.message else uid

    bot.answer_callback_query(call.id)

    if data == "home":
        bot.send_message(
            chat_id, "🏠 Main Menu",
            reply_markup=main_menu(uid)
        )
        return

    if data == "manage":
        bot.send_message(
            chat_id,
            "⚙️ <b>Manage Channels</b>",
            reply_markup=manage_menu(),
            parse_mode="HTML"
        )
        return

    if data == "mylist":
        bot.send_message(
            chat_id, channel_list_text(uid), parse_mode="HTML"
        )
        return

    if data in (
        "source_add", "dest_add",
        "source_remove", "dest_remove"
    ):
        set_pending(uid, data)

        prompts = {
            "source_add":
                "➕ <b>Add Source IDs</b>\n\n"
                "Send channel IDs separated by spaces or new lines.\n"
                "The bot must be an administrator in each source.\n"
                "Cancel: /cancel",

            "dest_add":
                "➕ <b>Add Forward IDs</b>\n\n"
                "Send destination IDs separated by spaces or new lines.\n"
                "An ID already registered anywhere cannot be added "
                "as a destination.\nCancel: /cancel",

            "source_remove":
                "🗑 <b>Delete Source IDs</b>\n\n"
                "Send IDs separated by spaces or new lines.\n"
                "Cancel: /cancel",

            "dest_remove":
                "🗑 <b>Delete Forward IDs</b>\n\n"
                "Send IDs separated by spaces or new lines.\n"
                "Cancel: /cancel"
        }

        bot.send_message(
            chat_id, prompts[data], parse_mode="HTML"
        )
        return

    if data == "broadcast":
        if uid != ADMIN_ID:
            bot.send_message(chat_id, "⛔ Unauthorized.")
            return

        set_pending(uid, "broadcast")
        bot.send_message(
            chat_id,
            "📣 <b>Admin Broadcast</b>\n\n"
            "Send the message to broadcast to registered users "
            "and destination chats.\nCancel: /cancel",
            parse_mode="HTML"
    )
    
            CREATE TABLE IF NOT EXISTS channel_settings (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL
                    CHECK(kind IN ('source', 'destination')),
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, chat_id, kind)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_settings_chat
            ON channel_settings(chat_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                user_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

    load_settings()


def load_settings():
    new_sources = defaultdict(dict)
    new_destinations = defaultdict(dict)
    new_pending = {}

    conn = get_db()
    try:
        for uid, cid, kind, title in conn.execute(
            "SELECT user_id, chat_id, kind, title FROM channel_settings"
        ).fetchall():
            target = new_sources if kind == "source" else new_destinations
            target[uid][cid] = title

        for uid, action in conn.execute(
            "SELECT user_id, action FROM pending_actions"
        ).fetchall():
            new_pending[uid] = action
    finally:
        conn.close()

    with state_lock:
        sources.clear()
        sources.update(dict(new_sources))
        destinations.clear()
        destinations.update(dict(new_destinations))
        pending.clear()
        pending.update(new_pending)


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


def save_channel(uid, cid, kind, title):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO channel_settings(user_id, chat_id, kind, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id, kind)
            DO UPDATE SET title=excluded.title
        """, (uid, cid, kind, title or str(cid)))
        conn.commit()
    finally:
        conn.close()


def delete_channel(uid, cid, kind):
    conn = get_db()
    try:
        cur = conn.execute("""
            DELETE FROM channel_settings
            WHERE user_id=? AND chat_id=? AND kind=?
        """, (uid, cid, kind))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_pending(uid, action):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO pending_actions(user_id, action)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET action=excluded.action
        """, (uid, action))
        conn.commit()
    finally:
        conn.close()

    with state_lock:
        pending[uid] = action


def get_pending(uid):
    with state_lock:
        return pending.get(uid)


def clear_pending(uid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (uid,))
        conn.commit()
    finally:
        conn.close()

    with state_lock:
        pending.pop(uid, None)


def get_chats(table, uid):
    store = sources if table == "sources" else destinations
    with state_lock:
        return list(store.get(uid, {}).items())


def add_source(uid, cid, title):
    with state_lock:
        save_channel(uid, cid, "source", title)
        sources.setdefault(uid, {})[cid] = title or str(cid)


def add_destination(uid, cid, title):
    # Forward IDs must be globally unused in either role.
    with state_lock:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM channel_settings WHERE chat_id=? LIMIT 1",
                (cid,)
            ).fetchone()

            if exists:
                conn.rollback()
                return False

            conn.execute("""
                INSERT INTO channel_settings(user_id, chat_id, kind, title)
                VALUES (?, ?, 'destination', ?)
            """, (uid, cid, title or str(cid)))
            conn.commit()

            destinations.setdefault(uid, {})[cid] = title or str(cid)
            return True
        finally:
            conn.close()


def remove_chat(table, uid, cid):
    kind = "source" if table == "sources" else "destination"
    store = sources if kind == "source" else destinations

    with state_lock:
        if cid not in store.get(uid, {}):
            return False

        if not delete_channel(uid, cid, kind):
            return False

        store[uid].pop(cid, None)
        if not store[uid]:
            store.pop(uid, None)
        return True


# =============== MENUS ===============
def main_menu(uid):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Add Source Channel", url=ADD_CHANNEL_URL),
        InlineKeyboardButton("➕ Add Forward Channel", url=ADD_CHANNEL_URL),
        InlineKeyboardButton("⚙️ Manage Channels", callback_data="manage"),
        InlineKeyboardButton("📋 My Channel List", callback_data="mylist")
    )
    if uid == ADMIN_ID:
        kb.add(
            InlineKeyboardButton("📣 Admin Broadcast", callback_data="broadcast")
        )
    return kb


def manage_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Add Source ID", callback_data="source_add"),
        InlineKeyboardButton("➕ Add Forward ID", callback_data="dest_add"),
        InlineKeyboardButton("🗑 Delete Source ID", callback_data="source_remove"),
        InlineKeyboardButton("🗑 Delete Forward ID", callback_data="dest_remove"),
        InlineKeyboardButton("⬅️ Main Menu", callback_data="home")
    )
    return kb


def channel_list_text(uid):
    src = get_chats("sources", uid)
    dst = get_chats("destinations", uid)
    lines = ["📋 <b>Your Channel Settings</b>", "", "📥 <b>Source Channels</b>"]
    lines += [
        f"• {html.escape(title)} — <code>{cid}</code>" for cid, title in src
    ] or ["• None"]
    lines += ["", "📤 <b>Forward Channels</b>"]
    lines += [
        f"• {html.escape(title)} — <code>{cid}</code>" for cid, title in dst
    ] or ["• None"]
    return "\n".join(lines)


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
        "📤 Add Forward Channel — copy posts to a destination.\n\n"
        "⚙️ Manage Channels to add or remove IDs.\n"
        "💾 Settings are saved in the database.",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="HTML"
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    if message.from_user:
        clear_pending(message.from_user.id)
    bot.reply_to(message, "❎ Cancelled.")


# =============== CALLBACKS ===============
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid = call.from_user.id
    data = call.data
    chat_id = call.message.chat.id if call.message else uid
    bot.answer_callback_query(call.id)

    if data == "home":
        bot.send_message(chat_id, "🏠 Main Menu", reply_markup=main_menu(uid))
    elif data == "manage":
        bot.send_message(
            chat_id, "⚙️ <b>Manage Channels</b>",
            reply_markup=manage_menu(), parse_mode="HTML"
        )
    elif data == "mylist":
        bot.send_message(
            chat_id, channel_list_text(uid), parse_mode="HTML"
        )
    elif data in ("source_add", "dest_add", "source_remove", "dest_remove"):
        set_pending(uid, data)
        prompts = {
            "source_add": "➕ <b>Add Source IDs</b>\nSend IDs separated by spaces/new lines. Bot must be admin in each source.\n/cancel",
            "dest_add": "➕ <b>Add Forward IDs</b>\nSend IDs separated by spaces/new lines. An ID already registered anywhere cannot be added as a Forward ID.\n/cancel",
            "source_remove": "🗑 <b>Delete Source IDs</b>\nSend IDs separated by spaces/new lines.\n/cancel",
            "dest_remove": "🗑 <b>Delete Forward IDs</b>\nSend IDs separated by spaces/new lines.\n/cancel"
        }
        bot.send_message(chat_id, prompts[data], parse_mode="HTML")
    elif data == "broadcast":
        if uid != ADMIN_ID:
            bot.send_message(chat_id, "⛔ Unauthorized.")
            return
        set_pending(uid, "broadcast")
        bot.send_message(
            chat_id,
            "📣 <b>Admin Broadcast</b>\nSend a message to broadcast.\n/cancel",
            parse_mode="HTML"
        )


# =============== ID PARSING / PERMISSIONS ===============
def parse_chat_ids(text):
    parts = [p for p in re.split(r"[\s,]+", (text or "").strip()) if p]
    if not parts or any(not re.fullmatch(r"-?\d+", p) for p in parts):
        return None
    return list(dict.fromkeys(int(p) for p in parts))


def verify_chat(cid, action):
    chat = bot.get_chat(cid)
    member = bot.get_chat_member(cid, bot.get_me().id)

    if action == "source_add":
        if member.status not in ("administrator", "creator"):
            raise ValueError("Bot must be admin in source.")
    elif chat.type == "channel":
        if member.status not in ("administrator", "creator"):
            raise ValueError("Bot must be admin in destination.")
        if getattr(member, "can_post_messages", None) is False:
            raise ValueError("Bot cannot post in destination.")
    elif member.status not in ("administrator", "creator", "member"):
        raise ValueError("No destination access.")

    return chat


def process_id_action(message, action):
    uid = message.from_user.id
    ids = parse_chat_ids(message.text)

    if not ids:
        bot.reply_to(
            message,
            "❌ Invalid IDs. Send numeric IDs separated by spaces/new lines."
        )
        return

    added, notes, failed = [], [], []

    for cid in ids:
        if action in ("source_add", "dest_add"):
            try:
                chat = verify_chat(cid, action)
                title = getattr(chat, "title", None) or str(cid)

                if action == "source_add":
                    add_source(uid, cid, title)
                    added.append(f"{cid} — Source")
                elif add_destination(uid, cid, title):
                    added.append(f"{cid} — Forward")
                else:
                    notes.append(f"{cid} — Already registered; cannot add as Forward")
            except Exception:
                logging.warning("Failed to verify %s", cid, exc_info=True)
                failed.append(f"{cid} — Access or permission check failed")
        else:
            table = "sources" if action == "source_remove" else "destinations"
            if remove_chat(table, uid, cid):
                notes.append(f"{cid} — Deleted")
            else:
                notes.append(f"{cid} — Not in your list")

    clear_pending(uid)

    lines = ["📋 <b>Channel ID Results</b>", ""]
    if added:
        lines += ["✅ <b>Added</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in added
        ]
    if notes:
        lines += ["", "ℹ️ <b>Skipped / Removed</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in notes
        ]
    if failed:
        lines += ["", "❌ <b>Failed</b>"] + [
            f"• <code>{html.escape(x)}</code>" for x in failed
        ]

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

import sqlite3
import logging
import time
import random
import string
import os
from dotenv import load_dotenv # .env ফাইল লোড করার জন্য
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, ConversationHandler
from telegram.error import BadRequest, Forbidden # Specific error handling
from telegram.helpers import escape_markdown # MarkdownV2 এর জন্য, কিন্তু Markdown এর জন্যও কিছু ক্ষেত্রে ব্যবহার করা যেতে পারে

# .env ফাইল থেকে ভ্যারিয়েবল লোড করুন
load_dotenv()

# --- কনফিগারেশন ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_ID_STR = os.environ.get("ADMIN_USER_ID")
TELEGRAM_CHANNEL_ID_STR = os.environ.get("TELEGRAM_CHANNEL_ID")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME")
DB_NAME = os.environ.get("DB_PATH") # Render.com এ ডিস্ক পাথ ব্যবহার করা হবে

REFERRAL_PERCENTAGE = 0.10
POINTS_TO_TAKA_RATE = 0.1

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# অত্যাবশ্যকীয় ভ্যারিয়েবল চেক
if not BOT_TOKEN:
    logger.critical("ত্রুটি: BOT_TOKEN এনভায়রনমেন্ট ভ্যারিয়েবল সেট করা হয়নি!")
    exit()
if not ADMIN_USER_ID_STR:
    logger.critical("ত্রুটি: ADMIN_USER_ID এনভায়রনমেন্ট ভ্যারিয়েবল সেট করা হয়নি!")
    exit()
ADMIN_ID = int(ADMIN_USER_ID_STR)

if not TELEGRAM_CHANNEL_ID_STR:
    logger.warning("TELEGRAM_CHANNEL_ID সেট করা নেই, চ্যানেল জয়েন ফিচার কাজ নাও করতে পারে।")
    CHANNEL_ID = 0 # একটি ডিফল্ট ফলব্যাক
else:
    CHANNEL_ID = int(TELEGRAM_CHANNEL_ID_STR)

if not CHANNEL_USERNAME:
    logger.warning("CHANNEL_USERNAME সেট করা নেই, চ্যানেল জয়েন ফিচার কাজ নাও করতে পারে।")
    CHANNEL_USERNAME = "" # একটি ডিফল্ট ফলব্যাক

if not DB_NAME:
    logger.warning("DB_PATH এনভায়রনমেন্ট ভ্যারিয়েবল সেট করা হয়নি, ডিফল্ট 'youtube_bot.db' ব্যবহৃত হচ্ছে।")
    DB_NAME = "youtube_bot.db"


logger.info(f"BOT_TOKEN: Loaded (partially hidden)")
logger.info(f"ADMIN_ID: {ADMIN_ID}")
logger.info(f"CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"CHANNEL_USERNAME: {CHANNEL_USERNAME}")
logger.info(f"DB_NAME: {DB_NAME}")

if CHANNEL_ID == 0 or not CHANNEL_USERNAME:
    logger.critical("গুরুত্বপূর্ণ: CHANNEL_ID অথবা CHANNEL_USERNAME সঠিকভাবে সেট করা হয়নি। বট সঠিকভাবে কাজ নাও করতে পারে।")

ASK_BKASH_NUMBER, ASK_WITHDRAW_POINTS = range(2)

# --- Database Functions (আগের মতোই, কোনো পরিবর্তন নেই) ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, points INTEGER DEFAULT 0,
        referral_code TEXT UNIQUE, referred_by INTEGER, channel_joined BOOLEAN DEFAULT 0,
        watching_video_id INTEGER, video_start_time INTEGER
    )''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS videos (
        video_id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_link TEXT UNIQUE,
        duration_seconds INTEGER, points_reward INTEGER
    )''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS withdrawal_requests (
        request_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bkash_number TEXT,
        points_withdrawn INTEGER, amount_taka REAL, status TEXT DEFAULT 'pending',
        request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    logger.info("Database initialized/checked successfully.")

def generate_referral_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def add_user(user_id, username, referred_by_code=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    new_referral_code = generate_referral_code()
    referrer_id = None
    if referred_by_code:
        try:
            cursor.execute("SELECT user_id FROM users WHERE referral_code = ?", (referred_by_code,))
            referrer = cursor.fetchone()
            if referrer: referrer_id = referrer[0]
            else: logger.warning(f"Referral code {referred_by_code} not found.")
        except Exception as e: logger.error(f"Error finding referrer for {referred_by_code}: {e}")
    try:
        cursor.execute("INSERT INTO users (user_id, username, referral_code, referred_by, channel_joined) VALUES (?, ?, ?, ?, ?)",
                       (user_id, username, new_referral_code, referrer_id, 0))
        conn.commit()
        logger.info(f"User {user_id} ({username}) added. Ref code: {new_referral_code}. Referred by: {referrer_id}")
    except sqlite3.IntegrityError:
        logger.info(f"User {user_id} ({username}) already exists.")
        cursor.execute("SELECT referral_code, referred_by FROM users WHERE user_id = ?", (user_id,))
        ex_user = cursor.fetchone()
        if ex_user and not ex_user[0]: 
            cursor.execute("UPDATE users SET referral_code = ? WHERE user_id = ?", (new_referral_code, user_id))
            conn.commit()
            logger.info(f"Generated missing referral code for existing user {user_id}.")
        if ex_user and referred_by_code and not ex_user[1] and referrer_id and referrer_id != user_id: 
            cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL", (referrer_id, user_id))
            conn.commit()
            logger.info(f"Applied new referral {referrer_id} to existing user {user_id}.")
    except Exception as e: logger.error(f"Error in add_user for {user_id}: {e}", exc_info=True)
    finally: conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, username, points, referral_code, referred_by, channel_joined, watching_video_id, video_start_time FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        if user: return {"user_id": user[0], "username": user[1], "points": user[2], "referral_code": user[3], "referred_by": user[4], "channel_joined": bool(user[5]), "watching_video_id": user[6], "video_start_time": user[7]}
        return None
    except Exception as e: logger.error(f"Error getting user {user_id}: {e}", exc_info=True); return None
    finally: conn.close()

def update_user_points(user_id, points_to_add):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points_to_add, user_id))
        conn.commit()
    except Exception as e: logger.error(f"Error updating points for user {user_id}: {e}", exc_info=True)
    finally: conn.close()

def set_channel_joined_status(user_id, status: bool):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET channel_joined = ? WHERE user_id = ?", (int(status), user_id))
        conn.commit()
        logger.info(f"Set channel_joined for user {user_id} to {status}.")
    except Exception as e: logger.error(f"Error setting channel_joined for {user_id}: {e}", exc_info=True)
    finally: conn.close()

def set_watching_video(user_id, video_id, start_time):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET watching_video_id = ?, video_start_time = ? WHERE user_id = ?", (video_id, start_time, user_id))
        conn.commit()
    except Exception as e: logger.error(f"Error setting watching video for {user_id}: {e}", exc_info=True)
    finally: conn.close()

def clear_watching_video(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET watching_video_id = NULL, video_start_time = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception as e: logger.error(f"Error clearing watching video for {user_id}: {e}", exc_info=True)
    finally: conn.close()

def add_video(youtube_link, duration_seconds, points_reward):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO videos (youtube_link, duration_seconds, points_reward) VALUES (?, ?, ?)", (youtube_link, duration_seconds, points_reward))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError: logger.warning(f"Duplicate video: {youtube_link}"); return None
    except Exception as e: logger.error(f"Error adding video {youtube_link}: {e}", exc_info=True); return None
    finally: conn.close()

def get_videos():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT video_id, youtube_link, duration_seconds, points_reward FROM videos")
        return cursor.fetchall()
    except Exception as e: logger.error(f"Error getting videos: {e}", exc_info=True); return []
    finally: conn.close()

def get_video_by_id(video_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT video_id, youtube_link, duration_seconds, points_reward FROM videos WHERE video_id = ?", (video_id,))
        v = cursor.fetchone()
        if v: return {"video_id": v[0], "link": v[1], "duration": v[2], "points": v[3]}
        return None
    except Exception as e: logger.error(f"Error getting video by ID {video_id}: {e}", exc_info=True); return None
    finally: conn.close()

def add_withdrawal_request(user_id, bkash_number, points, amount_taka):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO withdrawal_requests (user_id, bkash_number, points_withdrawn, amount_taka) VALUES (?, ?, ?, ?)", (user_id, bkash_number, points, amount_taka))
        conn.commit()
        return cursor.lastrowid
    except Exception as e: logger.error(f"Error adding withdrawal request for {user_id}: {e}", exc_info=True); return None
    finally: conn.close()

def get_pending_withdrawals():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT w.request_id, w.user_id, u.username, w.bkash_number, w.points_withdrawn, w.amount_taka, w.request_time FROM withdrawal_requests w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending' ORDER BY w.request_time ASC")
        return cursor.fetchall()
    except Exception as e: logger.error(f"Error getting pending withdrawals: {e}", exc_info=True); return []
    finally: conn.close()

def update_withdrawal_status(request_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE withdrawal_requests SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
    except Exception as e: logger.error(f"Error updating withdrawal status for {request_id}: {e}", exc_info=True)
    finally: conn.close()

# --- Telegram Functions ---
# (check_channel_join, start_command, button_callback, referral_command ইত্যাদি ফাংশনে 
# escape_markdown এবং রেফারেল লিঙ্ক (` `) দিয়ে ফরম্যাটিং করা হয়েছে।)

async def check_channel_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_telegram_obj = None
    if update.effective_user: 
        user_telegram_obj = update.effective_user
    elif update.callback_query and update.callback_query.from_user: 
        user_telegram_obj = update.callback_query.from_user
    else:
        logger.error("check_channel_join: Could not determine user from update object.")
        return False

    user_id = user_telegram_obj.id
    username_for_log = user_telegram_obj.username if user_telegram_obj.username else "N/A"

    if CHANNEL_ID == 0: # CHANNEL_ID এখন int হিসেবে লোড হয়, অথবা 0 যদি সেট না থাকে
        logger.warning(f"CHANNEL_ID is 0 or not set. Skipping join check for {user_id}.")
        set_channel_joined_status(user_id, True) 
        return True

    # CHANNEL_USERNAME এখন "" হতে পারে যদি সেট না থাকে
    if not CHANNEL_USERNAME: # যদি ইউজারনেম খালি থাকে
        logger.warning(f"CHANNEL_USERNAME is not set. Skipping join check as URL cannot be formed for user {user_id}.")
        # এখানে আপনি চাইলে ব্যবহারকারীকে একটি বার্তা দিতে পারেন যে চ্যানেল কনফিগার করা হয়নি
        # অথবা set_channel_joined_status(user_id, True) করতে পারেন যদি এই চেক ঐচ্ছিক হয়
        return True # অথবা False, আপনার লজিক অনুযায়ী

    effective_channel_id = CHANNEL_ID
    logger.info(f"Checking join for user {user_id} (TG: @{username_for_log}) in channel ID {effective_channel_id} (Configured: @{CHANNEL_USERNAME})")
    try:
        member = await context.bot.get_chat_member(chat_id=effective_channel_id, user_id=user_id)
        logger.info(f"User {user_id} status in channel {effective_channel_id}: {member.status}")
        if member.status in ['member', 'administrator', 'creator']:
            set_channel_joined_status(user_id, True); return True
        else:
            set_channel_joined_status(user_id, False); return False
    except BadRequest as e: 
        logger.error(f"BadRequest checking membership for {user_id} in {effective_channel_id}: {e.message}", exc_info=False)
        set_channel_joined_status(user_id, False); return False
    except Forbidden as e: 
        logger.error(f"Forbidden error checking membership for {user_id} in {effective_channel_id}: {e.message}. BOT NEEDS ADMIN RIGHTS IN THE CHANNEL.", exc_info=False)
        set_channel_joined_status(user_id, False); return False
    except Exception as e:
        logger.error(f"Unexpected error checking membership for {user_id} in {effective_channel_id}: {e}", exc_info=True)
        set_channel_joined_status(user_id, False); return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        logger.error("start_command: update.effective_user is None.")
        await update.message.reply_text("একটি ত্রুটি হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।")
        return

    user_data = get_user(user.id)
    referral_code_used = context.args[0] if context.args else None

    username_to_store = user.username if user.username else f"User_{user.id}"
    if not user_data:
        add_user(user.id, username_to_store, referral_code_used)
        user_data = get_user(user.id) # ডেটাবেস থেকে নতুন ইউজার ডেটা আবার নিন

    if not user_data: # যদি add_user ব্যর্থ হয় বা অন্য কোনো সমস্যা
        logger.critical(f"Failed to get/create user_data for {user.id} after add_user attempt.")
        await update.message.reply_text("একটি গুরুতর ত্রুটি হয়েছে। অনুগ্রহ করে অ্যাডমিনের সাথে যোগাযোগ করুন।")
        return
    
    # যদি ইউজারের নাম ডেটাবেসে না থাকে বা ভিন্ন হয়, আপডেট করুন
    if user_data.get('username') != username_to_store:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username_to_store, user.id))
            conn.commit()
            logger.info(f"Updated username for user {user.id} to {username_to_store}")
            user_data['username'] = username_to_store # লোডেড ডেটা আপডেট করুন
        except Exception as e:
            logger.error(f"Error updating username for user {user.id}: {e}")
        finally:
            conn.close()


    if CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context): # চ্যানেল আইডি ও ইউজারনেম সেট থাকলেই কেবল চেক করুন
        logger.info(f"User {user.id} not in channel @{CHANNEL_USERNAME}. Prompting to join.")
        keyboard = [[InlineKeyboardButton(f"চ্যানেলে জয়েন করুন (@{CHANNEL_USERNAME})", url=f"https://t.me/{CHANNEL_USERNAME}")],
                    [InlineKeyboardButton("✅ জয়েন করেছি, চেক করুন", callback_data="check_join")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"বটটি ব্যবহার করার জন্য প্রথমে আমাদের টেলিগ্রাম চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করতে হবে। "
            f"অনুগ্রহ করে নিচের বাটনে ক্লিক করে জয়েন করুন এবং তারপর 'জয়েন করেছি' বাটনে ক্লিক করুন।",
            reply_markup=reply_markup )
        return

    user_first_name_safe = escape_markdown(user.first_name, version=1) 
    welcome_message = f"স্বাগতম, {user_first_name_safe}!\nভিডিও দেখে পয়েন্ট অর্জন করুন।"
    
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    
    ref_code_from_db = user_data.get('referral_code')
    # যদি কোড না থাকে বা খালি স্ট্রিং হয়, জেনারেট করার চেষ্টা করুন
    if not ref_code_from_db: 
        new_code = generate_referral_code() 
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        try:
            # শুধুমাত্র যদি referral_code NULL অথবা খালি স্ট্রিং হয় তাহলেই আপডেট করুন
            c.execute("UPDATE users SET referral_code = ? WHERE user_id = ? AND (referral_code IS NULL OR referral_code = '')", (new_code, user.id))
            conn.commit()
            if c.rowcount > 0:
                logger.info(f"Generated and set missing referral code {new_code} for user {user.id} during /start.")
                ref_code_from_db = new_code 
            else:
                # যদি আপডেট না হয়, তাহলে সম্ভবত অন্য কোনো ভ্যালু আছে অথবা কোডটি ইতিমধ্যে অন্য কোথাও ইউনিক
                # পুনরায় ইউজার ডেটা রিফ্রেশ করে দেখা যেতে পারে
                user_data_refreshed_for_code = get_user(user.id)
                if user_data_refreshed_for_code and user_data_refreshed_for_code.get('referral_code'):
                    ref_code_from_db = user_data_refreshed_for_code.get('referral_code')
                else:
                    logger.warning(f"Could not set new referral code for user {user.id} in /start. Existing might be non-empty or DB issue.")
        except sqlite3.IntegrityError: # ইউনিক কোড কনফ্লিক্ট হলে
            logger.warning(f"Generated referral code {new_code} already exists. User {user.id} might need manual check or retry /start.")
            # এখানে আপনি চাইলে আবার generate_referral_code() কল করে নতুন কোড তৈরির চেষ্টা করতে পারেন
        except Exception as e_db: logger.error(f"Error setting generated ref code for {user.id} in /start: {e_db}")
        finally: c.close()

    if ref_code_from_db:
        actual_link_url = f"https://t.me/{bot_username}?start={ref_code_from_db}"
        welcome_message += f"\nআপনার রেফারেল কোড: `{actual_link_url}`\n\n"
    else:
        welcome_message += f"\nআপনার রেফারেল কোড তৈরিতে একটি সমস্যা হয়েছে। অনুগ্রহ করে আবার /start কমান্ড দিন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।\n\n"
        logger.error(f"Failed to get/generate referral code for user {user.id} in /start.")

    welcome_message += "কমান্ড তালিকা:\n/watch - ভিডিও দেখুন\n/balance - পয়েন্ট দেখুন\n/referral - রেফারেল তথ্য\n/withdraw - উইথড্র করুন\n/help - সাহায্য"
    
    try:
        await update.message.reply_text(welcome_message, parse_mode='Markdown', disable_web_page_preview=True)
    except BadRequest as e:
        logger.error(f"Markdown parse error in start_command for user {user.id}: {e}. Message: {welcome_message}")
        # ফলব্যাক হিসেবে সাধারণ টেক্সট পাঠান
        fallback_text = welcome_message.replace("`", "") # ব্যাকটিক সরিয়ে দিন
        await update.message.reply_text(fallback_text, disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_data = get_user(update.effective_user.id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)) :
        await start_command(update, context); return
        
    help_text = "কমান্ড:\n/start - বট শুরু করুন\n/watch - ভিডিও দেখুন\n/balance - আপনার পয়েন্ট দেখুন\n/referral - আপনার রেফারেল লিঙ্ক পান\n/withdraw - পয়েন্ট উইথড্র করুন\n/cancelwatch - ভিডিও দেখা বাতিল করুন\n/help - এই সাহায্য বার্তা"
    if update.effective_user.id == ADMIN_ID:
        help_text += "\n\nঅ্যাডমিন কমান্ড:\n`/addvideo <লিঙ্ক> <সেকেন্ড> <পয়েন্ট>` - নতুন ভিডিও যোগ করুন\n`/pendingwithdrawals` - পেন্ডিং উইথড্রয়াল দেখুন\n`/approve <রিকোয়েস্ট_আইডি>` - উইথড্রয়াল অনুমোদন করুন\n`/reject <রিকোয়েস্ট_আইডি> [কারণ]` - উইথড্রয়াল বাতিল করুন"
    
    try:
        await update.message.reply_text(help_text, parse_mode='Markdown')
    except BadRequest as e:
        logger.error(f"Markdown parse error in help_command: {e.message}")
        await update.message.reply_text(help_text.replace("`", ""))


async def watch_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)):
        await start_command(update, context); return
    if user_data['watching_video_id']:
        await update.message.reply_text("আপনি ইতিমধ্যে একটি ভিডিও দেখছেন। /cancelwatch ব্যবহার করুন।"); return
    videos = get_videos()
    if not videos: await update.message.reply_text("দুঃখিত, এই মুহূর্তে কোনো ভিডিও উপলব্ধ নেই।"); return
    keyboard = [[InlineKeyboardButton(f"ভিডিও (🔗) - {v[3]} পয়েন্ট", callback_data=f"watch_{v[0]}")] for v in videos]
    await update.message.reply_text("দেখার জন্য একটি ভিডিও নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not query.from_user: 
        logger.error("CallbackQuery received without 'from_user'. Cannot proceed.")
        if query.message: await query.message.reply_text("একটি অপ্রত্যাশিত ত্রুটি হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।")
        return

    user_id = query.from_user.id
    user_first_name_from_callback = query.from_user.first_name 
    
    logger.info(f"Button callback: User {user_id} (TG: {user_first_name_from_callback}), Data {data}")
    user_data = get_user(user_id)

    if not user_data: 
        logger.warning(f"User data not found for user_id {user_id} during button callback. Prompting /start.")
        if query.message: await query.message.reply_text("অনুগ্রহ করে প্রথমে /start কমান্ড দিন।")
        else: logger.error(f"query.message is None for user {user_id} in button_callback. Cannot send reply when user_data not found.")
        return

    if data == "check_join":
        # check_channel_join ফাংশনটি update (যা CallbackQueryHandler থেকে আসে) অবজেক্ট গ্রহণ করে
        is_member_api = await check_channel_join(update, context) 
        user_data_refreshed = get_user(user_id) # ডেটাবেস থেকে ফ্রেশ ডেটা নিন

        if is_member_api and user_data_refreshed and user_data_refreshed['channel_joined']:
            logger.info(f"User {user_id} verified join via button.")
            
            user_first_name_safe = escape_markdown(user_first_name_from_callback, version=1)
            referral_code = user_data_refreshed.get('referral_code')
            bot_info = await context.bot.get_me(); bot_username = bot_info.username
            
            ref_link_msg_part = ""
            if not referral_code: 
                new_code = generate_referral_code()
                conn = sqlite3.connect(DB_NAME); c = conn.cursor()
                try:
                    c.execute("UPDATE users SET referral_code = ? WHERE user_id = ? AND (referral_code IS NULL OR referral_code = '')", (new_code, user_id)); conn.commit()
                    if c.rowcount > 0: 
                        referral_code = new_code
                        logger.info(f"Generated/set missing ref code {new_code} for user {user_id} in check_join.")
                    else: # যদি আপডেট না হয়, ডেটাবেস থেকে রিফ্রেশ করে দেখুন
                        user_data_recheck = get_user(user_id)
                        if user_data_recheck and user_data_recheck.get('referral_code'):
                            referral_code = user_data_recheck.get('referral_code')
                        else:
                            logger.warning(f"Could not set new ref code for user {user_id} in check_join after recheck.")
                except sqlite3.IntegrityError:
                     logger.warning(f"Generated ref code {new_code} (check_join) already exists for user {user_id}.")
                except Exception as e_db: logger.error(f"DB error setting ref code for {user_id} in check_join: {e_db}")
                finally: c.close()
            
            if referral_code:
                actual_link_url = f"https://t.me/{bot_username}?start={referral_code}"
                ref_link_msg_part = f"আপনার রেফারেল কোড: `{actual_link_url}`\n\n"
            else:
                logger.error(f"Still no referral code for user {user_id} after generation attempt in check_join.")
                ref_link_msg_part = "আপনার রেফারেল কোড তৈরিতে একটি সমস্যা হয়েছে। অনুগ্রহ করে আবার /start কমান্ড দিন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।\n\n"

            welcome_text = f"স্বাগতম, {user_first_name_safe}!\nভিডিও দেখে পয়েন্ট অর্জন করুন।\n"
            commands_list_text = "কমান্ড তালিকা:\n/watch - ভিডিও দেখুন\n/balance - পয়েন্ট দেখুন\n/referral - রেফারেল তথ্য\n/withdraw - উইথড্র করুন\n/help - সাহায্য"
            final_message = welcome_text + ref_link_msg_part + commands_list_text
            
            try: await query.edit_message_text(text=final_message, parse_mode='Markdown', disable_web_page_preview=True)
            except BadRequest as e:
                if "Message is not modified" in str(e): logger.info(f"Msg not modified for user {user_id} on join check.")
                else:
                    logger.error(f"BadRequest editing msg for join check user {user_id}: {e}. Message: {final_message}")
                    if query.message: await query.message.reply_text(text=final_message.replace("`",""), disable_web_page_preview=True) 
            except Exception as e:
                 logger.error(f"Error editing msg for join check user {user_id}: {e}")
                 if query.message: await query.message.reply_text(text=final_message.replace("`",""), disable_web_page_preview=True) 
        else:
            logger.warning(f"User {user_id} clicked check_join but not verified (API: {is_member_api}, DB: {user_data_refreshed.get('channel_joined') if user_data_refreshed else 'N/A'}).")
            keyboard = [[InlineKeyboardButton(f"চ্যানেলে জয়েন করুন (@{CHANNEL_USERNAME})", url=f"https://t.me/{CHANNEL_USERNAME}")],
                        [InlineKeyboardButton("✅ জয়েন করেছি, চেক করুন", callback_data="check_join")]]
            try: await query.edit_message_text(text=f"আপনি এখনও চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করেননি। অনুগ্রহ করে জয়েন করে আবার চেষ্টা করুন।", reply_markup=InlineKeyboardMarkup(keyboard))
            except BadRequest as e:
                if "Message is not modified" in str(e): logger.info(f"Msg not modified for user {user_id} (not joined prompt).")
                else: raise e 
        return

    if CHANNEL_ID != 0 and CHANNEL_USERNAME and not user_data['channel_joined']: 
        if not await check_channel_join(update, context):
             if query.message: await query.message.reply_text(f"অনুগ্রহ করে প্রথমে চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করুন এবং তারপর /start দিন অথবা 'জয়েন করেছি' বাটনে ক্লিক করুন।"); return

    if data.startswith("watch_"):
        if user_data['watching_video_id']:
            if query.message: await query.message.reply_text("আপনি ইতিমধ্যে একটি ভিডিও দেখছেন।"); return
        try: video_id = int(data.split("_")[1])
        except (IndexError, ValueError): 
            if query.message: await query.message.reply_text("অবৈধ ভিডিও আইডি।"); return
        video = get_video_by_id(video_id)
        if not video: 
            await query.edit_message_text("ভিডিওটি আর উপলব্ধ নেই।"); return 
        set_watching_video(user_id, video_id, int(time.time()))
        keyboard = [[InlineKeyboardButton("✅ সম্পূর্ণ দেখেছি", callback_data=f"watched_{video_id}")]]
        await query.edit_message_text(f"দেখছেন: {video['link']}\nদৈর্ঘ্য: {video['duration']}s.\nসম্পূর্ণ দেখলে {video['points']} পয়েন্ট।", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=False)

    elif data.startswith("watched_"):
        if not user_data['watching_video_id'] or not user_data['video_start_time']:
            await query.edit_message_text("আপনি কোনো ভিডিও দেখছেন না। /watch থেকে শুরু করুন।"); return
        try: claimed_video_id = int(data.split("_")[1])
        except (IndexError, ValueError): 
            if query.message: await query.message.reply_text("অবৈধ ভিডিও আইডি।"); return
        
        current_video = get_video_by_id(user_data['watching_video_id'])
        if not current_video or user_data['watching_video_id'] != claimed_video_id:
            await query.edit_message_text("ত্রুটি। আবার চেষ্টা করুন।"); clear_watching_video(user_id); return
        
        time_elapsed = int(time.time()) - user_data['video_start_time']
        if time_elapsed >= current_video['duration']:
            points = current_video['points']
            update_user_points(user_id, points)
            referrer_id = user_data['referred_by']
            clear_watching_video(user_id)
            await query.edit_message_text(f"অভিনন্দন! আপনি {points} পয়েন্ট পেয়েছেন।")
            if referrer_id:
                commission = int(points * REFERRAL_PERCENTAGE)
                if commission > 0:
                    update_user_points(referrer_id, commission)
                    try: 
                        user_first_name_safe = escape_markdown(user_first_name_from_callback, version=1)
                        await context.bot.send_message(chat_id=referrer_id, text=f"আপনার রেফারের ({user_first_name_safe}) মাধ্যমে আপনি {commission} পয়েন্ট কমিশন পেয়েছেন!", parse_mode='Markdown')
                    except Exception as e: logger.error(f"Failed to send commission to {referrer_id}: {e}")
        else:
            if query.message: await query.message.reply_text(f"ভিডিওটি সম্পূর্ণ দেখেননি। আরও {current_video['duration'] - time_elapsed}s দেখুন।")

async def cancel_watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    if user_data['watching_video_id']:
        clear_watching_video(user_id); await update.message.reply_text("ভিডিও দেখা বাতিল হয়েছে।")
    else: await update.message.reply_text("আপনি কোনো ভিডিও দেখছেন না।")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    await update.message.reply_text(f"আপনার বর্তমান পয়েন্ট: {user_data['points']}")

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    
    ref_code = user_data.get('referral_code')
    if not ref_code: 
        new_code = generate_referral_code()
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        try: 
            c.execute("UPDATE users SET referral_code = ? WHERE user_id = ? AND (referral_code IS NULL OR referral_code = '')", (new_code, user_id)); conn.commit()
            if c.rowcount > 0: ref_code = new_code; logger.info(f"Generated/set missing ref code {new_code} for user {user_id} in referral_command.")
            else: # রিফ্রেশ করে দেখুন
                user_data_recheck = get_user(user_id)
                if user_data_recheck and user_data_recheck.get('referral_code'): ref_code = user_data_recheck.get('referral_code')
                else: logger.warning(f"Could not set new ref code for user {user_id} in referral_command after recheck.")
        except sqlite3.IntegrityError: logger.warning(f"Generated ref code {new_code} (referral_command) already exists for user {user_id}.")
        except Exception as e_db: logger.error(f"DB error setting ref code for {user_id} in referral_command: {e_db}")
        finally: c.close()

    if not ref_code:
        await update.message.reply_text("আপনার রেফারেল কোড তৈরিতে একটি সমস্যা হয়েছে। অনুগ্রহ করে আবার /start কমান্ড দিন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।")
        logger.error(f"Failed to get/generate referral code for user {user_id} in referral_command.")
        return

    bot_info = await context.bot.get_me(); bot_username = bot_info.username 
    actual_link_url = f"https://t.me/{bot_username}?start={ref_code}"

    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    try: c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)); count = c.fetchone()[0]
    except: count = 0
    finally: c.close()
    
    message_text = f"আপনার রেফারেল লিঙ্ক: `{actual_link_url}`\n" \
                   f"এটি বন্ধুদের সাথে শেয়ার করে পয়েন্ট অর্জন করুন!\n\n" \
                   f"মোট রেফার: {count} জন\n" \
                   f"প্রতি রেফারে ভিডিও দেখার পর আপনি {REFERRAL_PERCENTAGE*100:.0f}% কমিশন পাবেন।"
    logger.info(f"Referral command message content for user {user_id}: [{message_text}]")
    try:
        await update.message.reply_text(message_text, parse_mode='Markdown', disable_web_page_preview=True)
    except BadRequest as e:
        logger.error(f"Error in referral_command sending Markdown message for user {user_id}: {e}. Content: {message_text}")
        await update.message.reply_text(message_text.replace("`",""), disable_web_page_preview=True)

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return # Guard
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)):
        await start_command(update, context); return ConversationHandler.END
    MIN_WITHDRAW = 10 
    if user_data['points'] < MIN_WITHDRAW:
        await update.message.reply_text(f"উইথড্র করতে কমপক্ষে {MIN_WITHDRAW} পয়েন্ট প্রয়োজন। আপনার আছে {user_data['points']} পয়েন্ট।"); return ConversationHandler.END
    await update.message.reply_text("আপনার বিকাশ নম্বর দিন (১১ সংখ্যার):"); return ASK_BKASH_NUMBER

async def ask_bkash_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bkash_no = update.message.text
    if not bkash_no.isdigit() or len(bkash_no) != 11:
        await update.message.reply_text("সঠিক ১১ সংখ্যার বিকাশ নম্বর দিন। /withdraw আবার চেষ্টা করুন।"); return ConversationHandler.END
    context.user_data['bkash_number'] = bkash_no
    if not update.effective_user: return ConversationHandler.END # Guard
    user_data = get_user(update.effective_user.id)
    if not user_data: await update.message.reply_text("ত্রুটি। /start করুন।"); return ConversationHandler.END 
    max_taka = user_data['points'] * POINTS_TO_TAKA_RATE
    await update.message.reply_text(f"কত পয়েন্ট উইথড্র করতে চান? (আপনার আছে {user_data['points']} পয়েন্ট, যা প্রায় {max_taka:.2f} টাকা)\nন্যূনতম ১০ পয়েন্ট উইথড্র করতে পারবেন।"); return ASK_WITHDRAW_POINTS

async def ask_withdraw_points_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return ConversationHandler.END # Guard
    user_id = update.effective_user.id; user_data = get_user(user_id)
    bkash_no = context.user_data.get('bkash_number')
    if not user_data or not bkash_no:
        await update.message.reply_text("ত্রুটি। /withdraw আবার করুন।"); context.user_data.clear(); return ConversationHandler.END
    try: points_wd = int(update.message.text)
    except ValueError: await update.message.reply_text("সঠিক সংখ্যায় পয়েন্ট লিখুন।"); context.user_data.clear(); return ConversationHandler.END
    if points_wd <= 0: await update.message.reply_text("পয়েন্ট ০ এর বেশি হতে হবে।"); context.user_data.clear(); return ConversationHandler.END
    if points_wd > user_data['points']: await update.message.reply_text(f"পর্যাপ্ত পয়েন্ট নেই ({user_data['points']})।"); context.user_data.clear(); return ConversationHandler.END
    MIN_REQ_POINTS = 10 
    if points_wd < MIN_REQ_POINTS: await update.message.reply_text(f"কমপক্ষে {MIN_REQ_POINTS} পয়েন্ট উইথড্র করতে হবে।"); context.user_data.clear(); return ConversationHandler.END

    amount_tk = points_wd * POINTS_TO_TAKA_RATE
    update_user_points(user_id, -points_wd)
    req_id = add_withdrawal_request(user_id, bkash_no, points_wd, amount_tk)
    if req_id is None:
        await update.message.reply_text("উইথড্রয়াল অনুরোধে সমস্যা। পয়েন্ট ফেরত দেওয়া হয়েছে।"); update_user_points(user_id, points_wd); context.user_data.clear(); return ConversationHandler.END
    
    user_full_name_safe = escape_markdown(update.effective_user.full_name or "N/A", version=1)
    user_username_safe = escape_markdown(update.effective_user.username or "N/A", version=1)

    await update.message.reply_text(f"আপনার উইথড্রয়াল অনুরোধ সফলভাবে জমা হয়েছে!\nID: {req_id}\nবিকাশ নম্বর: {bkash_no}\nউইথড্র করা পয়েন্ট: {points_wd}\nটাকার পরিমাণ: {amount_tk:.2f} টাকা\n\nঅ্যাডমিন আপনার অনুরোধটি পর্যালোচনা করে শীঘ্রই ব্যবস্থা নিবেন।")
    if ADMIN_ID != 0:
        admin_notify_text = f"🔔 নতুন উইথড্রয়াল অনুরোধ!\nব্যবহারকারী: {user_full_name_safe} (`@{user_username_safe}`, ID: `{user_id}`)\nরিকোয়েস্ট ID: `{req_id}`\nবিকাশ নম্বর: `{bkash_no}`\nপয়েন্ট: {points_wd}\nটাকা: {amount_tk:.2f}\n\nঅনুমোদন করতে: `/approve {req_id}`\nবাতিল করতে: `/reject {req_id}`"
        try: await context.bot.send_message(ADMIN_ID, admin_notify_text, parse_mode='Markdown')
        except Exception as e: logger.error(f"Failed to send admin WD notification: {e}")
    context.user_data.clear(); return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data: await update.message.reply_text("উইথড্র প্রক্রিয়া বাতিল করা হয়েছে।"); context.user_data.clear()
    else: await update.message.reply_text("কোনো সক্রিয় প্রক্রিয়া (যেমন উইথড্র) চালু নেই।")
    return ConversationHandler.END

async def admin_add_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return # Guard
    if len(context.args) != 3: await update.message.reply_text("ব্যবহার: `/addvideo <ইউটিউব_লিঙ্ক> <সময়_সেকেন্ডে> <পয়েন্ট>`", parse_mode='Markdown'); return
    link, dur_s, pts_s = context.args
    try: dur = int(dur_s); pts = int(pts_s); assert dur > 0 and pts > 0
    except: await update.message.reply_text("সময় (সেকেন্ডে) এবং পয়েন্ট অবশ্যই ধনাত্মক সংখ্যা হতে হবে।"); return
    if not ("youtube.com/" in link or "youtu.be/" in link): await update.message.reply_text("অনুগ্রহ করে একটি সঠিক ইউটিউব লিঙ্ক দিন।"); return
    vid = add_video(link, dur, pts)
    if vid: await update.message.reply_text(f"ভিডিও সফলভাবে যোগ করা হয়েছে (ID: `{vid}`)।", parse_mode='Markdown')
    else: await update.message.reply_text("এই ইউটিউব লিঙ্কটি ইতিমধ্যে ডাটাবেসে বিদ্যমান অথবা ভিডিও যোগ করতে কোনো সমস্যা হয়েছে।")

async def admin_pending_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return # Guard
    reqs = get_pending_withdrawals()
    if not reqs: await update.message.reply_text("কোনো পেন্ডিং উইথড্রয়াল অনুরোধ নেই।"); return
    msg_parts = ["⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ:*\n\n"]
    for r_id, u_id, u_name, bkash, pts, tk, time_req in reqs:
        u_name_safe = escape_markdown(u_name or 'নাম নেই', version=1)
        bkash_safe = escape_markdown(bkash, version=1)
        part = f"*ID:* `{r_id}`\n*ব্যবহারকারী:* {u_name_safe} (ID: `{u_id}`)\n*বিকাশ নম্বর:* `{bkash_safe}`\n*পয়েন্ট:* {pts} (প্রায় {tk:.2f} টাকা)\n*অনুরোধের সময়:* {str(time_req).split('.')[0]}\n`/approve {r_id}`\n`/reject {r_id}`\n\n---\n\n"
        if sum(len(p) for p in msg_parts) + len(part) > 4090: 
            await update.message.reply_text("".join(msg_parts), parse_mode='Markdown'); msg_parts = ["⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ (অংশ ২):*\n\n", part]
        else: msg_parts.append(part)
    if msg_parts and (len(msg_parts) > 1 or msg_parts[0] != "⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ:*\n\n"):
        await update.message.reply_text("".join(msg_parts), parse_mode='Markdown')

async def admin_process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE, new_status: str):
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return # Guard
    cmd_usage = f"ব্যবহার: `/{new_status} <রিকোয়েস্ট_আইডি>{' [কারণ]' if new_status == 'rejected' else ''}`"
    if not context.args or (new_status == 'rejected' and len(context.args) < 1) or (new_status == 'approved' and len(context.args) != 1):
        await update.message.reply_text(cmd_usage, parse_mode='Markdown'); return
    try: req_id_proc = int(context.args[0])
    except: await update.message.reply_text("রিকোয়েস্ট আইডি একটি সংখ্যা হতে হবে।"); return
    
    reason_raw = " ".join(context.args[1:]) if new_status == 'rejected' and len(context.args) > 1 else "অ্যাডমিন কর্তৃক প্রক্রিয়াজাত।"
    reason_safe = escape_markdown(reason_raw, version=1)
    
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    try: c.execute("SELECT user_id, points_withdrawn, amount_taka, status FROM withdrawal_requests WHERE request_id = ?", (req_id_proc,)); req_data = c.fetchone()
    except Exception as e: logger.error(f"Error fetching WD {req_id_proc} for {new_status}: {e}"); await update.message.reply_text("উইথড্রয়াল তথ্য আনতে সমস্যা হয়েছে।"); c.close(); return
    c.close()

    if not req_data: await update.message.reply_text(f"রিকোয়েস্ট আইডি `{req_id_proc}` খুঁজে পাওয়া যায়নি।", parse_mode='Markdown'); return
    u_id_notify, pts_refund, tk_amt, curr_status = req_data
    if curr_status != 'pending': await update.message.reply_text(f"রিকোয়েস্ট আইডি `{req_id_proc}` ইতিমধ্যে '{curr_status}' হিসেবে চিহ্নিত আছে।", parse_mode='Markdown'); return

    update_withdrawal_status(req_id_proc, new_status)
    user_msg_text = ""
    admin_reply_text = ""

    if new_status == 'approved':
        admin_reply_text = f"রিকোয়েস্ট আইডি `{req_id_proc}` সফলভাবে অনুমোদিত হয়েছে। ব্যবহারকারীকে {tk_amt:.2f} টাকা তার বিকাশ নম্বরে পাঠান।"
        user_msg_text = f"🎉 অভিনন্দন! আপনার উইথড্রয়াল অনুরোধ (ID: `{req_id_proc}`) অনুমোদিত হয়েছে। {pts_refund} পয়েন্টের বিনিময়ে {tk_amt:.2f} টাকা শীঘ্রই আপনার বিকাশ অ্যাকাউন্টে পাঠানো হবে।"
    elif new_status == 'rejected':
        update_user_points(u_id_notify, pts_refund) 
        admin_reply_text = f"রিকোয়েস্ট আইডি `{req_id_proc}` বাতিল করা হয়েছে। ব্যবহারকারীকে {pts_refund} পয়েন্ট ফেরত দেওয়া হয়েছে।"
        user_msg_text = f" দুঃখিত, আপনার উইথড্রয়াল অনুরোধ (ID: `{req_id_proc}`) বাতিল করা হয়েছে।\nকারণ: {reason_safe}\nআপনার {pts_refund} পয়েন্ট আপনার অ্যাকাউন্টে ফেরত দেওয়া হয়েছে।"
    
    await update.message.reply_text(admin_reply_text, parse_mode='Markdown')
    if u_id_notify and user_msg_text:
        try: await context.bot.send_message(chat_id=u_id_notify, text=user_msg_text, parse_mode='Markdown')
        except Exception as e: logger.warning(f"Could not notify user {u_id_notify} for WD {req_id_proc} ({new_status}): {e}")

async def admin_approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_process_withdrawal(update, context, 'approved')

async def admin_reject_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_process_withdrawal(update, context, 'rejected')

async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("/start", "বট শুরু করুন"), BotCommand("/watch", "ভিডিও দেখুন ও পয়েন্ট অর্জন করুন"),
            BotCommand("/balance", "আপনার বর্তমান পয়েন্ট দেখুন"), BotCommand("/referral", "আপনার রেফারেল লিঙ্ক পান"),
            BotCommand("/withdraw", "পয়েন্ট উইথড্র করুন"), BotCommand("/cancelwatch", "বর্তমান ভিডিও দেখা বাতিল করুন"),
            BotCommand("/help", "সাহায্য ও কমান্ড তালিকা")
        ])
        logger.info("User commands set.")
    except Exception as e: logger.error(f"Failed to set commands: {e}")

def main():
    init_db() # ডেটাবেস ইনিশিয়ালাইজ করুন
    
    # অত্যাবশ্যকীয় ভ্যারিয়েবলগুলো main ফাংশনের শুরুতেও চেক করা যেতে পারে,
    # তবে গ্লোবাল স্কোপে চেক করা হয়েছে, যা ঠিক আছে।

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("withdraw", withdraw_command)],
        states={
            ASK_BKASH_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bkash_number_received)],
            ASK_WITHDRAW_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_withdraw_points_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation), MessageHandler(filters.COMMAND, cancel_conversation)],
        conversation_timeout=300 )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch_video_command))
    application.add_handler(CommandHandler("cancelwatch", cancel_watch_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("addvideo", admin_add_video))
    application.add_handler(CommandHandler("pendingwithdrawals", admin_pending_withdrawals))
    application.add_handler(CommandHandler("approve", admin_approve_withdrawal))
    application.add_handler(CommandHandler("reject", admin_reject_withdrawal))
    
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("বট চালু হচ্ছে...")
    try:
        application.run_polling()
    except Exception as e:
        logger.critical(f"বট চালাতে গুরুতর ত্রুটি হয়েছে: {e}", exc_info=True)
    finally:
        logger.info("বট বন্ধ করা হচ্ছে।")

if __name__ == "__main__":
    main()
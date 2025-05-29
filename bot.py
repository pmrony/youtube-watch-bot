import psycopg2 # psycopg2-binary ইনস্টল করা থাকতে হবে
import logging
import time
import random
import string
import os
from dotenv import load_dotenv # .env ফাইল লোড করার জন্য
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, ConversationHandler
from telegram.error import BadRequest, Forbidden # Specific error handling
from telegram.helpers import escape_markdown

# .env ফাইল থেকে ভ্যারিয়েবল লোড করুন
load_dotenv()

# --- কনফিগারেশন ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_ID_STR = os.environ.get("ADMIN_USER_ID")
TELEGRAM_CHANNEL_ID_STR = os.environ.get("TELEGRAM_CHANNEL_ID")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME")
DATABASE_URL = os.environ.get("DATABASE_URL") # PostgreSQL কানেকশন URL

REFERRAL_PERCENTAGE = 0.10
POINTS_TO_TAKA_RATE = 0.1
WATCH_COOLDOWN_SECONDS = 20 * 60 * 60  # ২০ ঘণ্টা (সেকেন্ডে)

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

if not DATABASE_URL:
    logger.critical("ত্রুটি: DATABASE_URL এনভায়রনমেন্ট ভ্যারিয়েবল সেট করা হয়নি!")
    exit()

if not TELEGRAM_CHANNEL_ID_STR:
    logger.warning("TELEGRAM_CHANNEL_ID সেট করা নেই, চ্যানেল জয়েন ফিচার কাজ নাও করতে পারে।")
    CHANNEL_ID = 0 
else:
    CHANNEL_ID = int(TELEGRAM_CHANNEL_ID_STR)

if not CHANNEL_USERNAME:
    logger.warning("CHANNEL_USERNAME সেট করা নেই, চ্যানেল জয়েন ফিচার কাজ নাও করতে পারে।")
    CHANNEL_USERNAME = "" 

logger.info(f"BOT_TOKEN: Loaded (partially hidden)")
logger.info(f"ADMIN_ID: {ADMIN_ID}")
logger.info(f"DATABASE_URL: Loaded (partially hidden)")
logger.info(f"CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"CHANNEL_USERNAME: {CHANNEL_USERNAME}")
logger.info(f"WATCH_COOLDOWN_SECONDS: {WATCH_COOLDOWN_SECONDS}")


if CHANNEL_ID == 0 or not CHANNEL_USERNAME: 
    logger.warning("গুরুত্বপূর্ণ: CHANNEL_ID অথবা CHANNEL_USERNAME সঠিকভাবে সেট করা হয়নি। চ্যানেল সম্পর্কিত ফিচার কাজ নাও করতে পারে।")

ASK_BKASH_NUMBER, ASK_WITHDRAW_POINTS = range(2) 
CLAIM_ASK_SCREENSHOT, CLAIM_ASK_USER_TEXT = range(10, 12) 
PENDING_CLAIMS = {} 

# --- Database Functions (PostgreSQL) ---

def get_db_connection():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"PostgreSQL ডেটাবেসে কানেক্ট করতে সমস্যা: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        logger.error("init_db: ডেটাবেস কানেকশন স্থাপন করা যায়নি।")
        return
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                username TEXT, 
                points INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE, 
                referred_by BIGINT, 
                channel_joined BOOLEAN DEFAULT FALSE,
                watching_video_id INTEGER, 
                video_start_time BIGINT
            )''')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                video_id SERIAL PRIMARY KEY, 
                youtube_link TEXT UNIQUE,
                duration_seconds INTEGER, 
                points_reward INTEGER
            )''')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                request_id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                bkash_number TEXT,
                points_withdrawn INTEGER, 
                amount_taka REAL, 
                status TEXT DEFAULT 'pending',
                request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_video_watch_history (
                user_id BIGINT,
                video_id INTEGER,
                last_watched_timestamp BIGINT,
                PRIMARY KEY (user_id, video_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            )''')
            conn.commit()
            logger.info("PostgreSQL Database initialized/checked successfully.")
    except Exception as e:
        logger.error(f"Error initializing PostgreSQL database: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

def generate_referral_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def add_user(user_id, username, referred_by_code=None):
    conn = get_db_connection()
    if not conn: return
    new_referral_code = generate_referral_code()
    try:
        with conn.cursor() as cursor:
            referrer_id = None
            if referred_by_code:
                try:
                    cursor.execute("SELECT user_id FROM users WHERE referral_code = %s", (referred_by_code,))
                    referrer = cursor.fetchone()
                    if referrer: referrer_id = referrer[0]
                    else: logger.warning(f"Referral code {referred_by_code} not found.")
                except Exception as e_ref: logger.error(f"Error finding referrer for {referred_by_code}: {e_ref}")
            
            cursor.execute(
                "INSERT INTO users (user_id, username, referral_code, referred_by, channel_joined) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, username, new_referral_code, referrer_id, False)
            )
            conn.commit()
            if cursor.rowcount > 0:
                 logger.info(f"User {user_id} ({username}) added. Ref code: {new_referral_code}. Referred by: {referrer_id}")
            else: # ইউজার আগে থেকেই আছে
                logger.info(f"User {user_id} ({username}) already exists or conflict occurred.")
                cursor.execute("SELECT referral_code, referred_by FROM users WHERE user_id = %s", (user_id,))
                ex_user = cursor.fetchone()
                if ex_user:
                    # যদি referral_code না থাকে, তাহলে নতুনটা সেট করি
                    if not ex_user[0]:
                        cursor.execute("UPDATE users SET referral_code = %s WHERE user_id = %s", (new_referral_code, user_id))
                        conn.commit()
                        logger.info(f"Generated missing referral code for existing user {user_id}.")
                    # যদি referred_by_code দেওয়া হয় এবং ইউজারের referred_by সেট করা না থাকে
                    if referred_by_code and not ex_user[1] and referrer_id and referrer_id != user_id:
                        cursor.execute("UPDATE users SET referred_by = %s WHERE user_id = %s AND referred_by IS NULL", (referrer_id, user_id))
                        conn.commit()
                        logger.info(f"Applied new referral {referrer_id} to existing user {user_id}.")

    except psycopg2.IntegrityError as ie: # Unique constraint (e.g. referral_code)
        logger.warning(f"IntegrityError (likely duplicate referral_code '{new_referral_code}') for user {user_id}: {ie}")
        conn.rollback() # রোলব্যাক করা ভালো
    except Exception as e_main: 
        logger.error(f"Error in add_user for {user_id}: {e_main}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()


def get_user(user_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, username, points, referral_code, referred_by, channel_joined, watching_video_id, video_start_time FROM users WHERE user_id = %s", (user_id,))
            user = cursor.fetchone()
            if user: return {"user_id": user[0], "username": user[1], "points": user[2], "referral_code": user[3], "referred_by": user[4], "channel_joined": bool(user[5]), "watching_video_id": user[6], "video_start_time": user[7]}
            return None
    except Exception as e: 
        logger.error(f"Error getting user {user_id}: {e}", exc_info=True)
        return None
    finally: 
        if conn: conn.close()

def update_user_points(user_id, points_to_add):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET points = points + %s WHERE user_id = %s", (points_to_add, user_id))
            conn.commit()
    except Exception as e: 
        logger.error(f"Error updating points for user {user_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()

def set_channel_joined_status(user_id, status: bool):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET channel_joined = %s WHERE user_id = %s", (status, user_id))
            conn.commit()
            logger.info(f"Set channel_joined for user {user_id} to {status}.")
    except Exception as e: 
        logger.error(f"Error setting channel_joined for {user_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()

def set_watching_video(user_id, video_id, start_time):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET watching_video_id = %s, video_start_time = %s WHERE user_id = %s", (video_id, start_time, user_id))
            conn.commit()
    except Exception as e: 
        logger.error(f"Error setting watching video for {user_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()

def clear_watching_video(user_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET watching_video_id = NULL, video_start_time = NULL WHERE user_id = %s", (user_id,))
            conn.commit()
    except Exception as e: 
        logger.error(f"Error clearing watching video for {user_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()

def add_video(youtube_link, duration_seconds, points_reward): 
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO videos (youtube_link, duration_seconds, points_reward) VALUES (%s, %s, %s) RETURNING video_id", 
                (youtube_link, duration_seconds, points_reward)
            )
            video_id = cursor.fetchone()[0]
            conn.commit()
            return video_id
    except psycopg2.IntegrityError: 
        logger.warning(f"Duplicate video (PostgreSQL): {youtube_link}")
        if conn: conn.rollback()
        return None
    except Exception as e: 
        logger.error(f"Error adding video {youtube_link}: {e}", exc_info=True)
        if conn: conn.rollback()
        return None
    finally: 
        if conn: conn.close()

def get_videos():
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT video_id, youtube_link, duration_seconds, points_reward FROM videos")
            return cursor.fetchall()
    except Exception as e: 
        logger.error(f"Error getting videos: {e}", exc_info=True)
        return []
    finally: 
        if conn: conn.close()

def get_video_by_id(video_id): 
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT video_id, youtube_link, duration_seconds, points_reward FROM videos WHERE video_id = %s", (video_id,))
            v = cursor.fetchone()
            if v: return {"video_id": v[0], "link": v[1], "duration": v[2], "points": v[3]}
            return None
    except Exception as e: 
        logger.error(f"Error getting video by ID {video_id}: {e}", exc_info=True)
        return None
    finally: 
        if conn: conn.close()

def add_withdrawal_request(user_id, bkash_number, points, amount_taka):
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO withdrawal_requests (user_id, bkash_number, points_withdrawn, amount_taka) VALUES (%s, %s, %s, %s) RETURNING request_id", 
                (user_id, bkash_number, points, amount_taka)
            )
            request_id = cursor.fetchone()[0]
            conn.commit()
            return request_id
    except Exception as e: 
        logger.error(f"Error adding withdrawal request for {user_id}: {e}", exc_info=True)
        if conn: conn.rollback()
        return None
    finally: 
        if conn: conn.close()

def get_pending_withdrawals():
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT w.request_id, w.user_id, u.username, w.bkash_number, w.points_withdrawn, w.amount_taka, w.request_time FROM withdrawal_requests w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending' ORDER BY w.request_time ASC")
            return cursor.fetchall()
    except Exception as e: 
        logger.error(f"Error getting pending withdrawals: {e}", exc_info=True)
        return []
    finally: 
        if conn: conn.close()

def update_withdrawal_status(request_id, status):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE withdrawal_requests SET status = %s WHERE request_id = %s", (status, request_id))
            conn.commit()
    except Exception as e: 
        logger.error(f"Error updating withdrawal status for {request_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()

def can_user_watch_video(user_id, video_id):
    conn = get_db_connection()
    if not conn: return False, -1
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT last_watched_timestamp FROM user_video_watch_history WHERE user_id = %s AND video_id = %s", (user_id, video_id))
            result = cursor.fetchone()
            if result:
                last_watched_time = result[0]; current_time = int(time.time())
                if (current_time - last_watched_time) < WATCH_COOLDOWN_SECONDS:
                    return False, WATCH_COOLDOWN_SECONDS - (current_time - last_watched_time) 
                return True, 0 
            return True, 0 
    except Exception as e: 
        logger.error(f"Error checking watch history for user {user_id}, video {video_id}: {e}")
        return False, -1 
    finally: 
        if conn: conn.close()

def record_video_watch(user_id, video_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            current_time = int(time.time())
            cursor.execute(
                """
                INSERT INTO user_video_watch_history (user_id, video_id, last_watched_timestamp) 
                VALUES (%s, %s, %s) 
                ON CONFLICT (user_id, video_id) 
                DO UPDATE SET last_watched_timestamp = EXCLUDED.last_watched_timestamp;
                """, 
                (user_id, video_id, current_time)
            )
            conn.commit()
            logger.info(f"Recorded watch for user {user_id}, video {video_id} at {current_time}")
    except Exception as e: 
        logger.error(f"Error recording watch history for user {user_id}, video {video_id}: {e}")
        if conn: conn.rollback()
    finally: 
        if conn: conn.close()


# --- Telegram Functions (বাকি কোড অপরিবর্তিত থাকবে) ---
async def post_init_setup(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("/start", "বট শুরু করুন"),
            BotCommand("/watch", "ভিডিও দেখুন ও পয়েন্ট অর্জন করুন"),
            BotCommand("/balance", "আপনার বর্তমান পয়েন্ট দেখুন"),
            BotCommand("/referral", "আপনার রেফারেল লিঙ্ক পান"),
            BotCommand("/withdraw", "পয়েন্ট উইথড্র করুন"),
            BotCommand("/cancelwatch", "বর্তমান ভিডিও দেখা বাতিল করুন"),
            BotCommand("/help", "সাহায্য ও কমান্ড তালিকা")
        ])
        logger.info("ডিফল্ট বট কমান্ড সফলভাবে সেট করা হয়েছে।")
    except Exception as e:
        logger.error(f"বট কমান্ড সেট করতে সমস্যা হয়েছে: {e}")

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

    if CHANNEL_ID == 0: 
        logger.warning(f"CHANNEL_ID is 0 or not set. Skipping join check for {user_id}.")
        # এখানে DB_NAME এর পরিবর্তে get_db_connection ব্যবহার করা হয়েছে
        set_channel_joined_status(user_id, True) 
        return True

    if not CHANNEL_USERNAME: 
        logger.warning(f"CHANNEL_USERNAME is not set. Skipping join check as URL cannot be formed for user {user_id}.")
        return True 

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
        user_data = get_user(user.id) # রিফ্রেশ 

    if not user_data: 
        logger.critical(f"Failed to get/create user_data for {user.id} after add_user attempt.")
        await update.message.reply_text("একটি গুরুতর ত্রুটি হয়েছে। অনুগ্রহ করে অ্যাডমিনের সাথে যোগাযোগ করুন।")
        return
    
    # ইউজারনেম আপডেট যদি পরিবর্তন হয়ে থাকে
    if user_data.get('username') != username_to_store:
        conn_s = get_db_connection()
        if conn_s:
            try:
                with conn_s.cursor() as cursor_s:
                    cursor_s.execute("UPDATE users SET username = %s WHERE user_id = %s", (username_to_store, user.id))
                    conn_s.commit(); logger.info(f"Updated username for user {user.id} to {username_to_store}")
                    user_data['username'] = username_to_store 
            except Exception as e: logger.error(f"Error updating username for user {user.id}: {e}")
            finally: 
                if conn_s: conn_s.close()

    if CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context): 
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
    
    bot_info = await context.bot.get_me(); bot_username = bot_info.username
    
    ref_code_from_db = user_data.get('referral_code')
    conn_ref = None
    if not ref_code_from_db: 
        new_code = generate_referral_code() 
        conn_ref = get_db_connection()
        if conn_ref:
            try:
                with conn_ref.cursor() as c:
                    c.execute("UPDATE users SET referral_code = %s WHERE user_id = %s AND (referral_code IS NULL OR referral_code = '')", (new_code, user.id))
                    conn_ref.commit()
                    if c.rowcount > 0:
                        logger.info(f"Generated and set missing referral code {new_code} for user {user.id} during /start.")
                        ref_code_from_db = new_code 
                    else: # যদি কোনো কারণে আপডেট না হয়, আবার ডেটাবেস থেকে চেক করুন
                        user_data_refreshed_for_code = get_user(user.id)
                        if user_data_refreshed_for_code and user_data_refreshed_for_code.get('referral_code'):
                            ref_code_from_db = user_data_refreshed_for_code.get('referral_code')
                        else:
                            logger.warning(f"Could not set new referral code for user {user.id} in /start. Existing might be non-empty or DB issue.")
            except psycopg2.IntegrityError: 
                logger.warning(f"Generated referral code {new_code} already exists (PostgreSQL). User {user.id} might need manual check or retry /start.")
                if conn_ref: conn_ref.rollback()
            except Exception as e_db: 
                logger.error(f"Error setting generated ref code for {user.id} in /start: {e_db}")
                if conn_ref: conn_ref.rollback()
            finally: 
                if conn_ref: conn_ref.close()

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
        fallback_text = welcome_message.replace("`", "") 
        await update.message.reply_text(fallback_text, disable_web_page_preview=True)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return 
    user_data = get_user(update.effective_user.id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)) :
        await start_command(update, context); return
        
    help_text = (
        "সাধারণ কমান্ড:\n"
        "/start - বট শুরু করুন\n"
        "/watch - ভিডিও দেখুন ও পয়েন্ট অর্জন করুন\n"
        "/balance - আপনার বর্তমান পয়েন্ট দেখুন\n"
        "/referral - আপনার রেফারেল লিঙ্ক পান\n"
        "/withdraw - পয়েন্ট উইথড্র করুন\n"
        "/cancelwatch - বর্তমান ভিডিও দেখা বাতিল করুন\n"
        "/help - এই সাহায্য বার্তা"
    )
    if update.effective_user.id == ADMIN_ID:
        help_text += (
            "\n\nঅ্যাডমিন কমান্ড:\n"
            "`/addvideo <লিঙ্ক> <সেকেন্ড> <পয়েন্ট>` - নতুন ভিডিও যোগ করুন\n"
            "`/listvideos` - সব ভিডিওর তালিকা দেখুন (আইডি সহ)\n"
            "`/updatevideo <আইডি> <নতুন_লিঙ্ক> <নতুন_সেকেন্ড> <নতুন_পয়েন্ট>` - ভিডিও তথ্য আপডেট করুন\n"
            "`/pendingwithdrawals` - পেন্ডিং উইথড্রয়াল দেখুন\n"
            "`/approve <রিকোয়েস্ট_আইডি>` - উইথড্রয়াল অনুমোদন করুন\n"
            "`/reject <রিকোয়েস্ট_আইডি> [কারণ]` - উইথড্রয়াল বাতিল করুন\n"
            "`/approveclaim <ক্লেইম_আইডি>` - পয়েন্ট ক্লেইম অনুমোদন করুন\n"
            "`/rejectclaim <ক্লেইম_আইডি> [কারণ]` - পয়েন্ট ক্লেইম বাতিল করুন"
        )
    
    try:
        await update.message.reply_text(help_text, parse_mode='Markdown')
    except BadRequest as e:
        logger.error(f"Markdown parse error in help_command: {e.message}")
        await update.message.reply_text(help_text.replace("`", "")) 

async def watch_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)):
        await start_command(update, context); return
    
    if user_data['watching_video_id']:
        await update.message.reply_text("আপনি ইতিমধ্যে একটি ভিডিও দেখছেন। /cancelwatch ব্যবহার করুন।"); return
    
    all_videos = get_videos()
    if not all_videos:
        await update.message.reply_text("দুঃখিত, এই মুহূর্তে কোনো ভিডিও উপলব্ধ নেই।"); return

    available_videos_keyboard = []
    for video_info in all_videos:
        video_id_db, _, duration, points = video_info # video_id_db ব্যবহার করছি কারণ video_id লোকাল ভ্যারিয়েবল হতে পারে
        can_watch, remaining_time = can_user_watch_video(user_id, video_id_db)
        if can_watch:
            button_text = f"🔗 দেখুন - {points} পয়েন্ট (সময়: {duration}s)"
            available_videos_keyboard.append([InlineKeyboardButton(button_text, callback_data=f"watch_{video_id_db}")])
        else:
            # hours, remainder = divmod(remaining_time, 3600)
            # minutes, _ = divmod(remainder, 60)
            pass # এই ভিডিও এখন দেখানো হবে না

    if not available_videos_keyboard:
        await update.message.reply_text("আপনার জন্য এই মুহূর্তে দেখার মতো কোনো নতুন ভিডিও নেই। অনুগ্রহ করে পরে আবার চেষ্টা করুন।")
        return
    await update.message.reply_text("দেখার জন্য একটি ভিডিও নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(available_videos_keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    if not query.from_user: logger.error("CallbackQuery no from_user"); return 
    user_id = query.from_user.id; user_first_name_from_callback = query.from_user.first_name 
    logger.info(f"Button callback: User {user_id}, Data {data}")
    user_data = get_user(user_id) # callback এর শুরুতে user_data লোড করা ভালো
    if not user_data: 
        if query.message: await query.message.reply_text("অনুগ্রহ করে /start দিন।"); return
    
    if data == "check_join":
        is_member_api = await check_channel_join(update, context); user_data_refreshed = get_user(user_id) 
        if is_member_api and user_data_refreshed and user_data_refreshed['channel_joined']:
            user_first_name_safe = escape_markdown(user_first_name_from_callback, version=1)
            referral_code = user_data_refreshed.get('referral_code'); bot_info = await context.bot.get_me(); bot_username = bot_info.username
            ref_link_msg_part = ""; conn_cb = None
            if not referral_code: 
                new_code_cb = generate_referral_code()
                conn_cb = get_db_connection()
                if conn_cb:
                    try:
                        with conn_cb.cursor() as c:
                            c.execute("UPDATE users SET referral_code = %s WHERE user_id = %s AND (referral_code IS NULL OR referral_code = '')", (new_code_cb, user_id))
                            conn_cb.commit()
                            if c.rowcount > 0: referral_code = new_code_cb
                            else: 
                                user_data_recheck = get_user(user_id)
                                if user_data_recheck: referral_code = user_data_recheck.get('referral_code')
                    except psycopg2.IntegrityError:
                         logger.warning(f"IntegrityError for referral code on check_join for user {user_id}")
                         if conn_cb: conn_cb.rollback()
                    except Exception as e_ref_cb: 
                        logger.error(f"Error setting ref code in check_join: {e_ref_cb}")
                        if conn_cb: conn_cb.rollback()
                    finally: 
                        if conn_cb: conn_cb.close()

            if referral_code: actual_link_url = f"https://t.me/{bot_username}?start={referral_code}"; ref_link_msg_part = f"আপনার রেফারেল কোড: `{actual_link_url}`\n\n"
            else: ref_link_msg_part = "রেফারেল কোড তৈরিতে সমস্যা। /start দিন।\n\n"
            final_message = f"স্বাগতম, {user_first_name_safe}!\nভিডিও দেখে পয়েন্ট অর্জন করুন।\n{ref_link_msg_part}কমান্ড তালিকা:\n/watch\n/balance\n/referral\n/withdraw\n/help"
            try: await query.edit_message_text(text=final_message, parse_mode='Markdown', disable_web_page_preview=True)
            except Exception as e: 
                logger.error(f"Error editing message in check_join: {e}")
                if query.message: await query.message.reply_text(final_message.replace("`",""), disable_web_page_preview=True)
        else:
            keyboard = [[InlineKeyboardButton(f"চ্যানেলে জয়েন করুন (@{CHANNEL_USERNAME})", url=f"https://t.me/{CHANNEL_USERNAME}")], [InlineKeyboardButton("✅ জয়েন করেছি, চেক করুন", callback_data="check_join")]]
            if query.message: # মেসেজ এডিট করার আগে চেক করা
                await query.edit_message_text(text=f"আপনি এখনও চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করেননি।", reply_markup=InlineKeyboardMarkup(keyboard))
            else: # যদি কোনো কারণে মেসেজ অবজেক্ট না থাকে (খুব বিরল)
                await context.bot.send_message(chat_id=user_id, text=f"আপনি এখনও চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করেননি।", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # চ্যানেল জয়েন চেক (যদি user_data তে false থাকে)
    if CHANNEL_ID != 0 and CHANNEL_USERNAME and (not user_data or not user_data['channel_joined']): 
        if not await check_channel_join(update, context):
             if query.message: await query.message.reply_text(f"অনুগ্রহ করে প্রথমে চ্যানেলে (@{CHANNEL_USERNAME}) জয়েন করুন এবং তারপর /start কমান্ড দিন।"); return

    if data.startswith("watch_"):
        if user_data['watching_video_id']: # user_data এখান থেকে ব্যবহার করা হচ্ছে
            if query.message: await query.message.reply_text("আপনি ইতিমধ্যে একটি ভিডিও দেখছেন।"); return
        
        try: video_id_to_watch = int(data.split("_")[1])
        except (IndexError, ValueError): 
            if query.message: await query.message.reply_text("অবৈধ ভিডিও আইডি।"); return
        
        can_watch_now, rem_time = can_user_watch_video(user_id, video_id_to_watch)
        if not can_watch_now:
            h,r = divmod(rem_time,3600); m,_ = divmod(r,60)
            if query.message: await query.edit_message_text(f"এই ভিডিওটি আপনি {int(h)} ঘণ্টা {int(m)} মিনিট পর আবার দেখতে পারবেন।"); return
        
        video = get_video_by_id(video_id_to_watch)
        if not video: 
            if query.message: await query.edit_message_text("ভিডিওটি আর উপলব্ধ নেই।"); return 
        
        set_watching_video(user_id, video_id_to_watch, int(time.time()))
        keyboard = [[InlineKeyboardButton("✅ সম্পূর্ণ দেখেছি", callback_data=f"watched_{video_id_to_watch}")]]
        if query.message: await query.edit_message_text(f"দেখছেন: {video['link']}\nদৈর্ঘ্য: {video['duration']}s.\nসম্পূর্ণ দেখলে {video['points']} পয়েন্ট।", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=False)
        return 
    
    if data.startswith("watched_"): 
        logger.warning(f"Callback '{data}' by general_button_callback, should be ConversationHandler.")
        if query.message: await query.edit_message_text("ক্লেইম প্রসেস করা হচ্ছে...") 

# ... (claim_entry_point এবং অন্যান্য ConversationHandler ফাংশনগুলো অপরিবর্তিত থাকবে,
# শুধুমাত্র ডেটাবেস ফাংশন কলগুলো PostgreSQL এর জন্য আপডেট করা হয়েছে)

async def claim_entry_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data 
    user_id = query.from_user.id
    user_data_claim = get_user(user_id) # এখানে user_data claim এর জন্য লোড করা
    if not user_data_claim: 
        await query.edit_message_text("ব্যবহারকারীর তথ্য নেই। /start দিন।")
        return ConversationHandler.END
    
    try: 
        claimed_video_id = int(data.split("_")[1])
    except (IndexError, ValueError): 
        await query.edit_message_text("অবৈধ ভিডিও আইডি।")
        return ConversationHandler.END
        
    # watching_video_id এবং video_start_time user_data_claim থেকে নেওয়া হচ্ছে
    if not user_data_claim.get('watching_video_id') or \
       user_data_claim.get('watching_video_id') != claimed_video_id or \
       not user_data_claim.get('video_start_time'):
        await query.edit_message_text("মনে হচ্ছে আপনি ইতিমধ্যে এই ভিডিওর জন্য ক্লেইম করেছেন অথবা দেখা বাতিল করেছেন।")
        clear_watching_video(user_id) 
        return ConversationHandler.END

    current_video = get_video_by_id(claimed_video_id) 
    if not current_video: 
        await query.edit_message_text("ত্রুটি। ভিডিওর তথ্য পাওয়া যায়নি।")
        clear_watching_video(user_id)
        return ConversationHandler.END
        
    time_elapsed = int(time.time()) - user_data_claim['video_start_time']

    if time_elapsed >= current_video['duration']:
        points_to_claim = current_video['points']
        # user_data_claim থেকে username এবং full_name নেওয়া হচ্ছে
        telegram_username = user_data_claim.get('username', query.from_user.username or "N/A")
        telegram_fullname = query.from_user.full_name # এটি টেলিগ্রাম অবজেক্ট থেকেই ভালো
        
        claim_id = f"claim_{user_id}_{claimed_video_id}_{int(time.time())}"
        
        PENDING_CLAIMS[claim_id] = {
            "user_id": user_id,
            "video_id": claimed_video_id,
            "points": points_to_claim,
            "status": "pending_screenshot",
            "telegram_username": telegram_username,
            "telegram_fullname": telegram_fullname
        }
        context.user_data['current_claim_id'] = claim_id
        
        clear_watching_video(user_id) 

        await query.edit_message_text(
            f"দেখা সম্পন্ন। পয়েন্ট ক্লেইম করতে, ভিডিওর শেষ মুহূর্তের স্ক্রিনশট পাঠান। ক্লেইম বাতিল করতে /cancelclaim টাইপ করুন।"
        )
        return CLAIM_ASK_SCREENSHOT 
    else:
        msg = f"ভিডিওটি সম্পূর্ণ দেখেননি। আরও {current_video['duration'] - time_elapsed}s দেখুন।"
        try:
            if query.message: await query.message.reply_text(msg) 
            else: await query.edit_message_text(msg) # এটি সম্ভবত কাজ করবে না যদি মূল মেসেজ না থাকে
        except BadRequest as e: 
            await context.bot.send_message(chat_id=user_id, text=msg)
            logger.warning(f"Could not edit message for claim denial (too short watch): {e}")
        return ConversationHandler.END

# received_screenshot_for_claim, received_user_text_for_claim, cancel_point_claim_conversation অপরিবর্তিত

async def received_screenshot_for_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        await update.message.reply_text("অনুগ্রহ করে একটি স্ক্রিনশট (ছবি) পাঠান। /cancelclaim দিয়ে বাতিল করতে পারেন।")
        return CLAIM_ASK_SCREENSHOT 

    user_id = update.effective_user.id; claim_id = context.user_data.get('current_claim_id')
    if not claim_id or claim_id not in PENDING_CLAIMS or PENDING_CLAIMS[claim_id]["user_id"] != user_id:
        await update.message.reply_text("ক্লেইম সেশন নেই। আবার ভিডিও দেখুন।"); context.user_data.clear(); return ConversationHandler.END
    
    PENDING_CLAIMS[claim_id]["screenshot_file_id"] = update.message.photo[-1].file_id
    PENDING_CLAIMS[claim_id]["status"] = "pending_user_text"
    await update.message.reply_text(f"স্ক্রিনশট পেয়েছি। এখন, যাচাইয়ের জন্য একটি টেক্সট পাঠান। /cancelclaim দিয়ে বাতিল করতে পারেন।")
    return CLAIM_ASK_USER_TEXT

async def received_user_text_for_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        await update.message.reply_text("অনুগ্রহ করে টেক্সট ফরম্যাটে পাঠান। /cancelclaim দিয়ে বাতিল করতে পারেন।")
        return CLAIM_ASK_USER_TEXT
    user_id = update.effective_user.id; user_submitted_text = update.message.text; claim_id = context.user_data.get('current_claim_id')
    if not claim_id or claim_id not in PENDING_CLAIMS or PENDING_CLAIMS[claim_id]["user_id"] != user_id:
        await update.message.reply_text("ক্লেইম সেশন নেই।"); context.user_data.clear(); return ConversationHandler.END
    
    claim_data = PENDING_CLAIMS[claim_id]; video_id = claim_data["video_id"]; points = claim_data["points"]
    screenshot_file_id = claim_data.get("screenshot_file_id")
    username_safe = escape_markdown(claim_data.get('telegram_username', f'User_{user_id}'),version=1)
    user_display_name_safe = escape_markdown(claim_data.get('telegram_fullname', 'N/A'),version=1)
    admin_message_text = (f"🔔 নতুন পয়েন্ট ক্লেইম!\n\n*ব্যবহারকারী:* {user_display_name_safe} (`@{username_safe}`, ID: `{user_id}`)\n*ভিডিও ID:* `{video_id}`\n*পয়েন্ট ক্লেইম:* {points}\n*ব্যবহারকারীর টেক্সট:*\n`{escape_markdown(user_submitted_text,version=1)}`\n*ক্লেইম ID:* `{claim_id}`\n\nঅনুমোদন: `/approveclaim {claim_id}`\nবাতিল: `/rejectclaim {claim_id}`")
    
    try:
        if ADMIN_ID != 0:
            if screenshot_file_id:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message_text, parse_mode='Markdown')
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=screenshot_file_id, caption=f"ক্লেইম ID `{claim_id}` এর স্ক্রিনশট। ব্যবহারকারী: {user_display_name_safe} (`@{username_safe}`)", parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message_text + "\n\n_(স্ক্রিনশট নেই)_", parse_mode='Markdown')
        
        await update.message.reply_text("ক্লেইম অনুরোধ অ্যাডমিনের কাছে পাঠানো হয়েছে। অপেক্ষা করুন।")
        PENDING_CLAIMS[claim_id]["status"] = "pending_admin_approval"; PENDING_CLAIMS[claim_id]["user_submitted_text"] = user_submitted_text
    except Exception as e: 
        logger.error(f"Error sending claim to admin: {e}")
        await update.message.reply_text("অনুরোধ পাঠাতে সমস্যা হয়েছে।")
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_point_claim_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    claim_id = context.user_data.get('current_claim_id')
    user_id = update.effective_user.id if update.effective_user else "UnknownUser"
    logger.info(f"User {user_id} cancelled point claim. Claim ID in context: {claim_id}")
    
    if claim_id and claim_id in PENDING_CLAIMS:
        if PENDING_CLAIMS[claim_id]["status"] not in ["pending_admin_approval", "approved", "rejected"]:
             logger.info(f"Deleting pending claim {claim_id} due to cancellation by user.")
             del PENDING_CLAIMS[claim_id]
        else: 
            logger.info(f"Claim {claim_id} already sent/processed. Not deleting from PENDING_CLAIMS on user cancel.")
    
    context.user_data.clear()
    await update.message.reply_text("পয়েন্ট ক্লেইম প্রক্রিয়া বাতিল করা হয়েছে।")
    return ConversationHandler.END


async def cancel_watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user: return 
    user_id = update.effective_user.id
    user_data_cw = get_user(user_id) # cancel_watch এর জন্য user_data লোড
    if not user_data_cw or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    if user_data_cw['watching_video_id']:
        clear_watching_video(user_id); await update.message.reply_text("ভিডিও দেখা বাতিল হয়েছে।")
    else: await update.message.reply_text("আপনি কোনো ভিডিও দেখছেন না।")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user: return 
    user_id = update.effective_user.id
    user_data_bal = get_user(user_id) # balance এর জন্য user_data লোড
    if not user_data_bal or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    await update.message.reply_text(f"আপনার বর্তমান পয়েন্ট: {user_data_bal['points']}")

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return 
    user_id = update.effective_user.id
    user_data_ref = get_user(user_id) # referral এর জন্য user_data লোড
    if not user_data_ref or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)): 
        await start_command(update, context); return
    
    ref_code = user_data_ref.get('referral_code')
    conn_ref_update = None
    if not ref_code: 
        new_code_ref_cmd = generate_referral_code()
        conn_ref_update = get_db_connection()
        if conn_ref_update:
            try: 
                with conn_ref_update.cursor() as c:
                    c.execute("UPDATE users SET referral_code = %s WHERE user_id = %s AND (referral_code IS NULL OR referral_code = '')", (new_code_ref_cmd, user_id))
                    conn_ref_update.commit()
                    if c.rowcount > 0: 
                        ref_code = new_code_ref_cmd
                        logger.info(f"Generated/set missing ref code {new_code_ref_cmd} for user {user_id} in referral_command.")
                    else: 
                        user_data_recheck = get_user(user_id) 
                        if user_data_recheck and user_data_recheck.get('referral_code'): 
                            ref_code = user_data_recheck.get('referral_code')
                        else: 
                            logger.warning(f"Could not set new ref code for user {user_id} in referral_command after recheck.")
            except psycopg2.IntegrityError: 
                logger.warning(f"Generated ref code {new_code_ref_cmd} (referral_command) already exists for user {user_id}.")
                if conn_ref_update: conn_ref_update.rollback()
            except Exception as e_db: 
                logger.error(f"DB error setting ref code for {user_id} in referral_command: {e_db}")
                if conn_ref_update: conn_ref_update.rollback()
            finally: 
                if conn_ref_update: conn_ref_update.close()

    if not ref_code:
        await update.message.reply_text("আপনার রেফারেল কোড তৈরিতে একটি সমস্যা হয়েছে। অনুগ্রহ করে আবার /start কমান্ড দিন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।")
        logger.error(f"Failed to get/generate referral code for user {user_id} in referral_command.")
        return

    bot_info = await context.bot.get_me(); bot_username = bot_info.username 
    actual_link_url = f"https://t.me/{bot_username}?start={ref_code}"

    count = 0
    conn_ref_count = get_db_connection()
    if conn_ref_count:
        try:
            with conn_ref_count.cursor() as c_count:
                c_count.execute("SELECT COUNT(*) FROM users WHERE referred_by = %s", (user_id,))
                count_result = c_count.fetchone()
                if count_result: 
                    count = count_result[0]
        except Exception as e_count: 
            logger.error(f"Error counting referrals for {user_id}: {e_count}")
        finally: 
            if conn_ref_count: conn_ref_count.close()
    
    message_text = f"আপনার রেফারেল লিঙ্ক: `{actual_link_url}`\n" \
                   f"এটি বন্ধুদের সাথে শেয়ার করে পয়েন্ট অর্জন করুন!\n\n" \
                   f"মোট রেফার: {count} জন\n" \
                   f"প্রতি রেফারে ভিডিও দেখার পর আপনি {REFERRAL_PERCENTAGE*100:.0f}% কমিশন পাবেন।"
    
    logger.info(f"Referral command message content for user {user_id}: [{message_text}]")
    try:
        await update.message.reply_text(message_text, parse_mode='Markdown', disable_web_page_preview=True)
    except BadRequest: 
        await update.message.reply_text(message_text.replace("`",""), disable_web_page_preview=True)

# withdraw_command এবং এর সাথে সম্পর্কিত ConversationHandler ফাংশনগুলো অপরিবর্তিত

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user: return 
    user_id = update.effective_user.id
    user_data_wd = get_user(user_id) # withdraw এর জন্য user_data লোড
    if not user_data_wd or (CHANNEL_ID != 0 and CHANNEL_USERNAME and not await check_channel_join(update, context)):
        await start_command(update, context); return ConversationHandler.END
    MIN_WITHDRAW = 10 
    if user_data_wd['points'] < MIN_WITHDRAW:
        await update.message.reply_text(f"উইথড্র করতে কমপক্ষে {MIN_WITHDRAW} পয়েন্ট প্রয়োজন। আপনার আছে {user_data_wd['points']} পয়েন্ট।"); return ConversationHandler.END
    await update.message.reply_text("আপনার বিকাশ নম্বর দিন (১১ সংখ্যার):"); return ASK_BKASH_NUMBER

async def ask_bkash_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    bkash_no = update.message.text
    if not bkash_no.isdigit() or len(bkash_no) != 11:
        await update.message.reply_text("সঠিক ১১ সংখ্যার বিকাশ নম্বর দিন। /withdraw আবার চেষ্টা করুন।"); return ConversationHandler.END
    context.user_data['bkash_number'] = bkash_no
    if not update.effective_user: return ConversationHandler.END 
    user_data_bkash = get_user(update.effective_user.id) # bkash নম্বর পাওয়ার পর user_data লোড
    if not user_data_bkash: await update.message.reply_text("ত্রুটি। /start করুন।"); return ConversationHandler.END 
    max_taka = user_data_bkash['points'] * POINTS_TO_TAKA_RATE
    await update.message.reply_text(f"কত পয়েন্ট উইথড্র করতে চান? (আপনার আছে {user_data_bkash['points']} পয়েন্ট, যা প্রায় {max_taka:.2f} টাকা)\nন্যূনতম ১০ পয়েন্ট উইথড্র করতে পারবেন।"); return ASK_WITHDRAW_POINTS

async def ask_withdraw_points_received(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user: return ConversationHandler.END 
    user_id = update.effective_user.id; user_data_wd_pts = get_user(user_id) # withdraw points পাওয়ার পর user_data লোড
    bkash_no = context.user_data.get('bkash_number')
    if not user_data_wd_pts or not bkash_no:
        await update.message.reply_text("ত্রুটি। /withdraw আবার করুন।"); context.user_data.clear(); return ConversationHandler.END
    try: points_wd = int(update.message.text)
    except ValueError: await update.message.reply_text("সঠিক সংখ্যায় পয়েন্ট লিখুন।"); context.user_data.clear(); return ConversationHandler.END
    if points_wd <= 0: await update.message.reply_text("পয়েন্ট ০ এর বেশি হতে হবে।"); context.user_data.clear(); return ConversationHandler.END
    if points_wd > user_data_wd_pts['points']: await update.message.reply_text(f"পর্যাপ্ত পয়েন্ট নেই ({user_data_wd_pts['points']})।"); context.user_data.clear(); return ConversationHandler.END
    MIN_REQ_POINTS = 10 
    if points_wd < MIN_REQ_POINTS: await update.message.reply_text(f"কমপক্ষে {MIN_REQ_POINTS} পয়েন্ট উইথড্র করতে হবে।"); context.user_data.clear(); return ConversationHandler.END
    amount_tk = points_wd * POINTS_TO_TAKA_RATE
    update_user_points(user_id, -points_wd) # পয়েন্ট কমানো
    req_id = add_withdrawal_request(user_id, bkash_no, points_wd, amount_tk)
    if req_id is None: # যদি অনুরোধ যোগ করতে সমস্যা হয়
        await update.message.reply_text("উইথড্রয়াল অনুরোধে সমস্যা। পয়েন্ট ফেরত দেওয়া হয়েছে।"); update_user_points(user_id, points_wd); context.user_data.clear(); return ConversationHandler.END
    
    user_full_name_safe = escape_markdown(update.effective_user.full_name or "N/A", version=1)
    user_username_safe = escape_markdown(update.effective_user.username or "N/A", version=1)
    await update.message.reply_text(f"আপনার উইথড্রয়াল অনুরোধ সফলভাবে জমা হয়েছে!\nID: {req_id}\nবিকাশ নম্বর: {bkash_no}\nউইথড্র করা পয়েন্ট: {points_wd}\nটাকার পরিমাণ: {amount_tk:.2f} টাকা\n\nঅ্যাডমিন আপনার অনুরোধটি পর্যালোচনা করে শীঘ্রই ব্যবস্থা নিবেন।")
    if ADMIN_ID != 0:
        admin_notify_text = (f"🔔 নতুন উইথড্রয়াল অনুরোধ!\nব্যবহারকারী: {user_full_name_safe} (`@{user_username_safe}`, ID: `{user_id}`)\nরিকোয়েস্ট ID: `{req_id}`\nবিকাশ নম্বর: `{bkash_no}`\nপয়েন্ট: {points_wd}\nটাকা: {amount_tk:.2f}\n\nঅনুমোদন করতে: `/approve {req_id}`\nবাতিল করতে: `/reject {req_id}`")
        try: await context.bot.send_message(ADMIN_ID, admin_notify_text, parse_mode='Markdown')
        except Exception as e: logger.error(f"Failed to send admin WD notification: {e}")
    context.user_data.clear(); return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if context.user_data: await update.message.reply_text("উইথড্র প্রক্রিয়া বাতিল করা হয়েছে।"); context.user_data.clear()
    else: await update.message.reply_text("কোনো সক্রিয় প্রক্রিয়া (যেমন উইথড্র) চালু নেই।")
    return ConversationHandler.END

# --- Admin Functions (PostgreSQL এর জন্য আপডেট করা) ---

async def admin_add_video(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return 
    if len(context.args) != 3: await update.message.reply_text("ব্যবহার: `/addvideo <ইউটিউব_লিঙ্ক> <সময়_সেকেন্ডে> <পয়েন্ট>`", parse_mode='Markdown'); return
    link, dur_s, pts_s = context.args
    try: dur = int(dur_s); pts = int(pts_s); assert dur > 0 and pts > 0
    except: await update.message.reply_text("সময় (সেকেন্ডে) এবং পয়েন্ট অবশ্যই ধনাত্মক সংখ্যা হতে হবে।"); return
    if not ("youtube.com/" in link or "youtu.be/" in link): await update.message.reply_text("অনুগ্রহ করে একটি সঠিক ইউটিউব লিঙ্ক দিন।"); return
    
    vid_id = add_video(link, dur, pts) # add_video এখন video_id রিটার্ন করে
    if vid_id: await update.message.reply_text(f"ভিডিও সফলভাবে যোগ করা হয়েছে (ID: `{vid_id}`)।", parse_mode='Markdown')
    else: await update.message.reply_text("এই ইউটিউব লিঙ্কটি ইতিমধ্যে ডাটাবেসে বিদ্যমান অথবা ভিডিও যোগ করতে কোনো সমস্যা হয়েছে।")

async def admin_list_videos(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return
    videos = get_videos()
    if not videos: await update.message.reply_text("কোনো ভিডিও ডেটাবেসে যোগ করা হয়নি।"); return
    message_parts = ["*ডেটাবেসে থাকা সব ভিডিওর তালিকা:*\n\n"]
    for video in videos:
        part = (f"*ID:* `{video[0]}`\n*লিঙ্ক:* {escape_markdown(video[1], version=1)}\n*সময়:* {video[2]}s, *পয়েন্ট:* {video[3]}\n---\n")
        if sum(len(p) for p in message_parts) + len(part) > 4090: 
            try: await update.message.reply_text("".join(message_parts), parse_mode='Markdown', disable_web_page_preview=True)
            except BadRequest as e: logger.error(f"Error sending listvideos part: {e}"); await update.message.reply_text("ভিডিও তালিকা পাঠাতে সমস্যা হয়েছে (অংশবিশেষ)।")
            message_parts = [part] 
        else: message_parts.append(part)
    if message_parts and (len(message_parts) > 1 or (len(message_parts)==1 and message_parts[0] != "*ডেটাবেসে থাকা সব ভিডিওর তালিকা:*\n\n")): 
        try: await update.message.reply_text("".join(message_parts), parse_mode='Markdown', disable_web_page_preview=True)
        except BadRequest as e: logger.error(f"Error sending final listvideos part: {e}"); await update.message.reply_text("ভিডিও তালিকা পাঠাতে সমস্যা হয়েছে।")

async def admin_update_video(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return
    usage_text = "ব্যবহার: `/updatevideo <আইডি> <নতুন_লিঙ্ক> <নতুন_সময়> <নতুন_পয়েন্ট>`" 
    if not context.args or len(context.args) != 4: 
        await update.message.reply_text(usage_text, parse_mode='Markdown'); return
    
    conn_uv = get_db_connection()
    if not conn_uv: 
        await update.message.reply_text("ডেটাবেস কানেকশনে সমস্যা।"); return

    try:
        video_id_to_update = int(context.args[0]); new_link = context.args[1]
        new_duration = int(context.args[2]); new_points = int(context.args[3])
        if not ("youtube.com/" in new_link or "youtu.be/" in new_link):
            await update.message.reply_text("অনুগ্রহ করে একটি সঠিক ইউটিউব লিঙ্ক দিন।"); return
        if new_duration <= 0 or new_points <= 0:
            await update.message.reply_text("সময় এবং পয়েন্ট অবশ্যই ধনাত্মক সংখ্যা হতে হবে।"); return
        
        with conn_uv.cursor() as cursor:
            # নতুন লিঙ্ক অন্য কোনো ভিডিওতে ব্যবহৃত হচ্ছে কিনা চেক করুন
            cursor.execute("SELECT video_id FROM videos WHERE youtube_link = %s AND video_id != %s", (new_link, video_id_to_update))
            existing_link_other_video = cursor.fetchone()
            if existing_link_other_video:
                await update.message.reply_text(f"ত্রুটি: লিঙ্ক `{escape_markdown(new_link,version=1)}` ইতিমধ্যে ভিডিও আইডি `{existing_link_other_video[0]}` এর জন্য ব্যবহৃত হচ্ছে।", parse_mode='Markdown')
                return 
            
            cursor.execute(
                "UPDATE videos SET youtube_link = %s, duration_seconds = %s, points_reward = %s WHERE video_id = %s", 
                (new_link, new_duration, new_points, video_id_to_update)
            )
            conn_uv.commit()
            if cursor.rowcount > 0:
                await update.message.reply_text(f"ভিডিও আইডি `{video_id_to_update}` সফলভাবে আপডেট করা হয়েছে।\n*নতুন লিঙ্ক:* {escape_markdown(new_link,version=1)}\n*নতুন সময়:* {new_duration}s, *নতুন পয়েন্ট:* {new_points}", parse_mode='Markdown', disable_web_page_preview=True)
            else:
                await update.message.reply_text(f"ভিডিও আইডি `{video_id_to_update}` খুঁজে পাওয়া যায়নি অথবা কোনো তথ্য পরিবর্তন করা হয়নি।", parse_mode='Markdown')

    except ValueError: await update.message.reply_text("অবৈধ ইনপুট। আইডি, সময় ও পয়েন্ট সংখ্যা হতে হবে।\n" + usage_text, parse_mode='Markdown')
    except psycopg2.IntegrityError: 
        await update.message.reply_text(f"ত্রুটি: লিঙ্ক `{escape_markdown(new_link,version=1)}` সম্ভবত অন্য কোনো ভিডিওর জন্য ইতিমধ্যে ব্যবহৃত হচ্ছে।", parse_mode='Markdown')
        if conn_uv: conn_uv.rollback()
    except Exception as e: 
        logger.error(f"Error updating video {context.args[0] if context.args else 'N/A'}: {e}")
        await update.message.reply_text("ভিডিও আপডেট করতে একটি অপ্রত্যাশিত সমস্যা হয়েছে।")
        if conn_uv: conn_uv.rollback()
    finally:
        if conn_uv: conn_uv.close()

async def admin_pending_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return 
    reqs = get_pending_withdrawals()
    if not reqs: await update.message.reply_text("কোনো পেন্ডিং উইথড্রয়াল অনুরোধ নেই।"); return
    msg_parts = ["⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ:*\n\n"]
    for r_id, u_id, u_name, bkash, pts, tk, time_req in reqs:
        u_name_safe = escape_markdown(u_name or 'নাম নেই', version=1)
        bkash_safe = escape_markdown(bkash, version=1)
        # request_time অবজেক্টকে স্ট্রিং এ কনভার্ট করা
        time_req_str = time_req.strftime('%Y-%m-%d %H:%M:%S') if time_req else 'N/A'
        part = f"*ID:* `{r_id}`\n*ব্যবহারকারী:* {u_name_safe} (ID: `{u_id}`)\n*বিকাশ নম্বর:* `{bkash_safe}`\n*পয়েন্ট:* {pts} (প্রায় {tk:.2f} টাকা)\n*অনুরোধের সময়:* {time_req_str}\n`/approve {r_id}`\n`/reject {r_id}`\n\n---\n\n"
        if sum(len(p) for p in msg_parts) + len(part) > 4090: 
            await update.message.reply_text("".join(msg_parts), parse_mode='Markdown'); msg_parts = ["⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ (অংশ ২):*\n\n", part]
        else: msg_parts.append(part)
    if msg_parts and (len(msg_parts) > 1 or msg_parts[0] != "⏳ *পেন্ডিং উইথড্রয়াল অনুরোধসমূহ:*\n\n"):
        await update.message.reply_text("".join(msg_parts), parse_mode='Markdown')


async def admin_process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE, new_status: str): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return 
    cmd_usage = f"ব্যবহার: `/{new_status} <রিকোয়েস্ট_আইডি>{' [কারণ]' if new_status == 'rejected' else ''}`"
    if not context.args or (new_status == 'rejected' and len(context.args) < 1) or (new_status == 'approved' and len(context.args) != 1):
        await update.message.reply_text(cmd_usage, parse_mode='Markdown'); return
    try: req_id_proc = int(context.args[0])
    except ValueError: await update.message.reply_text("রিকোয়েস্ট আইডি একটি সংখ্যা হতে হবে।"); return
    
    reason_raw = " ".join(context.args[1:]) if new_status == 'rejected' and len(context.args) > 1 else "অ্যাডমিন কর্তৃক প্রক্রিয়াজাত।"
    reason_safe = escape_markdown(reason_raw, version=1)
    
    conn_wd_proc = get_db_connection()
    if not conn_wd_proc: 
        await update.message.reply_text("ডেটাবেস কানেকশনে সমস্যা।"); return
    
    req_data = None
    try: 
        with conn_wd_proc.cursor() as c:
            c.execute("SELECT user_id, points_withdrawn, amount_taka, status FROM withdrawal_requests WHERE request_id = %s", (req_id_proc,))
            req_data = c.fetchone()
    except Exception as e: 
        logger.error(f"Error fetching WD {req_id_proc} for {new_status}: {e}")
        await update.message.reply_text("উইথড্রয়াল তথ্য আনতে সমস্যা হয়েছে।"); return
    finally: # কানেকশন এখানে ক্লোজ করা যাবে না, কারণ নিচে update_withdrawal_status কল হবে
        pass 

    if not req_data: 
        await update.message.reply_text(f"রিকোয়েস্ট আইডি `{req_id_proc}` খুঁজে পাওয়া যায়নি।", parse_mode='Markdown')
        if conn_wd_proc: conn_wd_proc.close()
        return
        
    u_id_notify, pts_refund, tk_amt, curr_status = req_data
    if curr_status != 'pending': 
        await update.message.reply_text(f"রিকোয়েস্ট আইডি `{req_id_proc}` ইতিমধ্যে '{curr_status}' হিসেবে চিহ্নিত আছে।", parse_mode='Markdown')
        if conn_wd_proc: conn_wd_proc.close()
        return

    # update_withdrawal_status ফাংশন নিজেই কানেকশন খুলবে এবং বন্ধ করবে
    update_withdrawal_status(req_id_proc, new_status) 
    user_msg_text = ""
    admin_reply_text = ""

    if new_status == 'approved':
        admin_reply_text = f"রিকোয়েস্ট আইডি `{req_id_proc}` সফলভাবে অনুমোদিত হয়েছে। ব্যবহারকারীকে {tk_amt:.2f} টাকা তার বিকাশ নম্বরে পাঠান।"
        user_msg_text = f"🎉 অভিনন্দন! আপনার উইথড্রয়াল অনুরোধ (ID: `{req_id_proc}`) অনুমোদিত হয়েছে। {pts_refund} পয়েন্টের বিনিময়ে {tk_amt:.2f} টাকা শীঘ্রই আপনার বিকাশ অ্যাকাউন্টে পাঠানো হবে।"
    elif new_status == 'rejected':
        # update_user_points ফাংশন নিজেই কানেকশন খুলবে এবং বন্ধ করবে
        update_user_points(u_id_notify, pts_refund) 
        admin_reply_text = f"রিকোয়েস্ট আইডি `{req_id_proc}` বাতিল করা হয়েছে। ব্যবহারকারীকে {pts_refund} পয়েন্ট ফেরত দেওয়া হয়েছে।"
        user_msg_text = f" দুঃখিত, আপনার উইথড্রয়াল অনুরোধ (ID: `{req_id_proc}`) বাতিল করা হয়েছে।\nকারণ: {reason_safe}\nআপনার {pts_refund} পয়েন্ট আপনার অ্যাকাউন্টে ফেরত দেওয়া হয়েছে।"
    
    await update.message.reply_text(admin_reply_text, parse_mode='Markdown')
    if u_id_notify and user_msg_text:
        try: await context.bot.send_message(chat_id=u_id_notify, text=user_msg_text, parse_mode='Markdown')
        except Exception as e: logger.warning(f"Could not notify user {u_id_notify} for WD {req_id_proc} ({new_status}): {e}")
    
    if conn_wd_proc: conn_wd_proc.close() # সব কাজ শেষে কানেকশন বন্ধ

async def admin_approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await admin_process_withdrawal(update, context, 'approved')

async def admin_reject_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await admin_process_withdrawal(update, context, 'rejected')

# admin_approve_claim এবং admin_reject_claim অপরিবর্তিত,
# কারণ update_user_points এবং record_video_watch PostgreSQL এর জন্য আপডেট করা হয়েছে

async def admin_approve_claim(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("ব্যবহার: `/approveclaim <claim_id>`", parse_mode='Markdown'); return
    claim_id_to_approve = context.args[0]
    if claim_id_to_approve in PENDING_CLAIMS:
        claim_data = PENDING_CLAIMS[claim_id_to_approve]
        if claim_data["status"] == "pending_admin_approval":
            user_id_to_reward = claim_data["user_id"]; points_to_add = claim_data["points"]; video_id_watched = claim_data["video_id"]
            update_user_points(user_id_to_reward, points_to_add); record_video_watch(user_id_to_reward, video_id_watched) 
            claim_data["status"] = "approved" 
            # PENDING_CLAIMS থেকে ডিলিট করা যেতে পারে অনুমোদনের পর
            # del PENDING_CLAIMS[claim_id_to_approve] 
            await update.message.reply_text(f"ক্লেইম আইডি `{claim_id_to_approve}` অনুমোদিত। ব্যবহারকারী `{user_id_to_reward}` কে `{points_to_add}` পয়েন্ট দেওয়া হয়েছে।")
            try: await context.bot.send_message(chat_id=user_id_to_reward, text=f"অভিনন্দন! আপনার ভিডিও দেখার (ID: {video_id_watched}) পয়েন্ট ক্লেইম অনুমোদিত হয়েছে এবং আপনি {points_to_add} পয়েন্ট পেয়েছেন।")
            except Exception as e: logger.warning(f"Could not notify user {user_id_to_reward} about approved claim: {e}")
        else: await update.message.reply_text(f"ক্লেইম আইডি `{claim_id_to_approve}` অ্যাডমিন অনুমোদনের জন্য পেন্ডিং নেই। বর্তমান স্ট্যাটাস: {claim_data['status']}")
    else: await update.message.reply_text(f"ক্লেইম আইডি `{claim_id_to_approve}` খুঁজে পাওয়া যায়নি বা ইতিমধ্যে প্রসেস করা হয়েছে।")

async def admin_reject_claim(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    if not update.effective_user or update.effective_user.id != ADMIN_ID: return
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("ব্যবহার: `/rejectclaim <claim_id> [কারণ]`", parse_mode='Markdown'); return
    claim_id_to_reject = context.args[0]; reason = " ".join(context.args[1:]) if len(context.args) > 1 else "অ্যাডমিন কর্তৃক বাতিল।"
    if claim_id_to_reject in PENDING_CLAIMS:
        claim_data = PENDING_CLAIMS[claim_id_to_reject]; user_id_to_notify = claim_data["user_id"]; video_id_rejected = claim_data["video_id"]
        claim_data["status"] = "rejected" 
        # PENDING_CLAIMS থেকে ডিলিট করা যেতে পারে বাতিলের পর
        # del PENDING_CLAIMS[claim_id_to_reject]
        await update.message.reply_text(f"ক্লেইম আইডি `{claim_id_to_reject}` বাতিল করা হয়েছে।")
        try: await context.bot.send_message(chat_id=user_id_to_notify, text=f"দুঃখিত, আপনার ভিডিও (ID: {video_id_rejected}) দেখার পয়েন্ট ক্লেইম বাতিল করা হয়েছে। কারণ: {escape_markdown(reason,version=1)}", parse_mode='Markdown')
        except Exception as e: logger.warning(f"Could not notify user {user_id_to_notify} about rejected claim: {e}")
    else: await update.message.reply_text(f"ক্লেইম আইডি `{claim_id_to_reject}` খুঁজে পাওয়া যায়নি বা ইতিমধ্যে প্রসেস করা হয়েছে।")


# --- Main Function ---
def main():
    # init_db() প্রথমে কল করা হচ্ছে যাতে ডেটাবেস টেবিল তৈরি থাকে
    # Render.com এ PostgreSQL ডাটাবেস ম্যানুয়ালি তৈরি করতে হবে, init_db শুধু টেবিল তৈরি করবে যদি না থাকে।
    init_db() 
    
    application_builder = Application.builder().token(BOT_TOKEN)
    application_builder.post_init(post_init_setup)
    application = application_builder.build()

    withdraw_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("withdraw", withdraw_command)],
        states={
            ASK_BKASH_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bkash_number_received)],
            ASK_WITHDRAW_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_withdraw_points_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)], 
        conversation_timeout=300 
    )

    point_claim_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(claim_entry_point, pattern='^watched_')], 
        states={
            CLAIM_ASK_SCREENSHOT: [MessageHandler(filters.PHOTO, received_screenshot_for_claim)],
            CLAIM_ASK_USER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_user_text_for_claim)],
        },
        fallbacks=[CommandHandler("cancelclaim", cancel_point_claim_conversation)], 
        map_to_parent={ ConversationHandler.END: ConversationHandler.END }, 
        conversation_timeout=600 
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch_video_command))
    application.add_handler(CommandHandler("cancelwatch", cancel_watch_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(withdraw_conv_handler) 

    application.add_handler(CommandHandler("addvideo", admin_add_video))
    application.add_handler(CommandHandler("listvideos", admin_list_videos)) 
    application.add_handler(CommandHandler("updatevideo", admin_update_video)) 
    application.add_handler(CommandHandler("pendingwithdrawals", admin_pending_withdrawals))
    application.add_handler(CommandHandler("approveclaim", admin_approve_claim)) 
    application.add_handler(CommandHandler("rejectclaim", admin_reject_claim))   
    application.add_handler(CommandHandler("approve", admin_approve_withdrawal)) 
    application.add_handler(CommandHandler("reject", admin_reject_withdrawal))   
    
    application.add_handler(CallbackQueryHandler(button_callback, pattern='^(watch_|check_join)'))
    application.add_handler(point_claim_conv_handler) 

    logger.info("বট চালু হচ্ছে (PostgreSQL এর সাথে)...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES) 
    except Exception as e:
        logger.critical(f"বট চালাতে গুরুতর ত্রুটি হয়েছে: {e}", exc_info=True)
    finally:
        logger.info("বট বন্ধ করা হচ্ছে।")

if __name__ == "__main__":
    main()
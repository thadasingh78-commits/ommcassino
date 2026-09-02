import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
import telebot
from dotenv import load_dotenv
import schedule
import time
import threading

load_dotenv()

# Environment variables & constants
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", os.getenv("BOT_TOKEN", "YOUR_ACTUAL_BOT_TOKEN_HERE"))
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/oomtyre")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:5000")
BACKEND_URL = os.getenv("BACKEND_URL", f"{BACKEND_BASE_URL}/api/admin/set_control")
ADMIN_SECRET = "SUPER_SECRET_KEY_123"

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "YOUR_ACTUAL_CHAT_ID_HERE")

# PostgreSQL URL fix for Render
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(ADMIN_BOT_TOKEN)

def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

# PostgreSQL Table Initialization
def init_game_table():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS game_control (
            id INTEGER PRIMARY KEY,
            next_outcome VARCHAR(50)
        )''')
        cursor.execute("SELECT * FROM game_control WHERE id = 1")
        res = cursor.fetchone()
        if not res:
            cursor.execute("INSERT INTO game_control (id, next_outcome) VALUES (1, NULL)")
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Admin Bot DB Initialized Successfully!")
    except Exception as e:
        print(f"⚠️ DB Init Warning: {e}")

init_game_table()

# --- COMMAND: /start & /help ---
@bot.message_handler(commands=['start', 'help'])
def start(message):
    bot.reply_to(
        message, 
        "⚡ **OOM TYRE Admin Control Panel**\n\n"
        "**User & Account Commands:**\n"
        "🔹 `/create username password balance` - Create New User\n"
        "🔹 `/add username amount` - Add User Balance\n"
        "🔹 `/min username amount` - Deduct User Balance\n\n"
        "**Game & Outcome Controls:**\n"
        "🔹 `/setwin 30` - Set Live Win Rate (0-100%)\n"
        "🔹 `/red`, `/green`, `/blue` - Fix Next Color Result\n"
        "🔹 `/num <1-36>` - Fix Next Roulette Number\n"
        "🔹 `/lock` - Toggle Forced Loss Emergency Lock\n"
        "🔹 `/house` - Live Daily Profit/Loss Report\n"
        "🔹 `/stopall` - Stop All Games\n"
        "🔹 `/stopwheel` - Stop Wheel/Color Game Only\n"
        "🔹 `/startall` - Resume All Games\n"
        "🔹 `/auto` - Reset Overrides & Back to Normal Algo",
        parse_mode="Markdown"
    )

# --- COMMAND: /create user pwd bal ---
@bot.message_handler(commands=['create'])
def create_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 4:
            raise ValueError("Insufficient arguments")
        
        user = parts[1]
        pwd = parts[2]
        bal = float(parts[3])

        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/create_user", json={
            "secret": ADMIN_SECRET,
            "username": user,
            "password": pwd,
            "balance": bal
        }).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"✅ Account Created: *{user}* | Balance: ₹{bal}", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/create username password balance`", parse_mode="Markdown")

# --- COMMAND: /add user amount ---
@bot.message_handler(commands=['add'])
def add_bal(message):
    try:
        _, user, amt = message.text.split()
        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/update_balance", json={
            "secret": ADMIN_SECRET,
            "username": user,
            "action": "add",
            "amount": float(amt)
        }).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"💰 {res.get('message')}")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/add username amount`", parse_mode="Markdown")

# --- COMMAND: /min user amount ---
@bot.message_handler(commands=['min'])
def minus_bal(message):
    try:
        _, user, amt = message.text.split()
        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/update_balance", json={
            "secret": ADMIN_SECRET,
            "username": user,
            "action": "deduct",
            "amount": float(amt)
        }).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"🔻 {res.get('message')}")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/min username amount`", parse_mode="Markdown")

# --- COMMAND: Dynamic Win Rate Setter (/setwin 30) ---
@bot.message_handler(commands=['setwin'])
def set_win_rate(message):
    try:
        _, rate = message.text.split()
        win_rate = float(rate)
        
        response = requests.post(BACKEND_URL, json={
            "winRate": win_rate,
            "secret": ADMIN_SECRET
        })
        
        res_data = response.json()
        if response.status_code == 200 and res_data.get("success"):
            bot.reply_to(message, f"🎯 **Live Win Rate Set To:** {win_rate}%", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Error: {res_data.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/setwin <0-100>` (Example: `/setwin 30`)", parse_mode="Markdown")

# --- COMMAND: Force Color Outcome Handler (/red, /green, /blue) ---
@bot.message_handler(commands=['red', 'green', 'blue'])
def set_result(message):
    try:
        color = message.text.replace('/', '').strip().lower()
        
        res = requests.post(BACKEND_URL, json={
            "force_outcome": color,
            "secret": ADMIN_SECRET
        }).json()

        if res.get("success"):
            bot.reply_to(message, f"🎯 **Next Result Fixed:** {color.upper()}")
        else:
            bot.reply_to(message, "❌ Failed to set outcome.")
    except Exception as e:
        bot.reply_to(message, f"❌ Outcome set error: {str(e)}")

# --- COMMAND: Emergency Lock Toggle (/lock) ---
@bot.message_handler(commands=['lock'])
def emergency_lock(message):
    try:
        requests.post(BACKEND_URL, json={
            "emergency_lock": True,
            "secret": ADMIN_SECRET
        })
        bot.reply_to(message, "🚨 **Emergency House Lock Activated!** (Forced Loss Mode Active)")
    except Exception as e:
        bot.reply_to(message, f"❌ Lock error: {str(e)}")

# --- COMMAND: System Controls (/stopall, /stopwheel, /startall, /auto) ---
@bot.message_handler(commands=['stopall'])
def stop_all(message):
    try:
        requests.post(BACKEND_URL, json={"stop_all_games": True, "secret": ADMIN_SECRET})
        bot.reply_to(message, "🛑 **All Games Stopped!**")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['stopwheel'])
def stop_wheel(message):
    try:
        requests.post(BACKEND_URL, json={"stop_wheel_game": True, "secret": ADMIN_SECRET})
        bot.reply_to(message, "🎡 **Wheel Game Stopped!**")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['startall'])
def start_all(message):
    try:
        requests.post(BACKEND_URL, json={
            "stop_all_games": False,
            "stop_wheel_game": False,
            "secret": ADMIN_SECRET
        })
        bot.reply_to(message, "▶️ **All Games Resumed!**")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['auto'])
def clear_overrides(message):
    try:
        requests.post(BACKEND_URL, json={
            "emergency_lock": False,
            "force_outcome": None,
            "stop_all_games": False,
            "stop_wheel_game": False,
            "secret": ADMIN_SECRET
        })
        bot.reply_to(message, "🔄 **System Restored to Normal Smart Algorithm!**")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# --- COMMAND: /house (Live P&L Report) ---
@bot.message_handler(commands=['house'])
def house_report(message):
    try:
        res = requests.get(f"{BACKEND_BASE_URL}/api/admin/house_stats?secret={ADMIN_SECRET}").json()
        if res.get("status") == "success":
            wagered = res.get("total_wagered", 0.0)
            payout = res.get("total_payout", 0.0)
            profit = res.get("house_profit", 0.0)

            status_emoji = "🟢 PROFIT" if profit >= 0 else "🔴 LOSS"

            msg = (
                f"📊 *DAILY HOUSE REPORT (Today)*\n\n"
                f"🎰 Total Bet (Wagered): *₹{wagered:.2f}*\n"
                f"🏆 Total Won by Users: *₹{payout:.2f}*\n"
                f"💰 House Net Result: *₹{profit:.2f}* ({status_emoji})"
            )
            bot.reply_to(message, msg, parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Stats fetch nahi ho sake.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# --- MIDNIGHT AUTOMATED 12 AM REPORT JOB ---
def midnight_job():
    try:
        res = requests.get(f"{BACKEND_BASE_URL}/api/admin/house_stats?secret={ADMIN_SECRET}").json()
        if res.get("status") == "success" and ADMIN_CHAT_ID != "YOUR_CHAT_ID_HERE":
            wagered = res.get("total_wagered", 0.0)
            payout = res.get("total_payout", 0.0)
            profit = res.get("house_profit", 0.0)
            status_emoji = "🟢 PROFIT" if profit >= 0 else "🔴 LOSS"

            msg = (
                f"🌙 *12 AM MIDNIGHT FINAL REPORT*\n"
                f"-----------------------------------\n"
                f"🎰 Total Bet Today: *₹{wagered:.2f}*\n"
                f"🏆 Total Won Today: *₹{payout:.2f}*\n"
                f"💰 Net House Profit/Loss: *₹{profit:.2f}* ({status_emoji})\n\n"
                f"🔄 *System automatically reset for the new day!*"
            )
            bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        print("Midnight job error:", e)

def run_scheduler():
    schedule.every().day.at("00:00").do(midnight_job)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == '__main__':
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    print("🤖 Telegram Admin Bot Active...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
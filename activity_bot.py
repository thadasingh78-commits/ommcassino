import time
import random
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Backend URL for live activity updates
BACKEND_URL = os.getenv("BACKEND_URL_ACTIVITY", "http://localhost:5000/api/live_activity")

FAKE_USERS = [
    "Rahul_9X", "Aman_K", "Vikas_Pro", "Rohan_Winner", 
    "Priya_77", "Lucky_S", "Alex_77", "Karan_01", 
    "Vikram_VIP", "Sneha_Win", "Kabir_Bet", "Anish_99"
]

COLORS = ["RED", "BLUE", "GREEN"]
BET_AMOUNTS = [100, 200, 500, 1000, 2000, 5000]
MULTIPLIERS = [1.5, 2.0, 3.0, 5.0]

def generate_fake_activity():
    user = random.choice(FAKE_USERS)
    color = random.choice(COLORS)
    amount = random.choice(BET_AMOUNTS)
    multiplier = random.choice(MULTIPLIERS)
    payout = int(amount * multiplier)

    payload = {
        "username": user,
        "color": color,
        "amount": amount,
        "payout": payout,
        "message": f"🔥 {user} placed ₹{amount} on {color} (Won ₹{payout})!"
    }

    try:
        response = requests.post(BACKEND_URL, json=payload, timeout=3)
        if response.status_code == 200:
            print(f"🤖 Activity Broadcasted: {user} won ₹{payout}")
        else:
            print(f"⚠️ Activity Server Response: {response.status_code}")
    except Exception as e:
        print(f"❌ Activity Bot Error: Server not reachable ({e})")

def start_bot():
    print("🤖 Activity Simulation Bot Started...")
    time.sleep(5)  # Wait for Flask server to initialize
    while True:
        time.sleep(random.randint(3, 8))
        generate_fake_activity()

if __name__ == '__main__':
    start_bot()
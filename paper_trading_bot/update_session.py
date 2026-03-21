"""
Quick script to update session token.
Run this each morning before market opens.

Usage:
    python3 update_session.py <new_session_token>

This updates the .env file and restarts the bot.
"""
import sys
import os
import subprocess

ENV_PATH = os.path.expanduser("~/nifty-algo-strategies/.env")
BOT_PID_FILE = os.path.expanduser("~/nifty-algo-strategies/paper_trading_bot/bot.pid")


def update_token(new_token):
    # Read current .env
    lines = []
    with open(ENV_PATH) as f:
        lines = f.readlines()

    # Update or add session token
    found = False
    for i, line in enumerate(lines):
        if line.startswith("SESSION_TOKEN="):
            lines[i] = f"SESSION_TOKEN={new_token}\n"
            found = True
            break
    if not found:
        lines.append(f"SESSION_TOKEN={new_token}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)

    print(f"Session token updated: {new_token[:4]}****")

    # Kill existing bot if running
    if os.path.exists(BOT_PID_FILE):
        with open(BOT_PID_FILE) as f:
            pid = f.read().strip()
        try:
            os.kill(int(pid), 9)
            print(f"Killed old bot process (PID: {pid})")
        except (ProcessLookupError, ValueError):
            pass

    # Start new bot
    bot_path = os.path.join(os.path.dirname(__file__), "bot.py")
    proc = subprocess.Popen(
        ["python3", bot_path, new_token],
        stdout=open(os.path.expanduser("~/nifty-algo-strategies/paper_trading_bot/logs/bot_stdout.log"), "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    with open(BOT_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    print(f"Bot started with PID: {proc.pid}")
    print("Check logs: tail -f ~/nifty-algo-strategies/paper_trading_bot/logs/bot_stdout.log")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 update_session.py <session_token>")
        sys.exit(1)
    update_token(sys.argv[1])

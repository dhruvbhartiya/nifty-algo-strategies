"""
Simple API server to update session token and restart the bot.
Runs on EC2 — call it from anywhere to refresh the daily token.

Usage:
    python3 api_server.py

API Endpoints:
    POST /update-token
        Body: {"token": "55070364"}
        → Updates session, restarts bot, sends confirmation email

    GET /status
        → Returns bot status, last trade, P&L summary

    GET /health
        → Simple health check

Example curl:
    curl -X POST http://ec2-43-205-237-132.ap-south-1.compute.amazonaws.com:8080/update-token \
         -H "Content-Type: application/json" \
         -d '{"token": "55070364"}'
"""
import sys
import os
import json
import signal
import subprocess
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOT_DIR = os.path.expanduser("~/nifty-algo-strategies/paper_trading_bot")
ENV_PATH = os.path.expanduser("~/nifty-algo-strategies/.env")
PID_FILE = os.path.join(BOT_DIR, "bot.pid")
TRADE_LOG = os.path.join(BOT_DIR, "trades.json")
LOG_DIR = os.path.join(BOT_DIR, "logs")
API_PORT = 8080

# Simple auth key — change this to something secure
API_KEY = "nifty2026algo"


def get_bot_pid():
    """Get running bot PID, or None."""
    if not os.path.exists(PID_FILE):
        return None
    with open(PID_FILE) as f:
        pid = f.read().strip()
    try:
        pid = int(pid)
        os.kill(pid, 0)  # check if process exists
        return pid
    except (ValueError, ProcessLookupError):
        return None


def kill_bot():
    """Kill the running bot process."""
    pid = get_bot_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            import time
            time.sleep(2)
            # Force kill if still alive
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return pid
    return None


def start_bot(session_token):
    """Start the bot with the given session token."""
    os.makedirs(LOG_DIR, exist_ok=True)
    bot_path = os.path.join(BOT_DIR, "bot.py")
    log_file = os.path.join(LOG_DIR, "bot_stdout.log")

    proc = subprocess.Popen(
        ["python3", bot_path, session_token],
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=BOT_DIR,
    )

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    return proc.pid


def load_trade_summary():
    """Load trade summary from JSON."""
    if not os.path.exists(TRADE_LOG):
        return {"total_trades": 0, "cum_pnl": 0}
    try:
        with open(TRADE_LOG) as f:
            data = json.load(f)
        return data.get("summary", {"total_trades": 0, "cum_pnl": 0})
    except Exception:
        return {"total_trades": 0, "cum_pnl": 0}


def get_last_log_lines(n=20):
    """Get last N lines from bot log."""
    log_file = os.path.join(LOG_DIR, "bot_{}.log".format(datetime.now().strftime("%Y%m%d")))
    if not os.path.exists(log_file):
        log_file = os.path.join(LOG_DIR, "bot_stdout.log")
    if not os.path.exists(log_file):
        return ["No log file found"]
    try:
        with open(log_file) as f:
            lines = f.readlines()
        return [l.strip() for l in lines[-n:]]
    except Exception:
        return ["Error reading log"]


def send_token_update_email(token, pid):
    """Send email confirming token update."""
    try:
        from bot_notifier import send_email
        html = """
        <html><body style="font-family: Arial, sans-serif; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; border: 2px solid #70AD47; border-radius: 10px; overflow: hidden;">
            <div style="background: #70AD47; color: white; padding: 15px 20px;">
                <h2 style="margin: 0;">SESSION TOKEN UPDATED</h2>
            </div>
            <div style="padding: 20px;">
                <p><strong>Time:</strong> {time}</p>
                <p><strong>Token:</strong> {token}****</p>
                <p><strong>Bot PID:</strong> {pid}</p>
                <p><strong>Status:</strong> Bot restarted and monitoring market</p>
            </div>
        </div>
        </body></html>
        """.format(
            time=datetime.now().strftime("%d %b %Y %H:%M:%S IST"),
            token=token[:4],
            pid=pid,
        )
        send_email("TOKEN UPDATED | Bot restarted | PID {}".format(pid), html)
    except Exception as e:
        print("Email error: {}".format(e))


class BotAPIHandler(BaseHTTPRequestHandler):

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "bot_running": get_bot_pid() is not None,
            })

        elif self.path == "/status":
            # Check API key
            if self.headers.get("X-API-Key") != API_KEY:
                self._send_json(401, {"error": "Invalid API key"})
                return

            pid = get_bot_pid()
            summary = load_trade_summary()
            logs = get_last_log_lines(15)

            self._send_json(200, {
                "bot_running": pid is not None,
                "bot_pid": pid,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_trades": summary.get("total_trades", 0),
                "cum_pnl": summary.get("cum_pnl", 0),
                "recent_logs": logs,
            })

        elif self.path == "/trades":
            if self.headers.get("X-API-Key") != API_KEY:
                self._send_json(401, {"error": "Invalid API key"})
                return

            if os.path.exists(TRADE_LOG):
                with open(TRADE_LOG) as f:
                    trades = json.load(f)
                self._send_json(200, trades)
            else:
                self._send_json(200, {"straddle": [], "v4_put": [], "summary": {"cum_pnl": 0, "total_trades": 0}})

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/update-token":
            body = self._read_body()

            # Auth check
            api_key = self.headers.get("X-API-Key", body.get("api_key", ""))
            if api_key != API_KEY:
                self._send_json(401, {"error": "Invalid API key. Pass X-API-Key header or api_key in body."})
                return

            token = body.get("token", "").strip()
            if not token:
                self._send_json(400, {"error": "Missing 'token' in request body"})
                return

            # Kill existing bot
            old_pid = kill_bot()

            # Start new bot
            new_pid = start_bot(token)

            # Send confirmation email
            send_token_update_email(token, new_pid)

            self._send_json(200, {
                "status": "success",
                "message": "Bot restarted with new session token",
                "old_pid": old_pid,
                "new_pid": new_pid,
                "token_preview": "{}****".format(token[:4]),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        elif self.path == "/stop":
            api_key = self.headers.get("X-API-Key", "")
            if api_key != API_KEY:
                self._send_json(401, {"error": "Invalid API key"})
                return

            pid = kill_bot()
            self._send_json(200, {
                "status": "stopped" if pid else "not_running",
                "killed_pid": pid,
            })

        else:
            self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        """Override to log to file instead of stderr."""
        print("[API {}] {}".format(datetime.now().strftime("%H:%M:%S"), format % args))


if __name__ == "__main__":
    print("=" * 60)
    print("  NIFTY Algo Bot — API Server")
    print("=" * 60)
    print("  Port: {}".format(API_PORT))
    print("  API Key: {}".format(API_KEY))
    print("")
    print("  Endpoints:")
    print("    POST /update-token  — Update session & restart bot")
    print("    GET  /status        — Bot status & P&L")
    print("    GET  /trades        — All trade history")
    print("    GET  /health        — Health check")
    print("    POST /stop          — Stop the bot")
    print("")
    print("  Example:")
    print('    curl -X POST http://localhost:{}/update-token \\'.format(API_PORT))
    print('         -H "Content-Type: application/json" \\')
    print('         -H "X-API-Key: {}" \\'.format(API_KEY))
    print('         -d \'{{"token": "YOUR_SESSION_TOKEN"}}\'')
    print("=" * 60)

    server = HTTPServer(("0.0.0.0", API_PORT), BotAPIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAPI server stopped")
        server.server_close()

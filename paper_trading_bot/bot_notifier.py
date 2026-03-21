"""
Email notification module for trade alerts.
Sends formatted HTML emails on trade entry, exit, and daily summary.
"""
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import logging

logger = logging.getLogger("notifier")


def load_email_creds():
    env = {}
    env_path = os.path.expanduser("~/nifty-algo-strategies/.env")
    with open(env_path) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env.get("GMAIL_USER"), env.get("GMAIL_APP_PASSWORD"), env.get("NOTIFY_EMAIL")


def send_email(subject, html_body):
    """Send an HTML email."""
    sender, password, receiver = load_email_creds()
    if not all([sender, password, receiver]):
        logger.error("Email credentials not configured in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"NIFTY Algo Bot <{sender}>"
    msg["To"] = receiver
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False


def notify_trade_entry(strategy, trade_info):
    """Send email when a new trade is entered."""
    s = trade_info
    color = "#2F5496" if strategy == "STRADDLE" else "#70AD47"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="max-width: 600px; margin: 0 auto; border: 2px solid {color}; border-radius: 10px; overflow: hidden;">
        <div style="background: {color}; color: white; padding: 15px 20px;">
            <h2 style="margin: 0;">TRADE ENTRY — {strategy}</h2>
            <p style="margin: 5px 0 0 0; opacity: 0.8;">Paper Trading | {datetime.now().strftime('%d %b %Y %H:%M:%S')}</p>
        </div>
        <div style="padding: 20px;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Entry Time</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('entry_time', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Spot Price</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('entry_spot', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Strike</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('strike', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Direction</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('direction', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Premium (C+P)</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('combined_premium', 0):.1f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Lots</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('lots', 2)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Qty</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('lots', 2) * 25}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">IV Used</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('iv', 0):.1%}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">Signal</td>
                    <td style="padding: 8px;">{s.get('signal', '-')}</td></tr>
            </table>
        </div>
    </div>
    </body></html>
    """

    subject = f"ENTRY {strategy} | Strike {s.get('strike', '?')} | Spot {s.get('entry_spot', '?')}"
    send_email(subject, html)


def notify_trade_exit(strategy, trade_info):
    """Send email when a trade is closed."""
    s = trade_info
    net_pnl = s.get('net_pnl', 0)
    pnl_color = "#70AD47" if net_pnl >= 0 else "#FF4444"
    border_color = "#2F5496" if strategy == "STRADDLE" else "#70AD47"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="max-width: 600px; margin: 0 auto; border: 2px solid {border_color}; border-radius: 10px; overflow: hidden;">
        <div style="background: {border_color}; color: white; padding: 15px 20px;">
            <h2 style="margin: 0;">TRADE EXIT — {strategy}</h2>
            <p style="margin: 5px 0 0 0; opacity: 0.8;">Paper Trading | {datetime.now().strftime('%d %b %Y %H:%M:%S')}</p>
        </div>
        <div style="padding: 20px;">
            <div style="text-align: center; margin: 10px 0 20px 0;">
                <span style="font-size: 28px; font-weight: bold; color: {pnl_color};">
                    Rs {net_pnl:+,.0f}
                </span>
                <br><span style="color: #888; font-size: 12px;">Net P&L (after costs)</span>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Entry</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('entry_time', '-')} @ {s.get('entry_spot', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Exit</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('exit_time', '-')} @ {s.get('exit_spot', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Strike</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('strike', '-')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Spot Move</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('spot_move', 0):.0f} pts</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">C+P Entry</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('combined_entry', 0):.1f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">C+P Exit</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('combined_exit', 0):.1f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Gross P&L</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('gross_pnl', 0):+,.0f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Txn Costs</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('txn_costs', 0):,.0f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Hold Time</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('hold_min', 0)} min</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">Exit Reason</td>
                    <td style="padding: 8px; font-weight: bold; color: {pnl_color};">{s.get('exit_reason', '-')}</td></tr>
            </table>
            <hr style="margin: 15px 0; border: none; border-top: 1px solid #eee;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 5px; font-weight: bold;">Cumulative Net P&L</td>
                    <td style="padding: 5px; font-weight: bold; color: {'#70AD47' if s.get('cum_pnl', 0) >= 0 else '#FF4444'};">
                        Rs {s.get('cum_pnl', 0):+,.0f}</td></tr>
                <tr><td style="padding: 5px; font-weight: bold;">ROI</td>
                    <td style="padding: 5px;">{s.get('roi', 0):+.1f}%</td></tr>
            </table>
        </div>
    </div>
    </body></html>
    """

    subject = f"EXIT {strategy} | {s.get('exit_reason', '?')} | Rs {net_pnl:+,.0f} | Cum: Rs {s.get('cum_pnl', 0):+,.0f}"
    send_email(subject, html)


def notify_daily_summary(summary):
    """Send end-of-day summary email."""
    s = summary
    pnl_color = "#70AD47" if s.get('net_pnl', 0) >= 0 else "#FF4444"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="max-width: 600px; margin: 0 auto; border: 2px solid #2F5496; border-radius: 10px; overflow: hidden;">
        <div style="background: #2F5496; color: white; padding: 15px 20px;">
            <h2 style="margin: 0;">DAILY SUMMARY</h2>
            <p style="margin: 5px 0 0 0; opacity: 0.8;">{s.get('date', datetime.now().strftime('%d %b %Y'))}</p>
        </div>
        <div style="padding: 20px;">
            <div style="text-align: center; margin: 10px 0 20px 0;">
                <span style="font-size: 24px; font-weight: bold; color: {pnl_color};">
                    Today: Rs {s.get('net_pnl', 0):+,.0f}
                </span>
                <br>
                <span style="font-size: 18px; color: #2F5496;">
                    Overall: Rs {s.get('cum_pnl', 0):+,.0f} ({s.get('roi', 0):+.1f}%)
                </span>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Straddle Trades</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('straddle_trades', 0)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">V4 PUT Trades</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('v4_trades', 0)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Winners</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('winners', 0)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Losers</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('losers', 0)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Gross P&L</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('gross_pnl', 0):+,.0f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">Txn Costs</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">Rs {s.get('total_costs', 0):,.0f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">NIFTY Open</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{s.get('nifty_open', '-')}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">NIFTY Close</td>
                    <td style="padding: 8px;">{s.get('nifty_close', '-')}</td></tr>
            </table>
            <p style="color: #888; font-size: 11px; margin-top: 15px;">
                Capital: Rs 2,00,000 | 2 lots/trade | Paper Trading
            </p>
        </div>
    </div>
    </body></html>
    """

    subject = f"DAILY SUMMARY | Rs {s.get('net_pnl', 0):+,.0f} today | {s.get('straddle_trades', 0) + s.get('v4_trades', 0)} trades | Cum: Rs {s.get('cum_pnl', 0):+,.0f}"
    send_email(subject, html)


def notify_bot_start():
    """Send email when bot starts for the day."""
    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="max-width: 600px; margin: 0 auto; border: 2px solid #70AD47; border-radius: 10px; overflow: hidden;">
        <div style="background: #70AD47; color: white; padding: 15px 20px;">
            <h2 style="margin: 0;">BOT STARTED</h2>
        </div>
        <div style="padding: 20px;">
            <p>NIFTY Algo Paper Trading Bot is now active.</p>
            <p><strong>Time:</strong> {datetime.now().strftime('%d %b %Y %H:%M:%S IST')}</p>
            <p><strong>Strategies:</strong> Straddle ATM + V4 PUT</p>
            <p><strong>Capital:</strong> Rs 2,00,000 | 2 lots/trade</p>
            <p>Monitoring market for signals...</p>
        </div>
    </div>
    </body></html>
    """
    send_email("BOT STARTED | Monitoring NIFTY", html)


def notify_error(error_msg):
    """Send email on critical error."""
    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="max-width: 600px; margin: 0 auto; border: 2px solid #FF4444; border-radius: 10px; overflow: hidden;">
        <div style="background: #FF4444; color: white; padding: 15px 20px;">
            <h2 style="margin: 0;">BOT ERROR</h2>
        </div>
        <div style="padding: 20px;">
            <p><strong>Time:</strong> {datetime.now().strftime('%d %b %Y %H:%M:%S IST')}</p>
            <p><strong>Error:</strong></p>
            <pre style="background: #f5f5f5; padding: 10px; border-radius: 5px; overflow-x: auto;">{error_msg}</pre>
            <p>The bot may need attention.</p>
        </div>
    </div>
    </body></html>
    """
    send_email(f"BOT ERROR | {error_msg[:50]}", html)

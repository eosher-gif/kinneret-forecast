#!/usr/bin/env python3
"""Kinneret Surf Forecast Bot — eFoil & SUP forecast for the Sea of Galilee."""

import json
import os
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ── Spots ──────────────────────────────────────────────────────────────────────
SPOTS = {
    "ginosar": {"name": "גינוסר", "name_en": "Ginosar", "lat": 32.87, "lon": 35.52, "shore": "west"},
    "ein_gev": {"name": "עין גב", "name_en": "Ein Gev", "lat": 32.78, "lon": 35.64, "shore": "east"},
}

MODELS = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless"]

# ── Hebrew helpers ─────────────────────────────────────────────────────────────
WEEKDAY_HEB = {0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"}

WIND_DIR_HEB = [
    (0, "צפון"), (45, "צפון-מזרח"), (90, "מזרח"), (135, "דרום-מזרח"),
    (180, "דרום"), (225, "דרום-מערב"), (270, "מערב"), (315, "צפון-מערב"), (360, "צפון"),
]


def wind_dir_to_hebrew(deg):
    if deg is None:
        return "—"
    best = min(WIND_DIR_HEB, key=lambda x: abs(x[0] - (deg % 360)))
    return best[1]


def score_color(score):
    if score >= 9:
        return "#d4edda"
    if score >= 7:
        return "#cce5ff"
    if score >= 5:
        return "#fff3cd"
    return "#f8d7da"


def score_emoji_efoil(score):
    if score >= 9:
        return "🪞"
    if score >= 7:
        return "✅"
    if score >= 5:
        return "⚠️"
    return "🚫"


def score_emoji_sup(score):
    if score >= 9:
        return "🏄"
    if score >= 7:
        return "✅"
    if score >= 5:
        return "⚠️"
    return "🚫"


def score_text_efoil(score):
    if score >= 9:
        return "מושלם! מים שטוחים כמראה"
    if score == 8:
        return "מצוין, כמעט שטוח"
    if score == 7:
        return "טוב, ציפול קל מתחיל"
    if score == 6:
        return "סביר, ציפול מורגש"
    if score == 5:
        return "גרוע, גלי"
    return "לא מומלץ"


def score_text_sup(score):
    if score >= 9:
        return "מושלם! שטוח ובטוח"
    if score == 8:
        return "מצוין, שקט"
    if score == 7:
        return "גבולי, תישארו קרוב לחוף"
    if score == 6:
        return "מסוכן — סכנת סחיפה"
    return "מסוכן! אל תצאו"


# ── API fetch ──────────────────────────────────────────────────────────────────
def fetch_forecast(lat, lon, model, retries=3, delay=2):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m"
        f"&wind_speed_unit=kn&timezone=Asia%2FJerusalem&forecast_days=3"
        f"&models={model}"
    )
    for attempt in range(retries):
        try:
            print(f"  Fetching {model} for ({lat},{lon})... attempt {attempt + 1}")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"  Error: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
    print(f"  FAILED after {retries} attempts for {model} ({lat},{lon})")
    return None


def extract_hourly(data):
    """Extract hourly arrays, handling model-suffixed keys."""
    h = data.get("hourly", {})
    times = h.get("time", [])
    wind_key = [k for k in h if "wind_speed" in k][0] if any("wind_speed" in k for k in h) else None
    gust_key = [k for k in h if "wind_gusts" in k][0] if any("wind_gusts" in k for k in h) else None
    dir_key = [k for k in h if "wind_direction" in k][0] if any("wind_direction" in k for k in h) else None
    temp_key = [k for k in h if "temperature" in k][0] if any("temperature" in k for k in h) else None

    return {
        "times": times,
        "wind": h.get(wind_key, []) if wind_key else [],
        "gusts": h.get(gust_key, []) if gust_key else [],
        "direction": h.get(dir_key, []) if dir_key else [],
        "temperature": h.get(temp_key, []) if temp_key else [],
    }


# ── Data aggregation ──────────────────────────────────────────────────────────
def fetch_all_data():
    """Fetch all spots × models, return structured data."""
    all_data = {}
    for spot_id, spot in SPOTS.items():
        all_data[spot_id] = {}
        print(f"Fetching forecasts for {spot['name_en']}...")
        for model in MODELS:
            raw = fetch_forecast(spot["lat"], spot["lon"], model)
            if raw:
                all_data[spot_id][model] = extract_hourly(raw)
            else:
                all_data[spot_id][model] = None
    return all_data


def get_days_from_data(all_data):
    """Get list of forecast dates from data."""
    for spot_id in all_data:
        for model in MODELS:
            d = all_data[spot_id].get(model)
            if d and d["times"]:
                dates = sorted(set(t[:10] for t in d["times"]))
                return dates
    return []


def get_hour_index(times, date_str, hour):
    """Find index for a specific date+hour."""
    target = f"{date_str}T{hour:02d}:00"
    for i, t in enumerate(times):
        if t == target:
            return i
    return None


def aggregate_hour(all_data, spot_id, date_str, hour):
    """Aggregate wind/gusts/dir/temp across models for a specific hour."""
    winds, gusts, dirs, temps = [], [], [], []
    for model in MODELS:
        d = all_data[spot_id].get(model)
        if not d:
            continue
        idx = get_hour_index(d["times"], date_str, hour)
        if idx is None or idx >= len(d["wind"]):
            continue
        w = d["wind"][idx]
        g = d["gusts"][idx] if idx < len(d["gusts"]) else None
        dr = d["direction"][idx] if idx < len(d["direction"]) else None
        tp = d["temperature"][idx] if idx < len(d["temperature"]) else None
        if w is not None:
            winds.append(w)
        if g is not None:
            gusts.append(g)
        if dr is not None:
            dirs.append(dr)
        if tp is not None:
            temps.append(tp)

    if not winds:
        return None

    return {
        "wind_avg": sum(winds) / len(winds),
        "wind_min": min(winds),
        "wind_max": max(winds),
        "gust_avg": sum(gusts) / len(gusts) if gusts else 0,
        "gust_max": max(gusts) if gusts else 0,
        "dir_avg": sum(dirs) / len(dirs) if dirs else 0,
        "temp_avg": sum(temps) / len(temps) if temps else 0,
        "model_spread": max(winds) - min(winds) if len(winds) > 1 else 0,
        "n_models": len(winds),
    }


def morning_stats(all_data, spot_id, date_str):
    """Get average wind and max gusts for 08:00-11:00 window."""
    winds, gusts_list = [], []
    for hour in range(8, 12):
        agg = aggregate_hour(all_data, spot_id, date_str, hour)
        if agg:
            winds.append(agg["wind_avg"])
            gusts_list.append(agg["gust_max"])
    if not winds:
        return None, None
    return sum(winds) / len(winds), max(gusts_list)


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_efoil(avg_wind, max_gust):
    if avg_wind is None:
        return 0
    if avg_wind < 2 and max_gust < 4:
        return 10
    if avg_wind < 3 and max_gust < 5:
        return 9
    if avg_wind < 4 and max_gust < 7:
        return 8
    if avg_wind < 5 and max_gust < 8:
        return 7
    if avg_wind < 7 and max_gust < 10:
        return 6
    if avg_wind < 9 and max_gust < 13:
        return 5
    return 4


def score_sup(avg_wind, max_gust):
    if avg_wind is None:
        return 0
    if avg_wind < 2 and max_gust < 4:
        return 10
    if avg_wind < 3 and max_gust < 5:
        return 9
    if avg_wind < 4 and max_gust < 6:
        return 8
    if avg_wind < 5 and max_gust < 7:
        return 7
    if avg_wind < 6 and max_gust < 8:
        return 6
    return 5


# ── Safety & analysis ─────────────────────────────────────────────────────────
def find_window_closure(all_data, spot_id, date_str):
    """Find first hour when ANY model shows sustained wind > 12kn."""
    for hour in range(6, 21):
        for model in MODELS:
            d = all_data[spot_id].get(model)
            if not d:
                continue
            idx = get_hour_index(d["times"], date_str, hour)
            if idx is None or idx >= len(d["wind"]):
                continue
            w = d["wind"][idx]
            if w is not None and w > 12:
                return f"{hour:02d}:00"
    return "לא צפוי היום"


def detect_alerts(all_data, date_str):
    """Detect safety alerts for a given day."""
    alerts = []

    # Offshore wind at Ein Gev (direction 60-120 in morning)
    for hour in range(7, 12):
        agg = aggregate_hour(all_data, "ein_gev", date_str, hour)
        if agg and 60 <= agg["dir_avg"] <= 120 and agg["wind_avg"] > 3:
            alerts.append("⚠️ רוח מהחוף בעין גב (מזרחית) — סכנת סחיפה לעומק!")
            break

    # Rapid transition: wind jumps >8kn between consecutive hours
    for spot_id in SPOTS:
        prev_wind = None
        for hour in range(6, 18):
            agg = aggregate_hour(all_data, spot_id, date_str, hour)
            if agg:
                if prev_wind is not None and abs(agg["wind_avg"] - prev_wind) > 8:
                    alerts.append(f"⚡ מעבר חד ברוח ב{SPOTS[spot_id]['name']} סביב {hour:02d}:00")
                    break
                prev_wind = agg["wind_avg"]

    # Extreme heat
    for hour in range(8, 14):
        for spot_id in SPOTS:
            agg = aggregate_hour(all_data, spot_id, date_str, hour)
            if agg and agg["temp_avg"] > 35:
                alerts.append(f"🌡️ חום קיצוני ({agg['temp_avg']:.0f}°C) — שתו הרבה מים!")
                break
        else:
            continue
        break

    # Model disagreement
    for hour in range(8, 12):
        for spot_id in SPOTS:
            agg = aggregate_hour(all_data, spot_id, date_str, hour)
            if agg and agg["model_spread"] > 5:
                alerts.append(f"🔀 חוסר הסכמה בין המודלים ({agg['model_spread']:.0f}kn) — אמינות נמוכה")
                break
        else:
            continue
        break

    return alerts


def recommend_spot(all_data, date_str):
    """Recommend better spot for the day."""
    gin_wind, gin_gust = morning_stats(all_data, "ginosar", date_str)
    ein_wind, ein_gust = morning_stats(all_data, "ein_gev", date_str)

    if gin_wind is None and ein_wind is None:
        return "אין מספיק נתונים"
    if gin_wind is None:
        return "עין גב"
    if ein_wind is None:
        return "גינוסר"

    # Lower wind = better (calmer)
    gin_score = gin_wind + (gin_gust * 0.3)
    ein_score = ein_wind + (ein_gust * 0.3)

    if gin_score < ein_score - 1:
        return "גינוסר (מוגן יותר בבוקר)"
    if ein_score < gin_score - 1:
        return "עין גב (שקט יותר היום)"
    return "שניהם דומים — בחרו לפי נוחות"


# ── HTML email builder ────────────────────────────────────────────────────────
def build_html(all_data, days):
    """Build the full HTML email."""
    now = datetime.now()

    # Overall scores (day 1, best spot)
    best_efoil = 0
    best_sup = 0
    for spot_id in SPOTS:
        w, g = morning_stats(all_data, spot_id, days[0])
        if w is not None:
            best_efoil = max(best_efoil, score_efoil(w, g))
            best_sup = max(best_sup, score_sup(w, g))

    date_display = days[0] if days else now.strftime("%Y-%m-%d")
    dt0 = datetime.strptime(days[0], "%Y-%m-%d") if days else now
    day_name_heb = WEEKDAY_HEB.get(dt0.weekday(), "")

    subject = f"🌊 תחזית כנרת — {day_name_heb} {date_display} | eFoil: {best_efoil}/10 | SUP: {best_sup}/10"

    html_parts = []

    # Header
    html_parts.append(f"""
<div style="background:linear-gradient(135deg,#0077b6,#00b4d8,#90e0ef);padding:25px;text-align:center;border-radius:12px 12px 0 0;">
  <h1 style="color:white;margin:0;font-size:28px;font-family:Arial,sans-serif;">🌊 תחזית גלישה — כנרת</h1>
  <p style="color:white;margin:8px 0 0;font-size:16px;font-family:Arial,sans-serif;">יום {day_name_heb}, {date_display}</p>
</div>
""")

    # Score cards - side by side
    efoil_bg = score_color(best_efoil)
    sup_bg = score_color(best_sup)
    html_parts.append(f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin:15px 0;">
<tr>
<td width="50%" style="padding:8px;">
  <div style="background:{efoil_bg};border-radius:10px;padding:20px;text-align:center;">
    <div style="font-size:32px;">{score_emoji_efoil(best_efoil)}</div>
    <div style="font-size:14px;color:#333;font-family:Arial,sans-serif;font-weight:bold;">eFoil</div>
    <div style="font-size:48px;font-weight:bold;color:#333;font-family:Arial,sans-serif;">{best_efoil}/10</div>
    <div style="font-size:13px;color:#555;font-family:Arial,sans-serif;">{score_text_efoil(best_efoil)}</div>
  </div>
</td>
<td width="50%" style="padding:8px;">
  <div style="background:{sup_bg};border-radius:10px;padding:20px;text-align:center;">
    <div style="font-size:32px;">{score_emoji_sup(best_sup)}</div>
    <div style="font-size:14px;color:#333;font-family:Arial,sans-serif;font-weight:bold;">SUP</div>
    <div style="font-size:48px;font-weight:bold;color:#333;font-family:Arial,sans-serif;">{best_sup}/10</div>
    <div style="font-size:13px;color:#555;font-family:Arial,sans-serif;">{score_text_sup(best_sup)}</div>
  </div>
</td>
</tr>
</table>
""")

    # Per-day sections
    for day_idx, date_str in enumerate(days):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_heb = WEEKDAY_HEB.get(dt.weekday(), "")

        # Day scores (best spot)
        day_efoil = 0
        day_sup = 0
        for spot_id in SPOTS:
            w, g = morning_stats(all_data, spot_id, date_str)
            if w is not None:
                day_efoil = max(day_efoil, score_efoil(w, g))
                day_sup = max(day_sup, score_sup(w, g))

        window = find_window_closure(all_data, "ginosar", date_str)
        spot_rec = recommend_spot(all_data, date_str)
        alerts = detect_alerts(all_data, date_str)

        html_parts.append(f"""
<div style="background:#f8f9fa;border-radius:10px;margin:15px 0;padding:15px;border:1px solid #dee2e6;">
  <h2 style="margin:0 0 10px;font-family:Arial,sans-serif;color:#0077b6;font-size:20px;text-align:right;">
    יום {day_heb} — {date_str}
  </h2>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
  <tr>
  <td width="50%" style="padding:4px;">
    <div style="background:{score_color(day_efoil)};border-radius:8px;padding:10px;text-align:center;">
      <span style="font-size:14px;font-family:Arial,sans-serif;font-weight:bold;">eFoil: {day_efoil}/10</span>
      <span style="font-size:18px;margin-right:5px;">{score_emoji_efoil(day_efoil)}</span>
    </div>
  </td>
  <td width="50%" style="padding:4px;">
    <div style="background:{score_color(day_sup)};border-radius:8px;padding:10px;text-align:center;">
      <span style="font-size:14px;font-family:Arial,sans-serif;font-weight:bold;">SUP: {day_sup}/10</span>
      <span style="font-size:18px;margin-right:5px;">{score_emoji_sup(day_sup)}</span>
    </div>
  </td>
  </tr>
  </table>
""")

        # Hourly table — use best spot (ginosar first)
        html_parts.append("""
  <table width="100%" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;direction:rtl;text-align:center;">
  <thead>
  <tr style="background:#0077b6;color:white;">
    <th style="padding:8px;border:1px solid #dee2e6;">שעה</th>
    <th style="padding:8px;border:1px solid #dee2e6;">רוח (kn)</th>
    <th style="padding:8px;border:1px solid #dee2e6;">משבים</th>
    <th style="padding:8px;border:1px solid #dee2e6;">כיוון</th>
    <th style="padding:8px;border:1px solid #dee2e6;">טמפ'</th>
  </tr>
  </thead>
  <tbody>
""")

        for i, hour in enumerate(range(8, 15)):
            agg_gin = aggregate_hour(all_data, "ginosar", date_str, hour)
            agg = agg_gin  # Primary spot
            if not agg:
                agg = aggregate_hour(all_data, "ein_gev", date_str, hour)

            row_bg = "#ffffff" if i % 2 == 0 else "#f2f2f2"

            if agg:
                wind_str = f"{agg['wind_avg']:.0f}"
                if agg["model_spread"] > 3:
                    wind_str += f" ({agg['wind_min']:.0f}-{agg['wind_max']:.0f})"

                gust_str = f"{agg['gust_max']:.0f}"
                dir_str = wind_dir_to_hebrew(agg["dir_avg"])
                temp_str = f"{agg['temp_avg']:.0f}°C"

                # Color wind cell based on intensity
                wind_val = agg["wind_avg"]
                if wind_val < 5:
                    wind_cell_bg = "#d4edda"
                elif wind_val < 10:
                    wind_cell_bg = "#fff3cd"
                else:
                    wind_cell_bg = "#f8d7da"
            else:
                wind_str = "—"
                gust_str = "—"
                dir_str = "—"
                temp_str = "—"
                wind_cell_bg = row_bg

            html_parts.append(f"""
    <tr style="background:{row_bg};">
      <td style="padding:6px;border:1px solid #dee2e6;font-weight:bold;">{hour:02d}:00</td>
      <td style="padding:6px;border:1px solid #dee2e6;background:{wind_cell_bg};font-weight:bold;">{wind_str}</td>
      <td style="padding:6px;border:1px solid #dee2e6;">{gust_str}</td>
      <td style="padding:6px;border:1px solid #dee2e6;">{dir_str}</td>
      <td style="padding:6px;border:1px solid #dee2e6;">{temp_str}</td>
    </tr>
""")

        html_parts.append("  </tbody></table>")

        # Window closure + spot recommendation
        html_parts.append(f"""
  <div style="margin-top:10px;font-family:Arial,sans-serif;font-size:14px;text-align:right;">
    <p style="margin:4px 0;">⏰ <b>חלון נסגר:</b> {window}</p>
    <p style="margin:4px 0;">📍 <b>ספוט מומלץ:</b> {spot_rec}</p>
  </div>
""")

        # Safety alerts
        if alerts:
            alerts_html = "<br>".join(alerts)
            html_parts.append(f"""
  <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:12px;margin-top:10px;font-family:Arial,sans-serif;font-size:13px;text-align:right;">
    <b>⚠️ התראות בטיחות:</b><br>{alerts_html}
  </div>
""")

        html_parts.append("</div>")  # close per-day div

    # Model confidence bar
    confidences = [("היום", 90, days[0] if len(days) > 0 else ""),
                   ("מחר", 75, days[1] if len(days) > 1 else ""),
                   ("מחרתיים", 60, days[2] if len(days) > 2 else "")]

    html_parts.append("""
<div style="background:#f8f9fa;border-radius:10px;margin:15px 0;padding:15px;border:1px solid #dee2e6;">
  <h3 style="margin:0 0 10px;font-family:Arial,sans-serif;color:#0077b6;text-align:right;">📊 אמינות המודלים</h3>
""")
    for label, pct, date_str in confidences:
        bar_color = "#28a745" if pct >= 80 else ("#ffc107" if pct >= 65 else "#dc3545")
        html_parts.append(f"""
  <div style="margin:6px 0;font-family:Arial,sans-serif;font-size:13px;text-align:right;">
    <span style="display:inline-block;width:60px;">{label}</span>
    <div style="display:inline-block;width:calc(100% - 120px);background:#e9ecef;border-radius:4px;height:18px;vertical-align:middle;">
      <div style="width:{pct}%;background:{bar_color};border-radius:4px;height:18px;text-align:center;color:white;font-size:11px;line-height:18px;">{pct}%</div>
    </div>
  </div>
""")
    html_parts.append("</div>")

    # Footer
    html_parts.append("""
<div style="background:#333;color:#aaa;padding:15px;text-align:center;border-radius:0 0 12px 12px;font-family:Arial,sans-serif;font-size:12px;">
  <p style="margin:4px 0;">נשלח אוטומטית ע"י Kinneret Forecast Bot 🤖</p>
  <p style="margin:4px 0;font-size:11px;color:#777;">התחזית מבוססת על מודלים מטאורולוגיים (ECMWF, ICON, GFS) ואינה מהווה תחליף לשיקול דעת אישי. בדקו תנאים בשטח לפני כניסה למים.</p>
</div>
""")

    # Wrap everything in a container
    body_html = f"""
<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#e9ecef;font-family:Arial,sans-serif;">
<div style="max-width:600px;margin:20px auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.1);">
{"".join(html_parts)}
</div>
</body>
</html>
"""

    return subject, body_html


# ── Email sending ─────────────────────────────────────────────────────────────
def send_email(subject, html_body):
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    notify_to = os.environ.get("NOTIFY_TO", "eosher@nvidia.com")
    notify_from = os.environ.get("NOTIFY_FROM", "kinneret-forecast@bot.dev")

    if not smtp_host or not smtp_user or not smtp_pass:
        print("SMTP secrets not configured — skipping email send.")
        print("Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD secrets in the repo.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = notify_from
    msg["To"] = notify_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(notify_from, [notify_to], msg.as_string())
        server.quit()
        print(f"Email sent to {notify_to}")
        return True
    except Exception as e:
        print(f"Email send failed: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Kinneret Surf Forecast Bot")
    print("=" * 60)

    # Fetch all data
    all_data = fetch_all_data()

    # Check we got data
    total_models = sum(
        1 for s in all_data.values() for m in MODELS if all_data[s] is not None and s in all_data
    )
    # Just count successful fetches
    success_count = 0
    for spot_id in all_data:
        for model in MODELS:
            if all_data[spot_id].get(model) is not None:
                success_count += 1
    print(f"\nSuccessfully fetched {success_count}/{len(SPOTS) * len(MODELS)} forecasts")

    if success_count == 0:
        print("ERROR: No forecast data retrieved. Exiting.")
        return

    # Get days
    days = get_days_from_data(all_data)
    print(f"Forecast days: {days}")

    # Print analysis summary
    for date_str in days:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_heb = WEEKDAY_HEB.get(dt.weekday(), "")
        print(f"\n--- {day_heb} {date_str} ---")
        for spot_id, spot in SPOTS.items():
            w, g = morning_stats(all_data, spot_id, date_str)
            if w is not None:
                ef = score_efoil(w, g)
                sp = score_sup(w, g)
                print(f"  {spot['name_en']}: wind={w:.1f}kn gusts={g:.1f}kn | eFoil={ef}/10 SUP={sp}/10")
            else:
                print(f"  {spot['name_en']}: no data")

    # Build HTML
    subject, html_body = build_html(all_data, days)
    print(f"\nSubject: {subject}")

    # Try sending email
    sent = send_email(subject, html_body)

    if not sent:
        print("\n" + "=" * 60)
        print("HTML OUTPUT (fallback — email not sent)")
        print("=" * 60)
        print(html_body)

    print("\nDone!")


if __name__ == "__main__":
    main()

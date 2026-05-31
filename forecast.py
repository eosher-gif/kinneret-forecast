#!/usr/bin/env python3
"""Kinneret surf forecast bot -- eFoil & SUP scoring for Sea of Galilee.

Fetches wind forecast from Open-Meteo API (ECMWF, ICON, GFS) for Ginosar
and Ein Gev, scores conditions for eFoil and SUP, sends styled Hebrew HTML email.

stdlib only -- no external dependencies.
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# -- Spots -----------------------------------------------------------------

SPOTS = {
    "ginosar": {"name": "גינוסר", "name_en": "Ginosar", "lat": 32.87, "lon": 35.52},
    "ein_gev": {"name": "עין גב", "name_en": "Ein Gev", "lat": 32.78, "lon": 35.64},
}

MODELS = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless"]

WEEKDAY_HEB = {
    0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי",
    4: "שישי", 5: "שבת", 6: "ראשון",
}

WIND_DIR_HEB = [
    (0, "צפון"), (45, "צפון-מזרח"), (90, "מזרח"), (135, "דרום-מזרח"),
    (180, "דרום"), (225, "דרום-מערב"), (270, "מערב"), (315, "צפון-מערב"), (360, "צפון"),
]


# -- Helpers ----------------------------------------------------------------

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


def score_text_color(score):
    if score >= 9:
        return "#28a745"
    if score >= 7:
        return "#007bff"
    if score >= 5:
        return "#856404"
    return "#dc3545"


def efoil_verdict(score):
    """Return (short label, detailed water description)."""
    if score >= 9:
        return "לכו לגלוש!", "פלטה — מים שטוחים כמראה, טיסה חלקה ומושלמת"
    if score >= 8:
        return "לכו לגלוש!", "כמעט פלטה — אדוות עדינות מאוד, טיסה חלקה"
    if score >= 7:
        return "גבולי-באמפי", "אדווה קלה מתחילה — ציפול מורגש, טיסה סבירה עם נקישות"
    if score >= 6:
        return "גבולי-באמפי", "צ'ופ מורגש — אדוות קשיחות, טיסה לא חלקה"
    if score >= 5:
        return "לא מומלץ", "צ'ופ קופצני — גלים קצרים ותכופים, קשה לשמור על יציבות"
    return "לא מומלץ", "גלי ומסוער — תנאים לא ראויים לטיסה"


def sup_verdict(score):
    """Return (short label, safety description)."""
    if score >= 9:
        return "בטוח לחלוטין", "רוח אפסית, אפס סכנת סחיפה. מתאים גם למתחילים"
    if score >= 8:
        return "בטוח לחלוטין", "רוח קלה מאוד, ללא סכנת סחיפה"
    if score >= 7:
        return "אזהרה: גבולי", "רוח מתחזקת — תישארו בטווח 100 מטר מהחוף"
    if score >= 6:
        return "אזהרה: גבולי", "סכנת סחיפה מתחילה — חובה להישאר צמוד לחוף"
    if score >= 4:
        return "סכנה: לא להיכנס", "רוח חזקה מדי לסאפ — סכנת סחיפה לעומק האגם"
    return "סכנה: לא להיכנס!", "סכנת חיים — איסור מוחלט להיכנס למים על סאפ"


# -- API fetch --------------------------------------------------------------

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
            req = urllib.request.Request(url, headers={"User-Agent": "KinneretForecastBot/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
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

    def pick(prefix):
        candidates = [k for k in h if prefix in k]
        return h[candidates[0]] if candidates else []

    return {
        "times": times,
        "wind": pick("wind_speed"),
        "gusts": pick("wind_gusts"),
        "direction": pick("wind_direction"),
        "temperature": pick("temperature"),
    }


# -- Data aggregation -------------------------------------------------------

def fetch_all_data():
    all_data = {}
    for spot_id, spot in SPOTS.items():
        all_data[spot_id] = {}
        print(f"Fetching forecasts for {spot['name_en']}...")
        for model in MODELS:
            raw = fetch_forecast(spot["lat"], spot["lon"], model)
            if raw:
                hourly = extract_hourly(raw)
                if hourly["times"]:
                    all_data[spot_id][model] = hourly
                    print(f"    Got {len(hourly['times'])} hours")
                else:
                    print(f"    WARNING: No hourly data")
                    all_data[spot_id][model] = None
            else:
                all_data[spot_id][model] = None
    return all_data


def get_days_from_data(all_data):
    for spot_id in all_data:
        for model in MODELS:
            d = all_data[spot_id].get(model)
            if d and d["times"]:
                return sorted(set(t[:10] for t in d["times"]))[:3]
    return []


def get_hour_index(times, date_str, hour):
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
        if idx is None:
            continue
        if idx < len(d["wind"]) and d["wind"][idx] is not None:
            winds.append(d["wind"][idx])
        if idx < len(d["gusts"]) and d["gusts"][idx] is not None:
            gusts.append(d["gusts"][idx])
        if idx < len(d["direction"]) and d["direction"][idx] is not None:
            dirs.append(d["direction"][idx])
        if idx < len(d["temperature"]) and d["temperature"][idx] is not None:
            temps.append(d["temperature"][idx])

    if not winds:
        return None

    return {
        "wind_avg": sum(winds) / len(winds),
        "wind_min": min(winds),
        "wind_max": max(winds),
        "gust_avg": sum(gusts) / len(gusts) if gusts else 0,
        "gust_max": max(gusts) if gusts else 0,
        "dir_avg": sum(dirs) / len(dirs) if dirs else None,
        "temp_avg": sum(temps) / len(temps) if temps else None,
        "model_spread": max(winds) - min(winds) if len(winds) > 1 else 0,
    }


def morning_stats(all_data, spot_id, date_str):
    """Get average wind and average gusts for 08:00-10:00 session window.

    Uses AVERAGE gusts (not max) across models and hours, because taking
    the single worst gust from any model is too conservative for the Kinneret
    where models disagree significantly due to low resolution.
    """
    winds, gusts_list = [], []
    for hour in range(8, 11):  # 08:00, 09:00, 10:00 — entry at 08:30
        agg = aggregate_hour(all_data, spot_id, date_str, hour)
        if agg:
            winds.append(agg["wind_avg"])
            gusts_list.append(agg["gust_avg"])  # average gusts, not max
    if not winds:
        return None, None
    return sum(winds) / len(winds), max(gusts_list)


# -- Scoring -----------------------------------------------------------------

def score_efoil(avg_wind, max_gust):
    """eFoil score = water smoothness ONLY (based on gusts).

    eFoil is electric — wind doesn't affect propulsion, only water chop.
    Score is determined purely by gust level.
    """
    if max_gust is None:
        return 0
    if max_gust < 4:
        return 10  # glass / plata
    if max_gust < 5:
        return 9
    if max_gust < 6:
        return 8
    if max_gust < 8:
        return 7   # light chop starting
    if max_gust < 10:
        return 6
    if max_gust < 13:
        return 5   # choppy
    return 4        # rough


def score_sup(avg_wind, max_gust):
    """SUP score = drifting danger ONLY. LIFE-SAFETY scoring.

    SUP rider = human sail. This scoring is deliberately conservative.
    A wrong recommendation can be life-threatening on the Kinneret.

    Hard ceilings:
    - Gusts 10+ kn → 1/10 (NO ENTRY)
    - Wind > 5kn OR Gusts > 7kn → max 5/10
    """
    if avg_wind is None:
        return 0
    # HARD CEILING: gusts 10+ = NO ENTRY
    if max_gust >= 10:
        return 1
    # HARD CEILING: wind > 5 or gusts > 7 = max 5/10
    if avg_wind > 5 or max_gust > 7:
        return min(5, max(1, 8 - int(avg_wind)))
    # Below thresholds — safe zone
    if avg_wind < 2 and max_gust < 4:
        return 10
    if avg_wind < 3 and max_gust < 5:
        return 9
    if avg_wind < 3.5 and max_gust < 6:
        return 8
    if avg_wind < 4 and max_gust < 6.5:
        return 7
    return 6       # borderline


def check_ein_gev_offshore(all_data, date_str):
    """LIFE-SAFETY CHECK: Ein Gev east coast "death trap" rule.

    ANY Easterly wind component (E, SE, NE) between 06:00-11:00 at Ein Gev
    triggers an automatic SUP score drop to 3/10 or lower.
    The water near shore looks deceptively calm with offshore wind,
    but a SUP paddler will be pushed to the center of the lake.
    """
    for hour in range(6, 11):
        agg = aggregate_hour(all_data, "ein_gev", date_str, hour)
        if not agg or agg["dir_avg"] is None:
            continue
        direction = agg["dir_avg"] % 360
        # ANY easterly component: NE (30) through SE (160)
        is_east = 30 <= direction <= 160
        if is_east and agg["wind_avg"] > 2:
            return True, (
                "⚠️ אזהרת אופ-שור חמורה! סכנת חיים! "
                f"רוח גב {agg['wind_avg']:.0f}kn מכיוון {wind_dir_to_hebrew(direction)} "
                "דוחפת למרכז האגם. המים ליד החוף נראים מטעים וחלקים, "
                "אך חל איסור מוחלט להתרחק מהחוף על סאפ!"
            )
    return False, ""


# -- Safety & analysis -------------------------------------------------------

def find_window_closure(all_data, spot_id, date_str):
    """Find when the session window closes.

    Kinneret rule: afternoon westerly winds ALWAYS drop from the mountains
    earlier than global models predict. If models show 12+ knots arriving
    at 13:00-14:00, override to 11:00 (11:30 max). Never tell a SUP
    paddler the window is open past noon when afternoon winds are expected.
    """
    raw_closure = None
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
                raw_closure = hour
                break
        if raw_closure is not None:
            break

    if raw_closure is None:
        return None

    # THE 11:00 RULE: Kinneret briza ALWAYS arrives earlier than models show.
    # STRICTLY FORBIDDEN to show window open past 11:00 when afternoon winds expected.
    if raw_closure >= 12:
        return "11:00 (חובה לצאת מהמים!)"
    return f"{raw_closure:02d}:00"


def detect_alerts(all_data, date_str):
    """Detect safety alerts for a given day."""
    alerts = []

    # Offshore wind at Ein Gev (E/SE pushing to deep water)
    is_offshore, offshore_alert = check_ein_gev_offshore(all_data, date_str)
    if is_offshore:
        alerts.append(offshore_alert)

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
            if agg and agg["temp_avg"] is not None and agg["temp_avg"] > 35:
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

    gin_score = gin_wind + (gin_gust * 0.3)
    ein_score = ein_wind + (ein_gust * 0.3)

    if gin_score < ein_score - 1:
        return "גינוסר (מוגן יותר בבוקר)"
    if ein_score < gin_score - 1:
        return "עין גב (שקט יותר היום)"
    return "שניהם דומים — בחרו לפי נוחות"


# -- HTML email builder -------------------------------------------------------

def _spot_section(spot_name, spot_type, efoil_score, sup_score, avg_wind, avg_gust, window, is_offshore, offshore_alert):
    """Build HTML for a single spot section."""
    F = "font-family:Arial,sans-serif;"
    ef_label, ef_water = efoil_verdict(efoil_score)
    sp_label, sp_safety = sup_verdict(sup_score)

    # eFoil color
    ef_color = score_color(efoil_score)
    ef_text_c = score_text_color(efoil_score)
    # SUP color
    sp_color = score_color(sup_score)
    sp_text_c = score_text_color(sup_score)

    wind_str = f"{avg_wind:.1f}" if avg_wind else "—"
    gust_str = f"{avg_gust:.1f}" if avg_gust else "—"
    window_str = window if window else "לא צפויה סגירה"

    html = f"""
<div style="margin:12px 0;padding:16px;border:1px solid #e2e8f0;border-radius:12px;background:#fff;">
  <h3 style="{F}color:#0369a1;margin:0 0 12px;font-size:16px;text-align:right;">
    📍 {spot_name} ({spot_type})
  </h3>
  <div style="{F}font-size:13px;color:#475569;margin-bottom:10px;text-align:right;">
    ⏰ <b>חלון זמן:</b> עד <b>{window_str}</b>
  </div>

  <div style="background:{ef_color};border-radius:8px;padding:12px;margin-bottom:8px;">
    <div style="{F}font-size:14px;text-align:right;">
      ⚡ <b>eFoil (איכות הטיסה):
      <span style="color:{ef_text_c};font-size:18px;">{efoil_score}/10</span>
      → {ef_label}</b>
    </div>
    <div style="{F}font-size:13px;color:#334155;margin-top:4px;text-align:right;">
      <i>מצב המים:</i> {ef_water}
    </div>
  </div>

  <div style="background:{sp_color};border-radius:8px;padding:12px;">
    <div style="{F}font-size:14px;text-align:right;">
      🧘 <b>SUP (מדד בטיחות):
      <span style="color:{sp_text_c};font-size:18px;">{sup_score}/10</span>
      → {sp_label}</b>
    </div>
    <div style="{F}font-size:13px;color:#334155;margin-top:4px;text-align:right;">
      <i>בטיחות:</i> {sp_safety}
    </div>
  </div>
"""
    if is_offshore:
        html += f"""
  <div style="margin-top:8px;padding:10px;background:#f8d7da;border:2px solid #dc3545;border-radius:8px;{F}font-size:13px;color:#721c24;text-align:right;">
    <b>{offshore_alert}</b>
  </div>
"""
    html += "</div>\n"
    return html


def build_html(all_data, days):
    """Build HTML email in the exact per-day, per-spot format."""

    confidence_labels = {0: "גבוהה", 1: "בינונית", 2: "נמוכה"}
    F = "font-family:Arial,sans-serif;"

    parts = []

    # Header
    parts.append(f"""
<div style="background:linear-gradient(135deg,#0ea5e9,#0369a1);padding:24px;text-align:center;">
  <div style="font-size:36px;">🌊</div>
  <h1 style="color:#fff;margin:8px 0 0;font-size:22px;{F}">תחזית גלישה וסאפ — כנרת</h1>
  <p style="color:#e0f2fe;margin:6px 0 0;font-size:13px;{F}">חלון בוקר 08:30–11:00 · מודלים: ECMWF + ICON + GFS</p>
</div>
""")

    # Track best day for subject line
    best_day_efoil, best_day_sup, best_day_name = 0, 0, ""

    for day_idx, date_str in enumerate(days):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_heb = WEEKDAY_HEB.get(dt.weekday(), "")
        conf = confidence_labels.get(day_idx, "נמוכה")

        # Score each spot
        gin_w, gin_g = morning_stats(all_data, "ginosar", date_str)
        ein_w, ein_g = morning_stats(all_data, "ein_gev", date_str)

        gin_efoil = score_efoil(gin_w, gin_g) if gin_w is not None else 0
        gin_sup = score_sup(gin_w, gin_g) if gin_w is not None else 0
        ein_efoil = score_efoil(ein_w, ein_g) if ein_w is not None else 0
        ein_sup = score_sup(ein_w, ein_g) if ein_w is not None else 0

        # Ein Gev offshore check
        is_offshore, offshore_alert = check_ein_gev_offshore(all_data, date_str)
        if is_offshore:
            ein_sup = min(ein_sup, 3)

        # Window closure
        gin_window = find_window_closure(all_data, "ginosar", date_str)
        ein_window = find_window_closure(all_data, "ein_gev", date_str)

        # General alerts
        alerts = detect_alerts(all_data, date_str)

        # Day summary
        best_efoil = max(gin_efoil, ein_efoil)
        best_sup = max(gin_sup, ein_sup)

        if best_efoil + best_sup > best_day_efoil + best_day_sup:
            best_day_efoil = best_efoil
            best_day_sup = best_sup
            best_day_name = day_heb

        # General trend text
        if best_sup >= 8:
            trend = "יום מצוין לגלישה וסאפ — רוח חלשה ומים שקטים"
        elif best_sup >= 7:
            trend = "יום סביר — רוח קלה, סאפ גבולי, eFoil בסדר"
        elif best_efoil >= 7:
            trend = "יום טוב ל-eFoil בלבד — רוח חזקה מדי לסאפ"
        else:
            trend = "יום לא מומלץ — רוח ומשבים גבוהים מדי"

        # Day section
        parts.append(f"""
<div style="padding:20px;border-top:3px solid #0ea5e9;">
  <h2 style="{F}color:#0f172a;margin:0 0 4px;font-size:19px;text-align:right;">
    📅 תחזית גלישה וסאפ ליום {day_heb} {date_str}
  </h2>
  <div style="{F}font-size:13px;color:#64748b;margin-bottom:4px;text-align:right;">
    🎯 אמינות תחזית: <b>{conf}</b>
  </div>
  <div style="{F}font-size:14px;color:#334155;margin-bottom:16px;text-align:right;background:#f0f9ff;padding:10px;border-radius:8px;">
    {trend}
  </div>
""")

        # Spot 1: Ginosar
        parts.append(_spot_section(
            "גינוסר", "חוף מערבי",
            gin_efoil, gin_sup, gin_w, gin_g, gin_window,
            False, ""  # Ginosar never has offshore danger
        ))

        # Spot 2: Ein Gev
        parts.append(_spot_section(
            "עין גב", "חוף מזרחי",
            ein_efoil, ein_sup, ein_w, ein_g, ein_window,
            is_offshore, offshore_alert
        ))

        # Day-level alerts
        if alerts:
            alerts_html = "<br>".join(alerts)
            parts.append(f"""
  <div style="margin-top:8px;padding:10px;background:#fff3cd;border:1px solid #ffc107;border-radius:8px;{F}font-size:13px;color:#92400e;text-align:right;">
    <b>⚠️ התראות בטיחות:</b><br>{alerts_html}
  </div>
""")

        parts.append("</div>\n")

    # Footer
    parts.append(f"""
<div style="padding:12px 20px;background:#f8fafc;{F}font-size:11px;color:#94a3b8;text-align:right;">
  ⚡ eFoil = איכות מים בלבד (משבים) · 🧘 SUP = סכנת סחיפה בלבד (רוח) · הציונים עצמאיים לחלוטין<br>
  ⏰ חלון 11:00 = כלל בטיחות כנרת — הבריזה המערבית תמיד מגיעה לפני מה שהמודלים מראים
</div>
<div style="background:#1e293b;padding:14px;text-align:center;">
  <p style="color:#94a3b8;margin:0;{F}font-size:11px;">
    Kinneret Forecast Bot 🤖 · ECMWF + ICON + GFS · Open-Meteo
  </p>
</div>
""")

    body_html = f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f4f8;{F}direction:rtl;">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.1);">
{"".join(parts)}
</div>
</body>
</html>"""

    subject = f"\U0001f30a כנרת — היום הכי טוב: יום {best_day_name} | eFoil {best_day_efoil}/10 | SUP {best_day_sup}/10"

    return subject, body_html


# -- Email sending ------------------------------------------------------------

def send_email(subject, html_body):
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    notify_to = os.environ.get("NOTIFY_TO", "eilonosher@gmail.com")
    notify_from = os.environ.get("NOTIFY_FROM", "kinneret-forecast@bot.dev")

    if not all([smtp_host, smtp_user, smtp_pass]):
        print("SMTP not configured -- printing HTML to stdout")
        print("=" * 60)
        print(f"Subject: {subject}")
        print("=" * 60)
        print(html_body)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = notify_from
    msg["To"] = notify_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        print(f"Connecting to SMTP {smtp_host}:{smtp_port}...")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(notify_from, notify_to.split(","), msg.as_string())
        print(f"Email sent to {notify_to}")
        return True
    except Exception as e:
        print(f"SMTP failed: {e}")
        print("Falling back to stdout")
        print("=" * 60)
        print(f"Subject: {subject}")
        print("=" * 60)
        print(html_body)
        return False


# -- Main ---------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Kinneret Surf Forecast Bot")
    print("=" * 60)

    # Parse --html-out flag
    html_out = None
    if "--html-out" in sys.argv:
        idx = sys.argv.index("--html-out")
        if idx + 1 < len(sys.argv):
            html_out = sys.argv[idx + 1]

    all_data = fetch_all_data()

    success_count = sum(
        1 for sid in all_data for m in MODELS if all_data[sid].get(m) is not None
    )
    print(f"\nSuccessfully fetched {success_count}/{len(SPOTS) * len(MODELS)} forecasts")

    if success_count == 0:
        print("ERROR: No forecast data retrieved. Exiting.")
        raise SystemExit(1)

    days = get_days_from_data(all_data)
    if not days:
        print("ERROR: No forecast days found. Exiting.")
        raise SystemExit(1)

    print(f"Forecast days: {days}")

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

    subject, html_body = build_html(all_data, days)
    print(f"\nSubject: {subject}")

    # Write HTML to file for GitHub Actions email action
    if html_out:
        Path(html_out).write_text(html_body, encoding="utf-8")
        print(f"HTML saved to {html_out}")
        # Set subject as GitHub Actions env var
        gh_env = os.environ.get("GITHUB_ENV")
        if gh_env:
            with open(gh_env, "a") as f:
                f.write(f"EMAIL_SUBJECT={subject}\n")

    # Try direct SMTP only if not using html-out (GitHub Actions handles email)
    if not html_out:
        send_email(subject, html_body)

    print("\nDone!")


if __name__ == "__main__":
    main()

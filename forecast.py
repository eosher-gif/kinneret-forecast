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


def efoil_rec(score):
    return {
        10: "מושלם! מים שטוחים כמראה 🪞",
        9: "מצוין — כמעט שטוח",
        8: "מעולה — שקט מאוד",
        7: "טוב — ציפול קל מתחיל",
        6: "סביר — ציפול מורגש",
        5: "גרוע — גלי",
    }.get(score, "לא מומלץ 🚫")


def sup_rec(score):
    return {
        10: "מושלם! שטוח ובטוח 🧘",
        9: "מצוין — מים רגועים",
        8: "מעולה — שקט ובטוח",
        7: "גבולי — תישארו קרוב לחוף",
        6: "מסוכן — סכנת סחיפה ⚠️",
    }.get(score, "מסוכן! אל תצאו 🚫")


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


# -- Scoring -----------------------------------------------------------------

def score_efoil(avg_wind, max_gust):
    """eFoil needs CALM flat water (electric -- doesn't need wind)."""
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
    """SUP scoring -- danger = drift + capsize."""
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


# -- Safety & analysis -------------------------------------------------------

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
    return None


def detect_alerts(all_data, date_str):
    """Detect safety alerts for a given day."""
    alerts = []

    # Offshore wind at Ein Gev (direction 60-120 in morning)
    for hour in range(7, 12):
        agg = aggregate_hour(all_data, "ein_gev", date_str, hour)
        if agg and agg["dir_avg"] is not None and 60 <= agg["dir_avg"] <= 120 and agg["wind_avg"] > 3:
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

def _verdict(score):
    """Return go/no-go emoji + text."""
    if score >= 8:
        return "✅", "לכו!"
    if score >= 7:
        return "⚠️", "אפשר, זהירות"
    if score >= 5:
        return "❌", "לא מומלץ"
    return "🚫", "מסוכן"


def build_html(all_data, days):
    """Build a clean, decision-focused HTML email."""

    # Collect per-day data for ranking
    day_rows = []
    for day_idx, date_str in enumerate(days):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_heb = WEEKDAY_HEB.get(dt.weekday(), "")

        # Best scores across both spots
        best_efoil, best_sup = 0, 0
        best_spot = "—"
        for spot_id in SPOTS:
            w, g = morning_stats(all_data, spot_id, date_str)
            if w is not None:
                ef = score_efoil(w, g)
                sp = score_sup(w, g)
                if ef > best_efoil or sp > best_sup:
                    best_spot = SPOTS[spot_id]["name"]
                best_efoil = max(best_efoil, ef)
                best_sup = max(best_sup, sp)

        window = find_window_closure(all_data, "ginosar", date_str)
        alerts = detect_alerts(all_data, date_str)

        # Morning wind summary (best spot)
        gin_w, gin_g = morning_stats(all_data, "ginosar", date_str)
        ein_w, ein_g = morning_stats(all_data, "ein_gev", date_str)

        day_rows.append({
            "date": date_str, "day_heb": day_heb, "day_idx": day_idx,
            "efoil": best_efoil, "sup": best_sup, "spot": best_spot,
            "window": window, "alerts": alerts,
            "gin_wind": gin_w, "gin_gust": gin_g,
            "ein_wind": ein_w, "ein_gust": ein_g,
        })

    # Sort by best combined score for ranking
    ranked = sorted(day_rows, key=lambda r: r["efoil"] + r["sup"], reverse=True)

    # Subject from best day
    best = ranked[0]
    subject = f"\U0001f30a כנרת — היום הכי טוב: יום {best['day_heb']} | eFoil {best['efoil']}/10 | SUP {best['sup']}/10"

    confidence_labels = {0: "גבוהה", 1: "בינונית", 2: "נמוכה"}

    parts = []

    # Header
    parts.append("""
<div style="background:linear-gradient(135deg,#0ea5e9,#0369a1);padding:24px;text-align:center;">
  <div style="font-size:32px;">🌊</div>
  <h1 style="color:#fff;margin:8px 0 0;font-size:22px;font-family:Arial,sans-serif;">תחזית גלישה — כנרת</h1>
  <p style="color:#e0f2fe;margin:6px 0 0;font-size:14px;font-family:Arial,sans-serif;">חלון בוקר 08:30–11:00 | גינוסר ועין גב</p>
</div>
""")

    # Ranking table
    parts.append("""
<div style="padding:20px;">
  <h2 style="color:#0f172a;margin:0 0 16px;font-size:18px;font-family:Arial,sans-serif;text-align:right;">📊 דירוג הימים</h2>
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-family:Arial,sans-serif;">
""")

    for rank, row in enumerate(ranked, 1):
        ef_emoji, ef_text = _verdict(row["efoil"])
        sp_emoji, sp_text = _verdict(row["sup"])

        # Row background based on rank
        if rank == 1:
            row_bg = "#f0fdf4"  # green tint
            border_color = "#22c55e"
            medal = "🥇"
        elif rank == 2:
            row_bg = "#f0f9ff"
            border_color = "#38bdf8"
            medal = "🥈"
        elif rank == 3:
            row_bg = "#fefce8"
            border_color = "#facc15"
            medal = "🥉"
        else:
            row_bg = "#f8fafc"
            border_color = "#e2e8f0"
            medal = ""

        # Window text
        window_text = f"עד {row['window']}" if row['window'] else "כל הבוקר"

        # Confidence
        conf = confidence_labels.get(row["day_idx"], "נמוכה")

        parts.append(f"""
    <tr style="background:{row_bg};border-right:4px solid {border_color};">
      <td style="padding:16px 12px;vertical-align:top;width:50px;text-align:center;">
        <div style="font-size:24px;">{medal}</div>
        <div style="font-size:11px;color:#94a3b8;">#{rank}</div>
      </td>
      <td style="padding:16px 8px;vertical-align:top;">
        <div style="font-size:17px;font-weight:bold;color:#0f172a;margin-bottom:6px;">יום {row['day_heb']} — {row['date']}</div>
        <table cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
          <tr>
            <td style="padding:3px 12px 3px 0;font-size:14px;">⚡ eFoil:</td>
            <td style="padding:3px 0;font-size:18px;font-weight:bold;color:{score_text_color(row['efoil'])};">{row['efoil']}/10</td>
            <td style="padding:3px 0 3px 8px;font-size:13px;">{ef_emoji} {ef_text}</td>
          </tr>
          <tr>
            <td style="padding:3px 12px 3px 0;font-size:14px;">🧘 SUP:</td>
            <td style="padding:3px 0;font-size:18px;font-weight:bold;color:{score_text_color(row['sup'])};">{row['sup']}/10</td>
            <td style="padding:3px 0 3px 8px;font-size:13px;">{sp_emoji} {sp_text}</td>
          </tr>
        </table>
        <div style="font-size:12px;color:#64748b;">
          📍 ספוט: <b>{row['spot']}</b> · ⏰ חלון: {window_text} · 🎯 אמינות: {conf}
        </div>
""")

        # Alerts inline
        if row["alerts"]:
            alerts_str = " · ".join(row["alerts"])
            parts.append(f"""
        <div style="margin-top:6px;padding:6px 10px;background:#fff3cd;border-radius:6px;font-size:12px;color:#92400e;">
          {alerts_str}
        </div>
""")

        parts.append("""
      </td>
    </tr>
    <tr><td colspan="2" style="height:2px;background:#e2e8f0;"></td></tr>
""")

    parts.append("  </table>\n</div>\n")

    # Quick legend
    parts.append("""
<div style="padding:12px 20px;background:#f8fafc;font-family:Arial,sans-serif;font-size:12px;color:#64748b;text-align:right;">
  <b>מה הציונים אומרים:</b><br>
  ⚡ eFoil = איכות מים (חלקות). 8+ = פלטה, 5-7 = ציפול, מתחת 5 = גלי<br>
  🧘 SUP = בטיחות. 8+ = בטוח, 7 = גבולי, מתחת 7 = סכנת סחיפה<br>
  ⏰ חלון = מתי הבריזה המערבית מגיעה (רוח 12+ קשר)
</div>
""")

    # Footer
    parts.append("""
<div style="background:#1e293b;padding:14px;text-align:center;">
  <p style="color:#94a3b8;margin:0;font-family:Arial,sans-serif;font-size:11px;">
    Kinneret Forecast Bot 🤖 · ECMWF + ICON + GFS · Open-Meteo
  </p>
</div>
""")

    body_html = f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;direction:rtl;">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.1);">
{"".join(parts)}
</div>
</body>
</html>"""

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

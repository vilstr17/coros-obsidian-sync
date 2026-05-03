#!/usr/bin/env python3
"""
COROS → Obsidian training log sync.
Usage: ./sync.py [--days 7] [--full-refresh]
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

from coros_client import (
    login, try_auto_login, get_stored_auth,
    fetch_daily_records, fetch_activities, fetch_activity_detail,
    fetch_sleep,
    StoredAuth, Activity, DailyRecord, SleepRecord,
)
from obsidian_writer import ObsidianWriter

# Load env
load_dotenv(os.path.expanduser("~/training-sync/.env"))

VAULT = os.environ.get("OBSIDIAN_VAULT", "Life")
API_KEY = os.environ.get("OBSIDIAN_API_KEY", "")
API_URL = os.environ.get("OBSIDIAN_API_URL", "http://127.0.0.1:27123")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def date_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def epoch_to_date(epoch: str) -> str:
    try:
        return datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d")
    except:
        return datetime.utcnow().strftime("%Y-%m-%d")

def normalize_date(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw

def month_from_raw(raw: str) -> str:
    return normalize_date(raw)[:7]

async def ensure_auth() -> StoredAuth:
    """Get valid auth, auto-login if needed."""
    auth = get_stored_auth()
    if auth:
        return auth

    print("🔐 No valid token, attempting auto-login...")
    auth = await try_auto_login()
    if auth:
        print("✅ Auto-login successful")
        return auth

    print("❌ Auto-login failed. Make sure COROS_EMAIL and COROS_PASSWORD are set in .env")
    sys.exit(1)

async def sync_daily_notes(writer: ObsidianWriter, auth: StoredAuth, days: int):
    """Sync daily notes with metrics, sleep, and activity summaries."""
    end = datetime.utcnow()
    start = end - timedelta(days=days)

    start_day = date_str(start)
    end_day = date_str(end)

    print(f"📅 Syncing daily notes: {start_day} → {end_day}")

    # Fetch all data in parallel
    daily_task = fetch_daily_records(auth, start_day, end_day)
    activities_task = fetch_activities(auth, start_day, end_day)
    sleep_task = fetch_sleep(auth, start_day, end_day)

    daily_records, activities, sleep_records = await asyncio.gather(
        daily_task, activities_task, sleep_task
    )

    # Index by date
    daily_by_date: dict[str, dict] = {}
    for d in daily_records:
        daily_by_date[d.date] = {
            "rhr": d.rhr,
            "avg_sleep_hrv": d.avg_sleep_hrv,
            "hrv_baseline": d.hrv_baseline,
            "training_load": d.training_load,
            "training_load_ratio": d.training_load_ratio,
            "vo2max": d.vo2max,
            "stamina_level": d.stamina_level,
            "stamina_level_7d": d.stamina_level_7d,
            "performance": d.performance,
            "distance": d.distance,
            "duration": d.duration,
        }

    sleep_by_date: dict[str, dict] = {}
    for s in sleep_records:
        sleep_by_date[s.date] = {
            "total_minutes": s.total_minutes,
            "deep_minutes": s.deep_minutes,
            "light_minutes": s.light_minutes,
            "rem_minutes": s.rem_minutes,
            "awake_minutes": s.awake_minutes,
            "nap_minutes": s.nap_minutes,
            "quality_score": s.quality_score,
            "avg_hr": s.avg_hr,
        }

    # Group activities by date
    activities_by_date: dict[str, list[dict]] = {}
    for a in activities:
        date = a.date
        if date not in activities_by_date:
            activities_by_date[date] = []
        # Fetch detail (HR zones, laps) for each activity
        print(f"  🔍 Fetching detail for {a.sport_name} ({a.activity_id[:8]})")
        try:
            detailed = await fetch_activity_detail(auth, a)
            activities_by_date[date].append({
                "activity_id": detailed.activity_id,
                "name": detailed.name,
                "sport_name": detailed.sport_name,
                "date": date,
                "duration_seconds": detailed.duration_seconds,
                "distance_meters": detailed.distance_meters,
                "avg_hr": detailed.avg_hr,
                "max_hr": detailed.max_hr,
                "calories": detailed.calories,
                "avg_power": detailed.avg_power,
                "normalized_power": detailed.normalized_power,
                "elevation_gain": detailed.elevation_gain,
                "elevation_loss": detailed.elevation_loss,
                "avg_pace": detailed.avg_pace,
                "avg_speed": detailed.avg_speed,
                "avg_cadence": detailed.avg_cadence,
                "training_load": detailed.training_load,
                "hr_zones": detailed.hr_zones,
                "lap_list": detailed.lap_list,
            })
        except Exception as e:
            print(f"    ⚠️ Detail failed: {e}")
            activities_by_date[date].append({
                "activity_id": a.activity_id,
                "name": a.name,
                "sport_name": a.sport_name,
                "date": date,
                "duration_seconds": a.duration_seconds,
                "distance_meters": a.distance_meters,
                "avg_hr": a.avg_hr,
                "max_hr": a.max_hr,
                "calories": a.calories,
                "avg_power": a.avg_power,
                "elevation_gain": a.elevation_gain,
                "elevation_loss": a.elevation_loss,
                "avg_pace": a.avg_pace,
                "avg_speed": a.avg_speed,
                "avg_cadence": a.avg_cadence,
                "training_load": a.training_load,
            })

    # Write daily notes
    current = start
    while current <= end:
        d = date_str(current)
        daily = daily_by_date.get(d)
        sleep = sleep_by_date.get(d)
        acts = activities_by_date.get(d, [])

        note_path = writer.daily_note_path(d)
        content = writer.build_daily_note(d, daily, acts, sleep)

        await writer.create_folder("Training Log/00-Daily")
        await writer.write_note(note_path, content)
        print(f"  ✅ Daily: {d} ({len(acts)} activities)")

        current += timedelta(days=1)

    return activities_by_date

async def sync_activity_notes(writer: ObsidianWriter, activities_by_date: dict):
    """Write detailed activity notes."""
    print("🏃 Syncing activity detail notes...")
    await writer.create_folder("Training Log/01-Activities")

    for date, acts in activities_by_date.items():
        for act in acts:
            filename = f"{date}_{act['sport_name'].replace(' ', '_')}_{act['activity_id'][:8]}"
            note_path = writer.activity_note_path(date, act['sport_name'], act['activity_id'])
            content = writer.build_activity_note(act)
            await writer.write_note(note_path, content)
            print(f"  ✅ Activity: {filename}")

async def sync_weekly_notes(writer: ObsidianWriter, activities_by_date: dict, daily_records: list, sleep_records: list, days: int):
    """Generate weekly summary notes."""
    print("📊 Syncing weekly summaries...")
    await writer.create_folder("Training Log/02-Weekly")

    # Group by ISO week
    from collections import defaultdict
    weeks = defaultdict(lambda: {"days": [], "activities": 0, "distance": 0, "duration": 0, "load": 0, "hrv": [], "sleep": []})

    end = datetime.utcnow()
    start = end - timedelta(days=days)
    current = start
    while current <= end:
        d = date_str(current)
        iso_year, iso_week, _ = current.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        weeks[week_key]["days"].append(d)

        # Add activity stats
        for act in activities_by_date.get(d, []):
            weeks[week_key]["activities"] += 1
            if act.get("distance_meters"):
                weeks[week_key]["distance"] += act["distance_meters"]
            weeks[week_key]["duration"] += act.get("duration_seconds", 0)
            weeks[week_key]["load"] += act.get("training_load", 0) or 0

        current += timedelta(days=1)

    # Add HRV and sleep stats
    def _parse_date(d):
        if len(d) == 8 and d.isdigit():
            return datetime.strptime(d, "%Y%m%d")
        return datetime.strptime(d, "%Y-%m-%d")

    for rec in daily_records:
        dt = _parse_date(rec.date)
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if rec.avg_sleep_hrv:
            weeks[week_key]["hrv"].append(rec.avg_sleep_hrv)

    for s in sleep_records:
        dt = _parse_date(s.date)
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        weeks[week_key]["sleep"].append(s.total_minutes)

    for week_key, stats in sorted(weeks.items()):
        week_start = min(stats["days"]) if stats["days"] else week_key
        week_end = max(stats["days"]) if stats["days"] else week_key

        summary = {
            "total_activities": stats["activities"],
            "total_distance_km": stats["distance"] / 1000,
            "total_duration_h": stats["duration"] / 3600,
            "total_training_load": stats["load"],
            "avg_hrv": round(sum(stats["hrv"]) / len(stats["hrv"]), 1) if stats["hrv"] else None,
            "avg_sleep_h": (sum(stats["sleep"]) / len(stats["sleep"]) / 60) if stats["sleep"] else None,
            "days": sorted(stats["days"]),
        }

        note_path = writer.weekly_note_path(week_key)
        content = writer.build_weekly_note(week_start, week_end, summary)
        await writer.write_note(note_path, content)
        print(f"  ✅ Weekly: {week_key} ({stats['activities']} activities)")

async def sync_monthly_mocs(writer: ObsidianWriter, activities_by_date: dict, daily_records: list, sleep_records: list, days: int):
    """Generate monthly Map of Content (MOC) notes."""
    print("📅 Syncing monthly MOCs...")

    # Collect months involved
    months = set()
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    current = start
    while current <= end:
        months.add(date_str(current)[:7])
        current += timedelta(days=1)

    # Also include months from existing activities
    for date in activities_by_date:
        months.add(date[:7])

    for rec in daily_records:
        months.add(month_from_raw(rec.date))

    for s in sleep_records:
        months.add(month_from_raw(s.date))

    for month in sorted(months):
        # Gather days in this month
        month_days = []
        month_acts = []
        for date, acts in activities_by_date.items():
            if date.startswith(month):
                month_days.append(date)
                month_acts.extend(acts)

        # Gather weeklies touching this month
        weeklies = []
        for i in range(35):  # generous
            check = end - timedelta(days=i)
            if date_str(check).startswith(month):
                iso_year, iso_week, _ = check.isocalendar()
                wk = f"{iso_year}-W{iso_week:02d}"
                if wk not in weeklies:
                    weeklies.append(wk)

        content = writer.build_monthly_moc(month, month_days, month_acts, weeklies)
        note_path = writer.monthly_note_path(month)
        await writer.write_note(note_path, content)
        print(f"  ✅ Monthly MOC: {month} ({len(month_days)} days, {len(month_acts)} activities)")

async def sync_dashboard(writer: ObsidianWriter, activities_by_date: dict, days: int):
    """Update the main dashboard with current month + recent."""
    print("📊 Updating dashboard...")

    # Calculate totals
    total_activities = 0
    total_distance = 0
    total_duration = 0
    total_load = 0
    sport_counts = {}

    for acts in activities_by_date.values():
        for a in acts:
            total_activities += 1
            if a.get("distance_meters"):
                total_distance += a["distance_meters"]
            total_duration += a.get("duration_seconds", 0)
            total_load += a.get("training_load", 0) or 0
            sport = a.get("sport_name", "Unknown")
            sport_counts[sport] = sport_counts.get(sport, 0) + 1

    # Recent activities list (last 7 days)
    recent = []
    end = datetime.utcnow()
    for i in range(7):
        d = date_str(end - timedelta(days=i))
        for a in activities_by_date.get(d, []):
            recent.append({
                "date": d,
                "sport": a["sport_name"],
                "name": a["name"],
                "duration": a.get("duration_seconds", 0),
                "distance": a.get("distance_meters", 0),
            })
    recent.sort(key=lambda x: x["date"], reverse=True)

    current_month = date_str(end)[:7]

    lines = [
        "# 🏋️ Training Dashboard",
        "",
        f"*Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        f"## 📅 Current Month: {writer.monthly_wikilink(current_month)}",
        "",
        "## 📈 Last {} Days Summary".format(days),
        "",
        f"- **Total Activities:** {total_activities}",
        f"- **Total Distance:** {total_distance/1000:.1f} km",
        f"- **Total Duration:** {total_duration/3600:.1f} h",
        f"- **Total Training Load:** {total_load}",
        "",
        "## 🏃 By Sport",
        "",
    ]
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- **{sport}:** {count}x")

    lines.extend([
        "",
        "## 📅 Recent Activities",
        "",
        "| Date | Sport | Name | Duration | Distance |",
        "|------|-------|------|----------|----------|",
    ])
    for r in recent[:20]:
        dur_m = r["duration"] // 60
        dur = f"{dur_m//60}h{dur_m%60}m" if dur_m >= 60 else f"{dur_m}m"
        dist = f"{r['distance']/1000:.2f} km" if r["distance"] else "—"
        lines.append(f"| {r['date']} | {r['sport']} | {r['name']} | {dur} | {dist} |")

    # HR Zones distribution
    hr_zone_totals = {}
    total_zone_mins = 0
    for acts in activities_by_date.values():
        for a in acts:
            for zone, mins in a.get("hr_zones", {}).items():
                hr_zone_totals[zone] = hr_zone_totals.get(zone, 0) + mins
                total_zone_mins += mins

    if hr_zone_totals and total_zone_mins > 0:
        lines.extend([
            "",
            "## 🫀 Heart Rate Zone Distribution",
            "",
            "| Zone | Time | Share |",
            "|------|------|-------|",
        ])
        for zone in sorted(hr_zone_totals.keys(), key=lambda z: int(z[1:]) if z[1:].isdigit() else 99):
            mins = hr_zone_totals[zone]
            pct = mins / total_zone_mins * 100
            hours = int(mins // 60)
            rem_mins = int(mins % 60)
            time_str = f"{hours}h {rem_mins}m" if hours > 0 else f"{rem_mins}m"
            lines.append(f"| **{zone}** | {time_str} | {pct:.1f}% |")
        
        lines.append(f"| *Total* | *{int(total_zone_mins // 60)}h {int(total_zone_mins % 60)}m* | *100%* |")

    lines.extend([
        "",
        "## 🔗 Quick Links",
        "",
        "- [[Training Log/03-Monthly|Monthly Overviews]]",
        "- [[Training Log/02-Weekly|Weekly Summaries]]",
        "- [[Training Log/00-Daily|Daily Logs]]",
        "- [[Training Log/01-Activities|Activity Details]]",
        "",
        "---",
    ])

    await writer.create_folder("Training Log")
    await writer.write_note("Training Log/Dashboard.md", "\n".join(lines))
    print("  ✅ Dashboard updated")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Sync COROS data to Obsidian")
    parser.add_argument("--days", type=int, default=7, help="Number of days to sync (default: 7)")
    parser.add_argument("--full-refresh", action="store_true", help="Sync all history")
    parser.add_argument("--no-sleep", action="store_true", help="Skip sleep data (no mobile token needed)")
    args = parser.parse_args()

    days = args.days if not args.full_refresh else 365

    if not API_KEY:
        print("❌ OBSIDIAN_API_KEY not set in .env")
        sys.exit(1)

    print(f"🚀 Training Sync — {days} days → Obsidian vault '{VAULT}'")

    # 1. Auth with COROS
    auth = await ensure_auth()

    # 2. Connect to Obsidian
    writer = ObsidianWriter(API_KEY, VAULT, API_URL)

    # Verify connection
    try:
        await writer.create_folder("Training Log")
        print("✅ Obsidian REST API connected")
    except Exception as e:
        print(f"❌ Cannot connect to Obsidian Local REST API at {API_URL}")
        print(f"   Make sure the plugin is enabled in Obsidian: Settings → Community Plugins → Local REST API")
        print(f"   Error: {e}")
        sys.exit(1)

    # 3. Sync
    activities_by_date = await sync_daily_notes(writer, auth, days)
    await sync_activity_notes(writer, activities_by_date)

    # Fetch data again for weekly (we need the objects)
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    daily_records = await fetch_daily_records(auth, date_str(start), date_str(end))
    sleep_records = [] if args.no_sleep else await fetch_sleep(auth, date_str(start), date_str(end))

    await sync_weekly_notes(writer, activities_by_date, daily_records, sleep_records, days)
    await sync_monthly_mocs(writer, activities_by_date, daily_records, sleep_records, days)
    await sync_dashboard(writer, activities_by_date, days)

    print("\n🎉 Sync complete!")

if __name__ == "__main__":
    asyncio.run(main())

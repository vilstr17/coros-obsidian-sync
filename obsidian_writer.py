"""
Obsidian vault writer via Local REST API plugin.
Uses monthly folder hierarchy: 00-Daily/YYYY-MM/, 01-Activities/YYYY-MM/
"""

import httpx
import json
import os
from datetime import datetime

class ObsidianWriter:
    def __init__(self, api_key: str, vault: str, base_url: str = "http://127.0.0.1:27123"):
        self.base_url = base_url.rstrip("/")
        self.vault = vault
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self.headers, **kwargs)
            if resp.status_code == 404:
                return {"exists": False, "status": 404}
            resp.raise_for_status()
            try:
                return resp.json()
            except:
                return {"text": resp.text, "status": resp.status_code}

    async def note_exists(self, path: str) -> bool:
        """Check if a note exists."""
        result = await self._request("GET", f"/vault/{path}")
        return result.get("status") != 404

    async def write_note(self, path: str, content: str) -> None:
        """Create or overwrite a note."""
        safe_path = path.replace(" ", "%20").replace("#", "%23")
        headers = {**self.headers, "Content-Type": "text/markdown"}
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.base_url}/vault/{safe_path}"
            resp = await client.put(url, headers=headers, content=content)
            resp.raise_for_status()

    async def append_to_note(self, path: str, content: str) -> None:
        """Append content to end of note."""
        safe_path = path.replace(" ", "%20").replace("#", "%23")
        headers = {**self.headers, "Content-Type": "text/markdown"}
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.base_url}/vault/{safe_path}"
            resp = await client.post(url, headers=headers, content=content)
            resp.raise_for_status()

    async def delete_note(self, path: str) -> None:
        safe_path = path.replace(" ", "%20").replace("#", "%23")
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.base_url}/vault/{safe_path}"
            resp = await client.request("DELETE", url, headers=self.headers)
            resp.raise_for_status()

    async def create_folder(self, path: str) -> None:
        """Create a folder in the vault — no-op, folders auto-created on file write."""
        return

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def month_from_date(date: str) -> str:
        return date[:7]  # 2026-05

    @staticmethod
    def daily_note_path(date: str) -> str:
        month = date[:7]
        return f"Training Log/00-Daily/{month}/{date}.md"

    @staticmethod
    def activity_note_path(date: str, sport_name: str, activity_id: str) -> str:
        month = date[:7]
        filename = f"{date}_{sport_name.replace(' ', '_')}_{activity_id[:8]}"
        return f"Training Log/01-Activities/{month}/{filename}.md"

    @staticmethod
    def weekly_note_path(week_key: str) -> str:
        return f"Training Log/02-Weekly/{week_key}.md"

    @staticmethod
    def monthly_note_path(month: str) -> str:
        return f"Training Log/03-Monthly/{month}.md"

    @staticmethod
    def daily_wikilink(date: str) -> str:
        month = date[:7]
        return f"[[00-Daily/{month}/{date}|{date}]]"

    @staticmethod
    def activity_wikilink(date: str, sport_name: str, activity_id: str) -> str:
        month = date[:7]
        filename = f"{date}_{sport_name.replace(' ', '_')}_{activity_id[:8]}"
        return f"[[01-Activities/{month}/{filename}|Full Activity Details]]"

    @staticmethod
    def weekly_wikilink(week_key: str) -> str:
        return f"[[02-Weekly/{week_key}|{week_key}]]"

    @staticmethod
    def monthly_wikilink(month: str) -> str:
        return f"[[03-Monthly/{month}|{month}]]"

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def build_daily_note(self, date: str, daily: dict, activities: list, sleep: dict = None) -> str:
        """Build markdown content for a daily training log note."""
        lines = [
            f"# {date}",
            "",
            "## 📊 Daily Metrics",
            "",
        ]

        if daily:
            metrics = []
            if daily.get("rhr"): metrics.append(f"- **RHR:** {daily['rhr']} bpm")
            if daily.get("avg_sleep_hrv"): metrics.append(f"- **HRV:** {daily['avg_sleep_hrv']:.1f} ms")
            if daily.get("hrv_baseline"): metrics.append(f"- **HRV Baseline:** {daily['hrv_baseline']:.1f} ms")
            if daily.get("training_load") is not None: metrics.append(f"- **Training Load:** {daily['training_load']}")
            if daily.get("training_load_ratio"): metrics.append(f"- **Load Ratio:** {daily['training_load_ratio']:.2f}")
            if daily.get("vo2max"): metrics.append(f"- **VO2max:** {daily['vo2max']} ml/kg/min")
            if daily.get("stamina_level"): metrics.append(f"- **Stamina:** {daily['stamina_level']:.1f}")
            if daily.get("performance") is not None and daily['performance'] != -1:
                metrics.append(f"- **Performance:** {daily['performance']}")
            if daily.get("distance"): metrics.append(f"- **Distance:** {daily['distance']/1000:.2f} km")
            if daily.get("duration"): metrics.append(f"- **Active Time:** {daily['duration']//3600}h {(daily['duration']%3600)//60}m")
            lines.extend(metrics)
            if not metrics:
                lines.append("- No data available")
        else:
            lines.append("- No data available")

        lines.extend(["", "## 😴 Sleep"])
        if sleep:
            lines.extend([
                "",
                f"- **Total Sleep:** {sleep.get('total_minutes', 0)//60}h {sleep.get('total_minutes', 0)%60}m",
            ])
            if sleep.get("deep_minutes") is not None:
                lines.append(f"- **Deep:** {sleep['deep_minutes']//60}h {sleep['deep_minutes']%60}m")
            if sleep.get("light_minutes") is not None:
                lines.append(f"- **Light:** {sleep['light_minutes']//60}h {sleep['light_minutes']%60}m")
            if sleep.get("rem_minutes") is not None:
                lines.append(f"- **REM:** {sleep['rem_minutes']//60}h {sleep['rem_minutes']%60}m")
            if sleep.get("awake_minutes") is not None:
                lines.append(f"- **Awake:** {sleep['awake_minutes']}m")
            if sleep.get("nap_minutes"):
                lines.append(f"- **Naps:** {sleep['nap_minutes']}m")
            if sleep.get("quality_score") is not None and sleep['quality_score'] != -1:
                lines.append(f"- **Sleep Score:** {sleep['quality_score']}/100")
            if sleep.get("avg_hr"):
                lines.append(f"- **Avg HR during sleep:** {sleep['avg_hr']} bpm")
        else:
            lines.append("- No sleep data")

        lines.extend(["", "## 🏃 Activities"])
        if activities:
            for act in activities:
                duration_m = act.get('duration_seconds', 0) // 60
                dist_km = act.get('distance_meters', 0) / 1000 if act.get('distance_meters') else 0
                lines.extend([
                    "",
                    f"### {act['sport_name']} — {act.get('name', 'Untitled')}",
                    f"- **Duration:** {duration_m//60}h {duration_m%60}m" if duration_m >= 60 else f"- **Duration:** {duration_m}m",
                ])
                if dist_km > 0:
                    lines.append(f"- **Distance:** {dist_km:.2f} km")
                if act.get("avg_hr"):
                    lines.append(f"- **Avg/Max HR:** {act['avg_hr']} / {act['max_hr']} bpm")
                if act.get("avg_power"):
                    lines.append(f"- **Power:** {act['avg_power']}W (NP: {act.get('normalized_power', '—')}W)")
                if act.get("calories"):
                    lines.append(f"- **Calories:** {act['calories']//1000:,} kcal")
                if act.get("elevation_gain"):
                    lines.append(f"- **Elevation:** +{act['elevation_gain']}m / -{act.get('elevation_loss', 0)}m")
                if act.get("training_load") is not None:
                    lines.append(f"- **Training Load:** {act['training_load']}")
                # HR Zones
                if act.get("hr_zones"):
                    lines.extend(["", "**🫀 HR Zones:**"])
                    def _zone_key(item):
                        name = item[0]
                        import re
                        nums = re.findall(r'\d+', name)
                        return int(nums[0]) if nums else 99
                    for zone, mins in sorted(act["hr_zones"].items(), key=_zone_key):
                        lines.append(f"  - {zone}: {mins} min")
                # Link to activity detail
                link = self.activity_wikilink(date, act['sport_name'], act['activity_id'])
                lines.append(f"- 📄 {link}")
        else:
            lines.append("- Rest day — no activities")

        lines.extend(["", "---", "", f"*Synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"])
        return "\n".join(lines)

    def build_activity_note(self, act: dict) -> str:
        """Build detailed note for a single activity."""
        date = act.get("date", "unknown")
        duration_m = act.get("duration_seconds", 0) // 60
        dist_km = act.get("distance_meters", 0) / 1000 if act.get("distance_meters") else 0
        start_dt = datetime.utcfromtimestamp(int(act.get("start_time", 0))).strftime("%H:%M")

        lines = [
            f"# {act.get('sport_name')} — {act.get('name', 'Untitled')}",
            "",
            f"**Date:** {self.daily_wikilink(date)} | **Start:** {start_dt} UTC",
            "",
            "## Summary",
            "",
        ]

        if duration_m > 0:
            lines.append(f"- **Duration:** {duration_m//60}h {duration_m%60}m" if duration_m >= 60 else f"- **Duration:** {duration_m}m")
        if dist_km > 0:
            lines.append(f"- **Distance:** {dist_km:.2f} km")
        if act.get("avg_hr"):
            lines.append(f"- **Avg HR:** {act['avg_hr']} bpm (max: {act['max_hr']} bpm)")
        if act.get("avg_pace"):
            pace = act["avg_pace"]
            lines.append(f"- **Avg Pace:** {pace//60}:{pace%60:02d} /km")
        if act.get("max_pace"):
            pace = act["max_pace"]
            lines.append(f"- **Max Pace:** {pace//60}:{pace%60:02d} /km")
        if act.get("avg_speed"):
            lines.append(f"- **Avg Speed:** {act['avg_speed']:.1f} km/h")
        if act.get("avg_cadence"):
            lines.append(f"- **Avg Cadence:** {act['avg_cadence']} rpm (max: {act.get('max_cadence', '—')})")
        if act.get("avg_power"):
            lines.append(f"- **Power:** {act['avg_power']}W avg, {act.get('normalized_power', '—')}W NP")
        if act.get("calories"):
            lines.append(f"- **Calories:** {act['calories']//1000:,} kcal")
        if act.get("elevation_gain"):
            lines.append(f"- **Elevation:** +{act['elevation_gain']}m / -{act.get('elevation_loss', 0)}m")
        if act.get("training_load") is not None:
            lines.append(f"- **Training Load:** {act['training_load']}")

        # HR Zones (main feature)
        if act.get("hr_zones"):
            lines.extend(["", "## 🫀 Heart Rate Zones"])
            def _zone_key(item):
                name = item[0]
                import re
                nums = re.findall(r'\d+', name)
                return int(nums[0]) if nums else 99
            for zone, mins in sorted(act["hr_zones"].items(), key=_zone_key):
                lines.append(f"- **{zone}:** {mins} min")
            total_zone_mins = sum(act["hr_zones"].values())
            if total_zone_mins > 0:
                lines.append(f"- *Total zone time: {total_zone_mins} min*")

        lines.extend(["", "---", "", f"*Activity ID: {act['activity_id']}*"])
        return "\n".join(lines)

    def build_weekly_note(self, week_start: str, week_end: str, stats: dict) -> str:
        """Build weekly summary note."""
        lines = [
            f"# Week {week_start} – {week_end}",
            "",
            "## 📈 Weekly Stats",
            "",
            f"- **Total Activities:** {stats.get('total_activities', 0)}",
            f"- **Total Distance:** {stats.get('total_distance_km', 0):.1f} km",
            f"- **Total Duration:** {stats.get('total_duration_h', 0):.1f} h",
        ]
        if stats.get("avg_hrv"):
            lines.append(f"- **Avg HRV:** {stats['avg_hrv']:.1f} ms")
        else:
            lines.append("- **Avg HRV:** —")
        if stats.get("avg_sleep_h"):
            lines.append(f"- **Avg Sleep:** {stats['avg_sleep_h']:.1f} h")
        else:
            lines.append("- **Avg Sleep:** —")
        lines.append(f"- **Total Training Load:** {stats.get('total_training_load', 0)}")
        lines.extend([
            "",
            "## 📅 Days",
        ])
        for day in stats.get("days", []):
            lines.append(f"- {self.daily_wikilink(day)}")

        lines.extend(["", "---", "", f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"])
        return "\n".join(lines)

    def build_monthly_moc(self, month: str, days: list[str], activities: list[dict], weeklies: list[str]) -> str:
        """Build a Monthly Map of Content (MOC) note."""
        lines = [
            f"# {month} — Monthly Overview",
            "",
            "## 📅 Daily Logs",
            "",
        ]
        for day in sorted(days):
            lines.append(f"- {self.daily_wikilink(day)}")

        lines.extend(["", "## 🏃 Activities", ""])
        if activities:
            for act in sorted(activities, key=lambda x: x.get("date", "")):
                date = act.get("date", "?")
                sport = act.get("sport_name", "?")
                name = act.get("name", "Untitled")
                link = self.activity_wikilink(date, sport, act.get("activity_id", ""))
                lines.append(f"- {date} — {link} ({sport})")
        else:
            lines.append("- No activities this month")

        lines.extend(["", "## 📊 Weekly Summaries", ""])
        for wk in sorted(weeklies):
            lines.append(f"- {self.weekly_wikilink(wk)}")

        lines.extend(["", "---", "", f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"])
        return "\n".join(lines)

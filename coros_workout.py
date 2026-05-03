"""
COROS Workout Uploader — extension module for training-sync.

Usage:
    from coros_client import get_stored_auth, try_auto_login
    from coros_workout import create_and_schedule, format_workout_summary

    auth = get_stored_auth()
    if not auth:
        auth = await try_auto_login()
    result = await create_and_schedule(auth, "intervalový běh 10x400m Z4/90s", "2026-05-04")
    print(format_workout_summary(result))

Authentication: reuses the same StoredAuth dataclass and token storage
as coros_client.py (no separate login needed).
"""

import json
import time
import re
from typing import Union

import httpx

# ------------------------------------------------------------------
# Import StoredAuth from coros_client.py if available (training-sync)
# Fallback: define inline for standalone usage
# ------------------------------------------------------------------

try:
    from coros_client import StoredAuth
except ImportError:
    from dataclasses import dataclass
    @dataclass
    class StoredAuth:
        access_token: str
        user_id: str
        region: Union[int, str]
        timestamp_ms: int

# ------------------------------------------------------------------
# Config — same as coros_client.py
# ------------------------------------------------------------------

BASE_URLS = {
    1: "https://teamapi.coros.com",      # US
    2: "https://teamcnapi.coros.com",    # CN
    3: "https://teameuapi.coros.com",    # EU
    4: "https://teamsgapi.coros.com",    # SG / Asia
}

ENDPOINTS = {
    "workout_add": "/training/program/add",
    "schedule_update": "/training/schedule/update",
}

# ------------------------------------------------------------------
# Low-level helpers
# ------------------------------------------------------------------

def _base(region) -> str:
    if isinstance(region, int):
        return BASE_URLS.get(region, BASE_URLS[3])
    mapping = {"us": 1, "cn": 2, "eu": 3, "asia": 4, "sg": 4}
    return BASE_URLS.get(mapping.get(str(region).lower(), 3))


def _headers(auth: StoredAuth) -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "accesstoken": auth.access_token,
        "Content-Type": "application/json",
    }


def _user_headers(auth: StoredAuth) -> dict:
    h = _headers(auth)
    h["yfheader"] = json.dumps({
        "timestamp": int(time.time() * 1000),
        "language": "en",
        "userId": auth.user_id,
    })
    return h


def _check(body: dict, context: str) -> None:
    if body.get("result") != "0000":
        msg = body.get("message", "unknown error")
        raise RuntimeError(f"{context}: {msg}")


# ------------------------------------------------------------------
# Natural language parser
# ------------------------------------------------------------------

def parse_natural_workout(text: str) -> tuple[list[dict], int, int]:
    """
    Parse free-text workout description into COROS steps.

    Returns: (steps_list, sport_type, intensity_type)
    """
    text_lower = text.lower()

    # Detect sport
    sport_type = 1  # default: Run
    if any(w in text_lower for w in ["mtb", "mountain bike"]):
        sport_type = 24
    elif any(w in text_lower for w in ["bike", "kolo", "cyklo", "cycling", "road bike"]):
        sport_type = 25
    elif any(w in text_lower for w in ["swim", "plavání"]):
        sport_type = 3
    elif any(w in text_lower for w in ["strength", "posilovna", "gym"]):
        sport_type = 4
    elif any(w in text_lower for w in ["yoga", "jóga"]):
        sport_type = 12

    # Detect intensity type (power vs HR vs RPE)
    intensity_type = 2  # default: HR zone
    if any(w in text_lower for w in ["watt", "w ", "power", "ftp", "sweet spot"]):
        intensity_type = 6  # Power (Watts)
    elif sport_type in (4, 12):  # strength / yoga
        intensity_type = 5  # RPE / no target

    # Very simple keyword-based step extraction
    steps = []
    lines = text.replace(",", "\n").replace(";", "\n").split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()

        # Detect warm-up
        if any(k in line_lower for k in ["warm-up", "rozehřátí", "rozestart", "zahřátí"]):
            dur = _extract_duration(line) or 10
            z = _extract_zone(line) or 1
            lo, hi = hr_zone_bounds(z)
            steps.append({
                "name": "Warm-up",
                "duration_minutes": dur,
                "intensity_low": lo,
                "intensity_high": hi,
            })
            continue

        # Detect cool-down
        if any(k in line_lower for k in ["cool-down", "vychlazení", "cooldown", "zakončení"]):
            dur = _extract_duration(line) or 5
            z = _extract_zone(line) or 1
            lo, hi = hr_zone_bounds(z)
            steps.append({
                "name": "Cool-down",
                "duration_minutes": dur,
                "intensity_low": lo,
                "intensity_high": hi,
            })
            continue

        # Detect repeats like "6×400m" or "8x 1 min"
        repeat = _extract_repeat(line)
        if repeat:
            count, work_dur, rest_dur, zone = repeat
            work_name = "Interval"
            rest_name = "Rest"
            lo, hi = hr_zone_bounds(zone)
            for i in range(1, count + 1):
                steps.append({
                    "name": f"{work_name} {i}",
                    "duration_minutes": work_dur,
                    "intensity_low": lo,
                    "intensity_high": hi,
                })
                if rest_dur > 0:
                    rlo, rhi = hr_zone_bounds(1)  # rest in Z1
                    steps.append({
                        "name": f"{rest_name} {i}",
                        "duration_minutes": rest_dur,
                        "intensity_low": rlo,
                        "intensity_high": rhi,
                    })
            continue

        # Generic block with duration
        dur = _extract_duration(line)
        if dur:
            z = _extract_zone(line) or 2
            lo, hi = hr_zone_bounds(z)
            steps.append({
                "name": _make_short_name(line),
                "duration_minutes": dur,
                "intensity_low": lo,
                "intensity_high": hi,
            })

    # If no steps parsed at all, fall back to single block
    if not steps:
        dur = _extract_duration(text) or 30
        z = _extract_zone(text) or 2
        lo, hi = hr_zone_bounds(z)
        steps = [{"name": "Workout", "duration_minutes": dur, "intensity_low": lo, "intensity_high": hi}]

    # Auto-add warm-up / cool-down if missing and workout looks structured
    _maybe_add_warmup_cooldown(steps)

    return steps, sport_type, intensity_type


def _extract_duration(text: str) -> int | None:
    """Extract minutes from text. Returns int or None."""
    m = re.search(r'(\d+)\s*(?:min|minutes|minut)', text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*(?:h|hod|hours?)', text, re.I)
    if m:
        return int(m.group(1)) * 60
    return None


def _extract_zone(text: str) -> int | None:
    """Extract zone number (1-5) from text."""
    m = re.search(r'z(\d)', text, re.I)
    if m:
        return int(m.group(1))
    # Czech / English keywords
    low = text.lower()
    if any(w in low for w in ["z1", "zone 1", "lehká", "easy", "recovery"]):
        return 1
    if any(w in low for w in ["z2", "zone 2", "aerobní", "endurance"]):
        return 2
    if any(w in low for w in ["z3", "zone 3", "tempo", "threshold"]):
        return 3
    if any(w in low for w in ["z4", "zone 4", "interval", "lactate"]):
        return 4
    if any(w in low for w in ["z5", "zone 5", "vo2max", "sprint", "max"]):
        return 5
    return None


def _extract_repeat(text: str) -> tuple[int, int, int, int] | None:
    """Parse repeats like '6×400m' or '8x 1 min Z4 / 90s rest'.
    Returns (count, work_minutes, rest_minutes, zone) or None."""
    # Pattern: N×X min or N×Xm / Y min rest
    m = re.search(r'(\d+)\s*[×x]\s*(\d+)\s*(?:min|m|minut)', text, re.I)
    if not m:
        return None
    count = int(m.group(1))
    work_dur = int(m.group(2))

    # Try to find rest duration
    rest_m = re.search(r'(\d+)\s*(?:min|s|minut|sek)\s*(?:rest|pauza|odpočinek)', text, re.I)
    rest_dur = 2  # default 2 min rest
    if rest_m:
        rest_val = int(rest_m.group(1))
        rest_dur = rest_val

    zone = _extract_zone(text) or 4
    return count, work_dur, rest_dur, zone


def _make_short_name(text: str) -> str:
    """Generate a short step name from text."""
    if len(text) <= 15:
        return text.strip().title()
    words = text.split()
    return " ".join(words[:3]).title()[:15]


def _maybe_add_warmup_cooldown(steps: list[dict]) -> None:
    """Add default warm-up / cool-down if workout has intervals but no WU/CD."""
    names = " ".join(s["name"].lower() for s in steps)
    has_warmup = any(w in names for w in ["warm", "rozehřátí", "zahřátí"])
    has_cooldown = any(c in names for c in ["cool", "vychlazení", "zakončení"])

    if not has_warmup:
        z1_lo, z1_hi = hr_zone_bounds(1)
        steps.insert(0, {
            "name": "Warm-up",
            "duration_minutes": 10,
            "intensity_low": z1_lo,
            "intensity_high": z1_hi,
        })

    if not has_cooldown:
        z1_lo, z1_hi = hr_zone_bounds(1)
        steps.append({
            "name": "Cool-down",
            "duration_minutes": 5,
            "intensity_low": z1_lo,
            "intensity_high": z1_hi,
        })


def hr_zone_bounds(zone: int) -> tuple[int, int]:
    mapping = {
        1: (120, 135),
        2: (135, 150),
        3: (150, 165),
        4: (165, 180),
        5: (180, 195),
    }
    return mapping.get(zone, (120, 135))


# ------------------------------------------------------------------
# Upload API
# ------------------------------------------------------------------

async def upload_workout(
    auth: StoredAuth,
    name: str,
    steps: list[dict],
    sport_type: int = 1,
    intensity_type: int = 2,
) -> str:
    """Create a structured workout in COROS Training Hub. Returns workout ID."""
    program_steps = []
    for s in steps:
        program_steps.append({
            "name": s["name"],
            "duration": int(s["duration_minutes"] * 60),
            "intensityType": intensity_type,
            "intensityValue": s.get("intensity_low", 0),
            "intensityDisplayUnit": 0,
            "intensityUpperValue": s.get("intensity_high", 0),
            "sportType": sport_type,
        })

    payload = {
        "name": name,
        "sportType": sport_type,
        "planType": 1,
        "duration": 0,
        "programList": [{"stepList": program_steps}],
        "userId": auth.user_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Calculate
        calc_url = f"{_base(auth.region)}/training/program/calculate"
        r = await client.post(calc_url, headers=_user_headers(auth), json=payload)
        _check(r.json(), "workout calculate")
        calc_data = r.json()["data"]

        # 2. Merge and save
        payload["totalSet"] = calc_data.get("totalSet", 0)
        payload["trainingLoad"] = calc_data.get("trainingLoad", 0)
        payload["estimatedTime"] = calc_data.get("estimatedTime", 0)

        add_url = f"{_base(auth.region)}{ENDPOINTS['workout_add']}"
        r = await client.post(add_url, headers=_user_headers(auth), json=payload)
        _check(r.json(), "workout add")
        return str(r.json()["data"]["id"])


async def schedule_workout(auth: StoredAuth, workout_id: str, happen_day: str) -> None:
    """Schedule a saved workout to a calendar day. happen_day: YYYYMMDD."""
    payload = {
        "happenDay": happen_day,
        "planProgramId": workout_id,
        "sortNo": 1,
        "userId": auth.user_id,
    }
    url = f"{_base(auth.region)}{ENDPOINTS['schedule_update']}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_user_headers(auth), json=payload)
        _check(r.json(), "schedule workout")


# ------------------------------------------------------------------
# Convenience: one-shot create + schedule + format reply
# ------------------------------------------------------------------

async def create_and_schedule(
    auth: StoredAuth,
    description: str,
    happen_day: str,  # YYYYMMDD or YYYY-MM-DD
    workout_name: str = "",
) -> dict:
    """Full pipeline: parse → upload → schedule → return summary."""
    steps, sport_type, intensity_type = parse_natural_workout(description)

    # Normalize date
    day = happen_day.replace("-", "")

    # Generate name if not provided
    if not workout_name:
        sport_names = {1: "Run", 24: "MTB", 25: "Bike", 3: "Swim", 4: "Strength", 12: "Yoga"}
        workout_name = f"{sport_names.get(sport_type, 'Workout')} — {len(steps)} steps"

    wid = await upload_workout(auth, workout_name, steps, sport_type, intensity_type)
    await schedule_workout(auth, wid, day)

    return {
        "workout_id": wid,
        "name": workout_name,
        "sport_type": sport_type,
        "steps": steps,
        "scheduled_day": day,
    }


def format_workout_summary(result: dict) -> str:
    """Format a human-readable summary for chat reply."""
    lines = [
        f"✅ **{result['name']}** vytvořen a naplánován",
        f"",
        f"📅 **Datum:** {result['scheduled_day'][:4]}-{result['scheduled_day'][4:6]}-{result['scheduled_day'][6:]}",
        f"🏃 **Sport:** {result['sport_type']} | **Kroků:** {len(result['steps'])}",
        f"",
        f"**Steps:**",
    ]
    for s in result["steps"]:
        target = f"{s['intensity_low']}-{s['intensity_high']} bpm"
        lines.append(f"  • {s['name']}: {s['duration_minutes']} min ({target})")
    lines.append(f"")
    lines.append(f"🔢 **Workout ID:** `{result['workout_id']}`")
    lines.append(f"Hodinky se synchronizují automaticky při připojení k COROS app.")
    return "\n".join(lines)

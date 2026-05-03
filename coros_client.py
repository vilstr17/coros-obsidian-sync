"""
Stripped-down COROS API client for training log sync.
Based on reverse-engineered endpoints from coros-mcp.
"""

import asyncio
import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

ENDPOINTS = {
    "login": "/account/login",
    "dashboard": "/dashboard/query",
    "analyse": "/analyse/query",
    "analyse_detail": "/analyse/dayDetail/query",
    "sleep": "/coros/data/statistic/daily",
    "activity_list": "/activity/query",
    "activity_detail": "/activity/detail/query",
}

BASE_URLS = {
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
    "asia": "https://teamcnapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
}

MOBILE_BASE_URLS = {
    "eu": "https://apieu.coros.com",
    "us": "https://api.coros.com",
    "asia": "https://apicn.coros.com",
    "cn": "https://apicn.coros.com",
}

TOKEN_TTL_MS = 24 * 60 * 60 * 1000  # 24h
_MOBILE_AES_IV = b"weloop3_2015_03#"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class StoredAuth:
    access_token: str
    user_id: str
    region: str
    timestamp: int
    mobile_access_token: Optional[str] = None
    mobile_login_payload: Optional[dict] = None

@dataclass
class Activity:
    activity_id: str
    name: str
    sport_type: int
    sport_name: str
    date: str  # YYYY-MM-DD
    start_time: str  # epoch seconds
    end_time: str
    duration_seconds: int
    distance_meters: Optional[float]
    avg_hr: Optional[int]
    max_hr: Optional[int]
    calories: Optional[int]  # raw cal (divide by 1000 for kcal)
    training_load: Optional[int]
    avg_power: Optional[int]
    normalized_power: Optional[int]
    elevation_gain: Optional[int]
    elevation_loss: Optional[int]
    avg_pace: Optional[int]  # seconds per km
    max_pace: Optional[int]
    avg_speed: Optional[float]
    max_speed: Optional[float]
    avg_cadence: Optional[int]
    max_cadence: Optional[int]
    hr_zones: Optional[dict] = None  # zone minutes
    lap_list: Optional[list] = None

@dataclass
class SleepRecord:
    date: str
    total_minutes: int
    deep_minutes: Optional[int]
    light_minutes: Optional[int]
    rem_minutes: Optional[int]
    awake_minutes: Optional[int]
    nap_minutes: Optional[int]
    avg_hr: Optional[int]
    min_hr: Optional[int]
    max_hr: Optional[int]
    quality_score: Optional[int]

@dataclass
class DailyRecord:
    date: str
    rhr: Optional[int]
    avg_sleep_hrv: Optional[float]
    hrv_baseline: Optional[float]
    training_load: Optional[int]
    training_load_ratio: Optional[float]
    tired_rate: Optional[float]
    ati: Optional[float]
    cti: Optional[float]
    performance: Optional[int]
    distance: Optional[float]
    duration: Optional[int]
    vo2max: Optional[int]
    lthr: Optional[int]
    ltsp: Optional[int]
    stamina_level: Optional[float]
    stamina_level_7d: Optional[float]

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()

def _base_url(region: str) -> str:
    return BASE_URLS.get(region, BASE_URLS["eu"])

def _mobile_base_url(region: str) -> str:
    return MOBILE_BASE_URLS.get(region, MOBILE_BASE_URLS["eu"])

def _check_response(body: dict, context: str) -> None:
    if body.get("result") != "0000":
        raise ValueError(f"Coros {context} error: {body.get('message', 'unknown')}")

def _auth_headers(auth: StoredAuth) -> dict:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "accessToken": auth.access_token,
        "yfheader": json.dumps({"userId": auth.user_id}),
    }

# ---------------------------------------------------------------------------
# Token storage (simple file-based)
# ---------------------------------------------------------------------------

TOKEN_FILE = os.path.expanduser("~/training-sync/.coros_token.json")

def _save_auth(auth: StoredAuth) -> None:
    data = {
        "access_token": auth.access_token,
        "user_id": auth.user_id,
        "region": auth.region,
        "timestamp": auth.timestamp,
        "mobile_access_token": auth.mobile_access_token,
        "mobile_login_payload": auth.mobile_login_payload,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

def _load_auth() -> Optional[StoredAuth]:
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        return StoredAuth(**d)
    except Exception:
        return None

def _is_token_valid(auth: StoredAuth) -> bool:
    now_ms = int(time.time() * 1000)
    return (now_ms - auth.timestamp) < TOKEN_TTL_MS

def get_stored_auth() -> Optional[StoredAuth]:
    auth = _load_auth()
    if auth and _is_token_valid(auth):
        return auth
    return None

# ---------------------------------------------------------------------------
# Mobile API encryption (AES-128-CBC)
# ---------------------------------------------------------------------------

def _mobile_encrypt(plaintext: str, app_key: str) -> str:
    from Crypto.Cipher import AES
    import base64

    key = app_key.encode("ascii")
    data = plaintext.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    pad_len = 16 - (len(xored) % 16)
    padded = xored + bytes([pad_len] * pad_len)
    cipher = AES.new(key, AES.MODE_CBC, _MOBILE_AES_IV)
    return base64.b64encode(cipher.encrypt(padded)).decode("ascii")

async def _mobile_login(email: str, password: str, region: str = "eu") -> tuple[str, dict]:
    mobile_base = _mobile_base_url(region)
    url = mobile_base + "/coros/user/login"
    app_key = str(random.randint(1_000_000_000_000_000, 9_999_999_999_999_999))
    payload = {
        "account": _mobile_encrypt(email, app_key) + "\n",
        "accountType": 2,
        "appKey": app_key,
        "clientType": 1,
        "hasHrCalibrated": 0,
        "kbValidity": 0,
        "pwd": _mobile_encrypt(_md5(password), app_key) + "\n",
        "region": "310|Europe/Berlin|US",
        "skipValidation": False,
    }
    yfheader = json.dumps({
        "appVersion": 1125917087236096,
        "clientType": 1,
        "language": "en-US",
        "mobileName": "sdk_gphone64_arm64,google,Google",
        "releaseType": 1,
        "systemVersion": "13",
        "timezone": 4,
        "versionCode": "404080400",
    }, separators=(",", ":"))
    headers = {
        "content-type": "application/json",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.12.0",
        "request-time": str(int(time.time() * 1000)),
        "yfheader": yfheader,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    _check_response(body, "mobile login")
    token = body.get("data", {}).get("accessToken")
    if not token:
        raise ValueError("No mobile accessToken")
    return token, payload

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def login(email: str, password: str, region: str = "eu") -> StoredAuth:
    pwd_hash = _md5(password)
    login_payload = {"account": email, "accountType": 2, "pwd": pwd_hash}
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _base_url(region) + ENDPOINTS["login"],
            json=login_payload,
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
    _check_response(body, "login")
    data = body.get("data", {})

    # Mobile token (sleep data)
    mobile_token = None
    mobile_payload = None
    try:
        mobile_token, mobile_payload = await _mobile_login(email, password, region)
    except Exception:
        pass

    auth = StoredAuth(
        access_token=data["accessToken"],
        user_id=data["userId"],
        region=region,
        timestamp=int(time.time() * 1000),
        mobile_access_token=mobile_token,
        mobile_login_payload=mobile_payload,
    )
    _save_auth(auth)
    return auth

async def try_auto_login() -> Optional[StoredAuth]:
    email = os.environ.get("COROS_EMAIL")
    password = os.environ.get("COROS_PASSWORD")
    region = os.environ.get("COROS_REGION", "eu")
    if not email or not password:
        return None
    try:
        return await login(email, password, region)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Daily records (HRV, RHR, training load, VO2max)
# ---------------------------------------------------------------------------

def _parse_daily(item: dict) -> DailyRecord:
    return DailyRecord(
        date=str(item.get("happenDay", "")),  # COROS returns YYYYMMDD
        rhr=item.get("rhr"),
        avg_sleep_hrv=item.get("avgSleepHrv"),
        hrv_baseline=item.get("sleepHrvBase"),
        training_load=item.get("trainingLoad"),
        training_load_ratio=item.get("trainingLoadRatio"),
        tired_rate=item.get("tiredRateNew"),
        ati=item.get("ati"),
        cti=item.get("cti"),
        performance=item.get("performance"),
        distance=item.get("distance"),
        duration=item.get("duration"),
        vo2max=item.get("vo2max"),
        lthr=item.get("lthr"),
        ltsp=item.get("ltsp"),
        stamina_level=item.get("staminaLevel"),
        stamina_level_7d=item.get("staminaLevel7d"),
    )

async def fetch_daily_records(auth: StoredAuth, start_day: str, end_day: str) -> list[DailyRecord]:
    """
    Fetch daily metrics (HRV, RHR, training load, VO2max, etc.) for a date range.
    COROS expects dates without dashes (YYYYMMDD).

    Merges data from two endpoints:
    - /analyse/dayDetail/query: supports up to ~24 weeks (no VO2max/fitness)
    - /analyse/query: last ~28 days with VO2max, LTHR, stamina (merged in)
    """
    start_fmt = start_day.replace("-", "")
    end_fmt = end_day.replace("-", "")

    headers = _auth_headers(auth)
    base = _base_url(auth.region)

    async with httpx.AsyncClient(timeout=30) as client:
        detail_resp, analyse_resp = await asyncio.gather(
            client.get(base + ENDPOINTS["analyse_detail"],
                       params={"startDay": start_fmt, "endDay": end_fmt}, headers=headers),
            client.get(base + ENDPOINTS["analyse"], headers=headers),
        )
    detail_resp.raise_for_status()
    analyse_resp.raise_for_status()
    detail_body = detail_resp.json()
    analyse_body = analyse_resp.json()
    _check_response(detail_body, "analyse_detail")

    records = {}
    for item in detail_body.get("data", {}).get("dayList", []):
        rec = _parse_daily(item)
        records[rec.date] = rec

    # Merge VO2max/fitness from t7dayList
    if analyse_body.get("result") == "0000":
        for item in analyse_body.get("data", {}).get("t7dayList", []):
            date = str(item.get("happenDay", ""))
            if date in records:
                rec = records[date]
                rec.vo2max = item.get("vo2max") or rec.vo2max
                rec.lthr = item.get("lthr") or rec.lthr
                rec.ltsp = item.get("ltsp") or rec.ltsp
                rec.stamina_level = item.get("staminaLevel") or rec.stamina_level
                rec.stamina_level_7d = item.get("staminaLevel7d") or rec.stamina_level_7d

    return sorted(records.values(), key=lambda r: r.date)

# ---------------------------------------------------------------------------
# Sleep (mobile API)
# ---------------------------------------------------------------------------

async def fetch_sleep(auth: StoredAuth, start_day: str, end_day: str) -> list[SleepRecord]:
    if not auth.mobile_access_token:
        return []

    mobile_base = _mobile_base_url(auth.region)
    url = mobile_base + ENDPOINTS["sleep"]
    headers = {
        "content-type": "application/json",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.12.0",
        "request-time": str(int(time.time() * 1000)),
        "accesstoken": auth.mobile_access_token,
    }
    params = {
        "startDate": start_day.replace("-", ""),
        "endDate": end_day.replace("-", ""),
        "userId": auth.user_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    if body.get("result") != "0000":
        return []

    records = []
    for item in body.get("data", []):
        records.append(SleepRecord(
            date=item.get("date", ""),
            total_minutes=item.get("sleepTime", 0),
            deep_minutes=item.get("deepSleepTime"),
            light_minutes=item.get("lightSleepTime"),
            rem_minutes=item.get("remSleepTime"),
            awake_minutes=item.get("awakeTime"),
            nap_minutes=item.get("shortSleepTime"),
            avg_hr=item.get("avgHeartRate"),
            min_hr=item.get("minHeartRate"),
            max_hr=item.get("maxHeartRate"),
            quality_score=item.get("sleepQualityScore"),
        ))
    return sorted(records, key=lambda r: r.date)

# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

SPORT_NAMES = {
    100: "Running", 102: "Trail Running", 103: "Track Running", 104: "Hiking",
    200: "Cycling", 201: "Indoor Cycling", 203: "Gravel Bike", 204: "MTB",
    400: "Cardio", 402: "Strength", 403: "Yoga",
    900: "Walking", 9807: "Bike Commute",
}

def _format_date_from_epoch(epoch_str: str) -> str:
    try:
        ts = int(epoch_str)
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")

def _parse_activity(item: dict) -> Activity:
    sport_type = item.get("sportType", 0)
    start_time = str(item["startTime"]) if item.get("startTime") else "0"
    date = _format_date_from_epoch(start_time)

    # Pace/speed from API if available
    avg_pace = item.get("avgPace") or item.get("avgPaceNew")
    max_pace = item.get("maxPace") or item.get("maxPaceNew")
    avg_speed = item.get("avgSpeed")
    max_speed = item.get("maxSpeed")

    # Cadence
    avg_cadence = item.get("avgCadence")
    max_cadence = item.get("maxCadence")

    cal_raw = item.get("calorie")

    return Activity(
        activity_id=str(item.get("labelId", "")),
        name=item.get("name") or item.get("remark") or "Untitled",
        sport_type=sport_type,
        sport_name=SPORT_NAMES.get(sport_type, f"Sport {sport_type}"),
        date=date,
        start_time=start_time,
        end_time=str(item["endTime"]) if item.get("endTime") else start_time,
        duration_seconds=item.get("totalTime", 0) or item.get("duration", 0) or 0,
        distance_meters=item.get("distance") if item.get("distance") is not None else item.get("totalDistance"),
        avg_hr=item.get("avgHr"),
        max_hr=item.get("maxHr"),
        calories=cal_raw,
        training_load=item.get("trainingLoad"),
        avg_power=item.get("avgPower"),
        normalized_power=item.get("np"),
        elevation_gain=item.get("ascent") if item.get("ascent") is not None else item.get("totalAscent"),
        elevation_loss=item.get("descent") if item.get("descent") is not None else item.get("totalDescent"),
        avg_pace=avg_pace,
        max_pace=max_pace,
        avg_speed=avg_speed,
        max_speed=max_speed,
        avg_cadence=avg_cadence,
        max_cadence=max_cadence,
    )

async def fetch_activities(auth: StoredAuth, start_day: str, end_day: str) -> list[Activity]:
    """
    Fetch activity list for a date range.
    COROS expects dates without dashes (YYYYMMDD).
    """
    # COROS activity endpoint needs dates without dashes
    start_fmt = start_day.replace("-", "")
    end_fmt = end_day.replace("-", "")

    params = {
        "startDay": start_fmt,
        "endDay": end_fmt,
        "pageNumber": 1,
        "size": 50,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            _base_url(auth.region) + ENDPOINTS["activity_list"],
            params=params,
            headers=_auth_headers(auth),
        )
        resp.raise_for_status()
        body = resp.json()
    _check_response(body, "activity list")

    data = body.get("data", {})
    items = data.get("dataList", data.get("list", []))
    return [_parse_activity(i) for i in items]

async def fetch_activity_detail(auth: StoredAuth, activity: Activity) -> Activity:
    """Fetch HR zones and lap data for an activity."""
    headers = {k: v for k, v in _auth_headers(auth).items() if k != "Content-Type"}
    url = _base_url(auth.region) + ENDPOINTS["activity_detail"]
    form_data = {
        "labelId": activity.activity_id,
        "userId": auth.user_id,
        "sportType": str(activity.sport_type),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=form_data, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    _check_response(body, "activity detail")
    data = body.get("data", {})

    # HR zones — COROS returns zoneList with type=126 for HR zones
    hr_zones = {}
    for zone_group in data.get("zoneList", []):
        if zone_group.get("type") == 126 or zone_group.get("zoneType") == 0:
            for z in zone_group.get("zoneItemList", []):
                idx = z.get("zoneIndex", 0)
                # COROS zones: 0=Z1, 1=Z2, 2=Z3, 3=Z4, 4=Z5, 5=Z6+
                name = f"Z{idx + 1}"
                seconds = z.get("second", 0)
                mins = round(seconds / 60, 1) if seconds else 0
                hr_zones[name] = mins

    # Lap data
    laps = []
    for lap in data.get("lapList", []):
        laps.append({
            "lap": lap.get("lap", 1),
            "distance_m": lap.get("distance"),
            "duration_s": lap.get("duration"),
            "avg_hr": lap.get("avgHr"),
            "avg_pace": lap.get("avgPace"),
            "avg_power": lap.get("avgPower"),
        })

    activity.hr_zones = hr_zones
    activity.lap_list = laps
    return activity

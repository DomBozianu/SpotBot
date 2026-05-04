import os
import arrow
import requests_cache
import openmeteo_requests
from retry_requests import retry
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pathlib import Path
import json
from datetime import datetime

# Environment & AI Setup
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_KEY"),
    timeout=30.0,
    max_retries=2
)

# Open-Meteo Setup
cache_session = requests_cache.CachedSession('.cache', expire_after=3600)
retry_session = retry(cache_session, retries=5)
openmeteo = openmeteo_requests.Client(session=retry_session)

def load_spots():
    # spots.json lives inside the knowledge base folder for tidiness
    spots_path = Path(__file__).parent / "spotbot_knowledge" / "spots.json"
    if spots_path.exists():
        with open(spots_path, "r") as f:
            return json.load(f)
    return {}

SPOTS = load_spots()

async def fetch_all_data(lat, lon, tide_id):
    """Fetch weather, marine, and tides in parallel to save time."""
    import asyncio
    
    # Create the tasks but don't 'await' them yet
    weather_task = asyncio.to_thread(fetch_spot_data, lat, lon)
    tide_task = asyncio.to_thread(fetch_tide_data, tide_id)
    
    # Run them simultaneously
    (weather_res, marine_res), tides = await asyncio.gather(weather_task, tide_task)
    return weather_res, marine_res, tides

def calculate_gear(weight_kg, wind_speed_kts, skill_level="intermediate"):
    """
    Calculates recommended windsurf gear based on physics and skill level.
    """
    # 1. Base Sail Calculation (Baseline: 75kg rider)
    # Mapping wind speed ranges to baseline sail sizes
    if wind_speed_kts < 12:
        base_sail = 8.0
    elif 12 <= wind_speed_kts < 16:
        base_sail = 7.0
    elif 16 <= wind_speed_kts < 20:
        base_sail = 6.0
    elif 20 <= wind_speed_kts < 25:
        base_sail = 5.0
    elif 25 <= wind_speed_kts < 30:
        base_sail = 4.2
    else:
        base_sail = 3.7

    # Adjust for rider weight (+/- 0.5m per 10kg difference from 75kg)
    weight_diff = (weight_kg - 75) / 10
    recommended_sail = round(base_sail + (weight_diff * 0.5), 1)

    # 2. Board Volume Calculation
    # Static weights: Board (~7kg) + Rig (~10kg) + Wetsuit/Harness (~3kg)
    static_load = 20 
    displacement_volume = weight_kg + static_load

    # Skill-based reserve buoyancy
    reserve_map = {
        "beginner": 100,      # High stability
        "intermediate": 40,   # Comfortable uphauling
        "advanced": 15        # Minimum to keep rig afloat
    }
    
    reserve = reserve_map.get(skill_level, 40)
    recommended_volume = int(displacement_volume + reserve)

    # 3. Sinker Detection Logic
    is_sinker = recommended_volume <= (weight_kg + static_load)
    
    return {
        "sail_size_m2": recommended_sail,
        "board_volume_l": recommended_volume,
        "is_sinker": is_sinker,
        "logic_used": f"Base {base_sail}m sail adjusted for {weight_kg}kg; {reserve}L reserve for {skill_level}."
    }

def calculate_wave_power(height, period):
    """
    Calculates wave power in kW/m and returns both the value and a description.
    Formula: P ≈ 0.5 * H² * T
    """
    try:
        h = float(height)
        t = float(period)
        power_val = round(0.5 * (h ** 2) * t, 1)
    except (ValueError, TypeError):
        return 0, "Flat"

    if power_val < 10:
        wave_desc = "Weak"
    elif 10 <= power_val <= 40:
        wave_desc = "Clean"
    else:
        wave_desc = "Heavy"
        
    return power_val, wave_desc

def calculate_steepness(height, period):
    """Calculates wave steepness. Higher = punchier/hollower."""
    if period == 0: return "Flat"
    # Simplified steepness index
    steepness = height / (period ** 2)
    if steepness > 0.025: return "Hollow/Steep"
    if steepness > 0.015: return "Average"
    return "Rolling/Fat"

def get_beaufort(knots):
    if knots < 1:  return {"f": 0, "name": "Calm", "desc": "Mirror flat"}
    if knots < 4:  return {"f": 1, "name": "Light Air", "desc": "Ripples"}
    if knots < 7:  return {"f": 2, "name": "Light Breeze", "desc": "Small wavelets"}
    if knots < 11: return {"f": 3, "name": "Gentle Breeze", "desc": "Large wavelets"}
    if knots < 17: return {"f": 4, "name": "Moderate Breeze", "desc": "Small waves"}
    if knots < 22: return {"f": 5, "name": "Fresh Breeze", "desc": "Many whitecaps"}
    if knots < 28: return {"f": 6, "name": "Strong Breeze", "desc": "Large waves, spray"}
    if knots < 34: return {"f": 7, "name": "Near Gale", "desc": "Sea heaps up"}
    if knots < 41: return {"f": 8, "name": "Gale", "desc": "High waves, breaking crests"}
    if knots < 48: return {"f": 9, "name": "Strong Gale", "desc": "Visibility affected"}
    if knots < 56: return {"f": 10, "name": "Storm", "desc": "Trees uprooted on land!"}
    if knots < 64: return {"f": 11, "name": "Violent Storm", "desc": "Widespread damage"}
    return {"f": 12, "name": "Hurricane", "desc": "Absolute devastation. Don't."}

def get_relative_wind(wind_deg, shoreline_deg):
    if shoreline_deg is None: return "Unknown"
    diff = abs(wind_deg - shoreline_deg) % 360
    if diff > 180: diff = 360 - diff
    if diff < 30: return "🚫 Onshore"
    if diff > 150: return "🚩 Offshore"
    if 60 < diff < 120: return "💎 Cross-shore"
    return "📈 Cross-on/off"

def get_compass_info(degrees):
    val = int((degrees / 22.5) + 0.5)
    directions = [
        {"word": "North", "arrow": "⬇️"}, {"word": "N-East", "arrow": "↙️"},
        {"word": "East", "arrow": "⬅️"}, {"word": "S-East", "arrow": "↖️"},
        {"word": "South", "arrow": "⬆️"}, {"word": "S-West", "arrow": "↗️"},
        {"word": "West", "arrow": "➡️"}, {"word": "N-West", "arrow": "↘️"}
    ]
    index = (val // 2) % 8 
    return directions[index]

def get_weather_desc(code):
    wmo_codes = {
        0: "Clear skies", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle", 
        61: "Slight rain", 63: "Moderate rain", 71: "Slight snow", 80: "Rain showers", 
        95: "Thunderstorms"
    }
    # Convert to int to handle float codes like 53.0
    code_int = int(code) if code is not None else 0
    return wmo_codes.get(code_int, f"Weather code {code_int}")

def get_sendiness_score(wind_knots, wind_relative):
    """
    Calculates a 1-10 'Sendiness' score based on wind speed and direction quality.
    This is the go/no-go signal — higher = more sendy.
    Wind direction multiplier: Cross-shore is ideal, Offshore is dangerous, Onshore is meh.
    """
    # Base score from wind speed
    if wind_knots < 8:    base = 1
    elif wind_knots < 12: base = 3
    elif wind_knots < 17: base = 5
    elif wind_knots < 22: base = 6
    elif wind_knots < 28: base = 8
    elif wind_knots < 36: base = 9
    else:                 base = 10

    # Direction quality modifier
    if "Cross-shore" in wind_relative:   modifier = 1    # Perfect
    elif "Cross-on" in wind_relative:    modifier = 0    # Decent
    elif "Onshore" in wind_relative:     modifier = -1   # Choppy but rideable
    elif "Offshore" in wind_relative:    modifier = -2   # Dangerous
    else:                                modifier = 0

    score = max(1, min(10, base + modifier))

    # Label for the UI badge
    if score <= 3:   label = "Stay Home"
    elif score <= 5: label = "Marginal"
    elif score <= 7: label = "Good Session"
    elif score <= 9: label = "Send It"
    else:            label = "NUKING 🔥"

    return score, label


def get_best_session_window(trend_12h):
    """
    Scans the 12-hour forecast and finds the best consecutive 3-hour block.
    'Best' = highest average wind speed that stays below gale force (34kts).
    Returns the start hour string and average speed, or None if no good window exists.
    """
    if len(trend_12h) < 3:
        return None

    best_avg = 0
    best_start = None

    for i in range(len(trend_12h) - 2):
        window = trend_12h[i:i+3]
        speeds = [h['speed'] for h in window]
        avg = sum(speeds) / 3

        # Sweet spot: above 15kts (planing) and below 34kts (gale)
        if 15 <= avg <= 34 and avg > best_avg:
            best_avg = avg
            best_start = window[0]['hour']

    if best_start is None:
        return None

    return {"start": best_start, "avg_knots": round(best_avg, 1)}


def get_wetsuit_rec(water_temp_c):
    """
    Returns a wetsuit recommendation string based on water temperature.
    Thresholds match the gear knowledge base (windsurf_chart.txt Section 5).
    """
    if water_temp_c >= 19:   return "2mm Shorty"
    elif water_temp_c >= 14: return "3/2mm Full Suit"
    elif water_temp_c >= 10: return "4/3mm + Booties"
    else:                    return "5/4mm Hooded + Gloves + Booties"


def fetch_spot_data(lat, lon):
    weather_params = {
        "latitude": lat, "longitude": lon,
        "current": [
            "weather_code", "wind_speed_10m", "wind_direction_10m",
            "wind_gusts_10m", "apparent_temperature", "cloud_cover", "visibility"
            ],
        "hourly": ["wind_speed_10m", "wind_gusts_10m", "weather_code"],
        "daily": ["sunrise", "sunset"],
        "wind_speed_unit": "kn", "timezone": "auto"
    }
    marine_params = {
        "latitude": lat, "longitude": lon,
        "current": ["wave_height", "wave_period"],
        "hourly": ["sea_surface_temperature"],
        "timezone": "auto",
        "length_unit": "metric"
    }
    weather_res = openmeteo.weather_api("https://api.open-meteo.com/v1/forecast", params=weather_params)[0]
    marine_res = openmeteo.weather_api("https://marine-api.open-meteo.com/v1/marine", params=marine_params)[0]
    return weather_res, marine_res

def fetch_tide_data(station_id):
    # FIX: Use the same key name as add_spot.py
    api_key = os.getenv("ADMIRALTY_API_KEY") or os.getenv("ADMIRALTY_KEY")
    if not api_key: return []
    
    # Request 48 hours (duration=2) to ensure we always have a 'next' tide
    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents?duration=2"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    try:
        response = cache_session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Tide API Error: {e}")
        return []
    return []

def process_tides(tides, tz_name, now_local):
    """Handles the Rule of Twelfths and Springs/Neaps logic."""
    tide_list = []
    next_tide_obj = None
    tidal_flow = "Low"
    tide_display = "Stable"
    next_tide_info = "Data Unavailable"
    
    if not tides:
        return tide_list, next_tide_info, tide_display, tidal_flow, "Unknown"

    all_heights = [event['Height'] for event in tides]
    weekly_max_range = max(all_heights) - min(all_heights)

    for event in tides:
        event_time = arrow.get(event['DateTime']).to(tz_name)
        t_data = {
            "date": event_time.format('ddd, MMM DD'),
            "time": event_time.format('HH:mm'),
            "type": "High Tide" if "High" in event['EventType'] else "Low Tide",
            "height": round(event['Height'], 1),
        }
        tide_list.append(t_data)

        if not next_tide_obj and event_time > now_local:
            next_tide_obj = t_data
            diff = event_time - now_local
            hours_until = diff.total_seconds() / 3600
            
            # Rule of Twelfths
            if 2.0 <= hours_until <= 4.0: tidal_flow = "Strong"
            elif hours_until < 1.0 or hours_until > 5.0: tidal_flow = "Slack (Weak)"
            else: tidal_flow = "Moderate"

            if "High" in t_data['type']:
                tide_display = "📈 Rising"
                next_tide_info = f"High @ {t_data['time']} ({int(hours_until)}h remaining)"
            else:
                tide_display = "📉 Falling"
                next_tide_info = f"Low @ {t_data['time']} ({int(hours_until)}h remaining)"

    # Springs/Neaps logic
    tide_phase = "Mid-Cycle"
    if len(tide_list) >= 4:
        today_range = max(all_heights[:4]) - min(all_heights[:4])
        ratio = today_range / weekly_max_range if weekly_max_range > 0 else 0.5
        if ratio > 0.85: tide_phase = "Springs"
        elif ratio < 0.60: tide_phase = "Neaps"

    return tide_list, next_tide_info, tide_display, tidal_flow, tide_phase

def process_forecast(hourly, current_hour_idx, tz_name):
    """Generates the 12-hour trend list."""
    all_speeds = hourly.Variables(0).ValuesAsNumpy()
    all_gusts = hourly.Variables(1).ValuesAsNumpy()
    all_codes = hourly.Variables(2).ValuesAsNumpy()
    
    trend_12h = []
    for i in range(12):
        idx = int(current_hour_idx + i)
        trend_12h.append({
            "hour": arrow.now(tz_name).shift(hours=i).format("HH:00"),
            "speed": float(round(all_speeds[idx], 1)),
            "gust": float(round(all_gusts[idx], 1)),
            "code": int(all_codes[idx])
        })
    return trend_12h

def process_marine_data(marine, current_hour_idx):
    """Calculates all wave-related physics and water temp."""
    m_curr = marine.Current()
    wave_h = m_curr.Variables(0).Value()
    wave_p = m_curr.Variables(1).Value()
    
    # Existing physics functions
    wave_steepness = calculate_steepness(wave_h, wave_p)
    wave_pwr_val, wave_pwr_desc = calculate_wave_power(wave_h, wave_p)
    
    # Water temp from hourly array
    m_hourly = marine.Hourly()
    w_temp_arr = m_hourly.Variables(0).ValuesAsNumpy()
    w_temp = float(w_temp_arr[min(current_hour_idx, len(w_temp_arr) - 1)])
    
    return {
        "height": float(round(wave_h, 1)),
        "period": float(round(wave_p, 1)),
        "steepness": wave_steepness,
        "power_val": wave_pwr_val,
        "power_desc": wave_pwr_desc,
        "temp": int(round(w_temp))
    }

def get_demo_report():
    """Returns the hardcoded 'Nuking' report for demos."""
    # Move your existing 'demo_epic' dictionary here
    demo_wind = 26.5
    demo_relative = "💎 Cross-shore"
    demo_score, demo_label = get_sendiness_score(demo_wind, demo_relative)
    demo_session = {"start": "14:00", "avg_knots": 24.5}
    demo_wetsuit = get_wetsuit_rec(12)
    
    # Mock 12h forecast for demo charts - more realistic wind pattern
    demo_forecast = []
    # Realistic afternoon wind pattern: builds, peaks, then drops
    base_speeds = [24, 26, 28, 27, 25, 23, 21, 19, 17, 16, 15, 14]
    for i, speed in enumerate(base_speeds):
        demo_forecast.append({
            "hour": f"{(14 + i) % 24:02d}:00",
            "speed": float(speed + (i % 3 - 1) * 0.5),  # Add slight variation
            "gust": float(speed + 5 + (i % 2)),  # Variable gust strength
            "code": 1 if i < 8 else 2  # Clear then partly cloudy
        })
    
    return {
        "metadata": {
            "spot_name": "🌟 DEMO: Epic Peak",
            "status": "Nuking!",
            "date": "Friday Demo",
            "last_updated": "Live",
            "sunrise": "06:00",
            "sunset": "20:30",
            "tide_list": [
                {"date": "Fri, May 02", "time": "16:30", "type": "High Tide", "height": 4.2},
                {"date": "Fri, May 02", "time": "22:45", "type": "Low Tide", "height": 0.8},
                {"date": "Sat, May 03", "time": "04:15", "type": "High Tide", "height": 4.1},
                {"date": "Sat, May 03", "time": "10:30", "type": "Low Tide", "height": 0.9},
                {"date": "Sat, May 03", "time": "16:45", "type": "High Tide", "height": 4.0},
                {"date": "Sat, May 03", "time": "23:00", "type": "Low Tide", "height": 1.0}
            ]
        },
        "live": {
            "wind_knots": demo_wind,
            "wind_color": "sweet",
            "beaufort_f": 6,
            "beaufort_name": "Strong Breeze",
            "beaufort_desc": "Large branches in motion; whistling in wires.",
            "wind_dir_name": "S-West",
            "wind_dir": 225,
            "wind_arrow": "↗️",
            "wind_relative": demo_relative,
            "wind_trend": "Building",
            "gusts_knots": 34.0,
            "waves_m": 2.5,
            "wave_period": 11.0,
            "wave_power": 34.4,
            "wave_power_desc": "Clean",
            "tide_display": "📈 Rising",
            "next_tide_info": "High @ 16:30 (2h remaining)",
            "tide_phase": "Springs",
            "tidal_flow": "Strong",
            "sun_status": "✨ Golden Hour!",
            "water_temp": 12,
            "wetsuit_rec": demo_wetsuit,
            "sendiness_score": demo_score,
            "sendiness_label": demo_label,
            "best_session": demo_session,
            "wave_steepness": "Hollow/Steep",
            "air_temp": 14,
            "cloud_cover": 20,
            "visibility": 15.0,
            "recommended_gear": {
                "sail_size_m2": 4.2, 
                "board_volume_l": 84, 
                "is_sinker": True
            },
        },
        "forecast_12h": demo_forecast,
        "local_knowledge": "Perfect cross-shore conditions. Watch the sandbar at low tide."
    }

async def get_shred_report(spot_key: str, user_weight: str = "75", level="intermediate"):
    spot = SPOTS.get(spot_key)
    if not spot: return None

    if spot_key == "demo_epic":
        return get_demo_report()

    # 2. Parallel Fetching (The Optimization)
    weather, marine, tides = await fetch_all_data(spot['lat'], spot['lon'], spot['tide_id'])

    # 2. Time Setup
    raw_tz = weather.Timezone()
    tz_name = raw_tz.decode('utf-8') if isinstance(raw_tz, bytes) else raw_tz or 'Europe/London'
    now_local = arrow.now(tz_name)
    current_hour_idx = now_local.hour
    
    # Define these for the metadata return
    today_date = now_local.format('ddd, MMM DD')
    last_updated = now_local.format('HH:mm')

    # 3. Extract Current Weather
    current = weather.Current()
    wind_spd = current.Variables(1).Value()
    wind_deg = current.Variables(2).Value()
    gust_spd = current.Variables(3).Value()
    app_temp = current.Variables(4).Value()
    clouds = current.Variables(5).Value()
    visibility_km = round(current.Variables(6).Value() / 1000, 1)
    
    # 3. Process Sub-Modules (The Optimizations)
    # This keeps the main function under 50 lines
    tide_list, next_info, t_disp, t_flow, t_phase = process_tides(tides, tz_name, now_local)
    marine_data = process_marine_data(marine, current_hour_idx)
    trend_12h = process_forecast(weather.Hourly(), current_hour_idx, tz_name)

    # 5. Physics & Logic
    weight_int = int(user_weight) if user_weight.isdigit() else 75
    gear = calculate_gear(weight_int, wind_spd, level)
    rel_wind = get_relative_wind(wind_deg, spot.get('shoreline_bearing'))
    sendiness_score, sendiness_label = get_sendiness_score(wind_spd, rel_wind)
    beaufort = get_beaufort(wind_spd)
    best_session = get_best_session_window(trend_12h)

    # Wind trend (Simplified comparison)
    all_speeds = weather.Hourly().Variables(0).ValuesAsNumpy()
    future_wind = all_speeds[min(current_hour_idx + 3, len(all_speeds)-1)] 
    if future_wind > wind_spd + 2: wind_trend = "Building"
    elif future_wind < wind_spd - 2: wind_trend = "Dropping"
    else: wind_trend = "Steady"
    
    # Wind colour drives the UI badge colour
    if wind_spd < 13: wind_color = "light"
    elif wind_spd < 19: wind_color = "green"
    elif wind_spd < 26: wind_color = "sweet"
    elif wind_spd < 36: wind_color = "heavy"
    else: wind_color = "nuke"

    # 6. Sun Logic
    daily = weather.Daily()
    sunrise = arrow.get(int(daily.Variables(0).ValuesInt64AsNumpy()[0])).to(tz_name).format("HH:mm")
    sunset = arrow.get(int(daily.Variables(1).ValuesInt64AsNumpy()[0])).to(tz_name).format("HH:mm")
    
    # Check if Golden Hour (within 1 hour of sunset)
    sunset_time = arrow.get(sunset, "HH:mm").replace(year=now_local.year, month=now_local.month, day=now_local.day)
    if now_local > sunset_time:
        sun_status = "Sun has set"
    else:
        diff = (sunset_time - now_local).total_seconds() / 3600
        sun_status = "✨ Golden Hour!" if diff < 1 else f"{int(diff)}h left" 
    rel_wind = get_relative_wind(wind_deg, spot.get('shoreline_bearing'))
    dir_info = get_compass_info(wind_deg)

    # --- 7. Local Knowledge ---
    wisdom = "No local knowledge found."
    path = Path(__file__).parent / "spotbot_knowledge" / "spots" / spot['knowledge_file']
    if path.exists():
        wisdom = path.read_text(encoding='utf-8')[:500]

    return {
        "metadata": {
            "spot_name": spot['name'],
            "status": get_weather_desc(current.Variables(0).Value()),
            "date": today_date,
            "last_updated": last_updated,
            "sunrise": sunrise,
            "sunset": sunset,
            "tide_list": tide_list
        },
        "live": {
            "wind_knots": float(round(wind_spd, 1)),
            "wind_color": wind_color,
            "recommended_gear": gear,  # <--- NEW: Passing the Python-calculated gear
            "beaufort_f": beaufort['f'],
            "beaufort_name": beaufort['name'],
            "beaufort_desc": beaufort['desc'],
            "wind_dir_name": dir_info['word'],
            "wind_dir": int(wind_deg),
            "wind_arrow": dir_info['arrow'],
            "wind_relative": rel_wind,
            "wind_trend": wind_trend,
            "gusts_knots": float(round(gust_spd, 1)),
            "waves_m": marine_data['height'],
            "wave_period": marine_data['period'],
            "wave_steepness": marine_data['steepness'],
            "wave_power": marine_data['power_val'],
            "wave_power_desc": marine_data['power_desc'],
            "air_temp": int(round(app_temp)),
            "cloud_cover": int(clouds),
            "visibility": visibility_km,
            "tide_display": t_disp,
            "next_tide_info": next_info,
            "tide_phase": t_phase,
            "tidal_flow": t_flow,
            "sun_status": sun_status,
            "water_temp": marine_data['temp'],
            "wetsuit_rec": get_wetsuit_rec(marine_data['temp']),
            "sendiness_score": sendiness_score,
            "sendiness_label": sendiness_label,
            "best_session": best_session,
        },
        "forecast_12h": trend_12h,
        "local_knowledge": wisdom
    }
async def get_ai_recommendation(report, user_weight, spot_key, user_level):
    try:
        base_path = Path(__file__).parent / "spotbot_knowledge"
        fundamentals = (base_path / "general" / "windsurfing_fundamentals.txt").read_text(encoding='utf-8')[:8000] if (base_path / "windsurfing_fundamentals.txt").exists() else ""
        spot_kb = (base_path / "spots" / f"{spot_key}.txt").read_text(encoding='utf-8')[:4000] if (base_path / "spots" / f"{spot_key}.txt").exists() else ""

        live = report.get('live', {})
        meta = report.get('metadata', {})
        gear = live.get('recommended_gear', {})
        
        # Comprehensive Data Feed
        prompt = f"""You are the Local Windsurf Legend. Talk like a seasoned pro—salty, punchy, and direct. 
No introductory fluff. No repeating instructions. 

### EXPERT KNOWLEDGE (FUNDAMENTALS):
{fundamentals}

### SPOT NUANCES:
{spot_kb}

### THE LIVE DATA:
- Rider: {user_level} ({user_weight}kg) at {meta.get('spot_name')}
- Wind: {live.get('wind_knots')}kts (Gusts: {live.get('gusts_knots')}kts) | {live.get('wind_relative')} | {live.get('wind_trend')}
- Sea: {live.get('waves_m')}m | {live.get('wave_steepness')} | {live.get('wave_power')} kW/m ({live.get('wave_power_desc')})
- Environment: {live.get('water_temp')}°C Water | {live.get('sun_status')} | {live.get('visibility')}km visibility | {live.get('cloud_cover')}% Clouds
- Tide: {live.get('tide_display')} | Flow: {live.get('tidal_flow')} | {live.get('next_tide_info')}
- Engine Recommendation: {gear.get('sail_size_m2')}m / {gear.get('board_volume_l')}L
- Calculated Sendiness: {live.get('sendiness_score')}/10

### YOUR MISSION:
1. DATA INTEGRITY: You must use the 'Calculated Sendiness' provided ({live.get('sendiness_score')}/10). Do not invent your own score.
2. THE PERSPECTIVE SHIFT: 
   - If Sendiness < 4: Your Vibe must be written from the perspective of someone standing ON THE BEACH. Do not use phrases like "you'll be survival sailing" or "hang on." Instead, explain why the conditions (e.g., onshore wind + light breeze) make it a "washout" or "SUP-only" day.
   - If Sendiness >= 4: Write from the perspective of someone ON THE WATER giving active tactical advice.
3. THE GEAR LOGIC: If Sendiness is < 4, output 'GEAR: N/A (Go to the Pub)'. Do not suggest sail sizes.
4. WIND VS TIDE: If 'Flow' and 'Wind Relative' are in the same direction, explain that the tide will "rob" the sail of its power, making it feel 5 knots lighter than it is.
5. SAFETY: Prioritize warnings for 'Golden Hour' (fading light) or cold water (<12°C).

RESPONSE FORMAT (STRICT):
SENDINESS: [Use Calculated Sendiness]
GEAR: [Exact Engine Gear OR "N/A (Go to the Pub)"]
THE VIBE: [One paragraph, max 4 sentences. Match your perspective to the Sendiness score.]
RESPONSE FORMAT (STRICT):
SENDINESS: [Use Calculated Sendiness]
GEAR: [Exact Engine Gear OR "N/A (Go to the Pub)"]
THE VIBE: [One paragraph, max 4 sentences. Be the salty expert who knows the physics of this specific water.]
"""

        # Using 0.3 to keep the Legend grounded in your Python math
        response = await client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3 
        )
        return response.choices[0].message.content

    except Exception as e:
        return f"The Legend is checking the rig: {str(e)}"
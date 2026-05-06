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
import numpy as np
import math
import tempfile

# Environment & AI Setup
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    timeout=30.0,
    max_retries=2
)

# Create the path in the /tmp directory for Cloud Run compatibility
cache_path = os.path.join(tempfile.gettempdir(), "spotbot_cache")
cache_session = requests_cache.CachedSession(cache_path, expire_after=3600)
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

def calculate_gear(weight_kg, wind_speed_kts, skill_level="intermediate", discipline="freeride"):
    """Calculates gear with rig-weight adjusted sinker logic."""
    
    # 1. Base Sail Step-Ladder
    if wind_speed_kts < 12: base_sail = 8.0
    elif 12 <= wind_speed_kts < 16: base_sail = 7.0
    elif 16 <= wind_speed_kts < 20: base_sail = 6.0
    elif 20 <= wind_speed_kts < 25: base_sail = 5.0
    elif 25 <= wind_speed_kts < 30: base_sail = 4.2
    else: base_sail = 3.7

    # 2. Weight Adjustment (+/- 0.5m per 10kg from 75kg)
    weight_diff = (weight_kg - 75) / 10
    recommended_sail = base_sail + (weight_diff * 0.5)

    discipline = discipline.lower()

    # 3. Discipline Offsets
    discipline_offsets = {
        "wave": -0.5, 
        "freestyle": -0.4, 
        "freeride": 0.0
    }
    recommended_sail += discipline_offsets.get(discipline, 0)
    final_sail = round(max(3.0, min(10.0, recommended_sail)), 1)

    # 4. Board Volume Logic (Mapped to your 3 HTML options)
    # Beginner: Needs to uphaul (Weight + 100)
    # Intermediate: Needs a safety margin (Weight + 40)
    # Advanced: Only needs enough to plane (Weight + 15)
    reserve_map = {
        "novice": 100, 
        "intermediate": 40, 
        "advanced": 15
    }
    # 4. Board Volume Logic (Wind-Adjusted)
    # Start with the base safety margin from the skill level
    reserve = reserve_map.get(skill_level, 40)
    
    # --- HIGH WIND TAX ---
    # For every 5 knots above 20kts, drop the volume by 5L to maintain control.
    # On a 30kt day, this reduces the board size by 10L.
    if wind_speed_kts > 20:
        wind_reduction = ((wind_speed_kts - 20) / 5) * 5
        reserve -= wind_reduction

    # Combine body weight with the wind-adjusted reserve
    recommended_volume = int(weight_kg + reserve)
    
    # Final discipline adjustment: Wave/Freestyle boards are naturally lower volume
    if discipline in ["wave", "freestyle"]:
        recommended_volume -= 10

    # 5. Sinker Logic (Accounting for ~15kg of Rig, Wetsuit, and Water)
    # If volume is less than (Body Weight + 15kg rig), it's a sinker.
    rig_weight_buffer = 15 
    is_sinker = recommended_volume < (weight_kg + rig_weight_buffer)

    return {
        "sail": f"{final_sail}m²",
        "board": f"{recommended_volume}L",
        "type": discipline,
        "is_sinker": is_sinker
    }

def determine_discipline(user_choice, wave_height, wind_speed):
    if user_choice != "auto":
        return user_choice
    
    if wave_height > 1.2 and wind_speed > 18:
        return "wave"
    elif wind_speed > 15 and wave_height < 0.6:
        return "freestyle"
    return "freeride"

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
    # Lookup table: (Max Knots, Beaufort Scale, Name, Description)
    BEAUFORT_TABLE = [
        (1, 0, "Calm", "Mirror flat"),
        (4, 1, "Light Air", "Ripples"),
        (7, 2, "Light Breeze", "Small wavelets"),
        (11, 3, "Gentle Breeze", "Large wavelets"),
        (17, 4, "Moderate Breeze", "Small waves"),
        (22, 5, "Fresh Breeze", "Many whitecaps"),
        (28, 6, "Strong Breeze", "Large waves, spray"),
        (34, 7, "Near Gale", "Sea heaps up"),
        (41, 8, "Gale", "High waves, breaking crests"),
        (48, 9, "Strong Gale", "Visibility affected"),
        (56, 10, "Storm", "Trees uprooted"),
        (64, 11, "Violent Storm", "Widespread damage"),
        (float('inf'), 12, "Hurricane", "Absolute devastation. Don't.") # dont what
    ]
    for limit, f, name, desc in BEAUFORT_TABLE:
        if knots < limit:
            return {"f": f, "name": name, "desc": desc}

def get_wind_color(knots):
    """Maps wind speed to a UI color badge name."""
    color_thresholds = [
        (13, "light"), 
        (19, "green"), 
        (26, "sweet"), 
        (36, "heavy"), 
        (float('inf'), "nuke")
    ]
    return next(color for limit, color in color_thresholds if knots < limit)

def get_relative_wind(wind_deg, shoreline_bearing):
    if shoreline_bearing is None: return "Unknown"
    
    # Calculate the shortest difference between wind and shore normal
    # 0 = Pure Onshore, 180 = Pure Offshore
    diff = (wind_deg - shoreline_bearing + 180) % 360 - 180
    abs_diff = abs(diff)

    if abs_diff < 45:
        return "Onshore"
    elif 45 <= abs_diff < 75:
        return "Cross-on"
    elif 75 <= abs_diff < 105:
        return "Cross-shore"
    elif 105 <= abs_diff < 135:
        return "Cross-off"
    else:
        return "Offshore"

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
    # Clean lookup for base score
    thresholds = [(8, 1), (12, 3), (17, 5), (22, 6), (28, 8), (36, 9), (float('inf'), 10)]
    base = next(score for limit, score in thresholds if wind_knots < limit)

    # Dictionary for modifiers
    modifiers = {
        "Cross-shore": 1,
        "Cross-off": 0.5,
        "Onshore": -0.5,
        "Offshore": -0.5
    }
    modifier = modifiers.get(wind_relative, 0)
    score = max(1, min(10, base + modifier))

    # Clean lookup for labels
    labels = [(3, "Stay Home"), (5, "Marginal"), (7, "Good Session"), (9, "Send It"), (float('inf'), "NUKING 🔥")]
    label = next(text for limit, text in labels if score <= limit)
    
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
    # Safety: Handle inland "N/A" strings
    if water_temp_c == "N/A" or water_temp_c is None:
        return "No recommendation (Inland)"

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
    api_key = os.getenv("ADMIRALTY_API_KEY")
    if not api_key: return []
    
    if station_id == "0000": return []

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

    # We need to find the tide that happened JUST BEFORE now
    prev_tide_time = None
    for event in tides:
        event_time = arrow.get(event['DateTime']).to(tz_name)
        if event_time < now_local:
            prev_tide_time = event_time  # Keep updating until we find the last one in the past

    for event in tides:
        event_time = arrow.get(event['DateTime']).to(tz_name)
        t_data = {
            "date": event_time.format('ddd, MMM DD'),
            "time": event_time.format('HH:mm'),
            "type": "High Tide" if "High" in event['EventType'] else "Low Tide",
            "height": round(event['Height'], 1),
        }
        tide_list.append(t_data)

        # Logic for the NEXT tide and tidal flow
        if not next_tide_obj and event_time > now_local:
            next_tide_obj = t_data
            next_tide_time = event_time
            
            if prev_tide_time:
                # --- RULE OF TWELFTHS MATH ---
                total_cycle_duration = (next_tide_time - prev_tide_time).total_seconds()
                time_elapsed = (now_local - prev_tide_time).total_seconds()
                
                if total_cycle_duration > 0:
                    cycle_position = time_elapsed / total_cycle_duration
                else:
                    cycle_position = 0 # Safety for identical timestamps
                
                # Middle of the tide (33% to 66% through the 6-hour window) is the strongest flow
                if 0.33 <= cycle_position <= 0.66:
                    tidal_flow = "Strong (Max Flow)"
                elif cycle_position < 0.16 or cycle_position > 0.84:
                    tidal_flow = "Slack (Weak)"
                else:
                    tidal_flow = "Moderate"

            # Update UI labels
            if "High" in t_data['type']:
                tide_display = "📈 Rising"
                next_tide_info = f"High @ {t_data['time']}"
            else:
                tide_display = "📉 Falling"
                next_tide_info = f"Low @ {t_data['time']}"

    # Springs/Neaps logic (unchanged)
    tide_phase = "Mid-Cycle"
    if len(tide_list) >= 4:
        today_range = max(all_heights[:4]) - min(all_heights[:4])
        ratio = today_range / weekly_max_range if weekly_max_range > 0 else 0.5
        if ratio > 0.85: tide_phase = "Springs"
        elif ratio < 0.60: tide_phase = "Neaps"

    return tide_list, next_tide_info, tide_display, tidal_flow, tide_phase

def process_forecast(hourly, current_hour_idx, tz_name):
    all_speeds = hourly.Variables(0).ValuesAsNumpy()
    all_gusts = hourly.Variables(1).ValuesAsNumpy()
    all_codes = hourly.Variables(2).ValuesAsNumpy()
    
    trend_12h = []
    for i in range(12):
        idx = int(current_hour_idx + i)
        
        # Safety: Ensure we don't index out of bounds if the array is short
        if idx >= len(all_speeds): break
            
        # Safety: Use np.nan_to_num to turn NaNs into 0.0
        s = np.nan_to_num(all_speeds[idx])
        g = np.nan_to_num(all_gusts[idx])
        c = np.nan_to_num(all_codes[idx])

        trend_12h.append({
            "hour": arrow.now(tz_name).shift(hours=i).format("HH:00"),
            "speed": float(round(s, 1)),
            "gust": float(round(g, 1)),
            "code": int(c)
        })
    return trend_12h

def process_marine_data(marine, current_hour_idx):
    """Calculates all wave-related physics and water temp with NaN safety."""
    m_curr = marine.Current()
    
    # Extract raw values
    raw_h = m_curr.Variables(0).Value()
    raw_p = m_curr.Variables(1).Value()
    
    # Safety Check: If coordinates are inland, these will be NaN
    wave_h = 0.0 if np.isnan(raw_h) else raw_h
    wave_p = 0.0 if np.isnan(raw_p) else raw_p
    
    # Existing physics functions (these are now safe because wave_h/p aren't NaN)
    wave_steepness = calculate_steepness(wave_h, wave_p)
    wave_pwr_val, wave_pwr_desc = calculate_wave_power(wave_h, wave_p)
    
    # Water temp logic
    m_hourly = marine.Hourly()
    w_temp_arr = m_hourly.Variables(0).ValuesAsNumpy()
    raw_temp = float(w_temp_arr[min(current_hour_idx, len(w_temp_arr) - 1)])
    
    # Final Temp Safety
    if np.isnan(raw_temp):
        final_temp = "N/A"
    else:
        final_temp = int(round(raw_temp))
    
    return {
        "height": float(round(wave_h, 1)),
        "period": float(round(wave_p, 1)),
        "steepness": wave_steepness,
        "power_val": wave_pwr_val,
        "power_desc": wave_pwr_desc,
        "temp": final_temp
    }

def get_demo_report(user_weight="75", level="intermediate", user_discipline="wave"):
    """Returns the hardcoded 'Nuking' report for demos."""
    tz_name = 'Europe/London'
    now_local = arrow.now(tz_name)
    current_hour_idx = now_local.hour
    # Move your existing 'demo_epic' dictionary here
    demo_wind = 26.5
    demo_waves = 2.5
    demo_relative = "💎 Cross-shore"
    weight_int = int(user_weight) if str(user_weight).isdigit() else 75
    final_discipline = determine_discipline(user_discipline, demo_waves, demo_wind)
    demo_gear = calculate_gear(weight_int, demo_wind, level, discipline=final_discipline)    
    demo_score, demo_label = get_sendiness_score(demo_wind, demo_relative)
    demo_session = {"start": "14:00", "avg_knots": 24.5}
    demo_wetsuit = get_wetsuit_rec(12)
    demo_color = get_wind_color(demo_wind)
    demo_beaufort = get_beaufort(demo_wind)
    
    # Mock 12h forecast for demo charts - more realistic wind pattern
    demo_forecast = []
    # Realistic afternoon wind pattern: builds, peaks, then drops
    base_speeds = [20, 22, 22, 24, 26, 28, 27, 25, 21, 19, 16, 14]

    for i, speed in enumerate(base_speeds):
        display_hour = (current_hour_idx + i) % 24
        demo_forecast.append({
            "hour": f"{display_hour:02d}:00",
            "speed": float(speed + (i % 3 - 1) * 0.5),  # Add slight variation
            "gust": float(speed + 5 + (i % 2)),  # Variable gust strength
            "code": 1 if i < 8 else 2  # Clear then partly cloudy
        })
    
    # 3. Best Session Calculation
    # Since the peak is at index 3, 4, 5, the start hour is now + 3 hours
    best_start_hour = (current_hour_idx + 3) % 24
    # (24+26+28) / 3 = 26.0 average
    demo_session = {"start": f"{best_start_hour:02d}:00", "avg_knots": 26.0}

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
            "wind_color": demo_color,
            "beaufort_f": demo_beaufort['f'],
            "beaufort_name": demo_beaufort['name'],
            "beaufort_desc": demo_beaufort['desc'],
            "wind_dir_name": "S-West",
            "wind_dir": 225,
            "wind_arrow": "↗️",
            "wind_relative": demo_relative,
            "wind_trend": "Building",
            "gusts_knots": 34.0,
            "waves_m": demo_waves,
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
            "recommended_gear": demo_gear,
        },
        "forecast_12h": demo_forecast,
        "local_knowledge": "Perfect cross-shore conditions. Watch the sandbar at low tide."
    }

async def get_shred_report(spot_key: str, user_weight: str = "75", level="intermediate", user_discipline='auto'):
    spot = SPOTS.get(spot_key)
    if not spot: return None

    if spot_key == "demo_epic":
        return get_demo_report(user_weight, level, user_discipline)

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
    safe_wind = 0.0 if np.isnan(wind_spd) else wind_spd
    
    # 3. Process Sub-Modules (The Optimizations)
    # This keeps the main function under 50 lines
    tide_list, next_info, t_disp, t_flow, t_phase = process_tides(tides, tz_name, now_local)
    marine_data = process_marine_data(marine, current_hour_idx)
    trend_12h = process_forecast(weather.Hourly(), current_hour_idx, tz_name)

    # --- NEW: Identify Discipline BEFORE calculating gear ---
    
    final_discipline = determine_discipline(user_discipline, marine_data['height'], wind_spd)
    # Now use final_discipline for gear and the rest of the report
    
    # 5. Physics & Logic
    weight_int = int(user_weight) if user_weight.isdigit() else 75
    gear = calculate_gear(weight_int, wind_spd, level, discipline=final_discipline)    
    rel_wind = get_relative_wind(wind_deg, spot.get('shoreline_bearing'))
    dir_info = get_compass_info(wind_deg)
    sendiness_score, sendiness_label = get_sendiness_score(safe_wind, rel_wind)
    beaufort = get_beaufort(wind_spd)
    best_session = get_best_session_window(trend_12h)

    # Wind trend (Simplified comparison)
    all_speeds = weather.Hourly().Variables(0).ValuesAsNumpy()
    future_wind = all_speeds[min(current_hour_idx + 3, len(all_speeds)-1)] 
    if future_wind > wind_spd + 2: wind_trend = "Building"
    elif future_wind < wind_spd - 2: wind_trend = "Dropping"
    else: wind_trend = "Steady"
    
    # Wind colour drives the UI badge colour
    wind_color = get_wind_color(wind_spd)

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
            "waves_m": float(round(marine_data['height'], 1)) if isinstance(marine_data['height'], (int, float)) else 0.0,
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

async def get_ai_recommendation(report, user_weight, spot_key, user_level, user_discipline):
    try:
        # 1. Setup paths and load ONLY the spot-specific info
        base_path = Path(__file__).parent / "spotbot_knowledge"
        spot_path = base_path / "spots" / f"{spot_key}.txt"
        spot_kb = spot_path.read_text(encoding='utf-8')[:10000] if spot_path.exists() else "Standard beach break."

        live = report.get('live', {})
        meta = report.get('metadata', {})
        gear = live.get('recommended_gear', {})
        final_discipline = gear.get('type', user_discipline)
        # 2. Extract Key Data
        sendiness = float(live.get('sendiness_score', 0))
        wind_knots = live.get('wind_knots', 0)
        gusts = live.get('gusts_knots', 0)
        wind_dir = live.get('wind_dir_name', 'Unknown')
        t_flow = live.get('tidal_flow', 'Neutral')
        w_rel = live.get('wind_relative', 'Unknown')
        temp = float(live.get('air_temp', 12))

        # 1. Identify the Discipline
        is_wave_session = float(live.get('waves_m', 0)) > 1.2
        is_high_wind = float(live.get('wind_knots', 0)) > 25

        # 2. Determine the "Vibe Label"
        if is_wave_session:
            session_label = "This is a proper WAVE session. Focus on surf safety and jumping."
        elif is_high_wind:
            session_label = "This is a high-wind BLASTING session. Focus on control and survival."
        else:
            session_label = "This is a standard FREERIDE session. Focus on carving and speed."
        
        # 3. Pre-Calculate the "Truths" (LLM just writes these into the story)
        # 3. Pre-Calculate the "Truths"
        if sendiness < 3.5:
            gear_fact = "N/A"
            vibe_mode = "Disappointed and salty. You are standing in the rain looking at a flat sea. You’re heading to the pub and telling others not to bother."
            mood = "Grumpy"
            kit_instruction = "Do NOT recommend any windsurf kit. Instead, suggest the pub, a coffee, or a paddleboard."
        elif sendiness < 7.0:
            gear_fact = f"{gear.get('sail')} sail and {gear.get('board')} board"
            vibe_mode = "Chill and helpful. You just finished a decent session. You're leaning against your van, watching the water and giving tactical advice."
            mood = "Stoked"
            kit_instruction = f"Strictly recommend the {gear_fact}. Tell them why this kit fits the {wind_knots}kt breeze."
        else:
            gear_fact = f"{gear.get('sail')} sail and {gear.get('board')} board"
            vibe_mode = "Hyped and direct! Conditions are epic/heavy. You're out of breath from an intense session and warning people it's a wild ride out there."
            mood = "Adrenaline-fueled"
            kit_instruction = f"Strictly recommend the {gear_fact}. Tell them why this kit fits the {wind_knots}kt breeze."

       # 3. Nuanced Tide Logic
        has_tide = "0000" not in str(meta.get('tide_station_id', '')) and "unavailable" not in live.get('tide_display', '').lower()
        
        # Initialize with a default value to prevent the 'not associated with a value' error
        tide_fact = "The tide is mellow." 

        if not has_tide:
            tide_fact = "This is inland/reservoir. Do NOT mention tides, flow, or currents."
        else:
            # Physics: Wind WITH tide robs power. Wind AGAINST tide adds grunt.
            is_aligned = ("falling" in t_flow.lower() and "offshore" in w_rel.lower()) or \
                         ("rising" in t_flow.lower() and "onshore" in w_rel.lower())

            is_fighting = ("falling" in t_flow.lower() and "onshore" in w_rel.lower()) or \
                          ("rising" in t_flow.lower() and "offshore" in w_rel.lower())

            if is_aligned:
                tide_fact = "The wind and tide are aligned—it'll rob 5 knots of power from the sail."
            elif is_fighting:
                tide_fact = "Wind is fighting the tide—expect extra 'grunt' in the sail but messy chop."
            else:
                # Fallback for Slack water or Cross-shore flow
                tide_fact = f"Tide is {t_flow}. Mention how it affects the drift or launch."

        # Temperature Fact
        temp_fact = "It's freezing (sub-8°C). Mention a thick 5/4 wetsuit and boots." if temp < 8 else "Standard wetsuit weather."

        # 4. Few-Shot Prompting (The "Many-Shot" approach)
        prompt = f"""You are the Local Windsurf Legend. Chilled out windsurfer who is a great gear recommender.
        Current Mood: {mood}
        Your Location: {vibe_mode}

        EXAMPLES OF TONE:
        - (Grumpy): "Don't bother rigging, mate. It's a mirror out there and I'm off for a pint."
        - (Stoked): "Solid session! The {gear_fact} was perfect for carving the swell."

        EXAMPLES OF THE VIBE:
        
        Input: Sendiness 2/10, Wind 8kts, Inland, Gear: Pub.
        Output: "Standing on the pebbles at Llandegfedd and it's a mirror, mate. A measly 8 knots won't even wiggle a flag, let alone a sail. Don't bother rigging—I'm heading to the pub to wait for a real breeze."
        
        Input: Sendiness 8/10, Wind 22kts, Tidal, Gear: 4.7m/85L.
        Output: "Just came in and it's firing! That 4.7m was the call with the wind fighting the tide—it’s giving the sail heaps of grunt but the chop is getting technical. Watch the rips near the estuary mouth if you're heading out now."
        
        Input: Sendiness 5/10, Wind 16kts, Tidal (Aligned), Gear: 6.5m/110L.
        Output: "Not bad, but that ebbing tide is running with the wind, so it's robbing you of about 5 knots of pull. I'd rig the 6.5m to keep the power up. Keep an eye on the sandbar as the tide drops—it'll catch your fin if you aren't careful."

        CURRENT DATA:
        - Rider at: {meta.get('spot_name')}
        - Wind: {wind_knots}kts {wind_dir} (Gusts: {gusts}kts)
        - Sendiness: {sendiness}/10
        - Gear to use: {gear_fact}
        - Tide Fact: {tide_fact}
        - Environment: {temp_fact}
        - Spot Hazards: {spot_kb}
        - Session type: {session_label}
        - Rider Profile: {user_level} level, {user_weight}kg.
        - Chosen Discipline: {user_discipline} (Finalized as: {final_discipline}).
        ...
        If they chose 'Wave' but it's flat, or 'Freeride' but it's 40 knots, give them the Legend's honest take on that choice.

        MISSION:
        Write THE VIBE.
        1. Perspective: {vibe_mode}.
        2. {kit_instruction}
        3. { "STRICT: Do NOT mention sail sizes or board volumes. Tell them to grab a paddleboard, go to the pub, or stay in bed." if sendiness < 3.5 else "STRICT: Explain why the " + gear_fact + " is the right call for " + session_label + "." }
        4. Include the Tide Fact: {tide_fact}.
        5. Mention one hazard from 'Spot Hazards'.
        6. Use a "You should..." or "I'm..." tone so the user knows exactly what the local vibe is.
        7. STRICT: 3 sentences max. Do not include any introductory fluff like 'Here is the vibe'.
        """

        response = await client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.6
        )
        return response.choices[0].message.content

    except Exception as e:
        return f"The Legend is checking the rig: {str(e)}"
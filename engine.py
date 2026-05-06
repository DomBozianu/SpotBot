import os
import arrow
import requests_cache
import openmeteo_requests
from retry_requests import retry
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pathlib import Path
import json
import numpy as np
import math
import tempfile
import asyncio

# Static Paths
BASE_DIR = Path(__file__).parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)
KB_DIR = BASE_DIR / "spotbot_knowledge"
SPOT_DIR = KB_DIR / "spots"
SPOTS_JSON = KB_DIR / "spots.json"

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
    if SPOTS_JSON.exists():
        with open(SPOTS_JSON, "r") as f:
            return json.load(f)
    return {}

SPOTS = load_spots()

async def fetch_all_data(lat, lon, tide_id):
    # Fetch weather, marine, and tides in parallel to save time.
    # Create tasks but don't 'await' them yet
    weather_task = asyncio.to_thread(fetch_spot_data, lat, lon)
    tide_task = asyncio.to_thread(fetch_tide_data, tide_id)
    
    # Run them simultaneously
    (weather_res, marine_res), tides = await asyncio.gather(weather_task, tide_task)
    return weather_res, marine_res, tides

def calculate_gear(weight_kg, wind_speed_kts, skill_level="intermediate", discipline="freeride"):
    # Calculates gear with rig-weight adjusted sinker logic.
    # 1. Base Sail if/elif/else
    if wind_speed_kts < 12: base_sail = 8.0
    elif 12 <= wind_speed_kts < 16: base_sail = 7.0
    elif 16 <= wind_speed_kts < 20: base_sail = 6.0
    elif 20 <= wind_speed_kts < 25: base_sail = 5.0
    elif 25 <= wind_speed_kts < 30: base_sail = 4.2
    else: base_sail = 3.7
    
    # 2. Weight Adjustment (+/- 0.5m per 10kg from 75kg)
    weight_diff = (weight_kg - 75) / 10
    recommended_sail = base_sail + (weight_diff * 0.5)

    # 3. Discipline Offsets
    discipline = discipline.lower()
    discipline_offsets = {
        "wave": -0.5, 
        "freestyle": -0.4, 
        "freeride": 0.0
    }
    recommended_sail += discipline_offsets.get(discipline, 0)
    final_sail = round(max(3.0, min(10.0, recommended_sail)), 1)

    # 4. Board Volume Logic
    # Beginner: Needs to uphaul (Weight + 100)
    # Intermediate: Needs a safety margin (Weight + 40)
    # Advanced: Only needs enough to plane (Weight + 15)
    reserve_map = {
        "novice": 100, 
        "intermediate": 40, 
        "advanced": 15
    }
    reserve = reserve_map.get(skill_level, 40)
    
    # HIGH WIND TAX
    # Lower volume when wind increases
    if wind_speed_kts > 20:
        wind_reduction = ((wind_speed_kts - 20) / 5) * 5
        reserve -= wind_reduction
    # Combine body weight with the wind-adjusted reserve
    recommended_volume = int(weight_kg + reserve)
    # Final discipline adjustment: Wave/Freestyle boards are naturally lower volume
    if discipline in ["wave", "freestyle"]:
        recommended_volume -= 10
    
    # 5. Sinker Logic
    # If volume is less than (Body Weight + 15kg rig), it's a sinker.
    is_sinker = recommended_volume < (weight_kg + 15) # can remove

    return {
        "sail": f"{final_sail}m²",
        "board": f"{recommended_volume}L",
        "type": discipline,
        "is_sinker": is_sinker #can remove
    }

def determine_discipline(user_choice, wave_height, wind_speed):
    # If user specified a discipline, use it.
    if user_choice != "auto":
        return user_choice
    
    if wave_height > 1.2 and wind_speed > 18:
        return "wave"
    elif wind_speed > 15 and wave_height < 0.6:
        return "freestyle"
    return "freeride"

def calculate_wave_power(height, period):
    # Calculates wave power in kW/m
    try:
        h = float(height)
        t = float(period)
        power_val = round(0.5 * (h ** 2) * t, 1)
    except (ValueError, TypeError):
        return 0, "Flat"

    if power_val < 10: wave_desc = "Weak"
    elif 10 <= power_val <= 40: wave_desc = "Clean"
    else: wave_desc = "Heavy"
        
    return power_val, wave_desc

def calculate_steepness(height, period):
    # Calculates wave steepness. Higher = punchier/hollower.
    if period == 0: return "Flat"
    steepness = height / (period ** 2)
    if steepness > 0.025: return "Hollow/Steep"
    if steepness > 0.015: return "Average"
    return "Rolling/Fat"

def get_beaufort(knots):
    # Lookup table: (Max Knots, Force, Name, Description)
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
    # Maps wind speed to a UI color badge name.
    color_thresholds = [(13, "light"), (19, "green"), (26, "sweet"), (36, "heavy"), (float('inf'), "nuke")]
    return next(color for limit, color in color_thresholds if knots < limit)

def get_relative_wind(wind_deg, shoreline_bearing):
    # Calculate wind angle relative to beach
    if shoreline_bearing is None: return "Unknown"
    
    # Calculate the shortest difference between wind and shore normal
    diff = (wind_deg - shoreline_bearing + 180) % 360 - 180
    abs_diff = abs(diff)

    if abs_diff < 45: return "Onshore"
    elif 45 <= abs_diff < 75: return "Cross-on"
    elif 75 <= abs_diff < 105: return "Cross-shore"
    elif 105 <= abs_diff < 135: return "Cross-off"
    else: return "Offshore"

def get_compass_info(degrees):
    # Converts wind direction degree into Cardinal with arrow
    directions = [
        {"word": "North", "arrow": "⬇️"}, {"word": "N-East", "arrow": "↙️"},
        {"word": "East", "arrow": "⬅️"}, {"word": "S-East", "arrow": "↖️"},
        {"word": "South", "arrow": "⬆️"}, {"word": "S-West", "arrow": "↗️"},
        {"word": "West", "arrow": "➡️"}, {"word": "N-West", "arrow": "↘️"}
    ]
    index = int((degrees + 22.5) % 360 // 45)
    return directions[index]

def get_weather_desc(code):
    # Maps weather code to word description
    wmo_codes = {
        0: "Clear skies", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle", 
        61: "Slight rain", 63: "Moderate rain", 71: "Slight snow", 80: "Rain showers", 
        95: "Thunderstorms"
    }
    # Convert to int to handle float codes
    return wmo_codes.get(int(code) if code is not None else 0, "Unknown")

def get_sendiness_score(wind_knots, wind_relative):
    #Calculates Sendiness rating (1/10) using wind speed with modifiers
    thresholds = [(8, 1), (12, 3), (17, 5), (22, 6), (28, 8), (36, 9), (float('inf'), 10)]
    base = next(score for limit, score in thresholds if wind_knots < limit)

    # Dictionary for modifiers
    modifiers = {
        "Cross-shore": 1,
        "Cross-off": 0.5,
        "Onshore": -0.5,
        "Offshore": -0.5
    }
    score = max(1, min(10, base + modifiers.get(wind_relative, 0)))
    
    #Sendy label
    labels = [(3, "Stay Home"), (5, "Marginal"), (7, "Good Session"), (9, "Send It"), (float('inf'), "NUKING 🔥")]
    label = next(text for limit, text in labels if score <= limit)
    
    return score, label

def get_best_session_window(trend_12h):
    # Find best 3 hour block of wind
    if len(trend_12h) < 3: return None

    best_avg = 0
    best_start = None

    for i in range(len(trend_12h) - 2):
        window = trend_12h[i:i+3]
        avg = sum(h['speed'] for h in window) / 3

        # Sweet spot: above 15kts (planing) and below 40kts
        if 15 <= avg <= 40 and avg > best_avg:
            best_avg = avg
            best_start = window[0]['hour']

    if best_start is None:
        return None

    return {"start": best_start, "avg_knots": round(best_avg, 1)} if best_start else None


def get_wetsuit_rec(water_temp_c):
    # Wetsuit recommendation
    # Safety: Handle inland "N/A" strings
    if water_temp_c == "N/A" or water_temp_c is None:
        return "No recommendation (Inland)"

    if water_temp_c >= 19:   return "2mm Shorty"
    elif water_temp_c >= 14: return "3/2mm Full Suit"
    elif water_temp_c >= 10: return "4/3mm + Booties"
    else:                    return "5/4mm Hooded + Gloves + Booties"


def fetch_spot_data(lat, lon):
    # Fetch weather and marine data
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
    # Fetches tide data
    api_key = os.getenv("ADMIRALTY_API_KEY")
    if not api_key: return []
    
    if station_id == "0000": return []

    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents?duration=2"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    try:
        response = cache_session.get(url, headers=headers, timeout=5)
        return response.json() if response.status_code == 200 else []
    except Exception as e:
        print(f"Tide API Error: {e}")
        return []

def process_tides(tides, tz_name, now_local):
    # Processes tide data into usable data with flow, status
    tide_list = []
    next_tide_obj = None
    tidal_flow, tide_display, next_tide_info = "Low", "Stable", "Data Unavailable"
    
    if not tides:
        return tide_list, next_tide_info, tide_display, tidal_flow, "Unknown"

    all_heights = [event['Height'] for event in tides]
    weekly_max_range = max(all_heights) - min(all_heights)

    # Find tide that just happened
    prev_tide_time = None
    for event in tides:
        event_time = arrow.get(event['DateTime']).to(tz_name)
        if event_time < now_local:
            prev_tide_time = event_time

    for event in tides:
        event_time = arrow.get(event['DateTime']).to(tz_name)
        # Build tide event list for chart        
        if event_time > now_local.shift(minutes=-30): 
            tide_list.append({
                "date": event_time.format('ddd, MMM DD'),
                "time": event_time.format('HH:mm'),
                "type": "High Tide" if "High" in event['EventType'] else "Low Tide",
                "height": round(event['Height'], 1),
            })

        # Logic for the upcoming tide and tidal flow
        if not next_tide_obj and event_time > now_local:
            anchor = prev_tide_time if prev_tide_time else now_local.shift(hours=-6)

            is_high = "High" in event['EventType']
            next_tide_info = f"{'High' if is_high else 'Low'} @ {event_time.format('HH:mm')}"
            tide_display = "📈 Rising" if is_high else "📉 Falling"
            next_tide_obj = event

            # Rule of Twelfths Logic
            if anchor:
                total_sec = (event_time - anchor).total_seconds()
                elapsed_sec = (now_local - anchor).total_seconds()
                pos = elapsed_sec / total_sec if total_sec > 0 else 0
                
                if 0.33 <= pos <= 0.66: tidal_flow = "Strong (Max Flow)"
                elif pos < 0.16 or pos > 0.84: tidal_flow = "Slack (Weak)"
                else: tidal_flow = "Moderate"

    # Springs/Neaps logic
    tide_phase = "Mid-Cycle"
    if len(tide_list) >= 4:
        today_range = max(all_heights[:4]) - min(all_heights[:4])
        ratio = today_range / weekly_max_range if weekly_max_range > 0 else 0.5
        if ratio > 0.85: tide_phase = "Springs"
        elif ratio < 0.60: tide_phase = "Neaps"

    return tide_list[:6], next_tide_info, tide_display, tidal_flow, tide_phase

def process_forecast(hourly, current_hour_idx, now_local):
    # Processes wind and weather data
    # Convert API arrays to numpy
    all_speeds = hourly.Variables(0).ValuesAsNumpy()
    all_gusts = hourly.Variables(1).ValuesAsNumpy()
    all_codes = hourly.Variables(2).ValuesAsNumpy()
    
    trend_12h = []
    for i in range(12):
        idx = int(current_hour_idx + i)
        
        if idx >= len(all_speeds): break
            
        # Safety: Use np.nan_to_num to turn NaNs into 0.0
        s = np.nan_to_num(all_speeds[idx]).item()
        g = np.nan_to_num(all_gusts[idx]).item()
        c = np.nan_to_num(all_codes[idx]).item()

        trend_12h.append({
            "hour": now_local.shift(hours=i).format("HH:00"),
            "speed": float(round(s, 1)),
            "gust": float(round(g, 1)),
            "code": int(c)
        })
    return trend_12h

def process_marine_data(marine, current_hour_idx):
    # Processes marine data
    m_curr = marine.Current()
    
    # Extract wave height and period with defaults
    raw_h = m_curr.Variables(0).Value()
    raw_p = m_curr.Variables(1).Value()
    wave_h = 0.0 if np.isnan(raw_h) else raw_h
    wave_p = 0.0 if np.isnan(raw_p) else raw_p
    
    # Wave physics calculations
    wave_steepness = calculate_steepness(wave_h, wave_p)
    wave_pwr_val, wave_pwr_desc = calculate_wave_power(wave_h, wave_p)
    
    # Water temperature logic
    m_hourly = marine.Hourly()
    w_temp_arr = m_hourly.Variables(0).ValuesAsNumpy()
    raw_temp = float(w_temp_arr[min(current_hour_idx, len(w_temp_arr) - 1)])
    
    # If it's NaN (like in a lake), return "N/A" so the wetsuit logic knows
    final_temp = int(round(raw_temp)) if not np.isnan(raw_temp) else "N/A"
    
    return {
        "height": float(round(wave_h, 1)),
        "period": float(round(wave_p, 1)),
        "steepness": wave_steepness,
        "power_val": wave_pwr_val,
        "power_desc": wave_pwr_desc,
        "temp": final_temp
    }

def get_demo_report(user_weight="75", level="intermediate", user_discipline="wave"):
    # Returns a hardcoded 'Nuking' report for demos
    tz_name = 'Europe/London'
    now_local = arrow.now(tz_name)
    current_hour_idx = now_local.hour
    today_str = now_local.format('ddd, MMM DD')
    tmrw_str = now_local.shift(days=1).format('ddd, MMM DD')

    # Demo dict values
    demo_wind = 26.5
    demo_waves = 2.5
    demo_period = 11.0
    demo_temp = 12
    demo_relative = 'Cross-shore'
    weight_int = int(user_weight) if str(user_weight).isdigit() else 75
    
    # Calculations using previous functions
    final_discipline = determine_discipline(user_discipline, demo_waves, demo_wind)
    demo_gear = calculate_gear(weight_int, demo_wind, level, final_discipline)    
    demo_score, demo_label = get_sendiness_score(demo_wind, demo_relative)
    demo_beaufort = get_beaufort(demo_wind)
    # Wave calculations
    pwr_val, pwr_desc = calculate_wave_power(demo_waves, demo_period)
    steepness = calculate_steepness(demo_waves, demo_period)

    # Mock 12h forecast for demo charts - more realistic wind pattern
    # Realistic afternoon wind pattern: builds, peaks, then drops
    base_speeds = [20, 22, 22, 24, 26, 28, 27, 25, 21, 19, 16, 14]
    demo_forecast = []
    for i, speed in enumerate(base_speeds):
        demo_forecast.append({
            "hour": now_local.shift(hours=i).format("HH:00"),
            "speed": float(speed),
            "gust": float(speed + 5),
            "code": 1
        })
    
    # 3. Best Session Calculation
    demo_session = get_best_session_window(demo_forecast)

    return {
        "metadata": {
            "spot_name": "DEMO: Epic Peak",
            "status": "Nuking!",
            "date": today_str,
            "last_updated": now_local.format('HH:mm'),
            "sunrise": "06:00",
            "sunset": "20:30",
            "tide_list": [
                {"date": today_str, "time": "16:30", "type": "High Tide", "height": 4.2},
                {"date": today_str, "time": "22:45", "type": "Low Tide", "height": 0.8},
                {"date": tmrw_str, "time": "04:15", "type": "High Tide", "height": 4.1},
                {"date": tmrw_str, "time": "10:30", "type": "Low Tide", "height": 0.9},
                {"date": tmrw_str, "time": "16:45", "type": "High Tide", "height": 4.0},
                {"date": tmrw_str, "time": "23:00", "type": "Low Tide", "height": 1.0}
            ]
        },
        "live": {
            "wind_knots": demo_wind,
            "wind_color": get_wind_color(demo_wind),
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
            "wave_period": demo_period,
            "wave_power": pwr_val,
            "wave_power_desc": pwr_desc,
            "tide_display": "📈 Rising",
            "next_tide_info": "High @ 16:30 (2h remaining)",
            "tide_phase": "Springs",
            "tidal_flow": "Strong",
            "sun_status": "✨ Golden Hour!",
            "water_temp": demo_temp,
            "wetsuit_rec": get_wetsuit_rec(demo_temp),
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
    # Uses all pre-calculated data to build the final report
    # 1. Spot validation
    spot = SPOTS.get(spot_key)
    if not spot: return None

    if spot_key == "demo_epic":
        return get_demo_report(user_weight, level, user_discipline)

    # 2. Parallel Fetching
    weather, marine, tides = await fetch_all_data(spot['lat'], spot['lon'], spot['tide_id'])

    # 3. Time Setup
    raw_tz = weather.Timezone()
    tz_name = raw_tz.decode('utf-8') if isinstance(raw_tz, bytes) else raw_tz or 'Europe/London'
    now_local = arrow.now(tz_name)
    current_hour_idx = now_local.hour
    today_date = now_local.format('ddd, MMM DD')
    last_updated = now_local.format('HH:mm')

    # 4. Extract Current Weather
    current = weather.Current()
    wind_spd = current.Variables(1).Value()
    wind_deg = current.Variables(2).Value()
    gust_spd = current.Variables(3).Value()
    app_temp = current.Variables(4).Value()
    clouds = current.Variables(5).Value()
    visibility_km = round(current.Variables(6).Value() / 1000, 1)
    
    # 5. Run sub modules
    tide_list, next_info, t_disp, t_flow, t_phase = process_tides(tides, tz_name, now_local)
    marine_data = process_marine_data(marine, current_hour_idx)
    trend_12h = process_forecast(weather.Hourly(), current_hour_idx, now_local)
    
    #6. Physics and Gear calclulations
    final_discipline = determine_discipline(user_discipline, marine_data['height'], wind_spd)
    
    weight_int = int(user_weight) if user_weight.isdigit() else 75
    gear = calculate_gear(weight_int, wind_spd, level, discipline=final_discipline)    
    
    rel_wind = get_relative_wind(wind_deg, spot.get('shoreline_bearing'))
    dir_info = get_compass_info(wind_deg)
    sendiness_score, sendiness_label = get_sendiness_score(wind_spd, rel_wind)
    beaufort = get_beaufort(wind_spd)
    best_session = get_best_session_window(trend_12h)

    # 7. Wind trend comparison
    all_speeds = weather.Hourly().Variables(0).ValuesAsNumpy()
    future_wind = all_speeds[min(current_hour_idx + 3, len(all_speeds)-1)] 
    if future_wind > wind_spd + 2: wind_trend = "Building"
    elif future_wind < wind_spd - 2: wind_trend = "Dropping"
    else: wind_trend = "Steady"
    
    # Wind colour sets the UI badge colour
    wind_color = get_wind_color(wind_spd)

    # 8. Sun Logic
    daily = weather.Daily()
    sunrise = arrow.get(int(daily.Variables(0).ValuesInt64AsNumpy()[0])).to(tz_name).format("HH:mm")
    sunset = arrow.get(int(daily.Variables(1).ValuesInt64AsNumpy()[0])).to(tz_name).format("HH:mm")
    
    sunset_time = arrow.get(sunset, "HH:mm").replace(year=now_local.year, month=now_local.month, day=now_local.day)
    if now_local > sunset_time:
        sun_status = "Post-Sunset"
    else:
        diff = (sunset_time - now_local).total_seconds() / 3600
        sun_status = "✨ Golden Hour" if diff < 1 else f"{int(diff)}h Daylight"

    # 8. Local Knowledge
    wisdom = "No local knowledge found."
    path = SPOT_DIR / spot['knowledge_file']    
    if path.exists():
        wisdom = path.read_text(encoding='utf-8')[:1000]

    # Build final report
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
            "recommended_gear": gear,
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
    # Inputs the report into LLM to get natural language output
    try:
        spot_path = SPOT_DIR / f"{spot_key}.txt"
        spot_kb = spot_path.read_text(encoding='utf-8')[:1000] if spot_path.exists() else "Standard beach break."

        # 1. Data Extraction
        live = report.get('live', {})
        meta = report.get('metadata', {})
        gear = live.get('recommended_gear', {})
        
        final_discipline = gear.get('type', user_discipline)

        sendiness = float(live.get('sendiness_score', 0))
        wind_knots = live.get('wind_knots', 0)
        gusts = live.get('gusts_knots', 0)
        wind_dir = live.get('wind_dir_name', 'Unknown')
        t_flow = live.get('tidal_flow', 'Neutral')
        wind_rel = live.get('wind_relative', 'Unknown')
        temp = float(live.get('air_temp', 12))
        waves = float(live.get('waves_m', 0))
        
        # 2. Wind and tide logic
        has_tide = "0000" not in str(meta.get('tide_station_id', '')) and "unavailable" not in live.get('tide_display', '').lower()
        if not has_tide:
            tide_intel = "Inland/Non-tidal water."
        else:
            aligned = ("falling" in t_flow and "offshore" in wind_rel) or ("rising" in t_flow and "onshore" in wind_rel)
            opposed = ("falling" in t_flow and "onshore" in wind_rel) or ("rising" in t_flow and "offshore" in wind_rel)
            if aligned:
                tide_intel = "Wind and tide are aligned, you'll feel underpowered."
            elif opposed:
                tide_intel = "Wind vs Tide—plenty of grunt in the sail but expect messy, short chop."
            else:
                tide_intel = f"Tide is {t_flow}. Check depth for your fins."

        # 3. Decision Logic (Go vs. No-Go)
        if sendiness < 3.5:
            decision = "NO-GO. It's too light. Tell them to grab a pint or a coffee instead."
            kit_instruction = "Do NOT mention specific sail sizes—just say it's not worth rigging."
            persona, mood = "Grumpy local", "Salty"
        else:
            decision = f"GO. Conditions are solid for {user_level} {final_discipline}."
            kit_instruction = f"YOU MUST tell them to rig the {gear.get('sail')} sail and {gear.get('board')} board."
            
            persona, mood = ("Hardcore storm-chaser", "Hyped") if sendiness > 7.0 else ("Helpful legend", "Stoked")
        

        # 4. System prompt
        system_context = f"""
        Role: {persona}. Mood: {mood}.
        Location Context: {meta.get('spot_name')}. Local Knowledge: {spot_kb}
        
        Discipline: {final_discipline}

        DATA:
        - Wind: {wind_knots}kts (Gusts: {gusts}kts) from {wind_dir}.
        - Waves: {waves}m ({live.get('wave_power_desc')} power, {live.get('wave_steepness')}).
        - Water: {live.get('water_temp')}°C. Wetsuit: {live.get('wetsuit_rec')}.
        - Tide Physics: {tide_intel}
        - Sun: {live.get('sun_status')}.
        - User: {user_weight}kg, {user_level} level

        YOUR DECISION: {decision}
        KIT ADVICE: {kit_instruction}

        TASK: 
        Write a 3-sentence 'vibe check'. 
        1. Start with the verdict (Go or No-Go).
        2. Give the KIT ADVICE
        2. Explain the 'Tide Physics' and 'Wind' data so they understand the feel.
        3. Mention one hazard from the local knowledge if they are going.
        NO introductions. NO 'Legend' name. Be authentic and direct.
        Also, if location is an inland lake or reservoir, do not mention waves or tide.
        """

        response = await client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[{"role": "system", "content": system_context}],
            temperature=0.7
        )
        return response.choices[0].message.content

    except Exception as err:
        return f"The Legend is tied up in the rigging: {str(err)}"
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
    api_key = os.getenv("ADMIRALTY_KEY")
    if not api_key: return []
    url = f"https://admiraltyapi.azure-api.net/uktidalapi/api/V1/Stations/{station_id}/TidalEvents"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    try:
        response = cache_session.get(url, headers=headers, timeout=3)
        if response.status_code == 200:
            return response.json()
    except: return []
    return []

async def get_shred_report(spot_key: str, user_weight: str = "75", level="intermediate"):
    spot = SPOTS.get(spot_key)
    if not spot: return None
    # --- DEMO / PRESENTATION MODE ---
    # Hardcoded "nuking" conditions so demos always look great regardless of real weather.
    # All fields must match the live report structure exactly.
    if spot_key == "demo_epic":
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
                "visibility": 15.0
            },
            "forecast_12h": demo_forecast,
            "local_knowledge": "Perfect cross-shore conditions. Watch the sandbar at low tide."
        }

    # --- 1. Fetch weather, marine, and tide data ---
    weather, marine = fetch_spot_data(spot['lat'], spot['lon'])
    tides = fetch_tide_data(spot['tide_id'])
    
    #time stuff
    raw_tz = weather.Timezone()
    tz_name = raw_tz.decode('utf-8') if isinstance(raw_tz, bytes) else raw_tz
    tz_name = tz_name or 'Europe/London'
    now_local = arrow.now(tz_name)
    current_hour_idx = now_local.hour

    last_updated = now_local.format('HH:mm')
    today_date = now_local.format('ddd, MMM DD')

    # --- 2. Current Wind ---
    current = weather.Current()
    app_temp = current.Variables(4).Value()
    clouds = current.Variables(5).Value()
    vis_meters = current.Variables(6).Value()
    visibility_km = round(vis_meters / 1000, 1)
    wind_spd = current.Variables(1).Value()
    wind_deg = current.Variables(2).Value()
    gust_spd = current.Variables(3).Value()
    
    #last_updated = arrow.now('Europe/London').format('HH:mm')
    #today_date = arrow.now('Europe/London').format('ddd, MMM DD')
    
    beaufort = get_beaufort(wind_spd)
    
    # Wind colour drives the UI badge colour
    if wind_spd < 13: wind_color = "light"
    elif wind_spd < 19: wind_color = "green"
    elif wind_spd < 26: wind_color = "sweet"
    elif wind_spd < 36: wind_color = "heavy"
    else: wind_color = "nuke"

    # Sun times from the daily forecast block
    daily = weather.Daily()
    sunrise = datetime.fromtimestamp(float(daily.Variables(0).ValuesInt64AsNumpy()[0])).strftime("%H:%M")
    sunset = datetime.fromtimestamp(float(daily.Variables(1).ValuesInt64AsNumpy()[0])).strftime("%H:%M")
    # After defining sunrise/sunset strings...
    sunset_obj = arrow.get(sunset, 'HH:mm')
    sunset_today = now_local.replace(hour=sunset_obj.hour, minute=sunset_obj.minute)

    if now_local > sunset_today:
        sun_status = "Sun has set"
    else:
        diff_sun = sunset_today - now_local
        if diff_sun.seconds / 3600 < 1:
            sun_status = "✨ Golden Hour!"
        else:
            sun_status = f"{diff_sun.seconds // 3600}h {(diff_sun.seconds % 3600) // 60}m left"

    rel_wind = get_relative_wind(wind_deg, spot.get('shoreline_bearing'))
    dir_info = get_compass_info(wind_deg)
    
    # --- 3. Marine (Waves & Water Temp) ---
    m_curr = marine.Current()
    wave_h = m_curr.Variables(0).Value()
    wave_p = m_curr.Variables(1).Value()
    wave_steepness = calculate_steepness(wave_h, wave_p)
    # Water temp comes from the hourly marine array; index by current hour
    m_hourly = marine.Hourly()
    w_temp_arr = m_hourly.Variables(0).ValuesAsNumpy()
    current_hour_idx = now_local.hour    
    if w_temp_arr.ndim == 0:
        w_temp = float(w_temp_arr)
    else:
        w_temp = float(w_temp_arr[current_hour_idx])

    wave_pwr_val, wave_pwr_desc = calculate_wave_power(wave_h, wave_p)
    
    
    # --- 4. Hourly Wind Trend & 12h Forecast ---
    hourly = weather.Hourly()
    all_speeds = hourly.Variables(0).ValuesAsNumpy()
    all_gusts = hourly.Variables(1).ValuesAsNumpy()
    all_codes = hourly.Variables(2).ValuesAsNumpy()

    # Simple trend: compare now vs 3 hours ahead
    future_wind = all_speeds[(current_hour_idx + 3) % 24]
    if future_wind > wind_spd + 2: wind_trend = "Building"
    elif future_wind < wind_spd - 2: wind_trend = "Dropping"
    else: wind_trend = "Steady"

    trend_12h = []
    for i in range(12):
        idx = (current_hour_idx + i) % 24
        trend_12h.append({
            "hour": f"{idx:02d}:00",
            "speed": float(round(all_speeds[idx], 1)),
            "gust": float(round(all_gusts[idx], 1)),
            "code": int(all_codes[idx])
        })

    # --- 5. Tides ---
    # Uses the Admiralty API data. Falls back gracefully if no API key is set.
    # THE RULE OF TWELFTHS: tidal flow is strongest 2-4 hours from high/low.
    tide_list = []
    next_tide_info = "Check tomorrow"
    tide_display = "Stable"
    tidal_flow = "Low"
    next_tide_obj = None
    tide_phase = "Unknown"

    if tides:
        # Benchmark for the whole week
        all_heights = [event['Height'] for event in tides]
        weekly_max_range = max(all_heights) - min(all_heights)

        for event in tides:
            # Create the time object for calculation
            event_time = arrow.get(event['DateTime']).to(tz_name)
            
            t_data = {
                "date": event_time.format('ddd, MMM DD'),
                "time": event_time.format('HH:mm'),
                "type": "High Tide" if "High" in event['EventType'] else "Low Tide",
                "height": round(event['Height'], 1),
            }

            # Strict Future Check: Use event_time (the Arrow object) for comparison
            if not next_tide_obj and event_time > now_local:
                next_tide_obj = t_data
                diff = event_time - now_local
                
                # THE RULE OF TWELFTHS (Tidal Flow)
                # Max flow is usually 2-4 hours away from high/low tide
                hours_until = diff.seconds / 3600
                if 2.0 <= hours_until <= 4.0:
                    tidal_flow = "Strong"
                elif hours_until < 1.0 or hours_until > 5.0:
                    tidal_flow = "Slack (Weak)"
                else:
                    tidal_flow = "Moderate"

                # Absolute time + countdown
                time_str = t_data['time']
                if "High" in t_data['type']:
                    tide_display = "📈 Rising"
                    next_tide_info = f"High @ {time_str} ({int(hours_until)}h remaining)"
                else:
                    tide_display = "📉 Falling"
                    next_tide_info = f"Low @ {time_str} ({int(hours_until)}h remaining)"
            
            tide_list.append(t_data)

        # Scalable Phase Logic
        today_heights = [t['height'] for t in tide_list[:4]] # First 24h
        today_range = max(today_heights) - min(today_heights)
        ratio = today_range / weekly_max_range if weekly_max_range > 0 else 0.5
        
        if ratio > 0.85: tide_phase = "Springs"
        elif ratio < 0.60: tide_phase = "Neaps"
        else: tide_phase = "Mid-Cycle"

    # --- 6. Sendiness Score, Best Session Window, Wetsuit ---
    sendiness_score, sendiness_label = get_sendiness_score(wind_spd, rel_wind)
    best_session = get_best_session_window(trend_12h)
    wetsuit_rec = get_wetsuit_rec(w_temp)

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
            "beaufort_f": beaufort['f'],
            "beaufort_name": beaufort['name'],
            "beaufort_desc": beaufort['desc'],
            "wind_dir_name": dir_info['word'],
            "wind_dir": int(wind_deg),
            "wind_arrow": dir_info['arrow'],
            "wind_relative": rel_wind,
            "wind_trend": wind_trend,
            "gusts_knots": float(round(gust_spd, 1)),
            "waves_m": float(round(wave_h, 1)),
            "wave_period": float(round(wave_p, 1)),
            "wave_steepness": wave_steepness,  # NEW
            "wave_power": wave_pwr_val,
            "wave_power_desc": wave_pwr_desc,
            "air_temp": int(round(app_temp)),  # NEW (Using Feels Like)
            "cloud_cover": int(clouds),         # NEW
            "visibility": visibility_km,        # NEW
            "tide_display": tide_display,
            "next_tide_info": next_tide_info,
            "tide_phase": tide_phase,
            "tidal_flow": tidal_flow,
            "sun_status": sun_status,
            "water_temp": int(round(w_temp)),
            "wetsuit_rec": wetsuit_rec,
            "sendiness_score": sendiness_score,
            "sendiness_label": sendiness_label,
            "best_session": best_session,
        },
        "forecast_12h": trend_12h,
        "local_knowledge": wisdom
    }
async def get_ai_recommendation(report, user_weight, spot_key, user_level):
    """
    Calls the LLM to generate the Local Legend's advice.
    Loads comprehensive windsurfing knowledge to ensure physically accurate recommendations.
    """
    try:
        base_path = Path(__file__).parent / "spotbot_knowledge"

        spot_kb_path = base_path / "spots" / f"{spot_key}.txt"
        spot_kb = spot_kb_path.read_text() if spot_kb_path.exists() else "No local knowledge available."

        metadata = report.get('metadata', {})
        spot_name = metadata.get('spot_name', 'The Beach')
        live = report.get('live', {})
        forecast = report.get('forecast_12h', [])

        # Current conditions
        wind_now = live.get('wind_knots', 0)
        wetsuit = live.get('wetsuit_rec', 'Unknown')
        best_session = live.get('best_session')
        
        # Forecast summary for AI context
        if forecast:
            forecast_summary = f"Next 12h: {forecast[0]['speed']}-{max(h['speed'] for h in forecast[:6])}kts"
            if best_session:
                forecast_summary += f". Best window: {best_session['start']} (~{best_session['avg_knots']}kts)"
        else:
            forecast_summary = "No forecast data available"

        # Calculate actual board volume for the user's weight
        user_weight_kg = int(user_weight)
        
        # Build a much cleaner, more focused prompt
        prompt = f"""You are the Local Legend - an expert windsurfer giving practical advice to a {user_level} level rider ({user_weight}kg).

CURRENT CONDITIONS:
Spot: {spot_name}
Wind: {wind_now}kts, F{live.get('beaufort_f')} ({live.get('beaufort_name')}), gusts {live.get('gusts_knots')}kts
Direction: {live.get('wind_relative')} (trend: {live.get('wind_trend')})
Waves: {live.get('waves_m')}m, {live.get('wave_power_desc')} power
Water: {live.get('water_temp')}°C
Forecast: {forecast_summary}

WINDSURFING PHYSICS RULES:
- Below 12kts: Too light for practical windsurfing. Recommend SUP or foil only.
- 12-15kts: Marginal conditions. Need huge gear (7-9m sails, {user_weight_kg + 50}L+ boards).
- 15kts+: Proper windsurfing. Use sail matrix and adjust for discipline.

SAIL MATRIX FOR {user_weight}KG RIDER:
- 10-15kts: 7.0-7.5m sail
- 15-20kts: 6.0-6.5m sail  
- 20-25kts: 5.0-5.5m sail
- 25-30kts: 4.2-4.7m sail
- 30kts+: 3.7-4.2m sail

BOARD VOLUMES FOR {user_weight}KG RIDER:
- Light wind (12-15kts): {user_weight_kg + 50}L (e.g. {user_weight_kg + 50}L board)
- Moderate wind (15-25kts): {user_weight_kg + 20}L (e.g. {user_weight_kg + 20}L board)  
- Strong wind (25kts+): {user_weight_kg + 10}L (e.g. {user_weight_kg + 10}L board)
- Wave sailing: Reduce by 10-20L for maneuverability

DISCIPLINES:
- Waves 1.5m+ & cross-shore = Wave sailing (smaller gear for control)
- Flat water & 20kts+ = Speed sailing  
- General conditions = Freeride
- Light wind = Learning/freeride only

LOCAL KNOWLEDGE: {spot_kb[:200]}

RESPONSE FORMAT:
If wind < 12kts: "SENDINESS: 1-2/10. Too light for windsurfing - try SUP or foil setup instead."
If wind 12-15kts: Give marginal advice with big gear
If wind 15kts+: Give full session advice

EXAMPLE GOOD RESPONSES:
"SENDINESS: 8/10
GEAR: 4.7m sail, 90L board
DISCIPLINE: Wave sailing - perfect cross-shore conditions  
WETSUIT: {wetsuit}
SESSION: Hit the water at 14:00 for best conditions
THE VIBE: Epic wave day! Cross-shore wind and clean power - watch the sandbar at low tide."

CRITICAL RULES:
- Give ACTUAL board volumes (e.g. "90L board" not formulas)
- If you say "too light" or SENDINESS 1-2, do NOT recommend windsurfing gear
- Never show technical formatting or backend calculations
- Keep it conversational and practical
- Conservative recommendations - better to say "too light" than recommend marginal gear
"""

        response = await client.chat.completions.create(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2  # Even lower for maximum consistency
        )
        return response.choices[0].message.content

    except Exception as e:
        return f"The Legend is lost in the fog: {e}"
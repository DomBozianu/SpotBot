from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from engine import get_shred_report, get_ai_recommendation, SPOTS
import json
from pathlib import Path

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- Load knowledge base once at startup, not on every request ---
KB_PATH = Path(__file__).parent / "spotbot_knowledge" / "general" / "knowledge_base.json"
with open(KB_PATH, "r") as f:
    KNOWLEDGE = json.load(f)
REPORT_CACHE = {}

sorted_spots = dict(sorted(SPOTS.items(), key=lambda item: item[1]['name']))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    spot_key = request.query_params.get("spot")
    weight = request.query_params.get("weight", "75")
    level = request.query_params.get("level", "intermediate")
    discipline = request.query_params.get("discipline", "auto")

    report_data = None
    if spot_key:
        report_data = await get_shred_report(spot_key, weight, level, discipline)
        # AI vibe is fetched client-side via /api/vibe after page load for better UX

        # --- CACHE MANAGEMENT ---
        # If cache gets too big, clear the oldest entry (or just wipe it)
        if len(REPORT_CACHE) > 100:
            # Wipe the oldest item (Python 3.7+ dicts preserve order)
            first_key = next(iter(REPORT_CACHE))
            del REPORT_CACHE[first_key]
            print(f"--- CACHE CLEANUP: Removed {first_key} ---")

        cache_key = f"{spot_key}_{weight}_{level}_{discipline}"
        REPORT_CACHE[cache_key] = report_data

    # Sort the spots alphabetically by their Name for the dropdown

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "report": report_data,
            "user_weight": weight,
            "user_level": level,
            "all_spots": sorted_spots,
            "kb_beaufort": KNOWLEDGE["beaufort_scale"],
            "kb_glossary": KNOWLEDGE["glossary"],
            "kb_sail_matrix": KNOWLEDGE["sail_matrix"]
        }
    )


@app.get("/api/vibe")
async def get_vibe_api(spot: str, weight: str = "75", level: str = "intermediate", discipline: str = "auto"):
    try:
        cache_key = f"{spot}_{weight}_{level}_{discipline}"
        
        # Check if we already have this report in memory
        if cache_key in REPORT_CACHE:
            print("--- CACHE HIT: Reusing existing report for AI ---")
            report_data = REPORT_CACHE[cache_key]
        else:
            # Fallback if the cache expired or was cleared
            report_data = await get_shred_report(spot, weight, level, discipline)
        
        final_disc = report_data['live']['recommended_gear']['type']

        # FIX: Explicitly name the arguments to avoid ordering errors
        vibe = await get_ai_recommendation(
            report=report_data, 
            user_weight=int(weight), 
            spot_key=spot, 
            user_level=level, 
            user_discipline=final_disc
        )
        return {"vibe": vibe}
    except Exception as e:
        print(f"Vibe Error: {e}")
        return {"vibe": "The Legend is lost in the fog."}
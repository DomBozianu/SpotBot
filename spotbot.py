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

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    spot_key = request.query_params.get("spot")
    weight = request.query_params.get("weight", "75")
    level = request.query_params.get("level", "intermediate")

    if weight == "": weight = "75"

    report_data = None
    if spot_key:
        report_data = await get_shred_report(spot_key, weight, level)
        # AI vibe is fetched client-side via /api/vibe after page load for better UX

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "report": report_data,
            "user_weight": weight,
            "user_level": level,
            "all_spots": SPOTS,
            "kb_beaufort": KNOWLEDGE["beaufort_scale"],
            "kb_glossary": KNOWLEDGE["glossary"],
            "kb_sail_matrix": KNOWLEDGE["sail_matrix"]
        }
    )


@app.get("/api/vibe")
async def get_vibe_api(spot: str, weight: int, level: str = "intermediate"):
    try:
        report_data = await get_shred_report(spot, str(weight), level)
        vibe = await get_ai_recommendation(report_data, weight, spot, level)
        return {"vibe": vibe}
    except Exception as e:
        print(f"Vibe Error: {e}")
        return {"vibe": "The Legend is speechless..."}
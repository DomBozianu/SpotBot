from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from engine import get_shred_report, SPOTS
import json

app = FastAPI()
templates = Jinja2Templates(directory="templates")

KB_PATH = "spotbot_knowledge/general/knowledge_base.json"

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    with open(KB_PATH, "r") as f:
        knowledge = json.load(f)

    spot_key = request.query_params.get("spot")
    weight = request.query_params.get("weight", "75")
    
    if weight == "": weight = "75"
    
    report_data = None
    if spot_key:
        report_data = await get_shred_report(spot_key, user_weight=weight)
    
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "report": report_data, 
            "user_weight": weight, 
            "all_spots": SPOTS,
            "kb_beaufort":knowledge["beaufort_scale"],
            "kb_glossary": knowledge["glossary"]
        }
    )

@app.get("/api/vibe")
async def get_vibe_api(spot: str, weight: int):
    from engine import get_ai_recommendation
    try:
        report_data = await get_shred_report(spot, user_weight=str(weight))
        vibe = await get_ai_recommendation(report_data, weight)
        return {"vibe": vibe}
    except Exception as e:
        return {"vibe": "The Legend is speechless..."}
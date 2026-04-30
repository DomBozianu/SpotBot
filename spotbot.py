from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from engine import get_shred_report, SPOTS  # <-- This pulls from your new engine.py

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    spot_key = request.query_params.get("spot")
    weight = request.query_params.get("weight")
    if not weight or weight == "":
        weight = "75"    
    report_data = None
    if spot_key:
        report_data = await get_shred_report(spot_key, user_weight=weight)
    
    # Modern FastAPI/Starlette style:
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"report": report_data, "user_weight": weight, "all_spots": SPOTS}
    )

@app.get("/api/vibe")
async def get_vibe_api(spot: str, weight: int):
    # This mimics the logic in your home route but returns JSON
    from engine import get_shred_report, get_ai_recommendation
    
    try:
        report_data = await get_shred_report(spot, user_weight=weight)
        vibe = await get_ai_recommendation(report_data, weight)
        return {"vibe": vibe}
    except Exception as e:
        return {"vibe": "The Legend is speechless... (Error calculating vibe)"}
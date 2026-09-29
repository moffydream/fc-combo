"""대시보드 서버:  uvicorn server:app --port 8010  → http://localhost:8010"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import analytics
from db import connect

app = FastAPI(title="FC온라인 감독모드 조합 분석")
con = connect()
STATIC = Path(__file__).parent / "static"


def _date_from(days: Optional[int]):
    if not days:
        return None
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


@app.get("/")
def index():
    for p in (STATIC / "dashboard.html", Path(__file__).parent / "dashboard.html"):
        if p.exists():
            return FileResponse(p)
    raise HTTPException(404, "dashboard.html을 static 폴더나 server.py와 같은 폴더에 두세요")


@app.get("/api/status")
def status():
    return analytics.status(con)


@app.get("/api/players")
def players(q: str):
    if len(q.strip()) < 1:
        return []
    return analytics.search_players(con, q.strip())


@app.get("/api/formations")
def formations(anchor: int, pos: int, days: Optional[int] = None,
               gmin: Optional[float] = None, gmax: Optional[float] = None):
    return analytics.formations(con, anchor, pos, _date_from(days), gmin, gmax)


@app.get("/api/combos")
def combos(anchor: int, pos: int, formation: str, slots: str,
           days: Optional[int] = None, gmin: Optional[float] = None,
           gmax: Optional[float] = None, min_games: int = 10, k: int = 20):
    try:
        slot_list = [int(s) for s in slots.split(",") if s][:2]
    except ValueError:
        raise HTTPException(400, "slots는 포지션 코드를 쉼표로 구분해 주세요 (예: 15,13)")
    if not slot_list:
        raise HTTPException(400, "비교할 포지션을 1~2개 선택해 주세요")
    return analytics.combos(con, anchor, pos, formation, slot_list, _date_from(days),
                            gmin, gmax, min_games, k)

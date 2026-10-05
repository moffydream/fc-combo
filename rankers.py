"""감독모드 랭커 스쿼드 수집기 (하루 1회용).

데이터센터 랭킹(상위 1만 명)에서 구단주 목록을 받고, 각 구단주의 스쿼드에서
선수별 참여도·개인 전술·발동 팀컬러·강화를 읽는다. 닉네임은 넥슨 오픈 API로 ouid로 바꿔
이미 모은 경기와 연결할 수 있게 한다.

개인정보 취급: 계정번호(sn)·캐릭터ID·닉네임은 이 비공개 DB(Actions 캐시)에만 두고
공개 사이트에는 싣지 않는다.

  python rankers.py --probe            # 1페이지 + 첫 구단주 1명만 받아 결과를 출력 (저장 안 함)
  python rankers.py --pages 500        # 랭킹 1만 명 + 스쿼드 수집
"""
import argparse
import asyncio
import html
import json
import os
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

BASE = "https://fconline.nexon.com"
API = "https://open.api.nexon.com"
DB_PATH = os.environ.get("FC_RANK_DB", str(Path(__file__).parent / "rankers.db"))
RANK_PARAMS = dict(rt="manager", n4seasonno=0, tc_01=0, tc_02=0, tc_l_01=0, tc_l_02=0, tc_c_01=0, tc_c_02=0,
                   tc_01_cnt_s=1, tc_01_cnt_e=11, tc_02_cnt_s=1, tc_02_cnt_e=11, formation_01="-",
                   formation_02="-", cv_s=0, cv_e="18000000000000000000", tier_s=3100, tier_e=800,
                   rank_s=1, rank_e=10000)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/datacenter/rank_m",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rankers(
  sn TEXT PRIMARY KEY, nickname TEXT, rank INTEGER, score REAL, club_value INTEGER,
  w INTEGER, d INTEGER, l INTEGER, team_color TEXT, formation TEXT,
  char_id TEXT, ouid TEXT, rank_at TEXT, squad_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS squads(
  sn TEXT, day TEXT, coach TEXT, total_price INTEGER, PRIMARY KEY(sn, day));
-- 선발 11명만. strategy는 개인 전술 [[코드, 값], ...], tc는 발동 팀컬러 [[id, 이름, 레벨], ...]
CREATE TABLE IF NOT EXISTS squad_players(
  sn TEXT, day TEXT, spid INTEGER, role TEXT, grade INTEGER, part_att INTEGER, part_def INTEGER,
  strategy TEXT, tc TEXT, PRIMARY KEY(sn, day, spid));
CREATE INDEX IF NOT EXISTS ix_sp_spid ON squad_players(spid);
CREATE INDEX IF NOT EXISTS ix_rankers_ouid ON rankers(ouid);
"""


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def parse_rank_page(page):
    """rank_inner 응답 HTML → 구단주 목록"""
    out = []
    for block in re.split(r'<div class="tr">', page)[1:]:
        sm = re.search(r'data-sn="(\d+)"[^>]*>([^<]+)<', block)
        if not sm:
            continue
        g = lambda pat: (re.search(pat, block, re.S) or [None, None])[1]
        rank = g(r'class="td rank_no">\s*(\d+)')
        price = g(r'class="price" alt="([\d,]+)"')
        score = g(r'class="td rank_r_win_point">\s*([\d.]+)')
        wdl = g(r'class="bottom">(.*?)</span>')
        tc = g(r'class="inner">(.*?)<small>')
        form = g(r'class="td formation">\s*([^<]+)<')
        nums = [int(x) for x in re.findall(r"\d+", strip(wdl or ""))]
        out.append({
            "sn": sm.group(1), "nickname": html.unescape(sm.group(2)).strip(),
            "rank": int(rank) if rank else None,
            "club_value": int(price.replace(",", "")) if price else None,
            "score": float(score) if score else None,
            "w": nums[0] if len(nums) > 0 else None, "d": nums[1] if len(nums) > 1 else None,
            "l": nums[2] if len(nums) > 2 else None,
            "team_color": strip(tc) if tc else None, "formation": form.strip() if form else None,
        })
    return out


def find_char_id(page):
    """구단주 팝업 HTML에서 strCharacterID(24자리 16진수)를 찾는다"""
    m = re.search(r"[Cc]haracter[Ii][Dd]\W{0,6}([0-9a-fA-F]{24})\b", page)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9a-f]{24})\b", page)
    return m.group(1) if m else None


def parse_squad(data):
    """SquadGetUserInfo JSON → (감독, 구단 총액, 선발 선수 목록)"""
    if isinstance(data, str):
        data = json.loads(data)
    players = []
    for p in data.get("players") or []:
        if p.get("state") != 0:          # 0: 선발, 1: 교체 명단
            continue
        tcs = []
        for k in ("teamColor1", "teamColor2", "teamColor3"):
            t = (p.get("teamColor") or {}).get(k) or {}
            if t.get("id") not in (None, "", 0, "0"):
                tcs.append([t.get("id"), t.get("name"), t.get("lv")])
        part = p.get("participation") or [None, None]
        players.append({
            "spid": int(p["spid"]), "role": (p.get("role") or "").upper(), "grade": p.get("buildUp"),
            "part_att": int(part[0]) if part and part[0] not in (None, "") else None,
            "part_def": int(part[1]) if len(part) > 1 and part[1] not in (None, "") else None,
            "strategy": p.get("strategy") or [], "tc": tcs,
        })
    return data.get("coach"), data.get("totalPrice"), players


class Web:
    def __init__(self, rps):
        self.c = httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)
        self.gap, self.next = 1 / rps, 0.0
        self.lock = asyncio.Lock()

    async def get(self, url, params=None, referer=None):
        async with self.lock:
            now = time.monotonic()
            if self.next > now:
                await asyncio.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap
        h = {"Referer": referer} if referer else None
        for attempt in range(3):
            try:
                r = await self.c.get(url, params=params, headers=h)
                if r.status_code == 200:
                    return r
                if r.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                return r
            except httpx.HTTPError:
                await asyncio.sleep(3 * (attempt + 1))
        return None

    async def close(self):
        await self.c.aclose()


async def fetch_squad(web, sn):
    r = await web.get(f"{BASE}/profile/squad/popup/{sn}", referer=f"{BASE}/datacenter/rank_m")
    if not r or r.status_code != 200:
        return None, None, f"팝업 HTTP {r.status_code if r else '실패'}"
    cid = find_char_id(r.text)
    if not cid:
        return None, None, "팝업에서 캐릭터ID를 못 찾음"
    s = await web.get(f"{BASE}/datacenter/SquadGetUserInfo",
                      params=dict(strTeamType=1, n1Type=1, n8NexonSN=sn, strCharacterID=cid),
                      referer=f"{BASE}/profile/squad/popup/{sn}")
    if not s or s.status_code != 200:
        return cid, None, f"스쿼드 HTTP {s.status_code if s else '실패'}"
    try:
        return cid, parse_squad(s.json() if s.text.strip().startswith(("{", "[")) else json.loads(s.text)), None
    except Exception as e:  # noqa: BLE001
        return cid, None, f"스쿼드 해석 실패: {e} / 응답 앞부분: {s.text[:120]!r}"


async def nick_to_ouid(nick, key):
    async with httpx.AsyncClient(headers={"x-nxopen-api-key": key}, timeout=15) as c:
        r = await c.get(f"{API}/fconline/v1/id", params={"nickname": nick})
        return r.json().get("ouid") if r.status_code == 200 else None


async def probe(web):
    r = await web.get(f"{BASE}/datacenter/rank_inner", params={**RANK_PARAMS, "n4pageno": 1})
    print(f"[probe] 랭킹 1페이지 HTTP {r.status_code if r else '실패'}")
    rows = parse_rank_page(r.text) if r else []
    print(f"[probe] 구단주 {len(rows)}명 읽음. 첫 줄: "
          f"{ {k: v for k, v in rows[0].items() if k not in ('sn',)} if rows else '없음'}")
    if not rows:
        print(f"[probe] 응답 앞부분: {r.text[:300]!r}" if r else "")
        return
    cid, squad, err = await fetch_squad(web, rows[0]["sn"])
    print(f"[probe] 캐릭터ID {'찾음' if cid else '못 찾음'}" + (f", 오류: {err}" if err else ""))
    if squad:
        coach, price, players = squad
        print(f"[probe] 스쿼드 선발 {len(players)}명, 감독 {coach}")
        for p in players[:3]:
            print(f"[probe]   {p['role']} spid {p['spid']} {p['grade']}강 참여도 {p['part_att']}/{p['part_def']} "
                  f"전술 {p['strategy']} 팀컬러 {[t[1] for t in p['tc']]}")
    elif err and "팝업" in err:
        pr = await web.get(f"{BASE}/profile/squad/popup/{rows[0]['sn']}")
        txt = pr.text if pr else ""
        i = txt.lower().find("character")
        print(f"[probe] 팝업 길이 {len(txt)}, 'character' 주변: {txt[max(0, i - 150): i + 150]!r}" if i >= 0
              else f"[probe] 팝업 앞부분: {txt[:300]!r}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--pages", type=int, default=500, help="랭킹 페이지 수 (페이지당 20명)")
    ap.add_argument("--squads", type=int, default=10000, help="이번 실행에서 받을 스쿼드 수")
    ap.add_argument("--rps", type=float, default=2.0, help="데이터센터 초당 요청 수 (낮게 유지)")
    ap.add_argument("--minutes", type=float, default=300, help="전체 실행 시간 상한")
    ap.add_argument("--keep-days", type=int, default=10)
    a = ap.parse_args()

    web = Web(a.rps)
    start = time.time()
    try:
        if a.probe:
            await probe(web)
            return
        con = connect()
        today = date.today().isoformat()
        # 1) 랭킹 목록
        got = 0
        for page in range(1, a.pages + 1):
            r = await web.get(f"{BASE}/datacenter/rank_inner", params={**RANK_PARAMS, "n4pageno": page})
            rows = parse_rank_page(r.text) if r and r.status_code == 200 else []
            if not rows:
                print(f"[rank] {page}페이지에서 목록이 비어 중단")
                break
            for x in rows:
                con.execute("INSERT INTO rankers(sn, nickname, rank, score, club_value, w, d, l, team_color, formation, rank_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sn) DO UPDATE SET nickname=excluded.nickname, "
                            "rank=excluded.rank, score=excluded.score, club_value=excluded.club_value, w=excluded.w, "
                            "d=excluded.d, l=excluded.l, team_color=excluded.team_color, formation=excluded.formation, "
                            "rank_at=excluded.rank_at",
                            (x["sn"], x["nickname"], x["rank"], x["score"], x["club_value"], x["w"], x["d"], x["l"],
                             x["team_color"], x["formation"], today))
            got += len(rows)
            con.commit()
        print(f"[rank] 구단주 {got}명 갱신")

        # 2) 닉네임 → ouid (아직 없는 사람만, 넥슨 오픈 API)
        key = (os.environ.get("NEXON_API_KEYS") or os.environ.get("NEXON_API_KEY") or "").split(",")[0].strip()
        if key:
            todo = [r for r in con.execute("SELECT sn, nickname FROM rankers WHERE ouid IS NULL AND rank_at=?", (today,))]
            for i in range(0, len(todo), 20):
                part = todo[i:i + 20]
                ouids = await asyncio.gather(*(nick_to_ouid(n, key) for _, n in part))
                con.executemany("UPDATE rankers SET ouid=? WHERE sn=?", [(o, s) for (s, _), o in zip(part, ouids) if o])
                con.commit()
            print(f"[ouid] {len(todo)}명 변환 시도")

        # 3) 스쿼드 (오늘 아직 안 받은 사람, 순위 순)
        todo = [r[0] for r in con.execute(
            "SELECT sn FROM rankers WHERE rank_at=? AND (squad_at IS NULL OR squad_at < ?) ORDER BY rank LIMIT ?",
            (today, time.time() - 20 * 3600, a.squads))]
        ok = fail = streak = 0
        reasons = {}
        for sn in todo:
            if time.time() - start > a.minutes * 60:
                print("[squad] 시간 상한 도달")
                break
            cid, squad, err = await fetch_squad(web, sn)
            if squad:
                coach, price, players = squad
                con.execute("UPDATE rankers SET char_id=?, squad_at=? WHERE sn=?", (cid, time.time(), sn))
                con.execute("INSERT OR REPLACE INTO squads VALUES(?,?,?,?)", (sn, today, coach, price))
                con.executemany("INSERT OR REPLACE INTO squad_players VALUES(?,?,?,?,?,?,?,?,?)",
                                [(sn, today, p["spid"], p["role"], p["grade"], p["part_att"], p["part_def"],
                                  json.dumps(p["strategy"], ensure_ascii=False), json.dumps(p["tc"], ensure_ascii=False))
                                 for p in players])
                ok += 1
                streak = 0
            else:
                fail += 1
                streak += 1
                reasons[err] = reasons.get(err, 0) + 1
                if streak >= 20:
                    print("[squad] 20건 연속 실패로 중단")
                    break
            if (ok + fail) % 50 == 0:
                con.commit()
        con.commit()
        cutoff = (date.today() - timedelta(days=a.keep_days)).isoformat()
        con.execute("DELETE FROM squad_players WHERE day < ?", (cutoff,))
        con.execute("DELETE FROM squads WHERE day < ?", (cutoff,))
        con.commit()
        print(f"[squad] 성공 {ok}, 실패 {fail} {reasons if reasons else ''}")
        n_ouid = con.execute("SELECT COUNT(*) FROM rankers WHERE ouid IS NOT NULL").fetchone()[0]
        print(f"[summary] 구단주 {got}명, ouid 확보 {n_ouid}명, 오늘 스쿼드 {ok}건, {(time.time() - start) / 60:.0f}분")
    finally:
        await web.close()


if __name__ == "__main__":
    asyncio.run(main())

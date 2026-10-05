"""선수 카드 정보 수집기 (공통점 분석용).

넥슨 오픈 API 메타데이터에는 선수 능력치가 없어서, FC온라인 데이터센터의 선수 상세 페이지에서
키·몸무게·체형·양발·능력치·특성을 읽어 온다. 카드 정보는 거의 바뀌지 않으므로 한 번 받으면
30일 동안 다시 받지 않고, 매 실행마다 소량씩만 요청한다.

  python cards.py --max 300 --rps 1
"""
import argparse
import asyncio
import html
import json
import re
import time

import httpx

from db import connect

URL = "https://m.fconline.nexon.com/datacenter/playerinfo"
STATS = ["속력", "가속력", "골 결정력", "슛 파워", "중거리 슛", "위치 선정", "발리슛", "페널티 킥",
         "짧은 패스", "시야", "크로스", "긴 패스", "프리킥", "커브", "드리블", "볼 컨트롤", "민첩성",
         "밸런스", "반응 속도", "대인 수비", "태클", "가로채기", "헤더", "슬라이딩 태클", "몸싸움",
         "스태미너", "적극성", "점프", "침착성"]
POSITIONS = "GK|SW|RWB|RB|RCB|CB|LCB|LB|LWB|RDM|CDM|LDM|RM|RCM|CM|LCM|LM|RAM|CAM|LAM|RF|CF|LF|RW|RS|ST|LS|LW"

SCHEMA = """
CREATE TABLE IF NOT EXISTS card_info(
  sp_id INTEGER PRIMARY KEY, ok INTEGER, ovr INTEGER, main_pos TEXT, height INTEGER, weight INTEGER,
  body TEXT, foot_l INTEGER, foot_r INTEGER, stats TEXT, traits TEXT, fetched_at REAL, main_foot TEXT);
"""


def migrate(con):
    """이전 버전 테이블에 주발 열을 추가하고, 양발 숫자가 다른 카드는 숫자로 주발을 채운다."""
    try:
        con.execute("ALTER TABLE card_info ADD COLUMN main_foot TEXT")
    except Exception:
        pass
    con.execute("UPDATE card_info SET main_foot = CASE WHEN foot_l > foot_r THEN 'L' ELSE 'R' END "
                "WHERE ok = 1 AND main_foot IS NULL AND foot_l != foot_r")
    con.commit()


def main_foot(page, foot_l, foot_r):
    """페이지에서 굵게 표시된 쪽이 주발. 표시를 못 찾으면 숫자가 큰 쪽, 둘 다 같으면 None."""
    km = re.search(r"\d{2,3}\s*kg", page)          # 키·몸무게·양발이 모여 있는 부분만 본다
    if km:
        page = page[km.start():km.start() + 800]
    bold = r"<(?:b|strong|em)\b[^>]*>\s*"
    if re.search(bold + r"R\s*\d", page):
        return "R"
    if re.search(bold + r"L\s*\d", page):
        return "L"
    m = re.search(r'class="[^"]*(?:strong|bold|main|on|active|select)[^"]*"[^>]*>\s*([LR])\s*\d', page)
    if m:
        return m.group(1)
    if foot_l != foot_r:
        return "L" if foot_l > foot_r else "R"
    return None


def to_text(page):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = html.unescape(t)
    return re.sub(r"[ \t\r\f\v]+", " ", t)


def parse(page):
    """선수 상세 페이지 HTML → dict. 핵심 항목(키/몸무게)을 못 찾으면 None."""
    text = to_text(page)
    m = re.search(r"(\d{3})\s*cm\s*(\d{2,3})\s*kg\s*(마름|보통|건장)\s*L\s*(\d)\s*[–\-]\s*R\s*(\d)", text)
    if not m:
        return None
    out = {"height": int(m.group(1)), "weight": int(m.group(2)), "body": m.group(3),
           "foot_l": int(m.group(4)), "foot_r": int(m.group(5))}
    stats = {}
    start = text.find("속력")
    seg = text[start:] if start >= 0 else text
    for name in STATS:
        sm = re.search(r"(?<!GK )" + re.escape(name) + r"\s*\n\s*(\d{1,3})\b", seg)
        if sm:
            stats[name] = int(sm.group(1))
    out["stats"] = stats
    om = re.search(r"\b(" + POSITIONS + r")\s*\n?\s*(\d{2,3})\b", text[:m.start()][-400:])
    out["main_pos"], out["ovr"] = (om.group(1), int(om.group(2))) if om else (None, None)
    traits = []
    for tag in re.findall(r"<img[^>]*/traits/[^>]*>", page, flags=re.I):
        am = re.search(r'alt="([^"]+)"', tag)
        if am and am.group(1) not in traits:
            traits.append(html.unescape(am.group(1)).strip())
    out["traits"] = traits
    out["main_foot"] = main_foot(page, out["foot_l"], out["foot_r"])
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=300, help="이번 실행에서 받을 최대 카드 수")
    ap.add_argument("--rps", type=float, default=1.0, help="초당 요청 수 (데이터센터 부담을 줄이기 위해 낮게)")
    ap.add_argument("--min-games", type=int, default=20, help="이 이상 출전한 카드만 대상")
    ap.add_argument("--refresh-days", type=float, default=30)
    ap.add_argument("--minutes", type=float, default=0, help="이 시간이 지나면 받던 것까지 저장하고 종료 (0=제한 없음)")
    ap.add_argument("--test", type=int, help="spid 하나만 받아서 파싱 결과를 출력하고 종료 (점검용)")
    a = ap.parse_args()

    if a.test:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (fc-combo stats)"}) as c:
            r = await c.get(URL, params={"spid": a.test})
        print(f"[test] HTTP {r.status_code}, 최종 주소 {r.url}")
        print(json.dumps(parse(r.text), ensure_ascii=False, indent=1))
        return

    con = connect()
    con.executescript(SCHEMA)
    migrate(con)
    stale = time.time() - a.refresh_days * 86400
    # 출전이 많은 카드부터, 정보가 없거나 오래된 것만
    cond_args = (stale, time.time() - 86400, time.time() - 7 * 86400, a.min_games)
    remaining = con.execute(
        "SELECT COUNT(*) FROM (SELECT p.sp_id FROM players p LEFT JOIN card_info c ON c.sp_id = p.sp_id "
        "WHERE p.pos NOT IN (0, 28) AND (c.sp_id IS NULL OR c.fetched_at < ? "
        "OR (c.ok = 0 AND c.fetched_at < ?) OR (c.ok = 1 AND c.main_foot IS NULL AND c.fetched_at < ?)) "
        "GROUP BY p.sp_id HAVING COUNT(*) >= ?)", cond_args).fetchone()[0]
    print(f"[cards] 받아야 할 카드 {remaining}장 중 이번에 최대 {min(remaining, a.max)}장")
    todo = [r[0] for r in con.execute(
        "SELECT p.sp_id FROM players p LEFT JOIN card_info c ON c.sp_id = p.sp_id "
        "WHERE p.pos NOT IN (0, 28) AND (c.sp_id IS NULL OR c.fetched_at < ? "
        "OR (c.ok = 0 AND c.fetched_at < ?) OR (c.ok = 1 AND c.main_foot IS NULL AND c.fetched_at < ?)) "
        "GROUP BY p.sp_id HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC LIMIT ?",
        (stale, time.time() - 86400, time.time() - 7 * 86400, a.min_games, a.max))]
    if not todo:
        print("[cards] 새로 받을 카드 없음")
        return
    ok = fail = streak = 0
    started = time.time()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (fc-combo stats)"}) as c:
        for i, sp in enumerate(todo):
            if a.minutes and time.time() - started > a.minutes * 60:
                print(f"[cards] 시간 제한({a.minutes:.0f}분) 도달, 여기까지 저장합니다")
                break
            try:
                r = await c.get(URL, params={"spid": sp})
                info = parse(r.text) if r.status_code == 200 else None
            except httpx.HTTPError:
                info = None
            if info:
                ok += 1
                streak = 0
                if ok == 1:
                    print(f"[cards] 첫 파싱 예시 {sp}: 키 {info['height']} 몸무게 {info['weight']} {info['body']} "
                          f"L{info['foot_l']}-R{info['foot_r']} 주발 {info['main_foot']} OVR {info['ovr']} 능력치 {len(info['stats'])}개 특성 {info['traits']}")
                con.execute("INSERT OR REPLACE INTO card_info VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sp, 1, info["ovr"], info["main_pos"], info["height"], info["weight"],
                             info["body"], info["foot_l"], info["foot_r"],
                             json.dumps(info["stats"], ensure_ascii=False),
                             json.dumps(info["traits"], ensure_ascii=False), time.time(), info["main_foot"]))
            else:
                fail += 1
                streak += 1
                con.execute("INSERT OR REPLACE INTO card_info(sp_id, ok, fetched_at) VALUES(?,?,?)",
                            (sp, 0, time.time()))
                if fail >= 10 and ok == 0:
                    print("[cards] 처음 10건이 모두 실패해서 중단합니다. 페이지 구조가 바뀌었거나 접근이 막혔을 수 있습니다.")
                    break
                if streak >= 20:
                    print("[cards] 20건 연속 실패해서 중단합니다. 요청이 차단됐을 수 있으니 속도를 낮춰 다음에 이어 받으세요.")
                    break
            if i % 50 == 49:
                con.commit()
            await asyncio.sleep(1 / a.rps)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM card_info WHERE ok=1").fetchone()[0]
    print(f"[cards] 성공 {ok}건, 실패 {fail}건, 보유 카드 정보 {total}개")


if __name__ == "__main__":
    asyncio.run(main())

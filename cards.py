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
PARSER_VERSION = "4"   # 읽는 방식을 고칠 때 올리면 기존 카드를 다시 받는다
STATS = ["속력", "가속력", "골 결정력", "슛 파워", "중거리 슛", "위치 선정", "발리슛", "페널티 킥",
         "짧은 패스", "시야", "크로스", "긴 패스", "프리킥", "커브", "드리블", "볼 컨트롤", "민첩성",
         "밸런스", "반응 속도", "대인 수비", "태클", "가로채기", "헤더", "슬라이딩 태클", "몸싸움",
         "스태미너", "적극성", "점프", "침착성",
         "GK 다이빙", "GK 핸들링", "GK 킥", "GK 반응속도", "GK 위치 선정"]
POSITIONS = "GK|SW|RWB|RB|RCB|CB|LCB|LB|LWB|RDM|CDM|LDM|RM|RCM|CM|LCM|LM|RAM|CAM|LAM|RF|CF|LF|RW|RS|ST|LS|LW"

SCHEMA = """
CREATE TABLE IF NOT EXISTS card_info(
  sp_id INTEGER PRIMARY KEY, ok INTEGER, ovr INTEGER, main_pos TEXT, height INTEGER, weight INTEGER,
  body TEXT, foot_l INTEGER, foot_r INTEGER, stats TEXT, traits TEXT, fetched_at REAL, main_foot TEXT,
  pay INTEGER, clubs TEXT, body_unique INTEGER, trait_meta TEXT);
"""


def migrate(con):
    """이전 버전 테이블에 주발 열을 추가하고, 양발 숫자가 다른 카드는 숫자로 주발을 채운다."""
    for col in ("main_foot TEXT", "pay INTEGER", "clubs TEXT", "body_unique INTEGER", "trait_meta TEXT"):
        try:
            con.execute(f"ALTER TABLE card_info ADD COLUMN {col}")
        except Exception:
            pass
    con.execute("UPDATE card_info SET main_foot = CASE WHEN foot_l > foot_r THEN 'L' ELSE 'R' END "
                "WHERE ok = 1 AND main_foot IS NULL AND foot_l != foot_r")
    # 파서가 바뀌면 클럽 경력·급여를 한 번 다시 받도록 표시 (clubs가 비면 수집 대상이 됨)
    con.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    ver = con.execute("SELECT value FROM kv WHERE key='cards_parser'").fetchone()
    if not ver or ver[0] != PARSER_VERSION:
        con.execute("UPDATE card_info SET clubs = NULL, pay = NULL, fetched_at = 0 WHERE ok = 1")
        con.execute("UPDATE card_info SET fetched_at = 0 WHERE ok = 0")
        con.execute("INSERT OR REPLACE INTO kv VALUES('cards_parser', ?)", (PARSER_VERSION,))
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


def parse_clubs(text):
    """'연도 / 클럽 / 임대 여부' 표에서 클럽 경력 목록 [[클럽, 임대여부], ...].
    연도와 클럽이 줄을 나눠 나오는 형태와 '2010 ~ 2011 맨체스터 시티 임대'처럼 한 줄에 나오는 형태를 모두 처리."""
    start = text.find("임대 여부")
    if start < 0:
        start = text.find("클럽 경력")
        if start < 0:
            return []
    end = text.find("플레이 평균 기록", start)
    seg = text[start + 5: end if end > 0 else start + 3000]
    clubs = []
    for line in (l.strip(" \t*-•·") for l in seg.split("\n")):
        if not line:
            continue
        ym = re.match(r"(\d{4})\s*~\s*(\d{4})?\s*(.*)$", line)
        if ym:
            line = ym.group(3).strip()
            if not line:
                continue                          # 연도만 있는 줄
        if line in ("임대", "연도", "클럽", "임대 여부"):
            if line == "임대" and clubs:
                clubs[-1][1] = 1
            continue
        if len(line) > 40:                        # 표가 끝난 뒤의 긴 문장
            break
        loan = 0
        if line.endswith(" 임대"):                 # 한 줄 형태의 임대 표시
            line, loan = line[:-3].strip(), 1
        clubs.append([line, loan])
    out = {}
    for name, loan in clubs:                     # 같은 클럽이 정식·임대로 모두 있으면 정식 우선
        out[name] = min(out.get(name, 1), loan)
    return [[n, l] for n, l in out.items()]


def parse(page, name=None):
    """선수 상세 페이지 HTML → dict. 핵심 항목(키/몸무게)을 못 찾으면 None."""
    text = to_text(page)
    m = re.search(r"(\d{3})\s*cm\s*(\d{2,3})\s*kg\s*(마름|보통|건장)\s*(\(고유\))?\s*L\s*(\d)\s*[–\-]\s*R\s*(\d)", text)
    if not m:
        return None
    out = {"height": int(m.group(1)), "weight": int(m.group(2)), "body": m.group(3),
           "body_unique": 1 if m.group(4) else 0,
           "foot_l": int(m.group(5)), "foot_r": int(m.group(6))}
    stats = {}
    start = text.find("속력")
    seg = text[start:] if start >= 0 else text
    for stat in STATS:
        pat = (r"(?<!GK )" if not stat.startswith("GK") else "") + re.escape(stat) + r"\s*\n\s*(\d{1,3})\b"
        sm = re.search(pat, seg)
        if sm:
            stats[stat] = int(sm.group(1))
    out["stats"] = stats
    om = re.search(r"\b(" + POSITIONS + r")\s*\n?\s*(\d{2,3})\b", text[:m.start()][-400:])
    out["main_pos"], out["ovr"] = (om.group(1), int(om.group(2))) if om else (None, None)
    # 특성: 이름(alt) + 아이콘 주소 + 주변 class 단서 + '(AI)' 표시 여부
    traits, meta = [], {}
    for tm in re.finditer(r"<img[^>]*/traits/[^>]*>", page, flags=re.I):
        tag = tm.group(0)
        am = re.search(r'alt="([^"]+)"', tag)
        if not am:
            continue
        tname = html.unescape(am.group(1)).strip()
        if tname in meta:
            continue
        sm = re.search(r'src="([^"]+)"', tag)
        ctx = page[max(0, tm.start() - 300): tm.end()]
        classes = " ".join(re.findall(r'class="([^"]*)"', ctx)[-3:])
        ai = bool(re.search(re.escape(tname) + r"\s*\(AI\)", text))
        traits.append(tname)
        meta[tname] = [sm.group(1) if sm else "", 1 if ai else 0, classes[:120]]
    out["traits"] = traits
    out["trait_meta"] = meta
    out["main_foot"] = main_foot(page, out["foot_l"], out["foot_r"]) or "?"  # ?: 판단 불가 (다시 받지 않음)
    pm = re.search(r"급여\s*[:：]?\s*\n?\s*(\d{1,2})\b", text)
    if not pm and name:
        # 카드 머리말 '126 ST 1 엘링 홀란 34' 에서 이름 바로 뒤 숫자가 급여
        pm = re.search(re.escape(name) + r"\s*\n\s*(\d{1,2})\s*\n", text[:m.start()])
    if not pm:
        # 이름 표기가 메타데이터와 달라도 되도록: 'OVR / 포지션 / 강화 / 이름 / 급여' 순서만으로 찾기
        pm = re.search(r"\b\d{2,3}\s*\n\s*(?:" + POSITIONS + r")\s*\n\s*\d{1,2}\s*\n\s*[^\n]{2,30}?\s*\n\s*(\d{1,2})\s*\n",
                       text[:m.start()])
    out["pay"] = int(pm.group(1)) if pm else -1          # -1: 페이지에서 못 찾음
    out["clubs"] = parse_clubs(text)
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
        row = connect().execute("SELECT name FROM meta_player WHERE sp_id=?", (a.test,)).fetchone()
        print(json.dumps(parse(r.text, row[0] if row else None), ensure_ascii=False, indent=1))
        return

    con = connect()
    con.executescript(SCHEMA)
    migrate(con)
    stale = time.time() - a.refresh_days * 86400
    # 출전이 많은 카드부터, 정보가 없거나 오래된 것만
    cond_args = (stale, time.time() - 86400, time.time() - 7 * 86400, a.min_games)
    remaining = con.execute(
        "SELECT COUNT(*) FROM (SELECT p.sp_id FROM players p LEFT JOIN card_info c ON c.sp_id = p.sp_id "
        "WHERE p.pos != 28 AND (c.sp_id IS NULL OR c.fetched_at < ? "
        "OR (c.ok = 0 AND c.fetched_at < ?) OR (c.ok = 1 AND (c.main_foot IS NULL OR c.pay IS NULL OR c.clubs IS NULL OR c.trait_meta IS NULL) AND c.fetched_at < ?)) "
        "GROUP BY p.sp_id HAVING COUNT(*) >= ?)", cond_args).fetchone()[0]
    print(f"[cards] 받아야 할 카드 {remaining}장 중 이번에 최대 {min(remaining, a.max)}장")
    todo = [r[0] for r in con.execute(
        "SELECT p.sp_id FROM players p LEFT JOIN card_info c ON c.sp_id = p.sp_id "
        "WHERE p.pos != 28 AND (c.sp_id IS NULL OR c.fetched_at < ? "
        "OR (c.ok = 0 AND c.fetched_at < ?) OR (c.ok = 1 AND (c.main_foot IS NULL OR c.pay IS NULL OR c.clubs IS NULL OR c.trait_meta IS NULL) AND c.fetched_at < ?)) "
        "GROUP BY p.sp_id HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC LIMIT ?",
        (*cond_args, a.max))]
    if not todo:
        print("[cards] 새로 받을 카드 없음")
        return
    names = {}
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        names.update(con.execute(f"SELECT sp_id, name FROM meta_player WHERE sp_id IN ({','.join('?' * len(part))})",
                                 part).fetchall())
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
                info = parse(r.text, names.get(sp)) if r.status_code == 200 else None
            except httpx.HTTPError:
                info = None
            if info:
                ok += 1
                streak = 0
                if ok == 1:
                    print(f"[cards] 첫 파싱 예시 {sp}: 키 {info['height']} 몸무게 {info['weight']} {info['body']} "
                          f"{'(고유) ' if info['body_unique'] else ''}L{info['foot_l']}-R{info['foot_r']} 주발 {info['main_foot']} 급여 {info['pay']} "
                          f"클럽 {[c[0] for c in info['clubs']][:4]} 특성아이콘 {list(info['trait_meta'].values())[:1]} OVR {info['ovr']} 능력치 {len(info['stats'])}개 특성 {info['traits']}")
                con.execute("INSERT OR REPLACE INTO card_info VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sp, 1, info["ovr"], info["main_pos"], info["height"], info["weight"],
                             info["body"], info["foot_l"], info["foot_r"],
                             json.dumps(info["stats"], ensure_ascii=False),
                             json.dumps(info["traits"], ensure_ascii=False), time.time(), info["main_foot"], info["pay"],
                             json.dumps(info["clubs"], ensure_ascii=False), info["body_unique"],
                             json.dumps(info["trait_meta"], ensure_ascii=False)))
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
    r = con.execute("SELECT SUM(pay >= 0), SUM(clubs IS NOT NULL AND clubs != '[]'), SUM(clubs IS NULL), "
                    "SUM(trait_meta IS NOT NULL) FROM card_info WHERE ok=1").fetchone()
    print(f"[cards] 점검: 급여 읽음 {r[0] or 0}/{total}, 클럽 경력 읽음 {r[1] or 0}/{total} "
          f"(아직 안 받음 {r[2] or 0}), 특성 아이콘 정보 {r[3] or 0}/{total}")


if __name__ == "__main__":
    asyncio.run(main())

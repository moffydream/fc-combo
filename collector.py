"""감독모드 매치 수집기.

수집 방식 두 가지
- global   : 전체 매치 목록 API(/fconline/v1/match). 매치 타입에 따라 지원되지 않을 수 있음.
- snowball : 시드 유저의 감독모드 기록(/fconline/v1/user/match)에서 시작해,
             경기에서 만난 상대 유저로 계속 확장. 전체 목록 API가 막혀 있을 때 사용.
기본값은 snowball. 감독모드(52)는 전체 목록 API가 400을 반환하는 것으로 확인됨.

PowerShell 예시
  $env:NEXON_API_KEY = "키"
  python collector.py --probe --seed "내닉네임"                   # API 지원 여부 점검
  python collector.py --seed "내닉네임" --focus "보비 찰턴" --focus-season FSL
  # 이후 재실행 시 --seed 생략 가능. 개발 키 한도(하루 1,000건)에 닿으면 자정까지 자동 대기
"""
import argparse
import asyncio
import hashlib
import os
import random
import time
from datetime import date, datetime, timedelta

import httpx

from db import connect
from positions import POS_NAME, formation_sig

API = "https://open.api.nexon.com"
META = f"{API}/static/fconline/meta"
RESULT = {"승": "W", "무": "D", "패": "L"}


class RateLimiter:
    def __init__(self, rps):
        self.interval = 1.0 / rps
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


class Budget:
    """키(앱) 하나의 일일 호출 수를 DB에 기록해서 한도 전에 멈춘다. 날짜는 PC 로컬 날짜 기준."""
    def __init__(self, con, daily, key_id):
        self.con, self.daily, self.key_id = con, daily, key_id

    def used(self):
        r = self.con.execute("SELECT calls FROM api_usage_key WHERE day=? AND key_id=?",
                             (date.today().isoformat(), self.key_id)).fetchone()
        return r[0] if r else 0

    def remaining(self):
        return max(0, self.daily - self.used())

    def take(self):
        if self.remaining() <= 0:
            return False
        self.con.execute("INSERT INTO api_usage_key(day, key_id, calls) VALUES(?,?,1) "
                         "ON CONFLICT(day, key_id) DO UPDATE SET calls=calls+1",
                         (date.today().isoformat(), self.key_id))
        self.con.commit()
        return True


class Slot:
    """앱 하나에 해당하는 키. 초당 제한과 일일 한도를 따로 가진다."""
    def __init__(self, key, rps, con, daily):
        self.key_id = hashlib.sha1(key.encode()).hexdigest()[:8]
        self.client = httpx.AsyncClient(headers={"x-nxopen-api-key": key}, timeout=20)
        self.rl = RateLimiter(rps)
        self.budget = Budget(con, daily, self.key_id)
        self.exhausted = False

    def available(self):
        return not self.exhausted and self.budget.remaining() > 0


class Nexon:
    def __init__(self, keys, rps, con, daily):
        self.slots = [Slot(k, rps, con, daily) for k in keys]
        self._rr = 0

    @property
    def exhausted(self):
        return not any(s.available() for s in self.slots)

    @exhausted.setter
    def exhausted(self, value):
        if not value:
            for s in self.slots:
                s.exhausted = False

    def used(self):
        return sum(s.budget.used() for s in self.slots)

    def remaining(self):
        return sum(s.budget.remaining() for s in self.slots if not s.exhausted)

    def _pick(self):
        n = len(self.slots)
        for i in range(n):
            s = self.slots[(self._rr + i) % n]
            if s.available():
                self._rr = (self._rr + i + 1) % n
                return s
        return None

    async def get(self, url, params=None):
        for attempt in range(5):
            slot = self._pick()
            if slot is None or not slot.budget.take():
                if slot:
                    slot.exhausted = True
                    continue
                return None
            await slot.rl.wait()
            try:
                r = await slot.client.get(url, params=params)
            except httpx.HTTPError:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                slot.hits_429 = getattr(slot, "hits_429", 0) + 1
                if slot.hits_429 >= 3:  # 초당 제한이 아니라 일일 한도로 판단
                    if not slot.exhausted:
                        print(f"[quota] 키 {slot.key_id}가 계속 429를 반환해 오늘은 제외합니다.")
                    slot.exhausted = True
                await asyncio.sleep(1 + random.random())
                continue
            if r.status_code >= 500:
                await asyncio.sleep(min(60, 2 ** attempt) + random.random())
                continue
            print(f"[warn] {r.status_code} {url} {params} {r.text[:200]}")
            return None
        print(f"[warn] 재시도 초과: {url} {params}")
        return None

    async def raw(self, url, params=None):
        slot = self.slots[0]
        slot.budget.take()
        await slot.rl.wait()
        r = await slot.client.get(url, params=params)
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        return r.status_code, body

    async def close(self):
        for s in self.slots:
            await s.client.aclose()


async def sync_meta(api, con):
    sp = await api.get(f"{META}/spid.json")
    if sp:
        con.executemany("INSERT OR REPLACE INTO meta_player VALUES(?,?)",
                        [(x["id"], x["name"]) for x in sp])
    se = await api.get(f"{META}/seasonid.json")
    if se:
        con.executemany("INSERT OR REPLACE INTO meta_season VALUES(?,?)",
                        [(x["seasonId"], x["className"].split("(")[0].strip()) for x in se])
    pos = await api.get(f"{META}/spposition.json")
    if pos:
        for x in pos:
            if POS_NAME.get(x.get("spposition")) != x.get("desc"):
                print(f"[warn] 포지션 코드 불일치: {x} — positions.py를 확인하세요")
    con.commit()
    print(f"[meta] 선수 {len(sp or [])}명, 시즌 {len(se or [])}개")


def parse_detail(d):
    """match-detail JSON → (match, teams, players) 또는 (None, 사유)."""
    infos = d.get("matchInfo") or []
    if len(infos) != 2:
        return None, "팀 정보 2개 아님"
    teams, players = [], []
    for side, info in enumerate(infos):
        md = info.get("matchDetail") or {}
        if md.get("matchEndType") not in (0, None):
            return None, f"비정상 종료 {md.get('matchEndType')}"
        res = RESULT.get(md.get("matchResult"))
        if not res:
            return None, "결과 없음"
        shoot = info.get("shoot") or {}
        gf = shoot.get("goalTotalDisplay", shoot.get("goalTotal", 0)) or 0
        starters = [p for p in (info.get("player") or []) if p.get("spPosition") not in (None, 28)]
        if len(starters) < 11:
            return None, "선발 11명 미만"
        grades = [p.get("spGrade") or 0 for p in starters]
        teams.append(dict(side=side, ouid=info.get("ouid"), result=res, gf=gf,
                          formation=formation_sig([p["spPosition"] for p in starters]),
                          avg_grade=sum(grades) / len(grades)))
        for p in starters:
            st = p.get("status") or {}
            players.append((side, p["spId"], p["spPosition"], p.get("spGrade") or 0,
                            st.get("spRating") or 0, st.get("goal") or 0, st.get("assist") or 0))
    teams[0]["ga"], teams[1]["ga"] = teams[1]["gf"], teams[0]["gf"]
    teams[0]["opp"], teams[1]["opp"] = teams[1]["avg_grade"], teams[0]["avg_grade"]
    return (dict(match_id=d["matchId"], date=d.get("matchDate"), type=d.get("matchType")),
            teams, players), None


def save(con, match_id, detail):
    if not detail:
        return False  # 조회 실패는 다음에 다시 시도
    parsed, reason = parse_detail(detail)
    if not parsed:
        con.execute("INSERT OR IGNORE INTO skipped VALUES(?,?)", (match_id, reason))
        return False
    m, teams, players = parsed
    con.execute("INSERT OR IGNORE INTO matches VALUES(?,?,?)", (m["match_id"], m["date"], m["type"]))
    for t in teams:
        con.execute("INSERT OR IGNORE INTO teams VALUES(?,?,?,?,?,?,?,?,?)",
                    (m["match_id"], t["side"], t["ouid"], t["result"], t["gf"], t["ga"],
                     t["formation"], t["avg_grade"], t["opp"]))
    con.executemany("INSERT INTO players VALUES(?,?,?,?,?,?,?,?)",
                    [(m["match_id"], *p) for p in players])
    return True


def known_ids(con, ids):
    q = ",".join("?" * len(ids))
    rows = con.execute(f"SELECT match_id FROM matches WHERE match_id IN ({q}) "
                       f"UNION SELECT match_id FROM skipped WHERE match_id IN ({q})", ids + ids)
    return {r[0] for r in rows}


FOCUS = set()        # 우선 추적할 선수 spId
WINDOW_FROM = None   # 이 시각(문자열) 이전 경기는 저장하지 않음
CHUNK = 20


async def fetch_and_save(api, con, match_ids):
    ok = 0
    for i in range(0, len(match_ids), CHUNK):
        if api.exhausted:
            break
        chunk = match_ids[i:i + CHUNK]  # 키가 여러 개면 슬롯별 속도 제한으로 자동 분산
        details = await asyncio.gather(*(api.get(f"{API}/fconline/v1/match-detail", {"matchid": m})
                                         for m in chunk))
        now = time.time()
        for mid, d in zip(chunk, details):
            if d and WINDOW_FROM and (d.get("matchDate") or "") < WINDOW_FROM:
                con.execute("INSERT OR IGNORE INTO skipped VALUES(?,?)", (mid, "보관 기간 밖"))
                continue
            ok += save(con, mid, d)
            for info in (d or {}).get("matchInfo") or []:
                ouid = info.get("ouid")
                if not ouid:
                    continue
                con.execute("INSERT OR IGNORE INTO crawl_users(ouid, last_crawled, found_at) "
                            "VALUES(?,0,?)", (ouid, now))
                if FOCUS and any(p.get("spId") in FOCUS for p in info.get("player") or []):
                    con.execute("UPDATE crawl_users SET focus=1 WHERE ouid=?", (ouid,))
        con.commit()
    return ok


async def crawl_global(api, con, matchtype, max_pages, limit):
    """전체 매치 목록 API로 수집. 지원 안 되면 None 반환."""
    fresh, seen = [], set()
    for page in range(max_pages):
        ids = await api.get(f"{API}/fconline/v1/match",
                            {"matchtype": matchtype, "offset": page * limit,
                             "limit": limit, "orderby": "desc"})
        if ids is None:
            return None if page == 0 else fresh_report(con, fresh, await fetch_and_save(api, con, fresh))
        if not ids:
            break
        known = known_ids(con, ids)
        new = [i for i in ids if i not in known and i not in seen]
        seen.update(new)
        fresh += new
        if len(new) < len(ids) * 0.2:
            break
    return fresh_report(con, fresh, await fetch_and_save(api, con, fresh))


def fresh_report(con, fresh, ok):
    total = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"[crawl] 신규 {len(fresh)}건 중 저장 {ok}건, 누적 {total}경기")
    return ok


async def add_seeds(api, con, nicknames):
    for nick in nicknames:
        r = await api.get(f"{API}/fconline/v1/id", {"nickname": nick})
        if r and r.get("ouid"):
            con.execute("INSERT OR IGNORE INTO crawl_users(ouid, last_crawled, found_at) VALUES(?,0,?)",
                        (r["ouid"], time.time()))
            print(f"[seed] {nick} → {r['ouid'][:8]}…")
        else:
            print(f"[warn] 닉네임을 찾지 못했습니다: {nick}")
    con.commit()


async def crawl_snowball(api, con, matchtype, users_per_round, limit, recrawl_hours, cap=0):
    cutoff = time.time() - recrawl_hours * 3600
    users = [r[0] for r in con.execute(
        "SELECT ouid FROM crawl_users WHERE last_crawled < ? "
        "ORDER BY focus DESC, last_crawled ASC, found_at DESC LIMIT ?", (cutoff, users_per_round))]
    if not users:
        n = con.execute("SELECT COUNT(*) FROM crawl_users").fetchone()[0]
        print("[snowball] 조회할 유저가 없습니다. --seed 로 닉네임을 넣어 주세요." if n == 0
              else f"[snowball] 유저 {n}명 모두 최근 {recrawl_hours}시간 안에 조회됨. 다음 주기에 이어갑니다.")
        return 0
    lists = await asyncio.gather(*(api.get(f"{API}/fconline/v1/user/match",
                                           {"ouid": u, "matchtype": matchtype, "offset": 0, "limit": limit})
                                   for u in users))
    now = time.time()
    con.executemany("UPDATE crawl_users SET last_crawled=? WHERE ouid=?",
                    [(now, u) for u, lst in zip(users, lists) if lst is not None])
    ids = list(dict.fromkeys(m for lst in lists if lst for m in lst))
    known = known_ids(con, ids) if ids else set()
    fresh = [m for m in ids if m not in known]
    if cap and len(fresh) > cap:
        # 여러 유저의 최근 경기가 고르게 섞이도록 앞에서부터 자름 (목록은 유저별 최신순)
        fs = set(fresh)
        per_user = [[m for m in (lst or []) if m in fs] for lst in lists]
        mixed = [m for tier in zip(*[u + [None] * (limit - len(u)) for u in per_user]) for m in tier if m]
        fresh = list(dict.fromkeys(mixed))[:cap]
    ok = await fetch_and_save(api, con, fresh)
    pool, focus = con.execute("SELECT COUNT(*), SUM(focus) FROM crawl_users").fetchone()
    print(f"[snowball] 유저 {len(users)}명 조회, 유저 풀 {pool}명 (추적 선수 사용자 {focus or 0}명)")
    return fresh_report(con, fresh, ok)


async def probe(api, matchtype, seeds):
    print("── matchtype 메타데이터")
    st, body = await api.raw(f"{META}/matchtype.json")
    if st == 200:
        for x in body:
            mark = "  ← 현재 설정" if x.get("matchtype") == matchtype else ""
            print(f"  {x.get('matchtype')}: {x.get('desc')}{mark}")
    else:
        print(f"  조회 실패 {st} {body}")
    print("── 전체 매치 목록 API")
    for mt in dict.fromkeys([50, matchtype]):
        st, body = await api.raw(f"{API}/fconline/v1/match",
                                 {"matchtype": mt, "offset": 0, "limit": 10, "orderby": "desc"})
        print(f"  matchtype={mt}: {st} " + (f"{len(body)}건" if st == 200 else str(body)))
    for nick in seeds:
        print(f"── 유저 매치 API ({nick})")
        st, body = await api.raw(f"{API}/fconline/v1/id", {"nickname": nick})
        if st != 200:
            print(f"  ouid 조회 실패 {st} {body}")
            continue
        st, body = await api.raw(f"{API}/fconline/v1/user/match",
                                 {"ouid": body["ouid"], "matchtype": matchtype, "offset": 0, "limit": 10})
        print(f"  matchtype={matchtype}: {st} " + (f"{len(body)}건" if st == 200 else str(body)))
        if st == 200 and body:
            st, d = await api.raw(f"{API}/fconline/v1/match-detail", {"matchid": body[0]})
            parsed, reason = parse_detail(d) if st == 200 else (None, f"상세 {st}")
            print("  첫 경기 파싱: " + ("성공, 포메이션 " + parsed[1][0]["formation"] if parsed else f"실패 ({reason})"))


async def run_batch(api, con, a):
    start = time.time()
    saved = 0
    while True:
        if api.exhausted:
            print("[batch] API 한도 소진")
            break
        if time.time() - start > a.run_minutes * 60:
            print("[batch] 실행 시간 한도 도달")
            break
        if a.max_matches and saved >= a.max_matches:
            print("[batch] 이번 실행 목표 경기 수 도달")
            break
        cap = a.max_matches - saved if a.max_matches else 0
        got = await crawl_snowball(api, con, a.matchtype, a.users, a.limit, a.recrawl_hours, cap)
        if got == 0:
            waiting = con.execute("SELECT COUNT(*) FROM crawl_users WHERE last_crawled < ?",
                                  (time.time() - a.recrawl_hours * 3600,)).fetchone()[0]
            if waiting == 0:
                print("[batch] 지금 조회할 유저가 없음")
                break
        saved += got
    if a.window_days:
        n = prune(con, a.window_days)
        print(f"[prune] 보관 기간 지난 경기 {n}건 삭제")
    total = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"[batch] 이번 실행 저장 {saved}건, 보관 중 {total}경기, API {api.used()}건 사용 "
          f"({(time.time() - start) / 60:.1f}분)")


def resolve_focus(con, names, season):
    ids = set()
    for name in names:
        rows = con.execute(
            "SELECT p.sp_id, s.label FROM meta_player p LEFT JOIN meta_season s "
            "ON s.season_id = p.sp_id / 1000000 WHERE p.name = ?", (name,)).fetchall()
        if season:
            rows = [r for r in rows if r[1] and season.lower() in r[1].lower()]
        if not rows:
            print(f"[warn] 추적 선수를 찾지 못했습니다: {name} {season or ''}")
        for r in rows:
            ids.add(r[0])
            print(f"[focus] {name} ({r[1]}) spId {r[0]}")
    if ids:
        q = ",".join("?" * len(ids))
        con.execute(f"UPDATE crawl_users SET focus=1 WHERE ouid IN (SELECT t.ouid FROM teams t JOIN players p "
                    f"ON p.match_id=t.match_id AND p.side=t.side WHERE p.sp_id IN ({q}))", list(ids))
        con.commit()
    return ids


def prune(con, days):
    """보관 기간이 지난 경기와 오래 안 보인 유저를 지운다."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    old = "SELECT match_id FROM matches WHERE match_date < ?"
    con.execute(f"DELETE FROM players WHERE match_id IN ({old})", (cutoff,))
    con.execute(f"DELETE FROM teams WHERE match_id IN ({old})", (cutoff,))
    n = con.execute("DELETE FROM matches WHERE match_date < ?", (cutoff,)).rowcount
    con.execute("DELETE FROM crawl_users WHERE found_at < ? AND last_crawled < ?",
                (time.time() - 2 * days * 86400,) * 2)
    # 기간 밖으로 판정된 경기는 다시 받지 않도록 남겨 두되, 너무 쌓이면 오래된 것부터 정리
    con.execute("DELETE FROM skipped WHERE rowid IN (SELECT rowid FROM skipped ORDER BY rowid LIMIT "
                "MAX(0, (SELECT COUNT(*) FROM skipped) - 500000))")
    con.execute("DELETE FROM api_usage_key WHERE day < ?", ((date.today() - timedelta(days=3)).isoformat(),))
    con.commit()
    return n


def meta_stale(con, hours):
    r = con.execute("SELECT value FROM kv WHERE key='meta_synced'").fetchone()
    return not r or time.time() - float(r[0]) > hours * 3600


def seconds_to_midnight():
    now = datetime.now()
    nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) + timedelta(minutes=5)
    return (nxt - now).total_seconds()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--probe", action="store_true", help="API 지원 여부만 점검하고 종료")
    ap.add_argument("--mode", choices=["auto", "global", "snowball"], default="snowball")
    ap.add_argument("--matchtype", type=int, default=52, help="감독모드 매치 타입 코드")
    ap.add_argument("--seed", nargs="*", default=[], help="스노우볼 시작 닉네임 (여러 개 가능)")
    ap.add_argument("--focus", nargs="*", default=[], help="우선 추적할 선수 이름 (예: 보비 찰턴)")
    ap.add_argument("--focus-season", default=None, help="추적 선수 시즌 (예: FSL). 생략하면 모든 시즌")
    ap.add_argument("--users", type=int, default=10, help="스노우볼 1회당 조회할 유저 수")
    ap.add_argument("--recrawl-hours", type=float, default=12, help="같은 유저 재조회 간격")
    ap.add_argument("--daily", type=int, default=950, help="키(앱)당 하루 호출 상한 (개발 키 1,000에서 여유분 제외)")
    ap.add_argument("--interval", type=int, default=60, help="라운드 사이 대기(초)")
    ap.add_argument("--pages", type=int, default=20, help="global 1회당 목록 최대 페이지")
    ap.add_argument("--limit", type=int, default=100, help="목록 1회 요청당 매치 수")
    ap.add_argument("--rps", type=float, default=4.0, help="초당 요청 수 (개발 키는 5 미만)")
    ap.add_argument("--refresh-meta", action="store_true", help="선수·시즌 메타데이터 강제 갱신")
    ap.add_argument("--batch", action="store_true",
                    help="대기 없이 라운드를 반복하다 --run-minutes 또는 --max-matches에 닿으면 정리 후 종료 (자동화용)")
    ap.add_argument("--run-minutes", type=float, default=30)
    ap.add_argument("--max-matches", type=int, default=0, help="이번 실행에서 새로 저장할 최대 경기 수 (0=제한 없음)")
    ap.add_argument("--window-days", type=float, default=0, help="이 기간보다 오래된 경기는 저장하지 않고 삭제 (0=보관)")
    ap.add_argument("--seed-if-empty", action="store_true", help="유저 풀이 비었을 때만 시드 등록")
    a = ap.parse_args()

    keys = [k.strip() for k in os.environ.get("NEXON_API_KEYS", "").split(",") if k.strip()]
    if not keys and os.environ.get("NEXON_API_KEY"):
        keys = [os.environ["NEXON_API_KEY"]]
    if not keys:
        raise SystemExit("NEXON_API_KEY 또는 NEXON_API_KEYS(쉼표 구분) 환경변수를 설정하세요.")
    keys = list(dict.fromkeys(keys))
    if len(keys) > 3:
        print(f"[warn] 키가 {len(keys)}개입니다. 넥슨 ID 하나당 한 게임에 앱은 3개까지이고, "
              "한 앱의 키 2개는 허용량을 공유합니다. 본인 ID의 앱별 키 1개씩만 넣으세요.")
    con = connect()
    api = Nexon(keys, a.rps, con, a.daily)
    print(f"[keys] {len(keys)}개 사용, 오늘 남은 호출 {api.remaining()}건")
    try:
        if a.probe:
            await probe(api, a.matchtype, a.seed)
            return
        global WINDOW_FROM, CHUNK
        CHUNK = max(20, int(a.rps * 2))
        if a.window_days:
            WINDOW_FROM = (datetime.now() - timedelta(days=a.window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        if a.refresh_meta or meta_stale(con, 24):
            await sync_meta(api, con)
            con.execute("INSERT OR REPLACE INTO kv VALUES('meta_synced', ?)", (str(time.time()),))
            con.commit()
        FOCUS.update(resolve_focus(con, a.focus, a.focus_season))
        pool = con.execute("SELECT COUNT(*) FROM crawl_users").fetchone()[0]
        if a.seed and (not a.seed_if_empty or pool == 0):
            await add_seeds(api, con, a.seed)
        if a.batch:
            await run_batch(api, con, a)
            return
        mode = a.mode
        while True:
            if api.exhausted:
                wait = seconds_to_midnight()
                print(f"[quota] 오늘 사용 {api.used()}건. 자정 이후 재개합니다 (약 {wait/3600:.1f}시간 후).", flush=True)
                if a.once:
                    break
                await asyncio.sleep(wait)
                api.exhausted = False
                continue
            if mode in ("auto", "global"):
                r = await crawl_global(api, con, a.matchtype, a.pages, a.limit)
                if r is None:
                    if mode == "global":
                        raise SystemExit("전체 매치 목록 API가 이 매치 타입을 지원하지 않습니다. --mode snowball 을 쓰세요.")
                    print("[auto] 전체 매치 목록 API를 쓸 수 없어 스노우볼 방식으로 전환합니다.")
                    mode = "snowball"
            if mode == "snowball":
                await crawl_snowball(api, con, a.matchtype, a.users, a.limit, a.recrawl_hours)
            print(f"[budget] 오늘 {api.used()}건 사용, 남은 {api.remaining()}건", flush=True)
            if a.once:
                break
            print(f"[wait] {a.interval}초 후 다음 라운드", flush=True)
            await asyncio.sleep(a.interval)
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())

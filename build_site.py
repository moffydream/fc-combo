"""자동 배포용 정적 사이트 빌더 (선수별 분할 방식).

DB 전체를 브라우저에 내려보내는 export_site.py 와 달리, 기준 선수(선수+포지션)마다
그 선수가 뛴 팀 기록만 담은 작은 JSON 파일을 만든다. 방문자는 고른 선수의 파일 하나만 받는다.

  python build_site.py --out site --min-games 20

공개용이므로 유저 식별자(ouid)와 매치 ID는 넣지 않는다.
"""
import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from analytics import SCORE, _bucket
from db import connect

HERE = Path(__file__).parent
RES = {"W": 0, "D": 1, "L": 2}


def hour_no(iso):
    """경기 시각을 1970년부터의 시간 수(정수)로. 파일 크기를 줄이기 위함. 로컬(KST) 시각 기준."""
    try:
        return int(datetime.fromisoformat(iso[:19]).timestamp() // 3600)
    except ValueError:
        return 0


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "site"))
    ap.add_argument("--min-games", type=int, default=20, help="기준 선수로 검색되려면 필요한 최소 경기 수")
    a = ap.parse_args()

    con = connect()
    out = Path(a.out)
    if (out / "data").exists():
        shutil.rmtree(out / "data")
    (out / "data" / "a").mkdir(parents=True)

    # 팀 단위로 라인업 모으기 (GK 제외, 포지션 코드 순)
    teams = {}
    for r in con.execute("SELECT t.match_id, t.side, t.result, t.gf, t.ga, t.formation, t.avg_grade, "
                         "t.opp_avg_grade, m.match_date FROM teams t JOIN matches m ON m.match_id=t.match_id"):
        teams[(r[0], r[1])] = {"res": r[2], "gf": r[3], "ga": r[4], "f": r[5], "g": r[6], "og": r[7],
                               "date": r[8] or "", "lineup": {}}
    for mid, side, sp, pos, grade, rating in con.execute(
            "SELECT match_id, side, sp_id, pos, grade, rating FROM players WHERE pos NOT IN (0, 28)"):
        t = teams.get((mid, side))
        if t:
            t["lineup"][pos] = (sp, grade, rating)

    # 체급 기대 승점표
    agg = defaultdict(lambda: [0.0, 0])
    for t in teams.values():
        b = _bucket(t["g"] - t["og"])
        agg[b][0] += SCORE[t["res"]]
        agg[b][1] += 1
    exp = {str(b): (s + 5) / (n + 10) for b, (s, n) in agg.items()}

    # 기준 선수별 행 모으기
    shards = defaultdict(list)
    formations = {}
    for t in teams.values():
        codes = [int(x) for x in t["f"].split("-")]
        if sorted(t["lineup"]) != codes:
            continue
        fi = formations.setdefault(t["f"], len(formations))
        for pos in codes:
            shards[(t["lineup"][pos][0], pos)].append((t, fi, codes, pos))

    names = {r[0]: r[1] for r in con.execute("SELECT sp_id, name FROM meta_player")}
    seasons = {r[0]: r[1] for r in con.execute("SELECT season_id, label FROM meta_season")}
    form_list = [None] * len(formations)
    for sig, i in formations.items():
        form_list[i] = sig

    anchors, anchor_names, written = [], {}, 0
    for (sp, pos), items in shards.items():
        if len(items) < a.min_games:
            continue
        pidx, plist = {}, []
        rows = []
        for t, fi, codes, apos in items:
            lineup = []
            grades = []
            for c in codes:
                s, g, _ = t["lineup"][c]
                if s not in pidx:
                    pidx[s] = len(plist)
                    plist.append(s)
                lineup.append(pidx[s])
                grades.append(g)
            rating = t["lineup"][apos][2] or 0
            rows.append([hour_no(t["date"]), RES[t["res"]], t["gf"], t["ga"], round(t["g"] * 10),
                         round(t["og"] * 10), round(rating * 100), fi, lineup, grades])
        dump(out / "data" / "a" / f"{sp}_{pos}.json",
             {"p": plist, "n": [names.get(s, f"#{s}") for s in plist], "r": rows})
        anchors.append([sp, pos, len(items)])
        anchor_names[sp] = names.get(sp, f"#{sp}")
        written += 1

    dates = [t["date"] for t in teams.values() if t["date"]]
    dump(out / "data" / "index.json", {
        "built_at": datetime.now().isoformat(timespec="minutes"),
        "matches": len(teams) // 2,
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "formations": form_list,
        "seasons": seasons,
        "names": anchor_names,
        "anchors": anchors,
        "exp": exp,
    })

    html = (HERE / "static" / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("<script>\nconst COORD", '<script src="engine_shard.js"></script>\n<script>\nconst COORD', 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    shutil.copy(HERE / "static" / "engine_shard.js", out / "engine_shard.js")
    (out / ".nojekyll").write_text("")
    size = sum(f.stat().st_size for f in (out / "data").rglob("*") if f.is_file()) / 1e6
    print(f"[site] {len(teams) // 2:,}경기, 기준 선수 파일 {written:,}개, 데이터 {size:.1f}MB → {out}")


if __name__ == "__main__":
    main()

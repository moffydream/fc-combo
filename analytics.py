"""조합 성적 집계.

지표
- 승률: W / n
- 보정 승률: 기준 선수·포메이션 전체 승률을 사전값으로 k경기만큼 섞은 값.
  표본이 적은 조합이 우연히 튀는 것을 눌러준다.
- 95% 하한: 승률의 Wilson 신뢰구간 하한. '최소한 이 정도는 된다'는 보수적 기준.
- 체급 보정: (실제 승점률 - 체급 기대 승점률) %p.
  기대값은 DB 전체에서 '우리 팀 평균 강화 - 상대 평균 강화' 구간별 평균 승점률.
  비싼 조합이 체급 덕에 이기는 효과를 일부 걷어낸다.
"""
import math
import time
from collections import defaultdict

from positions import POS_NAME, formation_label, formation_positions

SCORE = {"W": 1.0, "D": 0.5, "L": 0.0}
_exp = {"t": 0.0, "tbl": {}}


def wilson_lower(w, n, z=1.96):
    if n == 0:
        return 0.0
    p = w / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / den


def _bucket(diff):
    return math.floor(diff * 2 + 0.5) / 2


def expected_score_fn(con):
    if time.time() - _exp["t"] > 600 or not _exp["tbl"]:
        rows = con.execute(
            "SELECT avg_grade - opp_avg_grade AS d, result FROM teams").fetchall()
        agg = defaultdict(lambda: [0.0, 0])
        for r in rows:
            b = _bucket(r["d"])
            agg[b][0] += SCORE[r["result"]]
            agg[b][1] += 1
        _exp["tbl"] = {b: (s + 5) / (n + 10) for b, (s, n) in agg.items()}
        _exp["t"] = time.time()
    tbl = _exp["tbl"]
    keys = sorted(tbl)

    def f(diff):
        if not keys:
            return 0.5
        b = _bucket(diff)
        if b in tbl:
            return tbl[b]
        return tbl[min(keys, key=lambda k: abs(k - b))]
    return f


def _labels(con, sp_ids):
    sp_ids = [s for s in set(sp_ids) if s is not None]
    if not sp_ids:
        return {}
    q = ",".join("?" * len(sp_ids))
    names = {r[0]: r[1] for r in con.execute(
        f"SELECT sp_id, name FROM meta_player WHERE sp_id IN ({q})", sp_ids)}
    seasons = {r[0]: r[1] for r in con.execute("SELECT season_id, label FROM meta_season")}
    out = {}
    for s in sp_ids:
        out[s] = {"sp_id": s,
                  "name": names.get(s, f"#{s}"),
                  "season": seasons.get(s // 1_000_000, str(s // 1_000_000))}
    return out


def status(con):
    r = con.execute("SELECT COUNT(*) n, MIN(match_date) a, MAX(match_date) b FROM matches").fetchone()
    return {"matches": r["n"], "first": r["a"], "last": r["b"]}


def search_players(con, q, limit=15):
    rows = con.execute("SELECT sp_id FROM meta_player WHERE name LIKE ? LIMIT 300",
                       (f"%{q}%",)).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    qs = ",".join("?" * len(ids))
    usage = defaultdict(dict)
    for r in con.execute(f"SELECT sp_id, pos, COUNT(*) n FROM players WHERE sp_id IN ({qs}) "
                         f"GROUP BY sp_id, pos", ids):
        usage[r["sp_id"]][POS_NAME.get(r["pos"], str(r["pos"]))] = r["n"]
    labels = _labels(con, ids)
    out = []
    for s in ids:
        u = usage.get(s, {})
        out.append({**labels[s], "games": sum(u.values()),
                    "positions": sorted(u.items(), key=lambda x: -x[1])})
    out.sort(key=lambda x: -x["games"])
    return out[:limit]


def _where(date_from, gmin, gmax):
    sql, args = "", []
    if date_from:
        sql += " AND m.match_date >= ?"
        args.append(date_from)
    if gmin is not None:
        sql += " AND t.avg_grade >= ?"
        args.append(gmin)
    if gmax is not None:
        sql += " AND t.avg_grade <= ?"
        args.append(gmax)
    return sql, args


def formations(con, anchor, pos, date_from=None, gmin=None, gmax=None):
    w, a = _where(date_from, gmin, gmax)
    rows = con.execute(
        "SELECT t.formation f, COUNT(*) n, SUM(t.result='W') w "
        "FROM players pa JOIN teams t ON t.match_id=pa.match_id AND t.side=pa.side "
        "JOIN matches m ON m.match_id=t.match_id "
        f"WHERE pa.sp_id=? AND pa.pos=? {w} GROUP BY t.formation ORDER BY n DESC LIMIT 30",
        [anchor, pos] + a).fetchall()
    return [{"sig": r["f"], "label": formation_label(r["f"]),
             "positions": formation_positions(r["f"]),
             "codes": [int(x) for x in r["f"].split("-")],
             "games": r["n"], "win_rate": r["w"] / r["n"]} for r in rows]


def combos(con, anchor, pos, formation, slots, date_from=None, gmin=None, gmax=None,
           min_games=10, k=20, matrix_top=8):
    slots = slots[:2]
    joins, cols, args = "", "", []
    for i, s in enumerate(slots):
        joins += (f" JOIN players s{i} ON s{i}.match_id=t.match_id AND s{i}.side=t.side "
                  f"AND s{i}.pos=?")
        cols += f", s{i}.sp_id AS p{i}, s{i}.grade AS g{i}"
        args.append(s)
    w, wa = _where(date_from, gmin, gmax)
    sql = ("SELECT t.result, t.gf, t.ga, t.avg_grade, t.opp_avg_grade, pa.rating ar, pa.grade ag"
           f"{cols} FROM players pa "
           "JOIN teams t ON t.match_id=pa.match_id AND t.side=pa.side "
           "JOIN matches m ON m.match_id=t.match_id "
           f"{joins} WHERE pa.sp_id=? AND pa.pos=? AND t.formation=? {w}")
    rows = con.execute(sql, args + [anchor, pos, formation] + wa).fetchall()
    exp = expected_score_fn(con)

    def blank():
        return {"n": 0, "w": 0, "d": 0, "l": 0, "gd": 0, "adj": 0.0, "ar": 0.0, "ar_n": 0,
                "grades": [0.0] * len(slots), "team_grade": 0.0}

    base = blank()
    groups = defaultdict(blank)
    for r in rows:
        key = tuple(r[f"p{i}"] for i in range(len(slots)))
        adj = SCORE[r["result"]] - exp(r["avg_grade"] - r["opp_avg_grade"])
        for g in (base, groups[key]):
            g["n"] += 1
            g[r["result"].lower()] += 1
            g["gd"] += r["gf"] - r["ga"]
            g["adj"] += adj
            g["team_grade"] += r["avg_grade"]
            if r["ar"]:
                g["ar"] += r["ar"]
                g["ar_n"] += 1
            for i in range(len(slots)):
                g["grades"][i] += r[f"g{i}"]

    if base["n"] == 0:
        return {"baseline": None, "rows": [], "matrix": None,
                "slots": [POS_NAME[s] for s in slots]}
    base_wr = base["w"] / base["n"]

    def finish(g):
        n = g["n"]
        return {"n": n, "w": g["w"], "d": g["d"], "l": g["l"],
                "win_rate": g["w"] / n,
                "shrunk": (g["w"] + k * base_wr) / (n + k),
                "lower95": wilson_lower(g["w"], n),
                "adj_pp": 100 * g["adj"] / n,
                "gd": g["gd"] / n,
                "anchor_rating": (g["ar"] / g["ar_n"]) if g["ar_n"] else None,
                "team_grade": g["team_grade"] / n,
                "grades": [x / n for x in g["grades"]]}

    labels = _labels(con, [p for key in groups for p in key])
    out = []
    for key, g in groups.items():
        if g["n"] < min_games:
            continue
        out.append({"players": [labels[p] for p in key], **finish(g)})
    out.sort(key=lambda x: -x["shrunk"])

    matrix = None
    if len(slots) == 2:
        cnt = [defaultdict(int), defaultdict(int)]
        for key, g in groups.items():
            cnt[0][key[0]] += g["n"]
            cnt[1][key[1]] += g["n"]
        top = [sorted(c, key=lambda p: -c[p])[:matrix_top] for c in cnt]
        cells = []
        for a in top[0]:
            row = []
            for b in top[1]:
                g = groups.get((a, b))
                row.append(None if not g else {"n": g["n"],
                                                 "shrunk": (g["w"] + k * base_wr) / (g["n"] + k),
                                                 "adj_pp": 100 * g["adj"] / g["n"]})
            cells.append(row)
        matrix = {"rows": [labels[p] | {"games": cnt[0][p]} for p in top[0]],
                  "cols": [labels[p] | {"games": cnt[1][p]} for p in top[1]],
                  "cells": cells}

    return {"baseline": finish(base), "rows": out, "matrix": matrix,
            "slots": [POS_NAME[s] for s in slots], "combos_total": len(groups)}

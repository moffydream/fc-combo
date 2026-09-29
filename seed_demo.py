"""API 키 없이 대시보드를 확인하기 위한 가상 데이터 생성기.

  FC_DB=demo.db python seed_demo.py
  FC_DB=demo.db uvicorn server:app --port 8010

선수 이름과 시즌은 예시일 뿐이고, 성적과 '숨은 궁합'은 난수로 만든 값입니다.
"""
import math
import random
from datetime import datetime, timedelta

from db import connect
from positions import formation_sig

random.seed(7)
SEASONS = {801: "FSL", 802: "ICON", 803: "TOTY", 804: "BTB"}
ANCHOR = 801_000_001
MIDS = ["루드 굴리트", "파트리크 비에이라", "로이 킨", "로타어 마테우스", "프랑크 레이카르트",
        "폴 스콜스", "사비 알론소", "미하엘 발락", "클라렌스 세도르프", "마이클 에시앙"]
OTHERS = ["수비수 A", "수비수 B", "수비수 C", "수비수 D", "공격수 A", "공격수 B", "공격수 C",
          "골키퍼 A", "공격형 미드 A", "공격형 미드 B"]
FORMS = {
    "4-2-2-2": [0, 7, 6, 4, 3, 15, 13, 19, 17, 26, 24],
    "5-2-2-1": [0, 8, 6, 5, 4, 2, 15, 13, 19, 17, 25],
}


def main():
    con = connect()
    for t in ("matches", "teams", "players", "meta_player", "meta_season", "skipped"):
        con.execute(f"DELETE FROM {t}")
    con.executemany("INSERT INTO meta_season VALUES(?,?)", SEASONS.items())
    con.execute("INSERT INTO meta_player VALUES(?,?)", (ANCHOR, "보비 찰턴"))
    pool = {}
    mids = []
    for i, name in enumerate(MIDS):
        for s in (801, 802):
            sp = s * 1_000_000 + 100 + i
            con.execute("INSERT INTO meta_player VALUES(?,?)", (sp, name))
            mids.append(sp)
            pool[sp] = random.gauss(0, 0.25)
    fillers = []
    for i, name in enumerate(OTHERS):
        sp = random.choice(list(SEASONS)) * 1_000_000 + 500 + i
        con.execute("INSERT INTO meta_player VALUES(?,?)", (sp, name))
        fillers.append(sp)
    synergy = {(random.choice(mids), random.choice(mids)): random.choice([-0.5, 0.5]) for _ in range(12)}
    popular = mids[:8]

    now = datetime.now()
    for m in range(40000):
        mid = f"demo{m:06d}"
        date = (now - timedelta(minutes=random.randint(0, 60 * 24 * 60))).strftime("%Y-%m-%dT%H:%M:%S")
        teams = []
        for side in (0, 1):
            fname = random.choice(list(FORMS)) if random.random() < .5 else "4-2-2-2"
            codes = FORMS[fname]
            has_anchor = random.random() < 0.55
            lcm = random.choice(popular if random.random() < .7 else mids)
            rcm = random.choice([x for x in (popular if random.random() < .7 else mids) if x != lcm])
            grade = random.choice([5, 6, 7, 8, 8, 9, 10])
            roster = []
            for c in codes:
                if c == 17 and has_anchor:
                    sp = ANCHOR
                elif c == 15:
                    sp = lcm
                elif c == 13:
                    sp = rcm
                else:
                    sp = random.choice(fillers)
                roster.append((sp, c, max(1, min(13, grade + random.randint(-1, 1)))))
            strength = (sum(g for _, _, g in roster) / 11) * 0.35 + pool.get(lcm, 0) + pool.get(rcm, 0)
            strength += synergy.get((lcm, rcm), 0) + (0.2 if has_anchor else 0)
            teams.append(dict(roster=roster, strength=strength, codes=codes))
        diff = teams[0]["strength"] - teams[1]["strength"]
        p0 = 1 / (1 + math.exp(-diff))
        r = random.random()
        draw = 0.18
        res0 = "W" if r < p0 * (1 - draw) else "D" if r < p0 * (1 - draw) + draw else "L"
        res1 = {"W": "L", "L": "W", "D": "D"}[res0]
        g0 = random.randint(1, 3) if res0 == "W" else random.randint(0, 2)
        g1 = g0 - random.randint(1, 2) if res0 == "W" else (g0 if res0 == "D" else g0 + random.randint(1, 2))
        g1 = max(0, g1)
        con.execute("INSERT INTO matches VALUES(?,?,?)", (mid, date, 52))
        avgs = [sum(g for _, _, g in t["roster"]) / 11 for t in teams]
        for side, (t, res, gf, ga) in enumerate(zip(teams, (res0, res1), (g0, g1), (g1, g0))):
            con.execute("INSERT INTO teams VALUES(?,?,?,?,?,?,?,?,?)",
                        (mid, side, f"u{random.randint(1, 5000)}", res, gf, ga,
                         formation_sig(t["codes"]), avgs[side], avgs[1 - side]))
            con.executemany("INSERT INTO players VALUES(?,?,?,?,?,?,?,?)",
                            [(mid, side, sp, c, g, round(random.gauss(7 if res == "W" else 6.3, .6), 2), 0, 0)
                             for sp, c, g in t["roster"]])
    con.commit()
    print("demo DB 생성 완료")


if __name__ == "__main__":
    main()

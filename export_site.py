"""GitHub Pages 같은 정적 호스팅용 사이트를 만든다.

  python export_site.py               # site 폴더 생성/갱신
  python export_site.py --push        # site 폴더가 git 저장소면 커밋+푸시까지

공개용이므로 유저 식별자(ouid)는 빼고 내보낸다.
"""
import argparse
import gzip
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from db import DB_PATH

HERE = Path(__file__).parent
SQLJS_TAG = '<script src="https://cdn.jsdelivr.net/npm/sql.js@1.13.0/dist/sql-wasm.js"></script>\n<script src="engine.js"></script>\n'

SLIM = """
CREATE TABLE info(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE matches(mid INTEGER PRIMARY KEY, match_date TEXT);
CREATE TABLE teams(mid INTEGER, side INTEGER, result TEXT, gf INTEGER, ga INTEGER,
                   formation TEXT, avg_grade REAL, opp_avg_grade REAL, PRIMARY KEY(mid, side)) WITHOUT ROWID;
CREATE TABLE players(mid INTEGER, side INTEGER, sp_id INTEGER, pos INTEGER, grade INTEGER, rating REAL);
CREATE TABLE meta_player(sp_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE meta_season(season_id INTEGER PRIMARY KEY, label TEXT);
"""
INDEXES = """
CREATE INDEX ix_p_sp ON players(sp_id, pos);
CREATE INDEX ix_p_team ON players(mid, side, pos);
CREATE INDEX ix_t_form ON teams(formation);
CREATE INDEX ix_m_date ON matches(match_date);
"""


def build_db(src_path, out_path, days):
    if out_path.exists():
        out_path.unlink()
    con = sqlite3.connect(out_path)
    con.executescript(SLIM)
    con.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S") if days else ""
    # 문자열 match_id 대신 정수 mid 로 바꿔 용량을 줄인다
    con.execute("CREATE TEMP TABLE idmap AS SELECT ROW_NUMBER() OVER (ORDER BY match_date) AS mid, match_id, match_date "
                "FROM src.matches WHERE match_date >= ?", (since,))
    con.execute("CREATE INDEX temp.ix_idmap ON idmap(match_id)")
    con.execute("INSERT INTO matches SELECT mid, match_date FROM idmap")
    con.execute("INSERT INTO teams SELECT i.mid, t.side, t.result, t.gf, t.ga, t.formation, t.avg_grade, t.opp_avg_grade "
                "FROM src.teams t JOIN idmap i ON i.match_id = t.match_id")
    con.execute("INSERT INTO players SELECT i.mid, p.side, p.sp_id, p.pos, p.grade, p.rating "
                "FROM src.players p JOIN idmap i ON i.match_id = p.match_id")
    con.execute("INSERT INTO meta_player SELECT sp_id, name FROM src.meta_player "
                "WHERE sp_id IN (SELECT DISTINCT sp_id FROM players)")
    con.execute("INSERT INTO meta_season SELECT season_id, label FROM src.meta_season")
    con.execute("INSERT INTO info VALUES('built_at', ?)", (datetime.now().isoformat(timespec="minutes"),))
    con.commit()
    con.execute("DETACH DATABASE src")
    con.executescript(INDEXES)
    n = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    con.execute("VACUUM")
    con.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "site"))
    ap.add_argument("--days", type=int, default=90, help="최근 며칠치만 내보낼지 (0이면 전체)")
    ap.add_argument("--push", action="store_true", help="site 폴더가 git 저장소면 커밋하고 푸시")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "data.db"
    n = build_db(Path(DB_PATH), tmp, a.days)
    with open(tmp, "rb") as f, gzip.open(out / "data.db.gz", "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    raw_mb = tmp.stat().st_size / 1e6
    tmp.unlink()
    gz_mb = (out / "data.db.gz").stat().st_size / 1e6

    html_src = HERE / "static" / "dashboard.html"
    if not html_src.exists():
        html_src = HERE / "dashboard.html"
    html = html_src.read_text(encoding="utf-8")
    html = html.replace("<script>\nconst COORD", SQLJS_TAG + "<script>\nconst COORD", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    eng = HERE / "static" / "engine.js"
    shutil.copy(eng if eng.exists() else HERE / "engine.js", out / "engine.js")
    (out / ".nojekyll").write_text("")

    print(f"[export] {n:,}경기, DB {raw_mb:.1f}MB → 압축 {gz_mb:.1f}MB, 위치 {out}")
    if gz_mb > 25:
        print("[warn] 25MB를 넘어서 GitHub 웹 업로드는 안 됩니다. GitHub Desktop이나 git으로 올리거나 --days를 줄이세요.")
    if gz_mb > 95:
        print("[warn] GitHub 파일 한도(100MB)에 가깝습니다. --days를 줄이세요.")

    if a.push:
        if not (out / ".git").exists():
            raise SystemExit("site 폴더가 git 저장소가 아닙니다. README의 배포 절차를 먼저 따라 주세요.")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", f"데이터 갱신 {stamp}"], ["git", "push"]):
            r = subprocess.run(cmd, cwd=out, capture_output=True, text=True)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                raise SystemExit(f"{' '.join(cmd)} 실패:\n{r.stderr or r.stdout}")
        print("[push] 완료. 1~2분 뒤 사이트에 반영됩니다.")


if __name__ == "__main__":
    main()

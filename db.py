import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("FC_DB", str(Path(__file__).parent / "fc_combo.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches(
  match_id TEXT PRIMARY KEY,
  match_date TEXT,
  match_type INTEGER
);
-- 한 경기의 한 팀 = 한 행
CREATE TABLE IF NOT EXISTS teams(
  match_id TEXT,
  side INTEGER,
  ouid TEXT,
  result TEXT,            -- W / D / L
  gf INTEGER,
  ga INTEGER,
  formation TEXT,         -- positions.formation_sig
  avg_grade REAL,         -- 선발 평균 강화 (체급 대리변수)
  opp_avg_grade REAL,
  PRIMARY KEY(match_id, side)
);
-- 선발 선수만 저장 (교체 28 제외)
CREATE TABLE IF NOT EXISTS players(
  match_id TEXT,
  side INTEGER,
  sp_id INTEGER,
  pos INTEGER,
  grade INTEGER,
  rating REAL,
  goal INTEGER,
  assist INTEGER,
  shoot INTEGER, eff_shoot INTEGER, pass_try INTEGER, pass_succ INTEGER,
  tackle INTEGER, intercept INTEGER, block INTEGER
);
CREATE INDEX IF NOT EXISTS ix_players_sp ON players(sp_id, pos);
CREATE INDEX IF NOT EXISTS ix_players_team ON players(match_id, side, pos);
CREATE INDEX IF NOT EXISTS ix_teams_form ON teams(formation);
CREATE INDEX IF NOT EXISTS ix_matches_date ON matches(match_date);

CREATE TABLE IF NOT EXISTS meta_player(sp_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS meta_season(season_id INTEGER PRIMARY KEY, label TEXT, img TEXT);
-- 몰수패·라인업 누락 등 분석에서 뺀 경기 (재조회 방지)
CREATE TABLE IF NOT EXISTS skipped(match_id TEXT PRIMARY KEY, reason TEXT);
-- 스노우볼 수집용: 경기 기록에서 발견한 유저와 마지막 조회 시각
CREATE TABLE IF NOT EXISTS crawl_users(ouid TEXT PRIMARY KEY, last_crawled REAL DEFAULT 0, found_at REAL,
                                       focus INTEGER DEFAULT 0);
-- 일일 API 호출 수 (개발 단계 키는 하루 1,000건)
CREATE TABLE IF NOT EXISTS api_usage(day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
-- 키(앱)별 일일 호출 수
CREATE TABLE IF NOT EXISTS api_usage_key(day TEXT, key_id TEXT, calls INTEGER DEFAULT 0,
                                         PRIMARY KEY(day, key_id));
"""


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # 이전 버전 DB 호환
    try:
        con.execute("ALTER TABLE crawl_users ADD COLUMN focus INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for tbl, col in (("meta_season", "img TEXT"),
                     ("players", "shoot INTEGER"), ("players", "eff_shoot INTEGER"),
                     ("players", "pass_try INTEGER"), ("players", "pass_succ INTEGER"),
                     ("players", "tackle INTEGER"), ("players", "intercept INTEGER"), ("players", "block INTEGER")):
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 한도 초과로 조회 실패한 경기는 다시 조회하도록 제외 목록에서 뺌
    con.execute("DELETE FROM skipped WHERE reason='상세 조회 실패'")
    con.commit()
    return con

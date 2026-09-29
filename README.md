# 감독모드 조합 분석

FC온라인 감독모드에서 기준 선수·포지션·포메이션을 고정하고, 다른 자리 선수 조합별 성적을 보는 대시보드.
GitHub Actions가 매시간 넥슨 오픈 API로 경기를 수집하고, 최근 7일치로 GitHub Pages 사이트를 자동 갱신한다. PC에서 돌릴 것은 없다.

## 처음 한 번 설정

1. **저장소에 파일 올리기**: 기존 Pages 저장소(넥슨에 등록한 URL의 저장소)를 쓰면 주소가 그대로 유지된다. 기존 파일은 지우고 이 폴더의 파일을 모두 올린다. `.github/workflows/collect.yml`도 반드시 포함.
2. **Settings → Secrets and variables → Actions**
   - Secrets 탭 → New repository secret: 이름 `NEXON_API_KEYS`, 값은 서비스 단계 키
   - Variables 탭 → New repository variable: 이름 `SEED_NICKNAMES`, 값은 시작 닉네임을 공백으로 구분 (예: `Lysergic Lysergic2`). 등급대가 다른 닉네임을 섞을수록 표본이 덜 쏠린다.
3. **Settings → Pages → Source**를 `GitHub Actions`로 변경
4. **Actions 탭 → 수집 및 배포 → Run workflow**로 첫 실행. 이후엔 매시간 자동 실행.

## 조정 가능한 값 (Variables, 선택)

| 이름 | 기본값 | 의미 |
|---|---|---|
| `MATCHES_PER_RUN` | 1000 | 1시간마다 새로 저장할 경기 수. 7일이면 약 16만 경기 |
| `WINDOW_DAYS` | 7 | 보관 기간 |
| `RUN_MINUTES` | 25 | 1회 수집 최대 시간 |
| `MIN_GAMES` | 20 | 기준 선수로 검색되기 위한 최소 경기 수 |

`MATCHES_PER_RUN`을 크게 올리면 사이트 용량이 커진다. GitHub Pages 사이트 한도가 1GB라 7일 기준 시간당 3,000경기 정도가 상한이다.

## 동작 구조

- `collector.py --batch`: 시드 유저에서 시작해 경기에서 만난 상대로 넓혀 가며(스노우볼) 수집. 기간 밖 경기는 버리고 오래된 경기는 삭제
- DB는 Actions 캐시에 보관 (공개되지 않음). 캐시가 사라지면 처음부터 다시 쌓이며, 7일이면 원상 복구
- `build_site.py`: 기준 선수(선수+포지션)별로 작은 JSON 파일을 만들어, 방문자는 고른 선수의 파일만 받음. 유저 식별자는 넣지 않음
- 넥슨 API는 감독모드 전체 경기 목록을 제공하지 않으므로, 결과는 전수가 아니라 표본이다

## 파일

| 파일 | 역할 |
|---|---|
| `.github/workflows/collect.yml` | 매시간 수집 → 사이트 빌드 → 배포 |
| `collector.py` | 넥슨 API 수집기 |
| `build_site.py` | 정적 사이트 생성 |
| `static/dashboard.html`, `static/engine_shard.js` | 대시보드와 브라우저 계산 엔진 |
| `analytics.py`, `positions.py`, `db.py` | 공용 계산·포지션·DB |
| `server.py`, `export_site.py`, `static/engine.js`, `seed_demo.py` | 로컬 실행용 (자동화에는 불필요) |

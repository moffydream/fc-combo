/* 분할 정적 사이트용 분석 엔진. build_site.py 가 만든 data/index.json 과
 * data/a/{선수}_{포지션}.json 을 읽어 계산한다.
 *
 * 표본을 늘리는 옵션 (요청 파라미터)
 *  merge_season=1 : 같은 선수의 다른 시즌 카드를 한 선수로 묶음 (기준 선수와 비교 자리 모두)
 *  merge_form=1   : 포메이션을 정확한 배치 대신 라인 표기(예: 4-2-2-2) 단위로 묶음
 *  mirror=1       : 두 자리를 비교할 때 좌우(순서)를 구분하지 않음 (A가 LCM·B가 RCM = B가 LCM·A가 RCM)
 *
 * 조합 추정 승률: 표본이 적은 조합을 '기준 승률'이 아니라 '두 선수 각자의 성적으로 예상한 승률'
 * 쪽으로 당겨서 추정한다. 궁합 = 조합 추정 승률 - 개인 합산 기대 승률. */
(function () {
  const POS = {0:"GK",1:"SW",2:"RWB",3:"RB",4:"RCB",5:"CB",6:"LCB",7:"LB",8:"LWB",9:"RDM",10:"CDM",11:"LDM",
    12:"RM",13:"RCM",14:"CM",15:"LCM",16:"LM",17:"RAM",18:"CAM",19:"LAM",20:"RF",21:"CF",22:"LF",23:"RW",
    24:"RS",25:"ST",26:"LS",27:"LW"};
  const LINES = [[1,8],[9,11],[12,16],[17,19],[20,27]];
  const RES = ["W", "D", "L"], SCORE = [1, 0.5, 0];
  const shardCache = new Map();
  const nameOf = new Map();          // sp_id -> 이름
  let idx, exp, labelOfFi = [];

  const ready = fetch("data/index.json", { cache: "no-cache" }).then(r => {
    if (!r.ok) throw new Error("index.json을 불러오지 못했습니다");
    return r.json();
  }).then(j => {
    idx = j;
    exp = new Map(Object.entries(j.exp).map(([k, v]) => [Number(k), v]));
    labelOfFi = j.formations.map(formationLabel);
    for (const [sp, name] of Object.entries(j.names)) nameOf.set(+sp, name);
  });

  /* 선수 파일 하나를 읽어 행을 {..., sp:[선수id...]} 형태로 바꿔 둔다 */
  function loadShard(sp, pos) {
    const key = `${sp}_${pos}`;
    if (!shardCache.has(key)) {
      shardCache.set(key, fetch(`data/a/${key}.json`).then(r => {
        if (!r.ok) throw new Error("이 선수의 데이터가 없습니다");
        return r.json();
      }).then(s => {
        s.p.forEach((id, i) => nameOf.set(id, s.n[i]));
        return s.r.map(r => ({ t: r[0], res: r[1], gf: r[2], ga: r[3], g: r[4], og: r[5], ar: r[6],
                               fi: r[7], sp: r[8].map(i => s.p[i]), gr: r[9] }));
      }));
    }
    return shardCache.get(key);
  }

  const base = sp => sp % 1e6;
  async function anchorRows(p) {
    const a = +p.anchor, pos = +p.pos;
    const sps = p.merge_season === "1"
      ? idx.anchors.filter(([sp, ps]) => ps === pos && base(sp) === base(a)).map(x => x[0])
      : [a];
    if (!sps.length) sps.push(a);
    const parts = await Promise.all(sps.map(sp => loadShard(sp, pos)));
    return parts.flat();
  }

  const bucket = d => Math.floor(d * 2 + 0.5) / 2;
  function expected(diff) {
    const b = bucket(diff);
    if (exp.has(b)) return exp.get(b);
    let best = null;
    for (const k of exp.keys()) if (best === null || Math.abs(k - b) < Math.abs(best - b)) best = k;
    return best === null ? 0.5 : exp.get(best);
  }
  function wilson(w, n, z = 1.96) {
    if (!n) return 0;
    const p = w / n, den = 1 + z * z / n, c = p + z * z / (2 * n);
    return (c - z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den;
  }
  // index.json의 시즌 정보는 [이름, 아이콘 주소] (이전 빌드는 이름 문자열만)
  const seasonRec = sp => idx.seasons[Math.floor(sp / 1e6)];
  const seasonOf = sp => { const r = seasonRec(sp); return r ? (Array.isArray(r) ? r[0] : r) : String(Math.floor(sp / 1e6)); };
  const seasonImg = sp => { const r = seasonRec(sp); return Array.isArray(r) ? r[1] || null : null; };
  function formationLabel(sig) {
    const codes = sig.split("-").map(Number);
    const counts = LINES.map(([a, b]) => codes.filter(c => c >= a && c <= b).length);
    return [counts[0], ...counts.slice(1).filter(Boolean)].join("-");
  }
  const since = days => days ? Math.floor((Date.now() - days * 864e5) / 36e5) : 0;
  function filterRows(rows, p) {
    const s = since(+p.days);
    const gmin = p.gmin === "" || p.gmin == null ? null : +p.gmin * 10;
    const gmax = p.gmax === "" || p.gmax == null ? null : +p.gmax * 10;
    return rows.filter(r => (!s || r.t >= s) && (gmin === null || r.g >= gmin) && (gmax === null || r.g <= gmax));
  }
  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));

  const posCache = new Map();
  function loadPos(pos) {
    if (!posCache.has(pos)) {
      posCache.set(pos, fetch(`data/pos/${pos}.json`).then(r => {
        if (!r.ok) throw new Error("이 포지션의 데이터가 없습니다");
        return r.json();
      }).then(d => {
        for (const [sp, n] of Object.entries(d.n)) if (!nameOf.has(+sp)) nameOf.set(+sp, n);
        return d;
      }));
    }
    return posCache.get(pos);
  }

  let cardsPromise;
  function loadCards() {
    if (!cardsPromise) cardsPromise = fetch("data/cards.json").then(r => r.ok ? r.json() : null).catch(() => null);
    return cardsPromise;
  }
  const mean = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0;
  function welch(a, b) {
    const ma = mean(a), mb = mean(b);
    const va = a.reduce((s, x) => s + (x - ma) ** 2, 0) / Math.max(1, a.length - 1);
    const vb = b.reduce((s, x) => s + (x - mb) ** 2, 0) / Math.max(1, b.length - 1);
    const se = Math.sqrt(va / a.length + vb / b.length);
    return se ? (ma - mb) / se : 0;
  }
  /* 수치 특징 하나에 대해 '이 값 이상 vs 미만'으로 나눴을 때 성과 차이가 가장 뚜렷한 기준값 */
  function bestSplit(xs) {
    xs.sort((a, b) => a.v - b.v);
    const n = xs.length, minSide = Math.max(5, Math.floor(n * 0.15));
    let best = null;
    const values = [...new Set(xs.map(x => x.v))];
    for (const t of values) {
      const lo = xs.filter(x => x.v < t).map(x => x.y), hi = xs.filter(x => x.v >= t).map(x => x.y);
      if (lo.length < minSide || hi.length < minSide) continue;
      const tt = welch(hi, lo);
      if (!best || Math.abs(tt) > Math.abs(best.t))
        best = { threshold: t, n_in: hi.length, n_out: lo.length, m_in: mean(hi), m_out: mean(lo), diff: mean(hi) - mean(lo), t: tt };
    }
    if (!best) return null;
    if (best.diff >= 0) best.rule = `≥ ${best.threshold}`;
    else {  // 낮을수록 좋은 경우: '미만' 그룹을 기준 그룹으로 뒤집어 표시
      best = { ...best, rule: `< ${best.threshold}`, n_in: best.n_out, n_out: best.n_in,
               m_in: best.m_out, m_out: best.m_in, diff: -best.diff, t: -best.t };
    }
    return best;
  }

  /* 포지션 집계에서 선수별 성적과 동료 대비 차이를 계산 (튀는 선수·공통점 탭 공용)
   * 각 차이는 경기 수가 적을수록 0으로 줄인 값(n/(n+K) 배). */
  const ROLE = pos => pos === 0 ? "gk" : pos <= 8 ? "def" : pos <= 16 ? "mid" : "att";
  async function playerStats(p) {
    const d = await loadPos(+p.pos);
    const fi = p.form ? d.f.indexOf(p.form) : -1;
    const gmin = p.gmin === "" || p.gmin == null ? 0 : +p.gmin;
    const gmax = p.gmax === "" || p.gmax == null ? 99 : +p.gmax;
    const K = +p.k || 30, minGames = +p.min_games || 30;
    const Z = () => ({ n: 0, w: 0, d: 0, rn: 0, rs: 0, adj: 0, g: 0, a: 0, sn: 0, df: 0, pt: 0, ps: 0, gs: 0 });
    const by = new Map(), T = Z();
    for (const e of d.e) {
      const [sp, f, g, n, w, dr, rn, rs, adj] = e;
      if (fi >= 0 && f !== fi) continue;
      if (g < gmin || g > gmax) continue;
      const a = by.get(sp) || Z();
      for (const x of [a, T]) {
        x.n += n; x.w += w; x.d += dr; x.rn += rn; x.rs += rs / 100; x.adj += adj / 1000;
        x.g += e[9] || 0; x.a += e[10] || 0; x.sn += e[11] || 0; x.df += e[12] || 0; x.pt += e[13] || 0; x.ps += e[14] || 0;
      }
      a.gs += g * n;
      by.set(sp, a);
    }
    if (!T.n) return { T: null, rows: [] };
    T.players = by.size;
    const rows = [];
    for (const [sp, a] of by) {
      if (a.n < minGames) continue;
      const on = T.n - a.n;
      if (on < minGames) continue;
      const sh = a.n / (a.n + K);
      const pwr = (T.w - a.w) / on, padj = (T.adj - a.adj) / on;
      const prat = T.rn - a.rn > 0 ? (T.rs - a.rs) / (T.rn - a.rn) : null;
      const wr = a.w / a.n, rat = a.rn ? a.rs / a.rn : null;
      // 개인 기록: 공격 포인트는 모든 경기, 수비 기록·패스는 확장 기록이 있는 경기만
      const ap = (a.g + a.a) / a.n, pap = (T.g + T.a - a.g - a.a) / on;
      const osn = T.sn - a.sn, shx = a.sn / (a.sn + K);
      const dfg = a.sn ? a.df / a.sn : null, pdfg = osn > 0 ? (T.df - a.df) / osn : null;
      const pp = a.pt ? a.ps / a.pt : null, ppp = T.pt - a.pt > 0 ? (T.ps - a.ps) / (T.pt - a.pt) : null;
      rows.push({
        sp_id: sp, name: nameOf.get(sp) || `#${sp}`, season: seasonOf(sp), season_img: seasonImg(sp),
        n: a.n, w: a.w, d: a.d, l: a.n - a.w - a.d, grade: a.gs / a.n,
        win_rate: wr, peer_win: pwr, win_diff: sh * (wr - pwr), adj_diff: 100 * sh * (a.adj / a.n - padj),
        rating: rat, peer_rating: prat, rating_diff: rat != null && prat != null ? sh * (rat - prat) : null,
        gpg: a.g / a.n, apg: a.a / a.n, ap_diff: sh * (ap - pap),
        defpg: dfg, def_diff: dfg != null && pdfg != null ? shx * (dfg - pdfg) : null, ext_n: a.sn,
        pass_pct: pp, pass_diff: pp != null && ppp != null ? shx * (pp - ppp) * 100 : null,
        z: pwr > 0 && pwr < 1 ? (a.w - a.n * pwr) / Math.sqrt(a.n * pwr * (1 - pwr)) : 0,
      });
    }
    composite(rows, ROLE(+p.pos));
    return { T, rows };
  }

  /* 종합 점수: 각 지표를 이 조건의 선수들 사이에서 표준화(z)한 뒤 가중 평균.
   *  체급 보정(승리 기여) 50% + 평점 25% + 포지션 역할 기록 25%
   *  역할 기록: 공격수=공격 포인트, 수비수=수비 기록, 미드필더=둘의 평균, 골키퍼=없음(체급 65%·평점 35%) */
  function composite(rows, role) {
    const sd = key => {
      const v = rows.map(r => r[key]).filter(x => x != null);
      if (v.length < 3) return null;
      const m = mean(v), s = Math.sqrt(mean(v.map(x => (x - m) ** 2)));
      return s > 1e-9 ? s : null;
    };
    const S = { adj: sd("adj_diff"), rat: sd("rating_diff"), ap: sd("ap_diff"), df: sd("def_diff") };
    for (const r of rows) {
      const parts = [];
      if (S.adj) parts.push([r.adj_diff / S.adj, role === "gk" ? 0.65 : 0.5]);
      if (S.rat && r.rating_diff != null) parts.push([r.rating_diff / S.rat, role === "gk" ? 0.35 : 0.25]);
      const zap = S.ap ? r.ap_diff / S.ap : null, zdf = S.df && r.def_diff != null ? r.def_diff / S.df : null;
      const roleZ = role === "att" ? zap : role === "def" ? (zdf ?? null)
                  : role === "mid" ? (zap != null && zdf != null ? (zap + zdf) / 2 : zap) : null;
      if (roleZ != null) parts.push([roleZ, 0.25]);
      const wsum = parts.reduce((s, x) => s + x[1], 0);
      r.score = wsum ? parts.reduce((s, x) => s + x[0] * x[1], 0) / wsum : 0;
    }
  }

  const handlers = {
    /* 이상치 탭: 포지션에 존재하는 포메이션 계열 목록 */
    async "/api/outlier_meta"(p) {
      const d = await loadPos(+p.pos);
      const cnt = new Map();
      for (const e of d.e) cnt.set(e[1], (cnt.get(e[1]) || 0) + e[3]);
      return [...cnt].sort((a, b) => b[1] - a[1]).map(([i, n]) => ({ label: d.f[i], games: n }));
    },

    /* 이상치 탭: 같은 포지션·포메이션 계열·강화 구간 안에서 동료 대비 튀는 선수 */
    async "/api/outliers"(p) {
      const [{ T, rows }, cards] = await Promise.all([playerStats(p), loadCards()]);
      if (!T) return { peers: null, rows: [] };
      const key = p.sort || "adj_diff", dir = p.dir === "asc" ? 1 : -1;
      rows.sort((a, b) => ((a[key] ?? 0) - (b[key] ?? 0)) * dir);
      rows.forEach((r, i) => {
        r.rank = i + 1;
        const c = cards && cards.c[r.sp_id];
        if (c) r.profile = { ovr: c[0], height: c[1], weight: c[2], foot_l: c[4], foot_r: c[5], main_foot: c[8] ?? -1, pay: c[9] ?? null,
                             clubs: c[10] == null ? null : c[10].map(v => (v >= 0 ? cards.clubs[v] : cards.clubs[-v - 1] + " (임대)")) };
      });
      // 팀 컬러(클럽 경력) 필터: 정확히 일치하는 클럽이 있으면 그 클럽만, 아니면 이름이 포함된 클럽들
      const club = String(p.club || "").trim();
      let clubOk = () => true, clubMatched = [];
      if (club && cards && cards.clubs) {
        const exact = cards.clubs.indexOf(club);
        const ids = new Set(exact >= 0 ? [exact] : cards.clubs.map((c, i) => c.includes(club) ? i : -1).filter(i => i >= 0));
        clubMatched = [...ids].map(i => cards.clubs[i]);
        const withLoan = p.loan !== "0";
        clubOk = sp => {
          const c = cards.c[sp];
          return !!c && (c[10] || []).some(v => v >= 0 ? ids.has(v) : withLoan && ids.has(-v - 1));
        };
      }
      // 이름 검색: 순위는 전체 기준으로 유지한 채 일치하는 선수만 보여 줌 (최소 경기 미달이면 별도 표시)
      const q = String(p.q || "").trim().toLowerCase();
      const match = r => (!q || r.name.toLowerCase().includes(q)) && clubOk(r.sp_id);
      let shown = q || club ? rows.filter(match) : rows;
      shown = shown.slice(0, q || club ? 500 : +p.limit || 50);
      let below = [], clubInfo = null;
      if (club && cards) {
        // 진단용: 최소 경기와 관계없이 이 조건에서 뛴 모든 선수 중 클럽 경력 수집 여부와 해당 클럽 선수
        const all = await playerStats({ ...p, min_games: 1 });
        const known = all.rows.filter(r => cards.c[r.sp_id] && cards.c[r.sp_id][10] != null);
        clubInfo = { players: all.rows.length, known: known.length,
                     matched_any: all.rows.filter(r => clubOk(r.sp_id)).length };
      }
      if (q || club) {
        const all = await playerStats({ ...p, min_games: 1 });
        const have = new Set(shown.map(r => r.sp_id));
        below = all.rows.filter(r => match(r) && !have.has(r.sp_id)).sort((a, b) => b.n - a.n)
          .map(r => ({ sp_id: r.sp_id, name: r.name, season: r.season, season_img: r.season_img, n: r.n }));
      }
      return { peers: { n: T.n, win_rate: T.w / T.n, rating: T.rn ? T.rs / T.rn : null, players: T.players },
               rows: shown, total: rows.length, query: q, below_min: below,
               club, club_matched: clubMatched.slice(0, 8), club_count: club ? rows.filter(r => clubOk(r.sp_id)).length : null,
               club_info: clubInfo };
    },

    /* 공통점 탭: 성적이 좋은 선수들이 공유하는 카드 특징 */
    async "/api/common"(p) {
      const cards = await loadCards();
      if (!cards) return { error: "nocards" };
      const { T, rows } = await playerStats(p);
      if (!T) return { error: "nodata" };
      const outcome = p.outcome === "rating_diff" ? "rating_diff" : "adj_diff";
      const pts = [];
      for (const r of rows) {
        const c = cards.c[r.sp_id];
        if (!c || r[outcome] == null) continue;
        pts.push({ r, c, y: r[outcome] });
      }
      const res = { analyzed: pts.length, eligible: rows.length, outcome, findings: [], profile: [] };
      if (pts.length < 20) return res;

      // OVR 영향 제거: y = a + b*OVR 직선을 빼고 남은 값으로 분석
      if (p.ovr_adjust === "1") {
        const q = pts.filter(x => x.c[0] != null);
        const mx = mean(q.map(x => x.c[0])), my = mean(q.map(x => x.y));
        let sxy = 0, sxx = 0;
        for (const x of q) { sxy += (x.c[0] - mx) * (x.y - my); sxx += (x.c[0] - mx) ** 2; }
        const b = sxx ? sxy / sxx : 0;
        for (const x of pts) x.y = x.c[0] != null ? x.y - (my + b * (x.c[0] - mx)) : x.y - my;
        res.ovr_slope = b;
      }

      const num = [["키", c => c[1]], ["몸무게", c => c[2]], ["약발", c => Math.min(c[4], c[5])]];
      if (p.ovr_adjust !== "1") num.unshift(["OVR", c => c[0]]);
      const isGK = +p.pos === 0;
      cards.stats.forEach((n, i) => { if (n.startsWith("GK") === isGK) num.push([n, c => c[6][i]]); });

      for (const [name, f] of num) {
        const xs = pts.filter(x => f(x.c) != null).map(x => ({ v: f(x.c), y: x.y }));
        if (xs.length < 20) continue;
        const best = bestSplit(xs);
        if (best) res.findings.push({ feature: name, kind: "num", ...best });
      }
      const cat = [];
      cards.bodies.forEach((b, i) => cat.push([`체형: ${b}`, c => c[3] === i]));
      cat.push(["양발 (약발 5)", c => Math.min(c[4], c[5]) >= 5]);
      cat.push(["고유 체형", c => c[11] === 1]);
      for (const [name, f] of cat) {
        const a = pts.filter(x => f(x.c)).map(x => x.y), b = pts.filter(x => !f(x.c)).map(x => x.y);
        if (a.length < 5 || b.length < 5) continue;
        res.findings.push({ feature: name, kind: "cat", rule: `${name} 보유`, n_in: a.length, n_out: b.length,
                            m_in: mean(a), m_out: mean(b), diff: mean(a) - mean(b), t: welch(a, b) });
      }
      // 주발: 왼발 vs 오른발 (주발을 알 수 없는 카드는 제외하고 비교)
      {
        const L = pts.filter(x => x.c[8] === 0).map(x => x.y), R = pts.filter(x => x.c[8] === 1).map(x => x.y);
        if (L.length >= 5 && R.length >= 5)
          res.findings.push({ feature: "주발: 왼발", kind: "foot", rule: "왼발 (오른발과 비교)", n_in: L.length, n_out: R.length,
                              m_in: mean(L), m_out: mean(R), diff: mean(L) - mean(R), t: welch(L, R) });
      }
      res.findings.sort((a, b) => Math.abs(b.t) - Math.abs(a.t));

      // 특성은 별도 표: 3장 이상 보유한 특성은 모두 비교
      const tinfo = cards.trait_info || [];
      res.traits = [];
      cards.traits.forEach((name, i) => {
        const a = pts.filter(x => x.c[7].includes(i)).map(x => x.y), b = pts.filter(x => !x.c[7].includes(i)).map(x => x.y);
        if (a.length < 3 || b.length < 3) return;
        const [icon, ai, group] = tinfo[i] || ["", 0, ""];
        res.traits.push({ name, icon, ai: !!ai, group: group || "", n_in: a.length, n_out: b.length,
                          m_in: mean(a), m_out: mean(b), diff: mean(a) - mean(b), t: welch(a, b) });
      });
      res.traits.sort((a, b) => b.diff - a.diff);
      res.trait_groups = [...new Set(tinfo.map(x => x[2]).filter(Boolean))];
      res.findings = res.findings.slice(0, +p.limit || 15);

      // 잘하는 상위 25% vs 못하는 하위 25% 평균 비교
      const sorted = [...pts].sort((a, b) => b.y - a.y), q = Math.max(5, Math.floor(pts.length / 4));
      const top = sorted.slice(0, q), bot = sorted.slice(-q);
      for (const [name, f] of num) {
        const all = pts.map(x => f(x.c)).filter(v => v != null);
        const sd = Math.sqrt(mean(all.map(v => (v - mean(all)) ** 2))) || 1;
        const mt = mean(top.map(x => f(x.c)).filter(v => v != null)), mb = mean(bot.map(x => f(x.c)).filter(v => v != null));
        res.profile.push({ feature: name, top: mt, bottom: mb, z: (mt - mb) / sd });
      }
      res.profile.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));
      res.profile = res.profile.slice(0, 12);
      res.quartile = q;
      return res;
    },

    /* 팀 컬러 입력칸 자동완성용 클럽 목록 (카드가 많은 클럽 순) */
    async "/api/clubs"() {
      const cards = await loadCards();
      if (!cards || !cards.clubs) return [];
      const cnt = new Array(cards.clubs.length).fill(0);
      for (const c of Object.values(cards.c)) for (const v of c[10] || []) cnt[v >= 0 ? v : -v - 1]++;
      return cards.clubs.map((name, i) => [name, cnt[i]]).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    },

    /* 이상치 탭에서 선수를 눌러 조합 분석으로 넘어갈 때 */
    "/api/player"(p) {
      const sp = +p.sp;
      const positions = idx.anchors.filter(a => a[0] === sp).map(a => [POS[a[1]], a[2]]).sort((a, b) => b[1] - a[1]);
      return { sp_id: sp, name: nameOf.get(sp) || idx.names[sp] || `#${sp}`, season: seasonOf(sp), season_img: seasonImg(sp),
               games: positions.reduce((s, x) => s + x[1], 0), positions };
    },

    "/api/status"() {
      return { matches: idx.matches, first: idx.first, last: idx.last, built_at: idx.built_at };
    },

    "/api/players"(p) {
      const q = String(p.q).toLowerCase();
      const by = new Map();
      for (const [sp, pos, n] of idx.anchors) {
        const name = idx.names[sp] || "";
        if (!name.toLowerCase().includes(q)) continue;
        const e = by.get(sp) || { sp_id: sp, name, season: seasonOf(sp), season_img: seasonImg(sp), games: 0, positions: [] };
        e.games += n; e.positions.push([POS[pos], n]); by.set(sp, e);
      }
      return [...by.values()].map(e => (e.positions.sort((a, b) => b[1] - a[1]), e))
        .sort((a, b) => b.games - a.games).slice(0, 15);
    },

    async "/api/formations"(p) {
      const fam = p.merge_form === "1";
      const agg = new Map();
      for (const r of filterRows(await anchorRows(p), p)) {
        const key = fam ? labelOfFi[r.fi] : idx.formations[r.fi];
        const a = agg.get(key) || { n: 0, w: 0, members: new Map() };
        a.n++; if (r.res === 0) a.w++;
        a.members.set(r.fi, (a.members.get(r.fi) || 0) + 1);
        agg.set(key, a);
      }
      return [...agg].sort((a, b) => b[1].n - a[1].n).slice(0, 30).map(([key, a]) => {
        const topFi = [...a.members].sort((x, y) => y[1] - x[1])[0][0];
        const codes = idx.formations[topFi].split("-").map(Number);
        return { sig: key, label: fam ? key : formationLabel(key), positions: codes.map(c => POS[c]), codes,
                 variants: a.members.size, games: a.n, win_rate: a.w / a.n };
      });
    },

    async "/api/combos"(p) {
      const fam = p.merge_form === "1", mergeS = p.merge_season === "1";
      const slots = String(p.slots).split(",").filter(Boolean).map(Number).slice(0, 2);
      // 좌우 대칭인 두 자리(LCM·RCM, LAM·RAM 등)일 때만 좌우를 합친다
      const MIRROR = {15:13,13:15,19:17,17:19,26:24,24:26,7:3,3:7,6:4,4:6,11:9,9:11,16:12,12:16,27:23,23:27,8:2,2:8,22:20,20:22};
      const mirror = p.mirror === "1" && slots.length === 2 && MIRROR[slots[0]] === slots[1];
      const minGames = +p.min_games || 1, K = +p.k || 20;
      const pkey = sp => mergeS ? base(sp) : sp;
      const label = k => mergeS
        ? { sp_id: k, name: nameOf.get(k) || [...nameOf].find(([sp]) => base(sp) === k)?.[1] || `#${k}`, season: "전 시즌" }
        : { sp_id: k, name: nameOf.get(k) || `#${k}`, season: seasonOf(k), season_img: seasonImg(k) };
      if (mergeS) for (const [sp, n] of nameOf) if (!nameOf.has(base(sp))) nameOf.set(base(sp), n);

      const slotNames = slots.map(c => POS[c]);
      const blank = () => ({ n: 0, w: 0, d: 0, l: 0, gd: 0, adj: 0, ar: 0, ar_n: 0, grades: slots.map(() => 0) });
      const tot = blank(), groups = new Map();
      const marg = [new Map(), new Map()];            // 자리별(좌우 무관이면 0번에 합산) 개인 성적
      for (const r of filterRows(await anchorRows(p), p)) {
        const sig = idx.formations[r.fi];
        if ((fam ? labelOfFi[r.fi] : sig) !== p.formation) continue;
        const codes = sig.split("-").map(Number);
        const si = slots.map(c => codes.indexOf(c));
        if (si.some(i => i < 0)) continue;
        let items = si.map(i => ({ k: pkey(r.sp[i]), g: r.gr[i] }));
        if (mirror) items.sort((a, b) => a.k - b.k);
        const key = items.map(x => x.k).join("|");
        if (!groups.has(key)) groups.set(key, blank());
        const win = r.res === 0 ? 1 : 0;
        const adj = SCORE[r.res] - expected((r.g - r.og) / 10);
        for (const g of [tot, groups.get(key)]) {
          g.n++; g[RES[r.res].toLowerCase()]++; g.gd += r.gf - r.ga; g.adj += adj;
          if (r.ar) { g.ar += r.ar / 100; g.ar_n++; }
          items.forEach((x, j) => g.grades[j] += x.g);
        }
        items.forEach((x, j) => {
          const m = marg[mirror ? 0 : j], e = m.get(x.k) || { n: 0, w: 0 };
          e.n++; e.w += win; m.set(x.k, e);
        });
      }
      if (!tot.n) return { baseline: null, rows: [], matrix: null, slots: slotNames };
      const bwr = tot.w / tot.n;
      const eff = (j, k) => {
        const e = marg[mirror ? 0 : j].get(k);
        return e ? (e.w + K * bwr) / (e.n + K) - bwr : 0;
      };
      const predOf = key => {
        if (slots.length < 2) return bwr;
        const [a, b] = key.split("|").map(Number);
        return clamp(bwr + eff(0, a) + eff(1, b), 0.02, 0.98);
      };
      const fin = (g, pred) => {
        const shrunk = (g.w + K * pred) / (g.n + K);
        return { n: g.n, w: g.w, d: g.d, l: g.l, win_rate: g.w / g.n, shrunk,
          pred: slots.length === 2 ? pred : null, synergy: slots.length === 2 ? shrunk - pred : null,
          lower95: wilson(g.w, g.n), adj_pp: 100 * g.adj / g.n, gd: g.gd / g.n,
          anchor_rating: g.ar_n ? g.ar / g.ar_n : null, grades: g.grades.map(x => x / g.n) };
      };

      const out = [];
      for (const [key, g] of groups) {
        if (g.n < minGames) continue;
        out.push({ players: key.split("|").map(x => label(+x)), ...fin(g, predOf(key)) });
      }
      out.sort((a, b) => b.shrunk - a.shrunk);

      let matrix = null;
      if (slots.length === 2) {
        const cnt = mirror ? [marg[0], marg[0]] : marg;
        const top = cnt.map(c => [...c.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 8).map(e => e[0]));
        matrix = {
          mirror,
          rows: top[0].map(x => ({ ...label(x), games: cnt[0].get(x).n })),
          cols: top[1].map(x => ({ ...label(x), games: cnt[1].get(x).n })),
          cells: top[0].map(a => top[1].map(b => {
            const key = mirror ? [a, b].sort((x, y) => x - y).join("|") : `${a}|${b}`;
            const g = groups.get(key);
            if (!g) return null;
            const f = fin(g, predOf(key));
            return { n: g.n, shrunk: f.shrunk, adj_pp: f.adj_pp, synergy: f.synergy };
          })),
        };
      }
      return { baseline: fin(tot, bwr), rows: out, matrix, slots: slotNames, mirror,
               merge_season: mergeS, combos_total: groups.size };
    },
  };

  window.FCEngine = {
    async call(path, params = {}) {
      await ready;
      const h = handlers[path];
      if (!h) throw new Error(`알 수 없는 경로: ${path}`);
      return h(params);
    },
  };
})();

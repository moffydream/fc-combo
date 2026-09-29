/* 분할 정적 사이트용 분석 엔진. build_site.py 가 만든 data/index.json 과
 * data/a/{선수}_{포지션}.json 을 읽어 analytics.py 와 같은 계산을 한다. */
(function () {
  const POS = {0:"GK",1:"SW",2:"RWB",3:"RB",4:"RCB",5:"CB",6:"LCB",7:"LB",8:"LWB",9:"RDM",10:"CDM",11:"LDM",
    12:"RM",13:"RCM",14:"CM",15:"LCM",16:"LM",17:"RAM",18:"CAM",19:"LAM",20:"RF",21:"CF",22:"LF",23:"RW",
    24:"RS",25:"ST",26:"LS",27:"LW"};
  const LINES = [[1,8],[9,11],[12,16],[17,19],[20,27]];
  const RES = ["W", "D", "L"], SCORE = [1, 0.5, 0];
  const shards = new Map();
  let idx, exp;

  const ready = fetch("data/index.json", { cache: "no-cache" }).then(r => {
    if (!r.ok) throw new Error("index.json을 불러오지 못했습니다");
    return r.json();
  }).then(j => { idx = j; exp = new Map(Object.entries(j.exp).map(([k, v]) => [Number(k), v])); });

  async function shard(sp, pos) {
    const key = `${sp}_${pos}`;
    if (!shards.has(key)) {
      shards.set(key, fetch(`data/a/${key}.json`).then(r => {
        if (!r.ok) throw new Error("이 선수의 데이터가 없습니다");
        return r.json();
      }));
    }
    return shards.get(key);
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
  const season = sp => idx.seasons[Math.floor(sp / 1e6)] || String(Math.floor(sp / 1e6));
  function formationLabel(sig) {
    const codes = sig.split("-").map(Number);
    const counts = LINES.map(([a, b]) => codes.filter(c => c >= a && c <= b).length);
    return [counts[0], ...counts.slice(1).filter(Boolean)].join("-");
  }
  // 행의 시각은 1970년부터의 시간 수 (빌드한 PC/러너의 로컬 시각 기준)
  const since = days => days ? Math.floor((Date.now() - days * 864e5) / 36e5) : 0;
  function filterRows(rows, p) {
    const s = since(+p.days);
    const gmin = p.gmin === "" || p.gmin == null ? null : +p.gmin * 10;
    const gmax = p.gmax === "" || p.gmax == null ? null : +p.gmax * 10;
    return rows.filter(r => (!s || r[0] >= s) && (gmin === null || r[4] >= gmin) && (gmax === null || r[4] <= gmax));
  }

  const handlers = {
    "/api/status"() {
      return { matches: idx.matches, first: idx.first, last: idx.last, built_at: idx.built_at };
    },

    "/api/players"(p) {
      const q = String(p.q).toLowerCase();
      const by = new Map();
      for (const [sp, pos, n] of idx.anchors) {
        const name = idx.names[sp] || "";
        if (!name.toLowerCase().includes(q)) continue;
        const e = by.get(sp) || { sp_id: sp, name, season: season(sp), games: 0, positions: [] };
        e.games += n; e.positions.push([POS[pos], n]); by.set(sp, e);
      }
      return [...by.values()].map(e => (e.positions.sort((a, b) => b[1] - a[1]), e))
        .sort((a, b) => b.games - a.games).slice(0, 15);
    },

    async "/api/formations"(p) {
      const s = await shard(p.anchor, p.pos);
      const agg = new Map();
      for (const r of filterRows(s.r, p)) {
        const a = agg.get(r[7]) || [0, 0];
        a[0]++; if (r[1] === 0) a[1]++;
        agg.set(r[7], a);
      }
      return [...agg].sort((a, b) => b[1][0] - a[1][0]).slice(0, 30).map(([fi, [n, w]]) => {
        const sig = idx.formations[fi], codes = sig.split("-").map(Number);
        return { sig, label: formationLabel(sig), positions: codes.map(c => POS[c]), codes, games: n, win_rate: w / n };
      });
    },

    async "/api/combos"(p) {
      const s = await shard(p.anchor, p.pos);
      const fi = idx.formations.indexOf(p.formation);
      const codes = p.formation.split("-").map(Number);
      const slots = String(p.slots).split(",").filter(Boolean).map(Number).slice(0, 2);
      const si = slots.map(c => codes.indexOf(c));
      const minGames = +p.min_games || 1, k = +p.k || 20;
      const slotNames = slots.map(c => POS[c]);

      const blank = () => ({ n: 0, w: 0, d: 0, l: 0, gd: 0, adj: 0, ar: 0, ar_n: 0, grades: slots.map(() => 0) });
      const base = blank(), groups = new Map();
      for (const r of filterRows(s.r, p)) {
        if (r[7] !== fi) continue;
        const key = si.map(i => r[8][i]).join("|");
        if (!groups.has(key)) groups.set(key, blank());
        const res = RES[r[1]], adj = SCORE[r[1]] - expected((r[4] - r[5]) / 10);
        for (const g of [base, groups.get(key)]) {
          g.n++; g[res.toLowerCase()]++; g.gd += r[2] - r[3]; g.adj += adj;
          if (r[6]) { g.ar += r[6] / 100; g.ar_n++; }
          si.forEach((i, j) => g.grades[j] += r[9][i]);
        }
      }
      if (!base.n) return { baseline: null, rows: [], matrix: null, slots: slotNames };
      const bwr = base.w / base.n;
      const fin = g => ({ n: g.n, w: g.w, d: g.d, l: g.l, win_rate: g.w / g.n,
        shrunk: (g.w + k * bwr) / (g.n + k), lower95: wilson(g.w, g.n), adj_pp: 100 * g.adj / g.n,
        gd: g.gd / g.n, anchor_rating: g.ar_n ? g.ar / g.ar_n : null, grades: g.grades.map(x => x / g.n) });
      const label = i => ({ sp_id: s.p[i], name: s.n[i], season: season(s.p[i]) });

      const out = [];
      for (const [key, g] of groups) {
        if (g.n < minGames) continue;
        out.push({ players: key.split("|").map(x => label(+x)), ...fin(g) });
      }
      out.sort((a, b) => b.shrunk - a.shrunk);

      let matrix = null;
      if (slots.length === 2) {
        const cnt = [new Map(), new Map()];
        for (const [key, g] of groups) key.split("|").map(Number).forEach((x, i) => cnt[i].set(x, (cnt[i].get(x) || 0) + g.n));
        const top = cnt.map(c => [...c.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(e => e[0]));
        matrix = {
          rows: top[0].map(x => ({ ...label(x), games: cnt[0].get(x) })),
          cols: top[1].map(x => ({ ...label(x), games: cnt[1].get(x) })),
          cells: top[0].map(a => top[1].map(b => {
            const g = groups.get(`${a}|${b}`);
            return g ? { n: g.n, shrunk: (g.w + k * bwr) / (g.n + k), adj_pp: 100 * g.adj / g.n } : null;
          })),
        };
      }
      return { baseline: fin(base), rows: out, matrix, slots: slotNames, combos_total: groups.size };
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

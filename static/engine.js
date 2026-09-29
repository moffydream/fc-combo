/* 정적 배포용 분석 엔진.
 * server.py/analytics.py 와 같은 계산을 브라우저에서 sql.js(SQLite WASM)로 수행한다.
 * export_site.py 가 만든 data.db.gz 를 읽는다. */
(function () {
  const SQLJS = "https://cdn.jsdelivr.net/npm/sql.js@1.13.0/dist/";
  const POS = {0:"GK",1:"SW",2:"RWB",3:"RB",4:"RCB",5:"CB",6:"LCB",7:"LB",8:"LWB",9:"RDM",10:"CDM",11:"LDM",
    12:"RM",13:"RCM",14:"CM",15:"LCM",16:"LM",17:"RAM",18:"CAM",19:"LAM",20:"RF",21:"CF",22:"LF",23:"RW",
    24:"RS",25:"ST",26:"LS",27:"LW",28:"SUB"};
  const LINES = [[1,8],[9,11],[12,16],[17,19],[20,27]];
  const SCORE = {W:1, D:0.5, L:0};

  let db, seasons = {}, expTable = null;

  const ready = (async () => {
    const SQL = await initSqlJs({ locateFile: f => SQLJS + f });
    const res = await fetch("data.db.gz", { cache: "no-cache" });
    if (!res.ok) throw new Error("data.db.gz를 불러오지 못했습니다");
    const stream = res.body.pipeThrough(new DecompressionStream("gzip"));
    const buf = new Uint8Array(await new Response(stream).arrayBuffer());
    db = new SQL.Database(buf);
    for (const r of rows("SELECT season_id, label FROM meta_season")) seasons[r.season_id] = r.label;
  })();

  function rows(sql, params = []) {
    const st = db.prepare(sql);
    st.bind(params);
    const out = [];
    while (st.step()) out.push(st.getAsObject());
    st.free();
    return out;
  }

  const bucket = d => Math.floor(d * 2 + 0.5) / 2;
  function expected(diff) {
    if (!expTable) {
      const agg = new Map();
      for (const r of rows("SELECT avg_grade - opp_avg_grade AS d, result FROM teams")) {
        const b = bucket(r.d), a = agg.get(b) || [0, 0];
        a[0] += SCORE[r.result]; a[1] += 1; agg.set(b, a);
      }
      expTable = new Map([...agg].map(([b, [s, n]]) => [b, (s + 5) / (n + 10)]));
    }
    const b = bucket(diff);
    if (expTable.has(b)) return expTable.get(b);
    let best = null;
    for (const k of expTable.keys()) if (best === null || Math.abs(k - b) < Math.abs(best - b)) best = k;
    return best === null ? 0.5 : expTable.get(best);
  }

  function wilson(w, n, z = 1.96) {
    if (!n) return 0;
    const p = w / n, den = 1 + z * z / n, c = p + z * z / (2 * n);
    return (c - z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den;
  }

  function labels(ids) {
    ids = [...new Set(ids)].filter(x => x != null);
    if (!ids.length) return {};
    const names = {};
    for (const r of rows(`SELECT sp_id, name FROM meta_player WHERE sp_id IN (${ids.map(() => "?").join(",")})`, ids))
      names[r.sp_id] = r.name;
    const out = {};
    for (const s of ids) {
      const sid = Math.floor(s / 1e6);
      out[s] = { sp_id: s, name: names[s] || `#${s}`, season: seasons[sid] || String(sid) };
    }
    return out;
  }

  function formationLabel(sig) {
    const codes = sig.split("-").map(Number);
    const counts = LINES.map(([a, b]) => codes.filter(c => c >= a && c <= b).length);
    return [counts[0], ...counts.slice(1).filter(Boolean)].join("-");
  }

  function dateFrom(days) {
    if (!days) return null;
    const d = new Date(Date.now() - days * 864e5), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function where(p) {
    let sql = "", args = [];
    const df = dateFrom(+p.days);
    if (df) { sql += " AND m.match_date >= ?"; args.push(df); }
    if (p.gmin !== "" && p.gmin != null) { sql += " AND t.avg_grade >= ?"; args.push(+p.gmin); }
    if (p.gmax !== "" && p.gmax != null) { sql += " AND t.avg_grade <= ?"; args.push(+p.gmax); }
    return [sql, args];
  }

  const handlers = {
    "/api/status"() {
      const r = rows("SELECT COUNT(*) n, MIN(match_date) a, MAX(match_date) b FROM matches")[0];
      const built = rows("SELECT value FROM info WHERE key='built_at'")[0];
      return { matches: r.n, first: r.a, last: r.b, built_at: built && built.value };
    },

    "/api/players"(p) {
      const ids = rows("SELECT sp_id FROM meta_player WHERE name LIKE ? LIMIT 300", [`%${p.q}%`]).map(r => r.sp_id);
      if (!ids.length) return [];
      const usage = {};
      for (const r of rows(`SELECT sp_id, pos, COUNT(*) n FROM players WHERE sp_id IN (${ids.map(() => "?").join(",")}) GROUP BY sp_id, pos`, ids))
        (usage[r.sp_id] ||= {})[POS[r.pos]] = r.n;
      const lb = labels(ids);
      return ids.map(s => {
        const u = usage[s] || {};
        return { ...lb[s], games: Object.values(u).reduce((a, b) => a + b, 0),
                 positions: Object.entries(u).sort((a, b) => b[1] - a[1]) };
      }).sort((a, b) => b.games - a.games).slice(0, 15);
    },

    "/api/formations"(p) {
      const [w, a] = where(p);
      return rows("SELECT t.formation f, COUNT(*) n, SUM(t.result='W') w FROM players pa " +
        "JOIN teams t ON t.mid=pa.mid AND t.side=pa.side JOIN matches m ON m.mid=t.mid " +
        `WHERE pa.sp_id=? AND pa.pos=? ${w} GROUP BY t.formation ORDER BY n DESC LIMIT 30`,
        [+p.anchor, +p.pos, ...a]).map(r => {
          const codes = r.f.split("-").map(Number);
          return { sig: r.f, label: formationLabel(r.f), positions: codes.map(c => POS[c]), codes,
                   games: r.n, win_rate: r.w / r.n };
        });
    },

    "/api/combos"(p) {
      const slots = String(p.slots).split(",").filter(Boolean).map(Number).slice(0, 2);
      const minGames = +p.min_games || 1, k = +p.k || 20;
      let joins = "", cols = "";
      slots.forEach((s, i) => {
        joins += ` JOIN players s${i} ON s${i}.mid=t.mid AND s${i}.side=t.side AND s${i}.pos=?`;
        cols += `, s${i}.sp_id AS p${i}, s${i}.grade AS g${i}`;
      });
      const [w, wa] = where(p);
      const data = rows("SELECT t.result, t.gf, t.ga, t.avg_grade, t.opp_avg_grade, pa.rating ar" + cols +
        " FROM players pa JOIN teams t ON t.mid=pa.mid AND t.side=pa.side JOIN matches m ON m.mid=t.mid" +
        joins + ` WHERE pa.sp_id=? AND pa.pos=? AND t.formation=? ${w}`,
        [...slots, +p.anchor, +p.pos, p.formation, ...wa]);

      const blank = () => ({ n: 0, w: 0, d: 0, l: 0, gd: 0, adj: 0, ar: 0, ar_n: 0, tg: 0, grades: slots.map(() => 0) });
      const base = blank(), groups = new Map();
      for (const r of data) {
        const key = slots.map((_, i) => r[`p${i}`]).join("|");
        if (!groups.has(key)) groups.set(key, blank());
        const adj = SCORE[r.result] - expected(r.avg_grade - r.opp_avg_grade);
        for (const g of [base, groups.get(key)]) {
          g.n++; g[r.result.toLowerCase()]++; g.gd += r.gf - r.ga; g.adj += adj; g.tg += r.avg_grade;
          if (r.ar) { g.ar += r.ar; g.ar_n++; }
          slots.forEach((_, i) => g.grades[i] += r[`g${i}`]);
        }
      }
      const slotNames = slots.map(s => POS[s]);
      if (!base.n) return { baseline: null, rows: [], matrix: null, slots: slotNames };
      const bwr = base.w / base.n;
      const fin = g => ({ n: g.n, w: g.w, d: g.d, l: g.l, win_rate: g.w / g.n,
        shrunk: (g.w + k * bwr) / (g.n + k), lower95: wilson(g.w, g.n), adj_pp: 100 * g.adj / g.n,
        gd: g.gd / g.n, anchor_rating: g.ar_n ? g.ar / g.ar_n : null, team_grade: g.tg / g.n,
        grades: g.grades.map(x => x / g.n) });

      const allIds = [...groups.keys()].flatMap(key => key.split("|").map(Number));
      const lb = labels(allIds);
      const out = [];
      for (const [key, g] of groups) {
        if (g.n < minGames) continue;
        out.push({ players: key.split("|").map(x => lb[+x]), ...fin(g) });
      }
      out.sort((a, b) => b.shrunk - a.shrunk);

      let matrix = null;
      if (slots.length === 2) {
        const cnt = [new Map(), new Map()];
        for (const [key, g] of groups) key.split("|").map(Number).forEach((x, i) => cnt[i].set(x, (cnt[i].get(x) || 0) + g.n));
        const top = cnt.map(c => [...c.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(e => e[0]));
        matrix = {
          rows: top[0].map(x => ({ ...lb[x], games: cnt[0].get(x) })),
          cols: top[1].map(x => ({ ...lb[x], games: cnt[1].get(x) })),
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

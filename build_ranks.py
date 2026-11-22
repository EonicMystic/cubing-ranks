#!/usr/bin/env python3
"""Build Sum of Ranks and Kinch Ranks (world, continent and country) from the WCA results export.

Usage: python3 build_ranks.py <WCA_export_tsv.zip | folder_with_tsvs> <output_dir>

Writes into <output_dir>:
  meta.json            event list, regional records and counts (the site recomputes totals from these)
  lb/*.json            leaderboards, one file per world / continent / country
  p/<n>.txt            per-person data, 256 small shards
  p/s_<key>.txt        name search buckets
"""
import json, math, os, re, shutil, sys, tempfile, unicodedata, zipfile
from collections import defaultdict

src, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
if src.endswith('.zip'):
    D = tempfile.mkdtemp()
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        for name in ('persons', 'results', 'countries'):
            z.extract(f'WCA_export_{name}.tsv', D)
        if 'metadata.json' in names:
            z.extract('metadata.json', D)
else:
    D = src

EVENTS = [('333', '3x3x3'), ('222', '2x2x2'), ('444', '4x4x4'), ('555', '5x5x5'), ('666', '6x6x6'),
          ('777', '7x7x7'), ('333bf', '3x3x3 Blindfolded'), ('333fm', 'Fewest Moves'), ('333oh', '3x3x3 One-Handed'),
          ('clock', 'Clock'), ('minx', 'Megaminx'), ('pyram', 'Pyraminx'), ('skewb', 'Skewb'), ('sq1', 'Square-1'),
          ('444bf', '4x4x4 Blindfolded'), ('555bf', '5x5x5 Blindfolded'), ('333mbf', '3x3x3 Multi-Blind')]
EV = [e for e, _ in EVENTS]
EIDX = {e: i for i, e in enumerate(EV)}
NE = len(EV)
HAS_AVG = [e != '333mbf' for e in EV]
BLIND = {EIDX['333bf'], EIDX['444bf'], EIDX['555bf']}
MBF = EIDX['333mbf']
SCOPE_LIMIT = {'w': 250, 'c': 250, 'n': 200}


def rows(name):
    with open(f'{D}/WCA_export_{name}.tsv', encoding='utf-8', errors='replace') as f:
        header = f.readline().rstrip('\n').split('\t')
        idx = {h: i for i, h in enumerate(header)}
        n = len(header)
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) == n:
                yield idx, p


try:
    stamp = json.load(open(f'{D}/metadata.json')).get('export_date', '')
except Exception:
    stamp = ''

# ---------- countries and continents ----------
cont_of_country = {}
for idx, p in rows('countries'):
    cont_of_country[p[idx['id']]] = p[idx['continent_id']].lstrip('_')
conts = sorted(set(cont_of_country.values()))
CIDX = {c: i for i, c in enumerate(conts)}
countries = sorted(cont_of_country)
NIDX = {c: i for i, c in enumerate(countries)}
cont_idx_of_country = [CIDX[cont_of_country[c]] for c in countries]

# ---------- personal bests ----------
pr = [[{} for _ in range(NE)], [{} for _ in range(NE)]]        # [single, average][event] -> {person: value}
for idx, p in rows('results'):
    ev = p[idx['event_id']]
    e = EIDX.get(ev)
    if e is None:
        continue
    pid = p[idx['person_id']]
    b = int(p[idx['best']])
    if b > 0:
        d = pr[0][e]
        if b < d.get(pid, 1 << 60): d[pid] = b
    a = int(p[idx['average']])
    if a > 0 and HAS_AVG[e]:
        d = pr[1][e]
        if a < d.get(pid, 1 << 60): d[pid] = a

pname, pcty = {}, {}
for idx, p in rows('persons'):
    w = p[idx['wca_id']]
    if p[idx['sub_id']] == '1' or w not in pname:
        pname[w] = p[idx['name']]
        pcty[w] = p[idx['country_id']]

people = sorted({pid for k in (0, 1) for e in range(NE) for pid in pr[k][e] if pid in pcty and pcty[pid] in NIDX})
PI = {pid: i for i, pid in enumerate(people)}
NP = len(people)
p_country = [NIDX[pcty[pid]] for pid in people]
p_cont = [cont_idx_of_country[c] for c in p_country]
print('people with a ranked result:', NP)

# ---------- ranks inside world / continent / country ----------
SH = 1 << 20
rankp = [[[None] * NE for _ in range(NP)] for _ in range(2)]    # packed (world, continent, country) rank per person/event
rec = {'w': [[0, 0] for _ in range(NE)], 'c': [[[0, 0] for _ in range(NE)] for _ in conts], 'n': [[[0, 0] for _ in range(NE)] for _ in countries]}
cnt = {'w': [[0, 0] for _ in range(NE)], 'c': [[[0, 0] for _ in range(NE)] for _ in conts], 'n': [[[0, 0] for _ in range(NE)] for _ in countries]}


def tie(st, v):
    st[0] += 1
    if v != st[1]:
        st[1] = v
        st[2] = st[0]
    return st[2]


for k in (0, 1):
    for e in range(NE):
        items = sorted((v, PI[pid]) for pid, v in pr[k][e].items() if pid in PI)
        if not items:
            continue
        ws = [0, None, 0]
        cs = [[0, None, 0] for _ in conts]
        ns = [[0, None, 0] for _ in countries]
        rec['w'][e][k] = items[0][0]
        cnt['w'][e][k] = len(items)
        for v, i in items:
            c, n = p_cont[i], p_country[i]
            rankp[k][i][e] = (tie(ws, v) << 40) | (tie(cs[c], v) << 20) | tie(ns[n], v)
            if not rec['c'][c][e][k]: rec['c'][c][e][k] = v
            if not rec['n'][n][e][k]: rec['n'][n][e][k] = v
        for c in range(len(conts)): cnt['c'][c][e][k] = cs[c][0]
        for n in range(len(countries)): cnt['n'][n][e][k] = ns[n][0]

MASK = SH - 1


def unpack(x):
    return x >> 40, (x >> 20) & MASK, x & MASK


# ---------- Sum of ranks: three scopes (world, continent, country) x three types (single, average, combined) ----------
sums = [[[0] * NP for _ in range(3)] for _ in range(3)]          # [type S,A,C][scope W,C,N][person]
for i in range(NP):
    c, n = p_cont[i], p_country[i]
    for k in (0, 1):
        rk = rankp[k][i]
        sw = sc = sn = 0
        for e in range(NE):
            if k == 1 and not HAS_AVG[e]:
                continue
            x = rk[e]
            if x is None:
                sw += cnt['w'][e][k] + 1; sc += cnt['c'][c][e][k] + 1; sn += cnt['n'][n][e][k] + 1
            else:
                a, b, d = unpack(x)
                sw += a; sc += b; sn += d
        sums[k][0][i] = sw; sums[k][1][i] = sc; sums[k][2][i] = sn
    for s in range(3):
        sums[2][s][i] = sums[0][s][i] + sums[1][s][i]


# ---------- Kinch ranks ----------
def eff(v):
    dd, t = v // 10 ** 7, (v // 100) % 100000
    return (99 - dd) + (3600 - min(t, 3600)) / 3600.0


def kscore(e, ps, pa, rs, ra):
    if e == MBF:
        return 100.0 * eff(ps) / eff(rs) if ps and rs else 0.0
    if e in BLIND:
        a = ra / pa if (pa and ra) else 0.0
        s = rs / ps if (ps and rs) else 0.0
        return 100.0 * max(a, s)
    return 100.0 * ra / pa if (pa and ra) else 0.0


kin = [[0.0] * NP for _ in range(3)]
for i, pid in enumerate(people):
    c, n = p_cont[i], p_country[i]
    tw = tc = tn = 0.0
    for e in range(NE):
        ps, pa = pr[0][e].get(pid, 0), pr[1][e].get(pid, 0)
        if not ps and not pa:
            continue
        tw += kscore(e, ps, pa, rec['w'][e][0], rec['w'][e][1])
        tc += kscore(e, ps, pa, rec['c'][c][e][0], rec['c'][c][e][1])
        tn += kscore(e, ps, pa, rec['n'][n][e][0], rec['n'][n][e][1])
    kin[0][i] = round(tw / NE, 2); kin[1][i] = round(tc / NE, 2); kin[2][i] = round(tn / NE, 2)

# ---------- rank everyone on every metric inside each scope, and collect leaderboards ----------
METRICS = [('k', kin, True), ('S', sums[0], False), ('A', sums[1], False), ('C', sums[2], False)]
pos = {}                                  # (metric, scope index, person) -> rank, kept as arrays
prank = {m: [[0] * NP for _ in range(3)] for m, _, _ in METRICS}
lb = {}                                   # scope key -> metric -> rows


def scope_key(s, i):
    return ('w', 0) if s == 0 else (('c', p_cont[i]) if s == 1 else ('n', p_country[i]))


for m, vals, desc in METRICS:
    for s in range(3):
        v = vals[s]
        order = sorted(range(NP), key=(lambda i: (-v[i], i)) if desc else (lambda i: (v[i], i)))
        states = {}
        for i in order:
            key = scope_key(s, i)
            st = states.get(key)
            if st is None:
                st = states[key] = [0, None, 0]
            r = tie(st, v[i])
            prank[m][s][i] = r
            if r <= SCOPE_LIMIT[key[0]]:
                lb.setdefault(key, {}).setdefault(m, []).append([r, people[i], pname.get(people[i], people[i]), v[i], p_country[i]])

# ---------- write leaderboards ----------
shutil.rmtree(os.path.join(out_dir, 'lb'), ignore_errors=True)
os.makedirs(os.path.join(out_dir, 'lb'))
files = {}
for (kind, ix), data in lb.items():
    fn = 'world.json' if kind == 'w' else f'{kind}{ix}.json'
    files[(kind, ix)] = fn
    with open(os.path.join(out_dir, 'lb', fn), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

from collections import Counter
POPC, POPN = Counter(p_cont), Counter(p_country)
meta = {
    'updated': stamp, 'people': NP,
    'events': EV, 'names': [n for _, n in EVENTS], 'avg': HAS_AVG,
    'conts': conts, 'countries': [[c, cont_idx_of_country[i]] for i, c in enumerate(countries)],
    'rec': rec,
    'pop': {'w': NP, 'c': [POPC.get(i, 0) for i in range(len(conts))], 'n': [POPN.get(i, 0) for i in range(len(countries))]},
    'files': {'w': 'world.json', 'c': {str(i): f'c{i}.json' for i in range(len(conts)) if ('c', i) in files},
              'n': {str(i): f'n{i}.json' for i in range(len(countries)) if ('n', i) in files}},
}
with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
    json.dump(meta, f, ensure_ascii=False, separators=(',', ':'))


# ---------- per-person shards and the search index ----------
def b36(n):
    d = '0123456789abcdefghijklmnopqrstuvwxyz'
    if n == 0: return '0'
    o = ''
    while n: n, m = divmod(n, 36); o = d[m] + o
    return o


def shard_of(w):
    h = 0
    for ch in w:
        h = (h * 31 + ord(ch)) % 1000003
    return h % 256


def fold(t):
    return ''.join(c for c in unicodedata.normalize('NFD', t) if not unicodedata.combining(c)).lower()


def bucket(tok):
    k = tok[:3]
    return k if re.fullmatch(r'[a-z0-9]{2,3}', k) else '_' + str(ord(tok[0]) % 64)


pdir = os.path.join(out_dir, 'p')
shutil.rmtree(pdir, ignore_errors=True)
os.makedirs(pdir)
shards, buckets = defaultdict(list), defaultdict(list)
for i, pid in enumerate(people):
    head = ([int(round(kin[s][i] * 100)) for s in range(3)] + [prank['k'][s][i] for s in range(3)]
            + [sums[t][s][i] for t in range(3) for s in range(3)] + [prank[m][s][i] for m in 'SAC' for s in range(3)])
    evs = []
    for e in range(NE):
        f = [str(e)]
        for k in (0, 1):
            x = rankp[k][i][e]
            if x is None:
                f += ['', '', '', '']
            else:
                a, b, d = unpack(x)
                f += [b36(a), b36(b), b36(d), b36(pr[k][e][pid])]
        if any(f[1:]):
            evs.append(','.join(f))
    nm = pname.get(pid, pid).replace('|', '/').replace('\t', ' ')
    shards[shard_of(pid)].append('|'.join([pid, nm, b36(p_country[i]), ','.join(b36(x) for x in head), ';'.join(evs)]))
    line = pid + '\t' + nm + '\t' + countries[p_country[i]]
    seen = set()
    for tok in re.split(r'[^\w]+', fold(nm)):
        if len(tok) < 2:
            continue
        k = bucket(tok)
        if k not in seen:
            seen.add(k); buckets[k].append(line)
for k, lines in shards.items():
    with open(os.path.join(pdir, f'{k}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
for k, lines in buckets.items():
    lines.sort(key=lambda x: x.split('\t')[1].lower())
    with open(os.path.join(pdir, f's_{k}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
print('done:', NP, 'people;', len(files), 'leaderboards;', len(shards), 'shards;', len(buckets), 'search buckets; export', stamp)

#!/usr/bin/env python3
"""Build Sum of Ranks and Kinch Ranks (world, continent and country) from the WCA results export.

Usage: python3 build_ranks.py <WCA_export_tsv.zip | folder_with_tsvs> <output_dir>

Writes into <output_dir>:
  meta.json            event list, regional records and counts (the site recomputes totals from these)
  lb/*.json            leaderboards, one file per world / continent / country
  p/<n>.txt            per-person data, 256 small shards
  p/s_<key>.txt        name search buckets
"""
import datetime, gzip, json, math, os, re, shutil, struct, sys, tempfile, unicodedata, zipfile
from array import array
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
SCOPE_LIMIT = {'w': 1000, 'c': 1000, 'n': 500}
EV_LIMIT = {'w': 5000, 'c': 5000, 'n': 2500}


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

GENDER = os.environ.get('RANK_GENDER', '')       # '', 'm' or 'f': rank only people of that group (used for the gender filters)
pname, pcty, pgen = {}, {}, {}
for idx, p in rows('persons'):
    w = p[idx['wca_id']]
    if p[idx['sub_id']] == '1' or w not in pname:
        pname[w] = p[idx['name']]
        pcty[w] = p[idx['country_id']]
        g_ = p[idx['gender']] if 'gender' in idx else ''
        pgen[w] = g_ if g_ in ('m', 'f') else 'o'

people = sorted({pid for k in (0, 1) for e in range(NE) for pid in pr[k][e] if pid in pcty and pcty[pid] in NIDX and (not GENDER or pgen.get(pid) == GENDER)})
PI = {pid: i for i, pid in enumerate(people)}
NP = len(people)
p_country = [NIDX[pcty[pid]] for pid in people]
p_cont = [cont_idx_of_country[c] for c in p_country]
print('people with a ranked result:', NP)

# ---------- ranks inside world / continent / country ----------
SH = 1 << 20
def b36(n):
    d = '0123456789abcdefghijklmnopqrstuvwxyz'
    if n == 0: return '0'
    o = ''
    while n: n, m = divmod(n, 36); o = d[m] + o
    return o



evdir = os.path.join(out_dir, 'ev')
cxdir = os.path.join(out_dir, 'cx')
shutil.rmtree(cxdir, ignore_errors=True)
os.makedirs(cxdir)
shutil.rmtree(evdir, ignore_errors=True)
os.makedirs(evdir)
rankp = [[[None] * NE for _ in range(NP)] for _ in range(2)]    # packed (world, continent, country) rank per person/event
rec = {'w': [[0, 0] for _ in range(NE)], 'c': [[[0, 0] for _ in range(NE)] for _ in conts], 'n': [[[0, 0] for _ in range(NE)] for _ in countries]}
cnt = {'w': [[0, 0] for _ in range(NE)], 'c': [[[0, 0] for _ in range(NE)] for _ in conts], 'n': [[[0, 0] for _ in range(NE)] for _ in countries]}


def tie(st, v):
    st[0] += 1
    if v != st[1]:
        st[1] = v
        st[2] = st[0]
    return st[2]


GRID = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000, 1500, 2000, 3000, 5000, 7000, 10000, 15000, 20000, 30000, 50000, 70000, 100000, 150000, 200000]
grid = [[[] for _ in range(NE)] for _ in range(2)]
for k in (0, 1):
    for e in range(NE):
        items = sorted((v, PI[pid]) for pid, v in pr[k][e].items() if pid in PI)
        if not items:
            continue
        ws = [0, None, 0]
        cs = [[0, None, 0] for _ in conts]
        ns = [[0, None, 0] for _ in countries]
        rec['w'][e][k] = items[0][0]
        grid[k][e] = [items[g - 1][0] for g in GRID if g <= len(items)]
        cnt['w'][e][k] = len(items)
        evrows = {}
        cx_ord = array('I', (i for _, i in items)); cx_val = array('I', (v for v, _ in items))
        cx_d = array('I', [cx_val[0]] + [(cx_val[j] - cx_val[j - 1]) & 0xFFFFFFFF for j in range(1, len(cx_val))])
        ob, db = cx_ord.tobytes(), cx_d.tobytes()
        blob = struct.pack('<I', len(items)) + ob[0::4] + ob[1::4] + ob[2::4] + db[0::4] + db[1::4] + db[2::4] + db[3::4]
        if not GENDER:
            with gzip.GzipFile(os.path.join(cxdir, f'{e}_{k}.gz'), 'wb', compresslevel=9, mtime=0) as gz:
                gz.write(blob)
        for v, i in items:
            c, n = p_cont[i], p_country[i]
            for key, lim in ((('w', 0), EV_LIMIT['w']), (('c', c), EV_LIMIT['c']), (('n', n), EV_LIMIT['n'])):
                lst = evrows.setdefault(key, [])
                if len(lst) < lim:
                    lst.append(people[i] + '|' + pname.get(people[i], people[i]).replace('|', '/').replace('\t', ' ') + '|' + b36(n) + '|' + b36(v))
            rankp[k][i][e] = (tie(ws, v) << 40) | (tie(cs[c], v) << 20) | tie(ns[n], v)
            if not rec['c'][c][e][k]: rec['c'][c][e][k] = v
            if not rec['n'][n][e][k]: rec['n'][n][e][k] = v
        for c in range(len(conts)): cnt['c'][c][e][k] = cs[c][0]
        for n in range(len(countries)): cnt['n'][n][e][k] = ns[n][0]
        for (kind, ix), lst in evrows.items():
            fn = f'{e}_{k}_w.txt' if kind == 'w' else f'{e}_{k}_{kind}{ix}.txt'
            with open(os.path.join(evdir, fn), 'w', encoding='utf-8') as f:
                f.write('\n'.join(lst))

MASK = SH - 1


def unpack(x):
    return x >> 40, (x >> 20) & MASK, x & MASK


# ---------- Sum of ranks: three scopes (world, continent, country) x three types (single, average, combined) ----------
EXCL = BLIND | {MBF}


def make_sums(skip):
    out = [[[0] * NP for _ in range(3)] for _ in range(3)]          # [type S,A,C][scope W,C,N][person]
    for i in range(NP):
        c, n = p_cont[i], p_country[i]
        for k in (0, 1):
            rk = rankp[k][i]
            sw = sc = sn = 0
            for e in range(NE):
                if e in skip or (k == 1 and not HAS_AVG[e]):
                    continue
                x = rk[e]
                if x is None:
                    sw += cnt['w'][e][k] + 1; sc += cnt['c'][c][e][k] + 1; sn += cnt['n'][n][e][k] + 1
                else:
                    a, b, d = unpack(x)
                    sw += a; sc += b; sn += d
            out[k][0][i] = sw; out[k][1][i] = sc; out[k][2][i] = sn
        for s_ in range(3):
            out[2][s_][i] = out[0][s_][i] + out[1][s_][i]
    return out


sums = make_sums(set())
sums2 = make_sums(EXCL)


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
kin2 = [[0.0] * NP for _ in range(3)]
for i, pid in enumerate(people):
    c, n = p_cont[i], p_country[i]
    tw = tc = tn = 0.0
    xw = xc = xn = 0.0
    for e in range(NE):
        ps, pa = pr[0][e].get(pid, 0), pr[1][e].get(pid, 0)
        if not ps and not pa:
            continue
        a1 = kscore(e, ps, pa, rec['w'][e][0], rec['w'][e][1])
        a2 = kscore(e, ps, pa, rec['c'][c][e][0], rec['c'][c][e][1])
        a3 = kscore(e, ps, pa, rec['n'][n][e][0], rec['n'][n][e][1])
        tw += a1; tc += a2; tn += a3
        if e not in EXCL:
            xw += a1; xc += a2; xn += a3
    kin[0][i] = round(tw / NE, 2); kin[1][i] = round(tc / NE, 2); kin[2][i] = round(tn / NE, 2)
    NX = NE - len(EXCL)
    kin2[0][i] = round(xw / NX, 2); kin2[1][i] = round(xc / NX, 2); kin2[2][i] = round(xn / NX, 2)

# ---------- rank everyone on every metric inside each scope, and collect leaderboards ----------
METRICS = [('k', kin, True), ('S', sums[0], False), ('A', sums[1], False), ('C', sums[2], False),
           ('k2', kin2, True), ('S2', sums2[0], False), ('A2', sums2[1], False), ('C2', sums2[2], False)]
pos = {}                                  # (metric, scope index, person) -> rank, kept as arrays
prank = {m: [[0] * NP for _ in range(3)] for m, _, _ in METRICS}
lb = {}                                   # scope key -> metric -> rows


def scope_key(s, i):
    return ('w', 0) if s == 0 else (('c', p_cont[i]) if s == 1 else ('n', p_country[i]))


# ---------- yesterday's ranks (for movement): compact daily snapshots kept in a history folder ----------
SNAPM = ['k', 'S', 'A', 'C']                       # metrics with movement tracking (all 17 events)
MV_LIMIT = {'w': 5000, 'c': 5000, 'n': 1000}       # movers lists cover people currently inside these ranks
MV_ROWS = {'w': 100, 'c': 100, 'n': 50}
MV_CUTS = {'w': [100, 500, 1000, 5000], 'c': [100, 500, 1000, 5000], 'n': [50, 100, 250, 500, 1000]}   # "only count people currently ranked within the top N"
NB_LIMIT, NB_CHUNK = 25000, 500                     # "around you" files: top 25,000 per scope, 500 per file
hist_dir = sys.argv[3] if len(sys.argv) > 3 else out_dir.rstrip('/') + '_history'
os.makedirs(hist_dir, exist_ok=True)
today_s = stamp[:10] or datetime.date.today().isoformat()
today_d = datetime.date.fromisoformat(today_s)
snap_files = {}
for fn_ in os.listdir(hist_dir):
    mm = re.fullmatch(r'ranks_(\d{4}-\d{2}-\d{2})\.txt\.gz', fn_)
    if mm:
        snap_files[datetime.date.fromisoformat(mm.group(1))] = os.path.join(hist_dir, fn_)
earlier = sorted(d for d in snap_files if d < today_d)
base_date = None
if earlier:
    ok_ = [d for d in earlier if d <= today_d - datetime.timedelta(days=7)]
    base_date = ok_[-1] if ok_ else earlier[0]      # newest snapshot at least a week old, else the oldest we have
basesnap = {}
if base_date:
    with gzip.open(snap_files[base_date], 'rt', encoding='utf-8') as f_:
        for ln in f_:
            a_ = ln.rstrip('\n').split(',')
            basesnap[a_[0]] = [int(x, 36) for x in a_[1:]]
mvc, nbc = {}, {}
RS_YEARS = 3                                         # "rising stars": first competed this year or in the previous 2
RS_ROWS = {'w': 100, 'c': 100, 'n': 50}
rs_min = today_d.year - RS_YEARS + 1
rsc = {}

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
                row_ = [r, people[i], pname.get(people[i], people[i]), v[i], p_country[i]]
                if m in SNAPM and not GENDER:
                    pv_ = basesnap.get(people[i])
                    row_.append(pv_[SNAPM.index(m) * 3 + s] if pv_ else 0)
                lb.setdefault(key, {}).setdefault(m, []).append(row_)
            if m in SNAPM and not GENDER and r <= MV_LIMIT[key[0]]:
                mvc.setdefault((key, m), []).append((r, i))
            if m in SNAPM and not GENDER and people[i][:4].isdigit() and int(people[i][:4]) >= rs_min:
                rsc.setdefault((key, m), []).append((r, i, v[i]))
            if m in ('k', 'C') and not GENDER and r <= NB_LIMIT:
                nbc.setdefault((m, key), []).append((r, i, v[i]))

# ---------- today's snapshot, movers lists and "around you" files ----------
with gzip.open(os.path.join(hist_dir, f'ranks_{today_s}.txt.gz'), 'wt', encoding='utf-8', compresslevel=9) as f_:
    for i, pid in enumerate(people if not GENDER else []):
        f_.write(pid + ',' + ','.join(b36(prank[m][s][i]) for m in SNAPM for s in range(3)) + '\n')
for d_, p_ in snap_files.items():
    if d_ < today_d - datetime.timedelta(days=10):
        os.remove(p_)

mvdir = os.path.join(out_dir, 'mv'); shutil.rmtree(mvdir, ignore_errors=True); os.makedirs(mvdir)
mv_out = {}
for (key, m), lst in mvc.items():
    s_ = {'w': 0, 'c': 1, 'n': 2}[key[0]]; ix = SNAPM.index(m) * 3 + s_
    ups, downs = [], []
    for r, i in lst:
        pv_ = basesnap.get(people[i])
        if not pv_:
            continue
        d_ = pv_[ix] - r
        row_ = [people[i], pname.get(people[i], people[i]), p_country[i], r, pv_[ix]]
        if d_ > 0: ups.append((-d_, r, row_))
        elif d_ < 0: downs.append((d_, r, row_))
    ups.sort(key=lambda t: (t[0], t[1])); downs.sort(key=lambda t: (t[0], t[1]))
    n_ = MV_ROWS[key[0]]
    mv_out.setdefault(key, {})[m] = {str(cut): {'up': [t[2] for t in ups if t[1] <= cut][:n_], 'down': [t[2] for t in downs if t[1] <= cut][:n_]} for cut in MV_CUTS[key[0]]}
for (kind, ix), data in mv_out.items():
    with open(os.path.join(mvdir, 'world.json' if kind == 'w' else f'{kind}{ix}.json'), 'w', encoding='utf-8') as f_:
        json.dump(data, f_, ensure_ascii=False, separators=(',', ':'))

for kind_, n_all in (('w', 1), ('c', len(conts)), ('n', len(countries))):
    for ix_ in range(n_all):
        fp_ = os.path.join(mvdir, 'world.json' if kind_ == 'w' else f'{kind_}{ix_}.json')
        if not os.path.exists(fp_):
            open(fp_, 'w').write('{}')

nbdir = os.path.join(out_dir, 'nb'); shutil.rmtree(nbdir, ignore_errors=True); os.makedirs(nbdir)
for (m, key), lst in nbc.items():
    fnk = 'w' if key[0] == 'w' else f'{key[0]}{key[1]}'
    by_chunk = {}
    for r, i, val in lst:
        by_chunk.setdefault((r - 1) // NB_CHUNK, []).append('|'.join([str(r), people[i], pname.get(people[i], people[i]).replace('|', '/').replace('\t', ' '), b36(p_country[i]), (f'{val:.2f}' if m == 'k' else str(val))]))
    for c_, lines_ in by_chunk.items():
        with open(os.path.join(nbdir, f'{m}_{fnk}_{c_}.txt'), 'w', encoding='utf-8') as f_:
            f_.write('\n'.join(lines_))
del mvc, nbc

rsdir = os.path.join(out_dir, 'rs'); shutil.rmtree(rsdir, ignore_errors=True); os.makedirs(rsdir)
rs_out = {}
for (key, m), lst in rsc.items():
    lst.sort(key=lambda t: (t[0], t[1]))
    for n_ in range(1, RS_YEARS + 1):
        y0 = today_d.year - n_ + 1
        sel = [t for t in lst if int(people[t[1]][:4]) >= y0][:RS_ROWS[key[0]]]
        rs_out.setdefault(key, {}).setdefault(m, {})[str(n_)] = [[people[i], pname.get(people[i], people[i]), p_country[i], r, val] for r, i, val in sel]
for (kind, ix), data in rs_out.items():
    with open(os.path.join(rsdir, 'world.json' if kind == 'w' else f'{kind}{ix}.json'), 'w', encoding='utf-8') as f_:
        json.dump(data, f_, ensure_ascii=False, separators=(',', ':'))
for kind_, n_all in (('c', len(conts)), ('n', len(countries))):
    for ix_ in range(n_all):
        fp_ = os.path.join(rsdir, f'{kind_}{ix_}.json')
        if not os.path.exists(fp_):
            open(fp_, 'w').write('{}')
if not os.path.exists(os.path.join(rsdir, 'world.json')):
    open(os.path.join(rsdir, 'world.json'), 'w').write('{}')
del rsc, rs_out

# ---------- write leaderboards ----------
shutil.rmtree(os.path.join(out_dir, 'lb'), ignore_errors=True)
os.makedirs(os.path.join(out_dir, 'lb'))
files = {}
for (kind, ix), data in lb.items():
    fn = 'world.json' if kind == 'w' else f'{kind}{ix}.json'
    files[(kind, ix)] = fn
    with open(os.path.join(out_dir, 'lb', fn), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

if GENDER:
    # the gender runs only need the leaderboards and the per-event lists; every file name the site may ask for must exist
    for kind_, n_all in (('c', len(conts)), ('n', len(countries))):
        for ix_ in range(n_all):
            fp_ = os.path.join(out_dir, 'lb', f'{kind_}{ix_}.json')
            if not os.path.exists(fp_): open(fp_, 'w').write('{}')
    if not os.path.exists(os.path.join(out_dir, 'lb', 'world.json')): open(os.path.join(out_dir, 'lb', 'world.json'), 'w').write('{}')
    for d_ in ('mv', 'nb', 'rs', 'cx'):
        shutil.rmtree(os.path.join(out_dir, d_), ignore_errors=True)
    print('gender', GENDER, ':', NP, 'people')
    sys.exit(0)

from collections import Counter
POPC, POPN = Counter(p_cont), Counter(p_country)
meta = {
    'updated': stamp, 'people': NP,
    'events': EV, 'names': [n for _, n in EVENTS], 'avg': HAS_AVG,
    'conts': conts, 'countries': [[c, cont_idx_of_country[i]] for i, c in enumerate(countries)],
    'mv': {'since': base_date.isoformat() if base_date else '', 'limit': MV_LIMIT, 'cuts': MV_CUTS, 'nb_limit': NB_LIMIT, 'nb_chunk': NB_CHUNK},
    'rs': {'year': today_d.year, 'n': RS_YEARS},
    'rec': rec, 'grid': grid, 'cntw': cnt['w'], 'grid_at': GRID, 'excl': sorted(EXCL),
    'pop': {'w': NP, 'c': [POPC.get(i, 0) for i in range(len(conts))], 'n': [POPN.get(i, 0) for i in range(len(countries))]},
    'files': {'w': 'world.json', 'c': {str(i): f'c{i}.json' for i in range(len(conts)) if ('c', i) in files},
              'n': {str(i): f'n{i}.json' for i in range(len(countries)) if ('n', i) in files}},
}
with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
    json.dump(meta, f, ensure_ascii=False, separators=(',', ':'))


# ---------- per-person shards and the search index ----------
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
            + [sums[t][s][i] for t in range(3) for s in range(3)] + [prank[m][s][i] for m in 'SAC' for s in range(3)]
            + [int(round(kin2[s][i] * 100)) for s in range(3)] + [prank['k2'][s][i] for s in range(3)]
            + [sums2[t][s][i] for t in range(3) for s in range(3)] + [prank[m][s][i] for m in ('S2', 'A2', 'C2') for s in range(3)])
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
    pv_ = basesnap.get(pid)
    shards[shard_of(pid)].append('|'.join([pid, nm, b36(p_country[i]), ','.join(b36(x) for x in head), ';'.join(evs), ','.join(b36(x) for x in pv_) if pv_ else '']))
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
with gzip.GzipFile(os.path.join(cxdir, 'people.txt.gz'), 'wb', compresslevel=9, mtime=0) as gz:
    gz.write('\n'.join(pid + '|' + pname.get(pid, pid).replace('|', '/').replace('\t', ' ') + '|' + b36(p_country[i]) for i, pid in enumerate(people)).encode('utf-8'))
print('done:', NP, 'people;', len(files), 'leaderboards;', len(shards), 'shards;', len(buckets), 'search buckets; export', stamp)

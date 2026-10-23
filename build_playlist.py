#!/usr/bin/env python3
"""Builds one checked IPTV playlist from iptv-org lists, grouped in Russian.

Two ways to run:
  build_playlist.py free-channels.m3u
      CI mode (GitHub Actions, outside Russia): keeps streams that answer and ones that only refuse
      this location (HTTP 400/401/403/451), since they may work from Russia. URLs listed in
      ru-blocklist.txt are skipped. A stream already in the playlist is dropped only after failing
      3 runs in a row (counts are kept in state/fails.json); a new stream that fails is not added.
  build_playlist.py free-channels.m3u --strict --write-blocklist ru-blocklist.txt
      Run from Russia (the home network): keeps only streams that answer here and records
      everything else in the blocklist, so the next CI runs skip it as well.

Either way, if the new playlist would have more than 30% fewer channels than the existing one
(iptv-org or the network is down), nothing is written and the script exits with an error.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://iptv-org.github.io/iptv/'
# (group shown in the player, list, optional filter on the source group-title)
SOURCES = [
    ('Русские', 'languages/rus.m3u', None),
    ('Сербия и Босния', 'countries/rs.m3u', None),
    ('Сербия и Босния', 'countries/ba.m3u', None),
    ('Сербия и Босния', 'languages/srp.m3u', None),
    ('Сербия и Босния', 'languages/bos.m3u', None),
    ('English (для изучения)', 'languages/eng.m3u', re.compile(r'Kids|Education|Documentary', re.I)),
    ('Детские', 'categories/kids.m3u', None),
    ('Обучающие', 'categories/education.m3u', None),
    ('Познавательные', 'categories/documentary.m3u', None),
    ('Музыка', 'categories/music.m3u', None),
]
# Redirectors to Pluto TV, Plex and Samsung TV Plus: these services refuse viewers in Russia.
EXCLUDED_HOSTS = {'jmp2.uk'}
UA = 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
# Answers that usually mean "not from your country" rather than "stream is gone".
GEO_CODES = {400, 401, 403, 451}
# Program guide (XMLTV) for the player. Its channel ids are the iptv-org tvg-id values; it is listed
# in iptv-org/epg GUIDES.md. Only some English, kids, documentary and music channels are covered.
EPG_URL = 'https://epg.kanya.io/us/guide.xml.gz'
# CI mode: a stream already in the playlist is dropped after failing this many runs in a row.
MAX_FAILS = 3
# Refuse to replace a playlist that would lose more than this share of its channels.
MAX_SHRINK = 0.30


def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.read().decode('utf-8', 'replace')


def entries(text):
    """(extinf line, option lines, url) for each channel."""
    out, info, opts = [], None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            info, opts = line, []
        elif line.startswith('#EXTVLCOPT') and info:
            opts.append(line)
        elif line and not line.startswith('#') and info:
            out.append((info, opts, line))
            info, opts = None, []
    return out


def host(url):
    return re.sub(r'^[a-z]+://([^/:?#]+).*', r'\1', url, flags=re.I).lower()


def channel_name(info):
    """Name shown in the player: the text after the first comma outside quoted attributes."""
    m = re.match(r'#EXTINF[^,"]*(?:"[^"]*"[^,"]*)*,(.*)$', info)
    return m.group(1).strip() if m else ''


def status(item):
    """'ok', 'geo' (refused, maybe only for this location) or 'dead'."""
    _, opts, url = item
    headers = {'User-Agent': UA}
    for o in opts:
        m = re.match(r'#EXTVLCOPT:http-(user-agent|referrer)=(.*)', o)
        if m:
            headers['User-Agent' if m.group(1) == 'user-agent' else 'Referer'] = m.group(2)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status != 200:
                return 'dead'
            head = r.read(2048)
            ctype = r.headers.get('Content-Type', '').lower()   # media types are case-insensitive
            if b'#EXTM3U' in head[:64]:
                return 'ok' if b'#EXT-X-' in head or b'#EXTINF' in head else 'dead'
            return 'ok' if head and 'text/html' not in ctype else 'dead'
    except urllib.error.HTTPError as e:
        return 'geo' if e.code in GEO_CODES else 'dead'
    except Exception:   # timeouts, refused connections, broken HTTP, bad URLs
        return 'dead'


def check_all(items):
    """{url: status}; a stream that fails is retried once with fewer parallel requests."""
    urls = list(items)
    with ThreadPoolExecutor(64) as pool:
        res = dict(zip(urls, pool.map(lambda u: status(items[u]), urls)))
    again = [u for u, s in res.items() if s != 'ok']
    with ThreadPoolExecutor(12) as pool:
        res.update(zip(again, pool.map(lambda u: status(items[u]), again)))
    return res


def read_blocklist(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding='utf-8') as f:
        return {line.strip() for line in f if line.strip() and not line.startswith('#')}


def read_state(path):
    """{url: consecutive failed runs} saved by the previous CI run (missing URL = 0); {} if none."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        print('warning: ignoring unreadable %s (%s)' % (path, e), file=sys.stderr)
        return {}
    return {u: n for u, n in data.items() if isinstance(n, int)} if isinstance(data, dict) else {}


def read_playlist(path):
    """(number of channels, set of stream URLs) of the playlist written before; (0, set()) if none."""
    if not os.path.exists(path):
        return 0, set()
    with open(path, encoding='utf-8') as f:
        items = entries(f.read())
    return len(items), {url for _, _, url in items}


def update_fails(results, keep, state, known, max_fails=MAX_FAILS):
    """{url: consecutive failed runs} for exactly the streams that may be published.

    results: {url: status} of this run (every URL still in a source); keep: statuses that count as
    answering; state: counts saved by the previous run; known: URLs of the previous playlist.
    A stream that answers is reset to 0. One that fails is counted up and kept while the count stays
    below max_fails if it was seen before (in state or known); a never seen one is left out. URLs
    missing from results (gone from every source or blocklisted) are pruned.
    The state file stores only the non-zero counts of the result.
    """
    new = {}
    for url, st in results.items():
        if st in keep:
            new[url] = 0
        elif url in state or url in known:
            n = state.get(url, 0) + 1
            if n < max_fails:
                new[url] = n
    return new


def select(groups, results, allowed, keep):
    """[(group, info, opts, url)] to publish: groups in SOURCES order, channels sorted by name.

    groups: (group, info, opts, url) in source order; allowed: URLs that may be published.
    A URL is listed once per group. When one name occurs more than once in a group, the first
    stream that answered in this run wins, otherwise the first one kept by the grace period.
    """
    best, seen = {}, set()
    for i, (group, info, opts, url) in enumerate(groups):
        if url not in allowed or (group, url) in seen:
            continue
        seen.add((group, url))
        name = channel_name(info)
        key = (group, ' '.join(name.split()).casefold() or url)
        rank = 0 if results.get(url) in keep else 1
        if key not in best or rank < best[key][0]:
            best[key] = (rank, name.casefold(), i, (group, info, opts, url))
    order = list(dict.fromkeys(g for g, _, _ in SOURCES))
    order += sorted({g for g, _ in best} - set(order))
    picked = sorted(best.values(), key=lambda b: (order.index(b[3][0]), b[1], b[2]))
    return [b[3] for b in picked]


def too_small(old_count, new_count, max_shrink=MAX_SHRINK):
    """True if the new playlist lost more than max_shrink of the old one's channels."""
    return old_count > 0 and new_count < old_count * (1 - max_shrink)


def write_file(path, text):
    """Replaces path in one step, so an interrupted run never leaves half a file."""
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', help='playlist to write')
    ap.add_argument('--strict', action='store_true', help='keep only streams that answer from here')
    ap.add_argument('--blocklist', default='ru-blocklist.txt', help='URLs to skip (default: %(default)s)')
    ap.add_argument('--write-blocklist', metavar='PATH', help='with --strict: save failing URLs here')
    ap.add_argument('--state', metavar='PATH',
                    help='CI mode: failed runs in a row per stream (default: state/fails.json next to OUT)')
    ap.add_argument('--max-shrink', type=float, default=MAX_SHRINK, metavar='SHARE',
                    help='refuse to write a playlist that lost more than this share of channels; '
                         '1 turns the check off (default: %(default)s)')
    args = ap.parse_args(argv)
    state_path = args.state or os.path.join(os.path.dirname(args.out), 'state', 'fails.json')

    blocked = read_blocklist(args.blocklist) if not args.strict else set()
    groups = []   # (group, info, opts, url)
    for group, path, only in SOURCES:
        for info, opts, url in entries(fetch(path)):
            src = re.search(r'group-title="([^"]*)"', info)
            if only and not (src and only.search(src.group(1))):
                continue
            if host(url) in EXCLUDED_HOSTS or url in blocked:
                continue
            groups.append((group, info, opts, url))
    items = {g[3]: (None, g[2], g[3]) for g in groups}
    print('checking %d unique streams…' % len(items), flush=True)
    res = check_all(items)
    keep = {'ok'} if args.strict else {'ok', 'geo'}

    old_count, old_urls = read_playlist(args.out)
    if args.strict:
        state = {}
        allowed = {u: 0 for u, s in res.items() if s in keep}
    else:
        state = read_state(state_path)
        allowed = update_fails(res, keep, state, old_urls)

    kept, counts = [], {}
    for group, info, opts, url in select(groups, res, allowed, keep):
        info = re.sub(r'group-title="[^"]*"', 'group-title="%s"' % group, info)
        if 'group-title=' not in info:
            info = info.replace('#EXTINF:-1', '#EXTINF:-1 group-title="%s"' % group, 1)
        kept.append('\n'.join([info] + opts + [url]))
        counts[group] = counts.get(group, 0) + 1

    if too_small(old_count, len(kept), args.max_shrink):
        msg = ('new playlist has %d channels, the old one %d (more than %d%% fewer); '
               'kept the old playlist. Check iptv-org and the network, or rerun with --max-shrink 1 '
               'if the drop is expected.' % (len(kept), old_count, round(args.max_shrink * 100)))
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print('::error title=Playlist not updated::' + msg.replace('%', '%25'), flush=True)
        sys.exit('error: ' + msg)

    header = '#EXTM3U url-tvg="%s"' % EPG_URL if EPG_URL else '#EXTM3U'
    write_file(args.out, header + '\n' + '\n'.join(kept) + '\n')
    if not args.strict:
        failing_now = {u: n for u, n in allowed.items() if n}
        write_file(state_path, json.dumps(failing_now, indent=0, sort_keys=True, ensure_ascii=False) + '\n')

    if args.strict and args.write_blocklist:
        failed = sorted(u for u, s in res.items() if s != 'ok')
        with open(args.write_blocklist, 'w', encoding='utf-8') as f:
            f.write('# Streams that did not answer from Russia; CI builds skip them.\n')
            f.write('\n'.join(failed) + '\n')
        print('blocklist: %d URLs -> %s' % (len(failed), args.write_blocklist))

    by = {s: sum(1 for v in res.values() if v == s) for s in ('ok', 'geo', 'dead')}
    print('ok %d, geo %d, dead %d of %d unique' % (by['ok'], by['geo'], by['dead'], len(res)))
    if not args.strict:
        failing = {u for u, s in res.items() if s not in keep}
        new = {u for u in failing if u not in state and u not in old_urls}
        grace = failing & set(allowed)
        print('failing but kept for now: %d, dropped after %d failed runs: %d, new failing not added: %d'
              % (len(grace), MAX_FAILS, len(failing - grace - new), len(new)))
    print('channels: %d (before: %d)' % (len(kept), old_count))
    for group in dict.fromkeys(g for g, _, _ in SOURCES):
        if group in counts:
            print('  %s: %d' % (group, counts.pop(group)))


if __name__ == '__main__':
    main()

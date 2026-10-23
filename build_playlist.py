#!/usr/bin/env python3
"""Builds one checked IPTV playlist from iptv-org lists, grouped in Russian.

Two ways to run:
  build_playlist.py free-channels.m3u
      CI mode (GitHub Actions, outside Russia): drops streams that are clearly dead and keeps
      ones that only refuse this location (HTTP 400/401/403/451), since they may work from Russia.
      URLs listed in ru-blocklist.txt are skipped.
  build_playlist.py free-channels.m3u --strict --write-blocklist ru-blocklist.txt
      Run from Russia (the home network): keeps only streams that answer here and records
      everything else in the blocklist, so the next CI runs skip it as well.
"""
import argparse
import os
import re
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
            ctype = r.headers.get('Content-Type', '')
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', help='playlist to write')
    ap.add_argument('--strict', action='store_true', help='keep only streams that answer from here')
    ap.add_argument('--blocklist', default='ru-blocklist.txt', help='URLs to skip (default: %(default)s)')
    ap.add_argument('--write-blocklist', metavar='PATH', help='with --strict: save failing URLs here')
    args = ap.parse_args()

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

    seen, kept, counts = set(), [], {}
    for group, info, opts, url in groups:
        if res[url] not in keep or (group, url) in seen:
            continue
        seen.add((group, url))
        info = re.sub(r'group-title="[^"]*"', 'group-title="%s"' % group, info)
        if 'group-title=' not in info:
            info = info.replace('#EXTINF:-1', '#EXTINF:-1 group-title="%s"' % group, 1)
        kept.append('\n'.join([info] + opts + [url]))
        counts[group] = counts.get(group, 0) + 1
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n' + '\n'.join(kept) + '\n')

    if args.strict and args.write_blocklist:
        failed = sorted(u for u, s in res.items() if s != 'ok')
        with open(args.write_blocklist, 'w', encoding='utf-8') as f:
            f.write('# Streams that did not answer from Russia; CI builds skip them.\n')
            f.write('\n'.join(failed) + '\n')
        print('blocklist: %d URLs -> %s' % (len(failed), args.write_blocklist))

    by = {s: sum(1 for v in res.values() if v == s) for s in ('ok', 'geo', 'dead')}
    print('ok %d, geo %d, dead %d of %d unique' % (by['ok'], by['geo'], by['dead'], len(res)))
    for group, _, _ in SOURCES:
        if group in counts:
            print('  %s: %d' % (group, counts.pop(group)))


if __name__ == '__main__':
    main()

"""Refresh the HSL 2017 Major / 2018 Major scoring race.

Reads the league's calendar feed for the full schedule, fetches the box
score of every game that's gone final since last time (cached in
cache/pages), then rebuilds docs/index.html.

    python3 -I tools/update.py            # from the "HSL Scoring Race" folder
"""
import concurrent.futures as cf
import hashlib
import time
import datetime as dt
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from totext import text  # noqa: E402

ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "cache")
PAGES = os.path.join(CACHE, "pages")
STATE = os.path.join(CACHE, "state.json")
OUT = os.path.join(ROOT, "docs", "index.html")

LEAGUE = "13952"  # HSL 2026-27 season
DIVS = {"32765": "2018 Major", "28374": "2017 Major"}
URL = "https://www.hockeysuperleague.ca/division/0/{}/game/view/{}"
PATHS = ["32765", "28374", "0"]  # any division path serves any game


def fetch(gid):
    """Page HTML. The site throws random 500s, so retry and rotate link forms."""
    for attempt in range(6):
        url = URL.format(PATHS[attempt % len(PATHS)], gid)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                time.sleep(0.15)  # be gentle with the league site
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if attempt == 5:
                raise
            time.sleep(1 + attempt)


def parse_date(s):
    m = re.match(r"\w+, (\w+ \d+, \d{4})", s)
    return dt.datetime.strptime(m.group(1), "%B %d, %Y").date() if m else None


def header(lines):
    """(date, rink, home, away, score, status) or None if the page has no game."""
    try:
        i = lines.index("Team Breakdown")
    except ValueError:
        return None
    h = lines[max(0, i - 10):i]
    at = next((k for k, x in enumerate(h) if " @ " in x), None)
    if at is None:
        return None
    rest = h[at:]
    date, rink, home = rest[0], rest[1], rest[2]
    if "Final" in " ".join(rest):  # date rink home Home score Final away Away
        return date, rink, home, rest[6], rest[4], rest[5]
    return date, rink, home, rest[4], "", "Preview"  # date rink home Home away Away


def box(lines):
    players = []
    j = lines.index("Game Timeline")
    while j < len(lines) - 3:
        if lines[j + 1] == "#" and lines[j + 2] == "Name":
            team, cols, m = lines[j], [], j + 3
            while lines[m] in ("G", "A", "PTS", "PIM", "MP", "GA", "Shots", "Saves", "Sav%"):
                cols.append(lines[m])
                m += 1
            n = len(cols) + 2
            while m + n <= len(lines) and re.fullmatch(r"\d+", lines[m]) and lines[m + 1] != "#":
                row = lines[m:m + n]
                if cols[:1] == ["G"]:
                    players.append(dict(team=team, num=row[0], name=row[1].strip(),
                                        G=int(row[2]), A=int(row[3]), PTS=int(row[4]), PIM=int(row[5])))
                m += n
            j = m
        else:
            j += 1
    return players


def examine(gid, html):
    lines = text_from(html)
    h = header(lines)
    if not h:
        return None
    date, rink, home, away, score, status = h
    divs = re.findall(rf"/team/{LEAGUE}/0/(\d+)/\d+/player/", html)
    div = max(set(divs), key=divs.count) if divs else None
    g = dict(gid=gid, date=date, day=str(parse_date(date)), rink=rink, home=home, away=away,
             score=score, status=status, div=div)
    if status.startswith("Final") and div:
        g["players"] = box(lines)
    return g


def text_from(html):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    try:
        return text(f.name)
    finally:
        os.unlink(f.name)


ICS = "https://www.hockeysuperleague.ca/calendar/master-schedule/481.ics?SID={}&DID={}&TZ=America%2FEdmonton"


def schedule(did):
    """Every game in a division from the league's calendar feed: {gid: (day, final?)}."""
    req = urllib.request.Request(ICS.format(LEAGUE, did), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ics = r.read().decode("utf-8", "ignore").replace("\r\n ", "").replace("\n ", "")
    out = {}
    for ev in ics.split("BEGIN:VEVENT")[1:]:
        uid = re.search(r"UID:leaguegame-(\d+)", ev)
        start = re.search(r"DTSTART[^:]*:(\d{8})", ev)
        if not (uid and start):
            continue
        day = dt.datetime.strptime(start.group(1), "%Y%m%d").date().isoformat()
        out[uid.group(1)] = (day, "Final" in ev)
    return out


def load(gid):
    p = os.path.join(PAGES, f"{gid}.html")
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="ignore").read(), True
    return fetch(gid), False


def main():
    os.makedirs(PAGES, exist_ok=True)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"games": {}}
    games = state["games"]

    # the calendar feed is the source of truth for which games exist and are final
    sched = {}
    for did, name in DIVS.items():
        try:
            s = schedule(did)
        except Exception as e:
            print(f"could not read the {name} calendar feed ({e}); nothing changed")
            return
        for gid, (day, final) in s.items():
            sched[gid] = (did, day, final)
    todo = [gid for gid, (did, day, final) in sched.items()
            if final and not (games.get(gid) and games[gid]["status"].startswith("Final") and games[gid].get("players"))]
    print(f"{len(sched)} scheduled games, {sum(1 for v in sched.values() if v[2])} final, {len(todo)} to fetch", flush=True)

    errors = []

    def work(gid):
        try:
            html, cached = load(gid)
            g = examine(gid, html)
            if not g or not g["status"].startswith("Final"):
                # calendar says final but the page isn't yet; try again next run
                return gid, None, None
            g["div"] = sched[gid][0]  # trust the calendar's division, not the roster links
            if not cached:
                open(os.path.join(PAGES, f"{gid}.html"), "w", encoding="utf-8").write(html)
            return gid, g, None
        except Exception as e:
            return gid, None, e

    with cf.ThreadPoolExecutor(3) as ex:
        for gid, g, err in ex.map(work, todo):
            if err is not None:
                errors.append(gid)
            elif g:
                games[gid] = g
    # drop anything the calendar no longer lists (cancelled / other league)
    games = {k: v for k, v in games.items() if k in sched and v}

    state["games"] = games
    build(state)
    json.dump(state, open(STATE, "w"), separators=(",", ":"))
    finals = [g for g in games.values() if g.get("div") in DIVS and g["status"].startswith("Final")]
    for d, name in DIVS.items():
        n = [g for g in finals if g["div"] == d]
        bad = [g["gid"] for g in n if sum(p["G"] for p in g["players"]) != sum(map(int, re.findall(r"\d+", g["score"])))]
        print(f"{name}: {len(n)} final games, last {max((g['day'] for g in n), default='-')}"
              + (f", goal totals off vs score in {bad} (shootouts count 1)" if bad else ""))
    print(f"pages that failed (will retry next run): {errors}" if errors else "all pages fetched")
    print(OUT)


def build(state):
    data = {}
    for d, name in DIVS.items():
        gs = [g for g in state["games"].values() if g and g.get("div") == d and g["status"].startswith("Final")]
        agg = {}
        for g in gs:
            for p in g["players"]:
                k = (p["team"], p["name"].lower())
                nm = p["name"].title() if p["name"].isupper() else p["name"]
                a = agg.setdefault(k, [nm, p["team"], p["num"], 0, 0, 0, 0, 0])
                a[3] += 1
                a[4] += p["G"]; a[5] += p["A"]; a[6] += p["PTS"]; a[7] += p["PIM"]
        rows = sorted(agg.values(), key=lambda r: (-r[6], -r[4], r[3], r[0]))
        last = max((g["day"] for g in gs), default=None)
        thru = dt.date.fromisoformat(last).strftime("%b %-d") if last else "-"
        data[name] = dict(games=len(gs), thru=thru, rows=rows)
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sig = hashlib.sha1(blob.encode()).hexdigest()
    if sig != state.get("sig"):  # stamp the time only when the numbers change
        state["sig"] = sig
        state["updated"] = dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p")
    tpl = open(os.path.join(HERE, "page.tpl.html"), encoding="utf-8").read()
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    html = html.replace("__UPDATED__", state["updated"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(html)


if __name__ == "__main__":
    main()

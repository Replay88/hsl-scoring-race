"""Refresh the HSL 2017 Major / 2018 Major scoring race.

Reads the league's calendar feed for the full schedule, fetches the box
score of every game that's gone final since last time (cached in
cache/pages), then rebuilds docs/index.html (and docs/og.png when the
numbers change).

    python3 -I tools/update.py            # from the site folder
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from totext import text  # noqa: E402

ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "cache")
PAGES = os.path.join(CACHE, "pages")
STATE = os.path.join(CACHE, "state.json")
OUT = os.path.join(ROOT, "docs", "index.html")

# Seasons on the league site (its "SID"), newest first. The first one is the live season;
# older ones are read once and then frozen.
SEASONS = [("13952", "2026-27"), ("11823", "2025-26")]
CURRENT = SEASONS[0][0]
URL = "https://www.hockeysuperleague.ca/division/0/{}/game/view/{}"
PATHS = ["32765", "28374", "0"]  # any division path serves any game
ICS = "https://www.hockeysuperleague.ca/calendar/master-schedule/481.ics?SID={}&TZ=America%2FEdmonton"
# Game types. The league's feed doesn't say whether a game is regular season, playoffs,
# exhibition or a tournament, so date ranges are set here by hand when the league
# announces them. Anything not listed counts as regular season. Only "Regular" games
# feed the leaders and standings; the rest show on the Scores tab with a label.
GAME_TYPES = {  # season id -> list of (label, first day, last day, division name or None for all)
    "13952": [],
    "11823": [],
}


def div_name(label):
    """The league labels the Minor tier with the bare year ("2018"); we call it "2018 Minor"."""
    return label + " Minor" if re.fullmatch(r"\d{4}", label) else label


def div_order(name):
    """Youngest birth year first; AAA > Major > Minor within a year."""
    y = re.match(r"\d{4}", name)
    tier = {"AAA": 0, "Major": 1, "Minor": 2}.get(name.split(" ")[-1], 1)
    return (-int(y.group()) if y else 0, tier, name)


def game_type(sid, g):
    for label, lo, hi, div in GAME_TYPES.get(sid, []):
        if lo <= g["day"] <= hi and (div is None or div == g["div"]):
            return label
    return "Regular"
PARSER = 3  # bump when the box-score parser changes so cached games get re-read
# The site's public address. Switch to "hslstats.com" once the domain points at GitHub Pages.
SITE = "hslstats.com"
BASE = "https://" + SITE + "/"


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


def ints_after(lines, i):
    out = []
    while i < len(lines) and re.fullmatch(r"-?\d+", lines[i]):
        out.append(int(lines[i]))
        i += 1
    return out, i


def tables(lines, home, away):
    """Period scoring and shots: {'score': {team: [p1, p2, p3, ..., total]}, 'shots': {...}}."""
    out = {}
    for label in ("Scoring", "Shots"):
        try:
            i = lines.index(label)
        except ValueError:
            continue
        d, j = {}, i + 1
        while j < len(lines) and len(d) < 2:
            if lines[j] in (home, away):
                team = lines[j]
                nums, j = ints_after(lines, j + 1)
                d[team] = nums
            else:
                j += 1
        out[label.lower()] = d
    return out


PLAYER = re.compile(r"#(\d+)\s+(.+?)(?:\s+\((\d+)\))?$")


def timeline(lines, home, away):
    """Goal and penalty events in order."""
    try:
        start = lines.index("Game Timeline")
    except ValueError:
        return []
    end = next((k for k in range(start, len(lines) - 2) if lines[k + 1] == "#" and lines[k + 2] == "Name"), len(lines))
    ev, per, i = [], "1", start + 1
    while i < end:
        x = lines[i]
        if x.startswith("Period ") or x in ("Overtime", "Shootout"):
            per = x.replace("Period ", "")
            i += 1
            continue
        if x in ("Home", "Away") and i + 2 < end:
            side, team = x, lines[i + 1]
            m = PLAYER.match(lines[i + 2])
            j = i + 3
            if m and j < end and lines[j] == "Goal":
                j += 1
                tags = []
                while j < end and lines[j] in ("PPG", "GWG", "SHG", "ENG", "PSG", "OTG"):
                    tags.append(lines[j])
                    j += 1
                assists = []
                while j < end and lines[j] != "Time":
                    assists += [PLAYER.match(a.strip()).group(2) for a in lines[j].split(",") if PLAYER.match(a.strip())]
                    j += 1
                t = lines[j + 1] if j + 1 < end else ""
                ev.append(dict(t="G", per=per, side=side[0], team=team, time=t, scorer=m.group(2), num=m.group(1),
                               assists=assists, tags=tags))
                while j < end and lines[j] != "Score":
                    j += 1
                i = j + 3
                continue
            if m and j < end and re.fullmatch(r"\d+:\d\d", lines[j]):  # player, length, Time, t, Penalty, kind
                mins = int(lines[j].split(":")[0])
                t = lines[j + 2] if j + 2 < end else ""
                kind = lines[j + 4] if j + 4 < end else ""
                ev.append(dict(t="P", per=per, side=side[0], team=team, time=t, player=m.group(2), num=m.group(1),
                               min=mins, kind=kind))
                i = j + 5
                continue
        i += 1
    return ev


def box(lines):
    players, goalies = [], []
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
                elif cols[:1] == ["MP"]:
                    mm, ss = (row[2].split(":") + ["0"])[:2]
                    goalies.append(dict(team=team, num=row[0], name=row[1].strip(), MIN=int(mm) + int(ss) / 60,
                                        GA=int(row[3]), SA=int(row[4]), SV=int(row[5])))
                m += n
            j = m
        else:
            j += 1
    return players, goalies


def examine(gid, html):
    lines = text_from(html)
    h = header(lines)
    if not h:
        return None
    date, rink, home, away, score, status = h
    g = dict(gid=gid, date=date, day=str(parse_date(date)), rink=rink, home=home, away=away,
             score=score, status=status, parser=PARSER)
    if status.startswith("Final"):
        g["players"], g["goalies"] = box(lines)
        g.update(tables(lines, home, away))
        g["events"] = timeline(lines, home, away)
        for p in g["players"]:  # PP / GW goals per player, from the timeline
            mine = [e for e in g["events"] if e["t"] == "G" and e["team"] == p["team"] and e["num"] == p["num"]]
            p["PPG"] = sum("PPG" in e["tags"] for e in mine)
            p["GWG"] = sum("GWG" in e["tags"] for e in mine)
    return g


def text_from(html):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    try:
        return text(f.name)
    finally:
        os.unlink(f.name)


def schedule(sid):
    """Every game of a season, all divisions, from the league's calendar feed."""
    req = urllib.request.Request(ICS.format(sid), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ics = r.read().decode("utf-8", "ignore").replace("\r\n ", "").replace("\n ", "")
    out = {}
    for ev in ics.split("BEGIN:VEVENT")[1:]:
        uid = re.search(r"UID:leaguegame-(\d+)", ev)
        start = re.search(r"DTSTART[^:]*:(\d{8})T(\d{4})", ev)
        summ = re.search(r"SUMMARY:([^:]*): (.+?) vs (.+)", ev)
        if not (uid and start and summ):
            continue
        day = dt.datetime.strptime(start.group(1), "%Y%m%d").date().isoformat()
        hh, mm = int(start.group(2)[:2]), start.group(2)[2:]
        loc = re.search(r"LOCATION:(.+)", ev)
        num = re.search(r"Game #(\d+)", ev)
        out[uid.group(1)] = dict(div=div_name(summ.group(1).strip()), day=day,
                                 time=f"{(hh - 1) % 12 + 1}:{mm} {'AM' if hh < 12 else 'PM'}",
                                 sort=hh * 60 + int(mm), home=summ.group(2).strip(), away=summ.group(3).strip(),
                                 loc=loc.group(1).split("\\,")[0].strip() if loc else "",
                                 num=num.group(1) if num else "", final="Final" in ev)
    return out


LOGO = re.compile(r'src="(https://cloud3\.rampinteractive\.com/[^"]*/team-logos/[^"]*?_([^"/]+?)\.(png|jpe?g|gif|webp))"', re.I)


def find_logos(html, logos):
    """Remember a logo URL for every team name seen in a page's image tags."""
    for url, name, _ in LOGO.findall(html):
        name = name.replace("&amp;", "&").replace("%20", " ").strip()
        logos.setdefault(name, url)


def knock_out_white(im):
    """Make a white/near-white background transparent, flood-filling in from the
    edges so white inside the crest is kept. No-op if the corners aren't white."""
    w, h = im.size
    px = im.load()

    def white(p):
        return p[3] > 0 and p[0] > 232 and p[1] > 232 and p[2] > 232
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    if sum(white(c) for c in corners) < 3:
        return im
    seen = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    while stack:
        x, y = stack.pop()
        i = y * w + x
        if seen[i] or not white(px[x, y]):
            continue
        seen[i] = 1
        px[x, y] = (255, 255, 255, 0)
        if x > 0: stack.append((x - 1, y))
        if x < w - 1: stack.append((x + 1, y))
        if y > 0: stack.append((x, y - 1))
        if y < h - 1: stack.append((x, y + 1))
    # soften the fringe: pixels next to transparent ones that are light get partial alpha
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a and r > 200 and g > 200 and b > 200 and any(
                    px[nx, ny][3] == 0 for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                    if 0 <= nx < w and 0 <= ny < h):
                px[x, y] = (r, g, b, max(0, 255 - (min(r, g, b) - 200) * 4))
    return im


def fetch_logos(logos, teams):
    """Download any team's logo we don't have yet into docs/logos/, shrunk to 128px PNG."""
    folder = os.path.join(os.path.dirname(OUT), "logos")
    os.makedirs(folder, exist_ok=True)
    have = {}
    for team in sorted(teams):
        url = logos.get(team) or next((u for n, u in logos.items() if team.startswith(n)), None)
        if not url:
            continue
        path = os.path.join(folder, slug(team) + ".png")
        if not os.path.exists(path):
            try:
                req = urllib.request.Request(url.replace(" ", "%20"), headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                from io import BytesIO
                from PIL import Image
                im = Image.open(BytesIO(raw)).convert("RGBA")
                im.thumbnail((256, 256))
                im = knock_out_white(im)
                im.thumbnail((128, 128))
                im.save(path, optimize=True)
            except Exception as e:
                print(f"logo for {team} skipped: {e}")
                continue
        have[team] = "logos/" + slug(team) + ".png"
    return have


def load(gid):
    p = os.path.join(PAGES, f"{gid}.html")
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="ignore").read(), True
    return fetch(gid), False


def main():
    os.makedirs(PAGES, exist_ok=True)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if "games" in state and "seasons" not in state:  # older state layout
        state = {"seasons": {CURRENT: {"games": state["games"], "sched": state.get("sched", {}),
                                       "ranks": state.get("ranks", {}), "sig": state.get("sig"),
                                       "updated": state.get("updated")}},
                 "logos": state.get("logos", {}), "logo_files": state.get("logo_files", {}),
                 "updated": state.get("updated")}
    state.setdefault("seasons", {})
    logos = state.setdefault("logos", {})
    if not logos:  # first time: mine every cached page
        for f in os.listdir(PAGES):
            find_logos(open(os.path.join(PAGES, f), encoding="utf-8", errors="ignore").read(), logos)
    today = dt.date.today().isoformat()
    failures = 0

    for sid, label in SEASONS:
        S = state["seasons"].setdefault(sid, {"games": {}, "sched": {}, "ranks": {}})
        if S.get("frozen"):
            continue
        try:
            sched = schedule(sid)
        except Exception as e:
            print(f"could not read the {label} calendar feed ({e}); that season is unchanged")
            failures += 1
            continue
        games = S["games"]
        # read every newly-final game, and re-read the last two weeks' games so scorekeeper
        # corrections on the league site (a goal moved to the right kid) flow through
        recent = (dt.date.fromisoformat(today) - dt.timedelta(days=14)).isoformat()
        todo = [gid for gid, s in sched.items()
                if s["final"] and (not (games.get(gid) and games[gid]["status"].startswith("Final")
                                        and games[gid].get("parser") == PARSER)
                                   or (sid == CURRENT and s["day"] >= recent))]
        if sid != CURRENT:
            todo = todo[:150]  # old seasons fill in a chunk per run; no rush
        print(f"{label}: {len(sched)} scheduled games, {sum(1 for s in sched.values() if s['final'])} final, "
              f"{len(todo)} to read", flush=True)
        errors = []

        def work(gid):
            try:
                if sid == CURRENT and sched[gid]["day"] >= recent:
                    html, cached = fetch(gid), False  # always fresh: the sheet may have been corrected
                else:
                    html, cached = load(gid)
                g = examine(gid, html)
                find_logos(html, logos)
                if not g or not g["status"].startswith("Final"):
                    return gid, None, None  # calendar says final but the page isn't yet; next run
                g["div"] = sched[gid]["div"]  # trust the calendar's division, not the roster links
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
        if todo and len(errors) > 0.3 * len(todo):
            print(f"{label}: {len(errors)} of {len(todo)} pages failed (e.g. {errors[:3]}); the site is probably down")
            failures += 1
        S["games"] = {k: v for k, v in games.items() if k in sched and v}  # drop cancelled / stray games
        for gid, g in S["games"].items():
            g["div"] = sched[gid]["div"]  # the calendar's division name, always
        S["sched"] = sched
        if sid != CURRENT and not errors and not todo:
            last = max((s["day"] for s in sched.values()), default="")
            if last and last < (dt.date.fromisoformat(today) - dt.timedelta(days=60)).isoformat():
                S["frozen"] = True  # past season fully read; never fetch it again
        if errors:
            print(f"{label}: pages that failed (will retry next run): {errors}")

    cur = state["seasons"][CURRENT]
    teams = {s["home"] for s in cur["sched"].values()} | {s["away"] for s in cur["sched"].values()}
    state["logo_files"] = fetch_logos(logos, teams)
    build(state)
    json.dump(state, open(STATE, "w"), separators=(",", ":"))
    for sid, label in SEASONS:
        S = state["seasons"][sid]
        for name in sorted({g["div"] for g in S["games"].values() if g.get("div")}, key=div_order):
            n = [g for g in S["games"].values() if g["div"] == name and g["status"].startswith("Final")]
            bad = [g["gid"] for g in n if sum(p["G"] for p in g["players"]) != sum(map(int, re.findall(r"\d+", g["score"])))]
            print(f"{label} {name}: {len(n)} final games, last {max((g['day'] for g in n), default='-')}"
                  + (f", box score off vs score in {len(bad)}" if bad else ""))
    print(OUT)
    if failures:
        sys.exit(f"{failures} problem(s) this run; see above")


def key(p):
    return p["team"] + "|" + p["name"].lower()


def nice(name):
    return name.title() if name.isupper() else name


def result(g, team):
    """'W', 'L' or 'T' for team in game g."""
    h, a = map(int, re.findall(r"\d+", g["score"]))
    gf, ga = (h, a) if team == g["home"] else (a, h)
    return "W" if gf > ga else "L" if gf < ga else "T"


def mmdd(day):
    return dt.date.fromisoformat(day).strftime("%b %-d")


def slug(s):
    return re.sub(r"-+$|^-+", "", re.sub(r"[^a-z0-9]+", "-", s.lower()))


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")


def write_calendars(did, name, sched, G):
    """One subscribable .ics per team (and one per division) in docs/cal/<div>/."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Edmonton")
    folder = os.path.join(os.path.dirname(OUT), "cal", slug(name))
    os.makedirs(folder, exist_ok=True)
    games = [(gid, s) for gid, s in sched.items() if s["div"] == did]
    teams = sorted({s["home"] for _, s in games} | {s["away"] for _, s in games})

    def event(gid, s):
        hh, mm = divmod(s["sort"], 60)
        y, m, d_ = map(int, s["day"].split("-"))
        start = dt.datetime(y, m, d_, hh, mm, tzinfo=tz).astimezone(dt.timezone.utc)
        end = start + dt.timedelta(minutes=90)
        g = G.get(gid)
        title = f"{s['away']} at {s['home']}"
        if g:
            title = f"{s['away']} {g['vs']} – {s['home']} {g['hs']} (Final)"
        return "\r\n".join([
            "BEGIN:VEVENT", f"UID:hsl-{gid}@hslstats.com",
            "DTSTAMP:" + start.strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"), "DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + ics_escape(f"{name}: {title}"),
            "LOCATION:" + ics_escape(s["loc"]),
            "DESCRIPTION:" + ics_escape(f"HSL {name} game #{s['num']}. Stats: {BASE}#{slug(name)}/g/{gid}"),
            "END:VEVENT"])

    def write(path, label, evs):
        body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HSL Statistics//EN", "CALSCALE:GREGORIAN",
                             "X-WR-CALNAME:" + ics_escape(label), "X-WR-TIMEZONE:America/Edmonton"] + evs + ["END:VCALENDAR"]) + "\r\n"
        open(path, "w", encoding="utf-8", newline="").write(body)

    ordered = sorted(games, key=lambda x: (x[1]["day"], x[1]["sort"]))
    write(os.path.join(folder, "all.ics"), f"HSL {name}", [event(gid, s) for gid, s in ordered])
    for team in teams:
        write(os.path.join(folder, slug(team) + ".ics"), f"{team} ({name})",
              [event(gid, s) for gid, s in ordered if team in (s["home"], s["away"])])


def weekend_window(sched, today):
    """The next game day on or after today, through that weekend's Sunday."""
    days = sorted({s["day"] for s in sched.values() if not s["final"] and s["day"] >= today})
    if not days:
        return None, None
    d0 = dt.date.fromisoformat(days[0])
    ahead = {4: 2, 5: 1, 6: 0}.get(d0.weekday(), 2)  # Fri->Sun, Sat->Sun, Sun->Sun, weekday -> +2
    return days[0], (d0 + dt.timedelta(days=ahead)).isoformat()


def build_season(state, sid, today):
    S = state["seasons"][sid]
    games, sched = S["games"], S["sched"]
    data = {}
    names = sorted({s["div"] for s in sched.values()}, key=div_order)
    for name in names:
        d = name
        allg = sorted((g for g in games.values() if g and g.get("div") == d and g["status"].startswith("Final")),
                      key=lambda g: (g["day"], sched.get(g["gid"], {}).get("sort", 0), g["gid"]))
        for g in allg:
            g["type"] = game_type(sid, g)
        gs = [g for g in allg if g["type"] == "Regular"]
        days = sorted({g["day"] for g in gs})
        last = days[-1] if days else None

        # --- games (results + box scores) ---
        G = {}
        for g in allg:
            h, a = map(int, re.findall(r"\d+", g["score"]))
            G[g["gid"]] = dict(day=g["day"], time=sched.get(g["gid"], {}).get("time", ""), rink=g["rink"],
                               home=g["home"], away=g["away"], hs=h, vs=a, status=g["status"], type=g["type"],
                               periods=g.get("scoring", {}), shots=g.get("shots", {}), events=g.get("events", []),
                               goalies=[dict(team=q["team"], name=nice(q["name"]), num=q["num"], MIN=round(q["MIN"]),
                                             GA=q["GA"], SA=q["SA"], SV=q["SV"]) for q in g.get("goalies", [])],
                               url=URL.format(d, g["gid"]))

        # --- skaters: season totals + game logs ---
        agg, perday, logs = {}, {}, {}
        for g in gs:
            for p in g["players"]:
                k = key(p)
                a = agg.setdefault(k, [nice(p["name"]), p["team"], p["num"], 0, 0, 0, 0, 0, 0, 0])
                a[3] += 1; a[4] += p["G"]; a[5] += p["A"]; a[6] += p["PTS"]; a[7] += p["PIM"]
                a[8] += p.get("PPG", 0); a[9] += p.get("GWG", 0)
                perday.setdefault(k, {}).setdefault(g["day"], 0)
                perday[k][g["day"]] += p["PTS"]
                logs.setdefault(k, []).append([g["gid"], p["G"], p["A"], p["PIM"]])
        order = sorted(agg, key=lambda k: (-agg[k][6], -agg[k][4], agg[k][3], agg[k][0]))
        pid = {k: i for i, k in enumerate(order)}
        ranks, prev, r = {}, None, 0
        for i, k in enumerate(order):
            if agg[k][6] != prev:
                r, prev = i + 1, agg[k][6]
            ranks[k] = r
        old = S.get("ranks", {}).get(name, {})
        rows = []
        for k in order:
            # name team num GP G A PTS PIM PPG GWG rank move log
            rows.append(agg[k] + [ranks[k], (old[k] - ranks[k]) if k in old else None, logs[k]])

        # --- hot this weekend: the last game day and the two days before it ---
        hot, span = [], ""
        if last:
            lo = (dt.date.fromisoformat(last) - dt.timedelta(days=2)).isoformat()
            tally = {}
            for g in gs:
                if g["day"] >= lo:
                    for p in g["players"]:
                        t_ = tally.setdefault(key(p), [nice(p["name"]), p["team"], 0, 0, 0, 0, pid[key(p)]])
                        t_[2] += 1; t_[3] += p["G"]; t_[4] += p["A"]; t_[5] += p["PTS"]
            hot = [h for h in sorted(tally.values(), key=lambda x: (-x[5], -x[3], x[0]))[:6] if h[5] > 0]
            span = mmdd(lo) + ("–" + str(int(last[-2:])) if lo[:7] == last[:7] else " – " + mmdd(last))

        # --- race: cumulative points by game day for the top 8 ---
        race = []
        for k in order[:8]:
            run, cum = [], 0
            for day in days:
                cum += perday[k].get(day, 0)
                run.append(cum)
            race.append([agg[k][0], agg[k][1], run, pid[k]])

        # --- standings (league rules: W 2, T 1) ---
        st = {}
        for g in gs:
            h, a = map(int, re.findall(r"\d+", g["score"]))
            if "SO" in g["status"]:  # league books a shootout game's goals at the tied score
                h = a = min(h, a)
            for team, gf, ga in ((g["home"], h, a), (g["away"], a, h)):
                s = st.setdefault(team, dict(team=team, GP=0, W=0, L=0, T=0, PTS=0, GF=0, GA=0, form=[], SF=0, SA=0,
                                             SGP=0, PPG=0, PIM=0, games=[]))
                s["GP"] += 1; s["GF"] += gf; s["GA"] += ga
                res = result(g, team)
                s[res] += 1; s["PTS"] += {"W": 2, "T": 1, "L": 0}[res]; s["form"].append(res)
                s["games"].append(g["gid"])
                opp = g["away"] if team == g["home"] else g["home"]
                sh = g.get("shots", {})
                if sh.get(team) and sh.get(opp):
                    s["SF"] += sh[team][-1]; s["SA"] += sh[opp][-1]; s["SGP"] += 1
                s["PPG"] += sum(p.get("PPG", 0) for p in g["players"] if p["team"] == team)
                s["PIM"] += sum(e["min"] for e in g.get("events", []) if e["t"] == "P" and e["team"] == team)
        standings = sorted(st.values(), key=lambda s: (-s["PTS"], -(s["PTS"] / (2 * s["GP"])), -(s["GF"] - s["GA"]), -s["GF"], s["team"]))
        for s in standings:
            s["DIFF"] = s["GF"] - s["GA"]
            s["PCT"] = round(s["PTS"] / (2 * s["GP"]), 3) if s["GP"] else 0
            f = s.pop("form")
            s["last5"] = f[-5:]
            streak, kind = 0, f[-1] if f else ""
            for x in reversed(f):
                if x != kind:
                    break
                streak += 1
            s["streak"] = f"{kind}{streak}" if f else ""
            s["roster"] = [pid[k] for k in order if agg[k][1] == s["team"]]

        # --- goalies: shots/minutes aren't recorded in every game, so SV% and GAA
        #     only use "complete" lines (minutes + shots recorded, GA adds up) ---
        gl = {}
        for g in gs:
            h, a = map(int, re.findall(r"\d+", g["score"]))
            allowed = {g["home"]: a, g["away"]: h}
            by_team = {}
            for q in g.get("goalies", []):
                tq = by_team.setdefault(q["team"], {})
                if key(q) not in tq or q["MIN"] + q["SA"] > tq[key(q)]["MIN"] + tq[key(q)]["SA"]:
                    tq[key(q)] = q  # the site sometimes lists a goalie twice
            for team, tq in by_team.items():
                qs = list(tq.values())
                res = result(g, team)
                starter = max(qs, key=lambda q: (q["MIN"], q["SA"]))
                complete = all(q["MIN"] > 0 and q["SA"] > 0 for q in qs) and sum(q["GA"] for q in qs) == allowed.get(team, -1)
                for q in qs:
                    a_ = gl.setdefault(key(q), dict(name=nice(q["name"]), team=team, num=q["num"], GP=0, GS=0, MIN=0.0,
                                                    GA=0, SA=0, SV=0, W=0, L=0, T=0, SO=0, log=[]))
                    a_["GP"] += 1
                    if q is starter:
                        a_[res] += 1
                    a_["log"].append([g["gid"], round(q["MIN"]), q["GA"], q["SA"], q["SV"], int(complete)])
                    if complete:
                        a_["GS"] += 1; a_["MIN"] += q["MIN"]; a_["GA"] += q["GA"]; a_["SA"] += q["SA"]; a_["SV"] += q["SV"]
                        if q["GA"] == 0 and len(qs) == 1:
                            a_["SO"] += 1
        goalies = []
        for a_ in gl.values():
            a_["SVP"] = round(a_["SV"] / a_["SA"], 3) if a_["SA"] else 0
            a_["GAA"] = round(a_["GA"] * 55 / a_["MIN"], 2) if a_["MIN"] else 0
            a_["MIN"] = round(a_["MIN"])
            goalies.append(a_)
        goalies.sort(key=lambda a_: (-(a_["GS"] >= 2), -a_["SVP"], a_["GAA"], -a_["SV"], -a_["GP"]))

        # --- records: hat tricks, big games, game of the week ---
        hats = sorted(([g["day"], pid[key(p)], g["gid"], p["G"], p["A"]] for g in gs for p in g["players"] if p["G"] >= 3),
                      key=lambda x: (-x[3], -x[4], x[0]))
        bigs = sorted(([g["day"], pid[key(p)], g["gid"], p["G"], p["A"]] for g in gs for p in g["players"] if p["PTS"] >= 4),
                      key=lambda x: (-(x[3] + x[4]), -x[3], x[0]))[:15]
        gotw = {}
        if last:
            lo = (dt.date.fromisoformat(last) - dt.timedelta(days=2)).isoformat()
            wk = [g for g in gs if g["day"] >= lo]

            def sc(g):
                return tuple(map(int, re.findall(r"\d+", g["score"])))
            if wk:
                gotw["closest"] = min(wk, key=lambda g: (abs(sc(g)[0] - sc(g)[1]), -sum(sc(g))))["gid"]
                gotw["blowout"] = max(wk, key=lambda g: abs(sc(g)[0] - sc(g)[1]))["gid"]
                gotw["goals"] = max(wk, key=lambda g: sum(sc(g)))["gid"]
                shots = [g for g in wk if g.get("shots") and len(g["shots"]) == 2 and all(g["shots"].values())]
                if shots:
                    gotw["shots"] = max(shots, key=lambda g: sum(v[-1] for v in g["shots"].values()))["gid"]

        # --- upcoming ---
        upcoming = sorted((dict(gid=gid, day=s["day"], time=s["time"], home=s["home"], away=s["away"], loc=s["loc"])
                           for gid, s in sched.items() if s["div"] == d and not s["final"]),
                          key=lambda s: (s["day"], sched[s["gid"]]["sort"]))
        if sid == CURRENT:
            write_calendars(d, name, sched, G)

        S.setdefault("ranks_new", {})[name] = ranks
        thru = mmdd(last) if last else "-"
        data[name] = dict(games=len(gs), thru=thru, days=days, rows=rows, hot=hot, hotspan=span, race=race,
                          standings=standings, goalies=goalies, G=G, hats=hats, bigs=bigs, gotw=gotw, upcoming=upcoming,
                          types=sorted({g["type"] for g in allg if g["type"] != "Regular"}))
        data[name]["recap"] = recap_text(sid, name, data[name], sched, today)
    return data


def recap_text(sid, name, D, sched, today):
    """Plain-text weekend recap, ready to paste into a group chat."""
    if not D["days"]:
        return "No games yet."
    last = D["days"][-1]
    lo = (dt.date.fromisoformat(last) - dt.timedelta(days=2)).isoformat()
    G = D["G"]
    wk = sorted(((gid, g) for gid, g in G.items() if g["day"] >= lo), key=lambda x: (x[1]["day"], sched.get(x[0], {}).get("sort", 0)))
    L = [f"HSL {name} – weekend recap, {D['hotspan']}", "", "RESULTS"]
    day = ""
    for gid, g in wk:
        if g["day"] != day:
            day = g["day"]
            L.append(dt.date.fromisoformat(day).strftime("%a %b %-d"))
        L.append(f"  {short(g['away'])} {g['vs']}, {short(g['home'])} {g['hs']}{' (SO)' if g['status'] == 'Final SO' else ''}")
    L += ["", "STANDINGS (top 5)"] + [f"  {i + 1}. {short(s['team'])} {s['W']}-{s['L']}-{s['T']}, {s['PTS']} pts" for i, s in enumerate(D["standings"][:5])]
    L += ["", "SCORING LEADERS"]
    for r in D["rows"][:5]:
        m = r[11]
        L.append(f"  {r[10]}. {r[0]} ({short(r[1])}) {r[6]} pts, {r[4]}G {r[5]}A in {r[3]} GP" + (f" ▲{m}" if m and m > 0 else f" ▼{-m}" if m and m < 0 else ""))
    if D["hot"]:
        L += ["", "HOT THIS WEEKEND"] + [f"  {h[0]} ({short(h[1])}) {h[5]} pts, {h[3]}G {h[4]}A in {h[2]} GP" for h in D["hot"][:5]]
    hats = [h for h in D["hats"] if h[0] >= lo]
    if hats:
        L += ["", "HAT TRICKS"]
        for h in hats:
            r, g = D["rows"][h[1]], G[h[2]]
            L.append(f"  {r[0]} ({short(r[1])}) {h[3]}G {h[4]}A vs {short(g['away'] if g['home'] == r[1] else g['home'])}")
    best = None
    for q in D["goalies"]:
        l = [x for x in q["log"] if x[5] and x[0] in G and G[x[0]]["day"] >= lo]
        sa, sv = sum(x[3] for x in l), sum(x[4] for x in l)
        if sa >= 15 and (best is None or sv / sa > best[1] / best[2]):
            best = (q, sv, sa)
    if best:
        q, sv, sa = best
        L += ["", "GOALIE OF THE WEEKEND", f"  {q['name']} ({short(q['team'])}) {sv}/{sa} saves, .{round(sv / sa * 1000):03d}"]
    lo2, hi2 = weekend_window({k: v for k, v in sched.items() if v["div"] == name}, today)
    if lo2:
        up = [u for u in D["upcoming"] if lo2 <= u["day"] <= hi2]
        if up:
            L += ["", "NEXT UP"]
            day = ""
            for u in up:
                if u["day"] != day:
                    day = u["day"]
                    L.append(dt.date.fromisoformat(day).strftime("%a %b %-d"))
                L.append(f"  {u['time']} {short(u['away'])} at {short(u['home'])}, {u['loc']}")
    L += ["", f"Full stats: {BASE}#{slug(name)}/leaders"]
    return "\n".join(L)


def short(team):
    return team.replace(" Hockey Academy", "").replace(" Hockey Club", "").replace(" Hockey", "")


def build(state):
    today = dt.date.today().isoformat()
    ddir = os.path.join(os.path.dirname(OUT), "data")
    os.makedirs(ddir, exist_ok=True)
    seasons_out, blobs = [], {}
    for sid, label in SEASONS:
        S = state["seasons"].get(sid)
        if not S or not S.get("sched"):
            continue
        data = build_season(state, sid, today)
        sdir = os.path.join(ddir, label)
        os.makedirs(sdir, exist_ok=True)
        # league-wide player index for this season (search across divisions)
        players = [[r[0], r[1], slug(n), r[3], r[4], r[5], r[6], slug(r[0] + " " + short(r[1]))]
                   for n, v in data.items() for r in v["rows"]]
        open(os.path.join(sdir, "players.json"), "w", encoding="utf-8").write(json.dumps(players, separators=(",", ":"), ensure_ascii=False))
        divs = []
        for n, v in data.items():
            blob = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
            blobs[(label, n)] = blob
            open(os.path.join(sdir, slug(n) + ".json"), "w", encoding="utf-8").write(blob)
            divs.append(dict(key=slug(n), name=n, games=v["games"], thru=v["thru"],
                             leader=(v["rows"][0][:2] + [v["rows"][0][6]]) if v["rows"] else None,
                             top=v["standings"][0]["team"] if v["standings"] else None,
                             weekend=len([u for u in v["upcoming"] if (lambda w: w[0] and w[0] <= u["day"] <= w[1])(weekend_window({k: s for k, s in S["sched"].items() if s["div"] == n}, today))]) if sid == CURRENT else 0,
                             hot=[[h[0], h[1], h[5], h[6]] for h in v["hot"][:3]],
                             movers=sorted(([r[0], r[1], r[6], r[11], r[10]] for r in v["rows"] if r[11] and r[11] >= 3), key=lambda x: -x[3])[:3],
                             hats=[[h[0], v["rows"][h[1]][0], v["rows"][h[1]][1], h[3], h[4]] for h in v["hats"]
                                   if v["days"] and h[0] >= (dt.date.fromisoformat(v["days"][-1]) - dt.timedelta(days=2)).isoformat()]))
        seasons_out.append(dict(key=label, divisions=divs, current=sid == CURRENT))
        sig = hashlib.sha1("".join(blobs[(label, n)] for n in sorted(data)).encode()).hexdigest()
        if sig != S.get("sig"):  # numbers changed: stamp the time and roll the rank snapshot
            S["sig"] = sig
            S["updated"] = dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p")
            S["ranks"] = S.get("ranks_new", {})
            if sid == CURRENT:
                state["updated"] = S["updated"]
                state["changed"] = True
        S.pop("ranks_new", None)
        if sid == CURRENT:
            write_cards(data, S["sched"], state.get("logo_files", {}), today)

    state.setdefault("updated", dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p"))
    index = dict(updated=state["updated"], logos=state.get("logo_files", {}), seasons=seasons_out,
                 divisions=next((s["divisions"] for s in seasons_out if s["current"]), []))
    open(os.path.join(ddir, "index.json"), "w", encoding="utf-8").write(json.dumps(index, separators=(",", ":"), ensure_ascii=False))
    tpl = open(os.path.join(HERE, "page.tpl.html"), encoding="utf-8").read()
    html = (tpl.replace("__INDEX__", json.dumps(index, separators=(",", ":"), ensure_ascii=False))
            .replace("__UPDATED__", state["updated"]).replace("__BASE__", BASE).replace("__SITE__", SITE))
    open(OUT, "w", encoding="utf-8").write(html)
    write_icons()
    png = os.path.join(os.path.dirname(OUT), "og.png")
    changed = state.pop("changed", False)
    if not changed and os.path.exists(png):
        return  # same numbers: leave the preview image byte-for-byte alone
    try:
        import og
        og.render(index, png, SITE)
    except Exception as e:  # the preview image is a nicety; never fail the run over it
        print(f"preview image skipped: {e}")


def write_if_changed(path, data):
    if os.path.exists(path) and open(path, "rb").read() == data:
        return
    open(path, "wb").write(data)


def write_icons():
    """Site icon + web app manifest, drawn once."""
    folder = os.path.dirname(OUT)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#0e1218"/>'
           '<rect x="0" y="0" width="7" height="64" rx="3" fill="#ff4d63"/>'
           '<text x="37" y="43" text-anchor="middle" font-family="Oswald,Impact,Arial Narrow,sans-serif" font-weight="700" '
           'font-size="30" fill="#e8ecf2" letter-spacing="1">HSL</text></svg>')
    write_if_changed(os.path.join(folder, "icon.svg"), svg.encode())
    manifest = json.dumps(dict(name="HSL Statistics", short_name="HSL Stats", start_url="./", display="standalone",
                               background_color="#0e1218", theme_color="#0e1218",
                               icons=[dict(src="icon-192.png", sizes="192x192", type="image/png"),
                                      dict(src="icon-512.png", sizes="512x512", type="image/png")]), indent=1)
    write_if_changed(os.path.join(folder, "manifest.webmanifest"), manifest.encode())
    if all(os.path.exists(os.path.join(folder, f)) for f in ("icon-192.png", "icon-512.png", "apple-touch-icon.png")):
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
        for size, fn in ((512, "icon-512.png"), (192, "icon-192.png"), (180, "apple-touch-icon.png")):
            im = Image.new("RGBA", (size, size), (14, 18, 24, 255))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=(14, 18, 24, 255))
            d.rectangle([0, 0, size // 9, size], fill=(255, 77, 99, 255))
            f = ImageFont.truetype(os.path.join(HERE, "fonts", "Oswald-SemiBold.ttf"), int(size * 0.5))
            tw = d.textlength("HSL", font=f)
            d.text(((size + size // 9) / 2 - tw / 2, size * 0.22), "HSL", font=f, fill=(232, 236, 242, 255))
            # corners: keep them transparent outside the rounded square
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=255)
            im.putalpha(mask)
            im.save(os.path.join(folder, fn), optimize=True)
    except Exception as e:
        print(f"icons skipped: {e}")


def write_cards(data, sched, logo_files, today):
    """A square 'this weekend' image per team, for dropping into a group chat."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return
    folder = os.path.join(os.path.dirname(OUT), "cards")
    os.makedirs(folder, exist_ok=True)
    logos_dir = os.path.dirname(OUT)
    fonts = os.path.join(HERE, "fonts")
    F = lambda n, s: ImageFont.truetype(os.path.join(fonts, n), s)
    bg, fg, muted, accent, line = (14, 18, 24), (232, 236, 242), (152, 162, 179), (255, 77, 99), (42, 50, 64)
    wanted = set()
    for name, D in data.items():
        lo, hi = weekend_window({k: s for k, s in sched.items() if s["div"] == name}, today)
        if not lo:
            continue
        teams = {u["home"] for u in D["upcoming"]} | {u["away"] for u in D["upcoming"]}
        sdir = os.path.join(folder, slug(name))
        os.makedirs(sdir, exist_ok=True)
        for team in sorted(teams):
            games = [u for u in D["upcoming"] if lo <= u["day"] <= hi and team in (u["home"], u["away"])]
            if not games:
                continue
            W = H = 1080
            im = Image.new("RGB", (W, H), bg)
            d = ImageDraw.Draw(im)
            d.rectangle([0, 0, 16, H], fill=accent)
            y = 70
            lf = logo_files.get(team)
            if lf:
                try:
                    lg = Image.open(os.path.join(logos_dir, lf)).convert("RGBA")
                    lg.thumbnail((120, 120))
                    im.paste(lg, (70, y), lg)
                    x0 = 215
                except Exception:
                    x0 = 70
            else:
                x0 = 70
            d.text((x0, y - 4), team.upper()[:26], font=F("Oswald-SemiBold.ttf", 58), fill=fg)
            d.text((x0 + 2, y + 66), f"{name} · This weekend", font=F("Barlow-Medium.ttf", 32), fill=accent)
            y = 240
            d.line([70, y - 20, W - 70, y - 20], fill=line, width=2)
            st = next((s for s in D["standings"] if s["team"] == team), None)
            if st:
                d.text((70, y - 8), f"{st['W']}-{st['L']}-{st['T']}, {st['PTS']} pts, {D['standings'].index(st) + 1}{['st', 'nd', 'rd'][D['standings'].index(st)] if D['standings'].index(st) < 3 else 'th'} of {len(D['standings'])}",
                       font=F("Barlow-Medium.ttf", 30), fill=muted)
                y += 60
            for u in games[:5]:
                opp = u["away"] if u["home"] == team else u["home"]
                vs = "vs" if u["home"] == team else "at"
                d.text((70, y), dt.date.fromisoformat(u["day"]).strftime("%a %b %-d").upper() + "  ·  " + u["time"],
                       font=F("Oswald-SemiBold.ttf", 34), fill=accent)
                lo_ = logo_files.get(opp)
                x = 70
                if lo_:
                    try:
                        lg = Image.open(os.path.join(logos_dir, lo_)).convert("RGBA")
                        lg.thumbnail((72, 72))
                        im.paste(lg, (70, y + 50), lg)
                        x = 160
                    except Exception:
                        pass
                d.text((x, y + 50), f"{vs} {opp}"[:34], font=F("Barlow-Medium.ttf", 44), fill=fg)
                d.text((x, y + 104), u["loc"][:48], font=F("Barlow-Medium.ttf", 28), fill=muted)
                y += 168
            d.text((70, H - 70), SITE, font=F("Barlow-Medium.ttf", 26), fill=muted)
            from io import BytesIO
            buf = BytesIO()
            im.save(buf, "PNG", optimize=True)
            path = os.path.join(sdir, slug(team) + ".png")
            write_if_changed(path, buf.getvalue())
            wanted.add(path)
    # drop cards for teams that have nothing this weekend
    for root, _, files in os.walk(folder):
        for f in files:
            p_ = os.path.join(root, f)
            if p_ not in wanted:
                os.remove(p_)


if __name__ == "__main__":
    main()

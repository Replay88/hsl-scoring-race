"""Refresh the HSL 2017 Major / 2018 Major scoring race.

Reads the league's calendar feed for the full schedule, fetches the box
score of every game that's gone final since last time (cached in
cache/pages), then rebuilds docs/index.html (and docs/og.png when the
numbers change).

    python3 -I tools/update.py            # from the "HSL Scoring Race" folder
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

LEAGUE = "13952"  # HSL 2026-27 season
DIVS = {  # every HSL division, in the league's own order
    "32761": "2014 AAA", "23371": "2014 Major", "37782": "2015 AAA", "23373": "2015 Major", "28373": "2015",
    "23374": "2016 Major", "32762": "2016", "28374": "2017 Major", "32763": "2017", "32765": "2018 Major",
    "32764": "2018", "37783": "2019 Major"}
URL = "https://www.hockeysuperleague.ca/division/0/{}/game/view/{}"
PATHS = ["32765", "28374", "0"]  # any division path serves any game
ICS = "https://www.hockeysuperleague.ca/calendar/master-schedule/481.ics?SID={}&DID={}&TZ=America%2FEdmonton"
PARSER = 3  # bump when the box-score parser changes so cached games get re-read


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


def schedule(did):
    """Every game in a division from the league's calendar feed."""
    req = urllib.request.Request(ICS.format(LEAGUE, did), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ics = r.read().decode("utf-8", "ignore").replace("\r\n ", "").replace("\n ", "")
    out = {}
    for ev in ics.split("BEGIN:VEVENT")[1:]:
        uid = re.search(r"UID:leaguegame-(\d+)", ev)
        start = re.search(r"DTSTART[^:]*:(\d{8})T(\d{4})", ev)
        summ = re.search(r"SUMMARY:[^:]*: (.+?) vs (.+)", ev)
        if not (uid and start and summ):
            continue
        day = dt.datetime.strptime(start.group(1), "%Y%m%d").date().isoformat()
        hh, mm = int(start.group(2)[:2]), start.group(2)[2:]
        loc = re.search(r"LOCATION:(.+)", ev)
        num = re.search(r"Game #(\d+)", ev)
        out[uid.group(1)] = dict(div=did, day=day, time=f"{(hh - 1) % 12 + 1}:{mm} {'AM' if hh < 12 else 'PM'}",
                                 sort=hh * 60 + int(mm), home=summ.group(1).strip(), away=summ.group(2).strip(),
                                 loc=loc.group(1).split("\\,")[0].strip() if loc else "",
                                 num=num.group(1) if num else "", final="Final" in ev)
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
            sched.update(schedule(did))
        except Exception as e:
            print(f"could not read the {name} calendar feed ({e}); nothing changed")
            return
    todo = [gid for gid, s in sched.items()
            if s["final"] and not (games.get(gid) and games[gid]["status"].startswith("Final")
                                   and games[gid].get("parser") == PARSER)]
    print(f"{len(sched)} scheduled games, {sum(1 for s in sched.values() if s['final'])} final, {len(todo)} to read", flush=True)

    errors = []

    def work(gid):
        try:
            html, cached = load(gid)
            g = examine(gid, html)
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
    games = {k: v for k, v in games.items() if k in sched and v}  # drop cancelled / stray games

    state["games"] = games
    state["sched"] = sched
    build(state)
    json.dump(state, open(STATE, "w"), separators=(",", ":"))
    finals = [g for g in games.values() if g.get("div") in DIVS and g["status"].startswith("Final")]
    for d, name in DIVS.items():
        n = [g for g in finals if g["div"] == d]
        bad = [g["gid"] for g in n if sum(p["G"] for p in g["players"]) != sum(map(int, re.findall(r"\d+", g["score"])))]
        print(f"{name}: {len(n)} final games, last {max((g['day'] for g in n), default='-')}"
              + (f", goal totals off vs score in {bad}" if bad else ""))
    print(f"pages that failed (will retry next run): {errors}" if errors else "all pages read")
    print(OUT)


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
            "BEGIN:VEVENT", f"UID:hsl-{gid}@replay88.github.io",
            "DTSTAMP:" + start.strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"), "DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + ics_escape(f"{name}: {title}"),
            "LOCATION:" + ics_escape(s["loc"]),
            "DESCRIPTION:" + ics_escape(f"HSL {name} game #{s['num']}. Stats: https://replay88.github.io/hsl-scoring-race/#{name[:4]}/g/{gid}"),
            "END:VEVENT"])

    def write(path, label, evs):
        body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HSL Scoring Race//EN", "CALSCALE:GREGORIAN",
                             "X-WR-CALNAME:" + ics_escape(label), "X-WR-TIMEZONE:America/Edmonton"] + evs + ["END:VCALENDAR"]) + "\r\n"
        open(path, "w", encoding="utf-8", newline="").write(body)

    ordered = sorted(games, key=lambda x: (x[1]["day"], x[1]["sort"]))
    write(os.path.join(folder, "all.ics"), f"HSL {name}", [event(gid, s) for gid, s in ordered])
    for team in teams:
        write(os.path.join(folder, slug(team) + ".ics"), f"{team} ({name})",
              [event(gid, s) for gid, s in ordered if team in (s["home"], s["away"])])


def build(state):
    data = {}
    games = state["games"]
    sched = state.get("sched", {})
    for d, name in DIVS.items():
        gs = sorted((g for g in games.values() if g and g.get("div") == d and g["status"].startswith("Final")),
                    key=lambda g: (g["day"], sched.get(g["gid"], {}).get("sort", 0), g["gid"]))
        days = sorted({g["day"] for g in gs})
        last = days[-1] if days else None

        # --- games (results + box scores) ---
        G = {}
        for g in gs:
            h, a = map(int, re.findall(r"\d+", g["score"]))
            G[g["gid"]] = dict(day=g["day"], time=sched.get(g["gid"], {}).get("time", ""), rink=g["rink"],
                               home=g["home"], away=g["away"], hs=h, vs=a, status=g["status"],
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
        old = state.get("ranks", {}).get(name, {})
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
        write_calendars(d, name, sched, G)

        state.setdefault("ranks_new", {})[name] = ranks
        thru = mmdd(last) if last else "-"
        data[name] = dict(games=len(gs), thru=thru, days=days, rows=rows, hot=hot, hotspan=span, race=race,
                          standings=standings, goalies=goalies, G=G, hats=hats, bigs=bigs, gotw=gotw, upcoming=upcoming)

    # one data file per division, loaded by the page on demand
    ddir = os.path.join(os.path.dirname(OUT), "data")
    os.makedirs(ddir, exist_ok=True)
    blobs = {name: json.dumps(v, separators=(",", ":"), ensure_ascii=False) for name, v in data.items()}
    sig = hashlib.sha1("".join(blobs[n] for n in sorted(blobs)).encode()).hexdigest()
    changed = sig != state.get("sig")
    if changed:  # numbers changed: stamp the time and roll the rank snapshot
        state["sig"] = sig
        state["updated"] = dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p")
        state["ranks"] = state["ranks_new"]
    state.pop("ranks_new", None)
    index = dict(updated=state["updated"], divisions=[
        dict(key=slug(name), name=name, games=v["games"], thru=v["thru"],
             leader=(v["rows"][0][:2] + [v["rows"][0][6]]) if v["rows"] else None)
        for name, v in data.items()])
    for name, blob in blobs.items():
        open(os.path.join(ddir, slug(name) + ".json"), "w", encoding="utf-8").write(blob)
    open(os.path.join(ddir, "index.json"), "w", encoding="utf-8").write(json.dumps(index, separators=(",", ":"), ensure_ascii=False))
    tpl = open(os.path.join(HERE, "page.tpl.html"), encoding="utf-8").read()
    html = tpl.replace("__INDEX__", json.dumps(index, separators=(",", ":"), ensure_ascii=False)).replace("__UPDATED__", state["updated"])
    open(OUT, "w", encoding="utf-8").write(html)
    png = os.path.join(os.path.dirname(OUT), "og.png")
    if not changed and os.path.exists(png):
        return  # same numbers: leave the preview image byte-for-byte alone
    try:
        import og
        og.render(index, png)
    except Exception as e:  # the preview image is a nicety; never fail the run over it
        print(f"preview image skipped: {e}")


if __name__ == "__main__":
    main()

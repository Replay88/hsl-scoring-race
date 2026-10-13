"""Collect every skater who played in the HSL's oldest divisions (the alumni pool).

Reads the league calendar for the chosen past seasons, fetches the box score of each
final game in the chosen divisions (cached in cache/pages like everything else), and
writes cache/alumni_pool.json: one row per player with the seasons/divisions/teams they
played for and their totals. A hand-curated tools/alumni.json then says where they went.

    python3 -I tools/alumni_scan.py
"""
import concurrent.futures as cf
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as U  # noqa: E402

# season id -> label, and which divisions hold the oldest kids
SCAN = {
    "3038": ("2019-20", ["U15 Prep", "U14 Varsity", "2008 Minor"]),
    "3573": ("2020-21", ["U18 Prep", "U15 Prep", "U14 Prep", "U14 Varsity", "2008 Major AAA", "2008 Major AA", "2008 Minor"]),
    "4328": ("2021-22", ["U18 Prep", "U15 Prep", "U14 Prep", "U14 Varsity", "2009 AAA", "2009 Major", "2009 Minor"]),
    "6445": ("2022-23", ["U18 Prep", "U15 Prep", "U14 Prep", "2010 Major AAA", "2010 Major"]),
    "7935": ("2023-24", ["2011 AAA", "2011 Major"]),
}
POOL = os.path.join(U.CACHE, "alumni_pool.json")


def main():
    os.makedirs(U.PAGES, exist_ok=True)
    pool = json.load(open(POOL)) if os.path.exists(POOL) else {"players": {}, "done": []}
    for sid, (label, divs) in SCAN.items():
        sched = U.schedule(sid)
        gids = [gid for gid, s in sched.items() if s["final"] and s["div"] in divs and f"{sid}:{gid}" not in pool["done"]]
        print(f"{label}: {len(gids)} games to read in {divs}", flush=True)

        def work(gid):
            try:
                html, cached = U.load(gid)
                g = U.examine(gid, html)
                if g and g["status"].startswith("Final"):
                    if not cached:
                        open(os.path.join(U.PAGES, f"{gid}.html"), "w", encoding="utf-8").write(html)
                    return gid, g, None
                return gid, None, None
            except Exception as e:
                return gid, None, e

        n = 0
        with cf.ThreadPoolExecutor(3) as ex:
            for gid, g, err in ex.map(work, gids):
                if err is not None:
                    continue
                pool["done"].append(f"{sid}:{gid}")
                if not g:
                    continue
                n += 1
                div = sched[gid]["div"]
                for p in g["players"]:
                    k = p["name"].strip().lower()
                    row = pool["players"].setdefault(k, {"name": U.nice(p["name"].strip()), "stints": {}})
                    st = row["stints"].setdefault(f"{label}|{div}|{p['team']}", {"season": label, "div": div, "team": p["team"], "GP": 0, "G": 0, "A": 0, "PTS": 0})
                    st["GP"] += 1; st["G"] += p["G"]; st["A"] += p["A"]; st["PTS"] += p["PTS"]
        json.dump(pool, open(POOL, "w"))
        print(f"{label}: read {n} games; pool now {len(pool['players'])} players", flush=True)


if __name__ == "__main__":
    main()

"""Put back box scores that a re-read wiped.

On Nov 10 2026 the league stopped showing player lines for its 2015-2019 groups, on old games
too. Before update.py learned to guard against that, its two-week re-read replaced 136 stored
box scores (Oct 30 - Nov 8) with empty ones. This copies those games' players, goalies and
goal/penalty events back from an earlier commit of cache/state.json. Scores and status stay as
they are now. Nothing is fetched from the league.

    python3 tools/restore_boxes.py 934be18d963        # the last state before the change
    python3 tools/restore_boxes.py 934be18d963 --dry  # report only
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "cache", "state.json")
SID = "13952"  # 2026-27


def main():
    src, dry = sys.argv[1], "--dry" in sys.argv
    old = json.loads(subprocess.run(["git", "-C", ROOT, "show", src + ":cache/state.json"],
                                    capture_output=True, text=True, check=True).stdout)["seasons"][SID]["games"]
    state = json.load(open(STATE))
    games = state["seasons"][SID]["games"]
    done = {}
    for gid, g in games.items():
        o = old.get(gid)
        if g.get("players") or not (o and o.get("players")):
            continue
        if (o["home"], o["away"]) != (g["home"], g["away"]):
            continue  # not the same game; leave it alone
        keep = dict(o, score=g["score"], status=g["status"], div=g["div"])
        if "type" in g:
            keep["type"] = g["type"]
        games[gid] = keep
        done.setdefault(g["div"], []).append(g["day"])
    for div in sorted(done):
        print(f"{div}: {len(done[div])} games, {min(done[div])} to {max(done[div])}")
    print("restored", sum(map(len, done.values())), "games" + (" (dry run, nothing written)" if dry else ""))
    if not dry:
        state["seasons"][SID].pop("sig", None)  # make the next build redraw everything
        json.dump(state, open(STATE, "w"), separators=(",", ":"))


if __name__ == "__main__":
    main()

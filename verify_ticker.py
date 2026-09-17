import sys
sys.path.insert(0, r"C:\Users\Starc\thc-social\app")
import ticker
print("cover tests:", ticker.cover_result(21, 17, -4.5),   # home -4.5, up 4 -> away
      ticker.cover_result(24, 17, -4.5),                    # home -4.5, up 7 -> home
      ticker.cover_result(20, 17, 3.5))                     # home +3.5 dog, down 3 -> home
games = ticker.get_scores()
print("games:", len(games))
leagues = {}
for g in games:
    leagues[g["league"]] = leagues.get(g["league"], 0) + 1
print("by league:", leagues)
live = [g for g in games if g["state"] == "in"][:4]
for g in live:
    print(g["league"], g["away"]["abbr"], g["away"]["score"], "@",
          g["home"]["abbr"], g["home"]["score"], "| spread:", g["spread"],
          "| covering:", g["covering"], "|", (g.get("clock") or "")[:20])
withspread = sum(1 for g in games if g.get("spread") is not None)
print("games with spread:", withspread)
open(r"C:\Users\Starc\thc-social\tk_out.txt", "w").write("done")

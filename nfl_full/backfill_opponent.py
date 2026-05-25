"""One-shot fix: derive opponent/matchup from the two distinct team_abbr
values per game in player_games.

The PFR scrape stored full team names in games_to_scrape.home_team/away_team
("Portsmouth Spartans") while player_games.team_abbr holds 3-letter codes
("CRD"). The matchup-build comparison never matched, leaving every
opponent/matchup NULL. Fix by looking purely within player_games.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "nfl_full.sqlite"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# For each game, find the two distinct team_abbr values present.
games = cur.execute("""
    SELECT game_id, GROUP_CONCAT(DISTINCT team_abbr) AS teams
    FROM player_games
    WHERE team_abbr IS NOT NULL
    GROUP BY game_id
""").fetchall()

print(f"scanning {len(games):,} games...")
fixed = 0
weird = 0
for game_id, teams_csv in games:
    teams = [t for t in teams_csv.split(",") if t]
    if len(teams) != 2:
        weird += 1
        continue
    a, b = teams
    # team_abbr=a -> opponent=b, matchup="a vs b"; and vice versa
    cur.execute(
        "UPDATE player_games SET opponent=?, matchup=? WHERE game_id=? AND team_abbr=?",
        (b, f"{a} vs {b}", game_id, a),
    )
    cur.execute(
        "UPDATE player_games SET opponent=?, matchup=? WHERE game_id=? AND team_abbr=?",
        (a, f"{b} vs {a}", game_id, b),
    )
    fixed += 1

conn.commit()
print(f"fixed: {fixed:,} games")
print(f"weird (not exactly 2 teams):     {weird:,}")
remaining = cur.execute("SELECT COUNT(*) FROM player_games WHERE matchup IS NULL").fetchone()[0]
print(f"rows still missing matchup: {remaining:,}")

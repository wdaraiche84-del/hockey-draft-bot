"""Stockage SQLite de l'état du draft. Un seul draft par serveur Discord."""

import sqlite3
import unicodedata
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER PRIMARY KEY,
    list_channel_id INTEGER,
    picks_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    team TEXT NOT NULL,
    position TEXT NOT NULL,
    drafted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gms (
    guild_id INTEGER NOT NULL,
    team_key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, team_key)
);
CREATE TABLE IF NOT EXISTS picks (
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    round INTEGER NOT NULL,
    in_round INTEGER NOT NULL,
    team TEXT NOT NULL,
    player_id INTEGER,
    message_id INTEGER,
    PRIMARY KEY (guild_id, number)
);
CREATE TABLE IF NOT EXISTS list_messages (
    guild_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (guild_id, position)
);
"""


def normalize(text: str) -> str:
    """Minuscules, sans accents ni espaces superflus, pour comparer des noms."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


@dataclass
class Player:
    id: int
    name: str
    team: str
    position: str


@dataclass
class Pick:
    number: int
    round: int
    in_round: int
    team: str
    player_id: int | None
    message_id: int | None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # Salons

    def set_channels(self, guild_id: int, list_channel_id: int, picks_channel_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO config VALUES (?, ?, ?)",
                (guild_id, list_channel_id, picks_channel_id),
            )

    def get_channels(self, guild_id: int) -> tuple[int, int] | None:
        row = self.db.execute(
            "SELECT list_channel_id, picks_channel_id FROM config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    # Joueurs

    def replace_players(self, guild_id: int, rows: list[tuple[str, str, str]]) -> None:
        """Remplace la liste de joueurs. Les choix déjà faits sont effacés."""
        with self.db:
            self.db.execute("DELETE FROM players WHERE guild_id = ?", (guild_id,))
            self.db.execute(
                "UPDATE picks SET player_id = NULL, message_id = NULL WHERE guild_id = ?", (guild_id,)
            )
            self.db.executemany(
                "INSERT INTO players (guild_id, name, team, position) VALUES (?, ?, ?, ?)",
                [(guild_id, *row) for row in rows],
            )

    def available_players(self, guild_id: int) -> list[Player]:
        rows = self.db.execute(
            "SELECT id, name, team, position FROM players WHERE guild_id = ? AND drafted = 0 ORDER BY id",
            (guild_id,),
        ).fetchall()
        return [Player(*row) for row in rows]

    def get_player(self, guild_id: int, player_id: int) -> Player | None:
        row = self.db.execute(
            "SELECT id, name, team, position FROM players WHERE guild_id = ? AND id = ?",
            (guild_id, player_id),
        ).fetchone()
        return Player(*row) if row else None

    def search_available(self, guild_id: int, query: str, limit: int = 25) -> list[Player]:
        q = normalize(query)
        players = self.available_players(guild_id)
        starts = [p for p in players if normalize(p.name).startswith(q)]
        contains = [p for p in players if q in normalize(p.name) and p not in starts]
        return (starts + contains)[:limit]

    def resolve_available(self, guild_id: int, value: str) -> list[Player]:
        """Trouve le joueur visé par une commande. Plus d'un résultat veut dire ambigu."""
        players = self.available_players(guild_id)
        value = value.strip()
        if value.isdigit():
            by_id = [p for p in players if p.id == int(value)]
            if by_id:
                return by_id
        q = normalize(value)
        exact = [p for p in players if normalize(p.name) == q]
        if exact:
            return exact
        return [p for p in players if q in normalize(p.name)]

    # DG

    def set_gm(self, guild_id: int, team: str, user_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO gms VALUES (?, ?, ?)", (guild_id, normalize(team), user_id)
            )

    def gm_for(self, guild_id: int, team: str) -> int | None:
        row = self.db.execute(
            "SELECT user_id FROM gms WHERE guild_id = ? AND team_key = ?", (guild_id, normalize(team))
        ).fetchone()
        return row[0] if row else None

    # Ordre et choix

    def set_order(self, guild_id: int, teams: list[str]) -> int:
        """Remplace l'ordre de sélection. Retourne le nombre d'équipes par ronde."""
        per_round = len({normalize(t) for t in teams})
        with self.db:
            self.db.execute("DELETE FROM picks WHERE guild_id = ?", (guild_id,))
            self.db.execute("UPDATE players SET drafted = 0 WHERE guild_id = ?", (guild_id,))
            self.db.executemany(
                "INSERT INTO picks (guild_id, number, round, in_round, team) VALUES (?, ?, ?, ?, ?)",
                [
                    (guild_id, i + 1, i // per_round + 1, i % per_round + 1, team)
                    for i, team in enumerate(teams)
                ],
            )
        return per_round

    def _picks(self, sql: str, params: tuple) -> list[Pick]:
        rows = self.db.execute(
            "SELECT number, round, in_round, team, player_id, message_id FROM picks " + sql, params
        ).fetchall()
        return [Pick(*row) for row in rows]

    def current_pick(self, guild_id: int) -> Pick | None:
        picks = self._picks(
            "WHERE guild_id = ? AND player_id IS NULL ORDER BY number LIMIT 1", (guild_id,)
        )
        return picks[0] if picks else None

    def upcoming_picks(self, guild_id: int, count: int) -> list[Pick]:
        return self._picks(
            "WHERE guild_id = ? AND player_id IS NULL ORDER BY number LIMIT ?", (guild_id, count)
        )

    def last_made_pick(self, guild_id: int) -> Pick | None:
        picks = self._picks(
            "WHERE guild_id = ? AND player_id IS NOT NULL ORDER BY number DESC LIMIT 1", (guild_id,)
        )
        return picks[0] if picks else None

    def get_pick(self, guild_id: int, number: int) -> Pick | None:
        picks = self._picks("WHERE guild_id = ? AND number = ?", (guild_id, number))
        return picks[0] if picks else None

    def record_pick(self, guild_id: int, number: int, player_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE picks SET player_id = ? WHERE guild_id = ? AND number = ?",
                (player_id, guild_id, number),
            )
            self.db.execute(
                "UPDATE players SET drafted = 1 WHERE guild_id = ? AND id = ?", (guild_id, player_id)
            )

    def set_pick_message(self, guild_id: int, number: int, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE picks SET message_id = ? WHERE guild_id = ? AND number = ?",
                (message_id, guild_id, number),
            )

    def undo_pick(self, guild_id: int, number: int) -> None:
        pick = self.get_pick(guild_id, number)
        if pick is None or pick.player_id is None:
            return
        with self.db:
            self.db.execute(
                "UPDATE picks SET player_id = NULL, message_id = NULL WHERE guild_id = ? AND number = ?",
                (guild_id, number),
            )
            self.db.execute(
                "UPDATE players SET drafted = 0 WHERE guild_id = ? AND id = ?",
                (guild_id, pick.player_id),
            )

    def transfer_pick(self, guild_id: int, number: int, team: str) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE picks SET team = ? WHERE guild_id = ? AND number = ? AND player_id IS NULL",
                (team, guild_id, number),
            )
        return cur.rowcount == 1

    # Messages de la liste publiée

    def list_messages(self, guild_id: int) -> list[tuple[int, str]]:
        rows = self.db.execute(
            "SELECT message_id, content FROM list_messages WHERE guild_id = ? ORDER BY position",
            (guild_id,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def set_list_messages(self, guild_id: int, messages: list[tuple[int, str]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM list_messages WHERE guild_id = ?", (guild_id,))
            self.db.executemany(
                "INSERT INTO list_messages VALUES (?, ?, ?, ?)",
                [(guild_id, i, mid, content) for i, (mid, content) in enumerate(messages)],
            )

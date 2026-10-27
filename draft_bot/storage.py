"""Stockage SQLite de l'état du draft. Un seul draft par serveur Discord."""

import sqlite3
import time
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
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    at REAL NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_messages (
    guild_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (guild_id, position)
);
"""

# Colonnes ajoutées après la première version : une base existante les reçoit au démarrage.
CONFIG_COLUMNS = {
    "pick_seconds": "INTEGER NOT NULL DEFAULT 0",
    "admin_role_id": "INTEGER",
    "clock_pick": "INTEGER",
    "clock_started": "REAL",
    "clock_alerted": "INTEGER NOT NULL DEFAULT 0",
    "button_message_id": "INTEGER",
    "started": "INTEGER NOT NULL DEFAULT 0",
}


def normalize(text: str) -> str:
    """Minuscules, sans accents ni espaces superflus, pour comparer des noms."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def team_display(name: str) -> str:
    """Met une majuscule aux noms d'équipes tapés tout en minuscules.

    « laval » devient « Laval » et « mtl » devient « MTL ». Un nom qui
    contient déjà une majuscule est gardé tel quel.
    """
    name = " ".join(name.split())
    if name != name.lower():
        return name
    return name.upper() if len(name) <= 3 else name.title()


class AlreadyRunning(Exception):
    """Une autre copie du bot utilise déjà cette base."""


class PickError(Exception):
    """Un choix refusé. Le message s'affiche tel quel au membre."""


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
    def __init__(self, path: str, exclusive: bool = False):
        """Avec exclusive, la base est verrouillée tant que le bot tourne : une deuxième
        copie du bot (deux fenêtres, ou deux déploiements en même temps) ne peut pas
        l'ouvrir et risquer d'enregistrer deux choix différents."""
        self.db = sqlite3.connect(path, timeout=1)
        self.db.row_factory = sqlite3.Row
        if exclusive:
            try:
                self.db.execute("PRAGMA locking_mode = EXCLUSIVE")
                with self.db:
                    self.db.execute("CREATE TABLE IF NOT EXISTS instance (id INTEGER PRIMARY KEY, started REAL)")
                    self.db.execute("INSERT OR REPLACE INTO instance VALUES (1, ?)", (time.time(),))
            except sqlite3.OperationalError as error:
                self.db.close()
                raise AlreadyRunning(path) from error
        self.db.executescript(SCHEMA)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(config)")}
        with self.db:
            for name, definition in CONFIG_COLUMNS.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE config ADD COLUMN {name} {definition}")
            if "started" not in existing:
                # Avant le bouton « Lancer le draft », définir l'ordre lançait le draft :
                # un draft déjà commencé dans une ancienne base continue sans rien changer.
                self.db.execute("UPDATE config SET started = 1 WHERE guild_id IN (SELECT guild_id FROM picks)")

    # Salons

    def set_channels(self, guild_id: int, list_channel_id: int, picks_channel_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO config (guild_id, list_channel_id, picks_channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET list_channel_id = excluded.list_channel_id, "
                "picks_channel_id = excluded.picks_channel_id",
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
        self.start_clock(guild_id)

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

    # Ordre et choix

    def set_order(self, guild_id: int, teams: list[str]) -> int:
        """Remplace l'ordre de sélection. Retourne le nombre d'équipes par ronde.

        Le nouveau draft attend que l'admin clique sur « Lancer le draft ».
        """
        per_round = len({normalize(t) for t in teams})
        with self.db:
            self.db.execute(
                "INSERT INTO config (guild_id, started) VALUES (?, 0) "
                "ON CONFLICT (guild_id) DO UPDATE SET started = 0",
                (guild_id,),
            )
            self.db.execute("DELETE FROM picks WHERE guild_id = ?", (guild_id,))
            self.db.execute("UPDATE players SET drafted = 0 WHERE guild_id = ?", (guild_id,))
            self.db.executemany(
                "INSERT INTO picks (guild_id, number, round, in_round, team) VALUES (?, ?, ?, ?, ?)",
                [
                    (guild_id, i + 1, i // per_round + 1, i % per_round + 1, team_display(team))
                    for i, team in enumerate(teams)
                ],
            )
        self.start_clock(guild_id)
        return per_round

    def start_draft(self, guild_id: int) -> bool:
        """Lance le draft. Retourne False s'il était déjà lancé ou s'il n'y a pas d'ordre."""
        if self.pick_count(guild_id) == 0:
            return False
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO config (guild_id) VALUES (?)", (guild_id,))
            cur = self.db.execute(
                "UPDATE config SET started = 1 WHERE guild_id = ? AND started = 0", (guild_id,)
            )
        if cur.rowcount != 1:
            return False
        self.start_clock(guild_id)
        return True

    def is_started(self, guild_id: int) -> bool:
        row = self.db.execute("SELECT started FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
        return bool(row and row[0])

    def _picks(self, sql: str, params: tuple) -> list[Pick]:
        rows = self.db.execute(
            "SELECT number, round, in_round, team, player_id, message_id FROM picks " + sql, params
        ).fetchall()
        return [Pick(*row) for row in rows]

    def current_pick(self, guild_id: int) -> Pick | None:
        """Le choix à faire maintenant. Aucun tant que le draft n'est pas lancé."""
        if not self.is_started(guild_id):
            return None
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

    def made_picks_count(self, guild_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM picks WHERE guild_id = ? AND player_id IS NOT NULL", (guild_id,)
        ).fetchone()[0]

    def pick_count(self, guild_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM picks WHERE guild_id = ?", (guild_id,)).fetchone()[0]

    def order_teams(self, guild_id: int) -> list[str]:
        """Les équipes de l'ordre, une seule fois chacune, dans l'ordre de leur premier choix."""
        rows = self.db.execute("SELECT team FROM picks WHERE guild_id = ? ORDER BY number", (guild_id,)).fetchall()
        return list(dict.fromkeys(row[0] for row in rows))

    def player_count(self, guild_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM players WHERE guild_id = ?", (guild_id,)).fetchone()[0]

    def get_pick(self, guild_id: int, number: int) -> Pick | None:
        picks = self._picks("WHERE guild_id = ? AND number = ?", (guild_id, number))
        return picks[0] if picks else None

    def make_pick(self, guild_id: int, number: int, player_id: int,
                  user_id: int | None = None, forced: bool = False) -> tuple[Pick, Player]:
        """Enregistre un choix, en vérifiant dans la même transaction que c'est encore
        le tour de ce choix et que le joueur est encore disponible."""
        with self.db:
            pick = self.current_pick(guild_id)
            if pick is None or pick.number != number:
                raise PickError("Le tour a changé pendant ta confirmation. Rien n'a été enregistré.")
            player = self.get_player(guild_id, player_id)
            drafted = self.db.execute(
                "SELECT drafted FROM players WHERE guild_id = ? AND id = ?", (guild_id, player_id)
            ).fetchone()
            if player is None or drafted[0]:
                raise PickError("Ce joueur n'est plus disponible. Rien n'a été enregistré.")
            self.db.execute(
                "UPDATE picks SET player_id = ? WHERE guild_id = ? AND number = ? AND player_id IS NULL",
                (player_id, guild_id, number),
            )
            self.db.execute(
                "UPDATE players SET drafted = 1 WHERE guild_id = ? AND id = ?", (guild_id, player_id)
            )
            self._log(guild_id, user_id, "choix admin" if forced else "choix",
                      f"#{number} {pick.team} : {player.name}")
        self.start_clock(guild_id)
        return pick, player

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
        self.start_clock(guild_id)

    def transfer_pick(self, guild_id: int, number: int, team: str) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE picks SET team = ? WHERE guild_id = ? AND number = ? AND player_id IS NULL",
                (team_display(team), guild_id, number),
            )
        if cur.rowcount != 1:
            return False
        current = self.current_pick(guild_id)
        if current is not None and current.number == number:
            # Le choix en cours change de main : la nouvelle équipe reçoit un délai complet,
            # et l'alerte de fin de délai pourra repartir.
            self.start_clock(guild_id)
        return True

    # Minuterie

    def set_timer(self, guild_id: int, seconds: int, admin_role_id: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO config (guild_id, pick_seconds, admin_role_id) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET pick_seconds = excluded.pick_seconds, "
                "admin_role_id = excluded.admin_role_id",
                (guild_id, seconds, admin_role_id),
            )
        # Le choix en cours repart avec le nouveau délai.
        self.start_clock(guild_id)

    def timer(self, guild_id: int) -> tuple[int, int | None]:
        row = self.db.execute(
            "SELECT pick_seconds, admin_role_id FROM config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return (row[0], row[1]) if row else (0, None)

    def start_clock(self, guild_id: int, now: float | None = None) -> None:
        """Démarre le chrono du choix en cours. Appelé chaque fois que le tour change."""
        pick = self.current_pick(guild_id)
        with self.db:
            self.db.execute(
                "INSERT INTO config (guild_id, clock_pick, clock_started, clock_alerted) VALUES (?, ?, ?, 0) "
                "ON CONFLICT (guild_id) DO UPDATE SET clock_pick = excluded.clock_pick, "
                "clock_started = excluded.clock_started, clock_alerted = 0",
                (guild_id, pick.number if pick else None, time.time() if now is None else now),
            )

    def deadline(self, guild_id: int) -> float | None:
        """Heure limite du choix en cours, ou None s'il n'y a pas de délai."""
        row = self.db.execute(
            "SELECT pick_seconds, clock_pick, clock_started FROM config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        pick = self.current_pick(guild_id)
        if not row or not row[0] or pick is None or row[1] != pick.number:
            return None
        return row[2] + row[0]

    def expired_pick(self, guild_id: int, now: float | None = None) -> Pick | None:
        """Le choix en cours si son délai est écoulé et qu'aucun admin n'a encore été averti."""
        deadline = self.deadline(guild_id)
        if deadline is None or (time.time() if now is None else now) < deadline:
            return None
        alerted = self.db.execute(
            "SELECT clock_alerted FROM config WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]
        return None if alerted else self.current_pick(guild_id)

    def mark_alerted(self, guild_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE config SET clock_alerted = 1 WHERE guild_id = ?", (guild_id,))

    def timed_guilds(self) -> list[int]:
        rows = self.db.execute("SELECT guild_id FROM config WHERE pick_seconds > 0").fetchall()
        return [row[0] for row in rows]

    # Bouton « Faire mon choix »

    def button_message(self, guild_id: int) -> int | None:
        """Le dernier message public qui porte le bouton « Faire mon choix »."""
        row = self.db.execute("SELECT button_message_id FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else None

    def set_button_message(self, guild_id: int, message_id: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO config (guild_id, button_message_id) VALUES (?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET button_message_id = excluded.button_message_id",
                (guild_id, message_id),
            )

    # Journal et sauvegardes

    def _log(self, guild_id: int, user_id: int | None, action: str, details: str) -> None:
        self.db.execute(
            "INSERT INTO log (guild_id, at, user_id, action, details) VALUES (?, ?, ?, ?, ?)",
            (guild_id, time.time(), user_id, action, details),
        )

    def add_log(self, guild_id: int, user_id: int | None, action: str, details: str) -> None:
        with self.db:
            self._log(guild_id, user_id, action, details)

    def log_entries(self, guild_id: int) -> list[tuple[float, int | None, str, str]]:
        rows = self.db.execute(
            "SELECT at, user_id, action, details FROM log WHERE guild_id = ? ORDER BY id", (guild_id,)
        ).fetchall()
        return [tuple(row) for row in rows]

    def backup(self, path: str) -> None:
        """Copie cohérente de toute la base, même pendant que le bot l'utilise."""
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()

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

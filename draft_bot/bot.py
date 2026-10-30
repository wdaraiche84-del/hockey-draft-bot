"""Bot Discord qui gère le draft d'une ligue de hockey simulée."""

import asyncio
import csv
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.utils import escape_markdown
from dotenv import load_dotenv

from . import capture
from . import formatting as fmt
from . import moves
from . import shm
from . import wheel
from .storage import AlreadyRunning, Pick, PickError, Player, Store, normalize, team_display

log = logging.getLogger("draft_bot")

PINGS = discord.AllowedMentions(users=True, roles=True, everyone=False)
NO_PINGS = discord.AllowedMentions.none()

# Secondes de suspense avant d'afficher le résultat de la roue.
WHEEL_SUSPENSE = 3
# Temps d'affichage du « ✅ repêché » avant qu'il s'efface.
PICK_REPLY_SECONDS = 8

# Nombre de sauvegardes horaires gardées (deux jours).
BACKUPS_KEPT = 48

# Un menu Discord montre au plus 25 choix. Au-delà, le membre cherche par nom.
MENU_LIMIT = 25
# Le panneau admin reste utilisable 14 minutes (Discord coupe la réponse à 15).
PANEL_SECONDS = 14 * 60
# Une liste de joueurs fait quelques kilo-octets : un fichier plus gros n'en est pas une.
MAX_FILE_BYTES = 2_000_000
# Identifiant fixe du bouton « Faire mon choix » : il marche encore après un redémarrage.
PICK_BUTTON_ID = "draft:faire-mon-choix"
# Une capture d'écran fait rarement plus de quelques Mo.
MAX_IMAGE_BYTES = 10_000_000
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
# Temps maximal pour lire une capture (le premier essai après un démarrage est le plus lent).
CAPTURE_SECONDS = 90
# Le bot regarde l'historique des transactions SHM de chaque ligue suivie toutes les 5 minutes.
SHM_CHECK_MINUTES = 5
# Après une longue panne, le bot lit au plus 3 pages SHM (45 transactions) pour rattraper son retard.
SHM_MAX_PAGES = 3
# Les admins sont prévenus dans le salon quand SHM ne répond plus depuis une heure.
SHM_WARN_SECONDS = 3600


class ShmStatus:
    """La dernière lecture SHM d'un serveur, pour le panneau et les avertissements."""

    def __init__(self) -> None:
        self.ok_at: float | None = None
        self.error: str | None = None
        self.failing_since: float | None = None
        self.warned = False


class DraftBot(discord.Client):
    def __init__(self, store: Store, backup_dir: Path, shm_client: shm.ShmClient | None = None):
        super().__init__(intents=discord.Intents.default())
        self.store = store
        self.backup_dir = backup_dir
        # Le compte SHM du bot, ou None s'il n'est pas configuré (SHM_USERNAME et SHM_PASSWORD).
        self.shm = shm_client
        self.shm_status: defaultdict[int, ShmStatus] = defaultdict(ShmStatus)
        # Les « Changements par équipe » lus la dernière fois, par ligue : pour savoir si rien n'a été manqué.
        self.shm_feeds: dict[str, list[shm.FeedRow]] = {}
        # Club-école -> équipe LNH, appris sur l'accueil ou les pages « Effectifs » de chaque ligue.
        self.shm_farms: defaultdict[str, dict[str, str]] = defaultdict(dict)
        # Les pages « Effectifs » déjà lues pour trouver un club-école, par ligue.
        self.shm_rosters_read: defaultdict[str, set[str]] = defaultdict(set)
        self.tree = app_commands.CommandTree(self)
        # Un verrou par serveur : deux commandes ne modifient jamais le même draft en même temps.
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Les serveurs où les commandes sont déjà publiées depuis le démarrage.
        self.synced_guilds: set[int] = set()
        # Une publication à la fois : un serveur qui arrive pendant le démarrage attend son tour.
        self.sync_lock = asyncio.Lock()
        # Les panneaux /admin ouverts, par serveur, pour les mettre à jour après chaque changement.
        self.panels: defaultdict[int, set[AdminPanel]] = defaultdict(set)
        # Une seule capture lue à la fois, tous serveurs confondus : la lecture prend de la mémoire.
        self.capture_lock = asyncio.Semaphore(1)
        register_commands(self)

    async def setup_hook(self) -> None:
        # Les boutons « Faire mon choix » déjà publiés répondent dès le démarrage.
        self.add_view(PickButtonView(self))
        await self.clear_global_commands()
        self.backup("demarrage")
        self.check_timers.start()
        self.hourly_backup.start()
        if self.shm is not None:
            self.check_shm.start()

    async def close(self) -> None:
        if self.shm is not None:
            await self.shm.close()
        await super().close()

    async def clear_global_commands(self) -> None:
        """Vide la liste globale chez Discord, pour éviter les doublons.

        Les commandes sont publiées serveur par serveur (visibles tout de suite). Une ancienne
        version les publiait aussi en global : elles s'afficheraient en double. Le bot garde
        quand même ses commandes en mémoire, pour répondre partout.
        """
        commands = self.tree.get_commands()
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            log.warning("Impossible de vider les anciennes commandes globales : des doublons peuvent apparaître.")
        finally:
            for command in commands:
                self.tree.add_command(command)

    async def sync_guild(self, guild: discord.abc.Snowflake) -> bool:
        """Publie les commandes sur un serveur. Rend False si Discord refuse."""
        async with self.sync_lock:
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.warning("Commandes refusées sur le serveur %s : réinvite le bot avec la portée "
                            "« applications.commands ».", guild.id)
                return False
            except discord.HTTPException:
                log.warning("Impossible de publier les commandes sur le serveur %s : nouvel essai au "
                            "prochain démarrage.", guild.id)
                return False
            self.synced_guilds.add(guild.id)
            return True

    async def sync_all_guilds(self) -> None:
        for guild in list(self.guilds):
            if guild.id not in self.synced_guilds:
                await self.sync_guild(guild)

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s sur %d serveur(s)", self.user, len(self.guilds))
        await self.sync_all_guilds()
        log.info("Commandes publiées sur %d serveur(s).", len(self.synced_guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Nouveau serveur : %s (%s)", guild.name, guild.id)
        await self.sync_guild(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Le draft du serveur reste dans la base : s'il réinvite le bot, tout est encore là.
        log.info("Le bot a été retiré du serveur %s (%s)", guild.name, guild.id)
        self.synced_guilds.discard(guild.id)
        self.tree.clear_commands(guild=guild)

    def channels(self, guild: discord.Guild) -> tuple[discord.TextChannel, discord.TextChannel] | None:
        ids = self.store.get_channels(guild.id)
        if not ids:
            return None
        list_channel, picks_channel = guild.get_channel(ids[0]), guild.get_channel(ids[1])
        if not list_channel or not picks_channel:
            return None
        return list_channel, picks_channel

    def team_role(self, guild: discord.Guild, team: str) -> discord.Role | None:
        """Le rôle Discord qui porte le nom de l'équipe (sans tenir compte des majuscules ni des accents)."""
        key = normalize(team)
        return discord.utils.find(lambda role: normalize(role.name) == key, guild.roles)

    def is_on_the_clock(self, member: discord.Member, team: str) -> bool:
        role = self.team_role(member.guild, team)
        return role is not None and role in member.roles

    def on_the_clock(self, guild: discord.Guild) -> str:
        pick = self.store.current_pick(guild.id)
        if pick is None:
            return fmt.on_the_clock(None, "")
        role = self.team_role(guild, pick.team)
        ping = role.mention if role else "(aucun rôle à ce nom)"
        return fmt.on_the_clock(pick, ping, self.store.deadline(guild.id))

    async def post_with_clock(self, guild: discord.Guild, channel: discord.TextChannel, text: str) -> discord.Message:
        """Publie un message suivi de « Au tour de … ».

        Pendant le draft, ce message porte le bouton « Faire mon choix ». Le bouton est retiré
        du message précédent : un seul bouton reste en vue, celui du choix en cours.
        """
        text += "\n" + self.on_the_clock(guild)
        has_button = self.store.current_pick(guild.id) is not None
        if has_button:
            message = await channel.send(text, allowed_mentions=PINGS, view=PickButtonView(self))
        else:
            message = await channel.send(text, allowed_mentions=PINGS)
        old = self.store.button_message(guild.id)
        self.store.set_button_message(guild.id, message.id if has_button else None)
        if old and old != message.id:
            try:
                await channel.get_partial_message(old).edit(view=None)
            except discord.HTTPException:
                # Message effacé ou dans un ancien salon : le bouton y fonctionne quand même.
                pass
        return message

    async def remove_pick_button(self, guild: discord.Guild) -> None:
        old = self.store.button_message(guild.id)
        if not old:
            return
        self.store.set_button_message(guild.id, None)
        channels = self.channels(guild)
        if channels is None:
            return
        try:
            await channels[1].get_partial_message(old).edit(view=None)
        except discord.HTTPException:
            # Message effacé : rien à retirer. Un clic sur un vieux bouton est de toute façon refusé.
            pass

    async def announce(self, guild: discord.Guild, text: str, with_clock: bool = True) -> str:
        """Publie un message dans le salon des sélections, en mentionnant l'équipe au tour.

        Retourne une phrase à ajouter à la réponse privée de l'admin.
        """
        channels = self.channels(guild)
        if channels is None:
            return "\n⚠️ Rien n'a été annoncé : les salons du draft sont introuvables (`/admin`, puis **Plus d'options**, **Changer les salons**)."
        try:
            if with_clock:
                await self.post_with_clock(guild, channels[1], text)
            else:
                await channels[1].send(text, allowed_mentions=PINGS)
        except discord.HTTPException:
            log.exception("Annonce impossible sur le serveur %s", guild.id)
            return f"\n⚠️ L'annonce dans {channels[1].mention} a échoué. Vérifie les permissions du bot."
        return f"\n📣 Annoncé dans {channels[1].mention}."

    def register_panel(self, guild_id: int, panel: "AdminPanel") -> None:
        self.panels[guild_id].add(panel)

    def forget_panel(self, guild_id: int, panel: "AdminPanel") -> None:
        self.panels[guild_id].discard(panel)

    async def refresh_panels(self, guild: discord.Guild, actor_id: int | None = None) -> None:
        """Après un changement, les panneaux admin ouverts montrent tout de suite le nouvel état.

        Le panneau de l'admin qui vient d'agir revient à l'écran principal, pour lui montrer
        la prochaine étape. Celui d'un autre admin reste où il est.
        """
        for panel in list(self.panels[guild.id]):
            if actor_id is not None and panel.user_id == actor_id:
                panel.more = False
            await panel.refresh(guild)

    def backup(self, reason: str) -> None:
        """Copie la base dans le dossier des sauvegardes et garde les plus récentes."""
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            self.store.backup(str(self.backup_dir / f"draft_{stamp}_{reason}.db"))
            backups = sorted(self.backup_dir.glob("draft_*.db"), key=lambda p: p.stat().st_mtime)
            for old in backups[:-BACKUPS_KEPT]:
                old.unlink()
        except OSError:
            log.exception("Sauvegarde impossible dans %s", self.backup_dir)

    @tasks.loop(hours=1)
    async def hourly_backup(self) -> None:
        self.backup("horaire")

    @hourly_backup.before_loop
    async def before_hourly_backup(self) -> None:
        await self.wait_until_ready()
        # La sauvegarde de démarrage vient d'être faite.
        await asyncio.sleep(3600)

    @tasks.loop(minutes=1)
    async def check_timers(self) -> None:
        """Avertit les admins quand le délai du choix en cours est écoulé.

        Le chrono est gardé dans la base : après un redémarrage, une alerte
        manquée pendant que le bot était éteint part au premier passage.
        """
        for guild_id in self.store.timed_guilds():
            guild = self.get_guild(guild_id)
            channels = self.channels(guild) if guild else None
            if channels is None:
                continue
            try:
                async with self.locks[guild_id]:
                    pick = self.store.expired_pick(guild_id)
                    if pick is None:
                        continue
                    _, admin_role_id = self.store.timer(guild_id)
                    admin_role = guild.get_role(admin_role_id) if admin_role_id else None
                    await channels[1].send(
                        fmt.time_up(pick, admin_role.mention if admin_role else ""), allowed_mentions=PINGS
                    )
                    self.store.mark_alerted(guild_id)
                    self.store.add_log(guild_id, None, "temps écoulé", f"#{pick.number} {pick.team}")
            except Exception:
                # Une erreur sur un serveur ne doit jamais arrêter la minuterie des autres.
                log.exception("Impossible d'annoncer la fin du délai sur le serveur %s", guild_id)

    @check_timers.before_loop
    async def before_check_timers(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=SHM_CHECK_MINUTES)
    async def check_shm(self) -> None:
        """Annonce les nouvelles transactions et le ballottage SHM de chaque ligue suivie.

        Une ligue suivie par plusieurs serveurs n'est lue qu'une fois. Une erreur sur une ligue
        ou un serveur n'arrête jamais les autres, et une page illisible n'empêche pas d'annoncer
        ce qui vient des autres pages.
        """
        leagues: defaultdict[str, list[tuple[int, int | None]]] = defaultdict(list)
        for guild_id, slug, channel_id in self.store.shm_guilds():
            if self.get_guild(guild_id) is not None:
                leagues[slug].append((guild_id, channel_id))
        for slug, followers in leagues.items():
            try:
                await self.follow_league(slug, followers)
            except Exception:
                log.exception("Erreur imprévue en suivant la ligue SHM %s", slug)

    async def follow_league(self, slug: str, followers: list[tuple[int, int | None]]) -> None:
        errors: list[str] = []
        trades = waivers = standings = None
        try:
            trades, teams = await self.read_shm(slug, [guild_id for guild_id, _ in followers])
        except shm.ShmError as error:
            log.warning("Transactions SHM illisibles pour la ligue %s : %s", slug, error)
            errors.append(str(error))
        try:
            waivers = await self.shm.waivers(slug)
        except shm.ShmError as error:
            log.warning("Ballottage SHM illisible pour la ligue %s : %s", slug, error)
            errors.append(str(error))
        feed, complete = await self.read_feed(slug)
        claims = shm.feed_claims(feed) if feed is not None else {}
        buyouts = shm.feed_buyouts(feed, self.shm_farms[slug]) if feed is not None else None
        teams_known = (waivers.teams if waivers is not None else {}) | (teams if trades is not None else {})
        if buyouts:
            buyouts = await self.find_farms(slug, buyouts, teams_known, [g for g, _ in followers])
        # Le classement n'est lu que si un serveur veut le publier.
        if any(self.store.shm_channels(guild_id)["standings"] for guild_id, _ in followers):
            try:
                standings = await self.shm.standings(slug)
            except shm.ShmError as error:
                log.warning("Classement SHM illisible pour la ligue %s : %s", slug, error)
                errors.append(str(error))
        for guild_id, channel_id in followers:
            try:
                channels, locked = self.shm_channels(guild_id)
                # Une sorte d'annonce arrêtée est quand même retenue, sans rien publier : la rallumer
                # plus tard n'annonce que la suite.
                if trades is not None:
                    await self.publish_shm(guild_id, channels["trades"], trades, teams)
                if waivers is not None:
                    await self.publish_waivers(guild_id, channels["waivers"], waivers, claims, complete)
                if buyouts is not None:
                    await self.publish_buyouts(guild_id, channels["buyouts"], buyouts, teams_known)
                if standings is not None and channels["standings"] is not None:
                    await self.publish_standings(guild_id, channels["standings"], standings)
                problems = errors + ([f"Le bot ne peut pas écrire dans {', '.join(locked)}."] if locked else [])
                if problems:
                    await self.shm_failed(guild_id, channel_id, problems[0])
                else:
                    await self.shm_ok(guild_id, channels["trades"])
            except Exception:
                log.exception("Annonces SHM impossibles sur le serveur %s", guild_id)

    def shm_channels(self, guild_id: int) -> tuple[dict[str, discord.TextChannel | None], list[str]]:
        """Le salon de chaque sorte d'annonce SHM (None : arrêtée ou inutilisable), et les salons
        choisis où le bot ne peut pas écrire."""
        guild = self.get_guild(guild_id)
        found, locked = {}, []
        for kind, channel_id in self.store.shm_channels(guild_id).items():
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if channel_id and (channel is None or not bot_can_post(channel, guild)):
                name = f"#{channel.name}" if channel is not None else "un salon effacé"
                if name not in locked:
                    locked.append(name)
                channel = None
            found[kind] = channel
        return found, locked

    async def find_farms(self, slug: str, buyouts: list[shm.Buyout], teams: dict[str, str],
                         guild_ids: list[int]) -> list[shm.Buyout]:
        """Remplace le club-école d'un rachat à annoncer par son équipe LNH.

        Si l'accueil ne l'a pas dit, le bot lit les pages « Effectifs » des équipes jusqu'à trouver
        à qui appartient ce club-école (une seule fois par équipe tant que le bot tourne)."""
        farms = self.shm_farms[slug]

        def to_announce(buyout):
            key = shm.buyout_key(buyout)
            return any(self.store.shm_buyouts_primed(g) and not self.store.is_announced(g, key) for g in guild_ids)

        missing = {b.team for b in buyouts if shm.known_team(b.team, None) is None and b.team not in farms
                   and to_announce(b)}
        for farm in sorted(missing):
            for code in teams:
                if farm in farms:
                    break
                if code in self.shm_rosters_read[slug] or shm.known_team(code, teams.get(code)) is None:
                    continue
                try:
                    found = await self.shm.farm(slug, code)
                except shm.ShmError as error:
                    log.warning("Page « Effectifs » %s illisible dans la ligue %s : %s", code, slug, error)
                    break
                self.shm_rosters_read[slug].add(code)
                if found:
                    farms[found[0]] = code
        return [replace(b, team=farms[b.team]) if b.team in farms else b for b in buyouts]

    async def read_feed(self, slug: str) -> tuple[list[shm.FeedRow] | None, bool]:
        """Les « Changements par équipe » de l'accueil (ou None), et s'ils couvrent tout depuis la
        lecture d'avant. Illisible, l'accueil ne bloque ni les transactions ni le ballottage."""
        try:
            feed = await self.shm.feed(slug)
        except shm.ShmError as error:
            log.warning("Accueil SHM illisible pour la ligue %s : %s", slug, error)
            self.shm_feeds.pop(slug, None)
            return None, False
        complete = shm.feed_follows(self.shm_feeds.get(slug, []), feed)
        self.shm_feeds[slug] = feed
        return feed, complete

    @check_shm.before_loop
    async def before_check_shm(self) -> None:
        await self.wait_until_ready()

    def all_new(self, guild_id: int, trades: list) -> bool:
        return bool(trades) and not any(self.store.is_announced(guild_id, shm.shm_key(t)) for t in trades)

    async def read_shm(self, slug: str, guild_ids: list[int]) -> tuple[list, dict[str, str]]:
        """Les transactions d'une ligue, la plus récente en premier.

        Si toute une page est nouvelle pour un serveur, la page suivante en contient peut-être
        d'autres (le bot était éteint) : on la lit aussi, jusqu'à SHM_MAX_PAGES.
        """
        page = await self.shm.history(slug)
        trades, teams = list(page.trades), dict(page.teams)
        number = 1
        primed = [g for g in guild_ids if self.store.shm_primed(g)]
        while (page.has_next and number < SHM_MAX_PAGES
               and any(self.all_new(g, page.trades) for g in primed)):
            number += 1
            page = await self.shm.history(slug, number)
            trades += page.trades
            teams.update(page.teams)
        return trades, teams

    async def publish_shm(self, guild_id: int, channel: discord.TextChannel | None, trades: list,
                          teams: dict[str, str]) -> int:
        """Annonce les transactions pas encore annoncées sur ce serveur. Rend le nombre annoncé.

        La première lecture d'une ligue retient les transactions déjà sur SHM sans les annoncer :
        seules les suivantes sont annoncées. Sans salon, elles sont retenues sans être annoncées.
        """
        guild = self.get_guild(guild_id)
        posted = 0
        async with self.locks[guild_id]:
            roles = team_roles(guild)
            learned = self.store.learned_logos(guild_id)
            fresh = []
            for sides in trades:
                key = shm.shm_key(sides)
                if self.store.is_announced(guild_id, key):
                    continue
                trade = shm.to_trade(sides, teams)
                capture.resolve(trade, roles, learned)
                # Déjà annoncée avec une capture d'écran : on ne la répète pas.
                if self.store.is_announced(guild_id, capture.fingerprint(trade)):
                    self.store.mark_announced(guild_id, key)
                    continue
                fresh.append((key, trade))
            primed = self.store.shm_primed(guild_id)
            roles_by_id = {role.id: role for role in roles}
            # SHM met la plus récente en haut : on annonce de la plus ancienne à la plus récente.
            for key, trade in reversed(fresh):
                if primed and channel is not None:
                    await channel.send(capture.announcement(trade, roles_by_id, guild_emojis(guild)),
                                       allowed_mentions=PINGS)
                    posted += 1
                    self.store.add_log(guild_id, None, "transaction SHM",
                                       capture.summary(trade, roles_by_id, 500).replace("**", ""))
                self.store.mark_announced(guild_id, key)
                self.store.mark_announced(guild_id, capture.fingerprint(trade))
            if not primed:
                self.store.set_shm_primed(guild_id)
        return posted

    async def publish_waivers(self, guild_id: int, channel: discord.TextChannel | None, page: shm.WaiversPage,
                              claims: dict[str, str], complete: bool) -> int:
        """Annonce les joueurs soumis au ballottage depuis la dernière lecture, puis ceux qui en sont
        sortis : réclamés (par qui) ou pas. Rend le nombre de messages envoyés.

        La première lecture retient le ballottage déjà sur SHM sans l'annoncer. « Pas réclamé »
        n'est annoncé que si l'accueil confirme qu'aucune réclamation n'a pu passer inaperçue.
        """
        guild = self.get_guild(guild_id)
        posted = 0
        async with self.locks[guild_id]:
            before = self.store.shm_waivers(guild_id)
            if before is not None and channel is not None:
                old = {d["key"]: shm.ShmWaiver(**d) for d in before}
                current = {waiver.key for waiver in page.waivers}
                roles, emojis = team_roles(guild), guild_emojis(guild)
                messages = []
                for key, waiver in old.items():
                    if key in current:
                        continue
                    claimer = claims.get(normalize(waiver.name)) or waiver.claimer
                    if claimer:
                        messages.append((moves.claimed(waiver, claimer, page.teams, roles, emojis), PINGS,
                                         f"{waiver.name} réclamé par {claimer} ({waiver.team})"))
                    elif complete:
                        messages.append((moves.unclaimed(waiver, page.teams, roles, emojis), NO_PINGS,
                                         f"{waiver.name} pas réclamé ({waiver.team})"))
                    else:
                        log.info("Fin du ballottage de %s non annoncée : l'accueil SHM ne permet pas de savoir "
                                 "s'il a été réclamé.", waiver.name)
                for waiver in page.waivers:
                    if waiver.key not in old:
                        # Pas de notification : plusieurs joueurs sont souvent soumis en même temps.
                        messages.append((moves.placed(waiver, page.teams, roles, emojis), NO_PINGS,
                                         f"{waiver.name} soumis par {waiver.team}"))
                for text, mentions, summary in messages:
                    try:
                        await channel.send(text, allowed_mentions=mentions)
                    except discord.HTTPException:
                        log.exception("Annonce du ballottage impossible sur le serveur %s", guild_id)
                        continue
                    posted += 1
                    self.store.add_log(guild_id, None, "ballottage SHM", summary)
            self.store.set_shm_waivers(guild_id, [asdict(waiver) for waiver in page.waivers])
        return posted

    async def publish_buyouts(self, guild_id: int, channel: discord.TextChannel | None, buyouts: list[shm.Buyout],
                              teams: dict[str, str]) -> int:
        """Annonce les rachats de l'accueil pas encore annoncés, du plus ancien au plus récent.

        La première lecture retient les rachats déjà sur l'accueil sans les annoncer."""
        guild = self.get_guild(guild_id)
        posted = 0
        async with self.locks[guild_id]:
            primed = self.store.shm_buyouts_primed(guild_id)
            roles, emojis = team_roles(guild), guild_emojis(guild)
            for buyout in reversed(buyouts):
                key = shm.buyout_key(buyout)
                if self.store.is_announced(guild_id, key):
                    continue
                if primed and channel is not None:
                    try:
                        await channel.send(moves.bought_out(buyout, teams, roles, emojis), allowed_mentions=NO_PINGS)
                    except discord.HTTPException:
                        log.exception("Annonce d'un rachat impossible sur le serveur %s", guild_id)
                        continue
                    posted += 1
                    self.store.add_log(guild_id, None, "rachat SHM",
                                       f"{buyout.player} ({buyout.team}, {buyout.salary} $)")
                self.store.mark_announced(guild_id, key)
            if not primed:
                self.store.set_shm_buyouts_primed(guild_id)
        return posted

    async def publish_standings(self, guild_id: int, channel: discord.TextChannel, page: shm.StandingsPage) -> None:
        """Publie le classement, puis modifie les mêmes messages quand il change (après une simulation)."""
        guild = self.get_guild(guild_id)
        chunks = moves.standings(page, guild_emojis(guild))
        async with self.locks[guild_id]:
            old_channel_id, old = self.store.standings_messages(guild_id)
            if old_channel_id != channel.id:
                # Nouveau salon : l'ancien classement est effacé, un nouveau est publié.
                previous = guild.get_channel(old_channel_id) if old_channel_id else None
                for message_id, _ in old if previous is not None else []:
                    try:
                        await delete_message(previous, message_id)
                    except discord.HTTPException:
                        pass
                old = []
            new = []
            try:
                for i, chunk in enumerate(chunks):
                    if i < len(old):
                        message_id, content = old[i]
                        if content != chunk:
                            try:
                                await channel.get_partial_message(message_id).edit(content=chunk)
                            except discord.NotFound:
                                # Effacé à la main : tout est republié dans l'ordre à la prochaine lecture.
                                for later_id, _ in old[i + 1:]:
                                    await delete_message(channel, later_id)
                                old = old[:i]
                                message_id = (await channel.send(chunk, allowed_mentions=NO_PINGS)).id
                        new.append((message_id, chunk))
                    else:
                        new.append(((await channel.send(chunk, allowed_mentions=NO_PINGS)).id, chunk))
                for message_id, _ in old[len(chunks):]:
                    await delete_message(channel, message_id)
                old = []
            finally:
                # Même après une erreur, on garde la trace des messages publiés pour les modifier ensuite.
                self.store.set_standings_messages(guild_id, channel.id, new + old[len(new):])

    async def shm_ok(self, guild_id: int, channel: discord.TextChannel | None) -> None:
        status = self.shm_status[guild_id]
        if status.warned and channel is not None:
            try:
                await channel.send("✅ Je lis de nouveau SHM : les annonces reprennent.", allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass
        status.ok_at, status.error, status.failing_since, status.warned = time.time(), None, None, False

    async def shm_failed(self, guild_id: int, channel_id: int | None, error: str) -> None:
        """Retient l'erreur pour le panneau. Après une heure de panne, prévient une seule fois dans le salon."""
        status = self.shm_status[guild_id]
        status.error = error
        now = time.time()
        if status.failing_since is None:
            status.failing_since = now
        if status.warned or now - status.failing_since < SHM_WARN_SECONDS:
            return
        status.warned = True
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild and channel_id else None
        if channel is None or not bot_can_post(channel, guild):
            return
        try:
            await channel.send(f"⚠️ Je n'arrive plus à lire SHM depuis une heure : {error}\n"
                               "Elles seront annoncées dès que ça remarche.", allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass

    async def refresh_list(self, guild_id: int, channel: discord.TextChannel, repost: bool = False) -> None:
        """Met à jour les messages de la liste en ne modifiant que ceux qui ont changé."""
        old = self.store.list_messages(guild_id)
        if repost:
            for message_id, _ in old:
                await delete_message(channel, message_id)
            old = []
        chunks = fmt.render_list(self.store.available_players(guild_id))
        new = []
        done = False
        try:
            for i, chunk in enumerate(chunks):
                if i < len(old):
                    message_id, content = old[i]
                    if content != chunk:
                        try:
                            await channel.get_partial_message(message_id).edit(content=chunk)
                        except discord.NotFound:
                            message_id = (await channel.send(chunk, allowed_mentions=NO_PINGS)).id
                    new.append((message_id, chunk))
                else:
                    message = await channel.send(chunk, allowed_mentions=NO_PINGS)
                    new.append((message.id, chunk))
            for message_id, _ in old[len(chunks):]:
                await delete_message(channel, message_id)
            done = True
        finally:
            # Même après une erreur, on garde la trace de tous les messages publiés
            # pour que la prochaine mise à jour les modifie au lieu d'en créer d'autres.
            self.store.set_list_messages(guild_id, new if done else new + old[len(new):])


async def delete_message(channel: discord.TextChannel, message_id: int) -> None:
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        pass


async def reply(interaction: discord.Interaction, text: str, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS, **kwargs)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS, **kwargs)


def is_admin(member: discord.Member) -> bool:
    """Admin du draft = permission « Gérer le serveur »."""
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


def explain_error(error: Exception) -> str:
    """Un message clair pour le membre, selon le type d'erreur."""
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, app_commands.MissingPermissions):
        return "🔒 Cette commande est réservée aux admins (permission « Gérer le serveur »)."
    if isinstance(error, app_commands.NoPrivateMessage):
        return "Cette commande fonctionne seulement sur un serveur."
    if isinstance(error, discord.Forbidden):
        return ("🔒 Le bot n'a pas les permissions nécessaires. Vérifie qu'il peut voir les salons du draft, "
                "y envoyer des messages, lire l'historique et mentionner les rôles.")
    if isinstance(error, discord.HTTPException):
        return "❌ Discord a refusé l'action. Réessaie dans un instant. Si ça se répète, préviens un admin."
    return "❌ Erreur inattendue. Réessaie, et préviens un admin si ça se répète."


async def report_error(interaction: discord.Interaction, error: Exception) -> None:
    log.exception("Erreur sur un bouton ou une fenêtre", exc_info=error)
    try:
        await reply(interaction, explain_error(error))
    except discord.HTTPException:
        pass


class Confirm(discord.ui.View):
    """Boutons « Confirmer » et « Annuler », réservés au membre qui a lancé la commande."""

    def __init__(self, user_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.origin: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="⏳ En cours…", view=None)
        await self.on_confirm(interaction)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Annulé. Rien n'a été changé.", view=None)

    async def on_timeout(self) -> None:
        if self.origin:
            try:
                await self.origin.edit_original_response(
                    content="⌛ Pas de confirmation après 2 minutes. Rien n'a été changé.", view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)


async def ask_confirmation(interaction: discord.Interaction, question: str,
                           on_confirm: Callable[[discord.Interaction], Awaitable[None]], edit: bool = False) -> None:
    """Pose la question avec Confirmer / Annuler. Avec edit, remplace le message d'où vient le clic."""
    view = Confirm(interaction.user.id, on_confirm)
    view.origin = interaction
    if edit:
        await interaction.response.edit_message(content=question, view=view)
    else:
        await reply(interaction, question, view=view)


async def read_attachment(attachment: discord.Attachment) -> str:
    return fmt.decode(await attachment.read())


def admin_only(func):
    # default_permissions cache la commande aux non-admins, mais un serveur peut changer
    # ce réglage : has_permissions revérifie la permission à chaque utilisation.
    func = app_commands.checks.has_permissions(manage_guild=True)(func)
    return app_commands.default_permissions(manage_guild=True)(func)


def reset_warning(store: Store, guild_id: int) -> str:
    made = store.made_picks_count(guild_id)
    if not made:
        return ""
    return f"\n⚠️ **{made} choix déjà faits seront effacés.** Une sauvegarde sera faite avant."


# Faire un choix : le bouton « Faire mon choix », /draft et le choix d'un admin passent tous par ici.

async def check_turn(bot: DraftBot, interaction: discord.Interaction, forced: bool) -> Pick | None:
    """Le choix en cours, si ce membre peut le faire maintenant. Sinon, explique pourquoi."""
    if not bot.channels(interaction.guild):
        await reply(interaction, "Le draft n'est pas encore prêt. Un admin doit le préparer avec `/admin`.")
        return None
    if not bot.store.is_started(interaction.guild_id):
        text = "Le draft n'est pas encore commencé. Attends l'annonce du début du draft."
        if is_admin(interaction.user):
            text += f"\n*Admin : le panneau `/admin` montre la prochaine étape, jusqu'à **▶ {LAUNCH_STEP}**.*"
        await reply(interaction, text)
        return None
    pick = bot.store.current_pick(interaction.guild_id)
    if pick is None:
        await reply(interaction, "Aucun choix en attente : le draft est terminé.")
        return None
    if not forced and not bot.is_on_the_clock(interaction.user, pick.team):
        text = "Ce n'est pas ton tour.\n" + bot.on_the_clock(interaction.guild)
        if is_admin(interaction.user):
            text += "\n*Admin : pour choisir à la place d'une équipe, fais `/admin` puis **Choisir pour l'équipe au tour**.*"
        await reply(interaction, text)
        return None
    return pick


def pick_heading(pick: Pick, forced: bool) -> str:
    who = f"pour **{escape_markdown(pick.team)}**" if forced else f"**{escape_markdown(pick.team)}**, c'est ton tour"
    return f"🏒 {fmt.pick_label(pick)} : {who}."


async def open_pick(bot: DraftBot, interaction: discord.Interaction, forced: bool) -> None:
    """Montre le menu des joueurs, ou une fenêtre de recherche s'il y en a trop pour un menu."""
    pick = await check_turn(bot, interaction, forced)
    if pick is None:
        return
    players = bot.store.available_players(interaction.guild_id)
    if not players:
        await reply(interaction, "Il ne reste aucun joueur dans la liste. Préviens un admin.")
        return
    if len(players) > MENU_LIMIT:
        await interaction.response.send_modal(SearchModal(bot, forced))
        return
    await reply(interaction, pick_heading(pick, forced) + "\nChoisis le joueur dans le menu :",
                view=PlayerMenu(bot, interaction.user.id, players, forced))


async def search_players(bot: DraftBot, interaction: discord.Interaction, query: str, forced: bool) -> None:
    """Après la recherche par nom : un seul joueur trouvé passe directement à la confirmation."""
    pick = await check_turn(bot, interaction, forced)
    if pick is None:
        return
    query = " ".join(query.split())
    matches = bot.store.search_available(interaction.guild_id, query, MENU_LIMIT + 1)
    if not matches:
        await reply(interaction, f"Aucun joueur disponible ne correspond à « {escape_markdown(query)} ».\n"
                    "Clique de nouveau sur **Faire mon choix** et essaie avec moins de lettres.")
        return
    if len(matches) == 1:
        await confirm_player(bot, interaction, pick, matches[0], forced)
        return
    if len(matches) > MENU_LIMIT:
        text = (f"Plus de {MENU_LIMIT} joueurs correspondent, voici les {MENU_LIMIT} premiers. "
                "Si le bon n'y est pas, recommence en tapant plus de lettres.")
        matches = matches[:MENU_LIMIT]
    else:
        text = f"{len(matches)} joueurs correspondent. Choisis le bon dans le menu :"
    await reply(interaction, pick_heading(pick, forced) + "\n" + text,
                view=PlayerMenu(bot, interaction.user.id, matches, forced))


async def confirm_player(bot: DraftBot, interaction: discord.Interaction, pick: Pick, player: Player,
                         forced: bool, edit: bool = False) -> None:
    async def confirmed(button_interaction: discord.Interaction) -> None:
        await finish_pick(bot, button_interaction, pick.number, player.id, forced)

    await ask_confirmation(
        interaction,
        f"**{escape_markdown(pick.team)}** repêche {fmt.player_line(player)} ({fmt.pick_label(pick)}) ?",
        confirmed,
        edit=edit,
    )


async def finish_pick(bot: DraftBot, interaction: discord.Interaction, number: int, player_id: int,
                      forced: bool) -> None:
    guild_id = interaction.guild_id
    store = bot.store
    problems = []
    async with bot.locks[guild_id]:
        current = store.current_pick(guild_id)
        # Revérifié au moment du clic : le rôle a pu être retiré entre-temps.
        if not forced and current and not bot.is_on_the_clock(interaction.user, current.team):
            await reply(interaction, "Ce n'est plus ton tour. Rien n'a été enregistré.")
            return
        try:
            pick, player = store.make_pick(guild_id, number, player_id, interaction.user.id, forced)
        except PickError as error:
            await reply(interaction, f"❌ {error}")
            return
        channels = bot.channels(interaction.guild)
        if channels is None:
            problems.append("les salons du draft sont introuvables (`/admin`, puis **Plus d'options**, **Changer les salons**)")
        else:
            role = bot.team_role(interaction.guild, pick.team)
            name = role.name if role else None
            team = capture.team_for_name(name or pick.team)
            text = fmt.announcement(pick, player, forced, name,
                                    capture.emoji_prefix(team, guild_emojis(interaction.guild)))
            try:
                message = await bot.post_with_clock(interaction.guild, channels[1], text)
                store.set_pick_message(guild_id, pick.number, message.id)
            except discord.HTTPException as error:
                log.exception("Annonce impossible", exc_info=error)
                problems.append(f"l'annonce dans {channels[1].mention} a échoué")
            try:
                await bot.refresh_list(guild_id, channels[0])
            except discord.HTTPException as error:
                log.exception("Liste impossible à mettre à jour", exc_info=error)
                problems.append(f"la liste dans {channels[0].mention} n'a pas pu être mise à jour")
    await bot.refresh_panels(interaction.guild, interaction.user.id)
    text = f"✅ **{escape_markdown(player.name)}** repêché par **{escape_markdown(pick.team)}**."
    if problems:
        text += ("\n⚠️ Le choix est bien enregistré, mais " + " et ".join(problems)
                 + ". Vérifie les permissions du bot.")
        await reply(interaction, text)
        return
    # Tout s'est bien passé : la réponse privée s'efface d'elle-même, pour qu'il ne reste rien à fermer.
    await interaction.edit_original_response(content=text + f" Annoncé dans {channels[1].mention}.", view=None)
    await asyncio.sleep(PICK_REPLY_SECONDS)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class PickButtonView(discord.ui.View):
    """Le bouton « Faire mon choix » sous l'annonce « Au tour de … ».

    Il est permanent : il marche encore après un redémarrage du bot, et agit
    toujours sur le choix en cours au moment du clic.
    """

    def __init__(self, bot: DraftBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Faire mon choix", emoji="🏒", style=discord.ButtonStyle.success,
                       custom_id=PICK_BUTTON_ID)
    async def pick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_pick(self.bot, interaction, forced=False)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)


class PlayerMenu(discord.ui.View):
    """Menu des joueurs : un clic sur un nom mène à Confirmer / Annuler."""

    def __init__(self, bot: DraftBot, user_id: int, players: list[Player], forced: bool):
        super().__init__(timeout=300)
        self.bot, self.user_id, self.forced = bot, user_id, forced
        self.menu = discord.ui.Select(
            placeholder="Clique ici pour voir les joueurs",
            options=[
                discord.SelectOption(label=p.name[:100], value=str(p.id),
                                     description=" · ".join(x for x in (p.team, p.position) if x)[:100] or None)
                for p in players[:MENU_LIMIT]
            ],
        )
        self.menu.callback = self.chosen
        self.add_item(self.menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def chosen(self, interaction: discord.Interaction) -> None:
        self.stop()
        store = self.bot.store
        player = store.get_player(interaction.guild_id, int(self.menu.values[0]))
        pick = store.current_pick(interaction.guild_id)
        if (player is None or pick is None
                or (not self.forced and not self.bot.is_on_the_clock(interaction.user, pick.team))):
            await interaction.response.edit_message(
                content="Le draft a changé entre-temps (ce n'est plus ton tour). Rien n'a été enregistré.", view=None)
            return
        await confirm_player(self.bot, interaction, pick, player, self.forced, edit=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)


# Les fenêtres (modals) : quelques cases à remplir, puis « Envoyer ».

def text_value(item: discord.ui.TextInput) -> str:
    return (item.value or "").strip()


class BotModal(discord.ui.Modal):
    def __init__(self, bot: DraftBot, title: str):
        super().__init__(title=title, timeout=600)
        self.bot = bot

    def ask(self, text: str, component: discord.ui.Item, description: str | None = None) -> discord.ui.Item:
        self.add_item(discord.ui.Label(text=text, description=description, component=component))
        return component

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error)


class SearchModal(BotModal):
    def __init__(self, bot: DraftBot, forced: bool):
        super().__init__(bot, "Choisir pour l'équipe au tour" if forced else "Faire mon choix")
        self.forced = forced
        self.query = self.ask("Nom du joueur", discord.ui.TextInput(placeholder="Quelques lettres suffisent, ex. : lemi",
                                                                    max_length=100),
                              "Tape une partie du nom. Le bot te montre ensuite les joueurs trouvés.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await search_players(self.bot, interaction, text_value(self.query), self.forced)


class ChannelsModal(BotModal):
    def __init__(self, bot: DraftBot, current: tuple[int, int] | None):
        super().__init__(bot, "Salons du draft")
        defaults = [[discord.Object(id=c, type=discord.TextChannel)] for c in current] if current else [[], []]
        self.list_channel = self.ask(
            "Salon de la liste des joueurs",
            discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], default_values=defaults[0]),
            "Le bot y affiche les joueurs disponibles.")
        self.picks_channel = self.ask(
            "Salon des annonces de choix",
            discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], default_values=defaults[1]),
            "Le bot y annonce chaque choix et l'équipe au tour.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        await save_channels(self.bot, interaction, guild.get_channel(self.list_channel.values[0].id),
                            guild.get_channel(self.picks_channel.values[0].id))


class PlayersModal(BotModal):
    def __init__(self, bot: DraftBot):
        super().__init__(bot, "Joueurs repêchables")
        self.file = self.ask("Fichier des joueurs", discord.ui.FileUpload(max_values=1),
                             "Une ligne par joueur : Nom;Équipe actuelle;Position (CSV ou texte).")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await import_players(self.bot, interaction, self.file.values[0])


class OrderModal(BotModal):
    def __init__(self, bot: DraftBot):
        super().__init__(bot, "Ordre du draft")
        self.teams = self.ask(
            "Équipes, dans l'ordre de la 1re ronde",
            discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=4000,
                                 placeholder="Laval\nGranby\nMontréal"),
            "Une équipe par ligne (ou séparées par des virgules), au nom exact de son rôle.")
        self.rounds = self.ask(
            "Nombre de rondes", discord.ui.TextInput(default="1", max_length=2),
            "Choix échangés ? Écris tous les choix dans l'ordre, puis mets 1 ronde.")
        self.snake = self.ask(
            "Serpentin ?",
            discord.ui.Select(options=[
                discord.SelectOption(label="Non : même ordre à chaque ronde", value="non", default=True),
                discord.SelectOption(label="Oui : l'ordre s'inverse à chaque ronde", value="oui"),
            ]))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        snake = bool(self.snake.values) and self.snake.values[0] == "oui"
        await define_order(self.bot, interaction, text_value(self.teams), text_value(self.rounds), snake)


class DelayModal(BotModal):
    def __init__(self, bot: DraftBot, seconds: int, admin_role_id: int | None):
        super().__init__(bot, "Délai par choix")
        self.delay = self.ask(
            "Temps pour chaque choix",
            discord.ui.TextInput(default=fmt.duration_text(seconds).replace(" ", "") if seconds else None,
                                 placeholder="ex. : 8h, 90m ou 1h30", max_length=10),
            "Écris 0 pour enlever le délai.")
        self.admin_role = self.ask(
            "Rôle à avertir quand le temps est écoulé",
            discord.ui.RoleSelect(required=False, min_values=0, max_values=1,
                                  default_values=[discord.Object(id=admin_role_id, type=discord.Role)]
                                  if admin_role_id else []),
            "Facultatif. Souvent le rôle des admins de la ligue.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        role = self.admin_role.values[0] if self.admin_role.values else None
        await set_delay(self.bot, interaction, text_value(self.delay), role)


class TradeModal(BotModal):
    def __init__(self, bot: DraftBot):
        super().__init__(bot, "Échanger un choix")
        self.number = self.ask("Numéro du choix", discord.ui.TextInput(placeholder="ex. : 12", max_length=5),
                               "Le numéro # affiché dans /statut et dans les annonces.")
        self.team = self.ask("Nouvelle équipe propriétaire", discord.ui.RoleSelect(),
                             "Choisis le rôle de l'équipe qui reçoit le choix.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await trade_pick(self.bot, interaction, text_value(self.number), self.team.values[0])


# Les actions des admins. Chaque changement est confirmé, noté au journal et suivi d'un panneau à jour.

async def save_channels(bot: DraftBot, interaction: discord.Interaction, liste, selections) -> None:
    store = bot.store
    if liste is None or selections is None:
        await reply(interaction, "Salon introuvable. Le bot voit-il ce salon ? Réessaie dans `/admin`.")
        return
    if liste.id == selections.id:
        await reply(interaction, "Choisis deux salons différents : un pour la liste, un pour les annonces.")
        return
    missing = []
    for channel in (liste, selections):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.read_message_history):
            missing.append(channel.mention)
    if missing:
        await reply(interaction, "🔒 Le bot ne peut pas voir, écrire ou lire l'historique dans : "
                    + ", ".join(missing) + ". Corrige ses permissions puis recommence.")
        return
    store.set_channels(interaction.guild_id, liste.id, selections.id)
    store.add_log(interaction.guild_id, interaction.user.id, "salons", f"{liste.name} / {selections.name}")
    await reply(interaction, f"✅ Liste dans {liste.mention}, annonces dans {selections.mention}."
                + next_step_hint(bot, interaction.guild))
    await bot.refresh_panels(interaction.guild, interaction.user.id)


async def import_players(bot: DraftBot, interaction: discord.Interaction, fichier: discord.Attachment) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    if not bot.channels(interaction.guild):
        await reply(interaction, "Choisis d'abord les salons du draft dans `/admin`.")
        return
    if fichier.size > MAX_FILE_BYTES:
        await reply(interaction, "Ce fichier est trop gros pour une liste de joueurs. Envoie un fichier CSV ou texte.")
        return
    rows = fmt.parse_players(await read_attachment(fichier))
    if not rows:
        await reply(interaction, "Aucun joueur trouvé dans le fichier. Une ligne par joueur : "
                    "`Nom;Équipe actuelle;Position`.")
        return
    duplicates = fmt.duplicate_names(rows)
    preview = "\n".join(f"• {name} · {team} · {pos}" for name, team, pos in rows[:3])

    async def confirmed(button_interaction: discord.Interaction) -> None:
        channels = bot.channels(interaction.guild)
        if not channels:
            await reply(button_interaction, "Les salons du draft ne sont plus accessibles. Choisis-les de nouveau dans `/admin`.")
            return
        async with bot.locks[guild_id]:
            bot.backup("avant-liste")
            store.replace_players(guild_id, rows)
            store.add_log(guild_id, interaction.user.id, "liste", f"{len(rows)} joueurs importés")
            await bot.refresh_list(guild_id, channels[0], repost=True)
        await reply(button_interaction, f"✅ {len(rows)} joueurs publiés dans {channels[0].mention}."
                    + next_step_hint(bot, interaction.guild))
        await bot.refresh_panels(interaction.guild, interaction.user.id)

    text = f"Importer **{len(rows)} joueurs** ? Les premiers :\n{preview}"
    if duplicates:
        text += "\n⚠️ Noms en double : " + ", ".join(duplicates)
    await ask_confirmation(interaction, text + reset_warning(store, guild_id), confirmed)


async def define_order(bot: DraftBot, interaction: discord.Interaction, teams_text: str, rounds_text: str,
                       snake: bool) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    rounds_text = rounds_text or "1"
    if not rounds_text.isdigit() or not 1 <= int(rounds_text) <= 50:
        await reply(interaction, "Le nombre de rondes doit être un nombre entre 1 et 50. Réessaie dans `/admin`.")
        return
    first_round = fmt.parse_order(teams_text)
    if not first_round:
        await reply(interaction, "Aucune équipe trouvée. Écris une équipe par ligne. Réessaie dans `/admin`.")
        return
    order = fmt.build_order(first_round, int(rounds_text), snake)
    teams = sorted({team_display(t) for t in order})
    missing = [t for t in teams if bot.team_role(interaction.guild, t) is None]

    async def confirmed(button_interaction: discord.Interaction) -> None:
        async with bot.locks[guild_id]:
            bot.backup("avant-ordre")
            per_round = store.set_order(guild_id, order)
            store.add_log(guild_id, interaction.user.id, "ordre",
                          f"{len(order)} choix : " + ", ".join(team_display(t) for t in order))
            channels = bot.channels(interaction.guild)
            if channels and store.list_messages(guild_id):
                await bot.refresh_list(guild_id, channels[0])
            # Le bouton « Faire mon choix » de l'ancien ordre ne doit plus rester en vue.
            await bot.remove_pick_button(interaction.guild)
        await reply(button_interaction, f"✅ {len(order)} choix, {per_round} équipes par ronde."
                    + next_step_hint(bot, interaction.guild))
        await bot.refresh_panels(interaction.guild, interaction.user.id)

    text = f"Définir un ordre de **{len(order)} choix** pour {len(teams)} équipes : {', '.join(teams)} ?"
    if missing:
        text += ("\n⚠️ Aucun rôle Discord ne porte le nom de : " + ", ".join(missing)
                 + ". Ces équipes ne pourront pas faire leur choix elles-mêmes. "
                 "Vérifie l'orthographe ou crée le rôle.")
    await ask_confirmation(interaction, text + reset_warning(store, guild_id), confirmed)


async def launch_draft(bot: DraftBot, interaction: discord.Interaction) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    if store.is_started(guild_id):
        await reply(interaction, "Le draft est déjà lancé.")
        return
    channels = bot.channels(interaction.guild)
    first = store.upcoming_picks(guild_id, 1)
    if channels is None or not first or store.player_count(guild_id) == 0:
        await reply(interaction, "Le draft n'est pas encore prêt." + next_step_hint(bot, interaction.guild))
        return

    async def confirmed(button_interaction: discord.Interaction) -> None:
        async with bot.locks[guild_id]:
            if not store.start_draft(guild_id):
                await reply(button_interaction, "Le draft est déjà lancé, ou l'ordre a été effacé. Rien n'a changé.")
                return
            store.add_log(guild_id, interaction.user.id, "lancement", f"{store.pick_count(guild_id)} choix")
            announced = await bot.announce(interaction.guild, "🏒 **Le draft commence!**")
        await reply(button_interaction, "✅ Le draft est lancé!" + announced)
        await bot.refresh_panels(interaction.guild, interaction.user.id)

    await ask_confirmation(
        interaction,
        f"Lancer le draft maintenant ? **{escape_markdown(first[0].team)}** sera averti que c'est son tour "
        f"dans {channels[1].mention}.",
        confirmed,
    )


async def set_delay(bot: DraftBot, interaction: discord.Interaction, delay_text: str,
                    admin: discord.Role | None) -> None:
    store = bot.store
    seconds = fmt.parse_duration(delay_text)
    if seconds is None:
        await reply(interaction, "Délai non reconnu. Écris par exemple `8h`, `90m` ou `1h30`, ou `0` pour aucun délai.")
        return
    await interaction.response.defer(ephemeral=True)
    announced = ""
    async with bot.locks[interaction.guild_id]:
        store.set_timer(interaction.guild_id, seconds, admin.id if admin else None)
        store.add_log(interaction.guild_id, interaction.user.id, "délai",
                      fmt.duration_text(seconds) if seconds else "aucun")
        # Pendant un draft, l'équipe au tour voit tout de suite son nouveau temps limite.
        if store.current_pick(interaction.guild_id) is not None:
            notice = (f"⌛ Nouveau délai : **{fmt.duration_text(seconds)}** par choix." if seconds
                      else "⌛ Il n'y a plus de délai par choix.")
            announced = await bot.announce(interaction.guild, notice)
    if seconds == 0:
        text = "✅ Aucun délai par choix." + announced
    else:
        text = f"✅ Délai de **{fmt.duration_text(seconds)}** par choix. Le chrono du choix en cours repart à zéro."
        text += f"\n{admin.mention} sera mentionné quand le temps est écoulé." if admin else (
            "\n⚠️ Aucun rôle choisi : l'alerte ne mentionnera personne.")
        text += announced
    await reply(interaction, text)
    await bot.refresh_panels(interaction.guild, interaction.user.id)


async def trade_pick(bot: DraftBot, interaction: discord.Interaction, number_text: str, team: discord.Role) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    number_text = number_text.lstrip("#").strip()
    if not number_text.isdigit():
        await reply(interaction, "Écris le numéro du choix, par exemple `12`. Il est affiché dans `/statut`.")
        return
    numero = int(number_text)
    pick = store.get_pick(guild_id, numero)
    if pick is None or pick.player_id is not None:
        await reply(interaction, f"Le choix #{numero} n'existe pas ou a déjà été fait. Vérifie avec `/statut`.")
        return
    equipe = team.name
    if normalize(equipe) == normalize(pick.team):
        await reply(interaction, f"Le choix #{numero} appartient déjà à **{escape_markdown(pick.team)}**.")
        return

    async def confirmed(button_interaction: discord.Interaction) -> None:
        async with bot.locks[guild_id]:
            if not store.transfer_pick(guild_id, numero, equipe):
                await reply(button_interaction, f"Le choix #{numero} a été fait entre-temps. Rien n'a changé.")
                return
            store.add_log(guild_id, interaction.user.id, "échange",
                          f"#{numero} : {pick.team} → {team_display(equipe)}")
            current = store.current_pick(guild_id)
            announced = await bot.announce(
                interaction.guild,
                f"🔁 **Échange** : le {fmt.pick_label(pick)} de **{escape_markdown(pick.team)}** "
                f"appartient maintenant à **{escape_markdown(team_display(equipe))}**.",
                # Si c'est le choix en cours, la nouvelle équipe est mentionnée tout de suite.
                with_clock=current is not None and current.number == numero,
            )
        await reply(button_interaction,
                    f"✅ Le choix #{numero} appartient maintenant à **{team_display(equipe)}**." + announced)
        await bot.refresh_panels(interaction.guild, interaction.user.id)

    await ask_confirmation(
        interaction,
        f"Donner le **{fmt.pick_label(pick)}** de **{escape_markdown(pick.team)}** "
        f"à **{escape_markdown(team_display(equipe))}** ?",
        confirmed,
    )


async def undo_last_pick(bot: DraftBot, interaction: discord.Interaction) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    last = store.last_made_pick(guild_id)
    if last is None:
        await reply(interaction, "Aucun choix à annuler.")
        return
    player = store.get_player(guild_id, last.player_id)

    async def confirmed(button_interaction: discord.Interaction) -> None:
        async with bot.locks[guild_id]:
            latest = store.last_made_pick(guild_id)
            if latest is None or (latest.number, latest.player_id) != (last.number, last.player_id):
                await reply(button_interaction, "Un autre choix a été fait entre-temps. Rien n'a été annulé.")
                return
            store.undo_pick(guild_id, last.number)
            store.add_log(guild_id, interaction.user.id, "annulation",
                          f"#{last.number} {last.team} : {player.name}")
            channels = bot.channels(interaction.guild)
            if channels:
                if last.message_id:
                    await delete_message(channels[1], last.message_id)
                await bot.refresh_list(guild_id, channels[0])
                await bot.post_with_clock(
                    interaction.guild, channels[1],
                    f"↩️ {fmt.pick_label(last)} annulé : **{escape_markdown(player.name)}** retourne dans la liste.",
                )
        await reply(button_interaction, f"✅ {fmt.pick_label(last)} annulé.")
        await bot.refresh_panels(interaction.guild, interaction.user.id)

    await ask_confirmation(
        interaction,
        f"Annuler le **{fmt.pick_label(last)}** : **{escape_markdown(last.team)}** avait pris {fmt.player_line(player)} ?",
        confirmed,
    )


async def send_journal(bot: DraftBot, interaction: discord.Interaction) -> None:
    entries = bot.store.log_entries(interaction.guild_id)
    if not entries:
        await reply(interaction, "Le journal est vide.")
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Date", "Membre", "Action", "Détails"])
    for at, user_id, action, details in entries:
        member = interaction.guild.get_member(user_id) if user_id else None
        name = member.display_name if member else (str(user_id) if user_id else "bot")
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)), name, action, details])
    # utf-8-sig : Excel affiche correctement les accents.
    data = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
    recent = "\n".join(f"• {action} : {details}" for _, _, action, details in entries[-5:])
    await reply(interaction, f"📒 {len(entries)} actions. Les dernières :\n{recent}",
                file=discord.File(data, filename="journal-draft.csv"))


# Annoncer une transaction : l'admin envoie une capture du tableau SHM, vérifie l'aperçu, puis confirme.

def capture_installed() -> bool:
    """La lecture des captures a besoin de RapidOCR, installé avec requirements.txt."""
    return all(importlib.util.find_spec(name) is not None for name in ("rapidocr", "onnxruntime", "cv2", "numpy"))


NOT_INSTALLED = ("⚠️ La lecture des captures n'est pas encore installée sur l'ordinateur du bot.\n"
                 "Ferme le bot, tape `pip install -U -r requirements.txt` dans le dossier du bot, puis relance-le.")


def is_image(attachment: discord.Attachment) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    return content_type.startswith("image/") or attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


async def read_capture(bot: DraftBot, data: bytes) -> dict:
    """Lit la capture dans un processus séparé : le texte et les logos. Lance CaptureError en cas d'échec."""
    async with bot.capture_lock:
        with tempfile.TemporaryDirectory(prefix="capture-") as folder:
            path = Path(folder) / "capture.img"
            path.write_bytes(data)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "draft_bot.capture_reader", str(path),
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(process.communicate(), CAPTURE_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise capture.CaptureError("La lecture de la capture a pris trop de temps. Réessaie dans un instant.")
    if process.returncode != 0:
        log.error("Lecture de capture impossible : %s", err.decode(errors="replace")[-2000:])
        raise capture.CaptureError("Je n'arrive pas à lire cette image. Envoie une capture d'écran en PNG ou JPG.")
    try:
        return json.loads(out)
    except ValueError:
        log.error("Réponse illisible du lecteur de captures : %r", out[:500])
        raise capture.CaptureError("La lecture de la capture a échoué. Réessaie, et préviens un admin si ça se répète.")


def guild_emojis(guild: discord.Guild) -> dict[str, str]:
    """Les emotes d'équipe du serveur (ex. :PIT:), ajoutées devant le nom des équipes dans les annonces."""
    return capture.team_emojis(getattr(guild, "emojis", None) or [])


def team_roles(guild: discord.Guild) -> list[discord.Role]:
    return [role for role in guild.roles if role.name != "@everyone" and not getattr(role, "managed", False)]


def bot_can_post(channel: discord.TextChannel, guild: discord.Guild) -> bool:
    perms = channel.permissions_for(guild.me)
    return bool(perms.view_channel and perms.send_messages)


class CaptureModal(BotModal):
    def __init__(self, bot: DraftBot, channel_id: int | None):
        super().__init__(bot, "Annoncer une transaction")
        self.file = self.ask("Capture des transactions SHM", discord.ui.FileUpload(max_values=1),
                             "Une capture d'écran du tableau des transactions, avec les logos.")
        default = [discord.Object(id=channel_id, type=discord.TextChannel)] if channel_id else []
        self.channel = self.ask(
            "Salon des annonces de transactions",
            discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], default_values=default),
            "Le bot y publiera la transaction. Il s'en souviendra.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = interaction.guild.get_channel(self.channel.values[0].id)
        await preview_trades(self.bot, interaction, self.file.values[0], channel)


async def preview_trades(bot: DraftBot, interaction: discord.Interaction, image: discord.Attachment,
                         channel: discord.TextChannel | None) -> None:
    guild = interaction.guild
    if channel is None:
        await reply(interaction, "Salon introuvable. Le bot voit-il ce salon ? Réessaie dans `/admin`.")
        return
    if not bot_can_post(channel, guild):
        await reply(interaction, f"🔒 Le bot ne peut pas écrire dans {channel.mention}. Corrige ses permissions "
                    "ou choisis un autre salon.")
        return
    if not is_image(image):
        await reply(interaction, "Ce fichier n'est pas une image. Envoie une capture d'écran (PNG ou JPG).")
        return
    if image.size > MAX_IMAGE_BYTES:
        await reply(interaction, "Cette image est trop grosse. Envoie une capture d'écran plus petite.")
        return
    bot.store.set_trades_channel(guild.id, channel.id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        found = await read_capture(bot, await image.read())
        trades = capture.parse_table(found.get("lines", []), found.get("logos", []))
    except capture.CaptureError as error:
        await reply(interaction, f"❌ {error}")
        return
    view = TradePreview(bot, interaction.user.id, guild, trades, channel.id)
    view.origin = interaction
    await reply(interaction, view.text(guild), view=view)


class TradePreview(discord.ui.View):
    """L'aperçu des transactions lues : choisir lesquelles annoncer, corriger, puis annoncer."""

    def __init__(self, bot: DraftBot, user_id: int, guild: discord.Guild, trades: list[capture.Trade],
                 channel_id: int):
        super().__init__(timeout=PANEL_SECONDS)
        self.bot, self.user_id, self.channel_id = bot, user_id, channel_id
        self.origin: discord.Interaction | None = None
        self.skipped = max(0, len(trades) - MENU_LIMIT)
        self.trades = trades[:MENU_LIMIT]
        roles = team_roles(guild)
        learned = bot.store.learned_logos(guild.id)
        for trade in self.trades:
            capture.resolve(trade, roles, learned)
        self.announced = {i for i, t in enumerate(self.trades)
                          if bot.store.is_announced(guild.id, capture.fingerprint(t))}
        # Par défaut, tout ce qui n'a pas déjà été annoncé est coché.
        self.selected = [i for i in range(len(self.trades)) if i not in self.announced]
        self.build(guild)

    def roles_by_id(self, guild: discord.Guild) -> dict[int, discord.Role]:
        return {role.id: role for role in team_roles(guild)}

    def build(self, guild: discord.Guild) -> None:
        self.clear_items()
        count = len(self.trades)
        if count > 1:
            menu = discord.ui.Select(
                placeholder="Coche les transactions à annoncer", min_values=0, max_values=count, row=0,
                options=[discord.SelectOption(label=self.option_label(guild, i), value=str(i),
                                              default=i in self.selected) for i in range(count)])
            menu.callback = self.select_changed
            self.add_item(menu)
            fix = discord.ui.Select(
                placeholder="✏️ Corriger une transaction…", row=1,
                options=[discord.SelectOption(label=self.option_label(guild, i), value=str(i)) for i in range(count)])
            fix.callback = self.fix_chosen
            self.add_item(fix)
        else:
            fix_button = discord.ui.Button(label="Corriger", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
            fix_button.callback = self.fix_only
            self.add_item(fix_button)
        label = "Annoncer" if len(self.selected) <= 1 else f"Annoncer les {len(self.selected)}"
        announce = discord.ui.Button(label=label, emoji="📣", style=discord.ButtonStyle.success, row=2,
                                     disabled=not self.selected)
        announce.callback = self.announce
        self.add_item(announce)
        cancel = discord.ui.Button(label="Annuler", style=discord.ButtonStyle.secondary, row=2)
        cancel.callback = self.cancel
        self.add_item(cancel)

    def option_label(self, guild: discord.Guild, index: int) -> str:
        roles = self.roles_by_id(guild)
        names = []
        for side in self.trades[index].sides:
            role = roles.get(side.role_id)
            names.append(role.name if role else (side.name or "équipe inconnue"))
        label = f"{index + 1}. " + " ⇄ ".join(names)
        if index in self.announced:
            label += " (déjà annoncée)"
        return label[:100]

    def text(self, guild: discord.Guild) -> str:
        # Avec beaucoup de transactions, chacune est raccourcie pour que tout tienne dans un message.
        for limit in (300, 200, 140, 100, 70, 50):
            text = self.render(guild, limit)
            if len(text) <= fmt.MAX_MESSAGE:
                return text
        return text[:fmt.MAX_MESSAGE - 1] + "…"

    def render(self, guild: discord.Guild, limit: int) -> str:
        roles = self.roles_by_id(guild)
        count = len(self.trades)
        lines = [f"📸 **{count} transaction{'s' if count > 1 else ''} lue{'s' if count > 1 else ''}** dans ta capture. "
                 "Vérifie bien avant d'annoncer :", ""]
        for i, trade in enumerate(self.trades):
            mark = "✅" if i in self.selected else "⬜"
            line = f"{mark} **{i + 1}.** {capture.summary(trade, roles, limit)}"
            if i in self.announced:
                line += " *(déjà annoncée)*"
            issues = capture.problems(trade, roles)
            if issues:
                line += f"\n　⚠️ {', '.join(issues)} : clique **Corriger**"
            lines.append(line)
        if self.skipped:
            lines.append(f"*{self.skipped} transactions plus anciennes ne sont pas montrées.*")
        lines += ["", "Une erreur ? **Corriger**. Tout est bon ? **Annoncer**." if count == 1 else
                  "Coche celles à annoncer. Une erreur ? **Corriger**. Tout est bon ? **Annoncer**."]
        return "\n".join(lines)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            return False
        if not is_admin(interaction.user):
            await reply(interaction, "🔒 Réservé aux admins (permission « Gérer le serveur »).")
            return False
        return True

    async def show(self, interaction: discord.Interaction) -> None:
        self.build(interaction.guild)
        await interaction.response.edit_message(content=self.text(interaction.guild), view=self)

    async def select_changed(self, interaction: discord.Interaction) -> None:
        menu = next(item for item in self.children if isinstance(item, discord.ui.Select) and item.row == 0)
        self.selected = sorted(int(v) for v in menu.values)
        await self.show(interaction)

    async def fix_chosen(self, interaction: discord.Interaction) -> None:
        menu = next(item for item in self.children if isinstance(item, discord.ui.Select) and item.row == 1)
        await interaction.response.send_modal(TradeFixModal(self, int(menu.values[0]), interaction.guild))

    async def fix_only(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TradeFixModal(self, 0, interaction.guild))

    async def corrected(self, interaction: discord.Interaction, index: int, role_ids: list[int | None],
                        texts: list[list[str]]) -> None:
        trade = self.trades[index]
        guild = interaction.guild
        for side, role_id in zip(trade.sides, role_ids):
            if role_id is not None and role_id != side.role_id:
                side.role_id = role_id
                # Le bot retient ce logo : la prochaine fois, il trouvera l'équipe tout seul.
                if side.logo is not None and side.logo.descriptor:
                    self.bot.store.learn_logo(guild.id, role_id, side.logo.descriptor)
        trade.texts = texts
        if self.bot.store.is_announced(guild.id, capture.fingerprint(trade)):
            self.announced.add(index)
        else:
            self.announced.discard(index)
        if index not in self.selected and index not in self.announced:
            self.selected = sorted([*self.selected, index])
        await self.show(interaction)

    async def announce(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        roles = self.roles_by_id(guild)
        chosen = [i for i in self.selected if i < len(self.trades)]
        blocked = [i + 1 for i in chosen if capture.problems(self.trades[i], roles)]
        if blocked:
            await reply(interaction, "⚠️ À corriger d'abord : transaction " + ", ".join(map(str, blocked))
                        + ". Clique **Corriger** et choisis le rôle de chaque équipe.")
            return
        channel = guild.get_channel(self.channel_id)
        if channel is None or not bot_can_post(channel, guild):
            await reply(interaction, "🔒 Le salon des transactions est introuvable ou le bot ne peut pas y écrire. "
                        "Recommence avec `/admin`.")
            return
        self.stop()
        await interaction.response.edit_message(content="⏳ Annonce en cours…", view=None)
        posted = 0
        try:
            # SHM met la plus récente en haut : on annonce de la plus ancienne à la plus récente.
            for i in reversed(chosen):
                trade = self.trades[i]
                await channel.send(capture.announcement(trade, roles, guild_emojis(guild)), allowed_mentions=PINGS)
                posted += 1
                self.bot.store.mark_announced(guild.id, capture.fingerprint(trade))
                self.bot.store.add_log(guild.id, interaction.user.id, "transaction",
                                       capture.summary(trade, roles, 500).replace("**", ""))
        except discord.HTTPException:
            log.exception("Annonce de transaction impossible sur le serveur %s", guild.id)
            await interaction.edit_original_response(
                content=f"⚠️ {posted} transaction(s) annoncée(s) sur {len(chosen)} : Discord a refusé la suite. "
                        f"Vérifie les permissions du bot dans {channel.mention}.", view=None)
            return
        await interaction.edit_original_response(
            content=f"✅ {posted} transaction{'s' if posted > 1 else ''} annoncée{'s' if posted > 1 else ''} "
                    f"dans {channel.mention}.", view=None)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Annulé. Rien n'a été annoncé.", view=None)

    async def on_timeout(self) -> None:
        if self.origin:
            try:
                await self.origin.edit_original_response(
                    content="⌛ Aperçu fermé après 14 minutes. Rien n'a été annoncé. Recommence avec `/admin`.",
                    view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)


class TradeFixModal(BotModal):
    def __init__(self, preview: TradePreview, index: int, guild: discord.Guild):
        super().__init__(preview.bot, f"Corriger la transaction {index + 1}")
        self.preview, self.index = preview, index
        trade = preview.trades[index]
        self.roles, self.items = [], []
        for number, (side, texts) in enumerate(zip(trade.sides, trade.texts), start=1):
            known = side.role_id if side.role_id and guild.get_role(side.role_id) else None
            self.roles.append(self.ask(
                f"Équipe {number}",
                discord.ui.RoleSelect(required=False, min_values=0, max_values=1,
                                      default_values=[discord.Object(id=known, type=discord.Role)] if known else []),
                "Le rôle de l'équipe" + (f" (logo : {side.name})." if side.name else ".")))
            self.items.append(self.ask(
                f"Ce que l'équipe {number} reçoit",
                discord.ui.TextInput(style=discord.TextStyle.paragraph, default="\n".join(texts)[:1000] or None,
                                     max_length=1000, required=True),
                "Un élément par ligne : un joueur ou un choix de repêchage."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        role_ids = [select.values[0].id if select.values else None for select in self.roles]
        texts = [[line.strip() for line in text_value(item).splitlines() if line.strip()] for item in self.items]
        await self.preview.corrected(interaction, self.index, role_ids, texts)


# Transactions SHM automatiques : le bot lit lui-même l'historique des transactions de la ligue sur SHM.

SHM_NOT_CONFIGURED = (
    "⚠️ Le compte SHM du bot n'est pas encore configuré.\n"
    "La personne qui s'occupe du bot doit ajouter `SHM_USERNAME` et `SHM_PASSWORD` (le compte SHM du bot) "
    "dans le fichier `.env` ou dans les variables Railway, puis redémarrer le bot.")


class ShmModal(BotModal):
    def __init__(self, bot: DraftBot, guild_id: int):
        super().__init__(bot, "Annonces SHM automatiques")
        slug, channels = bot.store.shm_league(guild_id), bot.store.shm_channels(guild_id)
        self.address = self.ask(
            "Adresse de la ligue sur SHM",
            discord.ui.TextInput(default=f"{shm.BASE_URL}/{slug}" if slug else None, required=False,
                                 max_length=200, placeholder=f"{shm.BASE_URL}/ma_ligue"),
            "Copie l'adresse de ta ligue sur SHM. Laisse vide pour arrêter les annonces automatiques.")

        def select(kind: str, required: bool) -> discord.ui.ChannelSelect:
            channel_id = channels[kind]
            default = [discord.Object(id=channel_id, type=discord.TextChannel)] if channel_id else []
            return discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], default_values=default,
                                            min_values=1 if required else 0, required=required)
        self.channel = self.ask("Salon des transactions", select("trades", True),
                                "Chaque nouvelle transaction y sera annoncée.")
        self.waivers = self.ask("Salon du ballottage", select("waivers", False),
                                "Laisse vide pour ne pas annoncer le ballottage.")
        self.buyouts = self.ask("Salon des rachats", select("buyouts", False),
                                "Laisse vide pour ne pas annoncer les rachats.")
        self.standings = self.ask("Salon du classement", select("standings", False),
                                  "Le classement y est publié et mis à jour tout seul. Laisse vide si tu n'en veux pas.")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def chosen(select: discord.ui.ChannelSelect) -> discord.TextChannel | None | bool:
            if not select.values:
                return None
            # False : un salon choisi que le bot ne voit pas.
            return interaction.guild.get_channel(select.values[0].id) or False
        await setup_shm(self.bot, interaction, text_value(self.address), chosen(self.channel),
                        {"waivers": chosen(self.waivers), "buyouts": chosen(self.buyouts),
                         "standings": chosen(self.standings)})


SHM_KIND_NAMES = {"trades": "transactions", "waivers": "ballottage", "buyouts": "rachats", "standings": "classement"}


async def setup_shm(bot: DraftBot, interaction: discord.Interaction, address: str,
                    channel: discord.TextChannel | None, others: dict | None = None) -> None:
    guild = interaction.guild
    if not address.strip():
        bot.store.set_shm_league(guild.id, None)
        bot.shm_status.pop(guild.id, None)
        bot.store.add_log(guild.id, interaction.user.id, "transactions SHM", "arrêtées")
        await reply(interaction, "🔕 Les annonces SHM automatiques sont arrêtées.")
        await bot.refresh_panels(guild)
        return
    if bot.shm is None:
        await reply(interaction, SHM_NOT_CONFIGURED)
        return
    try:
        slug = shm.league_slug(address)
    except shm.ShmError as error:
        await reply(interaction, f"❌ {error}")
        return
    others = others if others is not None else {"waivers": channel, "buyouts": channel, "standings": None}
    chosen = {"trades": channel, **others}
    for kind, target in chosen.items():
        if target is False or (kind == "trades" and target is None):
            await reply(interaction, f"Salon des {SHM_KIND_NAMES[kind]} introuvable. Le bot voit-il ce salon ? "
                        "Réessaie dans `/admin`.")
            return
        if target is not None and not bot_can_post(target, guild):
            await reply(interaction, f"🔒 Le bot ne peut pas écrire dans {target.mention} "
                        f"({SHM_KIND_NAMES[kind]}). Corrige ses permissions ou choisis un autre salon.")
            return
    await interaction.response.defer(ephemeral=True, thinking=True)
    # Un essai tout de suite : l'admin sait aussitôt si le compte du bot voit bien la ligue.
    try:
        page = await bot.shm.history(slug)
    except shm.ShmError as error:
        await reply(interaction, f"❌ {error}\nRien n'a été changé.")
        return
    # Le ballottage et les rachats sont retenus tout de suite aussi : seuls les nouveaux seront annoncés.
    try:
        waivers = await bot.shm.waivers(slug)
    except shm.ShmError as error:
        log.warning("Ballottage SHM illisible pour la ligue %s : %s", slug, error)
        waivers = None
    feed, _ = await bot.read_feed(slug)
    standings, standings_error = None, None
    if chosen["standings"] is not None:
        try:
            standings = await bot.shm.standings(slug)
        except shm.ShmError as error:
            standings_error = str(error)
    bot.store.set_shm_channels(guild.id, channel.id, *(chosen[k].id if chosen[k] else None
                                                       for k in ("waivers", "buyouts", "standings")))
    bot.store.set_shm_league(guild.id, slug)
    bot.store.add_log(guild.id, interaction.user.id, "transactions SHM", f"ligue {slug} → #{channel.name}")
    posted = await bot.publish_shm(guild.id, channel, page.trades, page.teams)
    if waivers is not None:
        await bot.publish_waivers(guild.id, chosen["waivers"], waivers, {}, False)
    if feed is not None:
        await bot.publish_buyouts(guild.id, chosen["buyouts"], shm.feed_buyouts(feed, bot.shm_farms[slug]),
                                  page.teams)
    if standings is not None:
        await bot.publish_standings(guild.id, chosen["standings"], standings)
    await bot.shm_ok(guild.id, channel)
    count = len(page.trades)
    lines = [f"✅ **C'est branché!** Le bot suit la ligue `{slug}` sur SHM."]
    if posted:
        lines.append(f"{posted} nouvelle{'s' if posted > 1 else ''} transaction{'s' if posted > 1 else ''} "
                     f"annoncée{'s' if posted > 1 else ''} dans {channel.mention}.")
    elif count:
        lines.append(f"Les {count} transactions déjà sur SHM ne sont pas réannoncées.")
    for kind in ("trades", "waivers", "buyouts", "standings"):
        target = chosen[kind]
        lines.append(f"• {SHM_KIND_NAMES[kind].capitalize()} : " + (target.mention if target else "pas annoncé"))
    if standings_error:
        lines.append(f"⚠️ Classement pas encore publié : {standings_error} Je réessaie tout seul.")
    lines.append(f"Ce qui arrive sur SHM est annoncé moins de {SHM_CHECK_MINUTES} minutes plus tard. "
                 "Ce qui y était déjà n'est pas réannoncé.")
    await reply(interaction, "\n".join(lines))
    await bot.refresh_panels(guild)


def shm_text(bot: DraftBot, guild: discord.Guild) -> str:
    slug = bot.store.shm_league(guild.id)
    if not slug:
        return "désactivées"
    text = f"ligue `{slug}`"
    status = bot.shm_status.get(guild.id)
    if status and status.error:
        text += f"\n　⚠️ {status.error}"
    elif status and status.ok_at:
        minutes = int((time.time() - status.ok_at) // 60)
        text += " (✅ lu " + ("à l'instant" if minutes < 1 else f"il y a {minutes} min") + ")"
    parts = []
    for kind, channel_id in bot.store.shm_channels(guild.id).items():
        channel = guild.get_channel(channel_id) if channel_id else None
        parts.append(f"{SHM_KIND_NAMES[kind].capitalize()} → " + (channel.mention if channel else "non"))
    return text + "\n　" + " · ".join(parts)


# Le panneau admin : un seul bouton pour la prochaine étape, le reste rangé dans « Plus d'options ».

# Les étapes de préparation, dans l'ordre : (clé, nom du bouton, explication).
SETUP_STEPS = [
    ("salons", "Choisir les salons",
     "Choisis le salon de la liste des joueurs et celui des annonces de choix."),
    ("joueurs", "Importer les joueurs",
     "Envoie le fichier des joueurs repêchables : une ligne par joueur, `Nom;Équipe actuelle;Position`."),
    ("ordre", "Définir l'ordre du draft",
     "Écris les équipes dans l'ordre de la 1re ronde et le nombre de rondes."),
]
LAUNCH_STEP = "Lancer le draft"
MORE_OPTIONS = "Plus d'options"
TRADE_BUTTON = "Annoncer une transaction"
CHANGE_CHANNELS = "Changer les salons"
SHM_BUTTON = "Annonces SHM auto"


def missing_setup_step(bot: DraftBot, guild: discord.Guild) -> int | None:
    """L'index de la première étape de préparation pas encore faite, ou None."""
    store = bot.store
    done = [bot.channels(guild) is not None, store.player_count(guild.id) > 0, store.pick_count(guild.id) > 0]
    return next((i for i, ok in enumerate(done) if not ok), None)


def panel_phase(bot: DraftBot, guild: discord.Guild) -> str:
    """Où en est le draft : « préparation », « prêt », « en cours » ou « terminé »."""
    if missing_setup_step(bot, guild) is not None:
        return "préparation"
    if not bot.store.is_started(guild.id):
        return "prêt"
    return "en cours" if bot.store.current_pick(guild.id) is not None else "terminé"


def next_step_hint(bot: DraftBot, guild: discord.Guild) -> str:
    step = missing_setup_step(bot, guild)
    if step is not None:
        return f"\n👉 Prochaine étape : **{SETUP_STEPS[step][1]}** (dans `/admin`)."
    if not bot.store.is_started(guild.id):
        return f"\n👉 Dernière étape : **▶ {LAUNCH_STEP}** (dans `/admin`)."
    return ""


def delay_text(bot: DraftBot, guild: discord.Guild) -> str:
    seconds, admin_role_id = bot.store.timer(guild.id)
    if not seconds:
        return "aucun"
    role = guild.get_role(admin_role_id) if admin_role_id else None
    return f"{fmt.duration_text(seconds)} par choix" + (f", alerte à {role.mention}" if role else "")


def missing_roles_warning(bot: DraftBot, guild: discord.Guild) -> list[str]:
    missing = [t for t in bot.store.order_teams(guild.id) if bot.team_role(guild, t) is None]
    if not missing:
        return []
    return ["⚠️ Aucun rôle Discord pour : " + ", ".join(escape_markdown(t) for t in missing)
            + ". Ces équipes ne pourront pas choisir elles-mêmes."]


def panel_text(bot: DraftBot, guild: discord.Guild, more: bool = False) -> str:
    store = bot.store
    if more:
        channels = bot.channels(guild)
        players = store.player_count(guild.id)
        picks = store.pick_count(guild.id)
        return "\n".join([
            f"⚙️ **{MORE_OPTIONS}**",
            "• Salons : " + (f"liste dans {channels[0].mention}, annonces dans {channels[1].mention}"
                             if channels else "pas encore choisis"),
            "• Joueurs : " + (f"{players}, dont {len(store.available_players(guild.id))} encore disponibles"
                              if players else "aucun"),
            "• Ordre : " + (f"{picks} choix, {store.made_picks_count(guild.id)} déjà faits"
                            if picks else "pas encore défini"),
            f"• Délai : {delay_text(bot, guild)}",
            f"• Annonces SHM automatiques : {shm_text(bot, guild)}",
            "",
            "Clique sur ce que tu veux faire, ou sur **Retour**.",
        ])
    phase = panel_phase(bot, guild)
    if phase == "préparation":
        step = missing_setup_step(bot, guild)
        _, name, explanation = SETUP_STEPS[step]
        return "\n".join([
            f"🛠️ **Préparer le draft : étape {step + 1} sur {len(SETUP_STEPS) + 1}**",
            "",
            f"👉 **{name}**",
            explanation,
            "",
            "Clique sur le bouton bleu ci-dessous.",
        ])
    picks = store.pick_count(guild.id)
    if phase == "prêt":
        channels = bot.channels(guild)
        return "\n".join([
            f"✅ **Tout est prêt!** Dernière étape : {len(SETUP_STEPS) + 1} sur {len(SETUP_STEPS) + 1}.",
            "",
            f"• {picks} choix, {len(store.order_teams(guild.id))} équipes, "
            f"{len(store.available_players(guild.id))} joueurs",
            f"• Délai : {delay_text(bot, guild)}",
            *missing_roles_warning(bot, guild),
            "",
            f"Clique sur **▶ {LAUNCH_STEP}** quand tu veux commencer. "
            f"La première équipe sera avertie dans {channels[1].mention}.",
        ])
    if phase == "en cours":
        return "\n".join([
            f"🏒 **Le draft est en cours** ({store.made_picks_count(guild.id)} choix faits sur {picks})",
            bot.on_the_clock(guild),
            *missing_roles_warning(bot, guild),
        ])
    return "\n".join([
        f"🏁 **Le draft est terminé** ({picks} choix).",
        f"Le journal de toutes les actions est dans **{MORE_OPTIONS}**.",
    ])


class PanelButton(discord.ui.Button):
    def __init__(self, key: str, label: str, action: Callable[[discord.Interaction], Awaitable[None]],
                 style: discord.ButtonStyle, emoji: str | None = None, row: int | None = None):
        super().__init__(label=label, emoji=emoji, style=style, row=row, custom_id=f"admin:{key}")
        self.key, self.action = key, action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.action(interaction)


class AdminPanel(discord.ui.View):
    """Le panneau ne montre que ce qui sert maintenant :
    - préparation : un seul bouton, celui de la prochaine étape ;
    - prêt : « Lancer le draft » ;
    - en cours : choisir pour l'équipe au tour, annuler le dernier choix ;
    - toujours : « Plus d'options », qui range tout le reste (rien n'est perdu).
    """

    def __init__(self, bot: DraftBot, user_id: int, guild_id: int):
        super().__init__(timeout=PANEL_SECONDS)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.origin: discord.Interaction | None = None
        self.more = False

    def button(self, key: str) -> PanelButton | None:
        """Le bouton affiché avec cette clé, ou None s'il n'est pas affiché maintenant."""
        return next((item for item in self.children if isinstance(item, PanelButton) and item.key == key), None)

    def add(self, key: str, label: str, action, style=discord.ButtonStyle.secondary,
            emoji: str | None = None, row: int | None = None) -> None:
        self.add_item(PanelButton(key, label, action, style, emoji, row))

    def build(self, guild: discord.Guild) -> str:
        """Remet les bons boutons pour l'état du draft et retourne le texte du panneau."""
        self.clear_items()
        primary, success, danger = discord.ButtonStyle.primary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        if self.more:
            self.add("salons", CHANGE_CHANNELS, self.open_channels, emoji="📺", row=0)
            self.add("joueurs", "Changer les joueurs", self.open_players, emoji="📋", row=0)
            self.add("ordre", "Changer l'ordre", self.open_order, emoji="🔢", row=0)
            self.add("delai", "Délai par choix", self.open_delay, emoji="⌛", row=1)
            self.add("echange", "Échanger un choix", self.open_trade, emoji="🔁", row=1)
            self.add("journal", "Journal", self.journal, emoji="📒", row=1)
            self.add("shm", SHM_BUTTON, self.open_shm, emoji="🔄", row=1)
            self.add("retour", "Retour", self.back, style=primary, emoji="⬅️", row=2)
            return panel_text(self.bot, guild, more=True)
        phase = panel_phase(self.bot, guild)
        if phase == "préparation":
            key, name, _ = SETUP_STEPS[missing_setup_step(self.bot, guild)]
            action = {"salons": self.open_channels, "joueurs": self.open_players, "ordre": self.open_order}[key]
            self.add(key, name, action, style=primary, emoji="➡️", row=0)
        elif phase == "prêt":
            self.add("lancer", LAUNCH_STEP, self.launch, style=success, emoji="▶️", row=0)
        elif phase == "en cours":
            self.add("choisir", "Choisir pour l'équipe au tour", self.force_pick, style=success, emoji="🏒", row=0)
            self.add("annuler", "Annuler le dernier choix", self.undo, style=danger, emoji="↩️", row=0)
        else:
            # Une erreur au dernier choix doit pouvoir être corrigée, même après la fin.
            self.add("annuler", "Annuler le dernier choix", self.undo, style=danger, emoji="↩️", row=0)
        # Les transactions se font toute la saison, avant, pendant et après le draft.
        self.add("transaction", TRADE_BUTTON, self.open_capture, emoji="📣", row=1)
        self.add("options", MORE_OPTIONS, self.show_more, emoji="⚙️", row=1)
        return panel_text(self.bot, guild)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            return False
        # La permission est revérifiée à chaque clic : elle a pu être retirée depuis l'ouverture.
        if not is_admin(interaction.user):
            await reply(interaction, "🔒 Ce panneau est réservé aux admins (permission « Gérer le serveur »).")
            return False
        return True

    async def refresh(self, guild: discord.Guild) -> None:
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_response(content=self.build(guild), view=self)
        except discord.HTTPException:
            self.bot.forget_panel(self.guild_id, self)

    async def on_timeout(self) -> None:
        self.bot.forget_panel(self.guild_id, self)
        if self.origin:
            try:
                await self.origin.edit_original_response(
                    content="⌛ Panneau fermé après 14 minutes. Refais `/admin` pour le rouvrir.", view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)

    async def show_more(self, interaction: discord.Interaction) -> None:
        self.more = True
        await interaction.response.edit_message(content=self.build(interaction.guild), view=self)

    async def back(self, interaction: discord.Interaction) -> None:
        self.more = False
        await interaction.response.edit_message(content=self.build(interaction.guild), view=self)

    async def open_channels(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChannelsModal(self.bot, self.bot.store.get_channels(interaction.guild_id)))

    async def open_players(self, interaction: discord.Interaction) -> None:
        if not self.bot.channels(interaction.guild):
            await reply(interaction, f"Choisis d'abord les salons : **{MORE_OPTIONS}**, puis **{CHANGE_CHANNELS}**.")
            return
        await interaction.response.send_modal(PlayersModal(self.bot))

    async def open_order(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OrderModal(self.bot))

    async def open_delay(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DelayModal(self.bot, *self.bot.store.timer(interaction.guild_id)))

    async def open_trade(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TradeModal(self.bot))

    async def journal(self, interaction: discord.Interaction) -> None:
        await send_journal(self.bot, interaction)

    async def launch(self, interaction: discord.Interaction) -> None:
        await launch_draft(self.bot, interaction)

    async def force_pick(self, interaction: discord.Interaction) -> None:
        await open_pick(self.bot, interaction, forced=True)

    async def undo(self, interaction: discord.Interaction) -> None:
        await undo_last_pick(self.bot, interaction)

    async def open_shm(self, interaction: discord.Interaction) -> None:
        if self.bot.shm is None:
            await reply(interaction, SHM_NOT_CONFIGURED)
            return
        await interaction.response.send_modal(ShmModal(self.bot, interaction.guild_id))

    async def open_capture(self, interaction: discord.Interaction) -> None:
        if not capture_installed():
            await reply(interaction, NOT_INSTALLED)
            return
        await interaction.response.send_modal(CaptureModal(self.bot, self.bot.store.trades_channel(interaction.guild_id)))


HELP_TEXT = """\
🏒 **Comment ça marche**
1. Quand c'est ton tour, le bot mentionne ton équipe dans le salon des annonces.
2. Clique sur le bouton vert **Faire mon choix** sous ce message.
3. Choisis ton joueur (ou tape quelques lettres de son nom), puis clique **Confirmer**.

Autres commandes : `/statut` montre qui est au tour, `/roue` fait un tirage au sort.
Tu préfères taper ? `/draft` suivi du nom du joueur fonctionne aussi."""

ADMIN_HELP = """

🛠️ **Admins** : `/admin` ouvre le panneau du draft. Il montre un seul bouton à la fois : \
clique dessus pour passer à l'étape suivante, jusqu'à **▶ Lancer le draft**. Pendant le draft : \
**Choisir pour l'équipe au tour** et **Annuler le dernier choix**. Tout le reste (échanger un choix, délai, \
journal, changer les salons, les joueurs ou l'ordre) est dans **Plus d'options**."""


def register_commands(bot: DraftBot) -> None:
    tree = bot.tree
    store = bot.store

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if not isinstance(error, app_commands.MissingPermissions):
            log.exception("Erreur dans /%s", interaction.command.name if interaction.command else "?",
                          exc_info=error)
        await reply(interaction, explain_error(error))

    @tree.command(name="admin", description="Ouvrir le panneau admin : préparer et gérer le draft")
    @app_commands.guild_only()
    @admin_only
    async def admin(interaction: discord.Interaction) -> None:
        panel = AdminPanel(bot, interaction.user.id, interaction.guild_id)
        await reply(interaction, panel.build(interaction.guild), view=panel)
        panel.origin = interaction
        bot.register_panel(interaction.guild_id, panel)

    @tree.command(name="aide", description="Comment faire son choix au draft")
    @app_commands.guild_only()
    async def aide(interaction: discord.Interaction) -> None:
        await reply(interaction, HELP_TEXT + (ADMIN_HELP if is_admin(interaction.user) else ""))

    async def player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        players = store.search_available(interaction.guild_id, current)
        return [
            app_commands.Choice(name=" · ".join(x for x in (p.name, p.team, p.position) if x)[:100],
                                value=str(p.id))
            for p in players
        ]

    @tree.command(name="draft", description="Repêcher un joueur (seulement l'équipe dont c'est le tour)")
    @app_commands.describe(joueur="Commence à taper le nom du joueur")
    @app_commands.autocomplete(joueur=player_autocomplete)
    @app_commands.guild_only()
    async def draft(interaction: discord.Interaction, joueur: str) -> None:
        pick = await check_turn(bot, interaction, forced=False)
        if pick is None:
            return
        matches = store.resolve_available(interaction.guild_id, joueur)
        if not matches:
            await reply(interaction, f"Aucun joueur disponible ne correspond à « {escape_markdown(joueur)} ».")
            return
        if len(matches) > 1:
            names = ", ".join(escape_markdown(p.name) for p in matches[:10])
            await reply(interaction, f"Plusieurs joueurs correspondent : {names}. Choisis dans la liste proposée.")
            return
        await confirm_player(bot, interaction, pick, matches[0], forced=False)

    @tree.command(name="statut", description="Voir qui est au tour et les prochains choix")
    @app_commands.guild_only()
    async def statut(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if store.pick_count(guild_id) and not store.is_started(guild_id):
            await reply(interaction, "Le draft n'est pas encore commencé. Les équipes seront averties au début.")
            return
        upcoming = store.upcoming_picks(guild_id, 6)
        if not upcoming:
            await reply(interaction, "Aucun choix en attente.")
            return
        lines = [bot.on_the_clock(interaction.guild), "", "**Ensuite :**"]
        lines += [f"{fmt.pick_label(p)} : {escape_markdown(p.team)}" for p in upcoming[1:]]
        lines.append(f"\n{len(store.available_players(guild_id))} joueurs disponibles.")
        if bot.is_on_the_clock(interaction.user, upcoming[0].team):
            # C'est son tour : le bouton est là, pas besoin d'aller le chercher dans le salon.
            await reply(interaction, "\n".join(lines), view=PickButtonView(bot))
        else:
            await reply(interaction, "\n".join(lines))

    @tree.command(name="roue", description="Tirer au sort une option, chances égales (ex. : signature d'un joueur autonome)")
    @app_commands.describe(choix="Les options, séparées par des virgules (ex. : ott, stl)",
                           titre="Ce qu'on tire au sort (ex. : le nom du joueur)")
    @app_commands.guild_only()
    async def roue(interaction: discord.Interaction, choix: str, titre: str | None = None) -> None:
        try:
            options = wheel.parse_options(choix)
        except wheel.WheelError as error:
            await reply(interaction, f"❌ {error}")
            return
        title = " ".join(titre.split())[:100] if titre and titre.strip() else None
        # Le résultat est tiré et noté au journal avant le suspense : il ne peut plus changer.
        winner = wheel.spin(options)
        store.add_log(interaction.guild_id, interaction.user.id, "roue", wheel.log_details(title, options, winner))
        await interaction.response.send_message(wheel.spinning_message(title, options), allowed_mentions=NO_PINGS)
        await asyncio.sleep(WHEEL_SUSPENSE)
        await interaction.edit_original_response(content=wheel.result_message(title, options, winner),
                                                 allowed_mentions=NO_PINGS)


class ConfigError(Exception):
    """Un réglage manquant ou invalide. Le message s'affiche tel quel au démarrage."""


def resolve_paths(env: dict) -> tuple[Path, Path]:
    """Où ranger la base du draft et ses sauvegardes.

    Sur un PC, tout reste à côté du bot. Sur Railway, tout va dans le volume
    (RAILWAY_VOLUME_MOUNT_PATH), le seul disque qui survit à un redéploiement.
    """
    on_railway = bool(env.get("RAILWAY_ENVIRONMENT") or env.get("RAILWAY_ENVIRONMENT_NAME"))
    data_dir = env.get("DATA_DIR") or env.get("RAILWAY_VOLUME_MOUNT_PATH")
    if on_railway and not env.get("RAILWAY_VOLUME_MOUNT_PATH") and not env.get("DB_PATH"):
        raise ConfigError("Aucun volume Railway n'est attaché : le draft serait effacé à chaque redéploiement. "
                          "Ajoute un volume monté sur /data au service, puis redéploie.")
    if env.get("DB_PATH"):
        db_path = Path(env["DB_PATH"])
    else:
        db_path = Path(data_dir) / "draft.db" if data_dir else Path("draft.db")
    backup_dir = Path(env.get("BACKUP_DIR") or db_path.resolve().parent / "sauvegardes")
    return db_path, backup_dir


def warn_old_guild_ids(env: dict) -> None:
    """GUILD_IDS ne sert plus : le bot marche sur tout serveur où il est invité."""
    if (env.get("GUILD_IDS") or env.get("GUILD_ID") or "").strip():
        log.info("GUILD_IDS n'est plus nécessaire : le bot marche sur tous les serveurs où il est invité.")


def open_store(db_path: Path, wait_seconds: int = 60) -> Store:
    """Ouvre la base. Pendant un redéploiement, attend que l'ancienne copie du bot s'arrête."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return Store(str(db_path), exclusive=True)
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise ConfigError(f"Une autre copie du bot utilise déjà {db_path}. Arrête-la avant d'en lancer une autre.")
            log.warning("La base est utilisée par une autre copie du bot, nouvel essai dans 2 secondes.")
            time.sleep(2)


def check_library() -> None:
    """Le panneau admin a besoin des fenêtres avec menus et envoi de fichier (discord.py 2.7)."""
    if not all(hasattr(discord.ui, name) for name in ("Label", "FileUpload")):
        raise ConfigError(f"discord.py {discord.__version__} est trop vieux pour cette version du bot. "
                          "Lance : pip install -U -r requirements.txt")


def shm_client_from_env(env: dict) -> shm.ShmClient | None:
    """Le compte SHM du bot, pour annoncer les transactions automatiquement. Facultatif."""
    username = (env.get("SHM_USERNAME") or "").strip()
    password = env.get("SHM_PASSWORD") or ""
    if not username and not password:
        log.info("Pas de compte SHM (SHM_USERNAME, SHM_PASSWORD) : transactions SHM automatiques désactivées.")
        return None
    if not username or not password:
        raise ConfigError("Il faut SHM_USERNAME et SHM_PASSWORD ensemble dans .env (le compte SHM du bot), "
                          "ou aucun des deux.")
    return shm.ShmClient(username, password)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    try:
        check_library()
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("DISCORD_TOKEN est vide : colle le jeton du bot dans .env (ou dans les variables Railway).")
        warn_old_guild_ids(os.environ)
        db_path, backup_dir = resolve_paths(os.environ)
        log.info("Base du draft : %s · sauvegardes : %s", db_path.resolve(), backup_dir)
        shm_client = shm_client_from_env(os.environ)
        store = open_store(db_path)
    except ConfigError as error:
        log.error("%s", error)
        raise SystemExit(1)
    DraftBot(store, backup_dir, shm_client).run(token)


if __name__ == "__main__":
    main()

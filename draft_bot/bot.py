"""Bot Discord qui gère le draft d'une ligue de hockey simulée."""

import asyncio
import csv
import io
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.utils import escape_markdown
from dotenv import load_dotenv

from . import formatting as fmt
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


class DraftBot(discord.Client):
    def __init__(self, store: Store, guild_ids: list[int], backup_dir: Path):
        super().__init__(intents=discord.Intents.default())
        self.store = store
        self.guild_ids = guild_ids
        self.backup_dir = backup_dir
        self.tree = app_commands.CommandTree(self)
        # Un verrou par serveur : deux commandes ne modifient jamais le même draft en même temps.
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.stale_cleared = False
        # Les panneaux /admin ouverts, par serveur, pour les mettre à jour après chaque changement.
        self.panels: defaultdict[int, set[AdminPanel]] = defaultdict(set)
        register_commands(self)

    async def setup_hook(self) -> None:
        # Les boutons « Faire mon choix » déjà publiés répondent dès le démarrage.
        self.add_view(PickButtonView(self))
        await self.sync_commands()
        self.backup("demarrage")
        self.check_timers.start()
        self.hourly_backup.start()

    async def sync_commands(self) -> None:
        """Publie les commandes d'une seule façon à la fois, pour éviter les doublons.

        Avec GUILD_IDS, les commandes sont publiées sur chaque serveur (visibles tout de suite)
        et la liste globale est vidée : sinon, les commandes globales d'une ancienne version
        s'affichent en double à côté des nouvelles.
        """
        if not self.guild_ids:
            await self.tree.sync()
            return
        for guild_id in self.guild_ids:
            self.tree.copy_global_to(guild=discord.Object(id=guild_id))
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        for guild_id in self.guild_ids:
            try:
                await self.tree.sync(guild=discord.Object(id=guild_id))
            except discord.Forbidden:
                log.warning("Le bot n'est pas encore sur le serveur %s : commandes non publiées.", guild_id)

    async def clear_stale_guild_commands(self) -> None:
        """Sans GUILD_IDS, les commandes sont globales : on retire celles qu'une ancienne
        version aurait publiées serveur par serveur."""
        if self.guild_ids or self.stale_cleared:
            return
        self.stale_cleared = True
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except discord.HTTPException:
                log.warning("Impossible de nettoyer les commandes du serveur %s.", guild.id)

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s sur %d serveur(s)", self.user, len(self.guilds))
        await self.clear_stale_guild_commands()
        if not self.guild_ids:
            log.warning("GUILD_IDS est vide : le bot accepte n'importe quel serveur.")
        for guild in list(self.guilds):
            await self.leave_if_not_allowed(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.leave_if_not_allowed(guild)

    async def leave_if_not_allowed(self, guild: discord.Guild) -> None:
        """Quitte tout serveur absent de GUILD_IDS. Sans liste, tous les serveurs sont acceptés."""
        if self.guild_ids and guild.id not in self.guild_ids:
            log.warning("Serveur non autorisé, le bot le quitte : %s (%s)", guild.name, guild.id)
            await guild.leave()

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

    async def announce(self, guild: discord.Guild, text: str, with_clock: bool = True) -> str:
        """Publie un message dans le salon des sélections, en mentionnant l'équipe au tour.

        Retourne une phrase à ajouter à la réponse privée de l'admin.
        """
        channels = self.channels(guild)
        if channels is None:
            return "\n⚠️ Rien n'a été annoncé : les salons du draft sont introuvables (`/admin`, puis **1. Salons**)."
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

    async def refresh_panels(self, guild: discord.Guild) -> None:
        """Après un changement, les panneaux admin ouverts montrent tout de suite les nouveaux ✅."""
        for panel in list(self.panels[guild.id]):
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
    pick = bot.store.current_pick(interaction.guild_id)
    if pick is None:
        await reply(interaction, "Aucun choix en attente : l'ordre n'est pas encore défini, ou le draft est terminé.")
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
            problems.append("les salons du draft sont introuvables (`/admin`, puis **1. Salons**)")
        else:
            role = bot.team_role(interaction.guild, pick.team)
            text = fmt.announcement(pick, player, forced, role.name if role else None)
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
    await bot.refresh_panels(interaction.guild)
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
        super().__init__(bot, "1. Salons du draft")
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
        super().__init__(bot, "2. Joueurs repêchables")
        self.file = self.ask("Fichier des joueurs", discord.ui.FileUpload(max_values=1),
                             "Une ligne par joueur : Nom;Équipe actuelle;Position (CSV ou texte).")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await import_players(self.bot, interaction, self.file.values[0])


class OrderModal(BotModal):
    def __init__(self, bot: DraftBot):
        super().__init__(bot, "3. Ordre du draft")
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
        super().__init__(bot, "4. Délai par choix")
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
        await reply(interaction, "Salon introuvable. Le bot voit-il ce salon ? Réessaie avec **1. Salons**.")
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
    await bot.refresh_panels(interaction.guild)


async def import_players(bot: DraftBot, interaction: discord.Interaction, fichier: discord.Attachment) -> None:
    store = bot.store
    guild_id = interaction.guild_id
    if not bot.channels(interaction.guild):
        await reply(interaction, "Fais d'abord l'étape **1. Salons**.")
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
            await reply(button_interaction, "Les salons du draft ne sont plus accessibles. Refais **1. Salons**.")
            return
        async with bot.locks[guild_id]:
            bot.backup("avant-liste")
            store.replace_players(guild_id, rows)
            store.add_log(guild_id, interaction.user.id, "liste", f"{len(rows)} joueurs importés")
            await bot.refresh_list(guild_id, channels[0], repost=True)
        await reply(button_interaction, f"✅ {len(rows)} joueurs publiés dans {channels[0].mention}."
                    + next_step_hint(bot, interaction.guild))
        await bot.refresh_panels(interaction.guild)

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
        await reply(interaction, "Le nombre de rondes doit être un nombre entre 1 et 50. Réessaie avec **3. Ordre**.")
        return
    first_round = fmt.parse_order(teams_text)
    if not first_round:
        await reply(interaction, "Aucune équipe trouvée. Écris une équipe par ligne. Réessaie avec **3. Ordre**.")
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
            announced = await bot.announce(interaction.guild, "🏒 **Le draft commence!**")
        await reply(button_interaction, f"✅ {len(order)} choix, {per_round} équipes par ronde." + announced)
        await bot.refresh_panels(interaction.guild)

    text = f"Définir un ordre de **{len(order)} choix** pour {len(teams)} équipes : {', '.join(teams)} ?"
    if missing:
        text += ("\n⚠️ Aucun rôle Discord ne porte le nom de : " + ", ".join(missing)
                 + ". Ces équipes ne pourront pas faire leur choix elles-mêmes. "
                 "Vérifie l'orthographe ou crée le rôle.")
    await ask_confirmation(interaction, text + reset_warning(store, guild_id), confirmed)


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
    await bot.refresh_panels(interaction.guild)


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
        await bot.refresh_panels(interaction.guild)

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
        await bot.refresh_panels(interaction.guild)

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


# Le panneau admin : l'état du draft en ✅ / ❌, et un bouton par action.

def setup_steps(bot: DraftBot, guild: discord.Guild) -> list[tuple[bool, str, str]]:
    """Les étapes de préparation : (faite, nom du bouton, détail)."""
    store = bot.store
    channels = bot.channels(guild)
    players = store.player_count(guild.id)
    picks = store.pick_count(guild.id)
    steps = [
        (channels is not None, "1. Salons",
         f"liste dans {channels[0].mention}, annonces dans {channels[1].mention}" if channels
         else "pas encore choisis"),
        (players > 0, "2. Joueurs",
         f"{players} joueurs, {len(store.available_players(guild.id))} encore disponibles" if players
         else "aucun joueur importé"),
    ]
    if picks:
        detail = f"{picks} choix, {store.made_picks_count(guild.id)} déjà faits"
        missing = [t for t in store.order_teams(guild.id) if bot.team_role(guild, t) is None]
        if missing:
            detail += "\n   ⚠️ Aucun rôle Discord pour : " + ", ".join(escape_markdown(t) for t in missing)
        steps.append((True, "3. Ordre", detail))
    else:
        steps.append((False, "3. Ordre", "pas encore défini"))
    return steps


def next_step_hint(bot: DraftBot, guild: discord.Guild) -> str:
    for done, name, _ in setup_steps(bot, guild):
        if not done:
            return f"\n👉 Prochaine étape : **{name}** (dans `/admin`)."
    return ""


def panel_text(bot: DraftBot, guild: discord.Guild) -> str:
    store = bot.store
    lines = ["🛠️ **Panneau admin du draft**", "", "**Préparer le draft** (dans l'ordre, un bouton par étape)"]
    steps = setup_steps(bot, guild)
    lines += [f"{'✅' if done else '❌'} **{name}** : {detail}" for done, name, detail in steps]
    seconds, admin_role_id = store.timer(guild.id)
    if seconds:
        role = guild.get_role(admin_role_id) if admin_role_id else None
        detail = f"{fmt.duration_text(seconds)} par choix" + (f", alerte à {role.mention}" if role else "")
    else:
        detail = "aucun délai"
    lines.append(f"➖ **4. Délai** (facultatif) : {detail}")
    lines.append("")
    todo = next((name for done, name, _ in steps if not done), None)
    if todo:
        lines.append(f"👉 Prochaine étape : clique sur **{todo}**.")
    elif store.current_pick(guild.id) is None:
        lines.append("🏁 Le draft est terminé. Le **Journal** contient tous les choix.")
    else:
        lines.append("🏒 Tout est prêt, le draft est en cours.")
        lines.append(bot.on_the_clock(guild))
    return "\n".join(lines)


class AdminPanel(discord.ui.View):
    def __init__(self, bot: DraftBot, user_id: int, guild_id: int):
        super().__init__(timeout=PANEL_SECONDS)
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.origin: discord.Interaction | None = None

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
            await self.origin.edit_original_response(content=panel_text(self.bot, guild), view=self)
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

    @discord.ui.button(label="1. Salons", style=discord.ButtonStyle.primary, row=0)
    async def channels(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ChannelsModal(self.bot, self.bot.store.get_channels(interaction.guild_id)))

    @discord.ui.button(label="2. Joueurs", style=discord.ButtonStyle.primary, row=0)
    async def players(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.bot.channels(interaction.guild):
            await reply(interaction, "Fais d'abord l'étape **1. Salons**.")
            return
        await interaction.response.send_modal(PlayersModal(self.bot))

    @discord.ui.button(label="3. Ordre", style=discord.ButtonStyle.primary, row=0)
    async def order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(OrderModal(self.bot))

    @discord.ui.button(label="4. Délai", style=discord.ButtonStyle.primary, row=0)
    async def delay(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(DelayModal(self.bot, *self.bot.store.timer(interaction.guild_id)))

    @discord.ui.button(label="Choisir pour l'équipe au tour", emoji="🏒", style=discord.ButtonStyle.success, row=1)
    async def force_pick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_pick(self.bot, interaction, forced=True)

    @discord.ui.button(label="Annuler le dernier choix", emoji="↩️", style=discord.ButtonStyle.danger, row=1)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await undo_last_pick(self.bot, interaction)

    @discord.ui.button(label="Échanger un choix", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def trade(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TradeModal(self.bot))

    @discord.ui.button(label="Journal", emoji="📒", style=discord.ButtonStyle.secondary, row=2)
    async def journal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await send_journal(self.bot, interaction)

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=panel_text(self.bot, interaction.guild), view=self)


HELP_TEXT = """\
🏒 **Comment ça marche**
1. Quand c'est ton tour, le bot mentionne ton équipe dans le salon des annonces.
2. Clique sur le bouton vert **Faire mon choix** sous ce message.
3. Choisis ton joueur (ou tape quelques lettres de son nom), puis clique **Confirmer**.

Autres commandes : `/statut` montre qui est au tour, `/roue` fait un tirage au sort.
Tu préfères taper ? `/draft` suivi du nom du joueur fonctionne aussi."""

ADMIN_HELP = """

🛠️ **Admins** : `/admin` ouvre le panneau du draft. Suis les étapes 1, 2, 3 (et 4 si tu veux un délai). \
Pendant le draft, le panneau permet de choisir pour une équipe, d'annuler le dernier choix, d'échanger un choix \
et de télécharger le journal."""


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
        await reply(interaction, panel_text(bot, interaction.guild), view=panel)
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


def parse_guild_ids(env: dict) -> list[int]:
    raw = (env.get("GUILD_IDS") or env.get("GUILD_ID") or "").replace(" ", "")
    try:
        return [int(x) for x in raw.split(",") if x]
    except ValueError:
        raise ConfigError(f"GUILD_IDS doit contenir des identifiants de serveur séparés par des virgules, pas « {raw} ».")


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


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    try:
        check_library()
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("DISCORD_TOKEN est vide : colle le jeton du bot dans .env (ou dans les variables Railway).")
        guild_ids = parse_guild_ids(os.environ)
        db_path, backup_dir = resolve_paths(os.environ)
        log.info("Base du draft : %s · sauvegardes : %s", db_path.resolve(), backup_dir)
        store = open_store(db_path)
    except ConfigError as error:
        log.error("%s", error)
        raise SystemExit(1)
    DraftBot(store, guild_ids, backup_dir).run(token)


if __name__ == "__main__":
    main()

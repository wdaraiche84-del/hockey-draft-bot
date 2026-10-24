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
from .storage import AlreadyRunning, PickError, Store, normalize, team_display

log = logging.getLogger("draft_bot")

PINGS = discord.AllowedMentions(users=True, roles=True, everyone=False)
NO_PINGS = discord.AllowedMentions.none()

# Secondes de suspense avant d'afficher le résultat de la roue.
WHEEL_SUSPENSE = 3
# Temps d'affichage du « ✅ repêché » avant qu'il s'efface.
PICK_REPLY_SECONDS = 8

# Nombre de sauvegardes horaires gardées (deux jours).
BACKUPS_KEPT = 48


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
        register_commands(self)

    async def setup_hook(self) -> None:
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

    def on_the_clock(self, guild: discord.Guild) -> str:
        pick = self.store.current_pick(guild.id)
        if pick is None:
            return fmt.on_the_clock(None, "")
        role = self.team_role(guild, pick.team)
        ping = role.mention if role else "(aucun rôle à ce nom)"
        return fmt.on_the_clock(pick, ping, self.store.deadline(guild.id))

    async def announce(self, guild: discord.Guild, text: str, with_clock: bool = True) -> str:
        """Publie un message dans le salon des sélections, en mentionnant l'équipe au tour.

        Retourne une phrase à ajouter à la réponse privée de l'admin.
        """
        channels = self.channels(guild)
        if channels is None:
            return "\n⚠️ Rien n'a été annoncé : les salons du draft sont introuvables (refais `/salons`)."
        if with_clock:
            text += "\n" + self.on_the_clock(guild)
        try:
            await channels[1].send(text, allowed_mentions=PINGS)
        except discord.HTTPException:
            log.exception("Annonce impossible sur le serveur %s", guild.id)
            return f"\n⚠️ L'annonce dans {channels[1].mention} a échoué. Vérifie les permissions du bot."
        return f"\n📣 Annoncé dans {channels[1].mention}."

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
        log.exception("Erreur après une confirmation", exc_info=error)
        await reply(interaction, explain_error(error))


async def ask_confirmation(interaction: discord.Interaction, question: str,
                           on_confirm: Callable[[discord.Interaction], Awaitable[None]]) -> None:
    view = Confirm(interaction.user.id, on_confirm)
    view.origin = interaction
    await reply(interaction, question, view=view)


async def read_attachment(attachment: discord.Attachment) -> str:
    return fmt.decode(await attachment.read())


def admin_only(func):
    # default_permissions cache la commande aux non-admins, mais un serveur peut changer
    # ce réglage : has_permissions revérifie la permission à chaque utilisation.
    func = app_commands.checks.has_permissions(manage_guild=True)(func)
    return app_commands.default_permissions(manage_guild=True)(func)


def register_commands(bot: DraftBot) -> None:
    tree = bot.tree
    store = bot.store

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if not isinstance(error, app_commands.MissingPermissions):
            log.exception("Erreur dans /%s", interaction.command.name if interaction.command else "?",
                          exc_info=error)
        await reply(interaction, explain_error(error))

    def reset_warning(guild_id: int) -> str:
        made = store.made_picks_count(guild_id)
        if not made:
            return ""
        return f"\n⚠️ **{made} choix déjà faits seront effacés.** Une sauvegarde sera faite avant."

    @tree.command(name="salons", description="Choisir le salon de la liste et le salon des sélections")
    @app_commands.describe(liste="Salon où le bot affiche les joueurs disponibles",
                           selections="Salon où le bot annonce chaque choix")
    @app_commands.guild_only()
    @admin_only
    async def draft_salons(interaction: discord.Interaction, liste: discord.TextChannel,
                           selections: discord.TextChannel) -> None:
        if liste.id == selections.id:
            await reply(interaction, "Choisis deux salons différents : un pour la liste, un pour les sélections.")
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
        await reply(interaction, f"✅ Liste dans {liste.mention}, sélections dans {selections.mention}.")

    @tree.command(name="joueurs", description="Importer les joueurs repêchables et publier la liste")
    @app_commands.describe(fichier="Fichier CSV ou texte : nom, équipe actuelle, position (une ligne par joueur)")
    @app_commands.guild_only()
    @admin_only
    async def draft_liste(interaction: discord.Interaction, fichier: discord.Attachment) -> None:
        guild_id = interaction.guild_id
        if not bot.channels(interaction.guild):
            await reply(interaction, "Configure d'abord les salons avec `/salons`.")
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
                await reply(button_interaction, "Les salons du draft ne sont plus accessibles. Refais `/salons`.")
                return
            async with bot.locks[guild_id]:
                bot.backup("avant-liste")
                store.replace_players(guild_id, rows)
                store.add_log(guild_id, interaction.user.id, "liste", f"{len(rows)} joueurs importés")
                await bot.refresh_list(guild_id, channels[0], repost=True)
            await reply(button_interaction, f"✅ {len(rows)} joueurs publiés dans {channels[0].mention}.")

        text = f"Importer **{len(rows)} joueurs** ? Les premiers :\n{preview}"
        if duplicates:
            text += "\n⚠️ Noms en double : " + ", ".join(duplicates)
        await ask_confirmation(interaction, text + reset_warning(guild_id), confirmed)

    @tree.command(name="ordre", description="Définir l'ordre de sélection (remet le draft à zéro)")
    @app_commands.describe(
        equipes="Ordre de la 1re ronde, séparé par des virgules (ex. : MTL, TOR, BOS)",
        rondes="Nombre de rondes",
        serpentin="Inverser l'ordre à chaque ronde",
        fichier="Ou un fichier avec l'ordre complet, une équipe par ligne (pour les choix échangés)",
    )
    @app_commands.guild_only()
    @admin_only
    async def draft_ordre(interaction: discord.Interaction, equipes: str | None = None,
                          rondes: app_commands.Range[int, 1, 50] = 1, serpentin: bool = False,
                          fichier: discord.Attachment | None = None) -> None:
        guild_id = interaction.guild_id
        if fichier:
            order = fmt.parse_order(await read_attachment(fichier))
        elif equipes:
            order = fmt.build_order(fmt.parse_order(equipes), rondes, serpentin)
        else:
            await reply(interaction, "Donne les équipes (ex. : `MTL, TOR, BOS`) ou un fichier d'ordre.")
            return
        if not order:
            await reply(interaction, "Aucune équipe trouvée.")
            return
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

        text = f"Définir un ordre de **{len(order)} choix** pour {len(teams)} équipes : {', '.join(teams)} ?"
        if missing:
            text += ("\n⚠️ Aucun rôle Discord ne porte le nom de : " + ", ".join(missing)
                     + ". Ces équipes ne pourront pas faire `/draft` elles-mêmes.")
        await ask_confirmation(interaction, text + reset_warning(guild_id), confirmed)

    @tree.command(name="delai", description="Régler le délai pour chaque choix (0 pour aucun délai)")
    @app_commands.describe(
        delai="Par exemple 8h, 90m ou 1h30. Mets 0 pour enlever le délai.",
        admin="Rôle à mentionner quand le temps est écoulé",
    )
    @app_commands.guild_only()
    @admin_only
    async def draft_temps(interaction: discord.Interaction, delai: str, admin: discord.Role | None = None) -> None:
        seconds = fmt.parse_duration(delai)
        if seconds is None:
            await reply(interaction, "Délai non reconnu. Écris par exemple `8h`, `90m` ou `1h30`.")
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
            await reply(interaction, "✅ Aucun délai par choix." + announced)
            return
        text = f"✅ Délai de **{fmt.duration_text(seconds)}** par choix. Le chrono du choix en cours repart à zéro."
        text += f"\n{admin.mention} sera mentionné quand le temps est écoulé." if admin else (
            "\n⚠️ Aucun rôle admin choisi : l'alerte ne mentionnera personne.")
        await reply(interaction, text + announced)

    @tree.command(name="echange", description="Donner un choix à une autre équipe")
    @app_commands.describe(numero="Numéro du choix (voir /statut)", equipe="Nouvelle équipe propriétaire")
    @app_commands.guild_only()
    @admin_only
    async def draft_echange(interaction: discord.Interaction, numero: int, equipe: str) -> None:
        guild_id = interaction.guild_id
        pick = store.get_pick(guild_id, numero)
        if pick is None or pick.player_id is not None:
            await reply(interaction, f"Le choix #{numero} n'existe pas ou a déjà été fait.")
            return
        warning = ""
        if bot.team_role(interaction.guild, equipe) is None:
            warning = f"\n⚠️ Aucun rôle Discord ne porte le nom **{team_display(equipe)}**."

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

        await ask_confirmation(
            interaction,
            f"Donner le **{fmt.pick_label(pick)}** de **{pick.team}** à **{team_display(equipe)}** ?" + warning,
            confirmed,
        )

    async def start_pick(interaction: discord.Interaction, joueur: str, forced: bool) -> None:
        guild_id = interaction.guild_id
        if not bot.channels(interaction.guild):
            await reply(interaction, "Le draft n'est pas configuré. Un admin doit faire `/salons`.")
            return
        pick = store.current_pick(guild_id)
        if pick is None:
            await reply(interaction, "Aucun choix en attente : l'ordre n'est pas défini ou le draft est terminé.")
            return
        if not forced and not is_on_the_clock(interaction.user, pick.team):
            await reply(interaction, "Ce n'est pas ton tour.\n" + bot.on_the_clock(interaction.guild))
            return
        matches = store.resolve_available(guild_id, joueur)
        if not matches:
            await reply(interaction, f"Aucun joueur disponible ne correspond à « {joueur} ».")
            return
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches[:10])
            await reply(interaction, f"Plusieurs joueurs correspondent : {names}. Choisis dans la liste proposée.")
            return
        player = matches[0]

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await finish_pick(button_interaction, pick.number, player.id, forced)

        await ask_confirmation(
            interaction,
            f"**{pick.team}** repêche {fmt.player_line(player)} ({fmt.pick_label(pick)}) ?",
            confirmed,
        )

    def is_on_the_clock(member: discord.Member, team: str) -> bool:
        role = bot.team_role(member.guild, team)
        return role is not None and role in member.roles

    async def finish_pick(interaction: discord.Interaction, number: int, player_id: int, forced: bool) -> None:
        guild_id = interaction.guild_id
        problems = []
        async with bot.locks[guild_id]:
            current = store.current_pick(guild_id)
            # Revérifié au moment du clic : le rôle a pu être retiré entre-temps.
            if not forced and current and not is_on_the_clock(interaction.user, current.team):
                await reply(interaction, "Ce n'est plus ton tour. Rien n'a été enregistré.")
                return
            try:
                pick, player = store.make_pick(guild_id, number, player_id, interaction.user.id, forced)
            except PickError as error:
                await reply(interaction, f"❌ {error}")
                return
            channels = bot.channels(interaction.guild)
            if channels is None:
                problems.append("les salons du draft sont introuvables (refais `/salons`)")
            else:
                role = bot.team_role(interaction.guild, pick.team)
                text = (fmt.announcement(pick, player, forced, role.name if role else None)
                        + "\n" + bot.on_the_clock(interaction.guild))
                try:
                    message = await channels[1].send(text, allowed_mentions=PINGS)
                    store.set_pick_message(guild_id, pick.number, message.id)
                except discord.HTTPException as error:
                    log.exception("Annonce impossible", exc_info=error)
                    problems.append(f"l'annonce dans {channels[1].mention} a échoué")
                try:
                    await bot.refresh_list(guild_id, channels[0])
                except discord.HTTPException as error:
                    log.exception("Liste impossible à mettre à jour", exc_info=error)
                    problems.append(f"la liste dans {channels[0].mention} n'a pas pu être mise à jour")
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
        await start_pick(interaction, joueur, forced=False)

    @tree.command(name="forcedraft", description="Repêcher un joueur pour l'équipe au tour (admin)")
    @app_commands.describe(joueur="Commence à taper le nom du joueur")
    @app_commands.autocomplete(joueur=player_autocomplete)
    @app_commands.guild_only()
    @admin_only
    async def forcedraft(interaction: discord.Interaction, joueur: str) -> None:
        await start_pick(interaction, joueur, forced=True)

    @tree.command(name="annuler-choix", description="Annuler le dernier choix (admin)")
    @app_commands.guild_only()
    @admin_only
    async def draft_annuler(interaction: discord.Interaction) -> None:
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
                    await channels[1].send(
                        f"↩️ {fmt.pick_label(last)} annulé : **{escape_markdown(player.name)}** "
                        "retourne dans la liste.\n" + bot.on_the_clock(interaction.guild),
                        allowed_mentions=PINGS,
                    )
            await reply(button_interaction, f"✅ {fmt.pick_label(last)} annulé.")

        await ask_confirmation(
            interaction,
            f"Annuler le **{fmt.pick_label(last)}** : **{last.team}** avait pris {fmt.player_line(player)} ?",
            confirmed,
        )

    @tree.command(name="statut", description="Voir qui est au tour et les prochains choix")
    @app_commands.guild_only()
    async def draft_statut(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        upcoming = store.upcoming_picks(guild_id, 6)
        if not upcoming:
            await reply(interaction, "Aucun choix en attente.")
            return
        lines = [bot.on_the_clock(interaction.guild), "", "**Ensuite :**"]
        lines += [f"{fmt.pick_label(p)} : {escape_markdown(p.team)}" for p in upcoming[1:]]
        lines.append(f"\n{len(store.available_players(guild_id))} joueurs disponibles.")
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

    @tree.command(name="journal", description="Télécharger le journal de toutes les actions du draft (admin)")
    @app_commands.guild_only()
    @admin_only
    async def draft_journal(interaction: discord.Interaction) -> None:
        entries = store.log_entries(interaction.guild_id)
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


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    try:
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

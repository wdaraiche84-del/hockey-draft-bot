"""Bot Discord qui gère le draft d'une ligue de hockey simulée."""

import asyncio
import logging
import os
from collections import defaultdict

import discord
from discord import app_commands
from dotenv import load_dotenv

from . import formatting as fmt
from .storage import Store

log = logging.getLogger("draft_bot")

PING_USERS = discord.AllowedMentions(users=True, roles=False, everyone=False)
NO_PINGS = discord.AllowedMentions.none()


class DraftBot(discord.Client):
    def __init__(self, store: Store, guild_id: int | None):
        super().__init__(intents=discord.Intents.default())
        self.store = store
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        register_commands(self)

    async def setup_hook(self) -> None:
        # Avec GUILD_ID, les commandes apparaissent tout de suite sur ce serveur.
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s", self.user)

    def channels(self, guild: discord.Guild) -> tuple[discord.TextChannel, discord.TextChannel] | None:
        ids = self.store.get_channels(guild.id)
        if not ids:
            return None
        list_channel, picks_channel = guild.get_channel(ids[0]), guild.get_channel(ids[1])
        if not list_channel or not picks_channel:
            return None
        return list_channel, picks_channel

    async def refresh_list(self, guild_id: int, channel: discord.TextChannel, repost: bool = False) -> None:
        """Met à jour les messages de la liste en ne modifiant que ceux qui ont changé."""
        old = self.store.list_messages(guild_id)
        if repost:
            for message_id, _ in old:
                await delete_message(channel, message_id)
            old = []
        chunks = fmt.render_list(self.store.available_players(guild_id))
        new = []
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
        self.store.set_list_messages(guild_id, new)


async def delete_message(channel: discord.TextChannel, message_id: int) -> None:
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        pass


async def reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


async def read_attachment(attachment: discord.Attachment) -> str:
    return fmt.decode(await attachment.read())


def register_commands(bot: DraftBot) -> None:
    tree = bot.tree
    store = bot.store
    admin = app_commands.default_permissions(manage_guild=True)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Erreur dans une commande", exc_info=error)
        await reply(interaction, f"❌ Une erreur est survenue : {error}")

    @tree.command(name="draft-salons", description="Choisir le salon de la liste et le salon des sélections")
    @app_commands.describe(liste="Salon où le bot affiche les joueurs disponibles",
                           selections="Salon où le bot annonce chaque choix")
    @app_commands.guild_only()
    @admin
    async def draft_salons(interaction: discord.Interaction, liste: discord.TextChannel,
                           selections: discord.TextChannel) -> None:
        store.set_channels(interaction.guild_id, liste.id, selections.id)
        await reply(interaction, f"✅ Liste dans {liste.mention}, sélections dans {selections.mention}.")

    @tree.command(name="draft-liste", description="Importer les joueurs repêchables et publier la liste")
    @app_commands.describe(fichier="Fichier CSV ou texte : nom, équipe actuelle, position (une ligne par joueur)")
    @app_commands.guild_only()
    @admin
    async def draft_liste(interaction: discord.Interaction, fichier: discord.Attachment) -> None:
        channels = bot.channels(interaction.guild)
        if not channels:
            await reply(interaction, "Configure d'abord les salons avec `/draft-salons`.")
            return
        rows = fmt.parse_players(await read_attachment(fichier))
        if not rows:
            await reply(interaction, "Aucun joueur trouvé dans le fichier.")
            return
        await interaction.response.defer(ephemeral=True)
        async with bot.locks[interaction.guild_id]:
            store.replace_players(interaction.guild_id, rows)
            await bot.refresh_list(interaction.guild_id, channels[0], repost=True)
        await reply(interaction, f"✅ {len(rows)} joueurs publiés dans {channels[0].mention}.")

    @tree.command(name="draft-ordre", description="Définir l'ordre de sélection (remet le draft à zéro)")
    @app_commands.describe(
        equipes="Ordre de la 1re ronde, séparé par des virgules (ex. : MTL, TOR, BOS)",
        rondes="Nombre de rondes",
        serpentin="Inverser l'ordre à chaque ronde",
        fichier="Ou un fichier avec l'ordre complet, une équipe par ligne (pour les choix échangés)",
    )
    @app_commands.guild_only()
    @admin
    async def draft_ordre(interaction: discord.Interaction, equipes: str | None = None,
                          rondes: app_commands.Range[int, 1, 50] = 1, serpentin: bool = False,
                          fichier: discord.Attachment | None = None) -> None:
        if fichier:
            order = fmt.parse_order(await read_attachment(fichier))
        elif equipes:
            order = fmt.build_order(fmt.parse_order(equipes), rondes, serpentin)
        else:
            await reply(interaction, "Donne les équipes ou un fichier d'ordre.")
            return
        if not order:
            await reply(interaction, "Aucune équipe trouvée.")
            return
        async with bot.locks[interaction.guild_id]:
            per_round = store.set_order(interaction.guild_id, order)
            channels = bot.channels(interaction.guild)
            if channels and store.list_messages(interaction.guild_id):
                await bot.refresh_list(interaction.guild_id, channels[0])
        current = store.current_pick(interaction.guild_id)
        missing = sorted({t for t in order if store.gm_for(interaction.guild_id, t) is None})
        text = f"✅ {len(order)} choix, {per_round} équipes par ronde.\n" + fmt.on_the_clock(
            current, store.gm_for(interaction.guild_id, current.team))
        if missing:
            text += "\n⚠️ Équipes sans DG (utilise `/draft-dg`) : " + ", ".join(missing)
        await reply(interaction, text)

    @tree.command(name="draft-dg", description="Associer un DG à une équipe")
    @app_commands.guild_only()
    @admin
    async def draft_dg(interaction: discord.Interaction, equipe: str, membre: discord.Member) -> None:
        store.set_gm(interaction.guild_id, equipe, membre.id)
        await reply(interaction, f"✅ {membre.mention} est le DG de **{equipe}**.")

    @tree.command(name="draft-echange", description="Donner un choix à une autre équipe")
    @app_commands.describe(numero="Numéro du choix (ex. : 12)", equipe="Nouvelle équipe propriétaire")
    @app_commands.guild_only()
    @admin
    async def draft_echange(interaction: discord.Interaction, numero: int, equipe: str) -> None:
        if store.transfer_pick(interaction.guild_id, numero, equipe):
            await reply(interaction, f"✅ Le choix #{numero} appartient maintenant à **{equipe}**.")
        else:
            await reply(interaction, f"Le choix #{numero} n'existe pas ou a déjà été fait.")

    async def make_pick(interaction: discord.Interaction, joueur: str, forced: bool) -> None:
        guild_id = interaction.guild_id
        channels = bot.channels(interaction.guild)
        if not channels:
            await reply(interaction, "Le draft n'est pas configuré. Un admin doit faire `/draft-salons`.")
            return
        async with bot.locks[guild_id]:
            pick = store.current_pick(guild_id)
            if pick is None:
                await reply(interaction, "Aucun choix en attente : l'ordre n'est pas défini ou le draft est terminé.")
                return
            if not forced and store.gm_for(guild_id, pick.team) != interaction.user.id:
                await reply(interaction, "Ce n'est pas ton tour.\n" + fmt.on_the_clock(
                    pick, store.gm_for(guild_id, pick.team)))
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
            await interaction.response.defer(ephemeral=True)
            store.record_pick(guild_id, pick.number, player.id)
            following = store.current_pick(guild_id)
            next_gm = store.gm_for(guild_id, following.team) if following else None
            text = fmt.announcement(pick, player, forced) + "\n" + fmt.on_the_clock(following, next_gm)
            message = await channels[1].send(text, allowed_mentions=PING_USERS)
            store.set_pick_message(guild_id, pick.number, message.id)
            await bot.refresh_list(guild_id, channels[0])
        await reply(interaction, f"✅ **{player.name}** repêché par **{pick.team}**.")

    async def player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        players = store.search_available(interaction.guild_id, current)
        return [
            app_commands.Choice(name=" · ".join(x for x in (p.name, p.team, p.position) if x)[:100],
                                value=str(p.id))
            for p in players
        ]

    @tree.command(name="draft", description="Repêcher un joueur (seulement le DG dont c'est le tour)")
    @app_commands.describe(joueur="Commence à taper le nom du joueur")
    @app_commands.autocomplete(joueur=player_autocomplete)
    @app_commands.guild_only()
    async def draft(interaction: discord.Interaction, joueur: str) -> None:
        await make_pick(interaction, joueur, forced=False)

    @tree.command(name="forcedraft", description="Repêcher un joueur pour l'équipe au tour (admin)")
    @app_commands.describe(joueur="Commence à taper le nom du joueur")
    @app_commands.autocomplete(joueur=player_autocomplete)
    @app_commands.guild_only()
    @admin
    async def forcedraft(interaction: discord.Interaction, joueur: str) -> None:
        await make_pick(interaction, joueur, forced=True)

    @tree.command(name="draft-annuler", description="Annuler le dernier choix (admin)")
    @app_commands.guild_only()
    @admin
    async def draft_annuler(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        channels = bot.channels(interaction.guild)
        last = store.last_made_pick(guild_id)
        if not channels or last is None:
            await reply(interaction, "Aucun choix à annuler.")
            return
        await interaction.response.defer(ephemeral=True)
        async with bot.locks[guild_id]:
            player = store.get_player(guild_id, last.player_id)
            if last.message_id:
                await delete_message(channels[1], last.message_id)
            store.undo_pick(guild_id, last.number)
            await bot.refresh_list(guild_id, channels[0])
            current = store.current_pick(guild_id)
            await channels[1].send(
                f"↩️ {fmt.pick_label(last)} annulé : **{player.name}** retourne dans la liste.\n"
                + fmt.on_the_clock(current, store.gm_for(guild_id, current.team)),
                allowed_mentions=PING_USERS,
            )
        await reply(interaction, f"✅ {fmt.pick_label(last)} annulé.")

    @tree.command(name="draft-statut", description="Voir qui est au tour et les prochains choix")
    @app_commands.guild_only()
    async def draft_statut(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        upcoming = store.upcoming_picks(guild_id, 6)
        if not upcoming:
            await reply(interaction, "Aucun choix en attente.")
            return
        lines = [fmt.on_the_clock(upcoming[0], store.gm_for(guild_id, upcoming[0].team)), "", "**Ensuite :**"]
        lines += [f"{fmt.pick_label(p)} : {p.team}" for p in upcoming[1:]]
        lines.append(f"\n{len(store.available_players(guild_id))} joueurs disponibles.")
        await reply(interaction, "\n".join(lines))


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    token = os.environ["DISCORD_TOKEN"]
    guild_id = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
    store = Store(os.environ.get("DB_PATH", "draft.db"))
    DraftBot(store, guild_id).run(token)


if __name__ == "__main__":
    main()

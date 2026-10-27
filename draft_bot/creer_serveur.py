"""Remplit un serveur Discord vide pour en faire le serveur support et de test du bot.

À lancer une seule fois, dans le dossier du bot :  python -m draft_bot.creer_serveur
Le programme se connecte avec le jeton de .env, demande quel serveur remplir, crée
les salons, rôles et messages, puis s'arrête. Ce n'est pas une commande du bot.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

import aiohttp
import discord
from dotenv import load_dotenv

from . import support
from .bot import ConfigError, parse_guild_ids

Ask = Callable[[str], Awaitable[str]]
Say = Callable[[str], None]


def invite_link(client: discord.Client, guild: discord.Guild | None = None) -> str:
    options = {"guild": guild} if guild is not None else {}
    return discord.utils.oauth_url(client.application_id or client.user.id,
                                   permissions=discord.Permissions(administrator=True),
                                   scopes=("bot", "applications.commands"), **options)


def describe(guild: discord.Guild) -> str:
    count = len(guild.text_channels) + len(guild.voice_channels)
    return f"{guild.name} ({count} salon{'s' if count > 1 else ''})"


def plan_text() -> str:
    lines = ["Le programme va créer :"]
    for category in support.BLUEPRINT:
        lines.append(f"  - {category.name} : " + ", ".join(c.name for c in category.channels))
    lines.append(f"  - les rôles {support.SUPPORT_ROLE} et {support.TESTER_ROLE}, et {len(support.TEST_TEAMS)} "
                 "équipes de test : " + ", ".join(support.TEST_TEAMS))
    lines.append("  - des messages de bienvenue, de règles et d'aide, épinglés")
    lines.append("Le propriétaire du serveur reçoit les rôles Support et des équipes de test.")
    lines.append("Ce qui existe déjà avec le même nom est gardé tel quel.")
    return "\n".join(lines)


async def fill_server(client: discord.Client, guild_ids: list[int], ask: Ask, say: Say) -> bool:
    """Le dialogue complet. Retourne True si le serveur a été rempli."""
    guilds = sorted(client.guilds, key=lambda g: g.name.casefold())
    if not guilds:
        say("Le bot n'est sur aucun serveur. Invite-le d'abord sur ton serveur vide avec ce lien :")
        say(invite_link(client))
        return False
    say("Le bot est sur ces serveurs :")
    for number, guild in enumerate(guilds, start=1):
        say(f"  {number}. {describe(guild)}")
    answer = (await ask("Tape le numéro du serveur à remplir, puis Entrée (rien pour annuler) : ")).strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(guilds):
        say("Annulé. Rien n'a été changé.")
        return False
    guild = guilds[int(answer) - 1]

    refused = support.refusal(guild)
    if refused:
        say(refused)
        if support.missing_permissions(guild):
            say(invite_link(client, guild))
        return False

    say("")
    say(plan_text())
    if (await ask(f"Remplir « {guild.name} » ? Tape oui puis Entrée : ")).strip().casefold() != "oui":
        say("Annulé. Rien n'a été changé.")
        return False

    say("Construction en cours, ça prend environ 30 secondes...")
    try:
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        report = await support.build_structure(guild, owner)
    except discord.HTTPException as error:
        say(f"Discord a refusé une partie de la construction ({error.status}).")
        say("Vérifie que le bot a la permission Administrateur, puis relance ce programme : "
            "ce qui existe déjà est gardé, le reste sera terminé.")
        return False

    say("")
    say(f"Terminé! {len(report.created)} éléments créés"
        + (f", {len(report.kept)} déjà présents gardés." if report.kept else "."))
    if report.roles_given:
        say(f"Rôles donnés à {owner.display_name} : " + ", ".join(report.roles_given))
    for problem in report.problems:
        say(f"Attention : {problem}")
    if guild_ids and guild.id not in guild_ids:
        say("")
        say("IMPORTANT : ce serveur n'est pas dans GUILD_IDS. Le bot de draft le quitterait au démarrage.")
        say(f"Ajoute ce numéro à GUILD_IDS dans le fichier .env : {guild.id}")
    say("Va voir le salon test-commandes pour préparer le draft de test.")
    return True


async def console_ask(prompt: str) -> str:
    # input() bloque : on le lance à part pour que la connexion à Discord reste active.
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return ""


def main() -> None:
    load_dotenv()
    try:
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("DISCORD_TOKEN est vide : lance ce programme dans le dossier du bot, à côté de .env.")
        guild_ids = parse_guild_ids(os.environ)
    except ConfigError as error:
        print(error)
        raise SystemExit(1)

    client = discord.Client(intents=discord.Intents.default())
    started = False

    @client.event
    async def on_ready() -> None:
        nonlocal started
        if started:
            return
        started = True
        try:
            await fill_server(client, guild_ids, console_ask, print)
        finally:
            await client.close()

    print("Connexion à Discord...")
    try:
        client.run(token, log_level=logging.WARNING)
    except discord.LoginFailure:
        print("Le jeton de .env est refusé par Discord. Vérifie DISCORD_TOKEN.")
        raise SystemExit(1)
    except aiohttp.ClientError:
        print("Impossible de joindre Discord. Vérifie ta connexion Internet, puis relance.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

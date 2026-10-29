"""Les transactions lues directement sur le site SHM, avec le compte SHM du bot.

Le bot se connecte à SHM avec son propre compte (SHM_USERNAME et SHM_PASSWORD dans .env).
Le commissaire d'une ligue ajoute ce compte comme membre de sa ligue, sans équipe. Le bot lit
alors la page « Historique des transactions » de la ligue toutes les quelques minutes et
annonce les nouvelles transactions dans Discord.

Ce module lit la page et parle à SHM. Il ne connaît rien de Discord : les transactions qu'il
rend sont celles du module `capture`, qui trouve les rôles et écrit les annonces.
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from . import capture
from .storage import normalize

log = logging.getLogger("draft_bot.shm")

BASE_URL = "https://simulatedhockeymanager.ca"
# Le bot se présente honnêtement : les admins de SHM savent qui visite leurs pages.
USER_AGENT = "HockeySimsTools/1.0 (bot Discord des ligues SHM)"
# Au plus une page SHM toutes les 2 secondes, toutes ligues confondues : très peu de trafic.
SECONDS_BETWEEN_REQUESTS = 2.0
REQUEST_SECONDS = 30
# Après un mot de passe refusé, le bot attend avant de réessayer : SHM bloque les essais répétés.
LOGIN_RETRY_SECONDS = 30 * 60

# Les adresses de SHM qui ne sont pas des ligues.
NOT_LEAGUES = {"home", "login", "logout", "register", "profile", "inbox", "language", "password",
               "privacy-policies", "terms-and-conditions", "cookie-policies", "about-us", "livewire"}
SLUG = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
PICK = re.compile(r"choix\s*de\s*(\d{1,2})\s*(?:re|er|e|eme)?\s*ronde|(\d{1,2})\s*(?:st|nd|rd|th)\s*round",
                  re.IGNORECASE)


class ShmError(Exception):
    """SHM ne donne pas les transactions. Le message s'affiche tel quel aux admins."""


class LoginNeeded(ShmError):
    """SHM montre la page de connexion : la session du bot a expiré."""


def league_slug(text: str) -> str:
    """Le nom court de la ligue dans l'adresse SHM.

    « lnhs_2 », « simulatedhockeymanager.ca/lnhs_2 » et
    « https://simulatedhockeymanager.ca/lnhs_2/trades/history » donnent tous « lnhs_2 ».
    """
    text = text.strip().strip("<>")
    if "/" in text or "." in text:
        url = urlparse(text if "://" in text else "https://" + text)
        host = (url.hostname or "").lower()
        if host not in ("simulatedhockeymanager.ca", "www.simulatedhockeymanager.ca"):
            raise ShmError("Cette adresse n'est pas sur simulatedhockeymanager.ca. Colle l'adresse de la page "
                           "d'accueil de ta ligue sur SHM.")
        parts = [p for p in url.path.split("/") if p]
        text = parts[0] if parts else ""
    if not SLUG.match(text) or text.lower() in NOT_LEAGUES:
        raise ShmError("Je ne trouve pas le nom de la ligue dans cette adresse. Ouvre ta ligue sur SHM et copie "
                       "l'adresse, par exemple https://simulatedhockeymanager.ca/lnhs_2")
    return text


def history_url(slug: str, page: int = 1) -> str:
    return f"{BASE_URL}/{slug}/trades/history" + (f"?page={page}" if page > 1 else "")


@dataclass
class ShmSide:
    code: str
    items: list[capture.Item] = field(default_factory=list)


@dataclass
class ShmPage:
    trades: list[list[ShmSide]]
    # Code SHM de l'équipe -> son nom sur SHM (ex. « CBS » -> « Columbus Blue Jackets »).
    teams: dict[str, str]
    has_next: bool


def is_login_page(soup: BeautifulSoup) -> bool:
    return soup.find("input", attrs={"name": "password"}) is not None


def page_teams(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str]]:
    """Les équipes de la ligue, d'après le menu « Effectifs » : nom par code, et code par fichier de logo.

    Le logo d'un choix de repêchage n'a pas de nom : son fichier dit à quelle équipe il appartient.
    """
    names, by_logo = {}, {}
    for link in soup.select('a[href*="/rosters/"]'):
        code = link["href"].rstrip("/").rsplit("/", 1)[-1]
        name = " ".join(link.get_text(" ").split())
        if not code or not name:
            continue
        names.setdefault(code, name)
        image = link.find("img")
        if image and image.get("src"):
            by_logo.setdefault(logo_file(image["src"]), code)
    return names, by_logo


def logo_file(src: str) -> str:
    return src.replace("\\", "/").rsplit("/", 1)[-1].split("?")[0]


def parse_item(li, by_logo: dict[str, str]) -> capture.Item:
    text = " ".join(li.get_text(" ").split())
    match = PICK.search(text)
    if not match:
        return capture.Item(text)
    round_number = int(match.group(1) or match.group(2))
    year = capture.YEAR.search(text[match.end():]) or capture.YEAR.search(text)
    image = li.find("img")
    origin = by_logo.get(logo_file(image["src"])) if image and image.get("src") else None
    return capture.Item(f"Choix de {capture.ordinal(round_number)} ronde", round_number,
                        year.group(1) if year else None,
                        capture.Logo((0, 0, 0, 0), b"", origin) if origin else None)


def parse_history(html: str) -> ShmPage:
    """Les transactions de la page « Historique des transactions », la plus récente en premier."""
    soup = BeautifulSoup(html, "html.parser")
    if is_login_page(soup):
        raise LoginNeeded("SHM demande de se connecter.")
    table = soup.select_one("table.tradeConfirmedTable")
    if table is None:
        raise ShmError("Je ne trouve pas le tableau des transactions sur SHM. Le compte SHM du bot est-il bien "
                       "membre de cette ligue ?")
    teams, by_logo = page_teams(soup)
    trades = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        sides = []
        for team_cell, items_cell in ((cells[0], cells[1]), (cells[2], cells[3])):
            image = team_cell.find("img")
            code = (image.get("alt") or "").strip() if image else ""
            if not code and image and image.get("src"):
                code = by_logo.get(logo_file(image["src"]), "")
            sides.append(ShmSide(code, [parse_item(li, by_logo) for li in items_cell.find_all("li")]))
        if any(side.items for side in sides):
            trades.append(sides)
    has_next = soup.select_one('a[rel="next"]') is not None
    return ShmPage(trades, teams, has_next)


def known_team(code: str, name: str | None) -> str | None:
    """L'équipe LNH connue (code du module capture) qui correspond au code et au nom SHM.

    SHM n'écrit pas toujours les codes comme la LNH (« CBS », « NAS », « TB »). Le nom complet
    du menu tranche : « Columbus Blue Jackets » est CBJ.
    """
    best, found = 0, []
    for team in capture.TEAM_NAMES:
        score = max(capture.team_score(code, team), capture.team_score(name, team) if name else 0)
        if score > best:
            best, found = score, [team]
        elif score and score == best:
            found.append(team)
    return found[0] if len(found) == 1 else None


def to_trade(sides: list[ShmSide], teams: dict[str, str]) -> capture.Trade:
    """La transaction dans le format du module capture : il trouve les rôles et écrit l'annonce."""
    result = []
    for side in sides:
        name = teams.get(side.code) or side.code or None
        team = known_team(side.code, name) if side.code else None
        items = []
        for item in side.items:
            if item.origin is not None:
                origin_code = item.origin.team
                origin = known_team(origin_code, teams.get(origin_code))
                item = capture.Item(item.text, item.round, item.year, capture.Logo((0, 0, 0, 0), b"", origin))
            items.append(item)
        result.append(capture.Side(capture.Logo((0, 0, 0, 0), b"", team), items, name=name))
    return capture.Trade(result)


def shm_key(sides: list[ShmSide]) -> str:
    """L'empreinte d'une transaction d'après SHM seulement (pas les rôles Discord) : renommer
    un rôle ne fait jamais réannoncer d'anciennes transactions."""
    parts = []
    for side in sides:
        items = []
        for item in side.items:
            origin = item.origin.team if item.origin else ""
            items.append(f"{normalize(item.text)}/{item.year or ''}/{origin or ''}")
        parts.append(f"{side.code}:" + "|".join(sorted(items)))
    return "shm:" + hashlib.sha256("#".join(sorted(parts)).encode()).hexdigest()[:32]


class ShmClient:
    """Une connexion à SHM avec le compte du bot. Une seule page demandée à la fois."""

    def __init__(self, username: str, password: str, base_url: str = BASE_URL):
        self.username, self.password, self.base_url = username, password, base_url
        self.session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.logged_in = False
        self.login_blocked_until = 0.0
        self.login_error = ""

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "fr"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_SECONDS),
                # unsafe : accepte aussi les témoins d'une adresse IP (le faux SHM des tests).
                cookie_jar=aiohttp.CookieJar(unsafe=True))
            self.logged_in = False
        return self.session

    async def _request(self, method: str, url: str, **kwargs) -> tuple[str, str]:
        """Rend (adresse finale après les redirections, texte de la page)."""
        wait = self.last_request + SECONDS_BETWEEN_REQUESTS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with self._session().request(method, url, **kwargs) as response:
                self.last_request = time.monotonic()
                text = await response.text(errors="replace")
                if response.status in (403, 429, 503) and ("cloudflare" in response.headers.get("Server", "").lower()
                                                            or "Just a moment" in text):
                    raise ShmError("SHM bloque le bot pour l'instant (protection anti-robots). "
                                   "Je réessaie tout seul plus tard.")
                if response.status == 429:
                    raise ShmError("SHM demande au bot de ralentir. Je réessaie tout seul plus tard.")
                if response.status >= 500:
                    raise ShmError(f"SHM ne répond pas bien en ce moment (erreur {response.status}). "
                                   "Je réessaie tout seul plus tard.")
                if response.status == 404:
                    raise ShmError("Cette ligue n'existe pas sur SHM. Vérifie l'adresse de la ligue.")
                if response.status >= 400:
                    raise ShmError(f"SHM refuse la page (erreur {response.status}). Le compte SHM du bot est-il "
                                   "bien membre de cette ligue ?")
                return str(response.url), text
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.last_request = time.monotonic()
            raise ShmError("Je n'arrive pas à joindre SHM en ce moment. Je réessaie tout seul plus tard.") from error

    async def login(self) -> None:
        if time.monotonic() < self.login_blocked_until:
            raise ShmError(self.login_error)
        _, html = await self._request("GET", f"{self.base_url}/login")
        token = BeautifulSoup(html, "html.parser").find("input", attrs={"name": "_token"})
        if token is None or not token.get("value"):
            raise ShmError("La page de connexion de SHM a changé : le bot ne sait plus se connecter. "
                           "Préviens la personne qui s'occupe du bot.")
        url, html = await self._request("POST", f"{self.base_url}/login", data={
            "_token": token["value"], "username": self.username, "password": self.password, "remember": "1"})
        if urlparse(url).path.rstrip("/").endswith("/login") or is_login_page(BeautifulSoup(html, "html.parser")):
            self.logged_in = False
            # Pas de nouvel essai avant un moment : SHM bloquerait le compte après plusieurs refus.
            self.login_blocked_until = time.monotonic() + LOGIN_RETRY_SECONDS
            self.login_error = ("SHM refuse le nom d'utilisateur ou le mot de passe du compte du bot "
                                "(SHM_USERNAME et SHM_PASSWORD). Corrige-les puis redémarre le bot.")
            raise ShmError(self.login_error)
        self.logged_in = True
        log.info("Connecté à SHM avec le compte %s.", self.username)

    async def history(self, slug: str, page: int = 1) -> ShmPage:
        """Une page de l'historique des transactions d'une ligue. Se connecte au besoin."""
        async with self.lock:
            for attempt in range(2):
                if not self.logged_in:
                    await self.login()
                url, html = await self._request("GET", history_url(slug, page))
                try:
                    return parse_history(html)
                except LoginNeeded:
                    # La session a expiré : une seule nouvelle connexion, pas de boucle.
                    self.logged_in = False
                    if attempt:
                        raise ShmError("SHM redemande la connexion à chaque page. Le compte du bot est-il bloqué ?")
            raise AssertionError("inaccessible")

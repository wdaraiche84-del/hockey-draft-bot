"""Lecture de l'historique des transactions SHM et connexion au site avec le compte du bot."""

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from draft_bot import capture, shm

FIXTURES = Path(__file__).parent / "fixtures"
HISTORY = (FIXTURES / "shm_historique.html").read_text(encoding="utf-8")
LOGIN = (FIXTURES / "shm_connexion.html").read_text(encoding="utf-8")


def items(side):
    return [(i.text, i.year, i.origin.team if i.origin else None) for i in side.items]


# La page « Historique des transactions »

def test_reads_every_trade_of_the_real_page():
    page = shm.parse_history(HISTORY)
    assert len(page.trades) == 15
    assert page.has_next
    first = page.trades[0]
    assert [side.code for side in first] == ["DET", "NYR"]
    assert items(first[0]) == [("Adam Fox", None, None), ("Choix de 2e ronde", "2026", "CAR")]
    assert items(first[1]) == [("Simon Edvinsson", None, None), ("Carter Bear", None, None)]
    # Un choix seul, sans joueur.
    kopitar = page.trades[7]
    assert [side.code for side in kopitar] == ["LAK", "NAS"]
    assert items(kopitar[0]) == [("Choix de 2e ronde", "2026", "MIN")]
    # Deux choix de la même équipe.
    assert items(page.trades[5][0])[1:] == [("Choix de 2e ronde", "2027", "BUF"), ("Choix de 2e ronde", "2028", "BUF")]
    assert page.teams["CBS"] == "Columbus Blue Jackets" and len(page.teams) == 30


def test_every_team_of_the_page_is_a_known_nhl_team():
    page = shm.parse_history(HISTORY)
    found = {code: shm.known_team(code, name) for code, name in page.teams.items()}
    assert None not in found.values()
    # SHM n'écrit pas tous les codes comme la LNH.
    assert (found["CBS"], found["NAS"], found["NJ"], found["SJ"], found["TB"], found["WAS"]) == (
        "CBJ", "NSH", "NJD", "SJS", "TBL", "WSH")
    assert (found["SEA"], found["UTA"], found["VGK"]) == ("SEA", "UTA", "VGK")
    assert len(set(found.values())) == 30


def test_trade_becomes_an_announcement_with_team_roles():
    page = shm.parse_history(HISTORY)

    class Role:
        def __init__(self, i, name):
            self.id, self.name, self.mention = i, name, f"<@&{i}>"
    roles = [Role(1, "Red Wings"), Role(2, "Rangers"), Role(3, "Hurricanes")]
    trade = shm.to_trade(page.trades[0], page.teams)
    capture.resolve(trade, roles, [])
    by_id = {r.id: r for r in roles}
    assert capture.announcement(trade, by_id) == (
        "🔁 **Transaction**\n"
        "➡️ <@&1> reçoit : **Adam Fox**, **Choix de 2e ronde 2026 (Hurricanes)**\n"
        "➡️ <@&2> reçoit : **Simon Edvinsson**, **Carter Bear**")
    # Une équipe sans rôle garde son nom SHM, sans mention.
    other = shm.to_trade(page.trades[1], page.teams)
    capture.resolve(other, roles, [])
    assert capture.announcement(other, by_id).splitlines()[1] == "➡️ **San Jose Sharks** reçoit : **Otto Stenberg**"


def test_key_ignores_order_and_changes_with_the_content():
    page = shm.parse_history(HISTORY)
    keys = [shm.shm_key(t) for t in page.trades]
    assert len(set(keys)) == 15
    assert shm.shm_key(list(reversed(page.trades[0]))) == keys[0]
    again = shm.parse_history(HISTORY)
    assert [shm.shm_key(t) for t in again.trades] == keys


def test_login_page_and_missing_table_are_recognized():
    with pytest.raises(shm.LoginNeeded):
        shm.parse_history(LOGIN)
    with pytest.raises(shm.ShmError, match="membre de cette ligue"):
        shm.parse_history("<html><body><h3>Accès refusé</h3></body></html>")
    empty = shm.parse_history('<table class="tradeConfirmedTable"><tbody></tbody></table>')
    assert empty.trades == [] and not empty.has_next


@pytest.mark.parametrize("text", [
    "lnhs_2", " lnhs_2 ", "https://simulatedhockeymanager.ca/lnhs_2", "simulatedhockeymanager.ca/lnhs_2/",
    "https://simulatedhockeymanager.ca/lnhs_2/trades/history?page=2", "<https://www.simulatedhockeymanager.ca/lnhs_2>",
])
def test_league_address_gives_the_league(text):
    assert shm.league_slug(text) == "lnhs_2"


@pytest.mark.parametrize("text", [
    "", "https://exemple.com/lnhs_2", "https://simulatedhockeymanager.ca/", "https://simulatedhockeymanager.ca/home",
    "https://simulatedhockeymanager.ca/login", "ma ligue",
])
def test_bad_league_address_is_refused(text):
    with pytest.raises(shm.ShmError):
        shm.league_slug(text)


# La connexion au site, avec un faux SHM local

class FakeShm:
    """Un petit serveur qui se comporte comme SHM : connexion par formulaire, puis pages de ligue."""

    def __init__(self):
        self.password = "secret"
        self.logins = 0
        self.history_requests = []
        self.expire_next = False
        self.blocked = False
        self.user_agents = set()
        app = web.Application()
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_get("/home", self.home)
        app.router.add_get("/{slug}/trades/history", self.history)
        self.app = app

    async def login_page(self, request):
        response = web.Response(text=LOGIN, content_type="text/html")
        response.set_cookie("session", "invite")
        return response

    async def login(self, request):
        form = await request.post()
        self.logins += 1
        if form.get("_token") != "JETON_DE_TEST" or request.cookies.get("session") != "invite":
            return web.Response(status=419, text="Page expirée")
        if form.get("username") != "HockeySimsTools" or form.get("password") != self.password:
            raise web.HTTPFound("/login")
        response = web.HTTPFound("/home")
        response.set_cookie("session", "connecte")
        raise response

    async def home(self, request):
        return web.Response(text="<html>Répertoire des ligues</html>", content_type="text/html")

    async def history(self, request):
        self.user_agents.add(request.headers.get("User-Agent"))
        if self.blocked:
            return web.Response(status=403, text="<title>Just a moment...</title>", headers={"Server": "cloudflare"})
        if request.cookies.get("session") != "connecte" or self.expire_next:
            self.expire_next = False
            raise web.HTTPFound("/login")
        slug = request.match_info["slug"]
        self.history_requests.append((slug, request.query.get("page", "1")))
        if slug == "inconnue":
            return web.Response(status=404, text="Introuvable")
        return web.Response(text=HISTORY, content_type="text/html")


@pytest.fixture
def fake_shm(monkeypatch):
    monkeypatch.setattr(shm, "SECONDS_BETWEEN_REQUESTS", 0)
    return FakeShm()


def with_server(fake, scenario):
    async def main():
        runner = web.AppRunner(fake.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = shm.ShmClient("HockeySimsTools", "secret", base_url=f"http://127.0.0.1:{port}")
        monkey_url = shm.history_url
        shm.history_url = lambda slug, page=1: monkey_url(slug, page).replace(shm.BASE_URL, client.base_url)
        try:
            return await scenario(client)
        finally:
            shm.history_url = monkey_url
            await client.close()
            await runner.cleanup()
    return asyncio.run(main())


def test_client_logs_in_once_then_reads_pages(fake_shm):
    async def scenario(client):
        first = await client.history("lnhs_2")
        second = await client.history("lnhs_2", 2)
        return first, second
    first, second = with_server(fake_shm, scenario)
    assert len(first.trades) == len(second.trades) == 15
    assert fake_shm.logins == 1
    assert fake_shm.history_requests == [("lnhs_2", "1"), ("lnhs_2", "2")]
    assert fake_shm.user_agents == {shm.USER_AGENT}


def test_client_logs_in_again_when_the_session_expires(fake_shm):
    async def scenario(client):
        await client.history("lnhs_2")
        fake_shm.expire_next = True
        return await client.history("lnhs_2")
    assert len(with_server(fake_shm, scenario).trades) == 15
    assert fake_shm.logins == 2


def test_wrong_password_is_not_retried_in_a_loop(fake_shm):
    fake_shm.password = "autre"

    async def scenario(client):
        errors = []
        for _ in range(3):
            with pytest.raises(shm.ShmError) as error:
                await client.history("lnhs_2")
            errors.append(str(error.value))
        return errors
    errors = with_server(fake_shm, scenario)
    assert all("mot de passe" in e for e in errors)
    # Un seul essai : SHM bloquerait un compte qui se trompe sans arrêt.
    assert fake_shm.logins == 1


def test_unknown_league_and_blocked_bot_are_explained(fake_shm):
    async def scenario(client):
        with pytest.raises(shm.ShmError) as missing:
            await client.history("inconnue")
        fake_shm.blocked = True
        with pytest.raises(shm.ShmError) as blocked:
            await client.history("lnhs_2")
        return str(missing.value), str(blocked.value)
    missing, blocked = with_server(fake_shm, scenario)
    assert "n'existe pas" in missing
    assert "anti-robots" in blocked


def test_unreachable_shm_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(shm, "SECONDS_BETWEEN_REQUESTS", 0)

    async def scenario():
        client = shm.ShmClient("HockeySimsTools", "secret", base_url="http://127.0.0.1:9")
        try:
            with pytest.raises(shm.ShmError, match="joindre SHM"):
                await client.history("lnhs_2")
        finally:
            await client.close()
    asyncio.run(scenario())

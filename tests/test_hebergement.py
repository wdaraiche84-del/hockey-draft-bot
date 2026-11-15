"""Le serveur (dossier hebergement) dépend de quelques détails du bot : ces tests les gardent en place."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
HEBERGEMENT = RACINE / "hebergement"


def test_le_bot_annonce_sa_connexion_comme_le_serveur_l_attend():
    # Après une mise à jour, hebergement/maj.sh attend cette phrase dans le journal du bot pour savoir
    # qu'il est bien connecté à Discord. Sans elle, chaque mise à jour serait annulée.
    # Si le message de on_ready change, changer aussi LIGNE_CONNECTE dans hebergement/maj.sh.
    attendu = next(line.split('"')[1] for line in (HEBERGEMENT / "maj.sh").read_text(encoding="utf-8").splitlines()
                   if line.startswith("LIGNE_CONNECTE="))
    assert attendu in (RACINE / "draft_bot" / "bot.py").read_text(encoding="utf-8")


def test_la_base_va_dans_data_avec_data_dir():
    from draft_bot.bot import resolve_paths
    assert resolve_paths({"DATA_DIR": "/data"}) == (Path("/data/draft.db"), Path("/data/sauvegardes"))


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="scripts pour Linux seulement")
@pytest.mark.parametrize("script", ["installer.sh", "maj.sh", "bot.sh"])
def test_les_scripts_du_serveur_sont_valides(script):
    subprocess.run(["bash", "-n", str(HEBERGEMENT / script)], check=True)


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="scripts pour Linux seulement")
def test_nuit_sh_valide():
    subprocess.run(["bash", "-n", str(HEBERGEMENT / "nuit.sh")], check=True)


# Ubuntu relançait le bot après ses mises à jour de sécurité, le matin (vu le 2026-11-15 à 6 h 46) :
# une fenêtre ouverte dans /admin donnait « Une erreur s'est produite ». Ces tests gardent la correction.

def _installer():
    return (HEBERGEMENT / "installer.sh").read_text(encoding="utf-8")


def test_ubuntu_ne_relance_jamais_le_bot_lui_meme():
    texte = _installer()
    regle = "$nrconf{override_rc}{qr(^hockey-bot)} = 0;"
    assert regle in texte
    # Écrit AVANT le premier apt-get : relancer l'installation ne doit pas relancer le bot non plus.
    assert texte.index("/etc/needrestart/conf.d/hockey-bot.conf") < texte.index("\napt-get ")


def _reglages():
    return (HEBERGEMENT / "reglages.sh").read_text(encoding="utf-8")


def test_mises_a_jour_de_securite_la_nuit_puis_bot_relance_seulement_si_besoin():
    texte = _reglages()
    minuteur = texte.split("apt-daily-upgrade.timer.d/hockey-bot.conf 644 <<'EOF'")[1].split("\nEOF")[0]
    # « OnCalendar= » vide d'abord : sinon l'heure d'Ubuntu (6 h à 7 h) resterait en plus de la nôtre.
    assert "OnCalendar=\nOnCalendar=*-*-* 03:30" in minuteur and "RandomizedDelaySec=30m" in minuteur
    assert "ExecStartPost=-/usr/local/sbin/hockey-bot-nuit" in texte
    assert "copier nuit.sh /usr/local/sbin/hockey-bot-nuit 755" in texte
    # Le redémarrage d'Ubuntu (si une mise à jour l'exige) reste après la fenêtre des mises à jour.
    assert 'Automatic-Reboot-Time "05:00"' in texte
    # Aussi dans les réglages réappliqués tout seuls, et avant d'installer quoi que ce soit.
    assert "$nrconf{override_rc}{qr(^hockey-bot)} = 0;" in texte
    assert texte.index("needrestart/conf.d/hockey-bot.conf") < texte.index("apt-get -o")


# Les réglages du serveur s'appliquent tout seuls à chaque version : plus de commande à recoller.

def test_l_installation_et_la_mise_a_jour_utilisent_les_memes_reglages():
    assert 'bash "$OUTILS/hebergement/reglages.sh" "$OUTILS/hebergement"' in _installer()
    maj = (HEBERGEMENT / "maj.sh").read_text(encoding="utf-8")
    principal = maj.split("\nprincipal() {")[1].split("\n}")[0]
    # Après chaque passage (nouvelle version ou pas), sauf en pause et pour « bot retour ».
    assert principal.index("mettre_a_jour") < principal.index("appliquer_reglages")
    assert principal.index("pause") < principal.index("appliquer_reglages")
    assert 'bash "$dossier/reglages.sh" "$dossier"' in maj


def test_les_reglages_ne_relancent_jamais_le_bot():
    texte = _reglages()
    for interdit in ("restart hockey-bot", "restart \"$SERVICE\"", "stop hockey-bot", "--now hockey-bot"):
        assert interdit not in texte


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="scripts pour Linux seulement")
def test_reglages_ecrit_seulement_ce_qui_change(tmp_path):
    import os
    import stat

    def lancer(source=HEBERGEMENT):
        return subprocess.run(["bash", str(HEBERGEMENT / "reglages.sh"), str(source)], check=True, text=True,
                              capture_output=True, env={**os.environ, "RACINE": str(tmp_path)}).stdout

    premiere = lancer()
    assert "mis à jour" in premiere
    for chemin, executable in (("usr/local/sbin/hockey-bot-maj", True), ("usr/local/bin/bot", True),
                               ("usr/local/sbin/hockey-bot-nuit", True), ("usr/local/sbin/hockey-bot-reglages", True),
                               ("etc/systemd/system/hockey-bot.service", False),
                               ("etc/needrestart/conf.d/hockey-bot.conf", False),
                               ("etc/systemd/system/apt-daily-upgrade.timer.d/hockey-bot.conf", False),
                               ("etc/apt/apt.conf.d/52hockey-bot", False)):
        fichier = tmp_path / chemin
        assert fichier.is_file(), chemin
        assert bool(fichier.stat().st_mode & stat.S_IXUSR) == executable, chemin
    assert (tmp_path / "usr/local/sbin/hockey-bot-maj").read_bytes() == (HEBERGEMENT / "maj.sh").read_bytes()
    assert "rien à changer" in lancer()  # deuxième fois : aucun fichier réécrit

    # Une nouvelle version change un outil : seul celui-là est réécrit.
    nouvelle = tmp_path / "nouvelle"
    shutil.copytree(HEBERGEMENT, nouvelle)
    (nouvelle / "bot.sh").write_text((HEBERGEMENT / "bot.sh").read_text(encoding="utf-8") + "\n# v2\n",
                                     encoding="utf-8")
    sortie = lancer(nouvelle)
    assert "/usr/local/bin/bot" in sortie and "hockey-bot-maj" not in sortie
    assert (tmp_path / "usr/local/bin/bot").read_text(encoding="utf-8").endswith("# v2\n")
    assert not [f for f in tmp_path.rglob("*.??????") if f.is_file()]  # aucun fichier temporaire oublié


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="scripts pour Linux seulement")
@pytest.mark.parametrize("lignes, attendu", [
    # Rien de remplacé : le bot n'est pas relancé.
    (["7f00-7f01 r-xp 00000000 fc:01 123 /usr/lib/x86_64-linux-gnu/libssl.so.3",
      "7f02-7f03 r-xp 00000000 fc:01 124 /usr/bin/python3.12"], "non"),
    # Python ou une bibliothèque du système remplacés par une mise à jour : relancer.
    (["7f00-7f01 r-xp 00000000 fc:01 123 /usr/lib/x86_64-linux-gnu/libssl.so.3 (deleted)"], "oui"),
    (["7f02-7f03 r-xp 00000000 fc:01 124 /usr/bin/python3.12 (deleted)"], "oui"),
    # Mémoire partagée ou fichier temporaire effacé : pas une mise à jour, on ne relance pas.
    (["7f00-7f01 rw-s 00000000 00:01 9 /dev/shm/sem.x (deleted)",
      "7f00-7f01 rw-s 00000000 00:01 9 /memfd:onnx (deleted)",
      "7f00-7f01 rw-p 00000000 00:01 9 /tmp/abc (deleted)"], "non"),
])
def test_nuit_relance_seulement_si_une_bibliotheque_a_ete_remplacee(tmp_path, lignes, attendu):
    maps = tmp_path / "maps"
    maps.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    sortie = subprocess.run(["bash", str(HEBERGEMENT / "nuit.sh"), "--verifier-maps", str(maps)],
                            check=True, capture_output=True, text=True).stdout.strip()
    assert sortie == attendu

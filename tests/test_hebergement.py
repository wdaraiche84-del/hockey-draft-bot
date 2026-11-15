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


def test_mises_a_jour_de_securite_la_nuit_puis_bot_relance_seulement_si_besoin():
    texte = _installer()
    minuteur = texte.split("apt-daily-upgrade.timer.d/hockey-bot.conf <<'EOF'")[1].split("\nEOF")[0]
    # « OnCalendar= » vide d'abord : sinon l'heure d'Ubuntu (6 h à 7 h) resterait en plus de la nôtre.
    assert "OnCalendar=\nOnCalendar=*-*-* 03:30" in minuteur and "RandomizedDelaySec=30m" in minuteur
    assert "ExecStartPost=-/usr/local/sbin/hockey-bot-nuit" in texte
    assert 'install -m 755 "$OUTILS/hebergement/nuit.sh" /usr/local/sbin/hockey-bot-nuit' in texte
    # Le redémarrage d'Ubuntu (si une mise à jour l'exige) reste après la fenêtre des mises à jour.
    assert 'Automatic-Reboot-Time "05:00"' in texte


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

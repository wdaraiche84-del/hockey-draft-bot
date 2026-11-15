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

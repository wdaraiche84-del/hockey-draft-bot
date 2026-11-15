#!/usr/bin/env bash
# Installe le bot Hockey Sims Tools sur un serveur Ubuntu 24.04 neuf (ex. un Droplet DigitalOcean).
#
# À coller dans la console du serveur, connecté en root :
#   curl -fsSL https://raw.githubusercontent.com/wdaraiche84-del/hockey-draft-bot/main/hebergement/installer.sh | bash
#
# Le script peut être relancé sans danger : il ne refait que ce qui manque, garde les secrets
# et la base du draft (/data), et met à jour les outils du serveur.
#
# Ce qu'il fait :
#   - fuseau horaire America/Toronto, mémoire d'appoint (swap) de 2 Go, mises à jour de sécurité
#     d'Ubuntu automatiques la nuit (le bot n'est relancé que la nuit, et seulement s'il le faut),
#     pare-feu (seul SSH entre) et fail2ban contre les essais de mot de passe ;
#   - un utilisateur « hockeybot » sans droits, qui fait tourner le bot ;
#   - /data : la base du draft et ses sauvegardes ;
#   - demande le jeton Discord et le compte SHM du bot (gardés sur le serveur seulement) ;
#   - installe la dernière version de main, lance les tests, puis démarre le bot (service systemd,
#     relancé tout seul s'il s'arrête et au redémarrage du serveur) ;
#   - toutes les 3 minutes, installe la nouvelle version si main a changé (voir maj.sh).
set -Eeuo pipefail
umask 022

DEPOT_URL=${DEPOT_URL:-https://github.com/wdaraiche84-del/hockey-draft-bot.git}
BRANCHE=${BRANCHE:-main}
BASE=/opt/hockey-bot
CONFIG=/etc/hockey-bot
ETAT=/var/lib/hockey-bot
UTILISATEUR=hockeybot
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

etape() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
vert() { printf '\033[1;32m%s\033[0m\n' "$*"; }
rouge() { printf '\033[1;31m%s\033[0m\n' "$*"; }
trap 'rouge "❌ L’installation s’est arrêtée (ligne $LINENO). Envoie une capture de cet écran à Claude."' ERR

# Tout est dans une fonction : avec « curl | bash », bash lit ainsi le script en entier avant de
# commencer, et aucune commande ne peut avaler la suite du script par erreur.
installer() {
if [ "$(id -u)" -ne 0 ]; then
    rouge "Lance ce script en root (la console DigitalOcean est déjà en root)."
    exit 1
fi
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
    rouge "Attention : ce script est prévu pour Ubuntu 24.04. On continue quand même."
fi

etape "1/8 Fuseau horaire : America/Toronto"
timedatectl set-timezone America/Toronto 2>/dev/null || ln -sf /usr/share/zoneinfo/America/Toronto /etc/localtime
echo "Heure du serveur : $(date '+%Y-%m-%d %H:%M')"

etape "2/8 Mémoire d'appoint (swap) de 2 Go"
if swapon --show=NAME --noheadings | grep -q .; then
    echo "Déjà en place."
else
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile > /dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Fait."
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-hockey-bot.conf
sysctl -q -p /etc/sysctl.d/90-hockey-bot.conf || true

etape "3/8 Logiciels nécessaires (1 à 3 minutes)"
# Avant tout « apt-get » : Ubuntu ne doit jamais relancer le bot lui-même après une mise à jour
# (il le faisait le matin, en pleine journée). Le bot est relancé la nuit, et seulement s'il le faut
# (voir nuit.sh et l'étape 4). Avec ce réglage, relancer ce script ne relance pas le bot non plus.
mkdir -p /etc/needrestart/conf.d
cat > /etc/needrestart/conf.d/hockey-bot.conf <<'EOF'
# Hockey Sims Tools : ne jamais relancer le bot après une mise à jour d'Ubuntu (hockey-bot-nuit s'en occupe, la nuit).
$nrconf{override_rc}{qr(^hockey-bot)} = 0;
EOF
# Sur un serveur neuf, Ubuntu fait souvent ses propres mises à jour au premier démarrage :
# on attend qu'il ait fini (jusqu'à 10 minutes) au lieu d'échouer.
apt-get -o DPkg::Lock::Timeout=600 update -q
apt-get -o DPkg::Lock::Timeout=600 install -y -q git curl ca-certificates python3 python3-venv libglib2.0-0t64 libgomp1 \
    ufw fail2ban unattended-upgrades > /dev/null
echo "Fait."

etape "4/8 Sécurité : pare-feu, fail2ban, mises à jour de sécurité automatiques"
ufw allow OpenSSH > /dev/null
ufw --force enable > /dev/null
systemctl enable --now fail2ban > /dev/null 2>&1 || true
# Mises à jour de sécurité la nuit, journal limité, etc. : voir reglages.sh (appliqué à l'étape 6,
# puis tout seul par la mise à jour automatique quand ces réglages changent).
ufw status | head -n 1

etape "5/8 Utilisateur du bot et dossiers"
id "$UTILISATEUR" > /dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$UTILISATEUR"
mkdir -p "$BASE/versions" "$BASE/venvs" "$CONFIG" "$ETAT" /data
chown "$UTILISATEUR:$UTILISATEUR" /data
chmod 700 /data "$CONFIG"
echo "$BRANCHE" > "$CONFIG/branche"
echo "Base du draft : /data/draft.db · sauvegardes : /data/sauvegardes"

etape "6/8 Code du bot (depuis GitHub)"
if [ ! -d "$BASE/depot.git" ]; then
    git clone --quiet --bare --single-branch --branch "$BRANCHE" "$DEPOT_URL" "$BASE/depot.git"
else
    git --git-dir="$BASE/depot.git" remote set-url origin "$DEPOT_URL"
fi
git --git-dir="$BASE/depot.git" fetch --quiet --prune origin "+refs/heads/$BRANCHE:refs/heads/$BRANCHE"
SHA=$(git --git-dir="$BASE/depot.git" rev-parse "refs/heads/$BRANCHE")
echo "Dernière version : ${SHA:0:7} « $(git --git-dir="$BASE/depot.git" log -1 --format=%s "$SHA" | cut -c1-80) »"
# Les outils du serveur viennent de la même version que le bot.
OUTILS=$(mktemp -d)
git --git-dir="$BASE/depot.git" archive "$SHA" hebergement | tar -x --no-same-permissions --no-same-owner -C "$OUTILS"
# Réglages du serveur, outils (« bot », mises à jour) et services. Ensuite, la mise à jour
# automatique les réapplique toute seule quand ils changent : plus jamais besoin de cette commande.
bash "$OUTILS/hebergement/reglages.sh" "$OUTILS/hebergement"
rm -rf "$OUTILS"
systemctl daemon-reload
systemctl enable hockey-bot.service > /dev/null 2>&1

etape "7/8 Secrets du bot (gardés sur ce serveur seulement)"
trap - ERR
/usr/local/bin/bot secrets --installation
trap 'rouge "❌ L’installation s’est arrêtée (ligne $LINENO). Envoie une capture de cet écran à Claude."' ERR

etape "8/8 Installation du bot, tests, puis démarrage (5 à 10 minutes la première fois)"
echo "⚠️  Le bot ne doit tourner qu'à un seul endroit : ferme-le sur ton PC s'il est ouvert."
trap - ERR
if /usr/local/sbin/hockey-bot-maj --forcer; then
    systemctl enable --now hockey-bot-maj.timer > /dev/null 2>&1
    echo
    vert "════════════════════════════════════════════════════"
    vert "  ✅ C'EST FAIT : le bot est en ligne sur le serveur."
    vert "════════════════════════════════════════════════════"
    echo "  Il redémarre tout seul s'il plante ou si le serveur redémarre."
    echo "  Chaque PR fusionnée sur main est installée toute seule en quelques minutes."
    echo
    /usr/local/bin/bot
else
    systemctl enable --now hockey-bot-maj.timer > /dev/null 2>&1 || true
    echo
    rouge "❌ Le bot n'a pas pu démarrer. Envoie une capture de cet écran à Claude."
    cat "$ETAT/dernier-resultat" 2>/dev/null || true
    journalctl -u hockey-bot -n 15 -o cat --no-pager 2>/dev/null || true
    exit 1
fi
}

installer "$@" < /dev/null

#!/usr/bin/env bash
# Les réglages du serveur pour le bot. Appliqués :
#   - par installer.sh, à l'installation ;
#   - par la mise à jour automatique (maj.sh), à chaque nouvelle version qui les change.
# Personne n'a donc à recoller la commande d'installation quand ces réglages changent.
#
# Sûr à relancer : un fichier n'est réécrit que si son contenu change, et ce script ne relance
# JAMAIS le bot (un changement de hockey-bot.service s'applique à son prochain redémarrage).
#
# Usage : reglages.sh DOSSIER     (DOSSIER = le dossier « hebergement » de la version à appliquer)
# Pour les tests : RACINE=/un/dossier écrit tout sous ce dossier, sans toucher au système
# (pas de systemctl, apt-get ni sysctl).
set -uo pipefail
umask 022

SOURCE=${1:?dossier hebergement}
RACINE=${RACINE:-}
CHANGES=()

systeme() { [ -z "$RACINE" ]; }

# ecrire FICHIER MODE : écrit l'entrée standard dans FICHIER seulement si le contenu change.
# L'écriture passe par un fichier temporaire renommé : un script en cours d'exécution (maj.sh
# lui-même) garde son ancienne copie, il n'est jamais coupé au milieu.
ecrire() {
    local cible=$RACINE$1 mode=$2 tmp
    mkdir -p "$(dirname "$cible")"
    tmp=$(mktemp "$cible.XXXXXX") || return 1
    cat > "$tmp"
    if [ -f "$cible" ] && cmp -s "$tmp" "$cible"; then
        rm -f "$tmp"
        return 0
    fi
    chmod "$mode" "$tmp" && mv -f "$tmp" "$cible" || { rm -f "$tmp"; return 1; }
    CHANGES+=("$1")
}

copier() { ecrire "$2" "$3" < "$SOURCE/$1"; }

a_change() {   # a_change MOTIF : un des fichiers changés correspond-il ?
    local f
    for f in "${CHANGES[@]+"${CHANGES[@]}"}"; do [[ $f == $1 ]] && return 0; done
    return 1
}

# 1. Ubuntu ne relance jamais le bot lui-même après une mise à jour (il le faisait le matin).
#    Écrit en premier : l'installation de logiciels plus bas ne relance donc pas le bot non plus.
ecrire /etc/needrestart/conf.d/hockey-bot.conf 644 <<'EOF'
# Hockey Sims Tools : ne jamais relancer le bot après une mise à jour d'Ubuntu (hockey-bot-nuit s'en occupe, la nuit).
$nrconf{override_rc}{qr(^hockey-bot)} = 0;
EOF

# 2. Logiciels nécessaires : seulement ceux qui manquent (rien à faire d'habitude).
PAQUETS=(git curl ca-certificates python3 python3-venv libglib2.0-0t64 libgomp1 ufw fail2ban unattended-upgrades)
if systeme; then
    manquants=()
    for p in "${PAQUETS[@]}"; do dpkg -s "$p" > /dev/null 2>&1 || manquants+=("$p"); done
    if [ ${#manquants[@]} -gt 0 ]; then
        export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
        apt-get -o DPkg::Lock::Timeout=600 update -q > /dev/null \
            && apt-get -o DPkg::Lock::Timeout=600 install -y -q "${manquants[@]}" > /dev/null \
            && CHANGES+=("logiciels : ${manquants[*]}") \
            || { echo "Logiciels pas installés : ${manquants[*]}"; exit 1; }
    fi
fi

# 3. Mises à jour de sécurité d'Ubuntu la nuit ; redémarrage du serveur à 5 h seulement si exigé.
ecrire /etc/apt/apt.conf.d/52hockey-bot 644 <<'EOF'
// Mises à jour de sécurité d'Ubuntu chaque nuit (3 h 30 à 4 h) ; redémarrage à 5 h seulement si une mise à jour l'exige.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "05:00";
EOF
ecrire /etc/systemd/system/apt-daily-upgrade.timer.d/hockey-bot.conf 644 <<'EOF'
# Hockey Sims Tools : mises à jour de sécurité la nuit, pas pendant que les ligues utilisent le bot.
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:30
RandomizedDelaySec=30m
EOF
# Juste après ces mises à jour : relancer le bot seulement s'il utilise une bibliothèque remplacée.
ecrire /etc/systemd/system/apt-daily-upgrade.service.d/hockey-bot.conf 644 <<'EOF'
[Service]
ExecStartPost=-/usr/local/sbin/hockey-bot-nuit
EOF

# 4. Journal limité à 300 Mo, mémoire d'appoint utilisée en dernier recours.
# (« < <(…) » et pas « | » : ecrire doit tourner dans ce shell pour retenir ce qui a changé.)
ecrire /etc/systemd/journald.conf.d/hockey-bot.conf 644 < <(printf '[Journal]\nSystemMaxUse=300M\n')
ecrire /etc/sysctl.d/90-hockey-bot.conf 644 < <(echo 'vm.swappiness=10')

# 5. Les outils du serveur et les services, de la même version que le bot.
copier maj.sh /usr/local/sbin/hockey-bot-maj 755
copier bot.sh /usr/local/bin/bot 755
copier nuit.sh /usr/local/sbin/hockey-bot-nuit 755
copier reglages.sh /usr/local/sbin/hockey-bot-reglages 755
copier hockey-bot.service /etc/systemd/system/hockey-bot.service 644
copier hockey-bot-maj.service /etc/systemd/system/hockey-bot-maj.service 644
copier hockey-bot-maj.timer /etc/systemd/system/hockey-bot-maj.timer 644

if systeme; then
    if a_change "/etc/systemd/*"; then
        systemctl daemon-reload
    fi
    a_change /etc/systemd/journald.conf.d/* && { systemctl restart systemd-journald || true; }
    a_change /etc/sysctl.d/* && { sysctl -q -p /etc/sysctl.d/90-hockey-bot.conf || true; }
    # « enable » ne démarre ni ne relance rien : le bot repartira au prochain démarrage du serveur.
    systemctl enable hockey-bot.service hockey-bot-maj.timer > /dev/null 2>&1 || true
fi

if [ ${#CHANGES[@]} -eq 0 ]; then
    echo "Réglages du serveur : rien à changer."
else
    echo "Réglages du serveur mis à jour : ${CHANGES[*]}"
    a_change /etc/systemd/system/hockey-bot.service && echo "(hockey-bot.service : appliqué au prochain redémarrage du bot)"
fi
exit 0

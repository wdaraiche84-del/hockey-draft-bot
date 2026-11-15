#!/usr/bin/env bash
# Après les mises à jour de sécurité d'Ubuntu (la nuit), relance le bot SEULEMENT s'il en a besoin.
# Installé dans /usr/local/sbin/hockey-bot-nuit, lancé par apt-daily-upgrade.service (voir installer.sh).
#
# Pourquoi : Ubuntu relançait tout seul le bot juste après ses mises à jour, le matin entre 6 h et 7 h.
# Un admin qui avait une fenêtre ouverte voyait « Une erreur s'est produite ». Maintenant :
#   - les mises à jour passent la nuit (3 h 30 à 4 h) ;
#   - Ubuntu ne relance plus jamais le bot lui-même (réglage needrestart) ;
#   - ce script relance le bot après ces mises à jour, seulement si une bibliothèque qu'il utilise
#     a été remplacée (sinon le bot garderait l'ancienne version, sans les correctifs de sécurité).
#
# Usage :
#   hockey-bot-nuit                          vérifier et relancer le bot si besoin
#   hockey-bot-nuit --verifier-maps FICHIER  dire « oui » ou « non » pour un fichier /proc/<pid>/maps (tests)
set -uo pipefail

SERVICE=hockey-bot
ETAT=/var/lib/hockey-bot

# Une bibliothèque du système remplacée par une mise à jour apparaît « (deleted) » dans les fichiers
# ouverts par le programme. On ne regarde que les fichiers du système (pas la mémoire partagée, etc.).
besoin_de_relancer() {
    grep -Eq ' /(usr|lib|lib64|bin|sbin)/[^ ]* \(deleted\)$' "$1" 2>/dev/null
}

journal() {
    local texte="$(date '+%Y-%m-%d %H:%M') $*"
    echo "$texte"
    mkdir -p "$ETAT"
    echo "$texte" >> "$ETAT/historique.log"
}

principal() {
    systemctl is-active --quiet "$SERVICE" || return 0
    local pid
    pid=$(systemctl show -p MainPID --value "$SERVICE")
    [ -n "$pid" ] && [ "$pid" != 0 ] || return 0
    besoin_de_relancer "/proc/$pid/maps" || return 0
    # Pas en plein milieu d'une mise à jour du bot : on attend qu'elle soit finie (même verrou que maj.sh).
    exec 9> /run/hockey-bot-maj.lock
    flock -w 1800 9 || { journal "Mises à jour de sécurité : bot pas relancé (une mise à jour du bot est en cours)."; return 0; }
    systemctl restart "$SERVICE"
    journal "Mises à jour de sécurité d'Ubuntu installées cette nuit : bot relancé pour les utiliser."
}

if [ "${1:-}" = "--verifier-maps" ]; then
    if besoin_de_relancer "${2:?fichier maps}"; then echo oui; else echo non; fi
    exit 0
fi
principal

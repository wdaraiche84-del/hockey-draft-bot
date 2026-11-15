#!/usr/bin/env bash
# La commande « bot » sur le serveur (installée dans /usr/local/bin/bot). Tape « bot » pour l'aide.
set -uo pipefail
umask 022

CONFIG=/etc/hockey-bot
SECRETS=$CONFIG/secrets.env
ETAT=/var/lib/hockey-bot
SERVICE=hockey-bot
DEPOT=/opt/hockey-bot/depot.git

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

gras() { printf '\033[1m%s\033[0m\n' "$*"; }
vert() { printf '\033[1;32m%s\033[0m\n' "$*"; }
rouge() { printf '\033[1;31m%s\033[0m\n' "$*"; }

aide() {
    gras "Commandes du bot :"
    echo "  bot etat        est-ce que le bot marche ? quelle version ?"
    echo "  bot journal     voir ce que fait le bot en direct (Ctrl+C pour sortir)"
    echo "  bot redemarrer  redémarrer le bot"
    echo "  bot secrets     changer le jeton Discord ou le compte SHM du bot"
    echo "  bot retour      revenir à la version d'avant (et mettre les mises à jour en pause)"
    echo "  bot reprendre   relancer les mises à jour automatiques (installe la dernière version)"
}

# Lit une valeur déjà enregistrée dans secrets.env (écrite par ligne_secret).
valeur() {
    grep -m1 "^$1=" "$SECRETS" 2>/dev/null | cut -d= -f2- | python3 -c 'import re,sys
v = sys.stdin.read().rstrip("\n")
if len(v) >= 2 and v[0] == v[-1] == "\"": v = re.sub(r"\\(.)", r"\1", v[1:-1])
print(v, end="")' || true
}

# Une ligne NOM="valeur" lue telle quelle par systemd, même avec des " \ $ ou espaces dans la valeur.
ligne_secret() {
    local v=${2//\\/\\\\}
    v=${v//\"/\\\"}
    printf '%s="%s"\n' "$1" "$v"
}

# Demande au clavier (aussi quand le script arrive par « curl | bash »).
demander() {   # demander "question" cache(0/1) → réponse dans $REPONSE
    REPONSE=""
    printf '%s' "$1" > /dev/tty
    if [ "$2" = 1 ]; then IFS= read -rs REPONSE < /dev/tty; echo > /dev/tty; else IFS= read -r REPONSE < /dev/tty; fi
    REPONSE=$(printf '%s' "$REPONSE" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
}

# Demande à Discord à quel bot appartient ce jeton. Affiche son nom, ou rien si le jeton est mauvais.
nom_du_bot() {
    curl -s --max-time 15 -H "Authorization: Bot $1" https://discord.com/api/v10/users/@me \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("username",""))
except Exception: pass' 2>/dev/null
}

secrets() {
    local installation=${1:-}
    local jeton shm_user shm_pass shm_cle nom
    jeton=$(valeur DISCORD_TOKEN); shm_user=$(valeur SHM_USERNAME); shm_pass=$(valeur SHM_PASSWORD); shm_cle=$(valeur SHM_BOT_KEY)

    if [ "$installation" = "--installation" ] && [ -n "$jeton" ]; then
        echo "Les secrets sont déjà enregistrés (pour les changer plus tard : bot secrets)."
        return 0
    fi

    echo
    gras "1) Le jeton du bot Discord"
    echo "   (Discord Developer Portal > ton application > Bot > Reset Token, puis Copy)"
    echo "   Colle-le ici puis appuie sur Entrée. Rien ne s'affiche quand tu colles : c'est normal."
    [ -n "$jeton" ] && echo "   Entrée sans rien coller = garder le jeton actuel."
    while true; do
        demander "   Jeton : " 1
        if [ -z "$REPONSE" ] && [ -n "$jeton" ]; then break; fi
        if [ -z "$REPONSE" ]; then rouge "   Rien n'a été collé. Réessaie (clic droit > Coller, ou Ctrl+Shift+V)."; continue; fi
        echo "   Vérification auprès de Discord…"
        nom=$(nom_du_bot "$REPONSE")
        if [ -n "$nom" ]; then vert "   ✅ Jeton bon : c'est le bot « $nom »."; jeton=$REPONSE; break; fi
        rouge "   ❌ Discord refuse ce jeton (${#REPONSE} caractères reçus). Recopie-le et réessaie."
    done

    echo
    gras "2) Le compte SHM du bot (pour les annonces SHM automatiques)"
    if [ -n "$shm_user" ]; then echo "   Entrée sans rien écrire = garder « $shm_user »."; else echo "   Entrée sans rien écrire = pas de compte SHM."; fi
    demander "   Nom d'utilisateur SHM : " 0
    if [ -n "$REPONSE" ]; then
        shm_user=$REPONSE
        shm_pass=""
    fi
    if [ -n "$shm_user" ]; then
        while true; do
            [ -n "$shm_pass" ] && echo "   Entrée sans rien écrire = garder le mot de passe actuel."
            demander "   Mot de passe SHM (rien ne s'affiche, c'est normal) : " 1
            if [ -n "$REPONSE" ]; then shm_pass=$REPONSE; break; fi
            [ -n "$shm_pass" ] && break
            rouge "   Il faut le mot de passe avec le nom d'utilisateur."
        done
        echo
        gras "3) La clé du bot pour SHM (facultatif)"
        if [ -n "$shm_cle" ]; then echo "   Entrée sans rien écrire = garder la clé actuelle."; else echo "   Entrée sans rien écrire = pas de clé."; fi
        demander "   Clé : " 1
        [ -n "$REPONSE" ] && shm_cle=$REPONSE
    fi

    umask 077
    mkdir -p "$CONFIG"
    {
        echo "# Secrets du bot. Lisible seulement par root. Pour les changer : bot secrets"
        ligne_secret DISCORD_TOKEN "$jeton"
        ligne_secret SHM_USERNAME "$shm_user"
        ligne_secret SHM_PASSWORD "$shm_pass"
        ligne_secret SHM_BOT_KEY "$shm_cle"
    } > "$SECRETS.tmp"
    chmod 600 "$SECRETS.tmp"
    mv "$SECRETS.tmp" "$SECRETS"
    vert "Secrets enregistrés sur le serveur (fichier $SECRETS)."
    if [ "$installation" != "--installation" ]; then
        redemarrer
    fi
}

redemarrer() {
    echo "Redémarrage du bot…"
    local depart; depart=$(date +%s)
    systemctl restart "$SERVICE"
    for _ in $(seq 1 24); do
        sleep 5
        if journalctl -u "$SERVICE" --since "@$depart" -o cat --no-pager | grep -q "Connecté en tant que"; then
            vert "✅ Le bot est en ligne."
            return 0
        fi
    done
    rouge "❌ Le bot ne se connecte pas. Voici ses derniers messages :"
    journalctl -u "$SERVICE" --since "@$depart" -o cat --no-pager | tail -n 15
    return 1
}

etat() {
    local version
    version=$(basename "$(readlink -f /opt/hockey-bot/actuelle 2>/dev/null)" 2>/dev/null)
    if systemctl is-active --quiet "$SERVICE"; then
        vert "✅ Le bot tourne (depuis $(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" | cut -d' ' -f2-3))."
    else
        rouge "❌ Le bot ne tourne pas."
    fi
    if [ -n "$version" ]; then
        echo "Version : ${version:0:7} « $(git --git-dir="$DEPOT" log -1 --format='%s' "$version" 2>/dev/null | cut -c1-80) »"
    fi
    if [ -f "$CONFIG/pause" ]; then
        rouge "Mises à jour automatiques : EN PAUSE (bot reprendre pour les relancer)."
    else
        echo "Mises à jour automatiques : actives (vérification toutes les 3 minutes)."
    fi
    [ -f "$ETAT/dernier-resultat" ] && echo "Dernière mise à jour : $(cat "$ETAT/dernier-resultat")"
    echo "Mémoire libre : $(free -m | awk '/^Mem:/ {print $7}') Mo · Disque libre : $(df -h / | awk 'NR==2 {print $4}')"
    echo
    gras "Derniers messages du bot :"
    journalctl -u "$SERVICE" -n 12 -o short --no-pager | cut -c1-200
}

case "${1:-}" in
    etat|état|status)        etat ;;
    journal|logs)            echo "(Ctrl+C pour sortir)"; journalctl -u "$SERVICE" -f -n 30 -o short ;;
    redemarrer|redémarrer)   redemarrer ;;
    secrets)                 secrets "${2:-}" ;;
    retour)                  /usr/local/sbin/hockey-bot-maj --retour ;;
    reprendre|maj)           echo "Recherche de la dernière version (jusqu'à 5 minutes)…"; /usr/local/sbin/hockey-bot-maj --forcer ;;
    *)                       aide ;;
esac

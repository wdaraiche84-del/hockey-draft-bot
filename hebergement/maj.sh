#!/usr/bin/env bash
# Mise à jour automatique du bot sur le serveur (installé dans /usr/local/sbin/hockey-bot-maj).
#
# Lancé toutes les 3 minutes par hockey-bot-maj.timer. Si la branche suivie (main) a changé sur GitHub :
#   1. la nouvelle version est copiée dans son propre dossier (l'ancienne reste intacte) ;
#   2. ses bibliothèques sont installées dans un environnement Python à part (réutilisé si
#      requirements.txt n'a pas changé) ;
#   3. tous les tests du bot passent, sinon la version est refusée et le bot n'est pas touché ;
#   4. le bot redémarre sur la nouvelle version, et doit se connecter à Discord en moins de 2 minutes ;
#   5. sinon, retour automatique à la version d'avant, et la version fautive n'est plus réessayée.
#
# Usage :
#   hockey-bot-maj              vérifier et mettre à jour si besoin (ce que fait le minuteur)
#   hockey-bot-maj --forcer     installer la dernière version tout de suite, même refusée ou en pause
#   hockey-bot-maj --retour     revenir à la version d'avant et mettre les mises à jour en pause
set -uo pipefail
umask 022

BASE=/opt/hockey-bot
DEPOT=$BASE/depot.git
VERSIONS=$BASE/versions
VENVS=$BASE/venvs
ACTUELLE=$BASE/actuelle
ETAT=/var/lib/hockey-bot
CONFIG=/etc/hockey-bot
SERVICE=hockey-bot
UTILISATEUR=hockeybot
BRANCHE=$(cat "$CONFIG/branche" 2>/dev/null || echo main)
ATTENTE_CONNEXION=120         # secondes pour que le bot se connecte à Discord après un redémarrage
LIGNE_CONNECTE="Connecté en tant que"

journal() {
    local texte="$(date '+%Y-%m-%d %H:%M') $*"
    echo "$texte"
    echo "$texte" >> "$ETAT/historique.log"
    tail -n 200 "$ETAT/historique.log" > "$ETAT/historique.tmp" && mv "$ETAT/historique.tmp" "$ETAT/historique.log"
}

resultat() {   # La dernière chose importante, montrée par « bot etat ».
    echo "$(date '+%Y-%m-%d %H:%M') $*" > "$ETAT/dernier-resultat"
    journal "$*"
}

version_actuelle() { basename "$(readlink -f "$ACTUELLE" 2>/dev/null)" 2>/dev/null || true; }

titre_version() { git --git-dir="$DEPOT" log -1 --format='%s' "$1" 2>/dev/null | cut -c1-80; }

# Prépare le dossier et l'environnement Python d'une version. Ne touche pas au bot qui tourne.
preparer() {
    local sha=$1 dossier=$VERSIONS/$1
    if [ ! -f "$dossier/.prete" ]; then
        rm -rf "$dossier" "$dossier.tmp"
        mkdir -p "$dossier.tmp"
        git --git-dir="$DEPOT" archive "$sha" | tar -x --no-same-permissions --no-same-owner -C "$dossier.tmp" || { journal "Copie de $sha impossible."; return 1; }
        mv "$dossier.tmp" "$dossier"
    fi
    local cle venv
    cle=$(sha256sum "$dossier/requirements.txt" | cut -c1-16)
    venv=$VENVS/$cle
    if [ ! -x "$venv/bin/python" ] || [ ! -f "$venv/.pret" ]; then
        journal "Installation des bibliothèques Python (quelques minutes)…"
        rm -rf "$venv"
        python3 -m venv "$venv" || return 1
        "$venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip \
            && "$venv/bin/pip" install --quiet --disable-pip-version-check -r "$dossier/requirements.txt" pytest \
            || { journal "Échec de l'installation des bibliothèques."; rm -rf "$venv"; return 1; }
        touch "$venv/.pret"
    fi
    ln -sfn "$venv" "$dossier/.venv"
    "$venv/bin/python" -m compileall -q "$dossier/draft_bot" > /dev/null || true
    touch "$dossier/.prete"
}

# Tous les tests du bot, lancés par l'utilisateur du bot (pas root), dans un dossier temporaire.
tester() {
    local dossier=$VERSIONS/$1 tmp code
    tmp=$(mktemp -d /tmp/hockey-bot-tests.XXXXXX)
    chown "$UTILISATEUR:" "$tmp"
    (cd "$dossier" && timeout 1200 runuser -u "$UTILISATEUR" -- env -i PATH=/usr/bin:/bin HOME="$tmp" TMPDIR="$tmp" \
        PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 TZ=America/Toronto \
        "$dossier/.venv/bin/python" -m pytest -q -p no:cacheprovider -x > "$ETAT/derniers-tests.log" 2>&1)
    code=$?
    rm -rf "$tmp"
    tail -n 3 "$ETAT/derniers-tests.log" | sed 's/^/    /'
    return $code
}

# Redémarre le bot et attend qu'il soit connecté à Discord.
demarrer_et_verifier() {
    local depart fin
    depart=$(date +%s)
    systemctl restart "$SERVICE"
    fin=$((depart + ATTENTE_CONNEXION))
    while [ "$(date +%s)" -lt "$fin" ]; do
        sleep 5
        if journalctl -u "$SERVICE" --since "@$depart" -o cat --no-pager 2>/dev/null | grep -q "$LIGNE_CONNECTE"; then
            sleep 10   # encore quelques secondes : il ne doit pas planter juste après
            systemctl is-active --quiet "$SERVICE" && return 0
            return 1
        fi
    done
    return 1
}

basculer() { ln -sfn "$VERSIONS/$1" "$ACTUELLE.tmp" && mv -T "$ACTUELLE.tmp" "$ACTUELLE"; }

nettoyer() {   # Garde la version actuelle et celle d'avant (pour « bot retour »), et leurs environnements Python.
    local actuelle precedente utilises=""
    actuelle=$(version_actuelle)
    precedente=$(cat "$ETAT/precedente" 2>/dev/null || true)
    for v in $(ls -1 "$VERSIONS" 2>/dev/null); do
        if [ "$v" != "$actuelle" ] && [ "$v" != "$precedente" ]; then rm -rf "${VERSIONS:?}/$v"; fi
    done
    for v in $(ls -1 "$VERSIONS" 2>/dev/null); do utilises+=" $(basename "$(readlink -f "$VERSIONS/$v/.venv")")"; done
    for e in $(ls -1 "$VENVS" 2>/dev/null); do
        grep -qw "$e" <<< "$utilises" || rm -rf "${VENVS:?}/$e"
    done
}

deployer() {
    local sha=$1 precedente
    precedente=$(version_actuelle)
    journal "Nouvelle version trouvée : ${sha:0:7} « $(titre_version "$sha") »"
    if ! preparer "$sha"; then
        resultat "❌ Version ${sha:0:7} pas installée : les bibliothèques n'ont pas pu être installées. Le bot continue sur l'ancienne version. Nouvel essai au prochain passage."
        return 1
    fi
    journal "Tests en cours (1 à 3 minutes)…"
    if ! tester "$sha"; then
        echo "$sha" > "$ETAT/refusee"
        resultat "❌ Version ${sha:0:7} refusée : des tests ne passent pas. Le bot continue sur l'ancienne version, rien n'a changé."
        return 1
    fi
    journal "Tests réussis. Redémarrage du bot sur la nouvelle version…"
    basculer "$sha"
    if demarrer_et_verifier; then
        [ -n "$precedente" ] && [ "$precedente" != "$sha" ] && echo "$precedente" > "$ETAT/precedente"
        rm -f "$ETAT/refusee"
        resultat "✅ Bot à jour : version ${sha:0:7} « $(titre_version "$sha") »."
        nettoyer
        return 0
    fi
    if [ -z "$precedente" ] || [ "$precedente" = "$sha" ]; then
        resultat "❌ Le bot ne se connecte pas à Discord. Vérifie le jeton avec : bot secrets"
        return 1
    fi
    journal "Le bot ne se connecte pas sur la nouvelle version : retour à ${precedente:0:7}…"
    basculer "$precedente"
    if demarrer_et_verifier; then
        echo "$sha" > "$ETAT/refusee"
        resultat "❌ Version ${sha:0:7} refusée : le bot ne démarrait pas avec. Retour automatique à la version d'avant (${precedente:0:7}), le bot marche."
    else
        # Même l'ancienne version ne se connecte pas : le problème vient d'ailleurs (Discord en panne,
        # jeton changé…). On ne refuse pas la nouvelle version : elle sera réessayée.
        rm -f "$ETAT/refusee"
        resultat "❌ Le bot ne se connecte pas à Discord, même sur l'ancienne version (Discord en panne ou jeton changé ?). Nouvel essai au prochain passage."
    fi
    return 1
}

retour() {
    local precedente
    precedente=$(cat "$ETAT/precedente" 2>/dev/null || true)
    if [ -z "$precedente" ] || [ ! -f "$VERSIONS/$precedente/.prete" ]; then
        echo "Il n'y a pas de version d'avant à remettre."
        return 1
    fi
    local mauvaise
    mauvaise=$(version_actuelle)
    touch "$CONFIG/pause"
    basculer "$precedente"
    echo "$mauvaise" > "$ETAT/precedente"
    if demarrer_et_verifier; then
        resultat "↩️ Retour à la version ${precedente:0:7} « $(titre_version "$precedente") ». Mises à jour automatiques en pause (bot reprendre pour les relancer)."
    else
        resultat "❌ Retour à ${precedente:0:7} fait, mais le bot ne se connecte pas. Regarde : bot etat"
        return 1
    fi
}

principal() {
    mkdir -p "$ETAT" "$VERSIONS" "$VENVS"
    local mode=${1:-}
    if [ "$mode" = "--retour" ]; then retour; return; fi
    if [ "$mode" != "--forcer" ] && [ -f "$CONFIG/pause" ]; then return 0; fi

    if ! git --git-dir="$DEPOT" fetch --quiet --prune origin "+refs/heads/$BRANCHE:refs/heads/$BRANCHE" 2>"$ETAT/git.err"; then
        # GitHub ne répond pas : pas grave, on réessaie au prochain passage, sans remplir le journal.
        [ "$mode" = "--forcer" ] && { echo "GitHub ne répond pas :"; cat "$ETAT/git.err"; return 1; }
        return 0
    fi
    local sha
    sha=$(git --git-dir="$DEPOT" rev-parse "refs/heads/$BRANCHE")
    if [ "$mode" = "--forcer" ]; then
        rm -f "$CONFIG/pause" "$ETAT/refusee"
        if [ "$sha" = "$(version_actuelle)" ]; then
            if systemctl is-active --quiet "$SERVICE"; then
                echo "Le bot est déjà sur la dernière version (${sha:0:7}) et il tourne."
                return 0
            fi
            demarrer_et_verifier && resultat "✅ Bot redémarré sur la version ${sha:0:7}." && return 0
            resultat "❌ Le bot ne se connecte pas à Discord. Vérifie le jeton avec : bot secrets"
            return 1
        fi
        deployer "$sha"
        return
    fi
    [ "$sha" = "$(version_actuelle)" ] && return 0
    [ "$sha" = "$(cat "$ETAT/refusee" 2>/dev/null)" ] && return 0
    deployer "$sha"
}

# Une seule mise à jour à la fois (le minuteur et les commandes « bot » partagent ce verrou).
exec 9> /run/hockey-bot-maj.lock
if ! flock -n 9; then
    [ -t 1 ] && echo "Une mise à jour est déjà en cours. Réessaie dans quelques minutes."
    exit 0
fi
principal "$@"

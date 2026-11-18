# DoktorTV Bot

Bot Telegram **privé** pour piloter un serveur multimédia maison (NAS) :
Jellyfin, Jellyseerr, Radarr, Sonarr, Transmission, cross-seed et Purgearr — depuis ton téléphone.

> Le bot ne répond qu'à **un seul compte** : son propriétaire. Tout autre utilisateur reçoit « Bot privé ».

## Ce qu'il fait

| Menu | Fonctions |
|---|---|
| 🔎 **Rechercher / demander** | Recherche dans Jellyseerr, fiche avec affiche, demande d'un film ou de saisons choisies. |
| 📥 **Téléchargements** | File d'attente Radarr/Sonarr avec progression et temps restant. |
| 🆕 **Derniers ajouts** · 📅 **À venir** | Imports récents, calendrier des épisodes et des sorties de films. |
| 🖥️ **Serveur** · 🐳 **Conteneurs** | Charge, RAM, température, espace disque ; état et redémarrage des conteneurs Docker. |
| 🔄 **Scanner Jellyfin** | Relance l'analyse de la bibliothèque. |
| 🧹 **Ménage** *([Purgearr](https://github.com/Lekarov/Purgearr))* | Suggestions de suppression une par une (films vus, séries vues ou entamées, jamais regardés, les plus lourds), fiche détaillée, **aperçu exact** de ce qui sera supprimé (bibliothèque, torrents, hardlinks, liens cross-seed, copies), confirmation, protection d'un titre. |
| 🔁 **Cross-seed** | Lancer une recherche ou un scan RSS tout de suite, tout re-chercher, suivre l'avancement ; message à la fin de la recherche. |
| 🧲 **Transmission** | Vitesses, ratio global, top seeds, actifs, inactifs, torrents à problème (passkey, retirés du tracker, données abîmées) avec revérifier / relancer / retirer, mode tortue, pause générale. |
| 🩺 **Santé du serveur** | SMART de chaque disque, températures, usure SSD, services en échec, test SMART hebdomadaire. |

**Notifications automatiques** : nouveau film/épisode disponible, téléchargement lancé, demandes Jellyseerr, disque presque plein, conteneur arrêté, disque qui faiblit (secteurs réalloués, erreurs, surchauffe, test SMART raté).

## Installation

👉 **Guide complet pas à pas : [docs/INSTALLATION.md](docs/INSTALLATION.md)**

En bref :

```bash
git clone https://github.com/Lekarov/DoktorTV-Bot.git /opt/doktortv-bot
cd /opt/doktortv-bot
cp .env.example .env && nano .env        # remplir jeton, chemins, clés
docker compose up -d --build
sudo ./host/install-health.sh /opt/doktortv-bot/data   # facultatif : menu 🩺
```

Puis envoie `/start` à ton bot : tu deviens son propriétaire.

## Sécurité

- **Aucun secret dans ce dépôt.** Jeton, clés API et mots de passe vont uniquement dans `.env`, ignoré par Git.
- Les clés Radarr/Sonarr/Jellyseerr/cross-seed sont **lues à chaud, en lecture seule**, dans leurs dossiers de configuration montés en `:ro`.
- Les webhooks sont protégés par un secret dans l'URL et doivent rester sur le **réseau local**.
- Le bot a accès au socket Docker (état/redémarrage des conteneurs) : c'est un accès puissant, réserve le serveur à ton réseau privé.
- Le bot **ne supprime jamais de fichiers lui-même** : le ménage passe par Purgearr, après aperçu et confirmation ; « retirer » un torrent ne touche pas aux données.

Détails : [docs/INSTALLATION.md#sécurité](docs/INSTALLATION.md#12-sécurité).

## Prérequis

- Un serveur Linux avec **Docker** et **Docker Compose v2**.
- Les services que tu veux piloter (au minimum Radarr, Sonarr et Jellyseerr ; les autres sont facultatifs).
- Pour 🩺 : `smartmontools` et `python3` sur l'hôte.
- Pour 🧹 : [Purgearr](https://github.com/Lekarov/Purgearr) ≥ 1.2.0 (routes `/api/bot/*`).

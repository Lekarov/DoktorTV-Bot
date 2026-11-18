# Guide d'installation de DoktorTV Bot

Ce guide part de zéro et va jusqu'au bot entièrement fonctionnel. Compte 30 à 45 minutes.
Chaque étape indique **quoi faire**, **pourquoi**, et **comment vérifier** que ça marche.

## Sommaire

1. [Ce qu'il te faut](#1-ce-quil-te-faut)
2. [Créer le bot Telegram](#2-créer-le-bot-telegram)
3. [Récupérer le code](#3-récupérer-le-code)
4. [Repérer les dossiers de configuration de tes services](#4-repérer-les-dossiers-de-configuration-de-tes-services)
5. [Créer une clé API Jellyfin](#5-créer-une-clé-api-jellyfin)
6. [Remplir le fichier `.env`](#6-remplir-le-fichier-env)
7. [Lancer le bot et devenir propriétaire](#7-lancer-le-bot-et-devenir-propriétaire)
8. [Brancher les notifications (webhooks)](#8-brancher-les-notifications-webhooks)
9. [Activer la santé du serveur (🩺)](#9-activer-la-santé-du-serveur-)
10. [Modules facultatifs : Transmission, cross-seed, Purgearr](#10-modules-facultatifs)
11. [Mettre à jour, sauvegarder, désinstaller](#11-mettre-à-jour-sauvegarder-désinstaller)
12. [Sécurité](#12-sécurité)
13. [Dépannage](#13-dépannage)

---

## 1. Ce qu'il te faut

| Élément | Obligatoire | Pour quoi |
|---|---|---|
| Serveur Linux (Debian, Ubuntu, OpenMediaVault…) | ✅ | Héberger le bot |
| Docker + Docker Compose **v2** (`docker compose`, avec un espace) | ✅ | Lancer le bot |
| Un compte Telegram | ✅ | Parler au bot |
| Radarr, Sonarr, Jellyseerr | ✅ | Recherche, demandes, téléchargements, calendrier |
| Jellyfin | conseillé | Scan de bibliothèque |
| Transmission | facultatif | Menu 🧲 |
| cross-seed (v6) en conteneur | facultatif | Menu 🔁 |
| Purgearr ≥ 1.1.0 | facultatif | Menu 🧹 |
| `smartmontools` + `python3` sur l'hôte | facultatif | Menu 🩺 |

Vérifie Docker :

```bash
docker --version
docker compose version     # doit afficher "Docker Compose version v2…"
```

> Si `docker compose` n'existe pas mais `docker-compose` oui, tu as l'ancienne version 1 : installe le paquet `docker-compose-plugin`.

---

## 2. Créer le bot Telegram

1. Dans Telegram, ouvre une conversation avec **@BotFather** (compte officiel, badge bleu).
2. Envoie `/newbot`.
3. Donne un **nom affiché** (ex. `Mon NAS`), puis un **identifiant** qui finit par `bot` (ex. `MonNas_Bot`).
4. BotFather répond avec un **jeton** du type `1234567890:AAH...`.
   **C'est un mot de passe** : quiconque l'a peut contrôler ton bot. Ne le colle nulle part ailleurs que dans `.env`.
5. Facultatif mais conseillé, toujours chez BotFather :
   - `/setprivacy` → ton bot → `Enable` (il ne lit pas les groupes) ;
   - `/setjoingroups` → ton bot → `Disable` (personne ne peut l'ajouter à un groupe).

---

## 3. Récupérer le code

On l'installe dans `/opt/doktortv-bot` (tu peux choisir un autre dossier, adapte alors les chemins du guide).

```bash
sudo git clone https://github.com/Lekarov/DoktorTV-Bot.git /opt/doktortv-bot
cd /opt/doktortv-bot
sudo cp .env.example .env
sudo chmod 600 .env          # seul root peut lire les secrets
```

Contenu du dossier :

```
bot.py                 le bot
Dockerfile             image Python 3.12
docker-compose.yml     lancement (tout est réglé via .env)
.env.example           modèle de configuration commenté
host/                  relevé de santé du serveur (installé sur l'hôte, hors Docker)
docs/INSTALLATION.md   ce guide
data/                  créé au premier lancement : propriétaire, état, relevés (jamais publié)
```

---

## 4. Repérer les dossiers de configuration de tes services

Le bot **ne te demande pas** de recopier les clés API de Radarr, Sonarr et Jellyseerr : il les lit directement, en lecture seule, dans leurs fichiers de configuration. Si tu régénères une clé, le bot suit tout seul.

Trouve où chaque conteneur range sa configuration :

```bash
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' radarr
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' sonarr
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' jellyseerr
```

Repère la ligne qui finit par `-> /config` (ou `-> /app/config` pour Jellyseerr). La partie **à gauche** est le dossier à noter.

Vérifie que les bons fichiers sont dedans :

```bash
ls /chemin/radarr/config.xml        # Radarr
ls /chemin/sonarr/config.xml        # Sonarr
ls /chemin/jellyseerr/settings.json # Jellyseerr (ou Overseerr)
```

Note aussi **le dossier où sont stockés tes médias** (ex. `/srv/storage`, ou un pool mergerfs) : le bot l'utilise pour afficher l'espace libre.

---

## 5. Créer une clé API Jellyfin

1. Jellyfin → **Tableau de bord** → **Clés API** → **+**.
2. Nom : `DoktorTV Bot` → **OK**, puis copie la clé.

> Jellyfin 10.11+ n'accepte plus les anciennes méthodes d'authentification (`X-Emby-Token`, `?api_key=`). Le bot utilise l'en-tête moderne `Authorization: MediaBrowser Token="…"`, rien à régler.

---

## 6. Remplir le fichier `.env`

```bash
sudo nano /opt/doktortv-bot/.env
```

Champ par champ :

| Variable | Exemple | Explication |
|---|---|---|
| `BOT_TOKEN` | `1234567890:AAH…` | Jeton de l'étape 2. **Secret.** |
| `OWNER_ID` | *(vide)* | Laisse vide : le premier `/start` deviendra propriétaire (étape 7). |
| `NAS_HOST` | `192.168.1.10` | IP locale du serveur où tournent Radarr, Sonarr… (`hostname -I` pour la trouver). |
| `BIND_IP` | `192.168.1.10` | IP sur laquelle exposer les webhooks. Mets l'IP **locale**, jamais une IP publique. |
| `HOOK_PORT` | `8099` | Port des webhooks. |
| `HOOK_SECRET` | *(généré)* | Génère-le : `openssl rand -hex 24`. **Secret.** |
| `RADARR_CONFIG_DIR` | `/srv/appdata/radarr` | Dossier de l'étape 4. |
| `SONARR_CONFIG_DIR` | `/srv/appdata/sonarr` | Dossier de l'étape 4. |
| `SEERR_CONFIG_DIR` | `/srv/appdata/jellyseerr` | Dossier de l'étape 4. |
| `CROSSSEED_CONFIG_DIR` | *(vide ou chemin)* | Dossier contenant `config.js` de cross-seed (étape 10). |
| `STORAGE_PATH` | `/srv/storage` | Stockage des médias. |
| `RADARR_PORT` / `SONARR_PORT` / `SEERR_PORT` | `7878` / `8989` / `5055` | À changer seulement si tes ports diffèrent. |
| `JELLYFIN_URL` | `http://192.168.1.10:8096` | Facultatif (par défaut `NAS_HOST:8096`). |
| `JELLYFIN_KEY` | *(clé)* | Clé de l'étape 5. **Secret.** |
| `TRANSMISSION_*` | | Voir étape 10. |
| `PURGEARR_URL` | | Voir étape 10. |
| `DISK_ALERT_PCT` | `90` | Alerte quand un stockage dépasse ce pourcentage. |

Règles du fichier :
- une variable par ligne, **sans espace** autour du `=` ;
- pas de guillemets nécessaires, même avec des espaces dans un chemin ;
- les lignes qui commencent par `#` sont des commentaires.

---

## 7. Lancer le bot et devenir propriétaire

```bash
cd /opt/doktortv-bot
sudo docker compose up -d --build
sudo docker compose logs -f
```

Tu dois voir :

```
INFO doktortv: DoktorTV_Bot prêt, webhooks sur :8099
INFO telegram.ext.Application: Application started
```

(`Ctrl+C` pour quitter l'affichage des journaux, le bot continue de tourner.)

Dans Telegram, ouvre ton bot et envoie **`/start`** :

> 🔐 Tu es enregistré comme propriétaire de ce bot. Il ne répondra qu'à toi.

Ton identifiant est enregistré dans `data/owner.json`. Pour le figer définitivement, copie-le dans `OWNER_ID` :

```bash
sudo cat /opt/doktortv-bot/data/owner.json     # {"id": 123456789, ...}
```

**Vérifications rapides** : touche 🖥️ Serveur (CPU, RAM, disques), 📥 Téléchargements, puis envoie un titre de film : la recherche Jellyseerr doit répondre.

---

## 8. Brancher les notifications (webhooks)

Remplace `IP`, `PORT` et `SECRET` par `BIND_IP`, `HOOK_PORT` et `HOOK_SECRET` de ton `.env`.

### Radarr
**Settings → Connect → + → Webhook**
- Name : `DoktorTV Bot`
- Triggers : ✅ On Grab, ✅ On File Import, ✅ On File Upgrade
- Webhook URL : `http://IP:PORT/hook/SECRET/radarr`
- Method : `POST`
- **Test** → tu reçois « 🧪 Test Radarr reçu ✅ » → **Save**

### Sonarr
Même chose avec l'URL `http://IP:PORT/hook/SECRET/sonarr` → « 🧪 Test Sonarr reçu ✅ ».

### Jellyseerr / Overseerr
**Paramètres → Notifications → Webhook**
- ✅ Activer
- URL : `http://IP:PORT/hook/SECRET/seerr`
- Laisse le modèle JSON par défaut.
- Types : en attente, approuvée, disponible, échec, refusée, approuvée automatiquement
- **Tester** → « 🧪 … » → **Enregistrer**

> Si le test échoue alors que le bot tourne : Radarr/Sonarr/Jellyseerr sont souvent dans un réseau Docker et doivent joindre l'**IP locale du serveur** (pas `localhost`). Vérifie `BIND_IP`.

---

## 9. Activer la santé du serveur (🩺)

Un conteneur ne peut pas lire l'état SMART des disques. Un petit script tourne donc **sur l'hôte**, toutes les 10 minutes, et écrit `data/health.json` que le bot lit.

```bash
sudo apt install smartmontools python3      # si absents
cd /opt/doktortv-bot
sudo ./host/install-health.sh /opt/doktortv-bot/data
```

Ce que ça installe :
- `/usr/local/bin/doktortv-health.py` — **lecture seule** : `smartctl -n standby` ne réveille pas un disque en veille ;
- `doktortv-health.timer` — relevé toutes les 10 minutes ;
- `doktortv-smart-test.timer` — **test SMART court** chaque dimanche à 4 h 30 (~2 minutes par disque, sans risque pour les données).

Vérifie :

```bash
systemctl list-timers | grep doktortv
sudo cat /opt/doktortv-bot/data/health.json | head
```

Dans le bot : 🩺 **Santé du NAS**. Les alertes (SMART en échec, secteurs abîmés qui augmentent, surchauffe, test raté, nouveau service en panne) arrivent toutes seules, **une fois par problème**.

Désinstaller : `sudo ./host/install-health.sh --uninstall`

---

## 10. Modules facultatifs

### 🧲 Transmission
Dans `.env` :

```
TRANSMISSION_URL=http://192.168.1.10:9091/transmission/rpc
TRANSMISSION_USER=ton_utilisateur
TRANSMISSION_PASS=ton_mot_de_passe
```

Ce sont les identifiants de l'interface web de Transmission (`rpc-username` / `rpc-password` dans `settings.json`).

Ce que le bot peut faire : revérifier, relancer, **retirer** un torrent (les fichiers restent toujours sur le disque), mode tortue, mise en pause générale. Il retient lesquels tournaient avant la pause, pour ne relancer que ceux-là.

### 🔁 cross-seed (v6)
1. `CROSSSEED_CONFIG_DIR` = dossier qui contient `config.js` (monté en lecture seule, pour lire `apiKey`).
2. `CROSSSEED_CONTAINER` = nom du conteneur (`docker ps`), par défaut `cross-seed`.
3. `CROSSSEED_PORT` = port de l'API (par défaut `2468`), joignable depuis `NAS_HOST`.

Si `apiKey` n'est pas défini dans `config.js`, ajoute-le (une chaîne d'au moins 24 caractères) puis redémarre cross-seed.

Bon à savoir : cross-seed n'accepte une passe anticipée que si la prochaine passe planifiée est assez proche. Sinon le bot répond « ⏳ impossible maintenant » : ce n'est pas une panne.

### 🧹 Purgearr
1. Installe [Purgearr](https://github.com/Lekarov/Purgearr) **1.1.0 ou plus** (il fournit les routes `/api/bot/*`).
2. Dans `.env` : `PURGEARR_URL=http://192.168.1.10:7979`.
3. Vérifie : `curl http://192.168.1.10:7979/api/bot/candidates?cat=lourds | head -c 300`.

Le bot ne supprime jamais rien lui-même. Il demande à Purgearr un **aperçu exact** (bibliothèque, torrents par tracker, hardlinks, liens cross-seed, copies), te le montre, puis demande ta confirmation.

---

## 11. Mettre à jour, sauvegarder, désinstaller

**Mettre à jour**

```bash
cd /opt/doktortv-bot
sudo git pull
sudo docker compose up -d --build
sudo ./host/install-health.sh /opt/doktortv-bot/data    # si host/ a changé
```

**À sauvegarder** : `.env` (secrets) et `data/` (propriétaire, état). Le reste se retélécharge.

**Désinstaller**

```bash
cd /opt/doktortv-bot
sudo docker compose down
sudo ./host/install-health.sh --uninstall
sudo rm -rf /opt/doktortv-bot
```

Pense aussi à retirer les webhooks dans Radarr, Sonarr et Jellyseerr, et à révoquer le jeton chez @BotFather (`/revoke`).

---

## 12. Sécurité

- **Bot privé** : un seul propriétaire. Les autres comptes reçoivent « ⛔ Bot privé » et aucun bouton ne fonctionne pour eux.
- **Secrets** : uniquement dans `.env` (permissions `600`), ignoré par Git via `.gitignore`. Ne fais jamais `git add -f .env`. Si un secret a fuité :
  - jeton Telegram → `/revoke` chez @BotFather ;
  - clé Jellyfin → supprime-la dans Jellyfin et crée-en une autre ;
  - `HOOK_SECRET` → regénère-le et mets à jour les 3 webhooks.
- **Clés API lues en lecture seule** dans les dossiers de configuration montés en `:ro` : le bot ne peut pas modifier la configuration de Radarr, Sonarr, Jellyseerr ou cross-seed.
- **Webhooks** : protégés par `HOOK_SECRET` dans l'URL. Garde `BIND_IP` sur l'IP **locale** et **n'ouvre pas** `HOOK_PORT` sur ta box.
- **Socket Docker** : nécessaire pour l'état et le redémarrage des conteneurs. Il donne un accès étendu au serveur, d'où l'importance de la règle « un seul propriétaire » et d'un jeton bien gardé. Si tu n'en veux pas, retire la ligne `docker.sock` du `docker-compose.yml` : les menus 🐳 et 🔁 ne fonctionneront plus, le reste oui.
- **Suppressions** : aucune suppression de fichiers par le bot lui-même. Purgearr fait le ménage après aperçu et confirmation ; « retirer » un torrent garde les données.
- **Accès à distance** : pour gérer ton serveur hors de chez toi, Telegram suffit, aucun port à ouvrir. Pour le reste, préfère un VPN (WireGuard, Tailscale) à l'ouverture de ports.

---

## 13. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Le bot ne répond pas du tout | Jeton faux, conteneur arrêté | `docker compose logs --tail 50` ; « Invalid token » → recopie `BOT_TOKEN` |
| « ⛔ Bot privé » | Un autre compte a fait `/start` en premier | Mets ton id dans `OWNER_ID` (via [@userinfobot](https://t.me/userinfobot)), puis `docker compose up -d` |
| `docker compose up` : « à définir dans .env » | Variable de chemin vide | Remplis `RADARR_CONFIG_DIR`, `SONARR_CONFIG_DIR`, `SEERR_CONFIG_DIR`, `STORAGE_PATH` |
| « radarr HTTP 401 » | Mauvais dossier de config | Le dossier doit contenir `config.xml` ; vérifie `RADARR_CONFIG_DIR` |
| Recherche : « Jellyseerr injoignable » | Port ou IP | `curl http://NAS_HOST:5055/api/v1/status` depuis le serveur |
| « 🔄 Scanner Jellyfin » → HTTP 401 | Clé Jellyfin absente ou révoquée | Recrée une clé (étape 5) |
| Webhook « Test » qui échoue | IP de bind ou pare-feu | `curl -X POST http://BIND_IP:8099/hook/SECRET/radarr -d '{"eventType":"Test"}' -H 'Content-Type: application/json'` |
| 🧲 « Transmission HTTP 401 » | Identifiants | Vérifie `TRANSMISSION_USER/PASS` ; whitelist RPC de Transmission |
| 🔁 « clé API cross-seed introuvable » | `apiKey` absent ou mauvais dossier | `CROSSSEED_CONFIG_DIR` doit contenir `config.js` avec `apiKey` |
| 🔁 « ⏳ Impossible maintenant » | Règle de cross-seed | Normal : passe déjà en cours ou trop récente |
| 🩺 « Relevé indisponible » | Timer non installé | Étape 9 ; `systemctl status doktortv-health.service` |
| 🩺 Disque « 💤 en veille » | Disque endormi | Normal : dernières valeurs connues affichées, il n'est pas réveillé exprès |
| 🧹 « Purgearr HTTP 404 » | Purgearr trop ancien | Mets à jour Purgearr en 1.1.0 ou plus |

Journaux utiles :

```bash
docker compose -f /opt/doktortv-bot/docker-compose.yml logs --tail 100
journalctl -u doktortv-health.service -n 20
```

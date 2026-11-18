#!/usr/bin/env python3
"""DoktorTV_Bot — panneau de gestion Telegram du NAS DoktorTV.

Recherche/demande Jellyseerr, téléchargements, derniers ajouts, calendrier,
état du serveur, conteneurs Docker, ménage via Purgearr (suggestions, fiche,
détail exact de la suppression), et notifications (Radarr/Sonarr/Seerr + alertes).
Bot privé : seul le propriétaire (OWNER_ID, ou le premier /start) peut l'utiliser.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
from aiohttp import web
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
from yarl import URL

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("doktortv")

TOKEN = os.environ["BOT_TOKEN"]
HOOK_SECRET = os.environ["HOOK_SECRET"]
HOST = os.environ.get("NAS_HOST", "127.0.0.1")
HOOK_PORT = int(os.environ.get("HOOK_PORT", "8099"))
DISK_ALERT = int(os.environ.get("DISK_ALERT_PCT", "90"))
DATA = "/data"
OWNER_FILE = f"{DATA}/owner.json"
SVC = {"radarr": (int(os.environ.get("RADARR_PORT", "7878")), "/api/v3"),
       "sonarr": (int(os.environ.get("SONARR_PORT", "8989")), "/api/v3"),
       "seerr": (int(os.environ.get("SEERR_PORT", "5055")), "/api/v1")}
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", f"http://{os.environ.get('NAS_HOST', '127.0.0.1')}:8096").rstrip("/")
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
PURGEARR = os.environ.get("PURGEARR_URL", f"http://{HOST}:7979").rstrip("/")
SESSION: aiohttp.ClientSession | None = None
APP: Application | None = None
e = html.escape


# ---------------------------------------------------------------- propriétaire
def owner_id() -> int | None:
    if os.environ.get("OWNER_ID"):
        return int(os.environ["OWNER_ID"])
    try:
        return json.load(open(OWNER_FILE))["id"]
    except (OSError, ValueError, KeyError):
        return None


def is_owner(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid is not None and uid == owner_id()


# ---------------------------------------------------------------- accès aux APIs
def api_key(app: str) -> str:
    if app == "seerr":
        return json.load(open("/keys/seerr/settings.json"))["main"]["apiKey"]
    return re.search(r"<ApiKey>(.*?)</ApiKey>", open(f"/keys/{app}/config.xml").read()).group(1)


async def api(app: str, method: str, path: str, body=None):
    port, base = SVC[app]
    url = URL(f"http://{HOST}:{port}{base}/{path}", encoded=True)
    async with SESSION.request(method, url, json=body, headers={"X-Api-Key": api_key(app)},
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
        txt = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"{app} HTTP {r.status}: {txt[:200]}")
        return json.loads(txt) if txt.strip() else None


async def docker(method: str, path: str):
    conn = aiohttp.UnixConnector(path="/var/run/docker.sock")
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.request(method, f"http://docker{path}", timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 400:
                raise RuntimeError(f"docker HTTP {r.status}: {(await r.text())[:200]}")
            return None if r.status == 204 else await r.json()


async def jellyfin_scan():
    key = os.environ.get("JELLYFIN_KEY")
    if not key:
        raise RuntimeError("JELLYFIN_KEY absent du .env")
    async with SESSION.post(f"{JELLYFIN_URL}/Library/Refresh",
                            headers={"Authorization": f'MediaBrowser Token="{key}"'},
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status >= 400:
            raise RuntimeError(f"Jellyfin HTTP {r.status}")


# ---------------------------------------------------------------- utilitaires
def bar(pct: float, n: int = 10) -> str:
    full = max(0, min(n, round(pct / 100 * n)))
    return "▰" * full + "▱" * (n - full)


def human(b: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}" if unit not in ("o", "Ko") else f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} Po"


def disk(path: str):
    st = os.statvfs(path)
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    return total, free, (1 - free / total) * 100 if total else 0


BACK = [B("⬅️ Menu", callback_data="menu")]
MAIN = M([
    [B("🔎 Rechercher / demander", callback_data="search")],
    [B("📥 Téléchargements", callback_data="dl"), B("🆕 Derniers ajouts", callback_data="new")],
    [B("📅 À venir", callback_data="cal"), B("🖥️ Serveur", callback_data="srv")],
    [B("🐳 Conteneurs", callback_data="ctr"), B("🔄 Scanner Jellyfin", callback_data="scan")],
    [B("🧹 Ménage (Purgearr)", callback_data="pm"), B("🔁 Cross-seed", callback_data="cs")],
    [B("🧲 Transmission", callback_data="tr"), B("🩺 Santé du NAS", callback_data="hl")],
])
STATUS = {2: "⏳ demandé", 3: "⬇️ en cours", 4: "🟡 en partie dispo", 5: "✅ disponible", 6: "🗑️ supprimé"}


async def show(update: Update, text: str, kb: M | None):
    """Édite le message du bouton si possible, sinon en envoie un nouveau."""
    q = update.callback_query
    if q and q.message and not q.message.photo:
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            return
        except BadRequest as ex:
            if "not modified" in str(ex):
                return
    await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=kb,
                                             disable_web_page_preview=True)


# ---------------------------------------------------------------- commandes
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if owner_id() is None:
        os.makedirs(DATA, exist_ok=True)
        json.dump({"id": update.effective_user.id, "since": time.time()}, open(OWNER_FILE, "w"))
        await update.message.reply_text("🔐 Tu es enregistré comme propriétaire de ce bot. Il ne répondra qu'à toi.")
    if not is_owner(update):
        await update.message.reply_text("⛔ Bot privé.")
        return
    await update.effective_chat.send_message(
        "🎛️ <b>DoktorTV</b> — panneau de contrôle\nEnvoie directement un titre pour le chercher dans Jellyseerr.",
        parse_mode="HTML", reply_markup=MAIN)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if ctx.user_data.pop("await", None) == "purge":
        try:
            await purge_search(update, ctx, update.message.text.strip())
        except Exception as ex:
            await update.effective_chat.send_message(f"🧹 {e(str(ex))[:300]}",
                                                     parse_mode="HTML", reply_markup=M([BACK]))
        return
    await do_search(update, update.message.text.strip())


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Usage : /cherche <titre> — ou envoie juste le titre.")
        return
    await do_search(update, q)


# ---------------------------------------------------------------- recherche Jellyseerr
async def do_search(update: Update, q: str):
    try:
        res = await api("seerr", "GET", f"search?query={quote(q, safe='')}&page=1&language=fr")
    except Exception as ex:
        await update.effective_chat.send_message(f"❌ Jellyseerr injoignable : {e(str(ex))}", parse_mode="HTML")
        return
    items = [r for r in res.get("results", []) if r.get("mediaType") in ("movie", "tv")][:8]
    if not items:
        await update.effective_chat.send_message(f"🤷 Rien trouvé pour « {e(q)} ».", reply_markup=M([BACK]))
        return
    rows = []
    for r in items:
        title = r.get("title") or r.get("name") or "?"
        year = (r.get("releaseDate") or r.get("firstAirDate") or "")[:4]
        icon = "🎬" if r["mediaType"] == "movie" else "📺"
        st = (r.get("mediaInfo") or {}).get("status")
        mark = {5: " ✅", 4: " 🟡", 3: " ⬇️", 2: " ⏳"}.get(st, "")
        rows.append([B(f"{icon} {title} ({year or '?'}){mark}"[:60], callback_data=f"m:{r['mediaType']}:{r['id']}")])
    rows.append(BACK)
    await update.effective_chat.send_message(f"🔎 Résultats pour « <b>{e(q)}</b> » :", parse_mode="HTML",
                                             reply_markup=M(rows))


async def media_detail(update: Update, mtype: str, mid: int):
    d = await api("seerr", "GET", f"{mtype}/{mid}?language=fr")
    title = d.get("title") or d.get("name") or "?"
    year = (d.get("releaseDate") or d.get("firstAirDate") or "")[:4]
    st = (d.get("mediaInfo") or {}).get("status")
    lines = [f"{'🎬' if mtype == 'movie' else '📺'} <b>{e(title)}</b> ({year or '?'})"]
    extra = []
    if d.get("voteAverage"):
        extra.append(f"⭐ {d['voteAverage']:.1f}")
    if mtype == "movie" and d.get("runtime"):
        extra.append(f"⏱️ {d['runtime']} min")
    if mtype == "tv" and d.get("numberOfSeasons"):
        extra.append(f"📚 {d['numberOfSeasons']} saison(s)")
    genres = ", ".join(g["name"] for g in d.get("genres", [])[:3])
    if genres:
        extra.append(genres)
    if extra:
        lines.append(" · ".join(extra))
    lines.append(f"État : {STATUS.get(st, '➖ pas dans la bibliothèque')}")
    ov = (d.get("overview") or "").strip()
    if ov:
        lines.append("\n" + e(ov[:600] + ("…" if len(ov) > 600 else "")))
    caption = "\n".join(lines)[:1024]
    btns = []
    if mtype == "movie" and st not in (2, 3, 5):
        btns.append([B("📥 Demander", callback_data=f"req:movie:{mid}")])
    elif mtype == "tv" and st != 5:
        btns.append([B("📺 Choisir les saisons", callback_data=f"ss:{mid}")])
        if st not in (2, 3, 4):
            btns.append([B("📥 Demander toute la série", callback_data=f"req:tv:{mid}")])
    btns.append(BACK)
    poster = d.get("posterPath")
    if poster:
        await update.effective_chat.send_photo(TMDB_IMG + poster, caption=caption, parse_mode="HTML",
                                               reply_markup=M(btns))
    else:
        await update.effective_chat.send_message(caption, parse_mode="HTML", reply_markup=M(btns))


SEASON_MARK = {5: "✅", 4: "🟡", 3: "⬇️", 2: "⏳"}
LOCKED = {5, 3, 2}  # dispo / en cours / demandée : non sélectionnable (🟡 reste cochable)


async def season_state(mid: int) -> list[dict]:
    """Saisons d'une série : numéro, nom, nb d'épisodes, état dans la bibliothèque/les demandes."""
    d = await api("seerr", "GET", f"tv/{mid}?language=fr")
    info = d.get("mediaInfo") or {}
    status = {s["seasonNumber"]: s.get("status") for s in info.get("seasons", [])}
    for req in info.get("requests", []):  # saisons déjà demandées mais pas encore là
        if req.get("status") in (1, 2):  # en attente / approuvée
            for s in req.get("seasons", []):
                status.setdefault(s["seasonNumber"], 2)
                if (status[s["seasonNumber"]] or 1) < 2:
                    status[s["seasonNumber"]] = 2
    out = []
    for s in d.get("seasons", []):
        n = s.get("seasonNumber")
        if n is None or n == 0:  # on ignore les « épisodes spéciaux »
            continue
        out.append({"n": n, "eps": s.get("episodeCount") or 0, "st": status.get(n)})
    return out


def season_keyboard(mid: int, seasons: list[dict], sel: set) -> M:
    rows, row = [], []
    for s in seasons:
        mark = SEASON_MARK.get(s["st"])
        if s["st"] in LOCKED:  # déjà dispo / demandée : non sélectionnable
            label, data = f"{mark} S{s['n']:02d}", f"sx:{mid}"
        else:
            box = "☑️" if s["n"] in sel else (mark or "⬜")
            label, data = f"{box} S{s['n']:02d} ({s['eps']} ép.)", f"st:{mid}:{s['n']}"
        row.append(B(label, callback_data=data))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    free = [s["n"] for s in seasons if s["st"] not in LOCKED]
    if free:
        rows.append([B("✔️ Tout cocher" if len(sel) < len(free) else "✖️ Tout décocher", callback_data=f"sa:{mid}")])
        rows.append([B(f"📥 Demander {len(sel)} saison(s)" if sel else "Coche des saisons…",
                       callback_data=f"sr:{mid}" if sel else f"sx:{mid}")])
    rows.append(BACK)
    return M(rows)


async def season_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mid: int, action: str, n: int | None = None):
    cache = ctx.user_data.setdefault("seasons", {})
    if action == "open" or mid not in cache:
        cache[mid] = {"seasons": await season_state(mid), "sel": set()}
    c = cache[mid]
    free = [s["n"] for s in c["seasons"] if s["st"] not in LOCKED]
    if action == "toggle" and n in free:
        c["sel"] ^= {n}
    elif action == "all":
        c["sel"] = set() if len(c["sel"]) >= len(free) else set(free)
    kb = season_keyboard(mid, c["seasons"], c["sel"])
    q = update.callback_query
    try:
        await q.edit_message_reply_markup(reply_markup=kb)
    except BadRequest as ex:
        if "not modified" not in str(ex):
            await update.effective_chat.send_message("📺 Choisis les saisons à demander :\n✅ dispo · 🟡 en partie · ⬇️/⏳ en cours ou demandée",
                                                     reply_markup=kb)


async def media_request(update: Update, mtype: str, mid: int, seasons: list[int] | None = None):
    body = {"mediaType": mtype, "mediaId": mid}
    if mtype == "tv":
        body["seasons"] = sorted(seasons) if seasons else "all"
    try:
        r = await api("seerr", "POST", "request", body)
        st = (r or {}).get("status")
        msg = "✅ Demande envoyée et approuvée, le téléchargement va démarrer." if st == 2 else "✅ Demande envoyée."
    except Exception as ex:
        msg = f"❌ Échec de la demande : {e(str(ex))}"
    await update.effective_chat.send_message(msg, parse_mode="HTML", reply_markup=M([BACK]))



# ---------------------------------------------------------------- ménage (Purgearr)
# Le bot ne supprime rien lui-même : il passe par Purgearr (mêmes routes que
# l'interface web), donc par sa recherche exacte des copies et des torrents.
# Purgearr : https://github.com/Lekarov/Purgearr (≥ 1.2.0, routes /api/bot/*).
PURGE_MENU = M([
    [B("🎬 Films déjà vus", callback_data="pl:vus:movie"), B("📺 Séries vues ou entamées", callback_data="pl:vus:series")],
    [B("💤 Jamais regardés", callback_data="pl:jamais:all"), B("🐘 Les plus lourds", callback_data="pl:lourds:all")],
    [B("🔎 Chercher un titre à supprimer", callback_data="ps")],
    BACK,
])
CATS = {"vus": "déjà vus", "jamais": "jamais regardés (ajoutés il y a plus de 2 mois)", "lourds": "les plus lourds"}
KINDS = {"movie": "films", "series": "séries", "all": "films et séries"}


PURGEARR_MIN = (1, 2, 0)
PURGEARR_REPO = "https://github.com/Lekarov/Purgearr"
_PURGEARR_OK = {"at": 0.0}


class PurgearrError(RuntimeError):
    pass


async def purgearr_check():
    """Vérifie (au plus toutes les 5 min) qu'on parle à un Purgearr compatible."""
    if time.time() - _PURGEARR_OK["at"] < 300:
        return
    try:
        async with SESSION.get(f"{PURGEARR}/api/bot/ping", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 404:
                # ancien Purgearr (sans /api/bot/ping) ou autre service ? /api/status tranche
                async with SESSION.get(f"{PURGEARR}/api/status", timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    st = await r2.json(content_type=None) if r2.status == 200 else None
                if isinstance(st, dict) and "radarr" in st and "transmission" in st:
                    raise PurgearrError(f"Purgearr répond mais il est trop ancien : mets-le à jour en "
                                        f"{'.'.join(map(str, PURGEARR_MIN))} ou plus ({PURGEARR_REPO}).")
                raise PurgearrError(f"{PURGEARR} n'est pas Purgearr : vérifie PURGEARR_URL.")
            info = await r.json(content_type=None)
    except aiohttp.ClientError as ex:
        raise PurgearrError(f"Purgearr injoignable à {PURGEARR} ({type(ex).__name__}). "
                            "Vérifie qu'il tourne et la variable PURGEARR_URL du .env.") from ex
    except ValueError as ex:
        raise PurgearrError(f"{PURGEARR} ne répond pas comme Purgearr : vérifie PURGEARR_URL.") from ex
    if not isinstance(info, dict) or info.get("app") != "purgearr":
        raise PurgearrError(f"{PURGEARR} n'est pas Purgearr : vérifie PURGEARR_URL.")
    try:
        ver = tuple(int(x) for x in str(info.get("version", "0")).split("-")[0].split(".")[:3])
    except ValueError:
        ver = (0,)
    if ver < PURGEARR_MIN:
        raise PurgearrError(f"Purgearr {info.get('version')} est trop ancien : "
                            f"{'.'.join(map(str, PURGEARR_MIN))} ou plus requis ({PURGEARR_REPO}).")
    _PURGEARR_OK["at"] = time.time()


async def purgearr(method: str, path: str, form: dict | None = None, raw: bool = False, timeout: int = 60):
    await purgearr_check()
    async with SESSION.request(method, f"{PURGEARR}{path}", data=form,
                               timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as r:
        if r.status >= 400:
            if raw and r.status == 404:
                return None
            raise RuntimeError(f"Purgearr HTTP {r.status}: {(await r.text())[:200]}")
        if raw:
            return await r.read()
        txt = await r.text()
        return json.loads(txt) if txt.strip().startswith(("{", "[")) else None


def ago(days) -> str:
    if days is None:
        return "?"
    if days == 0:
        return "aujourd'hui"
    if days < 60:
        return f"il y a {days} j"
    if days < 730:
        return f"il y a {days // 30} mois"
    return f"il y a {days // 365} ans"


def card_text(d: dict, pos: str = "") -> str:
    icon = "🎬" if d.get("type") == "Movie" else "📺"
    head = f"{icon} <b>{e(d.get('title') or '?')}</b>" + (f" ({d['year']})" if d.get("year") else "")
    if d.get("rating"):
        head += f" · ⭐ {d['rating']:.1f}"
    lines = [head]
    tech = [f"💾 <b>{e(d.get('size_human', '?'))}</b>"]
    if d.get("video"):
        tech.append(e(d["video"]))
    if d.get("type") == "Series" and d.get("episodes"):
        tech.append(f"{d.get('seasons') or '?'} saison(s) · {d.get('files') or d['episodes']} fichiers")
    elif d.get("runtime_min"):
        tech.append(f"{d['runtime_min']} min")
    lines.append(" · ".join(tech))
    if d.get("audio"):
        lines.append("🔊 " + ", ".join(d["audio"][:5]) + (f" · 💬 {', '.join(d['subs'][:4])}" if d.get("subs") else ""))
    if d.get("watched_by"):
        lines.append(f"👁️ Vu par {e(', '.join(d['watched_by']))} · {ago(d.get('last_played_days'))}")
    elif d.get("started_by"):
        lines.append(f"▶️ Entamé par {e(', '.join(d['started_by']))}")
    else:
        lines.append("💤 Jamais regardé")
    lines.append(f"📅 Ajouté {ago(d.get('added_days'))}")
    if d.get("favorite"):
        lines.append("⭐ <b>En favori</b> — suppression bloquée")
    elif d.get("protected"):
        lines.append("🛡️ <b>Protégé</b> dans Purgearr")
    if d.get("path"):
        short = d["path"].split("/downloads/", 1)[-1]
        lines.append(f"📁 <code>{e(short[:120])}</code>")
    ov = d.get("overview") or ""
    if ov:
        lines.append("\n<i>" + e(ov[:280] + ("…" if len(ov) > 280 else "")) + "</i>")
    if pos:
        lines.append(f"\n{pos}")
    return "\n".join(lines)[:1024]


def card_keyboard(st: dict) -> M:
    i, n = st["idx"], len(st["items"])
    nav = []
    if n > 1:
        nav = [B("⬅️", callback_data="pg:prev"), B(f"{i + 1}/{n}", callback_data="pg:noop"),
               B("Suivant ➡️", callback_data="pg:next")]
    cur = st["items"][i]
    rows = [nav] if nav else []
    if not cur.get("protected"):
        rows.append([B("🗑️ Supprimer…", callback_data="pg:del"), B("🛡️ Garder", callback_data="pg:keep")])
    rows.append([B("🧹 Ménage", callback_data="pm")] + BACK)
    return M(rows)


async def send_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = True):
    st = ctx.user_data.get("purge")
    if not st or not st["items"]:
        await show(update, "✨ Plus rien à proposer ici.", PURGE_MENU)
        return
    st["idx"] %= len(st["items"])
    cur = st["items"][st["idx"]]
    try:
        d = await purgearr("GET", f"/api/bot/item/{cur['id']}")
        d["protected"] = d.get("protected") or cur.get("protected")
    except Exception:
        d = cur
    pos = st.get("label", "")
    text = card_text(d, pos)
    kb = card_keyboard(st)
    img = await purgearr("GET", f"/api/bot/poster/{cur['id']}", raw=True)
    q = update.callback_query
    if edit and q and q.message and q.message.photo and img:
        try:
            await q.edit_message_media(InputMediaPhoto(img, caption=text, parse_mode="HTML"), reply_markup=kb)
            return
        except BadRequest as ex:
            if "not modified" in str(ex):
                return
    if edit and q and q.message:
        try:
            await q.message.delete()
        except BadRequest:
            pass
    if img:
        await update.effective_chat.send_photo(img, caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=kb)


async def purge_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, cat: str, kind: str):
    await show(update, "⏳ Je regarde ce qui prend de la place…", None)
    r = await purgearr("GET", f"/api/bot/candidates?cat={cat}&kind={kind}", timeout=180)
    items = r.get("items", [])
    if not items:
        await show(update, f"✨ Aucun élément « {CATS[cat]} » à proposer.", PURGE_MENU)
        return
    ctx.user_data["purge"] = {"items": items, "idx": 0,
                              "label": f"🧹 {KINDS[kind].capitalize()} {CATS[cat]} · {r['count']} au total, {r['total_human']}"}
    await send_card(update, ctx)


async def purge_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, q: str):
    r = await purgearr("GET", f"/api/bot/search?q={quote(q)}", timeout=180)
    items = r.get("items", [])
    if not items:
        await update.effective_chat.send_message(f"🤷 Rien dans la bibliothèque pour « {e(q)} ».",
                                                 reply_markup=PURGE_MENU)
        return
    ctx.user_data["purge"] = {"items": items, "idx": 0, "label": f"🔎 « {e(q)} » · {len(items)} résultat(s)"}
    await send_card(update, ctx, edit=False)


def preview_text(cur: dict, scan: dict, with_names: bool) -> tuple[str, int]:
    links = scan.get("service_links") or {}
    copies = scan.get("copies") or []
    verified = [c for c in copies if c.get("match_method") != "name"]
    names = [c for c in copies if c.get("match_method") == "name"]
    freed = (scan.get("source_size_bytes") or 0) + sum(c.get("size_bytes") or 0 for c in verified if not c.get("is_hardlink"))
    if with_names:
        freed += sum(c.get("size_bytes") or 0 for c in names)
    icon = "🎬" if cur["type"] == "Movie" else "📺"
    out = [f"🗑️ <b>Supprimer {icon} {e(cur['title'])} ?</b>\n"]
    if cur["type"] == "Movie":
        arr = "Radarr ✓ (+ exclusion)" if "/movie/" in (links.get("radarr") or "") else "absent de Radarr → suppression directe"
    else:
        arr = "Sonarr ✓ (+ exclusion)" if "/series/" in (links.get("sonarr") or "") else "absent de Sonarr → suppression directe"
    out.append(f"📚 Bibliothèque : <b>{e(scan.get('source_size_human') or '?')}</b> · {arr}")
    torrents = links.get("transmission_torrents") or []
    if torrents:
        trackers = sorted({t.get("tracker_name") or "?" for t in torrents})
        out.append(f"🧲 {len(torrents)} torrent(s) retiré(s) : {e(', '.join(trackers))}")
    else:
        out.append("🧲 Aucun torrent ne seede ce fichier")
    label = {"inode": "🔗 hardlink", "symlink": "↪️ lien cross-seed", "hash": "📄 copie identique"}
    if verified:
        out.append(f"\n<b>Copies trouvées ({len(verified)})</b>")
        for c in verified[:10]:
            short = c["path"].split("/downloads/", 1)[-1]
            size = "0 o libéré" if c.get("is_hardlink") else c.get("size_human", "")
            out.append(f"{label.get(c.get('match_method'), '•')} {e(short[:90])} · {size}")
        if len(verified) > 10:
            out.append(f"… et {len(verified) - 10} autre(s)")
    else:
        out.append("\nAucune autre copie sur le NAS.")
    if names:
        tot = sum(c.get("size_bytes") or 0 for c in names)
        mark = "✅ incluses" if with_names else "non incluses"
        out.append(f"\n<b>Autres versions trouvées par le nom ({len(names)}, {human(tot)}) — {mark}</b>")
        for c in names[:6]:
            out.append(f"• {e(c['path'].split('/downloads/', 1)[-1][:90])} · {c.get('size_human', '')}")
    out.append(f"\n💾 <b>Espace libéré : {human(freed)}</b>")
    return "\n".join(out)[:4000], len(names)


async def purge_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE, toggle: bool = False):
    st = ctx.user_data.get("purge")
    if not st:
        await show(update, "Session expirée.", PURGE_MENU)
        return
    cur = st["items"][st["idx"]]
    if not toggle or "scan" not in st:
        await update.effective_chat.send_message(f"🔍 Je cherche tout ce qui appartient à <b>{e(cur['title'])}</b>…",
                                                 parse_mode="HTML")
        st["scan"] = await purgearr("POST", "/api/scan/copies", {
            "jellyfin_item_id": cur["id"], "item_type": cur["type"], "item_title": cur["title"],
            "series_title": cur["title"] if cur["type"] == "Series" else ""}, timeout=180)
        st["with_names"] = False
    else:
        st["with_names"] = not st.get("with_names")
    text, n_names = preview_text(cur, st["scan"], st["with_names"])
    rows = [[B("✅ Supprimer définitivement", callback_data="pg:ok")]]
    if n_names:
        rows.append([B("➖ Ne pas inclure les autres versions" if st["with_names"] else
                       f"➕ Inclure aussi les {n_names} autres versions", callback_data="pg:names")])
    rows.append([B("❌ Annuler", callback_data="pg:back")])
    q = update.callback_query
    if toggle and q and q.message and not q.message.photo:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=M(rows))
    else:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=M(rows))


async def purge_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("purge")
    if not st or "scan" not in st:
        await show(update, "Session expirée, relance la suppression.", PURGE_MENU)
        return
    cur = st["items"][st["idx"]]
    names = [c["path"] for c in st["scan"].get("copies", []) if c.get("match_method") == "name"] if st.get("with_names") else []
    await show(update, f"🗑️ Suppression de <b>{e(cur['title'])}</b> en cours…", None)
    r = await purgearr("POST", "/api/delete/manual", {
        "jellyfin_item_id": cur["id"], "item_type": cur["type"], "item_title": cur["title"],
        "series_title": cur["title"] if cur["type"] == "Series" else "",
        "source_hash": st["scan"].get("source_hash", ""), "selected_name_paths": json.dumps(names)}, timeout=600)
    st.pop("scan", None)
    if r.get("blocked_by_favorite"):
        msg = f"⭐ <b>{e(cur['title'])}</b> est en favori : suppression bloquée."
    elif r.get("success"):
        c = r.get("cleanup") or {}
        services = {"radarr": "Radarr", "sonarr": "Sonarr", "transmission": "Transmission",
                    "jellyfin": "Jellyfin", "filesystem": "disque"}
        done = ", ".join(services.get(x, x) for x in r.get("services", []))
        msg = f"✅ <b>{e(cur['title'])}</b> supprimé ({e(done)})."
        if c.get("copies_deleted"):
            msg += f"\n🧽 {c['copies_deleted']} copie(s)/lien(s) nettoyé(s), {e(c.get('size_human', ''))} en plus."
        if c.get("copies_failed"):
            msg += f"\n⚠️ {c['copies_failed']} copie(s) n'ont pas pu être supprimées."
        if r.get("errors"):
            msg += "\n⚠️ " + e("; ".join(r["errors"]))[:300]
        try:
            await purgearr("POST", f"/api/bot/forget/{cur['id']}")
        except Exception:
            pass
        st["items"].pop(st["idx"])
    else:
        msg = f"❌ Échec : {e('; '.join(r.get('errors') or ['inconnu']))[:500]}"
    kb = [[B("➡️ Suivant", callback_data="pg:show")]] if st["items"] else []
    await show(update, msg, M(kb + [[B("🧹 Ménage", callback_data="pm")] + BACK]))


async def purge_keep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.user_data.get("purge")
    cur = st["items"][st["idx"]]
    await purgearr("POST", "/protected/add", {"title": "", "jellyfin_id": cur["id"]})
    try:
        await purgearr("POST", f"/api/bot/forget/{cur['id']}")
    except Exception:
        pass
    st["items"].pop(st["idx"])
    await update.effective_chat.send_message(f"🛡️ <b>{e(cur['title'])}</b> gardé : protégé dans Purgearr, "
                                             "il ne sera plus proposé.", parse_mode="HTML")
    await send_card(update, ctx)


async def on_purge_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str):
    st = ctx.user_data.get("purge")
    if data == "pm":
        ctx.user_data.pop("await", None)
        try:
            await purgearr_check()
        except PurgearrError as ex:
            await show(update, f"🧹 <b>Ménage indisponible</b>\n\n{e(str(ex))}", M([BACK]))
            return
        await show(update, "🧹 <b>Ménage</b> — que veux-tu passer en revue ?\n"
                           "Les favoris et les titres protégés ne sont jamais proposés.", PURGE_MENU)
    elif data.startswith("pl:"):
        _, cat, kind = data.split(":")
        await purge_list(update, ctx, cat, kind)
    elif data == "ps":
        ctx.user_data["await"] = "purge"
        await show(update, "🔎 Envoie le titre du film ou de la série à supprimer.", M([[B("🧹 Ménage", callback_data="pm")]]))
    elif not st:
        await show(update, "Session expirée.", PURGE_MENU)
    elif data in ("pg:next", "pg:prev"):
        st["idx"] += 1 if data == "pg:next" else -1
        await send_card(update, ctx)
    elif data in ("pg:show", "pg:back"):
        await send_card(update, ctx, edit=data == "pg:show")
    elif data == "pg:del":
        await purge_preview(update, ctx)
    elif data == "pg:names":
        await purge_preview(update, ctx, toggle=True)
    elif data == "pg:ok":
        await purge_confirm(update, ctx)
    elif data == "pg:keep":
        await purge_keep(update, ctx)


async def cmd_transmission(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text, kb = await view_transmission()
    await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=kb)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_chat.send_message(view_health(), parse_mode="HTML",
                                             reply_markup=M([[B("🔃 Actualiser", callback_data="hl")] + BACK]))


async def cmd_crossseed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_chat.send_message(await crossseed_status(), parse_mode="HTML", reply_markup=CS_MENU)


async def cmd_purge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if q:
        await purge_search(update, ctx, q)
    else:
        await update.effective_chat.send_message("🧹 <b>Ménage</b> — que veux-tu passer en revue ?",
                                                 parse_mode="HTML", reply_markup=PURGE_MENU)



# ---------------------------------------------------------------- cross-seed
# Lancement manuel des tâches du démon cross-seed (POST /api/job, clé X-Api-Key
# lue à chaud dans sa config montée en lecture seule) + état lu dans ses journaux.
CS_PORT = int(os.environ.get("CROSSSEED_PORT", "2468"))
CS_CONFIG = "/keys/cross-seed/config.js"
CS_CONTAINER = os.environ.get("CROSSSEED_CONTAINER", "cross-seed")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CS_MENU = M([
    [B("🔎 Recherche maintenant", callback_data="cs:search"), B("📡 Scan RSS maintenant", callback_data="cs:rss")],
    [B("🔥 Tout re-chercher (même déjà cherché)", callback_data="cs:force?")],
    [B("📊 État", callback_data="cs:status")] + BACK,
])
CS_WATCH: dict = {}


def crossseed_key() -> str:
    keys = re.findall(r"""["']?apiKey["']?\s*:\s*["']([^"']+)["']""", open(CS_CONFIG).read())
    if not keys:
        raise RuntimeError("clé API cross-seed introuvable dans config.js")
    return keys[-1]


async def crossseed_job(name: str, force: bool = False) -> tuple[int, str]:
    body = {"name": name}
    if force:
        body.update({"ignoreExcludeRecentSearch": True, "ignoreExcludeOlder": True})
    async with SESSION.post(f"http://{HOST}:{CS_PORT}/api/job", json=body,
                            headers={"X-Api-Key": crossseed_key()},
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
        return r.status, (await r.text())[:200]


async def crossseed_logs(hours: int = 30) -> list[str]:
    since = int(time.time() - hours * 3600)
    conn = aiohttp.UnixConnector(path="/var/run/docker.sock")
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.get(f"http://docker/containers/{CS_CONTAINER}/logs?stdout=1&stderr=1&since={since}",
                         timeout=aiohttp.ClientTimeout(total=60)) as r:
            raw = await r.read()
    out, i = bytearray(), 0
    while i + 8 <= len(raw):  # flux multiplexé Docker : en-tête de 8 octets par bloc
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out += raw[i + 8:i + 8 + size]
        i += 8 + size
    return [ANSI.sub("", l) for l in out.decode("utf-8", "replace").splitlines()]


def paris(line: str) -> str:
    """Les journaux cross-seed sont en UTC : « 18/11 01:37 » heure de Paris."""
    try:
        d = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            d = d.astimezone(ZoneInfo("Europe/Paris"))
        except Exception:
            d = d + timedelta(hours=1)
        return d.strftime("%d/%m %H:%M")
    except ValueError:
        return "?"


def _last(lines: list[str], pat: str) -> str | None:
    rx = re.compile(pat)
    return next((l for l in reversed(lines) if rx.search(l)), None)


async def crossseed_status() -> str:
    lines = await crossseed_logs()
    out = ["🔁 <b>Cross-seed</b>\n"]
    start = _last(lines, r"starting job: search")
    found = _last(lines, r"\[search\] Found \d+ cross seeds")
    running = start and (not found or lines.index(found) < lines.index(start))
    if running:
        prog = _last(lines, r"\[search\] \((\d+)/(\d+)\)")
        m = re.search(r"\((\d+)/(\d+)\)", prog or "")
        out.append(f"🔎 Recherche <b>en cours</b>" + (f" : {m.group(1)}/{m.group(2)} torrents" if m else ""))
        if m:
            out.append(bar(int(m.group(1)) / int(m.group(2)) * 100))
    if found:
        m = re.search(r"Found (\d+) cross seeds from (\d+)", found)
        out.append(f"✅ Dernière recherche ({paris(found)}) : "
                   f"<b>{m.group(1)}</b> trouvé(s) sur {m.group(2)} torrents")
    for job, label in (("search", "🔎 Prochaine recherche"), ("rss", "📡 Prochain RSS")):
        l = _last(lines, rf"\[scheduler\] {job}: .*next run in")
        if l:
            delta = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
                        for n, u in re.findall(r"(\d+)\s*([dhms])", l.split("next run in")[-1]))
            try:
                at = datetime.strptime(l[:19], "%Y-%m-%d %H:%M:%S") + timedelta(seconds=delta)
                out.append(f"{label} : {paris(at.strftime('%Y-%m-%d %H:%M:%S'))}")
            except ValueError:
                pass
    rss = _last(lines, r"RSS scan complete")
    if rss:
        m = re.search(r"checked (\d+)", rss)
        out.append(f"📡 Dernier RSS {paris(rss)} : {m.group(1) if m else '?'} nouveautés examinées")
    inj = _last(lines, r"\[inject\] Injected \d+/\d+")
    if inj:
        m = re.search(r"Injected (\d+)/(\d+) torrents(?:, (\d+) failed)?", inj)
        if m:
            fail = f", ⚠️ {m.group(3)} en échec" if m.group(3) else ""
            out.append(f"💉 Dernière injection {paris(inj)} : {m.group(1)}/{m.group(2)} ajoutés{fail}")
    errs = sum(1 for l in lines[-400:] if " error: " in l)
    if errs:
        out.append(f"\n⚠️ {errs} ligne(s) d'erreur récentes dans ses journaux")
    return "\n".join(out)


async def crossseed_watch(started: float):
    """Prévient quand la recherche lancée à la main se termine (au plus 12 h)."""
    deadline = started + 12 * 3600
    while time.time() < deadline:
        await asyncio.sleep(90)
        try:
            lines = await crossseed_logs(hours=13)
        except Exception:
            continue
        for l in reversed(lines):
            m = re.search(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*\[search\] Found (\d+) cross seeds from (\d+)", l)
            if not m:
                continue
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            # les journaux du conteneur sont en UTC
            if when >= started - 5:
                await notify(f"🔁 <b>Recherche cross-seed terminée</b>\n{m.group(2)} nouveau(x) cross-seed sur "
                             f"{m.group(3)} torrents examinés.")
                CS_WATCH.pop("search", None)
                return
            break
    CS_WATCH.pop("search", None)


async def on_crossseed_button(update: Update, data: str):
    if data == "cs":
        await show(update, "🔁 <b>Cross-seed</b> — recherche automatique chaque jour, RSS toutes les 30 min.\n"
                           "Tu peux lancer une passe tout de suite :", CS_MENU)
        return
    if data == "cs:status":
        await show(update, await crossseed_status(), M([[B("🔃 Actualiser", callback_data="cs:status")],
                                                        [B("🔁 Cross-seed", callback_data="cs")] + BACK]))
        return
    if data == "cs:force?":
        await show(update, "🔥 <b>Tout re-chercher</b> interroge tous tes trackers pour <b>tous</b> les torrents, "
                           "même ceux cherchés ces 7 derniers jours ou de plus de 28 jours.\n"
                           "C'est long (environ 30 s par torrent) et ça sollicite les trackers. On y va ?",
                   M([[B("✅ Oui, tout re-chercher", callback_data="cs:force"), B("❌ Non", callback_data="cs")]]))
        return
    job = "rss" if data == "cs:rss" else "search"
    force = data == "cs:force"
    st, txt = await crossseed_job(job, force)
    if st == 200:
        if job == "search":
            msg = "🔎 Recherche cross-seed lancée" + (" (tout re-chercher)" if force else "") + \
                  ".\nJe te préviens quand elle est finie. Suis l'avancement avec 📊 État."
            if "search" not in CS_WATCH:
                CS_WATCH["search"] = asyncio.get_running_loop().create_task(crossseed_watch(time.time()))
        else:
            msg = "📡 Scan RSS lancé, résultat dans 📊 État d'ici une minute."
    elif st == 409:
        msg = ("⏳ Impossible maintenant : la tâche tourne déjà, ou elle a déjà été avancée récemment "
               "(cross-seed n'autorise une passe anticipée que lorsque la prochaine passe planifiée est proche).")
    elif st == 404:
        msg = "🚫 Cette tâche est désactivée dans la configuration de cross-seed."
    else:
        msg = f"❌ cross-seed a répondu {st} : {e(txt)}"
    await show(update, msg, M([[B("📊 État", callback_data="cs:status")], [B("🔁 Cross-seed", callback_data="cs")] + BACK]))



# ---------------------------------------------------------------- Transmission
# RPC Transmission (identifiants dans le .env du NAS). Le bot ne supprime jamais
# de données : « Retirer » enlève seulement le torrent de Transmission.
TR_URL = os.environ.get("TRANSMISSION_URL", f"http://{HOST}:9091/transmission/rpc")
TR_AUTH = ("Basic " + __import__("base64").b64encode(
    f"{os.environ['TRANSMISSION_USER']}:{os.environ.get('TRANSMISSION_PASS', '')}".encode()).decode()) \
    if os.environ.get("TRANSMISSION_USER") else ""
TR_SID = {"id": ""}
PAUSED_FILE = f"{DATA}/paused_by_bot.json"
TR_STATUS = {0: "⏹️ arrêté", 1: "⏳ vérif. en attente", 2: "🔍 vérification", 3: "⏳ en attente",
             4: "⬇️ téléchargement", 5: "⏳ seed en attente", 6: "⬆️ seed"}
PAGE = 8


async def trpc(method: str, args: dict | None = None) -> dict:
    for _ in range(2):
        async with SESSION.post(TR_URL, json={"method": method, "arguments": args or {}},
                                headers={"X-Transmission-Session-Id": TR_SID["id"], "Authorization": TR_AUTH},
                                timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status == 409:
                TR_SID["id"] = r.headers.get("X-Transmission-Session-Id", "")
                continue
            if r.status >= 400:
                raise RuntimeError(f"Transmission HTTP {r.status}")
            j = await r.json(content_type=None)
            if j.get("result") != "success":
                raise RuntimeError(f"Transmission : {j.get('result')}")
            return j.get("arguments", {})
    raise RuntimeError("Transmission : session refusée")


TR_FIELDS = ["id", "name", "status", "error", "errorString", "uploadRatio", "uploadedEver", "rateUpload",
             "rateDownload", "peersGettingFromUs", "activityDate", "percentDone", "sizeWhenDone",
             "downloadDir", "trackerStats", "hashString"]


async def torrents() -> list[dict]:
    return (await trpc("torrent-get", {"fields": TR_FIELDS}))["torrents"]


def tracker_of(t: dict) -> str:
    from urllib.parse import urlparse
    for ts in t.get("trackerStats") or []:
        host = ts.get("sitename") or urlparse(ts.get("announce") or "").hostname or ""
        if host:
            return re.sub(r"^(tracker|www|tk)\.", "", host)
    if "/.cross-seed-links/" in (t.get("downloadDir") or ""):
        return t["downloadDir"].rsplit("/", 1)[-1]
    return "?"


def tname(t: dict) -> str:
    """Nom du torrent sans entités HTML déjà présentes (&#x27; …)."""
    return html.unescape(t.get("name") or "?")


def problem_of(t: dict) -> str | None:
    """Message clair pour un torrent en erreur ou refusé par son tracker."""
    msg = t.get("errorString") or ""
    if not t.get("error"):
        bad = [ts.get("lastAnnounceResult") or "" for ts in t.get("trackerStats") or []
               if ts.get("lastAnnounceSucceeded") is False and ts.get("hasAnnounced")]
        bad = [b for b in bad if b and b != "Success" and "did not respond" not in b and "Could not connect" not in b]
        if not bad:
            return None
        msg = bad[0]
    low = msg.lower()
    if "passkey" in low:
        return "🔑 passkey invalide (compte ou lien tracker à mettre à jour)"
    if "unregistered" in low or "not registered" in low or "non répertorié" in low or "removed from the tracker" in low:
        return "🚫 retiré du tracker (n'existe plus là-bas)"
    if "403" in low or "forbidden" in low:
        return "⛔ refusé par le tracker (403)"
    if "corrupt" in low or "verify local data" in low:
        return "🧩 données abîmées : à revérifier"
    if "no data found" in low:
        return "📂 fichiers introuvables sur le disque"
    return "⚠️ " + msg[:80]


async def view_transmission() -> tuple[str, M]:
    sess = await trpc("session-get", {"fields": ["alt-speed-enabled", "version"]})
    st = await trpc("session-stats")
    ts = await torrents()
    cum = st.get("cumulative-stats", {})
    up, down = cum.get("uploadedBytes", 0), cum.get("downloadedBytes", 0)
    by = {}
    for t in ts:
        by[t["status"]] = by.get(t["status"], 0) + 1
    probs = [t for t in ts if problem_of(t)]
    now = time.time()
    idle = [t for t in ts if t["status"] == 6 and now - (t.get("activityDate") or 0) > 30 * 86400]
    sending = [t for t in ts if t.get("rateUpload")]
    paused = _paused_ids()
    lines = [f"🧲 <b>Transmission</b> {e(sess.get('version', '').split()[0])}\n",
             f"⬇️ {human(st.get('downloadSpeed', 0))}/s · ⬆️ {human(st.get('uploadSpeed', 0))}/s"
             + (" · 🐢 <b>mode tortue</b>" if sess.get("alt-speed-enabled") else ""),
             f"📦 {len(ts)} torrents : " + ", ".join(f"{n} {TR_STATUS.get(k, k)}" for k, n in sorted(by.items(), key=lambda x: -x[1])),
             f"📤 Envoyé au total : <b>{human(up)}</b> · reçu {human(down)} · ratio global <b>{up / down:.2f}</b>" if down else "",
             f"⚡ En train d'envoyer : {len(sending)} torrent(s)",
             f"⚠️ Problèmes : <b>{len(probs)}</b>" + ("" if probs else " ✅"),
             f"💤 Aucune activité depuis 30 j : {len(idle)}"]
    if paused:
        lines.append(f"\n⏸️ <b>{len(paused)} torrents mis en pause par le bot</b>")
    rows = [[B("🏆 Top seeds", callback_data="tr:top"), B("⚡ Actifs", callback_data="tr:act")],
            [B(f"⚠️ Problèmes ({len(probs)})", callback_data="tr:pb:0"), B("💤 Inactifs", callback_data="tr:idle")],
            [B("🐢 Désactiver mode tortue" if sess.get("alt-speed-enabled") else "🐢 Mode tortue", callback_data="tr:alt"),
             B("▶️ Reprendre tout" if paused else "⏸️ Tout en pause", callback_data="tr:resume" if paused else "tr:pause?")],
            [B("🔃 Actualiser", callback_data="tr")] + BACK]
    return "\n".join(l for l in lines if l), M(rows)


def _paused_ids() -> list[int]:
    try:
        return json.load(open(PAUSED_FILE))
    except (OSError, ValueError):
        return []


def _short(name: str, n: int = 48) -> str:
    name = html.unescape(name)
    return name if len(name) <= n else name[:n - 1] + "…"


async def tr_list(kind: str) -> str:
    ts = await torrents()
    now = time.time()
    if kind == "top":
        ts = sorted(ts, key=lambda t: t.get("uploadedEver") or 0, reverse=True)[:12]
        out = ["🏆 <b>Ceux qui ont le plus envoyé</b>\n"]
        for t in ts:
            out.append(f"• {e(_short(t['name']))}\n   ⬆️ {human(t.get('uploadedEver') or 0)} · ratio {max(t.get('uploadRatio') or 0, 0):.1f} · {e(tracker_of(t))}")
    elif kind == "act":
        ts = sorted([t for t in ts if t.get("rateUpload") or t.get("rateDownload")],
                    key=lambda t: (t.get("rateUpload") or 0) + (t.get("rateDownload") or 0), reverse=True)[:15]
        out = ["⚡ <b>Actifs en ce moment</b>\n"] + [
            f"• {e(_short(t['name']))}\n   ⬆️ {human(t.get('rateUpload') or 0)}/s · ⬇️ {human(t.get('rateDownload') or 0)}/s · {t.get('peersGettingFromUs', 0)} pair(s)"
            for t in ts] or []
        if len(out) == 1:
            out.append("Personne ne télécharge chez toi en ce moment. 😴")
    else:  # inactifs
        idle = [t for t in ts if t["status"] == 6 and now - (t.get("activityDate") or 0) > 30 * 86400]
        size = sum(t.get("sizeWhenDone") or 0 for t in idle)
        count = len(idle)
        idle = sorted(idle, key=lambda t: t.get("sizeWhenDone") or 0, reverse=True)[:12]
        out = [f"💤 <b>Aucun envoi depuis plus de 30 jours</b> — {count} torrents, {human(size)}\n",
               "<i>Les plus gros (à supprimer via 🧹 Ménage si tu n'en veux plus) :</i>"]
        for t in idle:
            when = f"{int((now - t['activityDate']) / 86400)} j" if t.get("activityDate") else "jamais envoyé"
            out.append(f"• {e(_short(t['name']))}\n   {human(t.get('sizeWhenDone') or 0)} · ratio {max(t.get('uploadRatio') or 0, 0):.1f} · {when} · {e(tracker_of(t))}")
    return "\n".join(out)[:4000]


async def tr_problems(ctx: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, M]:
    ts = [t for t in await torrents() if problem_of(t)]
    ts.sort(key=lambda t: (problem_of(t), t["name"]))
    ctx.user_data["tr_probs"] = {t["id"]: t["name"] for t in ts}
    if not ts:
        return "✅ Aucun torrent en erreur.", M([[B("🧲 Transmission", callback_data="tr")] + BACK])
    pages = (len(ts) - 1) // PAGE + 1
    page %= pages
    chunk = ts[page * PAGE:(page + 1) * PAGE]
    out = [f"⚠️ <b>Torrents à problème</b> ({len(ts)}) — touche-en un pour agir\n"]
    rows = []
    for i, t in enumerate(chunk, 1 + page * PAGE):
        out.append(f"{i}. {e(_short(t['name'], 60))}\n   {problem_of(t)} · {e(tracker_of(t))}")
        rows.append([B(f"{i}. {_short(t['name'], 40)}", callback_data=f"tr:t:{t['id']}")])
    if pages > 1:
        rows.append([B("⬅️", callback_data=f"tr:pb:{page - 1}"), B(f"{page + 1}/{pages}", callback_data="pg:noop"),
                     B("➡️", callback_data=f"tr:pb:{page + 1}")])
    rows.append([B("🧲 Transmission", callback_data="tr")] + BACK)
    return "\n".join(out)[:4000], M(rows)


async def tr_detail(tid: int) -> tuple[str, M]:
    r = (await trpc("torrent-get", {"ids": [tid], "fields": TR_FIELDS}))["torrents"]
    if not r:
        return "Ce torrent n'existe plus.", M([[B("⚠️ Problèmes", callback_data="tr:pb:0")] + BACK])
    t = r[0]
    out = [f"🧲 <b>{e(tname(t))}</b>\n",
           f"{TR_STATUS.get(t['status'], t['status'])} · {t.get('percentDone', 0) * 100:.0f} % · {human(t.get('sizeWhenDone') or 0)}",
           f"🏷️ {e(tracker_of(t))} · ratio {max(t.get('uploadRatio') or 0, 0):.2f} · envoyé {human(t.get('uploadedEver') or 0)}",
           f"📁 <code>{e((t.get('downloadDir') or '').replace('/downloads/', ''))}</code>"]
    p = problem_of(t)
    if p:
        out.append(f"\n{p}")
        if t.get("errorString") and not p.startswith("⚠️"):
            out.append(f"<i>Message du tracker : {e(t['errorString'][:200])}</i>")
    rows = [[B("🔁 Revérifier", callback_data=f"tr:v:{tid}"), B("▶️ Relancer", callback_data=f"tr:s:{tid}")],
            [B("🗑️ Retirer de Transmission (garder les fichiers)", callback_data=f"tr:rm?:{tid}")],
            [B("⚠️ Problèmes", callback_data="tr:pb:0")] + BACK]
    return "\n".join(out), M(rows)


async def on_transmission_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str):
    if data == "tr":
        text, kb = await view_transmission()
        await show(update, text, kb)
    elif data in ("tr:top", "tr:act", "tr:idle"):
        await show(update, await tr_list(data[3:]), M([[B("🔃 Actualiser", callback_data=data)],
                                                       [B("🧲 Transmission", callback_data="tr")] + BACK]))
    elif data.startswith("tr:pb:"):
        text, kb = await tr_problems(ctx, int(data[6:]))
        await show(update, text, kb)
    elif data.startswith("tr:t:"):
        text, kb = await tr_detail(int(data[5:]))
        await show(update, text, kb)
    elif data.startswith(("tr:v:", "tr:s:")):
        tid = int(data[5:])
        await trpc("torrent-verify" if data.startswith("tr:v:") else "torrent-start", {"ids": [tid]})
        text, kb = await tr_detail(tid)
        await show(update, ("🔁 Vérification lancée.\n\n" if data.startswith("tr:v:") else "▶️ Relancé.\n\n") + text, kb)
    elif data.startswith("tr:rm?:"):
        tid = int(data[7:])
        name = html.unescape(ctx.user_data.get("tr_probs", {}).get(tid, f"#{tid}"))
        await show(update, f"Retirer <b>{e(name)}</b> de Transmission ?\nLes fichiers restent sur le disque "
                           "(Purgearr ou le ménage s'en chargeront si besoin).",
                   M([[B("✅ Retirer", callback_data=f"tr:rm:{tid}"), B("❌ Non", callback_data=f"tr:t:{tid}")]]))
    elif data.startswith("tr:rm:"):
        tid = int(data[6:])
        await trpc("torrent-remove", {"ids": [tid], "delete-local-data": False})
        text, kb = await tr_problems(ctx, 0)
        await show(update, "🗑️ Torrent retiré (fichiers conservés).\n\n" + text, kb)
    elif data == "tr:alt":
        cur = (await trpc("session-get", {"fields": ["alt-speed-enabled"]}))["alt-speed-enabled"]
        await trpc("session-set", {"alt-speed-enabled": not cur})
        text, kb = await view_transmission()
        await show(update, ("🐢 Mode tortue activé (vitesses réduites)." if not cur else "🐇 Mode tortue désactivé.")
                   + "\n\n" + text, kb)
    elif data == "tr:pause?":
        await show(update, "⏸️ Mettre <b>tous</b> les torrents en pause ?\nPlus aucun seed tant que tu ne relances pas. "
                           "Je retiens lesquels tournaient, pour ne relancer que ceux-là.",
                   M([[B("✅ Tout en pause", callback_data="tr:pause"), B("❌ Non", callback_data="tr")]]))
    elif data == "tr:pause":
        ids = [t["id"] for t in await torrents() if t["status"] != 0]
        prev = _paused_ids()
        json.dump(sorted(set(prev) | set(ids)), open(PAUSED_FILE, "w"))
        if ids:
            await trpc("torrent-stop", {"ids": ids})
        text, kb = await view_transmission()
        await show(update, f"⏸️ {len(ids)} torrents en pause.\n\n" + text, kb)
    elif data == "tr:resume":
        ids = _paused_ids()
        if ids:
            await trpc("torrent-start", {"ids": ids})
        try:
            os.remove(PAUSED_FILE)
        except OSError:
            pass
        text, kb = await view_transmission()
        await show(update, f"▶️ {len(ids)} torrents relancés.\n\n" + text, kb)


# ---------------------------------------------------------------- santé du NAS
# Relevé fait sur l'hôte par doktortv-health.timer (SMART, températures,
# services en échec) dans /data/health.json — le conteneur n'a pas accès aux disques.
HEALTH_FILE = f"{DATA}/health.json"
HDD_HOT, NVME_HOT, CPU_HOT = 50, 70, 90
WATCH_KEYS = {"realloc": "secteurs réalloués", "pending": "secteurs en attente",
              "offline_unc": "secteurs illisibles", "uncorrect": "erreurs non corrigées",
              "crc": "erreurs de câble (CRC)", "media_errors": "erreurs du SSD"}


def health() -> dict | None:
    try:
        return json.load(open(HEALTH_FILE))
    except (OSError, ValueError):
        return None


def disk_label(d: dict) -> str:
    m = d.get("model") or d["name"]
    if not d.get("rotational"):
        return m
    brand = "Seagate" if m.startswith("ST") else "WD" if m.startswith("WDC") else m.split()[0]
    return f"{brand} {round((d.get('size') or 0) / 1e12)} To"


def view_health() -> str:
    h = health()
    if not h:
        return "🩺 Relevé de santé indisponible (doktortv-health.timer ne tourne pas ?)."
    age = (time.time() - h["at"]) / 60
    out = ["🩺 <b>Santé du NAS</b>\n"]
    t = h.get("cpu_temp")
    if t:
        out.append(f"🌡️ Processeur : {t:.0f} °C" + (" 🔥" if t >= CPU_HOT else ""))
    for d in h.get("disks", []):
        s = d.get("smart", {})
        ok = s.get("passed")
        bad = [f"{s[k]} {v}" for k, v in WATCH_KEYS.items() if (s.get(k) or 0) > 0]
        icon = "🔴" if ok is False or bad else "🟢"
        out.append(f"\n{icon} <b>{e(disk_label(d))}</b> ({d['name']})")
        bits = ["SMART ✅" if ok else "SMART ❌" if ok is False else "SMART ?"]
        if s.get("sleeping"):
            bits.append("💤 en veille")
        if s.get("temp") is not None:
            hot = HDD_HOT if d.get("rotational") else NVME_HOT
            bits.append(f"{s['temp']} °C" + (" 🔥" if s["temp"] >= hot else ""))
        if s.get("hours") is not None:
            bits.append(f"{s['hours']} h ({s['hours'] / 24 / 30.4:.0f} mois)")
        out.append(" · ".join(bits))
        if d.get("usage"):
            u = d["usage"]
            pct = (1 - u["free"] / u["total"]) * 100 if u["total"] else 0
            out.append(f"{bar(pct)} {pct:.0f} % · {human(u['free'])} libres")
        if s.get("wear_pct") is not None:
            out.append(f"Usure {s['wear_pct']} % · {human(s.get('written_bytes') or 0)} écrits")
        out.append("⚠️ " + ", ".join(bad) if bad else "Aucun secteur abîmé ni erreur")
        lt = s.get("last_test")
        if lt:
            ago_h = (s.get("hours") or 0) - (lt.get("at_hours") or 0)
            out.append(f"Dernier test : {'✅' if lt.get('passed') else '❌'} {e(lt.get('type') or '')} il y a {ago_h // 24} j")
        else:
            out.append("Dernier test : aucun encore (test court automatique chaque semaine)")
    fu = h.get("failed_units") or []
    out.append("\n⚙️ Services système : " + ("tous OK ✅" if not fu else f"{len(fu)} en échec ⚠️"))
    groups: dict[str, int] = {}
    for u in fu:
        base = u.split("@")[0].replace(".service", "")
        groups[base] = groups.get(base, 0) + 1
    for base, n in list(groups.items())[:6]:
        out.append(f"• <code>{e(base)}</code>" + (f" ×{n}" if n > 1 else ""))
    out.append(f"\n<i>Relevé il y a {age:.0f} min</i>" + (" ⚠️ relevé en retard" if age > 30 else ""))
    return "\n".join(out)


HEALTH_STATE = f"{DATA}/health_alerted.json"


async def health_alerts():
    """Appelé par le watchdog : n'alerte que sur ce qui change ou empire."""
    h = health()
    try:
        seen = json.load(open(HEALTH_STATE))
    except (OSError, ValueError):
        seen = {}
    alerts, new = [], {}
    if not h or time.time() - h["at"] > 45 * 60:
        new["stale"] = 1
        if not seen.get("stale"):
            alerts.append("🩺 Le relevé de santé du NAS ne se met plus à jour (doktortv-health.timer).")
    else:
        t = h.get("cpu_temp") or 0
        new["cpu_hot"] = int(t >= CPU_HOT)
        if new["cpu_hot"] and not seen.get("cpu_hot"):
            alerts.append(f"🔥 Processeur à {t:.0f} °C.")
        for d in h.get("disks", []):
            s, n = d.get("smart", {}), d["name"]
            lab = f"{disk_label(d)} ({n})"
            if s.get("passed") is False and not seen.get(f"{n}:fail"):
                alerts.append(f"🔴 <b>{e(lab)} : SMART en échec</b> — sauvegarde ce qui compte, le disque peut lâcher.")
            new[f"{n}:fail"] = int(s.get("passed") is False)
            for k, v in WATCH_KEYS.items():
                cur = s.get(k) or 0
                new[f"{n}:{k}"] = max(cur, seen.get(f"{n}:{k}", 0))
                if cur > seen.get(f"{n}:{k}", 0):
                    alerts.append(f"⚠️ <b>{e(lab)}</b> : {v} {seen.get(f'{n}:{k}', 0)} → <b>{cur}</b>")
            hot = HDD_HOT if d.get("rotational") else NVME_HOT
            new[f"{n}:hot"] = int((s.get("temp") or 0) >= hot)
            if new[f"{n}:hot"] and not seen.get(f"{n}:hot"):
                alerts.append(f"🔥 {e(lab)} à {s['temp']} °C.")
            lt = s.get("last_test") or {}
            key = f"{n}:test:{lt.get('at_hours')}"
            new[key] = 1
            if lt and lt.get("passed") is False and not seen.get(key):
                alerts.append(f"❌ {e(lab)} : test SMART en échec ({e(lt.get('status') or '')}).")
        for u in h.get("failed_units") or []:
            new[f"unit:{u}"] = 1
            if not seen.get(f"unit:{u}") and seen:  # pas d'alerte au tout premier passage
                alerts.append(f"⚙️ Service en échec sur le NAS : <code>{e(u)}</code>")
    json.dump(new, open(HEALTH_STATE, "w"))
    for a in alerts:
        await notify(a)


# ---------------------------------------------------------------- vues
async def view_downloads() -> str:
    out = ["📥 <b>Téléchargements en cours</b>"]
    n = 0
    for app, inc in (("radarr", "includeMovie=true"), ("sonarr", "includeSeries=true&includeEpisode=true")):
        try:
            q = await api(app, "GET", f"queue?pageSize=50&{inc}")
        except Exception as ex:
            out.append(f"⚠️ {app} : {e(str(ex))[:80]}")
            continue
        for r in q.get("records", []):
            n += 1
            size, left = r.get("size") or 0, r.get("sizeleft") or 0
            pct = (1 - left / size) * 100 if size else 0
            if app == "radarr":
                name = (r.get("movie") or {}).get("title") or r.get("title")
            else:
                ep = r.get("episode") or {}
                name = f"{(r.get('series') or {}).get('title', '?')} S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
            eta = r.get("timeleft") or ""
            state = r.get("trackedDownloadState") or r.get("status") or ""
            warn = " ⚠️" if r.get("trackedDownloadStatus") in ("warning", "error") else ""
            out.append(f"\n{'🎬' if app == 'radarr' else '📺'} {e(name)}{warn}\n{bar(pct)} {pct:.0f}% · {human(size)}"
                       + (f" · reste {e(eta)}" if eta and pct < 100 else f" · {e(state)}"))
    if not n:
        out.append("\nRien en cours. 😴")
    return "\n".join(out)


async def view_new() -> str:
    items = []
    for app, inc in (("radarr", "includeMovie=true"), ("sonarr", "includeSeries=true&includeEpisode=true")):
        try:
            h = await api(app, "GET", f"history?pageSize=40&sortKey=date&sortDirection=descending&eventType=3&{inc}")
        except Exception:
            continue
        for r in h.get("records", []):
            if app == "radarr":
                m = r.get("movie") or {}
                key = f"🎬 {m.get('title', r.get('sourceTitle', '?'))} ({m.get('year', '')})"
                items.append((r.get("date", ""), key, None))
            else:
                s, ep = r.get("series") or {}, r.get("episode") or {}
                items.append((r.get("date", ""), f"📺 {s.get('title', '?')}",
                              f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"))
    items.sort(reverse=True)
    groups: dict[str, dict] = {}
    for date, key, ep in items:  # regroupe les épisodes d'une même série
        g = groups.setdefault(key, {"date": date, "eps": []})
        if ep and ep not in g["eps"]:
            g["eps"].append(ep)
    out = ["🆕 <b>Derniers ajouts</b>\n"]
    for key, g in list(groups.items())[:12]:
        d = g["date"][:10].split("-")
        when = f" <i>{d[2]}/{d[1]}</i>" if len(d) == 3 else ""
        eps = sorted(g["eps"])
        detail = "" if not eps else (f" {eps[0]}" if len(eps) == 1 else f" — {len(eps)} épisodes ({eps[0]}→{eps[-1]})")
        out.append(f"• {e(key)}{detail}{when}")
    if len(out) == 1:
        out.append("Aucun import récent.")
    return "\n".join(out)


async def view_calendar() -> str:
    now = datetime.now(timezone.utc)
    s, end_ep, end_mv = now.strftime("%Y-%m-%d"), (now + timedelta(days=10)).strftime("%Y-%m-%d"), (now + timedelta(days=45)).strftime("%Y-%m-%d")
    out = ["📅 <b>À venir</b>\n", "<b>Épisodes (10 jours)</b>"]
    try:
        eps = await api("sonarr", "GET", f"calendar?start={s}&end={end_ep}&includeSeries=true")
        for x in eps[:15]:
            when = (x.get("airDateUtc") or "")[:10].split("-")
            out.append(f"• {e(x.get('series', {}).get('title', '?'))} S{x['seasonNumber']:02d}E{x['episodeNumber']:02d}"
                       + (f" — {when[2]}/{when[1]}" if len(when) == 3 else "") + (" ✅" if x.get("hasFile") else ""))
        if not eps:
            out.append("Rien de prévu.")
    except Exception as ex:
        out.append(f"⚠️ {e(str(ex))[:80]}")
    out.append("\n<b>Films (45 jours)</b>")
    try:
        mv = await api("radarr", "GET", f"calendar?start={s}&end={end_mv}")
        for x in mv[:12]:
            d = x.get("digitalRelease") or x.get("physicalRelease") or x.get("inCinemas") or ""
            dd = d[:10].split("-")
            out.append(f"• {e(x.get('title', '?'))}" + (f" — {dd[2]}/{dd[1]}" if len(dd) == 3 else "") + (" ✅" if x.get("hasFile") else ""))
        if not mv:
            out.append("Rien de prévu.")
    except Exception as ex:
        out.append(f"⚠️ {e(str(ex))[:80]}")
    return "\n".join(out)


def cpu_temp() -> float | None:
    temps = []
    base = "/sys/class/thermal"
    try:
        for z in os.listdir(base):
            try:
                temps.append(int(open(f"{base}/{z}/temp").read()) / 1000)
            except (OSError, ValueError):
                pass
    except OSError:
        return None
    return max(temps) if temps else None


async def view_server() -> str:
    up = float(open("/proc/uptime").read().split()[0])
    load = open("/proc/loadavg").read().split()[:3]
    mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    mt, ma = mem["MemTotal"], mem["MemAvailable"]
    out = ["🖥️ <b>Serveur DoktorTV</b>\n",
           f"⏱️ Allumé depuis {int(up // 86400)} j {int(up % 86400 // 3600)} h",
           f"⚙️ Charge : {' / '.join(load)}",
           f"🧠 RAM : {bar((1 - ma / mt) * 100)} {human(mt - ma)} / {human(mt)}"]
    t = cpu_temp()
    if t:
        out.append(f"🌡️ CPU : {t:.0f} °C")
    out.append("")
    for label, path in (("💾 Stockage (HDD)", "/pool"), ("⚡ SSD système", "/data")):
        try:
            total, free, pct = disk(path)
            out.append(f"{label}\n{bar(pct)} {pct:.0f}% · {human(free)} libres sur {human(total)}")
        except OSError:
            pass
    try:
        cs = await docker("GET", "/containers/json?all=1")
        running = sum(1 for c in cs if c["State"] == "running")
        out.append(f"\n🐳 Conteneurs : {running}/{len(cs)} actifs")
    except Exception:
        pass
    return "\n".join(out)


async def view_containers():
    cs = await docker("GET", "/containers/json?all=1")
    cs.sort(key=lambda c: c["Names"][0])
    lines, rows, row = ["🐳 <b>Conteneurs</b> — touche pour redémarrer\n"], [], []
    for c in cs:
        name = c["Names"][0].lstrip("/")
        ok = c["State"] == "running"
        health = " 🩺" if "unhealthy" in c.get("Status", "") else ""
        lines.append(f"{'🟢' if ok else '🔴'} {e(name)} <i>{e(c.get('Status', ''))}</i>{health}")
        row.append(B(f"{'🔄' if ok else '▶️'} {name}"[:30], callback_data=f"rs:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("🔃 Actualiser", callback_data="ctr")] + BACK)
    return "\n".join(lines), M(rows)


# ---------------------------------------------------------------- boutons
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        await q.answer("⛔ Bot privé", show_alert=True)
        return
    data = q.data or ""
    await q.answer()
    try:
        if data == "pg:noop":
            return
        if data == "tr" or data.startswith("tr:"):
            await on_transmission_button(update, ctx, data)
            return
        if data == "hl":
            await show(update, view_health(), M([[B("🔃 Actualiser", callback_data="hl")] + BACK]))
            return
        if data == "cs" or data.startswith("cs:"):
            await on_crossseed_button(update, data)
            return
        if data == "pm" or data.startswith(("pl:", "pg:")) or data == "ps":
            await on_purge_button(update, ctx, data)
            return
        if data == "menu":
            ctx.user_data.pop("await", None)
            await show(update, "🎛️ <b>DoktorTV</b> — panneau de contrôle", MAIN)
        elif data == "search":
            await show(update, "🔎 Envoie-moi le titre d'un film ou d'une série.", M([BACK]))
        elif data == "dl":
            await show(update, await view_downloads(), M([[B("🔃 Actualiser", callback_data="dl")] + BACK]))
        elif data == "new":
            await show(update, await view_new(), M([BACK]))
        elif data == "cal":
            await show(update, await view_calendar(), M([BACK]))
        elif data == "srv":
            await show(update, await view_server(), M([[B("🔃 Actualiser", callback_data="srv")] + BACK]))
        elif data == "ctr":
            text, kb = await view_containers()
            await show(update, text, kb)
        elif data == "scan":
            await jellyfin_scan()
            await show(update, "🔄 Scan de la bibliothèque Jellyfin lancé.", M([BACK]))
        elif data.startswith("rs:"):
            name = data[3:]
            await show(update, f"Redémarrer <b>{e(name)}</b> ?", M([[B("✅ Oui", callback_data=f"rsok:{name}"),
                                                                     B("❌ Non", callback_data="ctr")]]))
        elif data.startswith("rsok:"):
            name = data[5:]
            await show(update, f"🔄 Redémarrage de <b>{e(name)}</b>…", None)
            await docker("POST", f"/containers/{quote(name)}/restart?t=20")
            text, kb = await view_containers()
            await show(update, f"✅ {e(name)} redémarré.\n\n" + text, kb)
        elif data.startswith("m:"):
            _, mtype, mid = data.split(":")
            await media_detail(update, mtype, int(mid))
        elif data.startswith("req:"):
            _, mtype, mid = data.split(":")
            await media_request(update, mtype, int(mid))
        elif data.startswith("ss:"):
            await season_picker(update, ctx, int(data[3:]), "open")
        elif data.startswith("st:"):
            _, mid, n = data.split(":")
            await season_picker(update, ctx, int(mid), "toggle", int(n))
        elif data.startswith("sa:"):
            await season_picker(update, ctx, int(data[3:]), "all")
        elif data.startswith("sx:"):
            pass  # saison non sélectionnable / rien coché
        elif data.startswith("sr:"):
            mid = int(data[3:])
            sel = ctx.user_data.get("seasons", {}).get(mid, {}).get("sel") or set()
            if sel:
                await media_request(update, "tv", mid, list(sel))
                ctx.user_data["seasons"].pop(mid, None)
    except Exception as ex:
        log.exception("bouton %s", data)
        await update.effective_chat.send_message(f"❌ Erreur : {e(str(ex))[:300]}", parse_mode="HTML",
                                                 reply_markup=M([BACK]))


# ---------------------------------------------------------------- notifications
async def notify(text: str, photo: str | None = None):
    oid = owner_id()
    if not oid:
        return
    try:
        if photo:
            await APP.bot.send_photo(oid, photo, caption=text[:1024], parse_mode="HTML")
        else:
            await APP.bot.send_message(oid, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        log.exception("notification")
        if photo:
            await APP.bot.send_message(oid, text, parse_mode="HTML", disable_web_page_preview=True)


def poster_of(obj: dict) -> str | None:
    for img in obj.get("images") or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl") or img.get("url")
    return None


async def hook(request: web.Request):
    if request.match_info["secret"] != HOOK_SECRET:
        return web.Response(status=403)
    src = request.match_info["src"]
    try:
        p = await request.json()
    except Exception:
        return web.Response(status=400)
    ev = p.get("eventType") or p.get("notification_type") or ""
    try:
        if src == "radarr":
            m = p.get("movie") or {}
            t = f"{e(m.get('title', '?'))} ({m.get('year', '')})"
            if ev == "Download":
                q = (p.get("movieFile") or {}).get("quality", "")
                await notify(f"🎬 <b>{'Mis à niveau' if p.get('isUpgrade') else 'Nouveau film dispo'}</b>\n{t}" + (f"\n<i>{e(q)}</i>" if q else ""), poster_of(m))
            elif ev == "Grab":
                await notify(f"⏬ Téléchargement lancé : {t}")
            elif ev == "Test":
                await notify("🧪 Test Radarr reçu ✅")
        elif src == "sonarr":
            s = p.get("series") or {}
            eps = ", ".join(f"S{x.get('seasonNumber', 0):02d}E{x.get('episodeNumber', 0):02d}" for x in p.get("episodes", [])[:6])
            t = f"{e(s.get('title', '?'))} {eps}"
            if ev == "Download":
                await notify(f"📺 <b>{'Mis à niveau' if p.get('isUpgrade') else 'Nouvel épisode dispo'}</b>\n{t}", poster_of(s))
            elif ev == "Grab":
                await notify(f"⏬ Téléchargement lancé : {t}")
            elif ev == "Test":
                await notify("🧪 Test Sonarr reçu ✅")
        elif src == "seerr":
            icon = {"MEDIA_PENDING": "⏳", "MEDIA_APPROVED": "👍", "MEDIA_AUTO_APPROVED": "👍", "MEDIA_AVAILABLE": "✅",
                    "MEDIA_DECLINED": "🚫", "MEDIA_FAILED": "❌", "TEST_NOTIFICATION": "🧪"}.get(ev, "🔔")
            await notify(f"{icon} <b>{e(p.get('subject') or ev)}</b>\n{e((p.get('message') or '')[:400])}", p.get("image") or None)
    except Exception:
        log.exception("webhook %s", src)
    return web.Response(text="ok")


async def watchdog():
    """Alertes : disque presque plein, conteneur arrêté."""
    alerted_disk, alerted_ctr = set(), set()
    await asyncio.sleep(60)
    while True:
        try:
            for label, path in (("Stockage HDD", "/pool"), ("SSD système", "/data")):
                _, free, pct = disk(path)
                if pct >= DISK_ALERT and label not in alerted_disk:
                    alerted_disk.add(label)
                    await notify(f"⚠️ <b>{label} plein à {pct:.0f}%</b> — {human(free)} libres.")
                elif pct < DISK_ALERT - 2:
                    alerted_disk.discard(label)
            cs = await docker("GET", "/containers/json?all=1")
            down = {c["Names"][0].lstrip("/") for c in cs if c["State"] != "running"}
            for name in down - alerted_ctr:
                await notify(f"🔴 <b>Conteneur arrêté</b> : {e(name)}")
            for name in alerted_ctr - down:
                await notify(f"🟢 Conteneur reparti : {e(name)}")
            alerted_ctr = down
        except Exception:
            log.exception("watchdog")
        try:
            await health_alerts()
        except Exception:
            log.exception("alertes santé")
        await asyncio.sleep(300)


# ---------------------------------------------------------------- démarrage
async def post_init(app: Application):
    global SESSION
    SESSION = aiohttp.ClientSession()
    web_app = web.Application()
    web_app.router.add_post("/hook/{secret}/{src}", hook)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HOOK_PORT).start()
    asyncio.get_running_loop().create_task(watchdog())
    await app.bot.set_my_commands([("start", "Ouvrir le panneau"), ("cherche", "Chercher un film ou une série"),
                                    ("menage", "Proposer des films/séries à supprimer"),
                                    ("supprime", "Chercher un titre à supprimer"),
                                    ("crossseed", "Lancer / suivre cross-seed"),
                                    ("transmission", "État et gestion de Transmission"),
                                    ("sante", "Santé du NAS et des disques")])
    log.info("DoktorTV_Bot prêt, webhooks sur :%s", HOOK_PORT)


def main():
    global APP
    APP = Application.builder().token(TOKEN).post_init(post_init).build()
    APP.add_handler(CommandHandler(["start", "menu"], start))
    APP.add_handler(CommandHandler("cherche", cmd_search))
    APP.add_handler(CommandHandler(["menage", "supprime"], cmd_purge))
    APP.add_handler(CommandHandler("crossseed", cmd_crossseed))
    APP.add_handler(CommandHandler("transmission", cmd_transmission))
    APP.add_handler(CommandHandler("sante", cmd_health))
    APP.add_handler(CallbackQueryHandler(on_button))
    APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    APP.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Collecte des flux de veille DirectVelo -> flux_2h.md

Récupère les publications des dernières 24 heures (DirectVelo, Le Gruppetto, PQR,
médias spécialisés, Bluesky, Reddit, FFC Ressources) et écrit un fichier Markdown
statique, avec en fin de fichier le statut HTTP de chaque source.

Toutes les adresses sont regroupées dans CONFIG ci-dessous : si une source passe
en erreur dans le tableau de statut, il suffit de corriger son URL ici.
Pour chaque site, plusieurs URL de flux sont essayées dans l'ordre ; si aucune
ne répond, le script cherche le flux annoncé dans la page d'accueil
(<link rel="alternate" type="application/rss+xml">).
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(hours=24)
OUTPUT = "flux_2h.md"
TIMEOUT = 20
MAX_ITEMS = 40  # par section

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DirectVeloVeilleBot/1.0; veille éditoriale)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

PQR_KEYWORDS = ["cyclisme", "vélo", "velo", "peloton", "coureur", "n1", "n2", "n3",
                "mercato", "cyclo-cross", "cyclocross"]
GRUPPETTO_TOPICS = ["transfert", "équipe", "equipe", "amateur", "cyclo-cross", "cyclocross"]
BLUESKY_QUERIES = ["DirectVelo", "TotalEnergies", "Mercato cyclisme"]

CONFIG = {
    "directvelo": {
        "name": "DirectVelo",
        "feeds": ["https://www.directvelo.com/rss/actualites", "https://www.directvelo.com/rss"],
        "home": "https://www.directvelo.com/actualites",
    },
    "gruppetto": {
        "name": "Le Gruppetto (forum)",
        # Flux par forum (phpBB) : f=12 « Cyclisme sur route : Professionnels » (mercato,
        # équipes, amateurs), f=13 « Cyclo-Cross, Piste, VTT & Autres disciplines ».
        "forums": {"Route (pros, mercato, équipes, amateurs)": "https://legruppetto.fr/forum/feed.php?f=12",
                   "Cyclo-cross, piste, VTT": "https://legruppetto.fr/forum/feed.php?f=13"},
        "feeds": ["https://legruppetto.fr/forum/feed.php"],
        "home": "https://legruppetto.fr/forum/",
    },
    "pqr": [
        {"name": "Ouest-France", "feeds": ["https://www.ouest-france.fr/rss/sport", "https://www.ouest-france.fr/rss-en-continu.xml"], "home": "https://www.ouest-france.fr/sport/"},
        {"name": "Le Télégramme", "feeds": ["https://www.letelegramme.fr/rss/sports.xml", "https://www.letelegramme.fr/sports/rss.xml"], "home": "https://www.letelegramme.fr/sports/"},
        {"name": "Le Progrès", "feeds": ["https://www.leprogres.fr/sport/rss", "https://www.leprogres.fr/rss"], "home": "https://www.leprogres.fr/sport"},
        {"name": "Sud Ouest", "feeds": ["https://www.sudouest.fr/sport/rss.xml", "https://www.sudouest.fr/rss.xml"], "home": "https://www.sudouest.fr/sport/"},
        {"name": "La Voix du Nord", "feeds": ["https://www.lavoixdunord.fr/sports/rss", "https://www.lavoixdunord.fr/rss"], "home": "https://www.lavoixdunord.fr/sports"},
        {"name": "Le Dauphiné Libéré", "feeds": ["https://www.ledauphine.com/sport/rss", "https://www.ledauphine.com/rss"], "home": "https://www.ledauphine.com/sport"},
    ],
    "medias": [
        {"name": "Cyclism'Actu", "feeds": ["https://www.cyclismactu.net/rss_social.xml", "https://www.cyclismactu.net/rss.xml"], "home": "https://www.cyclismactu.net/"},
        {"name": "Velo101", "feeds": ["https://www.velo101.com/feed", "https://www.velo101.com/feed/"], "home": "https://www.velo101.com/"},
        {"name": "WielerFlits", "feeds": ["https://www.wielerflits.nl/feed/", "https://www.wielerflits.nl/rss"], "home": "https://www.wielerflits.nl/"},
        {"name": "In de Leiderstrui", "feeds": ["https://www.indeleiderstrui.nl/sitemap/news.xml", "https://www.indeleiderstrui.nl/feed"], "home": "https://www.indeleiderstrui.nl/"},
        {"name": "Cyclingnews", "feeds": ["https://www.cyclingnews.com/feeds.xml", "https://www.cyclingnews.com/rss/"], "home": "https://www.cyclingnews.com/"},
    ],
    "reddit": {"name": "Reddit r/peloton", "feeds": ["https://www.reddit.com/r/peloton/new/.rss"], "home": None},
    "ffc": {"name": "FFC Ressources", "urls": ["https://ffc.fr/ressources", "https://www.ffc.fr/ressources/"]},
}

STATUS = []  # (source, url, code, nb_elements, detail)


def record(source, url, code, count=0, detail=""):
    STATUS.append((source, url, code, count, detail))


def get(url, **kw):
    """GET avec gestion d'erreur ; renvoie (response | None, code_texte)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
        return r, f"{r.status_code} {'OK' if r.ok else r.reason}"
    except requests.RequestException as e:
        return None, f"Erreur ({type(e).__name__})"


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def fmt_time(dt):
    if not dt:
        return "heure inconnue"
    return dt.astimezone(PARIS).strftime("%d/%m %Hh%M")


def discover_feed(home):
    if not home:
        return None
    r, _ = get(home)
    if not r or not r.ok:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    link = soup.find("link", rel="alternate", type=re.compile(r"(rss|atom)\+xml"))
    return urljoin(home, link["href"]) if link and link.get("href") else None


def fetch_feed(source):
    """Essaie chaque URL de flux, puis l'auto-découverte. Renvoie la liste des entrées."""
    tried = list(source.get("feeds", []))
    found = None
    for url in tried:
        r, code = get(url)
        if r is not None and r.ok:
            parsed = feedparser.parse(r.content)
            if parsed.entries:
                record(source["name"], url, code, len(parsed.entries))
                return parsed.entries
            record(source["name"], url, code, 0, "réponse sans entrées de flux")
        else:
            record(source["name"], url, code)
    found = discover_feed(source.get("home"))
    if found and found not in tried:
        r, code = get(found)
        if r is not None and r.ok:
            parsed = feedparser.parse(r.content)
            record(source["name"], found, code, len(parsed.entries), "flux auto-découvert")
            return parsed.entries
        record(source["name"], found, code, 0, "flux auto-découvert")
    return []


def recent(entries, keywords=None, field_fn=None):
    """Filtre sur 24 h (les entrées sans date sont gardées) et, si besoin, sur des mots-clés."""
    out = []
    for e in entries:
        dt = entry_datetime(e)
        if dt and dt < SINCE:
            continue
        if keywords:
            text = (field_fn(e) if field_fn else f"{e.get('title', '')} {e.get('summary', '')}").lower()
            if not any(re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", text) for k in keywords):
                continue
        out.append((dt, e.get("title", "(sans titre)").strip(), e.get("link", "")))
    out.sort(key=lambda x: x[0] or NOW, reverse=True)
    return out[:MAX_ITEMS]


def lines(items, prefix=""):
    if not items:
        return ["_Aucune publication dans les 24 dernières heures (ou source indisponible, voir le statut technique)._", ""]
    res = [f"- {fmt_time(dt)} — {prefix}[{title}]({link})" for dt, title, link in items]
    return res + [""]


# ---------------------------------------------------------------- sections

def section_directvelo():
    src = CONFIG["directvelo"]
    items = recent(fetch_feed(src))
    if items:
        return items
    # Repli : lecture de la page des actualités
    r, code = get(src["home"])
    record(src["name"] + " (page actualités)", src["home"], code)
    if not r or not r.ok:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen, out = set(), []
    for a in soup.select("a[href*='/actualite/']"):
        href = urljoin(src["home"], a["href"])
        title = a.get_text(" ", strip=True)
        if not title or href in seen:
            continue
        seen.add(href)
        out.append((None, title, href))
    STATUS[-1] = (STATUS[-1][0], STATUS[-1][1], STATUS[-1][2], len(out), "repli par lecture de la page (heures non disponibles)")
    return out[:MAX_ITEMS]


def section_gruppetto():
    """Renvoie [(nom_du_forum, éléments)] à partir des flux par forum ;
    repli sur le flux général filtré par sujets si ces flux ne répondent pas."""
    src = CONFIG["gruppetto"]
    blocks = []
    for label, url in src["forums"].items():
        blocks.append((label, recent(fetch_feed({"name": f"Le Gruppetto ({label})", "feeds": [url]}))))
    if any(items for _, items in blocks) or any(s[2].startswith("200") for s in STATUS if s[0].startswith("Le Gruppetto (")):
        return blocks
    entries = fetch_feed(src)

    def text(e):
        cats = " ".join(t.get("term", "") + " " + (t.get("label") or "") for t in e.get("tags", []))
        return f"{cats} {e.get('title', '')}"

    return [("Flux général filtré", recent(entries, GRUPPETTO_TOPICS, text))]


def section_pqr():
    blocks = []
    for src in CONFIG["pqr"]:
        blocks.append((src["name"], recent(fetch_feed(src), PQR_KEYWORDS)))
    return blocks


def section_medias():
    return [(src["name"], recent(fetch_feed(src))) for src in CONFIG["medias"]]


def bluesky_token():
    handle, password = os.getenv("BSKY_HANDLE"), os.getenv("BSKY_APP_PASSWORD")
    if not handle or not password:
        return None
    try:
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                          json={"identifier": handle, "password": password}, timeout=TIMEOUT)
        return r.json().get("accessJwt") if r.ok else None
    except requests.RequestException:
        return None


def section_bluesky():
    out = []
    token = None
    for q in BLUESKY_QUERIES:
        params = f"q={quote_plus(q)}&limit=25&sort=latest&since={SINCE.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        url = f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?{params}"
        r, code = get(url)
        if (r is None or r.status_code in (401, 403)) and token is None:
            token = bluesky_token() or ""
        if (r is None or not r.ok) and token:
            url = f"https://bsky.social/xrpc/app.bsky.feed.searchPosts?{params}"
            try:
                r = requests.get(url, headers={**HEADERS, "Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
                code = f"{r.status_code} {'OK' if r.ok else r.reason} (authentifié)"
            except requests.RequestException as e:
                r, code = None, f"Erreur ({type(e).__name__})"
        posts = r.json().get("posts", []) if r is not None and r.ok else []
        record(f"Bluesky « {q} »", url.split("?")[0], code, len(posts))
        for p in posts:
            try:
                dt = datetime.fromisoformat(p["record"]["createdAt"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                dt = None
            if dt and dt < SINCE:
                continue
            handle = p.get("author", {}).get("handle", "?")
            rkey = p.get("uri", "").rsplit("/", 1)[-1]
            text = re.sub(r"\s+", " ", p.get("record", {}).get("text", ""))[:200]
            likes, reposts = p.get("likeCount", 0), p.get("repostCount", 0)
            out.append((dt, f"@{handle} : {text} ({likes} j'aime, {reposts} reposts)",
                        f"https://bsky.app/profile/{handle}/post/{rkey}", q))
    out.sort(key=lambda x: x[0] or NOW, reverse=True)
    return out[:MAX_ITEMS]


def section_reddit():
    return recent(fetch_feed(CONFIG["reddit"]))


def section_ffc():
    src = CONFIG["ffc"]
    for url in src["urls"]:
        r, code = get(url)
        if not r or not r.ok:
            record(src["name"], url, code)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        seen, out = set(), []
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            # Les documents ont la forme /ressources/<rubrique>/<fichier>/
            if not re.search(r"/ressources/[^/?#]+/[^/?#]+/?$", href) or href in seen:
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or a.get("title", "") or href)
            title = re.sub(r"^Document\s+", "", title)
            seen.add(href)
            out.append((title, href))
        detail = "" if out else "page reçue mais aucun lien de document trouvé (page de protection ou contenu chargé en JavaScript ?)"
        record(src["name"], url, code, len(out), detail)
        if out:
            return out
    return []


# ---------------------------------------------------------------- écriture

def main():
    md = [
        "# Flux de veille DirectVelo (tampon 2 h)",
        "",
        f"Généré le {NOW.astimezone(PARIS).strftime('%d/%m/%Y à %Hh%M')} (heure de Paris). "
        f"Fenêtre : publications depuis le {SINCE.astimezone(PARIS).strftime('%d/%m à %Hh%M')}.",
        "",
    ]

    md += ["## 1. Nos publications internes (DirectVelo)", ""] + lines(section_directvelo())
    md += ["## 2. Forum Le Gruppetto (route pros : mercato, équipes, amateurs ; cyclo-cross)", ""]
    for name, items in section_gruppetto():
        md += [f"### {name}", ""] + lines(items)

    md += ["## 3. Presse quotidienne régionale (filtrée sur le cyclisme)", ""]
    for name, items in section_pqr():
        md += [f"### {name}", ""] + lines(items)

    md += ["## 4. Médias spécialisés", ""]
    for name, items in section_medias():
        md += [f"### {name}", ""] + lines(items)

    md += ["## 5. Réseaux sociaux", "", "### Bluesky", ""]
    bsky = section_bluesky()
    if bsky:
        md += [f"- {fmt_time(dt)} — [{q}] [{text}]({link})" for dt, text, link, q in bsky] + [""]
    else:
        md += lines([])
    md += ["### Reddit r/peloton", ""] + lines(section_reddit())

    md += ["## 6. Sources primaires : FFC Ressources (page 1)", ""]
    ffc = section_ffc()
    md += [f"- [{t}]({u})" for t, u in ffc] + [""] if ffc else lines([])

    md += ["## 7. Statut technique des sources", "",
           "| Source | URL interrogée | Code HTTP | Éléments | Remarque |",
           "|---|---|---|---|---|"]
    for source, url, code, count, detail in STATUS:
        md.append(f"| {source} | {url} | {code} | {count} | {detail} |")
    md.append("")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"{OUTPUT} écrit ({len(STATUS)} requêtes).")


if __name__ == "__main__":
    main()

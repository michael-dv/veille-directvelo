#!/usr/bin/env python3
"""Collecte des flux de veille DirectVelo -> flux_2h.md

Récupère les publications des dernières 24 heures (DirectVelo, Le Gruppetto, PQR,
médias spécialisés, Bluesky, YouTube, Reddit, relais Google News des réseaux
non lisibles (X, Instagram, TikTok, Facebook, LinkedIn), FFC Ressources, presse internationale,
sources officielles et antidopage, bases de transferts et cyclo-cross), ajoute en
tête un radar « Tendances & signaux Google Discover » (Google Trends France,
médias sportifs grand public, sujets cités par 3 sources ou plus) et écrit
un fichier Markdown statique, avec en fin de fichier le statut HTTP de chaque source.

Toutes les adresses sont regroupées dans CONFIG ci-dessous : si une source passe
en erreur dans le tableau de statut, il suffit de corriger son URL ici.
Pour chaque site, plusieurs URL de flux sont essayées dans l'ordre ; si aucune
ne répond, le script cherche le flux annoncé dans la page d'accueil
(<link rel="alternate" type="application/rss+xml">), puis, si la source a une
clé "gn", une recherche Google News équivalente.
"""

import json
import os
import re
import time
import unicodedata
from collections import defaultdict
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(hours=24)
OUTPUT = "flux_2h.md"
OUTPUT_DELTA = "flux_delta.md"  # version légère : Tendances, Nouveautés, Statut technique
TIMEOUT = 10  # secondes par requête
MAX_ITEMS = 40  # par section
ETAT = "etat"                                  # état persistant, versionné dans le dépôt
VUS = f"{ETAT}/vus.json"                       # URL -> première détection (7 jours glissants)
TRAITES = f"{ETAT}/traites.jsonl"              # écrit par l'agent de veille : sujets/URL déjà traités
FFC_CONNUS = f"{ETAT}/ffc_connus.json"         # URL des documents FFC -> première détection
SURVEILLANCE = f"{ETAT}/surveillance.txt"      # noms surveillés (un par ligne, # = commentaire)
FFC_NEW_HOURS = 3                              # un document FFC reste « nouveau » 3 h

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DirectVeloVeilleBot/1.0; veille éditoriale)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

PQR_KEYWORDS = ["cyclisme", "vélo", "velo", "peloton", "coureur", "n1", "n2", "n3",
                "mercato", "cyclo-cross", "cyclocross"]
GRUPPETTO_TOPICS = ["transfert", "équipe", "equipe", "amateur", "cyclo-cross", "cyclocross"]
BLUESKY_QUERIES = ["DirectVelo", "mercato cyclisme", "transfert cyclisme", "cyclisme N1", "cyclo-cross", "Seixas", "TotalEnergies cyclisme",
                   "Groupama-FDJ", "Decathlon CMA CGM", "Cofidis", "FFC cyclisme", "cyclisme féminin"]  # recherche : seulement si les secrets Bluesky existent
# Comptes Bluesky lus sans authentification (getAuthorFeed est public) : liste éditable dans etat/bluesky.txt
# (un identifiant par ligne, # = commentaire). Repli si le fichier manque :
BLUESKY_FILE = f"{ETAT}/bluesky.txt"
BLUESKY_ACTORS = ["directvelo.bsky.social"]
BLUESKY_PER_ACCOUNT = 6   # posts gardés au plus par compte et par génération
BLUESKY_MAX = 200         # posts Bluesky au plus dans le flux
# Chaînes YouTube suivies : liste éditable dans etat/youtube.txt (« ID de chaîne | nom | velo » ;
# « velo » = chaîne généraliste, on ne garde que les vidéos dont le titre parle de cyclisme).
YOUTUBE_FILE = f"{ETAT}/youtube.txt"
YOUTUBE_HOURS = 48  # les vidéos sont plus rares que les articles : fenêtre de 48 h
# Relais des réseaux sociaux : articles qui reprennent une annonce faite sur X, Instagram, TikTok, Facebook, LinkedIn ou YouTube
SOCIAL_RELAY_QUERIES = [
    ('(cyclisme OR cycliste OR coureur OR coureuse OR équipe OR club) ("sur Instagram" OR "sur X" OR "sur TikTok" OR '
     '"sur Facebook" OR "sur LinkedIn" OR "sur YouTube" OR "sur ses réseaux" OR "sur les réseaux sociaux" OR "sur Twitter")', "fr"),
    ('(cycling OR cyclist OR "pro cycling" OR WorldTour) ("on Instagram" OR "on social media" OR "on X" OR "on TikTok" OR "on Twitter")', "en"),
    ('(wielrenner OR wielrennen OR veldrijder OR renner) ("op Instagram" OR "op sociale media" OR "op X" OR "op Twitter")', "nl"),
]


GN_EDITIONS = {
    "fr": "hl=fr&gl=FR&ceid=FR:fr",
    "nl": "hl=nl&gl=BE&ceid=BE:nl",
    "it": "hl=it&gl=IT&ceid=IT:it",
    "es": "hl=es&gl=ES&ceid=ES:es",
    "en": "hl=en-US&gl=US&ceid=US:en",
}


# Exclusions dans la requête : désactivées, car Google News renvoie un flux vide pour les requêtes « site: » qui en
# contiennent (constaté le 03/11/2026). Les faits divers sont écartés après coup par gn_ok() (EXCLUDED_TERMS).
GN_EXCLUSIONS = ""


def google_news(query, lang="fr", days=1, hours=None, exclure=True):
    """Flux RSS Google News d'une recherche, limité aux N derniers jours (ou N dernières heures).
    Les liens renvoyés sont des redirections news.google.com vers l'article.
    exclure=True ajoute les exclusions de faits divers (désactivé pour les sources officielles)."""
    window = f"{hours}h" if hours else f"{days}d"
    q = f"{query} {GN_EXCLUSIONS}" if exclure and GN_EXCLUSIONS else query
    return f"https://news.google.com/rss/search?q={quote_plus(f'{q} when:{window}')}&{GN_EDITIONS[lang]}"


# ---------------------------------------------------------------- radar tendances
# Coureurs et équipes suivis en priorité (nom affiché -> variantes recherchées, sans accents ni majuscules).
WATCHLIST = {
    "Paul Seixas": ["seixas"], "Tadej Pogačar": ["pogacar"], "Jonas Vingegaard": ["vingegaard"],
    "Mathieu van der Poel": ["van der poel", "vdp"], "Wout van Aert": ["van aert"], "Remco Evenepoel": ["evenepoel"],
    "Julian Alaphilippe": ["alaphilippe"], "Romain Grégoire": ["romain gregoire"], "Lenny Martinez": ["lenny martinez"],
    "Paul Magnier": ["magnier"], "Kévin Vauquelin": ["vauquelin"], "David Gaudu": ["gaudu"], "Valentin Madouas": ["madouas"],
    "Pauline Ferrand-Prévot": ["ferrand-prevot", "ferrand prevot"], "Demi Vollering": ["vollering"],
    "Primož Roglič": ["roglic"], "Thibau Nys": ["thibau nys"], "Émilien Jacquelin": ["jacquelin"],
    "Decathlon CMA CGM": ["decathlon"], "Groupama-FDJ": ["groupama"], "Cofidis": ["cofidis"],
    "TotalEnergies": ["totalenergies"], "Unibet Rose Rockets": ["unibet", "rose rockets"], "FDJ United-Suez": ["fdj united-suez", "fdj-suez"],
    "Visma-Lease a Bike": ["visma"], "UAE Team Emirates": ["uae team", "uae emirates"], "Soudal Quick-Step": ["soudal"],
    "Lidl-Trek": ["lidl-trek", "lidl trek"], "Netcompany Ineos": ["ineos"], "Alpecin": ["alpecin"], "Red Bull-Bora": ["red bull-bora", "bora"],
    "Tour de France": ["tour de france"], "Giro": ["giro"], "Vuelta": ["vuelta"],
}
TREND_KEYWORDS = ["cyclisme", "cycliste", "velo", "peloton", "tour de france", "giro", "vuelta", "cyclo-cross"] + \
    [v for variants in WATCHLIST.values() for v in variants if len(v) > 4]
# Filtre lexical des résultats Google News : au moins un terme cycliste explicite, aucun terme de fait divers.
CYCLING_TERMS = ["cyclisme", "cycliste", "cyclistes", "velo", "velos", "coureur", "coureurs", "coureuse", "coureuses",
                 "peloton", "etape", "cyclo-cross", "cyclocross", "piste", "velodrome", "koers", "veldrijden", "veldrit",
                 "wielrennen", "wielrenner", "renner", "ciclismo", "ciclista", "cycling", "cyclist", "rider", "riders",
                 "race", "uci", "worldtour", "ffc", "course", "courses", "vtt", "bmx", "gravel", "maillot", "contre-la-montre",
                 "chrono", "sprinteur", "grimpeur", "classique", "cyclosportive", "criterium", "ronde", "giro", "vuelta",
                 "cordon-ragot", "ferrand-prevot", "alaphilippe", "seixas"]
CYCLING_OUTLETS = ["directvelo", "cyclism actu", "cyclismactu", "velo101", "cyclingnews", "wielerflits", "velo club",
                   "tuttobiciweb", "ciclo21", "escape collective", "cyclingweekly", "procyclinguk", "velonews", "idl procycling"]
EXCLUDED_TERMS = ["accident", "proces", "meurtre", "football", "faits divers", "fait divers"]


SURV_TERMS = []  # noms de etat/surveillance.txt (normalisés), chargés dans main() : un nom suivi suffit à garder un titre
REJECTED = []  # (source, titre, lien) écartés par le filtre lexical : liste de contrôle en fin de flux complet


def gn_ok(title, mode="strict"):
    """mode strict : terme cycliste obligatoire + exclusions ; exclude : exclusions seules ; none : tout passe."""
    if mode == "none":
        return True
    n = " " + norm(title) + " "
    if any(f" {w} " in n for w in EXCLUDED_TERMS):
        return False
    if mode == "exclude":
        return True
    terms = CYCLING_TERMS + TREND_KEYWORDS + CYCLING_OUTLETS + SURV_TERMS
    return any(f" {t} " in n or f" {t}-" in n or f"-{t} " in n for t in terms)


# Mots qui ne commencent jamais un nom propre détecté automatiquement
STOP_START = {"le", "la", "les", "l", "un", "une", "des", "du", "de", "d", "ce", "cette", "ces", "il", "elle", "ils", "on",
              "pour", "avec", "dans", "sur", "apres", "avant", "comment", "pourquoi", "quand", "qui", "que", "quel", "quelle",
              "en", "et", "au", "aux", "mais", "ou", "son", "sa", "ses", "leur", "the", "a", "an", "in", "on", "for", "with",
              "how", "why", "what", "het", "een", "il", "lo", "gli", "el", "los", "las", "video", "direct", "officiel",
              "mercato", "transfert", "transferts", "cyclisme", "resultats", "classement", "classements", "photos"}
SOURCE_NAMES = {"directvelo", "google news", "cyclingnews", "velo101", "wielerflits", "l equipe", "sporza", "hln"}


def norm(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 -]+", " ", text.lower()).strip()


def entities(title):
    """Noms propres d'un titre : coureurs/équipes de la WATCHLIST + suites de 2 à 4 mots à majuscule."""
    found, n = set(), " " + norm(title) + " "
    for name, variants in WATCHLIST.items():
        if any(f" {v} " in n or f" {v}-" in n or f"-{v} " in n for v in variants):
            found.add(name)
    for m in re.finditer(r"\b([A-ZÀ-Ý][\w'’-]+(?:\s+(?:van|der|de|du|le|la|den|von|di)?\s*[A-ZÀ-Ý][\w'’-]+){1,3})", title):
        cand = re.sub(r"\s+", " ", m.group(1)).strip()
        words = norm(cand).split()
        if len(words) < 2 or words[0] in STOP_START or norm(cand) in SOURCE_NAMES:
            continue
        if any(norm(cand) in [norm(x) for x in found] or norm(cand) in norm(x) for x in found):
            continue
        found.add(cand)
    return found


TRACKED = defaultdict(set)  # entité -> ensemble des sources qui la citent


def track(source, items):
    mark_new(source, items)
    for item in items:
        title = item[1] if len(item) > 2 else item[0]
        for ent in entities(title):
            TRACKED[ent].add(source)
    return items


def velocity(min_sources=3, top=10):
    hot = sorted(((e, len(s)) for e, s in TRACKED.items() if len(s) >= min_sources), key=lambda x: -x[1])
    return hot[:top]


def section_trends():
    """Google Trends France (RSS) filtré sur le vélo : (sujet, trafic, article associé, lien)."""
    url = "https://trends.google.com/trending/rss?geo=FR"
    r, code = get(url)
    entries = feedparser.parse(r.content).entries if r is not None and r.ok else []
    out = []
    for e in entries:
        text = " ".join(str(v) for k, v in e.items() if k in ("title", "summary") or "news_item" in k)
        if not any(k in " " + norm(text) + " " for k in TREND_KEYWORDS):
            continue
        out.append((e.get("title", "?"), e.get("ht_approx_traffic", "?"),
                    e.get("ht_news_item_title", ""), e.get("ht_news_item_url", "")))
    record("Google Trends France", url, code, len(entries), f"{len(out)} sujet(s) vélo")
    return out


DISCOVER_MEDIA = {"name": "Médias grand public « capteurs Discover » (via Google News, 12 h)",
                  "feeds": [google_news("(site:le10sport.com OR site:rmcsport.bfmtv.com OR site:dicodusport.fr "
                                        "OR site:sports.fr OR site:eurosport.fr) cyclisme", "fr", hours=12)]}


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
        {"name": "Le Télégramme (via Google News)", "feeds": [google_news("site:letelegramme.fr cyclisme")], "prefiltered": True},
        {"name": "Le Progrès", "feeds": ["https://www.leprogres.fr/rss"], "home": "https://www.leprogres.fr/sport"},  # /sport/rss renvoie un flux vide
        {"name": "Sud Ouest", "feeds": ["https://www.sudouest.fr/sport/rss.xml", "https://www.sudouest.fr/rss.xml"], "home": "https://www.sudouest.fr/sport/"},
        {"name": "La Voix du Nord (via Google News)", "feeds": [google_news("site:lavoixdunord.fr cyclisme")], "prefiltered": True},
        {"name": "PQR complémentaire : NR, La Montagne, L'Est Républicain, DNA, La Dépêche, Midi Libre, La Provence, Nice-Matin (via Google News)",
         "feeds": [google_news("(site:lanouvellerepublique.fr OR site:lamontagne.fr OR site:estrepublicain.fr OR site:dna.fr "
                               "OR site:ladepeche.fr OR site:midilibre.fr OR site:laprovence.com OR site:nicematin.com) cyclisme")],
         "prefiltered": True},
        {"name": "Le Dauphiné Libéré", "feeds": ["https://www.ledauphine.com/sport/rss", "https://www.ledauphine.com/rss"], "home": "https://www.ledauphine.com/sport"},
    ],
    "medias": [
        {"name": "Cyclism'Actu", "feeds": ["https://www.cyclismactu.net/rss_social.xml", "https://www.cyclismactu.net/rss.xml"], "home": "https://www.cyclismactu.net/"},
        {"name": "Velo101", "feeds": ["https://www.velo101.com/feed", "https://www.velo101.com/feed/"], "home": "https://www.velo101.com/"},
        {"name": "WielerFlits", "feeds": ["https://www.wielerflits.nl/feed/", "https://www.wielerflits.nl/rss"], "home": "https://www.wielerflits.nl/"},
        {"name": "In de Leiderstrui", "feeds": ["https://www.indeleiderstrui.nl/sitemap/news.xml", "https://www.indeleiderstrui.nl/feed"], "home": "https://www.indeleiderstrui.nl/"},
        {"name": "Cyclingnews", "feeds": ["https://www.cyclingnews.com/feeds.xml", "https://www.cyclingnews.com/rss/"], "home": "https://www.cyclingnews.com/"},
        {"name": "L'Équipe (via Google News)", "feeds": [google_news("site:lequipe.fr cyclisme")]},
    ],
    "international": [
        {"name": "Tuttobiciweb (Italie)", "feeds": ["https://www.tuttobiciweb.it/rss", "https://www.tuttobiciweb.it/rss.xml"],
         "home": "https://www.tuttobiciweb.it/", "gn": ("site:tuttobiciweb.it", "it", 1)},
        {"name": "La Gazzetta dello Sport, ciclismo (via Google News)", "feeds": [google_news("site:gazzetta.it ciclismo", "it")]},
        {"name": "Ciclo21 (Espagne)", "feeds": ["https://www.ciclo21.com/feed/"], "home": "https://www.ciclo21.com/",
         "gn": ("site:ciclo21.com", "es", 1)},
        {"name": "Sporza, Het Nieuwsblad, HLN (via Google News)",
         "feeds": [google_news("(site:sporza.be OR site:nieuwsblad.be OR site:hln.be) (koers OR veldrijden OR transfer)", "nl")]},
        {"name": "Escape Collective", "feeds": ["https://escapecollective.com/feed/"], "home": "https://escapecollective.com/",
         "gn": ("site:escapecollective.com", "en", 1)},
    ],
    # Sources officielles et antidopage : fenêtre de 3 jours (communiqués rares mais décisifs)
    "officiel": [
        {"name": "ITA, cyclisme (via Google News)", "gn_filter": "none", "feeds": [google_news("site:ita.sport cycling", "en", 3, exclure=False)]},
        {"name": "AFLD (via Google News)", "gn_filter": "none", "feeds": [google_news("site:afld.fr", "fr", 3, exclure=False)]},
        {"name": "UCI, communiqués (via Google News)", "gn_filter": "none", "feeds": [google_news("site:uci.org", "en", 3, exclure=False)]},
        {"name": "UEC (via Google News)", "gn_filter": "none", "feeds": [google_news("site:uec.ch", "en", 3, exclure=False)]},
        {"name": "UCI, enregistrement des équipes 2027 (via Google News)",
         "gn_filter": "none", "feeds": [google_news('UCI ("2027") (enregistrement OR licence OR "liste des équipes" OR WorldTeam OR ProTeam)', "fr", 3, exclure=False),
                   google_news('UCI ("2027") ("team registration" OR "registered teams" OR licence OR WorldTeam OR ProTeam)', "en", 3, exclure=False)],
         "prefiltered": True, "merge": True},
        {"name": "TAS / CAS, cyclisme (via Google News)",
         "gn_filter": "none", "feeds": [google_news('("Tribunal arbitral du sport" OR TAS OR "Court of Arbitration for Sport" OR CAS) (cyclisme OR cycling OR cycliste OR cyclist)', "fr", 7, exclure=False)]},
        {"name": "Discipline FFC (via Google News)",
         "gn_filter": "none", "feeds": [google_news('(FFC OR "Fédération française de cyclisme") (sanction OR suspension OR suspendu OR "commission de discipline" OR radiation)', "fr", 3, exclure=False)]},
    ],
    # Annonces des équipes françaises (Google News, requête par équipe + verbes d'annonce)
    "equipes": ["Groupama-FDJ United", "Decathlon CMA CGM", "Cofidis", "TotalEnergies", "Unibet Rose Rockets",
                "FDJ United-Suez", "St Michel-Préférence Home-Auber 93", "Nice Métropole Côte d'Azur",
                "CIC Pro Cycling Academy", "Van Rysel-Roubaix", "Mayenne Monbana My Pie", "Ma Petite Entreprise",
                "VC Rouen 76", "Burgos Burpellet BH"],
    # Résultats du week-end et de la semaine (48 h) : cyclo-cross, piste, courses amateurs
    "resultats": [
        {"name": "Cyclo-cross : résultats (via Google News, 48 h)",
         "feeds": [google_news('(cyclo-cross OR cyclocross) (classement OR résultats OR "s\'impose" OR victoire OR vainqueur)', "fr", 2)]},
        {"name": "Courses amateurs et piste : résultats (via Google News, 48 h)",
         "feeds": [google_news('(cyclisme) ("Coupe de France" OR N1 OR "élite nationale" OR piste OR vélodrome) (classement OR résultats OR "s\'impose" OR victoire)', "fr", 2)]},
    ],
    "transferts": {
        "name": "Transferts (ProCyclingStats, sinon FirstCycling)",
        "urls": ["https://www.procyclingstats.com/transfers.php", "https://firstcycling.com/transfer.php"],
        "max": 15,
    },
    "cyclocross24": {"name": "Cyclocross24", "feeds": ["https://www.cyclocross24.com/feed/"],
                     "home": "https://www.cyclocross24.com/", "gn": ("site:cyclocross24.com", "en", 1)},
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
                entries = parsed.entries
                if "news.google.com" in url:
                    mode = source.get("gn_filter", "strict")
                    kept = [e for e in entries if gn_ok(e.get("title", ""), mode)]
                    REJECTED.extend((source["name"], e.get("title", ""), e.get("link", "")) for e in entries if e not in kept)
                    record(source["name"], url, code, len(kept), f"{len(entries) - len(kept)} écarté(s) par le filtre lexical" if len(kept) < len(entries) else "")
                    return kept
                record(source["name"], url, code, len(entries))
                return entries
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
    if source.get("gn"):
        query, lang, days = source["gn"]
        url = google_news(query, lang, days)
        r, code = get(url)
        parsed = feedparser.parse(r.content) if r is not None and r.ok else None
        kept = [e for e in parsed.entries if gn_ok(e.get("title", ""), source.get("gn_filter", "strict"))] if parsed else []
        record(source["name"], url, code, len(kept), "repli Google News")
        if parsed:
            return kept
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
        # Les flux Google News sont déjà filtrés par la requête « cyclisme »
        keywords = None if src.get("prefiltered") else PQR_KEYWORDS
        blocks.append((src["name"], recent(fetch_feed(src), keywords)))
    return blocks


CYCLISMACTU_SITEMAPS = ["https://www.cyclismactu.net/sitemap-news.xml", "https://www.cyclismactu.net/sitemap_news.xml",
                        "https://www.cyclismactu.net/news-sitemap.xml", "https://www.cyclismactu.net/sitemap.xml"]
CYCLISMACTU_PAGES = ["https://www.cyclismactu.net/cyclisme/", "https://www.cyclismactu.net/"]


def fetch_cyclismactu():
    """Cyclism'Actu : sitemap news si disponible, sinon liste des articles des pages Cyclisme et Accueil
    (le flux RSS public ne contient que 5 éléments)."""
    name = "Cyclism'Actu"
    for url in CYCLISMACTU_SITEMAPS:
        r, code = get(url)
        if not r or not r.ok or "<url" not in r.text:
            record(name + " (sitemap)", url, code, 0, "absent ou vide" if r is not None else "")
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        out = []
        for u in soup.find_all("url"):
            loc = u.find("loc")
            if not loc or "/news-" not in loc.get_text():
                continue
            title = u.find(re.compile("title$"))
            date = u.find(re.compile("publication_date$")) or u.find("lastmod")
            e = {"title": title.get_text(strip=True) if title else loc.get_text(strip=True).rsplit("/", 1)[-1],
                 "link": loc.get_text(strip=True)}
            if date:
                try:
                    e["published_parsed"] = datetime.fromisoformat(date.get_text(strip=True).replace("Z", "+00:00")).utctimetuple()
                except ValueError:
                    pass
            out.append(e)
        record(name + " (sitemap)", url, code, len(out))
        if out:
            return out
    for url in CYCLISMACTU_PAGES:
        r, code = get(url)
        if not r or not r.ok:
            record(name + " (page)", url, code)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        seen, out = set(), []
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            if "/news-" not in href or href in seen or len(title) < 15:
                continue
            seen.add(href)
            e = {"title": title, "link": href}
            m = re.match(r"^(\d{1,2}):(\d{2})\s+(.*)$", title)
            if m:  # les articles du jour sont précédés de leur heure de publication (heure de Paris)
                h, mi = int(m.group(1)), int(m.group(2))
                local = NOW.astimezone(PARIS).replace(hour=h, minute=mi, second=0, microsecond=0)
                if local > NOW.astimezone(PARIS) + timedelta(minutes=5):
                    local -= timedelta(days=1)
                e["title"] = m.group(3)
                e["published_parsed"] = local.astimezone(timezone.utc).utctimetuple()
            out.append(e)
        record(name + " (page)", url, code, len(out), "lecture de la page (heure lue pour les articles du jour)")
        if out:
            return out
    return fetch_feed(next(s for s in CONFIG["medias"] if s["name"] == name))


def section_medias():
    return [(src["name"], recent(fetch_cyclismactu() if src["name"] == "Cyclism'Actu" else fetch_feed(src)))
            for src in CONFIG["medias"]]


def bluesky_token():
    """Ouvre une session Bluesky avec BSKY_HANDLE / BSKY_APP_PASSWORD. Renvoie (jeton, URL du serveur) ou (None, None).
    Le serveur (PDS) du compte est résolu à partir de l'identifiant, pour les comptes hébergés hors de bsky.social."""
    handle = (os.getenv("BSKY_HANDLE") or "").strip().lstrip("@")
    password = (os.getenv("BSKY_APP_PASSWORD") or "").strip()
    if not handle or not password:
        return None, None
    servers = ["https://bsky.social"]
    try:
        did = handle if handle.startswith("did:") else requests.get(
            "https://bsky.social/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle},
            timeout=TIMEOUT).json().get("did")
        if did and did.startswith("did:plc:"):
            doc = requests.get(f"https://plc.directory/{did}", timeout=TIMEOUT).json()
            pds = next((s["serviceEndpoint"] for s in doc.get("service", []) if s.get("id") == "#atproto_pds"), None)
            if pds and pds.rstrip("/") not in servers:
                servers.append(pds.rstrip("/"))
    except (requests.RequestException, ValueError):
        pass
    last = "aucune réponse"
    for base in servers:
        try:
            r = requests.post(f"{base}/xrpc/com.atproto.server.createSession",
                              json={"identifier": handle, "password": password}, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = f"Erreur ({type(e).__name__})"
            continue
        if r.ok and r.json().get("accessJwt"):
            record("Bluesky connexion (compte de la veille)", f"{base}/xrpc/com.atproto.server.createSession",
                   f"{r.status_code} OK", 1, "session ouverte : recherche par mots-clés active")
            return r.json()["accessJwt"], base
        try:
            msg = r.json().get("message") or r.json().get("error") or ""
        except ValueError:
            msg = ""
        last = f"{r.status_code} {r.reason} {msg}".strip()
    record("Bluesky connexion (compte de la veille)", "createSession", last, 0,
           "échec de connexion : vérifier les secrets BSKY_HANDLE (identifiant sans @) et BSKY_APP_PASSWORD (mot de passe d'application)")
    return None, None


def bluesky_post(p, label):
    """Convertit un post Bluesky (format de l'API) en ligne (date, texte, lien, étiquette)."""
    try:
        dt = datetime.fromisoformat(p["record"]["createdAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        dt = None
    handle = p.get("author", {}).get("handle", "?")
    rkey = p.get("uri", "").rsplit("/", 1)[-1]
    text = re.sub(r"\s+", " ", p.get("record", {}).get("text", ""))[:200]
    likes, reposts = p.get("likeCount", 0), p.get("repostCount", 0)
    return (dt, f"@{handle} : {text} ({likes} j'aime, {reposts} reposts)",
            f"https://bsky.app/profile/{handle}/post/{rkey}", label)


def load_list(path, default):
    """Lit une liste éditable de etat/ (une valeur par ligne, # = commentaire)."""
    try:
        with open(path, encoding="utf-8") as f:
            vals = [l.split("#", 1)[0].strip() for l in f]
        vals = [v for v in vals if v]
        return vals or default
    except OSError:
        return default


def section_bluesky():
    out = []

    # 1. Comptes suivis, sans authentification (endpoint public getAuthorFeed) : posts propres, hors réponses et reposts.
    #    Ligne de etat/bluesky.txt : « identifiant » ou « identifiant | velo » (compte généraliste : on ne garde que les
    #    posts qui parlent de cyclisme). Au plus BLUESKY_PER_ACCOUNT posts par compte, pour qu'aucun compte ne noie les autres.
    def one(line):
        parts = [p.strip() for p in line.split("|")]
        actor, filtre = parts[0], len(parts) > 1 and parts[1] == "velo"
        url = (f"https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={quote_plus(actor)}"
               f"&limit=30&filter=posts_no_replies")
        r, code = get(url)
        try:
            feed = r.json().get("feed", []) if r is not None and r.ok else []
        except ValueError:
            feed = []
        posts = []
        for item in feed:
            if item.get("reason"):  # repost : on ne garde que ce que le compte publie lui-même
                continue
            post = bluesky_post(item.get("post", {}), f"@{actor}")
            if post[0] is not None and post[0] < SINCE:
                continue
            if filtre and not gn_ok(post[1], "strict"):
                continue
            posts.append(post)
        record(f"Bluesky @{actor}", url.split("&")[0], code, min(len(posts), BLUESKY_PER_ACCOUNT),
               "" if feed else "aucun post renvoyé")
        return posts[:BLUESKY_PER_ACCOUNT]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        for posts in ex.map(one, load_list(BLUESKY_FILE, BLUESKY_ACTORS)):
            out += posts

    # 2. Recherche par mots-clés : seulement si les secrets BSKY_HANDLE / BSKY_APP_PASSWORD sont définis
    #    (la recherche publique sans compte renvoie 403 depuis 2025 : ce n'est pas une panne).
    token, base = bluesky_token()
    if token:
        for q in BLUESKY_QUERIES:
            params = f"q={quote_plus(q)}&limit=25&sort=latest&since={SINCE.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            url = f"{base}/xrpc/app.bsky.feed.searchPosts?{params}"
            try:
                r = requests.get(url, headers={**HEADERS, "Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
                code = f"{r.status_code} {'OK' if r.ok else r.reason} (authentifié)"
            except requests.RequestException as e:
                r, code = None, f"Erreur ({type(e).__name__})"
            posts = r.json().get("posts", []) if r is not None and r.ok else []
            record(f"Bluesky recherche « {q} »", url.split("?")[0], code, len(posts))
            for p in posts:
                post = bluesky_post(p, q)
                if post[0] is None or post[0] >= SINCE:
                    out.append(post)

    seen, res = set(), []
    for p in sorted(out, key=lambda x: x[0] or NOW, reverse=True):
        if p[2] not in seen:
            seen.add(p[2])
            res.append(p)
    return res[:BLUESKY_MAX]


YT_HEADERS = {**HEADERS, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
YT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}  # évite la page de consentement
YT_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def load_youtube():
    chans = []
    for line in load_list(YOUTUBE_FILE, []):
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0].startswith("UC"):
            chans.append((parts[0], parts[1] if len(parts) > 1 else parts[0], len(parts) > 2 and parts[2] == "velo"))
    return chans


def youtube_videos(cid, name):
    """Vidéos récentes d'une chaîne : flux RSS officiel, sinon page /videos (date approximative « il y a N h/j »)."""
    rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    r, code = get(rss)
    if r is not None and r.ok:
        parsed = feedparser.parse(r.content)
        if parsed.entries:
            record(f"YouTube {name}", rss, code, len(parsed.entries))
            return [(entry_datetime(e), e.get("title", "(sans titre)").strip(), e.get("link", "")) for e in parsed.entries]
    page = f"https://www.youtube.com/channel/{cid}/videos?hl=en&gl=FR"
    try:
        r = requests.get(page, headers=YT_HEADERS, cookies=YT_COOKIES, timeout=TIMEOUT)
        pcode = f"{r.status_code} {'OK' if r.ok else r.reason}"
    except requests.RequestException as e:
        record(f"YouTube {name}", page, f"Erreur ({type(e).__name__})", 0, f"RSS : {code}")
        return []
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S) if r.ok else None
    if not m:
        record(f"YouTube {name}", page, pcode, 0, f"RSS : {code} ; page sans données vidéo")
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        record(f"YouTube {name}", page, pcode, 0, f"RSS : {code} ; données illisibles")
        return []
    lockups = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                lockups.append(o["lockupViewModel"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    vids = []
    for x in lockups[:15]:
        vid = x.get("contentId")
        md = x.get("metadata", {}).get("lockupMetadataViewModel", {})
        title = (md.get("title") or {}).get("content", "(sans titre)")
        dt = None
        mm = re.search(r'"accessibilityLabel": "(\d+) (second|minute|hour|day|week|month|year)s? ago"', json.dumps(md))
        if mm:
            dt = NOW - timedelta(seconds=int(mm.group(1)) * YT_UNITS[mm.group(2)])
        if vid:
            vids.append((dt, title, f"https://www.youtube.com/watch?v={vid}"))
    record(f"YouTube {name}", page, pcode, len(vids), f"repli page /videos (RSS : {code}), heure approximative")
    return vids


def section_youtube():
    since = NOW - timedelta(hours=YOUTUBE_HOURS)
    out = []
    for cid, name, filtre in load_youtube():
        for dt, title, link in youtube_videos(cid, name):
            if dt is None or dt < since:
                continue
            if filtre and not gn_ok(title, "strict"):
                continue
            out.append((dt, f"[{name}] {title}", link))
    out.sort(key=lambda x: x[0] or NOW, reverse=True)
    return out[:MAX_ITEMS]


def section_relais_sociaux():
    """Articles de presse qui reprennent une annonce faite sur un réseau non lisible automatiquement."""
    out = []
    for q, lang in SOCIAL_RELAY_QUERIES:
        out += recent(fetch_feed({"name": f"Relais réseaux sociaux ({lang}, via Google News)",
                                  "feeds": [google_news(q, lang, 1)], "gn_filter": "strict"}))
    seen, res = set(), []
    for it in sorted(out, key=lambda x: x[0] or NOW, reverse=True):
        if url_key(it[2]) not in seen:
            seen.add(url_key(it[2]))
            res.append(it)
    return res[:MAX_ITEMS]


def section_list(key):
    return [(src["name"], recent(fetch_feed(src))) for src in CONFIG[key]]


def section_transferts():
    """Lit le tableau des derniers transferts (PCS, sinon FirstCycling) : une ligne par mouvement."""
    src = CONFIG["transferts"]
    for url in src["urls"]:
        r, code = get(url)
        if not r or not r.ok:
            record(src["name"], url, code, 0, "bloqué ou indisponible" if r is not None else "")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
                cells = [c for c in cells if c]
                if len(cells) < 2:
                    continue
                a = tr.find("a", href=True)
                link = urljoin(url, a["href"]) if a else url
                out.append((" | ".join(cells)[:200], link))
                if len(out) >= src["max"]:
                    break
            if out:
                break
        detail = "" if out else "page reçue mais aucun tableau lisible (protection anti-robot ?)"
        record(src["name"], url, code, len(out), detail)
        if out:
            return out
    return []


def section_reddit():
    return recent(fetch_feed(CONFIG["reddit"]))


def section_ffc_api(n=18):
    """Documents FFC via l'API WordPress publique du site (type « ressources » et ses fichiers attachés).
    Renvoie [(titre, lien du document, date)] : un élément par fichier, lien au format ffc.fr/ressources/<rubrique>/<fichier>/."""
    base = "https://ffc.fr/wp-json/wp/v2"
    url = f"{base}/ressources?per_page={n}&orderby=date&order=desc&_fields=id,date,modified,link,title"
    r, code = get(url)
    if not r or not r.ok:
        record("FFC Ressources (API WordPress)", url, code)
        return []
    try:
        items = r.json()
    except ValueError:
        record("FFC Ressources (API WordPress)", url, code, 0, "réponse non JSON")
        return []
    out = []
    for it in items:
        title = BeautifulSoup(it.get("title", {}).get("rendered", ""), "html.parser").get_text(" ", strip=True)
        mr, _ = get(f"{base}/media?parent={it['id']}&per_page=10&_fields=id,date,link")
        medias = mr.json() if mr is not None and mr.ok else []
        if not medias:
            out.append((title, it.get("link", ""), it.get("date", "")))
        for m in medias:
            out.append((title, m.get("link", ""), m.get("date", "") or it.get("date", "")))
    record("FFC Ressources (API WordPress)", url, code, len(out), f"{len(items)} ressources")
    return out


def section_ffc():
    api = section_ffc_api()
    if api:
        return [(f"{t} ({d[:10]})" if d else t, u) for t, u, d in api]
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


# ---------------------------------------------------------------- état persistant

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(ETAT, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0, sort_keys=True)


def url_key(u):
    """Clé de comparaison d'une URL : sans protocole, sans paramètres de suivi, sans / final."""
    u = re.sub(r"^https?://(www\.)?", "", (u or "").strip())
    u = re.sub(r"[?&](utm_[^&]+|oc=\d+|hl=[^&]+|gl=[^&]+|ceid=[^&]+)", "", u)
    return u.rstrip("/?&").lower()


def load_traites():
    """URL déjà traitées par l'agent (fichier JSON Lines : une ligne {"url": ..., ...} par sujet)."""
    keys = set()
    try:
        with open(TRAITES, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for u in [rec.get("url")] + list(rec.get("urls", []) or []):
                    if u:
                        keys.add(url_key(u))
    except OSError:
        pass
    return keys


def load_surveillance():
    try:
        with open(SURVEILLANCE, encoding="utf-8") as f:
            return [l.strip().strip('"').strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []


NEW_ITEMS = []  # (date, source, titre, lien) : éléments vus pour la première fois à cette génération


def mark_new(source, items):
    """Repère les éléments jamais vus aux générations précédentes et met à jour etat/vus.json en mémoire."""
    for it in items:
        dt, title, link = it[0], it[1], it[2]
        k = url_key(link)
        if not k:
            continue
        if k not in VUS_DB:
            VUS_DB[k] = {"vu": NOW.isoformat(timespec="minutes"), "source": source, "titre": title[:160],
                         "pub": dt.isoformat(timespec="minutes") if dt else None}
            if not FIRST_RUN:
                NEW_ITEMS.append((dt, source, title, link))
    return items


def section_surveillance():
    """Recherche Google News par paquets de 6 noms surveillés (coureurs, clubs, encadrants)."""
    names = load_surveillance()
    out = []
    for i in range(0, len(names), 6):
        chunk = names[i:i + 6]
        q = "(" + " OR ".join(f'"{n}"' for n in chunk) + ") (cyclisme OR vélo OR cycliste OR coureur OR équipe)"
        src = {"name": f"Surveillance nominative ({chunk[0]} …)", "feeds": [google_news(q, "fr", 1)], "gn_filter": "exclude"}
        out += recent(fetch_feed(src))
    seen, res = set(), []
    for it in sorted(out, key=lambda x: x[0] or NOW, reverse=True):
        if url_key(it[2]) not in seen:
            seen.add(url_key(it[2]))
            res.append(it)
    return res[:MAX_ITEMS], len(names)


def section_equipes():
    out = []
    for team in CONFIG["equipes"]:
        q = f'"{team}" (annonce OR officialise OR signe OR recrue OR prolonge OR "a signé" OR effectif OR arrivée OR départ)'
        out += [(dt, f"[{team}] {t}", l) for dt, t, l in recent(fetch_feed({"name": f"Équipe : {team} (via Google News)",
                                                                              "feeds": [google_news(q, "fr", 1)], "gn_filter": "exclude"}))]
    seen, res = set(), []
    for it in sorted(out, key=lambda x: x[0] or NOW, reverse=True):
        if url_key(it[2]) not in seen:
            seen.add(url_key(it[2]))
            res.append(it)
    return res[:MAX_ITEMS]


def recent_hours(entries, hours):
    since = NOW - timedelta(hours=hours)
    out = [(entry_datetime(e), e.get("title", "(sans titre)").strip(), e.get("link", "")) for e in entries]
    out = [x for x in out if not x[0] or x[0] >= since]
    out.sort(key=lambda x: x[0] or NOW, reverse=True)
    return out[:MAX_ITEMS]


# ---------------------------------------------------------------- écriture

def block(md, title, items, source=None):
    """Ajoute une sous-section au Markdown et enregistre ses titres pour le calcul de vélocité."""
    md += [f"### {title}", ""] + lines(track(source or title, items))


VUS_DB = {}
FIRST_RUN = False


def main():
    global VUS_DB, FIRST_RUN
    VUS_DB = load_json(VUS, None)
    FIRST_RUN = VUS_DB is None
    VUS_DB = VUS_DB or {}
    traites = load_traites()
    SURV_TERMS.extend(norm(n) for n in load_surveillance())
    body = []

    body += ["## 1. Nos publications internes (DirectVelo)", ""] + lines(mark_new("DirectVelo", section_directvelo()))
    body += ["## 2. Forum Le Gruppetto (route pros : mercato, équipes, amateurs ; cyclo-cross)", ""]
    for name, items in section_gruppetto():
        block(body, name, items, f"Le Gruppetto – {name}")

    body += ["## 3. Presse quotidienne régionale (filtrée sur le cyclisme)", ""]
    for name, items in section_pqr():
        block(body, name, items)

    body += ["## 4. Médias spécialisés", ""]
    for name, items in section_medias():
        block(body, name, items)

    body += ["## 5. Réseaux sociaux (Bluesky, YouTube, Reddit, relais des réseaux non lisibles)", "", "### Bluesky", ""]
    bsky = section_bluesky()
    track("Bluesky", bsky)
    if bsky:
        body += [f"- {fmt_time(dt)} — [{q}] [{text}]({link})" for dt, text, link, q in bsky] + [""]
    else:
        body += lines([])
    block(body, "YouTube : vidéos des 48 dernières heures (etat/youtube.txt)", section_youtube(), "YouTube")
    block(body, "Reddit r/peloton", section_reddit())
    block(body, "Relais des réseaux sociaux : annonces faites sur X, Instagram, TikTok, Facebook, LinkedIn (via Google News)",
          section_relais_sociaux(), "Relais réseaux sociaux")

    body += ["## 6. Sources primaires : FFC Ressources (page 1)", ""]
    ffc = section_ffc()
    connus = load_json(FFC_CONNUS, None)
    ffc_first = connus is None
    connus = connus or {}
    ffc_new = []
    for t, u in ffc:
        k = url_key(u)
        if k not in connus:
            # À la première exécution, les documents existants sont enregistrés comme anciens
            connus[k] = "2000-01-01T00:00+00:00" if ffc_first else NOW.isoformat(timespec="minutes")
            if not ffc_first:
                ffc_new.append((t, u))
        elif datetime.fromisoformat(connus[k]) >= NOW - timedelta(hours=FFC_NEW_HOURS):
            ffc_new.append((t, u))
    if ffc:
        save_json(FFC_CONNUS, connus)
        body += [f"- {'🆕 **NOUVEAU** — ' if (t, u) in ffc_new else ''}[{t}]({u})" for t, u in ffc] + [""]
    else:
        body += lines([])

    body += ["## 7. Presse internationale (transferts & cyclo-cross)", ""]
    for name, items in section_list("international"):
        block(body, name, items)

    body += ["## 8. Sources officielles & antidopage (ITA, AFLD, UCI, UEC — 3 derniers jours)", ""]
    for name, items in section_list("officiel"):
        block(body, name, items)

    body += ["## 9. Bases de données : transferts & cyclo-cross", "", "### Derniers transferts", ""]
    tr = section_transferts()
    body += [f"- [{t}]({u})" for t, u in tr] + [""] if tr else lines([])
    block(body, "Cyclocross24", recent(fetch_feed(CONFIG["cyclocross24"])))

    body += ["## 11. Surveillance nominative (coureurs, clubs, encadrants : etat/surveillance.txt)", ""]
    surv, n_names = section_surveillance()
    body += [f"_{n_names} noms surveillés._", ""]
    block(body, "Articles citant un nom surveillé", surv, "Surveillance nominative")

    body += ["## 12. Annonces des équipes françaises (via Google News)", ""]
    block(body, "Annonces repérées", section_equipes(), "Annonces des équipes")

    body += ["## 13. Résultats (48 h) : cyclo-cross, piste, courses amateurs", ""]
    for src in CONFIG["resultats"]:
        block(body, src["name"], recent_hours(fetch_feed(src), 48))

    # Radar tendances (placé en tête du fichier, calculé après la collecte)
    radar = ["## TENDANCES & SIGNAUX GOOGLE DISCOVER", ""]
    media = recent(fetch_feed(DISCOVER_MEDIA))
    track("Médias grand public", media)
    hot = velocity()
    if hot:
        radar += ["🔥 Sujets à forte vélocité (potentiel Discover) : "
                  + ", ".join(f"{e} ({n} sources)" for e, n in hot), ""]
    else:
        radar += ["🔥 Sujets à forte vélocité (potentiel Discover) : aucun nom cité par 3 sources ou plus sur la fenêtre.", ""]
    radar += ["### Google Trends France (sujets liés au vélo)", ""]
    trends = section_trends()
    if trends:
        for topic, traffic, art, link in trends:
            line = f"- **{topic}** (trafic ≈ {traffic})"
            if art:
                line += f" — article associé : [{art}]({link})" if link else f" — {art}"
            radar.append(line)
        radar.append("")
    else:
        radar += ["_Aucun sujet vélo dans les tendances Google France en ce moment._", ""]
    radar += ["### Baromètre des médias grand public (Le10Sport, RMC Sport, Dicodusport, Sports.fr, Eurosport — 12 h)", ""]
    radar += lines(media)

    # Nouveautés depuis la génération précédente (hors URL déjà traitées par l'agent)
    nouv = ["## 0. NOUVEAUTÉS DEPUIS LA GÉNÉRATION PRÉCÉDENTE", ""]
    if FIRST_RUN:
        nouv += ["_Première génération avec mémoire : la liste des nouveautés sera disponible à la prochaine génération._", ""]
    else:
        fresh = [x for x in NEW_ITEMS if url_key(x[3]) not in traites]
        fresh.sort(key=lambda x: x[0] or NOW, reverse=True)
        if ffc_new:
            nouv += [f"- 🆕 **ALERTE FFC — nouveau document** : [{t}]({u})" for t, u in ffc_new]
        nouv += [f"- {fmt_time(dt)} — {src} — [{t}]({l})" for dt, src, t, l in fresh[:120]] or \
                ["_Aucune publication nouvelle depuis la génération précédente._"]
        if len(fresh) > 120:
            nouv.append(f"- … et {len(fresh) - 120} autres (voir les sections ci-dessous).")
        nouv.append("")
    # Purge des URL vues il y a plus de 7 jours, puis sauvegarde
    limit = (NOW - timedelta(days=7)).isoformat(timespec="minutes")
    VUS_DB = {k: v for k, v in VUS_DB.items() if v.get("vu", "") >= limit}
    save_json(VUS, VUS_DB)

    md = [
        "# Flux de veille DirectVelo (tampon 2 h)",
        "",
        f"Généré le {NOW.astimezone(PARIS).strftime('%d/%m/%Y à %Hh%M')} (heure de Paris). "
        f"Fenêtre : publications depuis le {SINCE.astimezone(PARIS).strftime('%d/%m à %Hh%M')}.",
        "",
    ] + radar + nouv + body

    md += ["## 14. Contrôle du filtre lexical (titres Google News écartés, 80 au plus)", "",
           "_À relire de temps en temps : un vrai article cycliste ici signale un terme à ajouter au filtre (CYCLING_TERMS)._", ""]
    md += [f"- {src} — [{t}]({l})" for src, t, l in REJECTED[:80]] or ["_Aucun._"]
    md.append("")
    md += ["## 10. Statut technique des sources", "",
           "| Source | URL interrogée | Code HTTP | Éléments | Remarque |",
           "|---|---|---|---|---|"]
    for source, url, code, count, detail in STATUS:
        md.append(f"| {source} | {url} | {code} | {count} | {detail} |")
    md.append("")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    # Version légère pour les éditions horaires : en-tête, Tendances, Nouveautés, Statut technique
    statut = md[md.index("## 10. Statut technique des sources"):]
    delta = md[:4] + radar + nouv + statut
    delta[0] = "# Flux de veille DirectVelo (delta horaire)"
    with open(OUTPUT_DELTA, "w", encoding="utf-8") as f:
        f.write("\n".join(delta))
    print(f"{OUTPUT} écrit ({len(STATUS)} requêtes).")


if __name__ == "__main__":
    main()

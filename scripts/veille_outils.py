# -*- coding: utf-8 -*-
"""Boîte à outils de la veille DirectVelo (workbench Composio).

Chargement : exec(open("/mnt/files/directvelo/veille_outils.py").read())
Fonctions : lire_flux(), section(), lire_traites(), ajouter_traites(), lire_vus(), audience().
Utilise proxy_execute (helper du workbench Composio) sur le toolkit github.
Copie de sauvegarde : dépôt michael-dv/veille-directvelo, scripts/veille_outils.py.
Réécrit le 04/11/2026.
"""
import base64, json, os, re, time, datetime
from zoneinfo import ZoneInfo

VO_OWNER = "michael-dv"
VO_REPO = "veille-directvelo"
VO_REF = "main"
VO_TZ = ZoneInfo("Europe/Paris")
VO_OUT = "/mnt/files/directvelo/out"
VO_WORKFLOW = "update_feed.yml"


def _vo_api(method, path, **kw):
    ep = path if path.startswith("http") else f"/repos/{VO_OWNER}/{VO_REPO}/{path.lstrip('/')}"
    r, e = proxy_execute(method, ep, "github", **kw)
    if e:
        raise RuntimeError(f"GitHub {method} {path} : {e}")
    return r


def _vo_raw(chemin):
    """Contenu brut d'un fichier du dépôt (str)."""
    r = _vo_api("GET", f"contents/{chemin}", query_params={"ref": VO_REF},
                headers={"Accept": "application/vnd.github.raw+json"})
    if isinstance(r, (bytes, bytearray)):
        r = r.decode("utf-8", "replace")
    if isinstance(r, dict) and "content" in r:  # repli si l'en-tête raw est ignoré
        r = base64.b64decode(re.sub(r"\s", "", r["content"])).decode("utf-8", "replace")
    return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def _vo_put(chemin, texte, message, sha=None):
    if sha is None:
        try:
            sha = _vo_api("GET", f"contents/{chemin}", query_params={"ref": VO_REF}).get("sha")
        except Exception:
            sha = None
    body = {"message": message, "branch": VO_REF,
            "content": base64.b64encode(texte.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    return _vo_api("PUT", f"contents/{chemin}", body=body)


def _vo_age_flux():
    """(âge en minutes, date du dernier commit de flux_2h.md)."""
    c = _vo_api("GET", "commits", query_params={"path": "flux_2h.md", "per_page": "1", "sha": VO_REF})
    d = c[0]["commit"]["committer"]["date"]
    t = datetime.datetime.fromisoformat(d.replace("Z", "+00:00"))
    age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 60
    return round(age), t


def lire_flux(seuil=90, attente_max=150):
    """Lit flux_2h.md. S'il a plus de `seuil` minutes, relance le workflow GitHub,
    attend (au plus `attente_max` s) un nouveau commit et relit.
    Renvoie (texte, âge en minutes, relance)."""
    age, t0 = _vo_age_flux()
    relance = False
    if age > seuil:
        relance = True
        try:
            _vo_api("POST", f"actions/workflows/{VO_WORKFLOW}/dispatches", body={"ref": VO_REF})
        except Exception as e:
            print("Relance du workflow impossible :", e)
        debut = time.time()
        while time.time() - debut < attente_max:
            time.sleep(15)
            try:
                age2, t1 = _vo_age_flux()
            except Exception:
                continue
            if t1 > t0:
                age = age2
                break
    texte = _vo_raw("flux_2h.md")
    return texte, age, relance


def section(texte, cle):
    """Extrait une section du flux : "0" à "14" ou "TENDANCES"."""
    hs = [(m.start(), m.group(0)) for m in re.finditer(r"^## .*$", texte, re.M)]
    cle = str(cle)
    for i, (p, h) in enumerate(hs):
        ok = ("TENDANCES" in h.upper()) if cle.upper() == "TENDANCES" else \
            bool(re.match(rf"^## {re.escape(cle)}[\s\.\-–—:]", h))
        if ok:
            fin = hs[i + 1][0] if i + 1 < len(hs) else len(texte)
            return texte[p:fin]
    return ""


def _vo_dt_entree(x):
    ed = x.get("edition") or ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})h", ed)
    if m:
        return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H").replace(tzinfo=VO_TZ)
    d = x.get("date")
    if d:
        try:
            return datetime.datetime.strptime(d[:10], "%Y-%m-%d").replace(tzinfo=VO_TZ)
        except ValueError:
            pass
    return None


def _vo_traites_bruts():
    meta = _vo_api("GET", "contents/etat/traites.jsonl", query_params={"ref": VO_REF})
    txt = base64.b64decode(re.sub(r"\s", "", meta.get("content", ""))).decode("utf-8", "replace") \
        if meta.get("content") else _vo_raw("etat/traites.jsonl")
    return txt, meta.get("sha")


def lire_traites(heures=48):
    """Entrées de etat/traites.jsonl des `heures` dernières heures (liste de dict)."""
    txt, _ = _vo_traites_bruts()
    limite = datetime.datetime.now(VO_TZ) - datetime.timedelta(hours=heures)
    res = []
    for l in txt.splitlines():
        l = l.strip()
        if not l:
            continue
        try:
            x = json.loads(l)
        except ValueError:
            continue
        dt = _vo_dt_entree(x)
        if dt is None or dt >= limite:
            res.append(x)
    return res


def ajouter_traites(entrees, edition, essais=3):
    """Ajoute des entrées (dict : type, sujet, url, source, pub, detail) à etat/traites.jsonl.
    Purge les entrées de plus de 10 jours. Renvoie le nombre d'entrées ajoutées."""
    jour = edition[:10]
    lignes = []
    for x in entrees:
        y = dict(x)
        y.setdefault("url", "")
        y.setdefault("source", "")
        y.setdefault("date", jour)
        y["edition"] = edition
        lignes.append(json.dumps(y, ensure_ascii=False))
    for i in range(essais):
        txt, sha = _vo_traites_bruts()
        limite = datetime.datetime.now(VO_TZ) - datetime.timedelta(days=10)
        garde = []
        for l in txt.splitlines():
            if not l.strip():
                continue
            try:
                dt = _vo_dt_entree(json.loads(l))
            except ValueError:
                dt = None
            if dt is None or dt >= limite:
                garde.append(l)
        corps = "\n".join(garde + lignes) + "\n"
        try:
            _vo_put("etat/traites.jsonl", corps, f"Veille {edition} : {len(lignes)} sujets traités", sha=sha)
            return len(lignes)
        except Exception as e:
            if i == essais - 1:
                raise
            time.sleep(3)
    return 0


def lire_vus():
    """etat/vus.json : {url: {vu, source, titre, pub}} (7 jours)."""
    return json.loads(_vo_raw("etat/vus.json"))


def audience(jour):
    """Données GA4 du rapport d'audience pour `jour` ("AAAA-MM-JJ"), ou None.
    Cherche d'abord /mnt/files/directvelo/out/audience_<jour>.json, puis etat/audience/<jour>.json dans le dépôt."""
    p = os.path.join(VO_OUT, f"audience_{jour}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    try:
        return json.loads(_vo_raw(f"etat/audience/{jour}.json"))
    except Exception:
        return None


print("veille_outils chargé : lire_flux, section, lire_traites, ajouter_traites, lire_vus, audience")

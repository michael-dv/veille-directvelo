#!/usr/bin/env python3
"""Vérifie une liste d'identifiants Instagram via l'API Graph (business_discovery) : etat/instagram_candidats.json
-> etat/instagram_check.json. Lancé à la main (workflow « Vérification des comptes Instagram »)."""
import json, os, time
import requests

GRAPH = "https://graph.facebook.com/v27.0"
uid, tok = os.environ["IG_USER_ID"].strip(), os.environ["IG_ACCESS_TOKEN"].strip()
cands = json.load(open("etat/instagram_candidats.json", encoding="utf-8"))
out = {"verifie_le": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "resultats": {}}
for entite, handles in cands.items():
    for h in handles:
        if h in out["resultats"]:
            continue
        fields = f"business_discovery.username({h}){{username,name,biography,followers_count,media_count,media.limit(1){{timestamp}}}}"
        try:
            r = requests.get(f"{GRAPH}/{uid}", params={"fields": fields, "access_token": tok}, timeout=15)
            d = r.json()
        except Exception as e:
            out["resultats"][h] = {"entite": entite, "ok": False, "erreur": type(e).__name__}
            continue
        if "business_discovery" in d:
            b = d["business_discovery"]
            m = (b.get("media") or {}).get("data") or [{}]
            out["resultats"][h] = {"entite": entite, "ok": True, "username": b.get("username"), "nom": b.get("name"),
                                   "abonnes": b.get("followers_count"), "bio": (b.get("biography") or "")[:200], "publications": b.get("media_count"),
                                   "dernier": m[0].get("timestamp")}
        else:
            err = d.get("error", {})
            out["resultats"][h] = {"entite": entite, "ok": False, "code": err.get("code"), "sous_code": err.get("error_subcode"),
                                   "erreur": (err.get("message") or "")[:160]}
            if err.get("code") in (4, 17, 32, 613, 190):  # limite de débit ou jeton invalide : on s'arrête
                out["arret"] = f"code {err.get('code')} : {err.get('message', '')[:160]}"
                break
        time.sleep(0.4)
    if "arret" in out:
        break
json.dump(out, open("etat/instagram_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
ok = sum(1 for v in out["resultats"].values() if v["ok"])
print(f"{ok} comptes valides sur {len(out['resultats'])} testés", out.get("arret", ""))

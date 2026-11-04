# -*- coding: utf-8 -*-
"""Rapport d'audience quotidien DirectVelo (PDF), workbench Composio.

Usage : exec(open("/mnt/files/directvelo/rapport_audience.py").read()); res = run()
Options : run(report_date=datetime.date(2026,11,2)), run(weekly=True) (force le bilan hebdo),
          run(test=True) (objet préfixé [TEST]), run(destinataires=[...], bcc=[...]) (autres destinataires),
          run(envoyer=False) (PDF sans envoi).
Source : GA4 properties/250526389, filtre hostName = www.directvelo.com, heure de Paris.
Sauvegarde les données dans /mnt/files/directvelo/out/audience_<date>.json et dans le dépôt
michael-dv/veille-directvelo (etat/audience/<date>.json) pour la veille.
Copie de sauvegarde du script : scripts/rapport_audience.py dans ce dépôt.
Réécrit le 04/11/2026 d'après RAPPORT_AUDIENCE_README.md.
"""
import os, json, base64, datetime, traceback
from zoneinfo import ZoneInfo

RA_PROP = "properties/250526389"
RA_HOST = "www.directvelo.com"
RA_TZ = ZoneInfo("Europe/Paris")
RA_OUT = "/mnt/files/directvelo/out"
RA_FONTS = "/mnt/files/directvelo/fonts"
RA_TO = ["redaction@directvelo.com"]
RA_BCC = ["michael.gilson@directvelo.com"]
RA_ROUGE, RA_JAUNE, RA_NOIR, RA_GRIS = "#e4002b", "#ffcc00", "#141414", "#8a8a8a"
RA_FRANCO = {"France", "Belgium", "Switzerland", "Luxembourg", "Canada", "Monaco", "Morocco", "Algeria",
             "Tunisia", "Senegal", "Côte d’Ivoire", "Côte d'Ivoire", "Réunion", "Guadeloupe", "Martinique",
             "French Guiana", "New Caledonia", "French Polynesia", "Mayotte", "Andorra", "(not set)"}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]


# ---------------------------------------------------------------- GA4
def _ra_host_filter(extra=None):
    f = {"filter": {"fieldName": "hostName", "stringFilter": {"value": RA_HOST}}}
    if extra:
        return {"andGroup": {"expressions": [f] + extra}}
    return f


def ga(dims, mets, ranges, extra_filter=None, order=None, limit=None):
    """runReport GA4. ranges : liste de (debut, fin, nom). Renvoie liste de dict (dims + mets + 'dateRange')."""
    body = {"dateRanges": [{"startDate": str(a), "endDate": str(b), "name": n} for a, b, n in ranges],
            "dimensions": [{"name": d} for d in dims],
            "metrics": [{"name": m} for m in mets],
            "dimensionFilter": _ra_host_filter(extra_filter)}
    if order:
        body["orderBys"] = order
    if limit:
        body["limit"] = limit
    r, e = proxy_execute("POST", f"https://analyticsdata.googleapis.com/v1beta/{RA_PROP}:runReport",
                         "google_analytics", body=body)
    if e:
        raise RuntimeError(f"GA4 : {e}")
    if isinstance(r, dict) and r.get("error"):
        raise RuntimeError(f"GA4 : {r['error']}")
    dh = [h["name"] for h in r.get("dimensionHeaders", [])]
    mh = [h["name"] for h in r.get("metricHeaders", [])]
    rows = []
    for row in r.get("rows", []) or []:
        x = {}
        for n, v in zip(dh, row.get("dimensionValues", [])):
            x[n] = v.get("value")
        for n, v in zip(mh, row.get("metricValues", [])):
            try:
                x[n] = float(v.get("value"))
            except (TypeError, ValueError):
                x[n] = 0.0
        if len(ranges) == 1:
            x.setdefault("dateRange", ranges[0][2])
        rows.append(x)
    return rows


def _par_range(rows, nom):
    return [x for x in rows if x.get("dateRange") == nom]


def _var(a, b):
    return None if not b else (a - b) / b * 100


def _fmt_var(v):
    return "–" if v is None else f"{v:+.1f} %".replace(".", ",")


def _fmt_int(v):
    return f"{int(round(v)):,}".replace(",", " ")


def _titre(t):
    t = t or ""
    for suf in (" - Actualité - DirectVelo", " - DirectVelo"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t


def _fmt_date(d):
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


ART_FILTER = [{"filter": {"fieldName": "pagePath", "stringFilter": {"matchType": "BEGINS_WITH", "value": "/actualite/"}}}]
KPI = [("activeUsers", "Utilisateurs"), ("newUsers", "Nouveaux utilisateurs"), ("sessions", "Sessions"),
       ("screenPageViews", "Pages vues"), ("screenPageViewsPerSession", "Pages / session"),
       ("averageSessionDuration", "Durée moy. session"), ("engagementRate", "Taux d'engagement"),
       ("bounceRate", "Taux de rebond")]


def _fmt_kpi(k, v):
    if k in ("engagementRate", "bounceRate"):
        return f"{v * 100:.1f} %".replace(".", ",")
    if k == "averageSessionDuration":
        return f"{int(v // 60)} min {int(v % 60):02d} s"
    if k == "screenPageViewsPerSession":
        return f"{v:.2f}".replace(".", ",")
    return _fmt_int(v)


# ---------------------------------------------------------------- collecte
def collecter_jour(d):
    D = {"date": str(d)}
    j2, j8, n1 = d - datetime.timedelta(1), d - datetime.timedelta(7), d - datetime.timedelta(364)
    D["dates"] = {"j": str(d), "j2": str(j2), "j8": str(j8), "n1": str(n1)}
    rg = [(d, d, "j"), (j2, j2, "j2"), (j8, j8, "j8"), (n1, n1, "n1")]
    rows = ga([], [k for k, _ in KPI], rg)
    D["kpi"] = {nm: {k: (_par_range(rows, nm)[0][k] if _par_range(rows, nm) else 0.0) for k, _ in KPI} for _, _, nm in rg}
    rows = ga(["hour"], ["activeUsers", "screenPageViews"], [(d, d, "j"), (j8, j8, "j8"), (n1, n1, "n1")])
    D["heures"] = {nm: [0.0] * 24 for nm in ("j", "j8", "n1")}
    for x in rows:
        D["heures"][x["dateRange"]][int(x["hour"])] = x["activeUsers"]
    deb30 = d - datetime.timedelta(29)
    rows = ga(["date"], ["activeUsers"], [(deb30, d, "j"), (deb30 - datetime.timedelta(364), n1, "n1")])
    D["tendance"] = {"j": sorted([(x["date"], x["activeUsers"]) for x in _par_range(rows, "j")]),
                     "n1": sorted([(x["date"], x["activeUsers"]) for x in _par_range(rows, "n1")])}
    rows = ga(["pagePath", "pageTitle"], ["screenPageViews", "activeUsers"], [(d, d, "j")], ART_FILTER,
              order=[{"metric": {"metricName": "screenPageViews"}, "desc": True}], limit=20)
    D["articles"] = [{"pagePath": x["pagePath"], "pageTitle": _titre(x["pageTitle"]),
                      "screenPageViews": int(x["screenPageViews"]), "activeUsers": int(x["activeUsers"])} for x in rows]
    rows = ga(["sessionDefaultChannelGroup"], ["sessions", "activeUsers"], [(d, d, "j"), (j8, j8, "j8")],
              order=[{"metric": {"metricName": "sessions"}, "desc": True}])
    D["canaux"] = rows
    D["sources"] = ga(["sessionSource", "sessionMedium"], ["sessions", "activeUsers"], [(d, d, "j")],
                      order=[{"metric": {"metricName": "sessions"}, "desc": True}], limit=12)
    D["appareils"] = ga(["deviceCategory"], ["activeUsers", "sessions"], [(d, d, "j")])
    D["fidelite"] = ga(["newVsReturning"], ["activeUsers"], [(d, d, "j")])
    D["entrees"] = ga(["landingPage"], ["sessions", "activeUsers"], [(d, d, "j")],
                      order=[{"metric": {"metricName": "sessions"}, "desc": True}], limit=10)
    rows = ga(["country"], ["activeUsers"], [(d, d, "j"), (n1, n1, "n1")],
              order=[{"metric": {"metricName": "activeUsers"}, "desc": True}], limit=60)
    D["pays"] = _pays(rows)
    return D


def _pays(rows):
    j = {x["country"]: x["activeUsers"] for x in _par_range(rows, "j")}
    n = {x["country"]: x["activeUsers"] for x in _par_range(rows, "n1")}
    top = sorted(j.items(), key=lambda t: -t[1])[:10]
    res = []
    for c, v in top:
        var = _var(v, n.get(c, 0))
        alerte = c not in RA_FRANCO and (n.get(c, 0) == 0 and v >= 200 or (var is not None and var > 300))
        res.append({"pays": c, "users": v, "n1": n.get(c, 0), "var": var, "alerte": bool(alerte)})
    return res


def collecter_semaine(lundi):
    W = {}
    dim = lundi + datetime.timedelta(6)
    s1 = (lundi - datetime.timedelta(7), dim - datetime.timedelta(7))
    n1 = (lundi - datetime.timedelta(364), dim - datetime.timedelta(364))
    W["periode"] = [str(lundi), str(dim)]
    rg = [(lundi, dim, "s"), (s1[0], s1[1], "s1"), (n1[0], n1[1], "n1")]
    rows = ga([], [k for k, _ in KPI], rg)
    W["kpi"] = {nm: {k: (_par_range(rows, nm)[0][k] if _par_range(rows, nm) else 0.0) for k, _ in KPI} for _, _, nm in rg}
    rows = ga(["date"], ["activeUsers", "screenPageViews"], rg)
    W["jours"] = {nm: sorted([(x["date"], x["activeUsers"], x["screenPageViews"]) for x in _par_range(rows, nm)])
                  for _, _, nm in rg}
    rows = ga(["dayOfWeek", "hour"], ["activeUsers"], [(lundi, dim, "s")])
    hm = [[0.0] * 24 for _ in range(7)]
    for x in rows:
        dw = (int(x["dayOfWeek"]) - 1) % 7  # GA : 0 = dimanche -> index lundi = 0
        hm[dw][int(x["hour"])] = x["activeUsers"]
    W["chaleur"] = hm
    W["canaux"] = ga(["sessionDefaultChannelGroup"], ["sessions"], rg[:2],
                     order=[{"metric": {"metricName": "sessions"}, "desc": True}])
    rows = ga(["pagePath", "pageTitle"], ["screenPageViews", "activeUsers"], [(lundi, dim, "s")], ART_FILTER,
              order=[{"metric": {"metricName": "screenPageViews"}, "desc": True}], limit=30)
    W["w_articles"] = [{"pagePath": x["pagePath"], "pageTitle": _titre(x["pageTitle"]),
                        "screenPageViews": int(x["screenPageViews"]), "activeUsers": int(x["activeUsers"])} for x in rows]
    rows = ga(["country"], ["activeUsers"], [(lundi, dim, "s"), (n1[0], n1[1], "n1")],
              order=[{"metric": {"metricName": "activeUsers"}, "desc": True}], limit=60)
    W["pays"] = _pays(rows)
    return W


# ---------------------------------------------------------------- graphiques
def _graphiques(D, W, dossier):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        p = os.path.join(RA_FONTS, f)
        if os.path.exists(p):
            font_manager.fontManager.addfont(p)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False})
    out = {}
    # courbe horaire
    h = D["heures"]
    fig, ax = plt.subplots(figsize=(7.2, 2.6), dpi=150)
    x = list(range(24))
    ax.plot(x, h["n1"], color=RA_GRIS, lw=1, ls=":", label="N-1")
    ax.plot(x, h["j8"], color="#c9a400", lw=1, ls="--", label="J-8")
    ax.plot(x, h["j"], color=RA_NOIR, lw=2, label="Jour")
    pics = sorted(range(24), key=lambda i: -h["j"][i])[:3]
    ax.scatter(pics, [h["j"][i] for i in pics], color=RA_ROUGE, zorder=5, s=30)
    for i in pics:
        ax.annotate(f"{i}h", (i, h["j"][i]), textcoords="offset points", xytext=(0, 6), ha="center",
                    color=RA_ROUGE, fontsize=7, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{i}h" for i in x], fontsize=6)
    ax.set_ylabel("Utilisateurs actifs")
    ax.legend(frameon=False, fontsize=7, ncol=3)
    fig.tight_layout()
    out["heures"] = os.path.join(dossier, "g_heures.png")
    fig.savefig(out["heures"])
    plt.close(fig)
    # tendance 30 j
    t = D["tendance"]
    fig, ax = plt.subplots(figsize=(7.2, 2.3), dpi=150)
    ax.plot(range(len(t["n1"])), [v for _, v in t["n1"]], color=RA_GRIS, lw=1, ls=":", label="N-1")
    ax.plot(range(len(t["j"])), [v for _, v in t["j"]], color=RA_ROUGE, lw=2, label="30 derniers jours")
    lab = [f"{s[6:8]}/{s[4:6]}" for s, _ in t["j"]]
    ax.set_xticks(range(0, len(lab), 3))
    ax.set_xticklabels(lab[::3], fontsize=6)
    ax.legend(frameon=False, fontsize=7)
    fig.tight_layout()
    out["tendance"] = os.path.join(dossier, "g_tendance.png")
    fig.savefig(out["tendance"])
    plt.close(fig)
    if W:
        js = W["jours"]
        fig, ax = plt.subplots(figsize=(7.2, 2.3), dpi=150)
        lbl = [j[:3] for j in JOURS]
        for nm, col, ls, lw, lb in (("n1", RA_GRIS, ":", 1, "N-1"), ("s1", "#c9a400", "--", 1, "S-1"),
                                    ("s", RA_NOIR, "-", 2, "Semaine")):
            ax.plot(range(len(js[nm])), [v[1] for v in js[nm]], color=col, ls=ls, lw=lw, label=lb, marker="o", ms=3)
        ax.set_xticks(range(7))
        ax.set_xticklabels(lbl)
        ax.legend(frameon=False, fontsize=7, ncol=3)
        fig.tight_layout()
        out["semaine"] = os.path.join(dossier, "g_semaine.png")
        fig.savefig(out["semaine"])
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(7.2, 2.4), dpi=150)
        im = ax.imshow(W["chaleur"], aspect="auto", cmap="Reds")
        ax.set_yticks(range(7))
        ax.set_yticklabels([j[:3] for j in JOURS])
        ax.set_xticks(range(24))
        ax.set_xticklabels([f"{i}h" for i in range(24)], fontsize=6)
        fig.colorbar(im, ax=ax, fraction=0.025)
        fig.tight_layout()
        out["chaleur"] = os.path.join(dossier, "g_chaleur.png")
        fig.savefig(out["chaleur"])
        plt.close(fig)
    return out


# ---------------------------------------------------------------- PDF
def _pdf(D, W, chemin, imgs):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak,
                                    KeepTogether)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xml.sax.saxutils import escape
    try:
        pdfmetrics.registerFont(TTFont("DV", os.path.join(RA_FONTS, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DVB", os.path.join(RA_FONTS, "DejaVuSans-Bold.ttf")))
        F, FB = "DV", "DVB"
    except Exception:
        F, FB = "Helvetica", "Helvetica-Bold"
    rouge, jaune, noir = colors.HexColor(RA_ROUGE), colors.HexColor(RA_JAUNE), colors.HexColor(RA_NOIR)
    st = {"h1": ParagraphStyle("h1", fontName=FB, fontSize=18, textColor=colors.white, leading=22),
          "sub": ParagraphStyle("sub", fontName=F, fontSize=9, textColor=colors.HexColor("#cccccc")),
          "h2": ParagraphStyle("h2", fontName=FB, fontSize=11.5, textColor=rouge, spaceBefore=8, spaceAfter=4),
          "p": ParagraphStyle("p", fontName=F, fontSize=8.5, leading=11),
          "c": ParagraphStyle("c", fontName=F, fontSize=7.5, leading=9),
          "cb": ParagraphStyle("cb", fontName=FB, fontSize=7.5, leading=9)}
    W_ = A4[0] - 24 * mm

    def tab(data, widths, header=True, align_right_from=1):
        rows = [[Paragraph(escape(str(c)), st["cb"] if (header and i == 0) else st["c"]) for c in r]
                for i, r in enumerate(data)]
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        s = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f6f6")])]
        if header:
            s.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fff4c2")))
        t.setStyle(TableStyle(s))
        return t

    d = datetime.date.fromisoformat(D["date"])
    el = []
    une = Table([[Paragraph("Audience <font color='%s'>DirectVelo</font>" % RA_JAUNE, st["h1"])],
                 [Paragraph(f"Rapport du {_fmt_date(d)} · www.directvelo.com · GA4 · heure de Paris", st["sub"])]],
                colWidths=[W_])
    une.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), noir), ("LINEBELOW", (0, -1), (-1, -1), 3, rouge),
                             ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, 0), 8),
                             ("BOTTOMPADDING", (0, -1), (-1, -1), 8)]))
    el += [une, Spacer(1, 6)]
    k = D["kpi"]
    u, u8, un = k["j"]["activeUsers"], k["j8"]["activeUsers"], k["n1"]["activeUsers"]
    pic = max(range(24), key=lambda i: D["heures"]["j"][i])
    a1 = D["articles"][0] if D["articles"] else None
    retenir = [f"<b>{_fmt_int(u)}</b> utilisateurs ({_fmt_var(_var(u, u8))} vs J-8, {_fmt_var(_var(u, un))} vs N-1).",
               f"Pic d'audience à <b>{pic}h</b> ({_fmt_int(D['heures']['j'][pic])} utilisateurs)."]
    if a1:
        retenir.append(f"Article le plus lu : <b>{escape(a1['pageTitle'])}</b> ({_fmt_int(a1['screenPageViews'])} pages vues).")
    al = [p["pays"] for p in D["pays"] if p["alerte"]]
    if al:
        retenir.append("<font color='%s'><b>Trafic suspect</b></font> : %s (hausse de plus de 300 %% vs N-1)." % (RA_ROUGE, ", ".join(al)))
    box = Table([[Paragraph("<b>À retenir</b><br/>" + "<br/>".join("• " + r for r in retenir), st["p"])]], colWidths=[W_])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbe6")),
                             ("BOX", (0, 0), (-1, -1), 1, jaune), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    el += [box, Paragraph("Chiffres clés", st["h2"])]
    data = [["Indicateur", "Jour", "J-2", "Var.", "J-8", "Var.", "N-1", "Var."]]
    for kk, lab in KPI:
        v = k["j"][kk]
        data.append([lab, _fmt_kpi(kk, v), _fmt_kpi(kk, k["j2"][kk]), _fmt_var(_var(v, k["j2"][kk])),
                     _fmt_kpi(kk, k["j8"][kk]), _fmt_var(_var(v, k["j8"][kk])), _fmt_kpi(kk, k["n1"][kk]),
                     _fmt_var(_var(v, k["n1"][kk]))])
    el.append(tab(data, [W_ * 0.22] + [W_ * 0.78 / 7] * 7))
    el.append(KeepTogether([Paragraph("Courbe horaire (3 pics en rouge)", st["h2"]), Image(imgs["heures"], W_, W_ * 2.6 / 7.2)]))
    tr = [("Nuit 0h-6h", 0, 6), ("Matin 6h-9h", 6, 9), ("Matinée 9h-12h", 9, 12), ("Midi 12h-14h", 12, 14),
          ("Après-midi 14h-18h", 14, 18), ("Soirée 18h-21h", 18, 21), ("Nuit 21h-24h", 21, 24)]
    data = [["Tranche", "Utilisateurs (somme horaire)", "J-8", "Var."]]
    for lab, a, b in tr:
        s, s8 = sum(D["heures"]["j"][a:b]), sum(D["heures"]["j8"][a:b])
        data.append([lab, _fmt_int(s), _fmt_int(s8), _fmt_var(_var(s, s8))])
    el.append(KeepTogether([Paragraph("Tranches horaires", st["h2"]), tab(data, [W_ * 0.34, W_ * 0.26, W_ * 0.2, W_ * 0.2])]))
    el.append(KeepTogether([Paragraph("Tendance sur 30 jours vs N-1", st["h2"]), Image(imgs["tendance"], W_, W_ * 2.3 / 7.2)]))
    el.append(Paragraph("Top 20 des articles", st["h2"]))
    data = [["#", "Article", "Pages vues", "Utilisateurs"]]
    for i, a in enumerate(D["articles"], 1):
        data.append([i, a["pageTitle"][:110], _fmt_int(a["screenPageViews"]), _fmt_int(a["activeUsers"])])
    el.append(tab(data, [W_ * 0.05, W_ * 0.65, W_ * 0.15, W_ * 0.15]))
    el.append(Paragraph("Canaux d'acquisition", st["h2"]))
    cj = {x["sessionDefaultChannelGroup"]: x for x in _par_range(D["canaux"], "j")}
    c8 = {x["sessionDefaultChannelGroup"]: x for x in _par_range(D["canaux"], "j8")}
    data = [["Canal", "Sessions", "J-8", "Var."]]
    for c, x in sorted(cj.items(), key=lambda t: -t[1]["sessions"]):
        s8 = c8.get(c, {}).get("sessions", 0)
        data.append([c, _fmt_int(x["sessions"]), _fmt_int(s8), _fmt_var(_var(x["sessions"], s8))])
    el.append(tab(data, [W_ * 0.4, W_ * 0.2, W_ * 0.2, W_ * 0.2]))
    el.append(Paragraph("Top 12 source / support", st["h2"]))
    data = [["Source / support", "Sessions", "Utilisateurs"]]
    for x in D["sources"]:
        data.append([f"{x['sessionSource']} / {x['sessionMedium']}", _fmt_int(x["sessions"]), _fmt_int(x["activeUsers"])])
    el.append(tab(data, [W_ * 0.6, W_ * 0.2, W_ * 0.2]))
    el.append(Paragraph("Google Discover n'apparaît pas comme source distincte dans GA4 ; le trafic de Google "
                        "Actualités apparaît sous news.google.com.", st["c"]))
    el.append(Paragraph("Appareils et fidélité", st["h2"]))
    tot = sum(x["activeUsers"] for x in D["appareils"]) or 1
    data = [["Appareil", "Utilisateurs", "Part"]] + [[x["deviceCategory"], _fmt_int(x["activeUsers"]),
                                                      f"{x['activeUsers'] / tot * 100:.1f} %".replace(".", ",")]
                                                     for x in sorted(D["appareils"], key=lambda t: -t["activeUsers"])]
    tot2 = sum(x["activeUsers"] for x in D["fidelite"]) or 1
    data += [["Nouveaux / fidèles", "", ""]] + [[{"new": "Nouveaux", "returning": "Fidèles"}.get(x["newVsReturning"], x["newVsReturning"]),
                                                 _fmt_int(x["activeUsers"]),
                                                 f"{x['activeUsers'] / tot2 * 100:.1f} %".replace(".", ",")]
                                                for x in D["fidelite"]]
    el.append(tab(data, [W_ * 0.5, W_ * 0.25, W_ * 0.25]))
    el.append(Paragraph("Top 10 des pages d'entrée", st["h2"]))
    data = [["Page d'entrée", "Sessions", "Utilisateurs"]] + [[(x["landingPage"] or "(non renseigné)")[:100], _fmt_int(x["sessions"]),
                                                               _fmt_int(x["activeUsers"])] for x in D["entrees"]]
    el.append(tab(data, [W_ * 0.6, W_ * 0.2, W_ * 0.2]))
    el.append(Paragraph("Top 10 des pays", st["h2"]))
    data = [["Pays", "Utilisateurs", "N-1", "Var.", "Alerte"]] + [[p["pays"], _fmt_int(p["users"]), _fmt_int(p["n1"]),
                                                                   _fmt_var(p["var"]), "TRAFIC SUSPECT" if p["alerte"] else ""]
                                                                  for p in D["pays"]]
    el.append(tab(data, [W_ * 0.32, W_ * 0.17, W_ * 0.17, W_ * 0.14, W_ * 0.2]))
    if W:
        lun = datetime.date.fromisoformat(W["periode"][0])
        dim = datetime.date.fromisoformat(W["periode"][1])
        el += [PageBreak(), Paragraph(f"Bilan de la semaine du {_fmt_date(lun)} au {_fmt_date(dim)}", st["h2"])]
        wk = W["kpi"]
        data = [["Indicateur", "Semaine", "S-1", "Var.", "N-1", "Var."]]
        for kk, lab in KPI:
            v = wk["s"][kk]
            data.append([lab, _fmt_kpi(kk, v), _fmt_kpi(kk, wk["s1"][kk]), _fmt_var(_var(v, wk["s1"][kk])),
                         _fmt_kpi(kk, wk["n1"][kk]), _fmt_var(_var(v, wk["n1"][kk]))])
        el.append(tab(data, [W_ * 0.25] + [W_ * 0.15] * 5))
        el.append(KeepTogether([Paragraph("Jour par jour (utilisateurs)", st["h2"]), Image(imgs["semaine"], W_, W_ * 2.3 / 7.2)]))
        el.append(KeepTogether([Paragraph("Carte de chaleur jour × heure", st["h2"]), Image(imgs["chaleur"], W_, W_ * 2.4 / 7.2)]))
        cs = {x["sessionDefaultChannelGroup"]: x["sessions"] for x in _par_range(W["canaux"], "s")}
        c1 = {x["sessionDefaultChannelGroup"]: x["sessions"] for x in _par_range(W["canaux"], "s1")}
        data = [["Canal", "Sessions", "S-1", "Var."]] + [[c, _fmt_int(v), _fmt_int(c1.get(c, 0)), _fmt_var(_var(v, c1.get(c, 0)))]
                                                         for c, v in sorted(cs.items(), key=lambda t: -t[1])]
        el.append(KeepTogether([Paragraph("Canaux de la semaine", st["h2"]), tab(data, [W_ * 0.4, W_ * 0.2, W_ * 0.2, W_ * 0.2])]))
        el.append(Paragraph("Top 30 des articles de la semaine", st["h2"]))
        data = [["#", "Article", "Pages vues", "Utilisateurs"]] + [[i, a["pageTitle"][:110], _fmt_int(a["screenPageViews"]),
                                                                    _fmt_int(a["activeUsers"])] for i, a in enumerate(W["w_articles"], 1)]
        el.append(tab(data, [W_ * 0.05, W_ * 0.65, W_ * 0.15, W_ * 0.15]))
        fr = next((p for p in W["pays"] if p["pays"] == "France"), None)
        el.append(Paragraph("Pays de la semaine" + (f" (référence France : {_fmt_var(fr['var'])} vs N-1)" if fr else ""), st["h2"]))
        data = [["Pays", "Utilisateurs", "N-1", "Var.", "Alerte"]] + [[p["pays"], _fmt_int(p["users"]), _fmt_int(p["n1"]),
                                                                       _fmt_var(p["var"]), "TRAFIC SUSPECT" if p["alerte"] else ""]
                                                                      for p in W["pays"]]
        el.append(tab(data, [W_ * 0.32, W_ * 0.17, W_ * 0.17, W_ * 0.14, W_ * 0.2]))

    def pied(c, doc):
        c.saveState()
        c.setFont(F, 7)
        c.setFillColor(colors.HexColor("#888888"))
        c.drawString(12 * mm, 8 * mm, f"DirectVelo · Rapport d'audience du {d.strftime('%d/%m/%Y')} · page {doc.page}")
        c.restoreState()

    doc = SimpleDocTemplate(chemin, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=14 * mm, title=f"Audience DirectVelo {D['date']}")
    doc.build(el, onFirstPage=pied, onLaterPages=pied)
    return chemin


# ---------------------------------------------------------------- sauvegarde pour la veille
def _sauver_json(D, W):
    data = dict(D)
    if W:
        data["semaine"] = W
        data["w_articles"] = W["w_articles"]
    p = os.path.join(RA_OUT, f"audience_{D['date']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    try:
        chemin = f"etat/audience/{D['date']}.json"
        ep = f"/repos/michael-dv/veille-directvelo/contents/{chemin}"
        sha = None
        r, e = proxy_execute("GET", ep, "github", query_params={"ref": "main"})
        if not e and isinstance(r, dict):
            sha = r.get("sha")
        body = {"message": f"Audience {D['date']}", "branch": "main",
                "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()}
        if sha:
            body["sha"] = sha
        proxy_execute("PUT", ep, "github", body=body)
    except Exception as e:
        print("Sauvegarde GitHub de l'audience impossible :", e)
    return p


# ---------------------------------------------------------------- run
def run(report_date=None, test=False, weekly=None, destinataires=None, bcc=None, envoyer=True):
    res = {"date": None, "users": None, "var_j8": None, "var_n1": None, "errors": [], "email_id": None,
           "email_error": None, "notify": False, "pdf": None}
    try:
        if report_date is None:
            report_date = datetime.datetime.now(RA_TZ).date() - datetime.timedelta(1)
        d = report_date
        res["date"] = str(d)
        os.makedirs(RA_OUT, exist_ok=True)
        D = collecter_jour(d)
        W = None
        if weekly or (weekly is None and datetime.datetime.now(RA_TZ).weekday() == 0):
            # semaine complète la plus récente (lundi-dimanche) se terminant au plus tard le jour du rapport
            lundi = d - datetime.timedelta(6) if d.weekday() == 6 else d - datetime.timedelta(d.weekday() + 7)
            try:
                W = collecter_semaine(lundi)
            except Exception as e:
                res["errors"].append(f"bilan hebdo : {e}")
        _sauver_json(D, W)
        k = D["kpi"]
        res["users"] = int(k["j"]["activeUsers"])
        res["var_j8"] = _var(k["j"]["activeUsers"], k["j8"]["activeUsers"])
        res["var_n1"] = _var(k["j"]["activeUsers"], k["n1"]["activeUsers"])
        dossier = os.path.join(RA_OUT, f"audience_{d}")
        os.makedirs(dossier, exist_ok=True)
        imgs = _graphiques(D, W, dossier)
        pdf = os.path.join(RA_OUT, f"DirectVelo_Audience_{d}.pdf")
        _pdf(D, W, pdf, imgs)
        res["pdf"] = pdf
        if envoyer:
            up, err = upload_local_file(pdf)
            if err:
                raise RuntimeError(f"upload : {err}")
            s3 = up.get("s3key") if isinstance(up, dict) else None
            if not s3 and isinstance(up, dict):
                s3 = (up.get("data") or {}).get("s3key")
            a1 = D["articles"][0] if D["articles"] else None
            sujet = ("[TEST] " if test else "") + f"📊 Audience DirectVelo – {_fmt_date(d)} : {_fmt_int(res['users'])} utilisateurs ({_fmt_var(res['var_j8'])} vs J-8)"
            if W:
                sujet += " + bilan de la semaine"
            corps = (f"<p><b>{_fmt_int(res['users'])}</b> utilisateurs le {_fmt_date(d)} : {_fmt_var(res['var_j8'])} vs J-8, "
                     f"{_fmt_var(res['var_n1'])} vs N-1.</p>")
            if a1:
                corps += f"<p>Article le plus lu : <a href=\"https://{RA_HOST}{a1['pagePath']}\">{a1['pageTitle']}</a> ({_fmt_int(a1['screenPageViews'])} pages vues).</p>"
            corps += "<p>Rapport complet en pièce jointe (PDF).</p>"
            to = destinataires or RA_TO
            args = {"recipient_email": to[0], "extra_recipients": to[1:], "bcc": bcc if bcc is not None else RA_BCC,
                    "subject": sujet, "body": corps, "is_html": True,
                    "attachment": {"name": os.path.basename(pdf), "mimetype": "application/pdf", "s3key": s3}}
            r, e = run_composio_tool("GMAIL_SEND_EMAIL", args)
            if e:
                res["email_error"] = str(e)
            else:
                dd = r.get("data", {}) if isinstance(r, dict) else {}
                dd = dd.get("response_data", dd)
                res["email_id"] = dd.get("id")
                if not res["email_id"]:
                    res["email_error"] = str(r)[:300]
    except Exception as e:
        res["errors"].append(f"{type(e).__name__} : {e}")
        print(traceback.format_exc())
    v = res["var_j8"]
    res["notify"] = bool(res["errors"] or res["email_error"] or (envoyer and not res["email_id"]) or
                         (v is not None and abs(v) > 25))
    return res


print("rapport_audience chargé : run(report_date=None, test=False, weekly=None, destinataires=None, bcc=None, envoyer=True)")

#!/usr/bin/env python3
"""
Surveillance du tea time Claridge's (The Foyer & Reading Room), 2 personnes.

Le script ouvre la page de reservation et lit le calendrier de disponibilites
que le site charge en arriere-plan (90 jours). Il alerte pour la date choisie
et enregistre l'etat de TOUTES les dates, pour l'analyse des changements.

Installation (une fois) :
    python3 -m pip install playwright requests
    python3 -m playwright install chromium

Utilisation :
    python3 claridges_tea.py --test            # une verification de controle
    caffeinate -i python3 claridges_tea.py     # surveillance continue (Mac eveille)
    python3 claridges_tea.py --analyse         # analyse des changements
    Ajouter --date AAAA-MM-JJ pour une autre date (defaut : 2026-12-01)
"""
import argparse
import csv
import json
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- Reglages
DATE = "2026-12-01"
GUESTS = 2
FOYER_ID = "ahNzfnNldmVucm9vbXMtc2VjdXJlchwLEg9uaWdodGxvb3BfVmVudWUYgICWz5_fnQoM"
NTFY_TOPIC = "claridges-tea-ct-8q4m2x7k"   # sujet a suivre dans l'app ntfy (facultatif)
HEADLESS = True        # passer a False si le site bloque le navigateur invisible
INTERVAL_NORMAL = 7    # minutes entre deux verifications
INTERVAL_FINAL = 3     # minutes, dans les 4 derniers jours avant la date

DIR = Path(__file__).resolve().with_name("claridges_data")
LOG = DIR / "journal_calendrier.csv"
LONDON = ZoneInfo("Europe/London")


def construire_url(d):
    return ("https://www.maybourne.com/en/hotels/claridges/booking/afternoon-tea"
            f"?venue=thefoyerandreadingroom&category=DAY&noOfGuests={GUESTS}"
            f"&ExperienceType=AfternoonTea&step=1&date={d}")


URL = construire_url(DATE)
STATE = DIR / f"etat_{DATE}.json"


# ---------------------------------------------------------------- Lecture du site
def extraire(body):
    """{date: [creneaux reservables]} pour l'afternoon tea de la Foyer."""
    res = {}
    for groupe in body or []:
        for v in groupe.get("venues") or []:
            if v.get("venueId") != FOYER_ID:
                continue
            for a in v.get("Availability") or []:
                if "tea" not in (a.get("name") or "").lower():
                    continue
                libres = {s.get("time") for s in a.get("slots") or []
                          if s.get("onlineBooking") == "book"}
                res.setdefault(v.get("date"), set()).update(libres)
    return {d: sorted(s) for d, s in res.items()}


def verifier(browser, debug=False):
    """Retourne (statut, creneaux de DATE, instantane de toutes les dates)."""
    ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London",
                              viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    capt = {"cal": None, "jour": [], "tout": []}

    def on_resp(r):
        if "/api/fnb/availability/v1" not in r.url or "/alternate" in r.url:
            return
        try:
            body = r.json()
        except Exception:
            return
        if debug:
            capt["tout"].append({"url": r.url, "body": body})
        if "/calendar" in r.url:
            capt["cal"] = body
        else:
            capt["jour"].append(body)

    page.on("response", on_resp)
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.click("#onetrust-accept-btn-handler", timeout=5000)
        except Exception:
            pass
        for _ in range(40):                       # attend le calendrier (20 s max)
            if capt["cal"] is not None:
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(3000)               # laisse arriver le detail du jour

        instantane = extraire(capt["cal"])
        for body in capt["jour"]:                 # le detail du jour fait foi
            instantane.update(extraire(body))

        if DATE in instantane:
            libres = instantane[DATE]
            statut = "dispo" if libres else "complet"
        else:
            libres, statut = [], "inconnu"

        if debug:
            page.screenshot(path=str(DIR / "capture.png"), full_page=True)
            (DIR / "reseau.json").write_text(
                json.dumps(capt["tout"], default=str), encoding="utf-8")
        return statut, libres, instantane
    finally:
        ctx.close()


# ---------------------------------------------------------------- Notifications
def notifier(titre, msg):
    print(f"*** {titre} - {msg}")
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers={"Title": titre, "Priority": "urgent",
                               "Click": URL, "Tags": "tea"}, timeout=10)
    except Exception:
        pass
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{titre}" sound name "Glass"'])


# ---------------------------------------------------------------- Surveillance
def ecrire_journal(horodatage, statut, instantane):
    nouveau = not LOG.exists()
    dates = sorted(instantane)
    compact = {d: s for d, s in instantane.items() if s}
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nouveau:
            w.writerow(["horodatage_londres", "statut", "premiere_date",
                        "derniere_date", "creneaux_libres"])
        w.writerow([horodatage.isoformat(timespec="seconds"), statut,
                    dates[0] if dates else "", dates[-1] if dates else "",
                    json.dumps(compact, separators=(",", ":"))])


def boucle(browser):
    prev = set(json.loads(STATE.read_text())) if STATE.exists() else None
    echecs = 0
    fin = datetime.fromisoformat(DATE + "T18:00").replace(tzinfo=LONDON)
    print(f"Surveillance du {DATE} pour {GUESTS} personnes. Ctrl+C pour arreter.")
    while datetime.now(LONDON) < fin:
        maintenant = datetime.now(LONDON)
        try:
            statut, libres, instantane = verifier(browser)
        except Exception as e:
            statut, libres, instantane = "erreur", [], {}
            print("Erreur :", e)
        if instantane:
            ecrire_journal(maintenant, statut, instantane)
        print(f"{maintenant:%d/%m %H:%M} (Londres) - {DATE} : {statut} - "
              f"{', '.join(libres) or 'aucun creneau libre'}")

        if statut in ("dispo", "complet"):
            echecs = 0
            cur = set(libres)
            nouveaux = cur - prev if prev is not None else cur
            if nouveaux:
                notifier("Tea time Claridge's : place libre",
                         f"{DATE}, {GUESTS} pers. : {', '.join(sorted(nouveaux))} "
                         "(heure de Londres). Reservez vite !")
            prev = cur
            STATE.write_text(json.dumps(sorted(cur)))
        else:
            echecs += 1
            if echecs == 3:
                notifier("Surveillance Claridge's",
                         "3 verifications sans lecture du site : a verifier.")

        jours = (fin - datetime.now(LONDON)).days
        minutes = INTERVAL_FINAL if jours <= 4 else INTERVAL_NORMAL
        time.sleep(max(60, minutes * 60 + random.randint(-60, 60)))
    print("Date passee, fin de la surveillance.")


# ---------------------------------------------------------------- Analyse
def tranche(jours):
    if jours <= 1:
        return "J-0 a J-1"
    if jours <= 2:
        return "J-2"
    if jours <= 7:
        return "J-3 a J-7"
    if jours <= 30:
        return "J-8 a J-30"
    return "plus de 30 j"


def analyser():
    if not LOG.exists():
        print("Pas encore de donnees : lancez d'abord la surveillance.")
        return
    rows = list(csv.DictReader(open(LOG, encoding="utf-8")))
    if len(rows) < 2:
        print("Pas assez de donnees pour l'instant.")
        return

    liberations, reprises, ouvertures = [], [], []
    ouverts, durees = {}, []
    checks_par_heure = Counter()
    prev, prev_fen = None, None
    for r in rows:
        t = datetime.fromisoformat(r["horodatage_londres"])
        checks_par_heure[t.hour] += 1
        cur = {(d, s) for d, slots in json.loads(r["creneaux_libres"]).items()
               for s in slots}
        fen = (r["premiere_date"], r["derniere_date"])
        if prev is not None:
            communs = lambda d: max(fen[0], prev_fen[0]) <= d <= min(fen[1], prev_fen[1])
            for d, s in cur - prev:
                if communs(d):
                    liberations.append((t, d, s))
                    ouverts[(d, s)] = t
            for d, s in prev - cur:
                if communs(d):
                    reprises.append((t, d, s))
                    if (d, s) in ouverts:
                        durees.append((t - ouverts.pop((d, s))).total_seconds() / 60)
            if fen[1] > prev_fen[1]:
                ouvertures.append((t, fen[1]))
        prev, prev_fen = cur, fen

    t0 = datetime.fromisoformat(rows[0]["horodatage_londres"])
    t1 = datetime.fromisoformat(rows[-1]["horodatage_londres"])
    print(f"{len(rows)} verifications du {t0:%d/%m %H:%M} au {t1:%d/%m %H:%M} (heure de Londres)")
    print(f"{len(liberations)} places liberees, {len(reprises)} reprises, toutes dates confondues.\n")

    if ouvertures:
        print("Ouverture de nouvelles dates (fenetre de 90 jours) :")
        for t, d in ouvertures[-10:]:
            print(f"  {t:%a %d/%m %H:%M} -> {d} mis en ligne")
        print()

    if liberations:
        print("Liberations par heure de la journee (Londres) :")
        par_heure = Counter(t.hour for t, _, _ in liberations)
        for h in range(24):
            if par_heure[h] or checks_par_heure[h]:
                print(f"  {h:02d}h  {'#' * min(par_heure[h], 40):<40} {par_heure[h]}")

        print("\nLiberations selon le delai avant la date du tea time :")
        par_delai = Counter(tranche((date.fromisoformat(d) - t.date()).days)
                            for t, d, _ in liberations)
        for k in ["J-0 a J-1", "J-2", "J-3 a J-7", "J-8 a J-30", "plus de 30 j"]:
            print(f"  {k:<13} {par_delai[k]}")

        print("\nLiberations par jour de la semaine :")
        jours = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        par_jour = Counter(t.weekday() for t, _, _ in liberations)
        for i, j in enumerate(jours):
            print(f"  {j}  {par_jour[i]}")

    if durees:
        print(f"\nUne place liberee reste libre en mediane {statistics.median(durees):.0f} min "
              f"(min {min(durees):.0f}, max {max(durees):.0f}).")

    cible = [(t, "+", s) for t, d, s in liberations if d == DATE] + \
            [(t, "-", s) for t, d, s in reprises if d == DATE]
    print(f"\nMouvements sur le {DATE} :")
    if cible:
        for t, signe, s in sorted(cible):
            print(f"  {t:%a %d/%m %H:%M}  {signe} {s}")
    else:
        print("  aucun pour l'instant")
    print("\nPrecision limitee par l'intervalle entre deux verifications.")


# ---------------------------------------------------------------- Main
def main():
    global DATE, URL, STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="une verification de controle")
    ap.add_argument("--analyse", action="store_true", help="analyse du journal")
    ap.add_argument("--date", help="date a surveiller, AAAA-MM-JJ (defaut : 2026-12-01)")
    args = ap.parse_args()
    if args.date:
        DATE = args.date
        URL = construire_url(DATE)
        STATE = DIR / f"etat_{DATE}.json"
    DIR.mkdir(exist_ok=True)

    if args.analyse:
        analyser()
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            if args.test:
                statut, libres, instantane = verifier(browser, debug=True)
                print(f"Date : {DATE}")
                print(f"Statut : {statut}")
                print(f"Creneaux libres : {', '.join(libres) or 'aucun'}")
                dispo = {d: s for d, s in instantane.items() if s}
                print(f"\nCalendrier lu : {len(instantane)} dates, dont {len(dispo)} avec des places.")
                for d in sorted(dispo)[:10]:
                    print(f"  {d} : {', '.join(dispo[d])}")
                if len(dispo) > 10:
                    print("  ...")
                notifier("Test Claridge's", "Les notifications fonctionnent.")
            else:
                boucle(browser)
        finally:
            browser.close()


if __name__ == "__main__":
    main()


# ============================================
# Scraper Leboncoin -> Analyse IA (Gemini) -> Supabase
# Executable sur Google Colab (test) ou en automatique via GitHub Actions
# ============================================

# --- Installation (a lancer une fois dans une cellule Colab) ---
# !pip install scrapfly-sdk supabase google-genai -q

import os
import json
import time
import re
from scrapfly import ScrapflyClient, ScrapeConfig
from supabase import create_client

# ============================================
# CONFIGURATION
# Les cles ne sont JAMAIS ecrites en clair ici : elles viennent des variables
# d'environnement (secrets GitHub Actions en prod, ou definies a la main sur Colab).
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SCRAPFLY_API_KEY = os.environ.get("SCRAPFLY_API_KEY")

for nom, valeur in [
    ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY),
    ("GEMINI_API_KEY", GEMINI_API_KEY), ("SCRAPFLY_API_KEY", SCRAPFLY_API_KEY),
]:
    if not valeur:
        raise RuntimeError(
            f"Variable d'environnement {nom} manquante. "
            f"Sur Colab : os.environ['{nom}'] = '...' dans une cellule avant ce script. "
            f"Sur GitHub Actions : configure-la dans Settings > Secrets and variables > Actions."
        )

scrapfly = ScrapflyClient(key=SCRAPFLY_API_KEY)

# Recherche : toutes voitures, France entiere, tri par date (plus recentes en premier)
SEARCH_URL = "https://www.leboncoin.fr/recherche"
SEARCH_PARAMS = {
    "category": "2",       # 2 = Voitures
    "sort": "time",
    "order": "desc",
}

MAX_PAGES_PAR_RUN = 3          # limite le volume par execution (anti-ban)
MAX_ANNONCES_PAR_RUN = 10      # plafond dur : controle la conso de credits Scrapfly, peu importe le nb de pages
DELAI_ENTRE_REQUETES = 4       # secondes entre chaque page
SEUIL_MARGE_MIN_PCT = 15       # % de marge minimum pour considerer que c'est une pepite

GEMINI_RPM_LIMIT = 14          # marge de securite sous la limite gratuite (15 req/min)
_dernier_appel_gemini = [0.0]

HEADERS = {
    # plus utilise directement : Scrapfly gere maintenant le navigateur/fingerprint a notre place
}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    from google import genai as google_genai
    gemini_client = google_genai.Client(api_key=GEMINI_API_KEY)
    GEMINI_MODEL = "gemini-3.1-flash-lite"  # stable, quota gratuit plus genereux (~15 req/min)
except Exception as e:
    gemini_client = None
    print("Gemini indisponible, le fallback sera utilise :", e)


# ============================================
# 1. SCRAPING : recuperer les annonces
# ============================================
def _recuperer_html_via_scrapfly(url: str) -> str:
    """Recupere une page en passant par Scrapfly (proxy residentiel FR + bypass DataDome)."""
    result = scrapfly.scrape(ScrapeConfig(
        url=url,
        asp=True,        # active le contournement anti-bot (DataDome)
        country="FR",    # obligatoire : Leboncoin bloque quasi tout ce qui n'est pas FR
        render_js=False, # le JSON __NEXT_DATA__ est deja dans le HTML brut, pas besoin de JS
    ))
    return result.content


def recuperer_annonces_page(page: int):
    """Recupere une page de resultats et parse le JSON embarque __NEXT_DATA__."""
    params = dict(SEARCH_PARAMS)
    params["page"] = page
    url = SEARCH_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    try:
        html = _recuperer_html_via_scrapfly(url)
    except Exception as e:
        print(f"Page {page} : echec Scrapfly -", e)
        return []

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        print(f"Page {page} : structure __NEXT_DATA__ introuvable (le site a peut-etre change)")
        return []

    data = json.loads(match.group(1))
    try:
        ads = data["props"]["pageProps"]["searchData"]["ads"]
    except (KeyError, TypeError):
        print(f"Page {page} : aucune annonce trouvee dans le JSON")
        return []

    return ads


def chercher_champ_recursif(data, cle):
    """Cherche recursivement la premiere valeur non vide pour une cle donnee dans un JSON imbrique.
    Utile car la structure exacte de la page annonce peut varier / changer cote Leboncoin."""
    if isinstance(data, dict):
        if cle in data and data[cle]:
            return data[cle]
        for v in data.values():
            resultat = chercher_champ_recursif(v, cle)
            if resultat:
                return resultat
    elif isinstance(data, list):
        for item in data:
            resultat = chercher_champ_recursif(item, cle)
            if resultat:
                return resultat
    return None


def recuperer_description_complete(url: str) -> str:
    """La page de resultats de recherche ne contient PAS le texte complet de l'annonce
    (seulement titre/prix/attributs). Il faut aller chercher la page de l'annonce elle-meme."""
    try:
        html = _recuperer_html_via_scrapfly(url)
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            return ""
        data = json.loads(match.group(1))
        return chercher_champ_recursif(data, "body") or ""
    except Exception as e:
        print("Impossible de recuperer la description complete :", e)
        return ""


def extraire_champs(ad: dict) -> dict:
    """Normalise une annonce brute Leboncoin vers nos colonnes Supabase.
    Important : Leboncoin fournit "value" (code brut, ex: "1") ET "value_label"
    (texte lisible, ex: "Essence") pour chaque attribut. On utilise value_label
    pour avoir du texte exploitable plutot que des codes numeriques."""
    attrs = {
        a.get("key"): a.get("value_label") or a.get("value")
        for a in ad.get("attributes", [])
    }
    # kilometrage/annee ont besoin de la valeur numerique brute, pas du label
    attrs_bruts = {a.get("key"): a.get("value") for a in ad.get("attributes", [])}
    images = ad.get("images", {}).get("urls", [])
    location = ad.get("location", {})

    return {
        "url": f"https://www.leboncoin.fr/ad/voitures/{ad.get('list_id')}",
        "titre": ad.get("subject"),
        "marque": attrs.get("brand"),
        "modele": attrs.get("model"),
        "annee": int(attrs_bruts.get("regdate")) if str(attrs_bruts.get("regdate", "")).isdigit() else None,
        "kilometrage": int(attrs_bruts.get("mileage")) if str(attrs_bruts.get("mileage", "")).isdigit() else None,
        "carburant": attrs.get("fuel"),
        "boite_vitesse": attrs.get("gearbox"),
        "localisation": location.get("city"),
        "region": location.get("region_name") or location.get("region"),
        "image_url": images[0] if images else None,
        "description": ad.get("body", ""),
        "prix_annonce": (ad.get("price") or [None])[0],
    }


# ============================================
# 2. ESTIMATION : Gemini + fallback
# ============================================
def _respecter_rate_limit_gemini():
    """Espace les appels Gemini pour rester sous le quota gratuit (evite les 429)."""
    delai_min = 60 / GEMINI_RPM_LIMIT
    attente = delai_min - (time.time() - _dernier_appel_gemini[0])
    if attente > 0:
        time.sleep(attente)
    _dernier_appel_gemini[0] = time.time()


def estimer_avec_gemini(annonce: dict) -> dict:
    _respecter_rate_limit_gemini()
    prompt = f"""
Tu es expert en mecanique et cote automobile en France, avec une bonne connaissance
des pannes et faiblesses connues de chaque modele/motorisation (forums, retours
d'experience, fiabilite constructeur). Analyse cette annonce et reponds
UNIQUEMENT en JSON valide, sans texte autour, avec ce format exact :
{{
  "prix_estime_marche": <nombre ou null>,
  "analyse_ia": "<resume court en francais du profil de l'annonce>",
  "vices_detectes": "<signaux suspects detectes dans le TEXTE de la description ci-dessous (contradictions, prix trop bas, urgence suspecte...), ou vide si rien de suspect>",
  "defauts_connus_modele": "<pannes/faiblesses connues pour ce modele et cette motorisation precise, d'apres ta connaissance generale (pas le texte de l'annonce) - ou vide si tu n'as pas d'info fiable>",
  "points_a_verifier": "<liste courte des points concrets a controler a l'essai/inspection pour ce modele precis : ex. courroie de distribution, vanne EGR, boite DSG, corrosion, etc.>",
  "score_confiance": <nombre entre 0 et 1>
}}
Important :
- Si le champ Description ci-dessous est vide, ne signale PAS "absence de description" comme un vice cache.
- "defauts_connus_modele" et "points_a_verifier" doivent venir de ta connaissance generale du modele, pas seulement du texte de l'annonce.
- Si tu n'as pas de connaissance fiable sur ce modele precis, renvoie une chaine vide plutot que d'inventer.

Annonce :
Marque: {annonce.get('marque')}, Modele: {annonce.get('modele')}, Annee: {annonce.get('annee')}
Kilometrage: {annonce.get('kilometrage')} km, Carburant: {annonce.get('carburant')}
Prix affiche: {annonce.get('prix_annonce')} EUR
Description: {annonce.get('description')}
"""
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    texte = response.text.strip().strip("```json").strip("```").strip()
    return json.loads(texte)


def estimer_avec_fallback(annonce: dict) -> dict:
    """Algorithme de secours : moyenne des annonces deja en base pour marque/modele/annee proches."""
    marque, modele, annee = annonce.get("marque"), annonce.get("modele"), annonce.get("annee")
    prix_estime = None

    if marque and modele and annee:
        result = (
            supabase.table("annonces")
            .select("prix_annonce")
            .eq("marque", marque)
            .eq("modele", modele)
            .gte("annee", annee - 1)
            .lte("annee", annee + 1)
            .execute()
        )
        prix = [r["prix_annonce"] for r in result.data if r.get("prix_annonce")]
        if len(prix) >= 3:
            prix_estime = sum(prix) / len(prix)

    return {
        "prix_estime_marche": prix_estime,
        "analyse_ia": "Estimation par moyenne des annonces similaires (fallback, Gemini indisponible)",
        "vices_detectes": "",
        "defauts_connus_modele": "",
        "points_a_verifier": "",
        "score_confiance": 0.3 if prix_estime else 0.0,
    }


def analyser_annonce(annonce: dict) -> dict:
    try:
        if gemini_client is None:
            raise RuntimeError("Gemini non configure")
        return estimer_avec_gemini(annonce)
    except Exception as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            print("Quota Gemini atteint, pause de 20s avant nouvel essai...")
            time.sleep(20)
            try:
                return estimer_avec_gemini(annonce)
            except Exception as e2:
                print("Gemini a echoue une 2e fois, fallback utilise :", e2)
                return estimer_avec_fallback(annonce)
        print("Gemini a echoue, fallback utilise :", e)
        return estimer_avec_fallback(annonce)


# ============================================
# 3. INSERTION SUPABASE
# ============================================
def enregistrer_si_pepite(annonce: dict, analyse: dict):
    prix_annonce = annonce.get("prix_annonce")
    prix_marche = analyse.get("prix_estime_marche")

    marge_estimee = None
    marge_pct = None
    if prix_annonce and prix_marche:
        marge_estimee = prix_marche - prix_annonce
        marge_pct = round((marge_estimee / prix_marche) * 100, 2)

    ligne = {
        **annonce,
        "prix_estime_marche": prix_marche,
        "marge_estimee": marge_estimee,
        "marge_pourcentage": marge_pct,
        "analyse_ia": analyse.get("analyse_ia"),
        "vices_detectes": analyse.get("vices_detectes"),
        "defauts_connus_modele": analyse.get("defauts_connus_modele"),
        "points_a_verifier": analyse.get("points_a_verifier"),
        "score_confiance": analyse.get("score_confiance"),
        "statut": "nouveau" if (marge_pct or 0) >= SEUIL_MARGE_MIN_PCT else "a_verifier",
    }

    try:
        supabase.table("annonces").upsert(ligne, on_conflict="url").execute()
        print(f"OK  [{ligne['statut']}] {annonce.get('titre')} - marge {marge_pct}%")
    except Exception as e:
        print("Erreur insertion Supabase :", e)


# ============================================
# 4. BOUCLE PRINCIPALE
# ============================================
def verifier_connexion_supabase():
    """Teste la connectivite vers Supabase avant de lancer le run (diagnostic DNS/reseau)."""
    try:
        supabase.table("annonces").select("id").limit(1).execute()
        print("Connexion Supabase OK")
        return True
    except Exception as e:
        print("ECHEC connexion Supabase :", e)
        print("-> Verifie SUPABASE_URL (pas de faute de frappe), que le projet n'est pas en pause")
        print("   (Supabase met en pause les projets gratuits inactifs), et relance la cellule")
        print("   (Runtime > Restart session) si l'erreur est 'Name or service not known'.")
        return False


def run():
    if not verifier_connexion_supabase():
        return

    total_traitees = 0

    for page in range(1, MAX_PAGES_PAR_RUN + 1):
        if total_traitees >= MAX_ANNONCES_PAR_RUN:
            print(f"Plafond de {MAX_ANNONCES_PAR_RUN} annonces atteint, arret du run.")
            break

        print(f"--- Page {page} ---")
        ads = recuperer_annonces_page(page)
        if not ads:
            break

        for ad in ads:
            if total_traitees >= MAX_ANNONCES_PAR_RUN:
                print(f"Plafond de {MAX_ANNONCES_PAR_RUN} annonces atteint, arret du run.")
                break

            annonce = extraire_champs(ad)
            if not annonce.get("prix_annonce"):
                continue
            if not annonce.get("description"):
                annonce["description"] = recuperer_description_complete(annonce["url"])
                time.sleep(2)  # requete supplementaire = pause supplementaire (anti-ban)
            analyse = analyser_annonce(annonce)
            enregistrer_si_pepite(annonce, analyse)
            total_traitees += 1
            time.sleep(1)  # petite pause entre chaque analyse IA

        time.sleep(DELAI_ENTRE_REQUETES)

    print(f"Run termine : {total_traitees} annonce(s) traitee(s).")


if __name__ == "__main__":
    run()

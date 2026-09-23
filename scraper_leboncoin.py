import os
import json
import time
from curl_cffi import requests
from bs4 import BeautifulSoup
from google import genai
from supabase import create_client, Client

# --- Configurations via les Secrets GitHub / Environnement ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://bxhaiycfywkjruhzdmp.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Initialisation des clients Supabase et Gemini
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# --- URLs ciblées : La Bassée (59410) + 25 km ---
SEARCH_URLS = [
    # Recherche iPhone
    "https://www.leboncoin.fr/recherche?text=iphone&locations=r_59410_25000&sort=time",
    # Recherche PS5 / PlayStation 5
    "https://www.leboncoin.fr/recherche?text=ps5&locations=r_59410_25000&sort=time"
]

def analyser_avec_gemini(titre, description, prix):
    """ Analyse la rentabilité d'un iPhone ou d'une PS5 avec Gemini """
    if not gemini_client:
        print("⚠️ Clé Gemini manquante, analyse ignorée.")
        return None

    prompt = f"""
    Tu es un expert en achat-revente de produits high-tech d'occasion (iPhones et consoles PS5).
    Analyse cette annonce Leboncoin (Secteur Nord / Pas-de-Calais, La Bassée / Lille / Lens) :
    - Titre : {titre}
    - Prix affiché : {prix} €
    - Description : {description}

    Règles de filtrage strictes :
    1. VALIDE uniquement s'il s'agit d'un vrai téléphone iPhone ou d'une vraie console PS5.
    2. REJETTE immédiatement : coques, verres trempés, manettes seules, boîtes vides, jeux, pièces détachées, téléphones bloqués iCloud ou écrans cassés (sauf si marge exceptionnelle > 100€).
    3. Estime le PRIX DE REVENTE réel en main propre dans la région.
    4. Calcule la MARGE POTENTIELLE (Prix revente estimé - Prix achat).

    Réponds STRICTEMENT sous forme de JSON valide sans texte additionnel :
    {{
        "est_un_produit_valide": <true_ou_false>,
        "prix_revente_estime": <nombre_entier>,
        "marge_potentielle": <nombre_entier>,
        "est_une_bonne_affaire": <true_ou_false_si_marge_sup_a_40_euros>,
        "remarques": "<bref commentaire sur l'état, la capacité Go, le % batterie ou le modèle exact>"
    }}
    """

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_json)
    except Exception as e:
        print(f"⚠️ Erreur lors de l'analyse Gemini : {e}")
        return None

def recuperer_annonces_lbc(url):
    """ Télécharge la page Leboncoin avec des headers complets anti-403 """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.leboncoin.fr/",
        "Sec-Ch-Ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

    try:
        response = requests.get(
            url, 
            headers=headers, 
            impersonate="chrome124", 
            timeout=20,
            allow_redirects=True
        )
        
        if response.status_code != 200:
            print(f"❌ Blocage HTTP {response.status_code} sur l'URL.")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        script_tag = soup.find("script", id="__NEXT_DATA__")
        
        if not script_tag:
            print("❌ Bloc JSON __NEXT_DATA__ introuvable.")
            return []

        data = json.loads(script_tag.string)
        raw_ads = data.get('props', {}).get('pageProps', {}).get('searchData', {}).get('ads', [])
        
        annonces = []
        for ad in raw_ads[:15]:
            price_val = ad.get('price', [0])[0] if isinstance(ad.get('price'), list) else ad.get('price', 0)
            annonces.append({
                "id": f"lbc_{ad.get('list_id')}",
                "titre": ad.get('subject', ''),
                "prix": price_val,
                "description": ad.get('body', ''),
                "url": ad.get('url', ''),
                "image_url": ad.get('images', {}).get('thumb_url', '')
            })
        return annonces

    except Exception as e:
        print(f"❌ Erreur lors du scraping : {e}")
        return []

def main():
    print("🚀 Scan Tech Leboncoin (La Bassée + 25 km)...")

    if not supabase:
        print("❌ Erreur : Connexion Supabase non initialisée.")
        return

    # 1. Récupération des ID déjà enregistrés
    existing_ids = set()
    try:
        res = supabase.table("annonces").select("id").execute()
        existing_ids = {item['id'] for item in res.data}
        print(f"📊 {len(existing_ids)} annonces déjà enregistrées en base.")
    except Exception as e:
        print(f"⚠️ Impossible de lire les anciens ID Supabase : {e}")

    # 2. Parcours des recherches
    for url in SEARCH_URLS:
        print(f"\n🔍 Scan de la recherche...")
        annonces = recuperer_annonces_lbc(url)

        for ad in annonces:
            if ad['id'] in existing_ids:
                print(f"ℹ️ Déjà en base : {ad['titre']}")
                continue

            print(f"➔ Nouvelle offre trouvée : {ad['titre']} ({ad['prix']} €)")
            analyse = analyser_avec_gemini(ad['titre'], ad['description'], ad['prix'])

            if analyse and analyse.get('est_une_bonne_affaire') and analyse.get('est_un_produit_valide'):
                payload = {
                    "id": ad['id'],
                    "titre": ad['titre'],
                    "prix_achat": ad['prix'],
                    "prix_revente_estime": analyse['prix_revente_estime'],
                    "marge_estimee": analyse['marge_potentielle'],
                    "analyse_ia": analyse['remarques'],
                    "url": ad['url'],
                    "image_url": ad['image_url']
                }
                try:
                    supabase.table("annonces").upsert(payload).execute()
                    print(f"🔥 PÉPITE ENREGISTRÉE ! {ad['titre']} -> Marge : +{analyse['marge_potentielle']} €")
                    existing_ids.add(ad['id'])
                except Exception as e:
                    print(f"❌ Erreur sauvegarde Supabase : {e}")
            else:
                print(f"   Rejeté (accessoire, invalide ou marge < 40 €).")

            time.sleep(2)

if __name__ == "__main__":
    main()

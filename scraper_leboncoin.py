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
        clean_json = response.text.replace('```json', '').replace('

"""
Petit helper pour envoyer des messages sur Telegram.

Il lit le token du bot et l'identifiant du chat/channel dans les variables
d'environnement TELEGRAM_TOKEN et TELEGRAM_CHAT_ID (voir README.md pour
savoir comment les obtenir).

Test rapide une fois les variables configurées :
    python telegram_bot.py
"""

import os
import time
import requests
from dotenv import load_dotenv

# Charge automatiquement les variables définies dans un fichier .env
# (s'il existe) dans le même dossier. Pratique pour tester en local sans
# avoir à taper les variables à chaque fois dans le terminal.
load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_RETRIES = 5
BASE_RETRY_DELAY_SECONDS = 3   # attente avant la 1ère nouvelle tentative, doublée à chaque échec
THROTTLE_DELAY_SECONDS = 0.6   # petite pause après chaque envoi réussi, pour ne pas enchaîner trop vite

# "Connection: close" évite de réutiliser la même connexion TCP/TLS pour
# chaque appel. Sur certains PC Windows, un antivirus ou pare-feu qui
# inspecte le trafic HTTPS coupe les connexions réutilisées trop souvent
# (erreur WinError 10054) — forcer une connexion neuve à chaque fois évite
# ce problème, au prix d'un tout petit temps de latence supplémentaire.
_REQUEST_HEADERS = {"Connection": "close"}


def send_message(text: str) -> None:
    """Envoie un message texte sur le channel/groupe Telegram configuré.

    Intègre une logique de nouvelle tentative avec attente croissante : en
    envoyant plusieurs messages d'affilée (un par match par exemple), il
    arrive qu'une connexion soit coupée en cours de route (antivirus,
    réseau instable, ou Telegram qui demande de ralentir). On retente
    automatiquement avant d'abandonner, plutôt que de faire planter tout
    le script.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_TOKEN et TELEGRAM_CHAT_ID doivent être définis "
            "en variables d'environnement. Regarde le README.md, section "
            "'Configurer les variables d'environnement'."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, headers=_REQUEST_HEADERS, timeout=10)
        except requests.exceptions.RequestException as exc:
            # Coupure réseau/SSL en cours de route (fréquent sur Windows si
            # plusieurs messages sont envoyés très vite) : on retente, en
            # attendant un peu plus longtemps à chaque nouvel échec.
            last_error = exc
            delay = BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            print(f"  (tentative {attempt}/{MAX_RETRIES} échouée : {exc} — nouvel essai dans {delay}s...)")
            time.sleep(delay)
            continue

        if response.ok:
            time.sleep(THROTTLE_DELAY_SECONDS)  # on ne repart pas immédiatement au message suivant
            return

        if response.status_code == 429:
            # Telegram demande explicitement de ralentir avant de renvoyer
            retry_after = response.json().get("parameters", {}).get("retry_after", BASE_RETRY_DELAY_SECONDS)
            print(f"  (Telegram demande d'attendre {retry_after}s avant de renvoyer...)")
            time.sleep(retry_after)
            continue

        # Autre erreur (token invalide, chat_id incorrect, bot pas admin...) :
        # inutile de retenter, on affiche le détail tout de suite.
        raise RuntimeError(f"Erreur Telegram ({response.status_code}): {response.text}")

    raise RuntimeError(f"Échec de l'envoi Telegram après {MAX_RETRIES} tentatives : {last_error}")


if __name__ == "__main__":
    # Ce bloc ne s'exécute que si tu lances directement "python telegram_bot.py"
    # C'est ton premier test : si tu reçois le message sur Telegram, tout est branché.
    send_message("✅ Le bot MLB est bien connecté à Telegram.")
    print("Message envoyé. Vérifie ton channel/groupe Telegram.")

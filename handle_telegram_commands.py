"""
Vérifie les nouveaux messages Telegram et lance le script correspondant si
une commande reconnue est trouvée. Pensé pour tourner via GitHub Actions
toutes les 5 minutes (voir .github/workflows/telegram-commands.yml) --
mais fonctionne aussi en local si tu veux tester avant de mettre en ligne.

Commandes reconnues :
    /bets     -> lance get_value_bets.py (cotes + value du jour)
    /value    -> pareil que /bets
    /debrief  -> lance debrief.py (bilan de la veille)

Usage :
    python handle_telegram_commands.py
"""

import os
import sys
import json
import subprocess
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATA_DIR = Path(__file__).parent / "data"
OFFSET_PATH = DATA_DIR / "telegram_offset.json"

COMMANDS = {
    "/bets": "get_value_bets.py",
    "/value": "get_value_bets.py",
    "/debrief": "debrief.py",
}


def load_offset() -> int:
    if not OFFSET_PATH.exists():
        return 0
    with open(OFFSET_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("last_update_id", 0)


def save_offset(update_id: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": update_id}, f)


def get_new_messages(offset: int) -> list[dict]:
    """Récupère les messages Telegram reçus depuis le dernier update_id
    traité. offset+1 dit à Telegram "ne renvoie que ce qui est après ça".
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": offset + 1, "timeout": 5}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("result", [])


def is_authorized_chat(message: dict) -> bool:
    """Vérifie que le message vient bien de ton channel/groupe configuré.

    Note expérimentale : si TELEGRAM_CHAT_ID est configuré en "@nom_public"
    plutôt qu'en identifiant numérique, cette vérification ne pourra pas
    matcher directement (Telegram renvoie toujours un ID numérique dans
    les messages reçus, jamais le @nom). Si les commandes ne déclenchent
    jamais rien alors qu'elles devraient, c'est le premier endroit à
    vérifier -- dis-le, on ajustera ensemble.
    """
    if not TELEGRAM_CHAT_ID:
        return True  # pas de vérification possible, on laisse passer

    chat_id = str(message.get("chat", {}).get("id", ""))
    configured = str(TELEGRAM_CHAT_ID)

    if configured.startswith("@"):
        username = message.get("chat", {}).get("username", "")
        return f"@{username}".lower() == configured.lower()

    return chat_id == configured


def run_script(script_name: str) -> None:
    print(f"Commande reconnue -> lancement de {script_name}...")
    result = subprocess.run(
        [sys.executable, script_name],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"⚠️ {script_name} a terminé avec une erreur :\n{result.stderr}")


def main() -> None:
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN manquant dans l'environnement.")
        return

    offset = load_offset()
    updates = get_new_messages(offset)

    if not updates:
        print("Aucun nouveau message.")
        return

    last_update_id = offset
    for update in updates:
        last_update_id = max(last_update_id, update.get("update_id", 0))

        message = update.get("message") or update.get("channel_post")
        if not message:
            continue

        if not is_authorized_chat(message):
            print("Message ignoré (chat non autorisé).")
            continue

        text = (message.get("text") or "").strip().lower()
        script = COMMANDS.get(text)
        if script:
            run_script(script)
        elif text:
            print(f"Message reçu sans commande reconnue : {text!r}")

    save_offset(last_update_id)


if __name__ == "__main__":
    main()

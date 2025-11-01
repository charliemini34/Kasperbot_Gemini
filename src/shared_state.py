
"""
Fichier: src/shared_state.py
Version: SMC (Compatible API v13)

État partagé global pour le bot, accessible par tous les threads.
"""

import threading
import copy

_BOT_RUNNING = True
_CONFIG = {}
_STATUS = {"status": "INITIALIZING", "message": "Bot starting..."}
_LOGS = []
_POSITIONS = []
_SYMBOL_DATA = {}
_lock = threading.Lock()

def is_bot_running():
    """Vérifie si le bot est censé être en cours d'exécution."""
    with _lock:
        return _BOT_RUNNING

def stop_bot():
    """Signale au bot de s'arrêter."""
    global _BOT_RUNNING
    with _lock:
        _BOT_RUNNING = False
    set_status("STOPPED", "Bot stopped by user.")

# --- AJOUTÉ POUR CORRIGER L'ERREUR DE L'API ---
def set_config(config_data):
    """Met à jour la configuration globale."""
    global _CONFIG
    with _lock:
        _CONFIG = config_data

def get_config():
    """Récupère la configuration globale."""
    with _lock:
        # Renvoyer une copie pour éviter les modifications concurrentes
        return copy.deepcopy(_CONFIG)
# --- FIN DE L'AJOUT ---

def set_status(status, message):
    """Met à jour le statut du bot pour l'API."""
    global _STATUS
    with _lock:
        _STATUS = {"status": status, "message": message}

def add_log(log_message):
    """Ajoute un log pour l'API."""
    with _lock:
        _LOGS.append(log_message)
        if len(_LOGS) > 100: # Limiter à 100 logs
            _LOGS.pop(0)

def update_positions(positions_list):
    """Met à jour la liste des positions ouvertes pour l'API."""
    global _POSITIONS
    with _lock:
        _POSITIONS = positions_list

def update_symbol_data(symbol, data):
     """Met à jour les données d'analyse par symbole pour l'API."""
     global _SYMBOL_DATA
     with _lock:
         _SYMBOL_DATA[symbol] = data

def get_all_data():
    """Point d'entrée unique pour l'API pour récupérer toutes les données."""
    with _lock:
        return {
            "status": _STATUS,
            "logs": _LOGS,
            "positions": _POSITIONS,
            "symbol_data": _SYMBOL_DATA
        }
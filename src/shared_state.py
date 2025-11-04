# Fichier: src/shared_state.py
# Version: 3.0
#
# Module pour gérer l'état partagé (shared state) du bot.
# Stocke les informations sur les symboles, les trades et l'état de l'interface
# de manière thread-safe (sécurisée).
# --------------------------------------------------------------------------

import threading
import logging
from typing import Dict, Any, List

# Configuration du Logger
logger = logging.getLogger(__name__)

# Le dictionnaire 'state' contiendra l'état en temps réel de tous les symboles
# pour le monitoring.
state: Dict[str, Dict[str, Any]] = {}

# Le 'trade_log' est une liste simple des trades passés (pour l'historique)
trade_log: List[Dict[str, Any]] = []

# Un verrou (lock) est essentiel pour empêcher les conflits
# si plusieurs threads (ex: API et bot) accèdent à 'state' en même temps.
lock = threading.Lock()

__version__ = "3.0"

# --- NOUVELLES FONCTIONS (V3.0) POUR LE DASHBOARD ---

def initialize_symbols(symbols: List[str]):
    """
    Initialise la structure d'état détaillée pour tous les symboles surveillés.
    Ceci est la structure principale pour le nouveau dashboard.
    """
    with lock:
        for symbol in symbols:
            if symbol not in state:
                state[symbol] = {
                    "symbol": symbol,
                    "last_analysis": {}, # Pour les données existantes (htf_trend, etc.)
                    "checks": {
                        # Critères de notation (5 étoiles)
                        "trend": {"status": "pending", "label": "Tendance HTF"},
                        "zone": {"status": "pending", "label": "Zone OTE/Discount"},
                        "confirmation": {"status": "pending", "label": "Confirmation LTF (CHOCH)"},
                        "session": {"status": "pending", "label": "Session Active (Volatilité)"},
                        "risk_rr": {"status": "pending", "label": "RRR Valide (>= Config)"},
                        
                        # Checks supplémentaires pour le debug
                        "poi": {"status": "pending", "label": "POI (OB/FVG) Détecté"},
                        "risk_sl": {"status": "pending", "label": "SL Sécurisé"},
                    },
                    "active_signal": {
                        "is_valid": False,
                        "rating": 0,
                        "stars": "☆☆☆☆☆",
                        "copy_string": ""
                    }
                }
    logger.info(f"État partagé initialisé pour {len(symbols)} symboles.")

def update_symbol_check(symbol: str, check_name: str, status: str):
    """
    Met à jour le statut d'un check spécifique pour le dashboard.
    (ex: 'pending', 'valid', 'invalid').
    """
    with lock:
        if symbol in state and check_name in state[symbol]["checks"]:
            state[symbol]["checks"][check_name]["status"] = status
        elif symbol not in state:
            logger.warning(f"[shared_state] Tentative de mise à jour de '{check_name}' pour le symbole inconnu '{symbol}'")

def update_symbol_signal(symbol: str, signal_data: Dict[str, Any]):
    """
    Publie ou efface un signal actif pour le dashboard (la grosse boîte).
    """
    with lock:
        if symbol in state:
            state[symbol]["active_signal"].update(signal_data)

def reset_all_checks():
    """
    Réinitialise tous les statuts des checks à 'pending' au début de chaque cycle
    et efface les signaux actifs.
    """
    with lock:
        for symbol in state:
            state[symbol]["active_signal"] = {
                "is_valid": False,
                "rating": 0,
                "stars": "☆☆☆☆☆",
                "copy_string": ""
            }
            for check_name in state[symbol]["checks"]:
                state[symbol]["checks"][check_name]["status"] = "pending"

def get_full_state() -> Dict[str, Dict[str, Any]]:
    """
    Retourne une copie thread-safe de l'état complet pour l'API.
    """
    with lock:
        # Renvoie une copie pour éviter les modifications concurrentes
        return state.copy()

def get_trade_log() -> List[Dict[str, Any]]:
    """
    Retourne une copie thread-safe du journal de trades pour l'API.
    """
    with lock:
        return trade_log.copy()

# --- FONCTIONS EXISTANTES (CONSERVÉES POUR COMPATIBILITÉ V1.X/V2.X) ---

def update_symbol_state(symbol: str, data: Dict[str, Any]):
    """
    Met à jour l'état d'analyse d'un symbole (utilisé par l'orchestrateur).
    Fonction conservée pour la compatibilité.
    """
    with lock:
        if symbol in state:
            # Fusionne les nouvelles données (ex: htf_trend) dans la clé 'last_analysis'
            state[symbol]["last_analysis"].update(data)
        else:
            # Sécurité: si le symbole n'est pas initialisé (ne devrait pas arriver)
            # On le crée à la volée, mais sans la structure complète du dashboard
            state[symbol] = {"symbol": symbol, "last_analysis": data}
            logger.warning(f"[shared_state] Symbole '{symbol}' mis à jour sans initialisation préalable.")

def log_trade(trade_result: Dict[str, Any], trade_params: Dict[str, Any]):
    """
    Enregistre un trade dans le journal de l'état partagé (utilisé par l'orchestrateur).
    Fonction conservée pour la compatibilité.
    
    NOTE: Cette fonction ajoute au log mémoire. L'écriture dans le CSV
    est gérée par le module 'journal' (si vous en avez un).
    """
    try:
        log_entry = {
            "timestamp": trade_result.get('time', 'N/A'),
            "order_id": trade_result.get('order', 'N/A'),
            "symbol": trade_params.get('symbol', 'N/A'),
            "type": trade_params.get('type_str', 'N/A'),
            "volume": trade_params.get('volume', 'N/A'),
            "price": trade_params.get('price', 'N/A'),
            "sl": trade_params.get('sl', 'N/A'),
            "tp": trade_params.get('tp', 'N/A'),
            "reason": trade_params.get('reason', 'N/A'),
            "status": "OPENED" # Statut initial
        }
        with lock:
            trade_log.append(log_entry)
        
        logger.info(f"[shared_state] Trade {log_entry['order_id']} enregistré dans le journal mémoire.")
        
    except Exception as e:
        logger.error(f"[shared_state] Erreur lors de l'enregistrement du trade dans le log: {e}")
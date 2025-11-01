# Fichier: src/shared_state.py
# Version: 20.0.1 (Fix NameError)
# Description: Ajout de 'import time' pour corriger le NameError dans lock_symbol.

import threading
import logging
import copy
import time  # <-- CORRECTION AJOUTÉE ICI
from typing import Dict, List, Any, Optional

# (R7) Contexte pour les trades ouverts
class TradeContext:
    """Stocke l'état original d'un trade pour la gestion (BE, TP Partiel)."""
    def __init__(self, ticket: int, original_sl: float, original_volume: float):
        self.ticket: int = ticket
        self.original_sl: float = original_sl
        self.original_volume: float = original_volume
        self.partial_tp_taken_percent: float = 0.0 # % cumulatif pris

class SharedState:
    """
    Classe centralisée pour gérer l'état partagé du bot à travers les threads.
    (Cette classe est requise par main.py v19.1.1)
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._bot_running = True
        self._config_changed_flag = False
        self._config: Dict[str, Any] = {}
        self._status: Dict[str, Any] = {"status": "INITIALIZING", "message": "Bot starting..."}
        self._logs: List[str] = []
        self._positions: List[Dict[str, Any]] = []
        self._pending_orders: List[Dict[str, Any]] = []
        self._symbol_data: Dict[str, Any] = {}
        self._backtest_status: Dict[str, Any] = {"running": False, "progress": 0, "results": None} # Corrigé _backtest_status
        self._last_deal_check_timestamp: int = 0
        self._symbol_locks: Dict[str, float] = {} # Pour l'idempotency (J.9)

    def is_shutdown(self) -> bool:
        """Vérifie si le bot a reçu un signal d'arrêt."""
        with self._lock:
            return not self._bot_running

    def shutdown(self):
        """Signale au bot de s'arrêter."""
        global _BOT_RUNNING # Doit être _bot_running (variable d'instance)
        with self._lock:
            self._bot_running = False
        self.update_status("STOPPED", "Bot stopped by user.")

    # --- Gestion de la Configuration ---
    
    def update_config(self, config_data: dict):
        """Met à jour la configuration globale."""
        with self._lock:
            self._config = config_data

    def get_config(self) -> dict:
        """Récupère une copie de la configuration globale."""
        with self._lock:
            return copy.deepcopy(self._config)

    def signal_config_changed(self):
        """Signale à la boucle principale qu'elle doit recharger config.yaml."""
        with self._lock:
            self._config_changed_flag = True
    
    @property
    def config_changed_flag(self) -> bool:
        """Vérifie si le drapeau de changement de config est levé."""
        with self._lock:
            return self._config_changed_flag
    
    def clear_config_changed_flag(self):
        """Baisse le drapeau de changement de config après rechargement."""
        with self._lock:
            self._config_changed_flag = False

    # --- Gestion de l'état de l'API ---
    
    def update_status(self, status: str, message: str, is_emergency: bool = False):
        """Met à jour le statut du bot pour l'API."""
        with self._lock:
            self._status = {"status": status, "message": message, "is_emergency": is_emergency}

    def add_log(self, log_message: str):
        """Ajoute un log pour l'API."""
        with self._lock:
            self._logs.append(log_message)
            if len(self._logs) > 100: # Limiter à 100 logs
                self._logs.pop(0)

    def update_positions(self, positions_list: List[Any]):
        """Met à jour la liste des positions ouvertes pour l'API (convertit les objets MT5)."""
        with self._lock:
            self._positions = [self._mt5_pos_to_dict(p) for p in positions_list]

    def update_pending_orders(self, orders_list: List[Any]):
        """Met à jour la liste des ordres en attente pour l'API (convertit les objets MT5)."""
        with self._lock:
            self._pending_orders = [self._mt5_order_to_dict(o) for o in orders_list]

    def initialize_symbol_data(self, symbols: List[str]):
        """Initialise le dictionnaire de données pour les symboles."""
        with self._lock:
            self._symbol_data = {symbol: {"patterns": {}} for symbol in symbols}
            
    def update_symbol_patterns(self, symbol: str, patterns_info: Dict[str, Any]):
        """Met à jour les informations de pattern pour un symbole."""
        with self._lock:
            if symbol not in self._symbol_data:
                self._symbol_data[symbol] = {"patterns": {}}
            self._symbol_data[symbol]["patterns"] = patterns_info

    def get_all_data(self) -> Dict[str, Any]:
        """Point d'entrée unique pour l'API pour récupérer toutes les données."""
        with self._lock:
            # (R7) Inclure les ordres en attente dans la réponse
            return {
                "status": self._status,
                "logs": self._logs,
                "positions": self._positions,
                "pending_orders": self._pending_orders, # (R7)
                "symbol_data": self._symbol_data,
                "analysis_suggestions": [] # Placeholder pour l'apprentissage
            }

    # --- Gestion du Backtest (pour l'API) ---
    def get_backtest_status(self) -> Dict[str, Any]:
        """Récupère l'état du backtest en cours."""
        with self._lock:
            return copy.deepcopy(self._backtest_status) # Corrigé _backtest_status

    def set_backtest_status(self, running: bool, progress: int = 0, results: Optional[dict] = None):
        """Met à jour l'état du backtest."""
        with self._lock:
            self._backtest_status = {"running": running, "progress": progress, "results": results} # Corrigé _backtest_status

    # --- Gestion des Timestamps et Verrous ---
    
    def get_last_deal_check_timestamp(self) -> int:
        """Récupère le timestamp du dernier deal vérifié (J.6)."""
        with self._lock:
            return self._last_deal_check_timestamp

    def set_last_deal_check_timestamp(self, timestamp: int):
        """Met à jour le timestamp du dernier deal vérifié (J.6)."""
        with self._lock:
            self._last_deal_check_timestamp = timestamp

    def lock_symbol(self, symbol: str, ttl_seconds: int):
        """Verrouille un symbole pour éviter les trades en double (J.9)."""
        with self._lock:
            expiry_time = time.time() + ttl_seconds
            self._symbol_locks[symbol] = expiry_time
            logging.info(f"Symbole {symbol} verrouillé pour {ttl_seconds}s.")

    def unlock_symbol(self, symbol: str):
        """Déverrouille manuellement un symbole (ex: ordre annulé)."""
        with self._lock:
            if symbol in self._symbol_locks:
                del self._symbol_locks[symbol]
                logging.info(f"Symbole {symbol} déverrouillé manuellement.")

    def is_symbol_locked(self, symbol: str) -> bool:
        """Vérifie si un symbole est actuellement verrouillé (J.9)."""
        with self._lock:
            if symbol not in self._symbol_locks:
                return False
            
            if time.time() > self._symbol_locks[symbol]:
                # Le verrou a expiré, on le supprime
                del self._symbol_locks[symbol]
                return False
            
            # Le verrou est toujours actif
            return True

    # --- Fonctions utilitaires de conversion (pour l'API) ---
    
    def _mt5_pos_to_dict(self, pos) -> Dict[str, Any]:
        """Convertit un objet MT5 Position en dictionnaire simple."""
        if pos is None: return {}
        return {
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "type": pos.type, # 0 pour BUY, 1 pour SELL
            "volume": pos.volume,
            "price_open": pos.price_open,
            "sl": pos.sl,
            "tp": pos.tp,
            "profit": pos.profit,
            "magic": pos.magic
        }
        
    def _mt5_order_to_dict(self, order) -> Dict[str, Any]:
        """Convertit un objet MT5 Order (pending) en dictionnaire simple."""
        if order is None: return {}
        return {
            "ticket": order.ticket,
            "symbol": order.symbol,
            "type": order.type, # 2=LIMIT_BUY, 3=LIMIT_SELL
            "volume": order.volume_initial,
            "price_open": order.price_open,
            "sl": order.sl,
            "tp": order.tp,
            "magic": order.magic,
            "time_setup": order.time_setup
        }


class LogHandler(logging.Handler):
    """
    Un gestionnaire de logging qui envoie les logs à l'état partagé (SharedState)
    pour affichage dans l'interface utilisateur.
    (Cette classe est requise par main.py v19.1.1)
    """
    def __init__(self, shared_state_instance: SharedState):
        super().__init__()
        self.shared_state = shared_state_instance

    def emit(self, record):
        """Envoie l'enregistrement de log formaté à l'état partagé."""
        log_entry = self.format(record)
        self.shared_state.add_log(log_entry)
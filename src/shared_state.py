# Fichier: src/shared_state.py
# Version: 9.3.0 (R7)
# Dépendances: threading, logging, collections.deque, time, datetime
# Description: Ajout gestion ordres limites (R7) à l'état partagé.

import threading
import logging
import time
from collections import deque
from datetime import datetime

class SharedState:
    """Classe thread-safe pour le partage d'état, v9.3 avec ordres limites."""
    def __init__(self, max_logs=200):
        self.lock = threading.Lock()
        self.status = {
            "status": "Initialisation", 
            "message": "Démarrage...", 
            "is_emergency": False, 
            "symbol_data": {},
            "analysis_suggestions": []
        }
        self.positions = []
        self.pending_orders = [] # (R7) Liste des ordres en attente
        self.logs = deque(maxlen=max_logs)
        self.config = {}
        self.config_changed_flag = False
        self._shutdown = False
        self.backtest_status = {'running': False, 'progress': 0, 'results': None}
        self.symbol_locks = {} 

    def update_status(self, status, message, is_emergency=False):
        with self.lock:
            self.status['status'] = status
            self.status['message'] = message
            self.status['is_emergency'] = is_emergency
            
    def update_analysis_suggestions(self, suggestions: list):
        with self.lock: self.status['analysis_suggestions'] = suggestions

    def update_positions(self, new_positions):
        with self.lock:
            self.positions = [
                {"ticket": p.ticket, "symbol": p.symbol, "type": p.type, "volume": p.volume, "profit": p.profit, "magic": p.magic}
                for p in new_positions
            ]

    # --- R7 : Nouvelle fonction ---
    def update_pending_orders(self, new_pending_orders):
        """Met à jour la liste des ordres en attente pour l'UI."""
        with self.lock:
            self.pending_orders = [
                 {"ticket": o.ticket, "symbol": o.symbol, "type": o.type, "volume": o.volume_initial, "price": o.price_open, "sl": o.sl, "tp": o.tp, "magic": o.magic}
                 for o in new_pending_orders
            ]
    # --- Fin R7 ---
    
    def initialize_symbol_data(self, symbols_list: list):
        with self.lock:
            for symbol in symbols_list:
                if symbol not in self.status['symbol_data']:
                    self.status['symbol_data'][symbol] = {'patterns': {}} 
            current_symbols_in_state = list(self.status['symbol_data'].keys())
            for symbol in current_symbols_in_state:
                if symbol not in symbols_list:
                    try: del self.status['symbol_data'][symbol]
                    except KeyError: pass 

    def update_symbol_patterns(self, symbol: str, new_patterns: dict):
        with self.lock:
            if symbol not in self.status['symbol_data']: self.status['symbol_data'][symbol] = {}
            self.status['symbol_data'][symbol]['patterns'] = new_patterns

    def add_log(self, record):
        with self.lock: self.logs.append(record)

    def update_config(self, new_config):
        with self.lock: self.config = new_config

    def get_all_data(self):
        with self.lock:
            return {
                "status": self.status.copy(),
                "positions": self.positions.copy(),
                "pending_orders": self.pending_orders.copy(), # (R7) Ajouter ordres limites
                "logs": list(self.logs),
            }

    def get_config(self):
        with self.lock: return self.config.copy()
        
    def signal_config_changed(self):
        with self.lock: self.config_changed_flag = True
        
    def clear_config_changed_flag(self):
        with self.lock: self.config_changed_flag = False
        
    def shutdown(self):
        with self.lock: self._shutdown = True
        
    def is_shutdown(self):
        with self.lock: return self._shutdown

    def lock_symbol(self, symbol: str, ttl_seconds: int = 300):
        with self.lock:
            lock_until = time.time() + ttl_seconds
            self.symbol_locks[symbol] = lock_until
            logging.debug(f"Symbole {symbol} verrouillé jusqu'à {datetime.fromtimestamp(lock_until).isoformat()}")

    # --- R7 : Nouvelle fonction ---
    def unlock_symbol(self, symbol: str):
        """Supprime le verrou d'idempotence pour un symbole (ex: après annulation ordre)."""
        with self.lock:
            if symbol in self.symbol_locks:
                try:
                    del self.symbol_locks[symbol]
                    logging.debug(f"Verrou d'idempotence supprimé pour {symbol}.")
                except KeyError:
                    pass # Déjà supprimé, ok
            # else: # Optionnel: log si on tente de déverrouiller un symbole non verrouillé
            #     logging.debug(f"Tentative de déverrouillage de {symbol} non verrouillé.")
    # --- Fin R7 ---
            
    def is_symbol_locked(self, symbol: str) -> bool:
        with self.lock:
            if symbol not in self.symbol_locks: return False
            if time.time() < self.symbol_locks[symbol]: return True
            else:
                try: del self.symbol_locks[symbol]
                except KeyError: pass
                logging.debug(f"Verrou d'idempotence expiré pour {symbol}.")
                return False
        
    # --- Backtest (inchangé) ---
    def start_backtest(self):
        with self.lock: self.backtest_status = {'running': True, 'progress': 0, 'results': None}
    def update_backtest_progress(self, progress):
        with self.lock:
            if self.backtest_status['running']: self.backtest_status['progress'] = progress
    def finish_backtest(self, results):
        with self.lock: self.backtest_status['running'] = False; self.backtest_status['results'] = results
    def get_backtest_status(self):
        with self.lock: return self.backtest_status.copy()

class LogHandler(logging.Handler):
    def __init__(self, shared_state):
        super().__init__()
        self.shared_state = shared_state
    def emit(self, record):
        self.shared_state.add_log(self.format(record))
# Fichier: src/shared_state.py

import threading
import logging
from collections import deque

class SharedState:
    """Classe thread-safe pour le partage d'état, v9.0 avec suggestions d'analyse."""
    def __init__(self, max_logs=200):
        self.lock = threading.Lock()
        self.status = {
            "status": "Initialisation", 
            "message": "Démarrage...", 
            "is_emergency": False, 
            "symbol_data": {},
            "analysis_suggestions": [] # NOUVEAU: pour stocker les suggestions
        }
        self.positions = []
        self.logs = deque(maxlen=max_logs)
        self.config = {}
        self.config_changed_flag = False
        self._shutdown = False
        self.backtest_status = {'running': False, 'progress': 0, 'results': None}

    def update_status(self, status, message, is_emergency=False):
        with self.lock:
            self.status['status'] = status
            self.status['message'] = message
            self.status['is_emergency'] = is_emergency
            
    def update_analysis_suggestions(self, suggestions: list):
        """Met à jour la liste des suggestions d'analyse."""
        with self.lock:
            self.status['analysis_suggestions'] = suggestions

    def update_positions(self, new_positions):
        with self.lock:
            self.positions = [
                {"ticket": p.ticket, "symbol": p.symbol, "type": p.type, "volume": p.volume, "profit": p.profit, "magic": p.magic}
                for p in new_positions
            ]
    
    def update_symbol_patterns(self, symbol: str, new_patterns: dict):
        """Met à jour les informations de pattern pour un symbole spécifique."""
        with self.lock:
            if symbol not in self.status['symbol_data']:
                self.status['symbol_data'][symbol] = {}
            self.status['symbol_data'][symbol]['patterns'] = new_patterns

    def add_log(self, record):
        with self.lock:
            self.logs.append(record)

    def update_config(self, new_config):
        with self.lock:
            self.config = new_config

    def get_all_data(self):
        with self.lock:
            return {
                "status": self.status.copy(),
                "positions": self.positions.copy(),
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
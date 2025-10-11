import threading
from collections import deque
from datetime import datetime
import logging

class LogHandler(logging.Handler):
    """Un gestionnaire de logs qui envoie les enregistrements à l'état partagé."""
    def __init__(self, shared_state):
        super().__init__()
        self.shared_state = shared_state

    def emit(self, record):
        log_entry = self.format(record)
        self.shared_state.add_log(log_entry)

class SharedState:
    """Classe Thread-safe pour partager l'état entre le bot, l'API et les logs."""
    def __init__(self):
        self._lock = threading.Lock()
        self._status = {
            "status": "Initialisation", "message": "Démarrage...", "daily_pnl": 0.0,
            "scores": {}, "is_emergency": False,
        }
        self._positions = []
        self._logs = deque(maxlen=200)
        self._shutdown_flag = False
        
        self._config = {}
        self.config_changed_flag = False
        
        self._backtest_status = {"running": False, "progress": 0, "results": None}

    # --- Gestion du statut ---
    def update_status(self, status, message, is_emergency=None):
        with self._lock:
            self._status['status'] = status
            self._status['message'] = message
            if is_emergency is not None:
                self._status['is_emergency'] = is_emergency

    def update_pnl(self, pnl):
        with self._lock:
            self._status['daily_pnl'] = pnl

    def update_scores(self, scores):
        with self._lock:
            self._status['scores'] = scores

    def update_positions(self, positions):
        with self._lock:
            self._positions = [self._position_to_dict(p) for p in positions]

    def add_log(self, message):
        with self._lock:
            self._logs.append(message)
    
    # --- Gestion de la configuration ---
    def update_config(self, config):
        with self._lock:
            self._config = config
    
    def get_config(self):
        with self._lock:
            return self._config.copy()
            
    def signal_config_changed(self):
        self.config_changed_flag = True
        
    def clear_config_changed_flag(self):
        self.config_changed_flag = False

    # --- Gestion du backtest ---
    def start_backtest(self):
        with self._lock:
            self._backtest_status = {"running": True, "progress": 0, "results": None}

    def update_backtest_progress(self, progress):
        with self._lock:
            if self._backtest_status["running"]:
                self._backtest_status["progress"] = progress

    def finish_backtest(self, results):
        with self._lock:
            self._backtest_status["running"] = False
            self._backtest_status["progress"] = 100
            self._backtest_status["results"] = results
            
    def get_backtest_status(self):
        with self._lock:
            return self._backtest_status.copy()

    # --- Accesseurs pour l'API ---
    def get_all_data(self):
        with self._lock:
            return {
                "status": self._status.copy(),
                "positions": self._positions[:],
                "logs": list(self._logs),
            }
            
    # --- Gestion de l'arrêt ---
    def shutdown(self):
        with self._lock:
            if not self._shutdown_flag:
                self._shutdown_flag = True
                self.add_log("ARRET D'URGENCE ACTIVE PAR L'UTILISATEUR.")
                self.update_status("Arrêt d'urgence", "Signal d'arrêt reçu", is_emergency=True)

    def is_shutdown(self):
        with self._lock:
            return self._shutdown_flag
        
    def _position_to_dict(self, pos):
        return {
            "ticket": pos.ticket, "time": pos.time, "type": pos.type,
            "volume": pos.volume, "price_open": pos.price_open, "sl": pos.sl,
            "tp": pos.tp, "price_current": pos.price_current, "profit": pos.profit,
        }
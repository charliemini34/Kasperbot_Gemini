# Fichier: src/shared_state.py
# Version: 1.1.0 (Implémentation Sugg 10.1)
# Dépendances: threading, logging, typing
# DESCRIPTION: Ajout Sugg 10.1 (Gestion des alertes visuelles).

import threading
import logging
from typing import Dict, Any, List, Optional

class SharedState:
    """
    Gère l'état partagé entre la boucle de trading et l'API Flask.
    v1.1.0: Ajout Sugg 10.1 (Alertes Visuelles).
    """
    def __init__(self):
        self._lock = threading.Lock()
        
        # État général
        self.status: Dict[str, Any] = {"status": "Démarrage", "message": "Initialisation...", "is_emergency": False}
        self.config: Dict[str, Any] = {}
        self._shutdown = False
        self._config_changed = False
        
        # Données de trading
        self.positions: List[Dict[str, Any]] = []
        self.symbol_data: Dict[str, Dict[str, Any]] = {} # { "EURUSD": {"patterns": {...}, ...} }
        self.pnl_history: List[float] = []
        
        # Journalisation UI
        self.logs: List[str] = []
        
        # --- AJOUT SUGGESTION 10.1 ---
        self._visual_alerts: List[str] = []
        # --- FIN SUGGESTION 10.1 ---

    def update_status(self, status: str, message: str, is_emergency: bool = False):
        with self._lock:
            self.status = {"status": status, "message": message, "is_emergency": is_emergency}
    
    def update_config(self, new_config: dict):
        with self._lock:
            self.config = new_config
            
    def update_positions(self, positions_list):
        with self._lock:
            self.positions = [self._format_position(pos) for pos in positions_list]

    def _format_position(self, pos) -> Dict[str, Any]:
        """Convertit un objet Position MT5 en dict pour JSON."""
        if not pos: return {}
        return {
            "ticket": pos.ticket, "symbol": pos.symbol,
            "type": "BUY" if pos.type == 0 else "SELL", # 0: BUY, 1: SELL
            "volume": pos.volume, "price_open": pos.price_open,
            "sl": pos.sl, "tp": pos.tp,
            "profit": pos.profit, "magic": pos.magic,
            "time": pos.time_msc, # Timestamp ms
        }

    def initialize_symbol_data(self, symbols: List[str]):
        with self._lock:
            self.symbol_data = {symbol: {"patterns": {}} for symbol in symbols}
            
    def update_symbol_patterns(self, symbol: str, patterns_info: Dict[str, Any]):
        with self._lock:
            if symbol in self.symbol_data:
                self.symbol_data[symbol]["patterns"] = patterns_info
            else:
                self.symbol_data[symbol] = {"patterns": patterns_info}

    def add_log_message(self, message: str):
        with self._lock:
            self.logs.insert(0, message)
            self.logs = self.logs[:100] # Limiter à 100 entrées

    def get_logs(self) -> List[str]:
        with self._lock:
            return list(self.logs)

    # --- AJOUT SUGGESTION 10.1 ---
    def add_visual_alert(self, message: str):
        with self._lock:
            self._visual_alerts.insert(0, message) # Ajouter au début
            # Limiter à 10 alertes
            self._visual_alerts = self._visual_alerts[:10]

    def get_visual_alerts(self) -> List[str]:
        with self._lock:
            return list(self._visual_alerts)
    # --- FIN SUGGESTION 10.1 ---

    def get_full_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self.status.copy(),
                "config": self.config.copy(), # Ne pas exposer les credentials
                "positions": list(self.positions),
                "symbol_data": self.symbol_data.copy(),
            }

    def shutdown(self):
        with self._lock:
            self._shutdown = True
            
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    def signal_config_changed(self):
        with self._lock:
            self._config_changed = True
            
    @property
    def config_changed_flag(self) -> bool:
        with self._lock:
            return self._config_changed
            
    def clear_config_changed_flag(self):
        with self._lock:
            self._config_changed = False

# Handler de logging pour l'UI
class LogHandler(logging.Handler):
    def __init__(self, state: SharedState):
        super().__init__()
        self.state = state

    def emit(self, record):
        try:
            msg = self.format(record)
            self.state.add_log_message(msg)
        except Exception:
            pass # Ignorer les erreurs d'émission de log
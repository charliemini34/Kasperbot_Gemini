from threading import Lock
from datetime import datetime
from src.constants import MAX_LOG_ENTRIES # MODIFICATION : Ré-importation

class SharedState:
    """
    Classe thread-safe pour partager l'état entre le bot, le serveur API
    et les autres composants.
    """
    
    def __init__(self):
        self.lock = Lock()
        self.monitored_symbols = []
        self.logs = []
        self.account_info = {}
        self.trades = []
        self.positions = []

        # MODIFICATIONS : Ajout des états attendus par server.py v2.1.0
        self.config = {}
        self.status = "Initialisation"
        self.status_message = "Le bot est en cours de démarrage."
        self.is_emergency = False
        self.analysis_suggestions = []
        self.symbol_data = {} # Pour les données d'analyse SMC par symbole
        self.backtest_status = {'running': False, 'progress': 0, 'results': {}}
        self.config_changed_flag = False

    def add_log(self, message):
        """Ajoute un message de log."""
        with self.lock:
            log_entry = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}"
            self.logs.append(log_entry)
            
            # MODIFICATION : Ajout de la limite de logs
            if len(self.logs) > MAX_LOG_ENTRIES:
                self.logs.pop(0)

    def get_logs(self):
        """Retourne une copie de tous les logs actuels."""
        with self.lock:
            return list(self.logs)

    # --- Fonctions pour les symboles, compte, trades, positions (inchangées) ---

    def set_monitored_symbols(self, symbols):
        with self.lock:
            if set(self.monitored_symbols) != set(symbols):
                self.monitored_symbols = list(symbols)

    def get_monitored_symbols(self):
        with self.lock:
            return list(self.monitored_symbols)

    def set_account_info(self, account_info):
        with self.lock:
            self.account_info = account_info

    def get_account_info(self):
        with self.lock:
            return dict(self.account_info)

    def set_trades(self, trades):
        with self.lock:
            self.trades = list(trades)

    def get_trades(self):
        with self.lock:
            return list(self.trades)

    def set_positions(self, positions):
        with self.lock:
            self.positions = list(positions)

    def get_positions(self):
        with self.lock:
            return list(self.positions)

    # --- MODIFICATIONS : Fonctions ajoutées pour server.py v2.1.0 ---

    def set_config(self, config):
        """Stocke la configuration chargée."""
        with self.lock:
            self.config = config

    def get_config(self):
        """Retourne une copie de la configuration (corrige AttributeError)."""
        with self.lock:
            return dict(self.config)

    def update_config(self, new_config):
        """Met à jour la configuration (appelé par l'API)."""
        with self.lock:
            self.config = new_config
            # Ici, vous pourriez aussi sauvegarder dans config.yaml si vous
            # ne le faites pas déjà dans server.py
            
    def signal_config_changed(self):
        """Signale au bot qu'il doit redémarrer (lu par server.py)."""
        with self.lock:
            self.config_changed_flag = True

    def check_config_changed(self):
        """Vérifié par le bot pour savoir s'il doit redémarrer."""
        with self.lock:
            if self.config_changed_flag:
                self.config_changed_flag = False
                return True
            return False

    def get_backtest_status(self):
        """Retourne l'état du backtest (attendu par server.py)."""
        with self.lock:
            return dict(self.backtest_status)

    def set_backtest_status(self, running=None, progress=None, results=None, error=None):
        """Met à jour l'état du backtest (appelé par backtester.py)."""
        with self.lock:
            if running is not None:
                self.backtest_status['running'] = running
            if progress is not None:
                self.backtest_status['progress'] = progress
            if results is not None:
                self.backtest_status['results'] = results
                self.backtest_status['error'] = None # Réinitialise l'erreur en cas de succès
            if error is not None:
                self.backtest_status['error'] = error
                self.backtest_status['running'] = False # Arrête le backtest en cas d'erreur
                
    def set_bot_status(self, status, message, is_emergency=False):
        """Met à jour le statut général du bot pour l'UI."""
        with self.lock:
            self.status = status
            self.status_message = message
            self.is_emergency = is_emergency

    def set_symbol_data(self, symbol, data):
        """Met à jour les données d'analyse pour un symbole spécifique (SMC)."""
        with self.lock:
            if symbol not in self.symbol_data:
                self.symbol_data[symbol] = {}
            # Fusionne les nouvelles données (ex: { 'patterns': {...} })
            self.symbol_data[symbol].update(data)

    def set_analysis_suggestions(self, suggestions):
        """Met à jour les suggestions d'analyse (mode apprentissage)."""
        with self.lock:
            self.analysis_suggestions = list(suggestions)

    def get_all_data(self):
        """
        Rassemble TOUTES les données pour l'endpoint /api/data.
        C'est la fonction CLÉ que l'interface appelle.
        """
        with self.lock:
            return {
                "status": {
                    "status": self.status,
                    "message": self.status_message,
                    "is_emergency": self.is_emergency,
                    "analysis_suggestions": list(self.analysis_suggestions),
                    "symbol_data": dict(self.symbol_data)
                },
                "positions": list(self.positions),
                "logs": list(self.logs)
                # Note : 'trades' (historique) n'est pas demandé par /api/data
                # mais pourrait l'être par un autre endpoint.
            }
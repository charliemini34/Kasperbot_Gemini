
# Fichier: src/data_ingest/mt5_connector.py
# Version: 1.0.1 (Connection-Hardened)
# Dépendances: MetaTrader5, pandas, logging, time
# Description: Connecteur MT5 robuste avec une logique de reconnexion améliorée.

import MetaTrader5 as mt5
import pandas as pd
import logging
import time

class MT5Connector:
    """Gère la connexion et la récupération de données depuis MetaTrader 5."""
    def __init__(self, credentials):
        self._credentials = credentials
        self._connection = mt5
        self.log = logging.getLogger(self.__class__.__name__)

    def check_connection(self):
        """Vérifie si le terminal est toujours accessible."""
        return self._connection.terminal_info() is not None

    def connect(self):
        """Initialise ou réinitialise la connexion au terminal MT5 avec des tentatives de reconnexion."""
        self.log.info("Tentative de connexion à MetaTrader 5...")
        
        for i in range(5):
            # --- MODIFICATION ---
            # S'assure de couper toute connexion précédente avant une nouvelle tentative.
            self._connection.shutdown()
            time.sleep(1)

            if self._connection.initialize(
                login=self._credentials['login'],
                password=self._credentials['password'],
                server=self._credentials['server']
            ):
                self.log.info(f"Connexion à MT5 réussie. Version: {self._connection.version()}")
                return True
            else:
                self.log.error(f"Échec de l'initialisation de MT5 (tentative {i+1}/5): {self._connection.last_error()}")
                time.sleep(i * 2)
        
        return False

    def disconnect(self):
        """Ferme la connexion à MT5."""
        try:
            self._connection.shutdown()
            self.log.info("Déconnecté de MetaTrader 5.")
        except Exception as e:
            self.log.error(f"Erreur lors de la déconnexion de MT5: {e}")


    def get_connection(self):
        """Retourne l'objet de connexion MT5 brut."""
        return self._connection

    def get_ohlc(self, symbol, timeframe_str, num_bars):
        """Récupère les données OHLC historiques de manière sécurisée."""
        try:
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe_str.upper()}")
            rates = self._connection.copy_rates_from_pos(symbol, timeframe, 0, num_bars)
            
            if rates is None or len(rates) == 0:
                self.log.warning(f"Impossible de récupérer les données OHLC pour {symbol} sur {timeframe_str}: {self._connection.last_error()}")
                return None
            
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            return df
            
        except AttributeError:
            self.log.error(f"Le timeframe '{timeframe_str}' est invalide.")
            return None
        except Exception as e:
            self.log.error(f"Erreur inattendue lors de la récupération des données OHLC pour {symbol}: {e}", exc_info=True)
            return None

    def get_tick(self, symbol):
        """Récupère le dernier tick de prix pour un symbole."""
        try:
            tick = self._connection.symbol_info_tick(symbol)
            if tick:
                return tick
            self.log.warning(f"Impossible d'obtenir le tick pour {symbol}: {self._connection.last_error()}")
            return None
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération du tick pour {symbol}: {e}")
            return None
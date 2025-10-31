# Fichier: src/data_ingest/mt5_connector.py
# Version: 2.0.0 (SMC MTF Integration)
# Dépendances: MetaTrader5, pandas, pytz, logging
# Description: Ajout de la méthode get_mtf_data pour l'analyse SMC.

import MetaTrader5 as mt5
import pandas as pd
import pytz
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

class MT5Connector:
    """Gère la connexion et l'extraction de données depuis MetaTrader 5."""

    # Dictionnaire de mapping pour convertir les strings en constantes MT5
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

    def __init__(self, credentials: dict):
        self.log = logging.getLogger(self.__class__.__name__)
        self.login = credentials.get('login')
        self.password = credentials.get('password')
        self.server = credentials.get('server')
        self.connection_status = False

    def connect(self) -> bool:
        """Initialise la connexion à MetaTrader 5."""
        self.log.info("Tentative de connexion à MetaTrader 5...")
        try:
            if not mt5.initialize(login=self.login, password=self.password, server=self.server):
                self.log.error(f"Échec de l'initialisation de MT5, code d'erreur = {mt5.last_error()}")
                self.connection_status = False
                return False
            
            account_info = mt5.account_info()
            if account_info is None:
                self.log.error(f"Échec de la récupération des informations du compte, code d'erreur = {mt5.last_error()}")
                mt5.shutdown()
                self.connection_status = False
                return False
                
            self.log.info(f"Connexion réussie au compte {account_info.name} (Serveur: {account_info.server})")
            self.connection_status = True
            return True
        except Exception as e:
            self.log.error(f"Exception lors de la connexion à MT5: {e}", exc_info=True)
            self.connection_status = False
            return False

    def disconnect(self):
        """Ferme la connexion à MetaTrader 5."""
        self.log.info("Fermeture de la connexion MetaTrader 5.")
        mt5.shutdown()
        self.connection_status = False

    def check_connection(self) -> bool:
        """Vérifie si la connexion est toujours active."""
        if not self.connection_status:
            self.log.warning("Vérification: Connexion déjà marquée comme inactive.")
            return False
        
        # Le simple fait de ping mt5.version() vérifie le socket
        if not mt5.version():
            self.log.error("La connexion à MT5 semble perdue (ping a échoué).")
            self.connection_status = False
            return False
        
        return True

    def get_connection(self):
        """Retourne l'objet mt5 pour un accès direct (par Executor, etc.)."""
        return mt5

    def get_tick(self, symbol: str) -> Optional[mt5.Tick]:
        """Récupère le tick actuel pour un symbole."""
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return tick
            self.log.warning(f"Impossible de récupérer le tick pour {symbol}: {mt5.last_error()}")
            return None
        except Exception as e:
            self.log.error(f"Exception lors de get_tick pour {symbol}: {e}")
            return None

    def get_ohlc(self, symbol: str, timeframe_str: str, num_candles: int, end_date_utc: datetime = None) -> Optional[pd.DataFrame]:
        """
        Récupère les données OHLC historiques pour un symbole et une timeframe.
        """
        timeframe_mt5 = self.TIMEFRAME_MAP.get(timeframe_str.upper())
        if timeframe_mt5 is None:
            self.log.error(f"Timeframe '{timeframe_str}' non reconnue.")
            return None

        self.log.debug(f"Récupération de {num_candles} bougies pour {symbol} en {timeframe_str}...")
        try:
            if end_date_utc:
                # S'assurer que la date est timezone-aware (UTC)
                if end_date_utc.tzinfo is None:
                     end_date_utc = pytz.utc.localize(end_date_utc)
                rates = mt5.copy_rates_from(symbol, timeframe_mt5, end_date_utc, num_candles)
            else:
                rates = mt5.copy_rates_from_pos(symbol, timeframe_mt5, 0, num_candles)
            
            if rates is None:
                self.log.warning(f"Aucune donnée OHLC récupérée pour {symbol} en {timeframe_str}. Code d'erreur = {mt5.last_error()}")
                return None
                
            if len(rates) == 0:
                self.log.warning(f"Données OHLC vides (0 bougies) pour {symbol} en {timeframe_str}.")
                return None

            data = pd.DataFrame(rates)
            data['time'] = pd.to_datetime(data['time'], unit='s')
            data.set_index('time', inplace=True)
            
            self.log.debug(f"Données OHLC pour {symbol} ({timeframe_str}) récupérées avec succès ({len(data)} bougies).")
            return data

        except Exception as e:
            self.log.error(f"Exception lors de la récupération des données OHLC pour {symbol}: {e}", exc_info=True)
            return None

    # --- NOUVELLE MÉTHODE (SMC Integration) ---
    def get_mtf_data(self, symbol: str, timeframes_config: dict) -> Dict[str, Optional[pd.DataFrame]]:
        """
        Récupère les données de marché pour plusieurs timeframes en un seul appel.
        Appelle get_ohlc pour chaque timeframe spécifiée dans la config.

        Args:
            symbol (str): Le symbole à trader (ex: "XAUUSD").
            timeframes_config (dict): Un dictionnaire mappant la timeframe (str) 
                                      au nombre de bougies.
                                      Ex: {'H4': 200, 'M15': 300}

        Returns:
            dict: Un dictionnaire où les clés sont les timeframes (str) et 
                  les valeurs sont les DataFrames de données.
                  Ex: {'H4': pd.DataFrame(...), 'M15': pd.DataFrame(...)}
        """
        mtf_data = {}
        self.log.info(f"Récupération des données multi-timeframe pour {symbol}...")

        for tf_str, num_candles in timeframes_config.items():
            if tf_str not in self.TIMEFRAME_MAP:
                self.log.warning(f"Timeframe '{tf_str}' non reconnue dans mtf_data_config. Elle sera ignorée.")
                continue
            
            data = self.get_ohlc(symbol, tf_str, num_candles)
            
            if data is not None and not data.empty:
                mtf_data[tf_str] = data
                self.log.debug(f"Données pour {symbol} ({tf_str}) ({len(data)} bougies) récupérées.")
            else:
                self.log.error(f"Échec de la récupération des données MTF pour {symbol} ({tf_str}).")
                mtf_data[tf_str] = None # Important de le marquer comme None

        return mtf_data
    # --- FIN DE LA NOUVELLE MÉTHODE ---
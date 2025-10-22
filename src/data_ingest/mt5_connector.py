
# Fichier: src/data_ingest/mt5_connector.py
# Version: 14.1.0 (HTF-Data-Added) # <-- Version mise à jour
# Dépendances: MetaTrader5, pandas, logging, time, datetime, pytz
# Description: Ajout de get_ohlc_range pour récupérer des données HTF.

import MetaTrader5 as mt5
import pandas as pd
import logging
import time
from datetime import datetime
import pytz # Importé pour la gestion des fuseaux horaires

class MT5Connector:
    """Gère la connexion et la récupération de données depuis MetaTrader 5."""
    def __init__(self, credentials):
        self._credentials = credentials
        self._connection = mt5
        self.log = logging.getLogger(self.__class__.__name__)

    def check_connection(self):
        """Vérifie si le terminal est toujours accessible."""
        # Tente une opération simple pour valider la connexion
        try:
            # terminal_info peut parfois retourner True même si déconnecté, utiliser account_info
            info = self._connection.account_info()
            return info is not None
        except Exception:
            return False


    def connect(self):
        """Initialise ou réinitialise la connexion au terminal MT5 avec des tentatives de reconnexion."""
        self.log.info("Tentative de connexion à MetaTrader 5...")

        for i in range(5):
            try: # Ajouter un try/except autour de shutdown
                 self._connection.shutdown()
                 time.sleep(0.5) # Petite pause après shutdown
            except Exception:
                 pass # Ignorer si déjà déconnecté ou erreur de shutdown

            if self._connection.initialize(
                login=self._credentials['login'],
                password=self._credentials['password'],
                server=self._credentials['server']
            ):
                term_info = self._connection.terminal_info()
                acc_info = self._connection.account_info()
                if term_info and acc_info: # Double vérification
                    self.log.info(f"Connexion à MT5 réussie. Compte: {acc_info.login}, Serveur: {acc_info.server}, Version MT5: {self._connection.version()}")
                    return True
                else:
                     self.log.error(f"Initialisation MT5 réussie mais impossible de récupérer les infos terminal/compte (tentative {i+1}/5).")
                     self._connection.shutdown() # Assurer la déconnexion
                     time.sleep(i * 2 + 1)
            else:
                self.log.error(f"Échec de l'initialisation de MT5 (tentative {i+1}/5): {self._connection.last_error()}")
                time.sleep(i * 2 + 1) # Augmenter l'attente

        self.log.critical("Échec de la connexion à MT5 après plusieurs tentatives.")
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

    def get_ohlc(self, symbol: str, timeframe_str: str, num_bars: int) -> pd.DataFrame | None:
        """Récupère les N dernières barres OHLC."""
        try:
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe_str.upper()}")
            # Utiliser copy_rates_from_pos pour obtenir les N dernières barres
            rates = self._connection.copy_rates_from_pos(symbol, timeframe, 0, num_bars)

            if rates is None or len(rates) < num_bars: # Vérifier si on a bien reçu le nombre demandé
                # Log moins sévère si simplement pas assez de données disponibles
                log_func = self.log.warning if rates is not None else self.log.error
                log_func(f"Données OHLC insuffisantes ou indisponibles pour {symbol} sur {timeframe_str}. Reçu {len(rates) if rates is not None else 0}/{num_bars} barres. Erreur MT5: {self._connection.last_error()}")
                return None

            df = pd.DataFrame(rates)
            # Convertir en DatetimeIndex UTC directement
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
            df.set_index('time', inplace=True)
            return df

        except AttributeError:
            self.log.error(f"Le timeframe '{timeframe_str}' est invalide.")
            return None
        except Exception as e:
            self.log.error(f"Erreur inattendue lors de la récupération des données OHLC (get_ohlc) pour {symbol}: {e}", exc_info=True)
            return None

    # --- NOUVELLE FONCTION ---
    def get_ohlc_range(self, symbol: str, timeframe_str: str, start_time_utc: datetime, end_time_utc: datetime) -> pd.DataFrame | None:
        """Récupère les données OHLC dans un intervalle de temps UTC."""
        if start_time_utc.tzinfo is None or end_time_utc.tzinfo is None:
             self.log.error("get_ohlc_range requiert des datetimes UTC avec fuseau horaire.")
             return None
        try:
            timeframe = getattr(mt5, f"TIMEFRAME_{timeframe_str.upper()}")
            # Utiliser copy_rates_range pour obtenir les données dans l'intervalle
            rates = self._connection.copy_rates_range(symbol, timeframe, start_time_utc, end_time_utc)

            if rates is None: # Ne pas vérifier len() ici, 0 barre est possible
                self.log.warning(f"Impossible de récupérer les données OHLC pour {symbol} sur {timeframe_str} entre {start_time_utc} et {end_time_utc}. Erreur MT5: {self._connection.last_error()}")
                return None
            if len(rates) == 0:
                 # self.log.debug(f"Aucune donnée OHLC trouvée pour {symbol} sur {timeframe_str} dans l'intervalle demandé.")
                 return pd.DataFrame() # Retourner un DF vide

            df = pd.DataFrame(rates)
            # Convertir en DatetimeIndex UTC directement
            df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
            df.set_index('time', inplace=True)
            return df

        except AttributeError:
            self.log.error(f"Le timeframe '{timeframe_str}' est invalide pour get_ohlc_range.")
            return None
        except Exception as e:
            self.log.error(f"Erreur inattendue lors de la récupération des données OHLC (get_ohlc_range) pour {symbol}: {e}", exc_info=True)
            return None

    def get_tick(self, symbol: str) -> mt5.Tick | None:
        """Récupère le dernier tick de prix pour un symbole."""
        try:
            tick = self._connection.symbol_info_tick(symbol)
            if tick and tick.time > 0: # Vérifier si le tick est valide
                return tick
            # Log moins sévère si tick invalide
            self.log.warning(f"Tick invalide ou indisponible pour {symbol}. Dernier tick reçu: {tick}. Erreur MT5: {self._connection.last_error()}")
            return None
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération du tick pour {symbol}: {e}", exc_info=False) # Pas besoin de trace complète souvent
            return None
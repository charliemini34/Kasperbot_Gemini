"""
Module pour la connexion et l'extraction de données depuis MetaTrader 5.

Ce module gère la connexion, la déconnexion, et la récupération
des données de marché (bougies) ainsi que la vérification des positions ouvertes.
"""

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import time
import logging

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def connect(login, password, server):
    """
    Initialise la connexion à MetaTrader 5.
    
    Args:
        login (int): Numéro de compte MT5.
        password (str): Mot de passe du compte MT5.
        server (str): Nom du serveur du courtier.

    Returns:
        bool: True si la connexion est réussie, False sinon.
    """
    logger.info("Tentative de connexion à MetaTrader 5...")
    if not mt5.initialize(login=login, password=password, server=server):
        logger.error(f"Échec de l'initialisation de MT5, code d'erreur = {mt5.last_error()}")
        return False
    
    account_info = mt5.account_info()
    if account_info is None:
        logger.error(f"Échec de la récupération des informations du compte, code d'erreur = {mt5.last_error()}")
        mt5.shutdown()
        return False
        
    logger.info(f"Connexion réussie au compte {account_info.name} (Serveur: {account_info.server})")
    return True

def disconnect():
    """Ferme la connexion à MetaTrader 5."""
    logger.info("Fermeture de la connexion MetaTrader 5.")
    mt5.shutdown()

def get_data(symbol, timeframe, num_candles):
    """
    Récupère les données de marché (bougies) pour un symbole et une timeframe donnés.

    Args:
        symbol (str): Le symbole à trader (ex: "EURUSD").
        timeframe (int): La constante de timeframe MT5 (ex: mt5.TIMEFRAME_M15).
        num_candles (int): Le nombre de bougies à récupérer.

    Returns:
        pd.DataFrame: Un DataFrame pandas avec les données (time, open, high, low, close, tick_volume),
                      ou None si la récupération échoue.
    """
    logger.debug(f"Récupération de {num_candles} bougies pour {symbol} en {timeframe}...")
    try:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, num_candles)
        
        if rates is None:
            logger.warning(f"Aucune donnée récupérée pour {symbol} en {timeframe}. Code d'erreur = {mt5.last_error()}")
            return None
            
        if len(rates) == 0:
            logger.warning(f"Données vides (0 bougies) pour {symbol} en {timeframe}.")
            return None

        # Conversion en DataFrame pandas pour une manipulation facile
        data = pd.DataFrame(rates)
        data['time'] = pd.to_datetime(data['time'], unit='s')
        data.set_index('time', inplace=True)
        
        logger.debug(f"Données récupérées avec succès pour {symbol}. Dernier prix 'close' : {data['close'].iloc[-1]}")
        return data

    except Exception as e:
        logger.error(f"Exception lors de la récupération des données pour {symbol}: {e}")
        return None

# --- NOUVELLE FONCTION AJOUTÉE ---
def get_mtf_data(symbol: str, timeframes_config: dict):
    """
    Récupère les données de marché pour plusieurs timeframes en un seul appel.

    Args:
        symbol (str): Le symbole à trader (ex: "EURUSD").
        timeframes_config (dict): Un dictionnaire mappant la timeframe (str) 
                                  au nombre de bougies.
                                  Ex: {'H4': 100, 'M15': 200, 'M1': 100}

    Returns:
        dict: Un dictionnaire où les clés sont les timeframes (str) et 
              les valeurs sont les DataFrames de données.
              Ex: {'H4': pd.DataFrame(...), 'M15': pd.DataFrame(...)}
    """
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

    mtf_data = {}
    logger.info(f"Récupération des données multi-timeframe pour {symbol}...")

    for tf_str, num_candles in timeframes_config.items():
        if tf_str not in TIMEFRAME_MAP:
            logger.warning(f"Timeframe '{tf_str}' non reconnue. Elle sera ignorée.")
            continue
        
        timeframe_mt5 = TIMEFRAME_MAP[tf_str]
        data = get_data(symbol, timeframe_mt5, num_candles)
        
        if data is not None:
            mtf_data[tf_str] = data
            logger.info(f"Données pour {tf_str} ({len(data)} bougies) récupérées.")
        else:
            logger.error(f"Échec de la récupération des données pour {tf_str}.")
            # Si une timeframe cruciale échoue, nous devrions peut-être arrêter.
            # Pour l'instant, nous continuons et retournons ce que nous avons.
            mtf_data[tf_str] = None

    return mtf_data
# --- FIN DE LA NOUVELLE FONCTION ---

def check_open_positions(symbol):
    """
    Vérifie s'il y a des positions ouvertes pour un symbole spécifique.

    Args:
        symbol (str): Le symbole à vérifier.

    Returns:
        int: Le nombre de positions ouvertes pour ce symbole.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        logger.error(f"Échec de la récupération des positions pour {symbol}. Code d'erreur = {mt5.last_error()}")
        # En cas d'erreur, on suppose qu'il n'y a pas de position pour éviter les doublons
        return 0
    
    return len(positions)
# Fichier: src/data_ingest/mt5_connector.py
"""
Module de Connexion et d'Ingestion de Données MetaTrader 5 (MT5).

Ce module gère toutes les interactions directes avec l'API MT5
pour la connexion, la récupération de données de marché et la
vérification des positions.

Version: 1.1.0 (Ajout de get_mt5_timeframe)
"""

__version__ = "1.1.0"

import MetaTrader5 as mt5
import pandas as pd
import time
import logging
from typing import Dict, Optional, List, Any

# --- AJOUT v1.1.0: Importation de la carte des timeframes ---
from src.constants import TIMEFRAME_MAP
# --- FIN AJOUT ---

logger = logging.getLogger(__name__)

# Variable globale pour maintenir l'état de la connexion
_is_initialized = False

def connect(login, password, server):
    """
    Initialise la connexion à MetaTrader 5.
    """
    global _is_initialized
    if _is_initialized:
        logger.info("Connexion MT5 déjà établie.")
        return True
    
    logger.info("Tentative de connexion à MetaTrader 5...")
    if not mt5.initialize():
        logger.critical(f"Échec de l'initialisation de MT5. Erreur: {mt5.last_error()}")
        return False

    authorized = mt5.login(login=login, password=password, server=server)
    if authorized:
        account_info = mt5.account_info()
        if account_info:
            logger.info(f"Connexion réussie au compte {account_info.name} (Serveur: {account_info.server})")
            _is_initialized = True
            return True
        else:
            logger.error(f"Connexion réussie mais impossible de récupérer les informations du compte. Erreur: {mt5.last_error()}")
            return False
    else:
        logger.critical(f"Échec de la connexion. Vérifiez les identifiants. Erreur: {mt5.last_error()}")
        return False

def disconnect():
    """Ferme la connexion à MetaTrader 5."""
    global _is_initialized
    if _is_initialized:
        mt5.shutdown()
        logger.info("Connexion MT5 fermée.")
        _is_initialized = False

def ensure_symbol(symbol: str) -> bool:
    """
    Vérifie si un symbole est disponible et visible dans MT5.
    Tente de l'activer s'il ne l'est pas.
    """
    if not _is_initialized:
        logger.error("MT5 non initialisé. Impossible de vérifier le symbole.")
        return False
        
    symbol_info = mt5.symbol_info(symbol)
    
    if symbol_info is None:
        logger.warning(f"Symbole {symbol} non trouvé (non valide ou non listé par le broker).")
        return False
        
    if not symbol_info.visible:
        logger.info(f"Symbole {symbol} non visible. Tentative d'activation...")
        if not mt5.symbol_select(symbol, True):
            logger.error(f"Échec de l'activation du symbole {symbol}. Erreur: {mt5.last_error()}")
            return False
        else:
            logger.info(f"Symbole {symbol} activé avec succès.")
            time.sleep(0.5) # Laisser le temps à MT5 de s'activer
            
    return True

def check_open_positions(symbol: str) -> int:
    """
    Vérifie s'il y a des positions ouvertes pour un symbole spécifique.
    Retourne le nombre de positions ouvertes pour ce symbole.
    """
    if not _is_initialized:
        logger.error("MT5 non initialisé. Impossible de vérifier les positions.")
        return 0
        
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        logger.error(f"Échec de la récupération des positions pour {symbol}. Erreur: {mt5.last_error()}")
        return 0
    
    return len(positions)

def get_mtf_data(symbol: str, timeframes_config: Dict[str, Dict]) -> Optional[Dict[str, pd.DataFrame]]:
    """
    Récupère les données Multi-Timeframe (MTF) basées sur la configuration.
    
    Args:
        symbol (str): Le symbole (ex: "XAUUSD").
        timeframes_config (dict): La configuration (ex: {'H4': {'count': 200}, ...}).

    Returns:
        Un dictionnaire de DataFrames, ou None si une erreur se produit.
    """
    if not _is_initialized:
        logger.error("MT5 non initialisé. Impossible de récupérer les données.")
        return None
    
    mtf_data = {}
    
    for tf_str, params in timeframes_config.items():
        count = params.get('count', 100)
        
        # --- CORRECTION (v1.1.0): Utilisation de la nouvelle fonction ---
        timeframe = get_mt5_timeframe(tf_str)
        # --- FIN CORRECTION ---
        
        if timeframe is None:
            logger.warning(f"Timeframe '{tf_str}' non reconnue. Ignorée.")
            continue
            
        data = get_market_data(symbol, timeframe, count)
        if data is None:
            logger.error(f"Échec de la récupération des données pour {symbol} sur {tf_str}.")
            return None # Si une timeframe échoue, on arrête
            
        mtf_data[tf_str] = data
        
    return mtf_data

def get_market_data(symbol: str, timeframe: Any, count: int) -> Optional[pd.DataFrame]:
    """
    Récupère les données OHLC brutes pour un symbole et une timeframe.
    """
    if not _is_initialized:
        logger.error("MT5 non initialisé.")
        return None
    
    try:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        
        if rates is None:
            logger.error(f"Échec de copy_rates_from_pos pour {symbol}/{timeframe}. Erreur: {mt5.last_error()}")
            return None

        if len(rates) == 0:
            logger.warning(f"Aucune donnée retournée for {symbol}/{timeframe} (count={count}).")
            return pd.DataFrame() # Retourner un DF vide

        data = pd.DataFrame(rates)
        data['time'] = pd.to_datetime(data['time'], unit='s')
        data.set_index('time', inplace=True)
        # Renommer les colonnes pour la compatibilité (lowercase)
        data.rename(columns={
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'tick_volume': 'volume'
        }, inplace=True)
        
        logger.info(f"Données pour {symbol} ({TIMEFRAME_MAP.get(timeframe, timeframe)}) ({len(data)} bougies) récupérées.")
        return data

    except Exception as e:
        logger.error(f"Erreur lors de la récupération des données {symbol}/{timeframe}: {e}", exc_info=True)
        return None


# --- NOUVELLE FONCTION v1.1.0 (Corrige l'AttributeError) ---
def get_mt5_timeframe(timeframe_str: str) -> Optional[int]:
    """
    Convertit une chaîne de caractères (ex: "H4") en constante MT5 (ex: mt5.TIMEFRAME_H4).
    Utilise la TIMEFRAME_MAP de src.constants.
    """
    if not isinstance(timeframe_str, str):
        logger.warning(f"La timeframe fournie n'est pas une chaîne de caractères: {timeframe_str}")
        return None
        
    tf_upper = timeframe_str.upper()
    
    if tf_upper in TIMEFRAME_MAP:
        return TIMEFRAME_MAP[tf_upper]
    else:
        logger.error(f"Timeframe '{timeframe_str}' non reconnue dans TIMEFRAME_MAP.")
        return None
# --- FIN NOUVELLE FONCTION ---
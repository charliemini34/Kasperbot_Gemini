"""
Fichier: src/execution/mt5_executor.py
Version: 3.0.0

Module pour l'exécution des ordres MT5.

Ce module gère :
- L'initialisation avec la connexion MT5.
- Le placement d'ordres (Achat/Vente) avec SL/TP.
- La modification d'ordres (mise à jour SL/TP pour BE et TSL).
"""

__version__ = "3.0.0"

import MetaTrader5 as mt5
import logging
import time

logger = logging.getLogger(__name__)

# Variable globale pour stocker la connexion MT5
_mt5_connector = None

def initialize_executor(connector):
    """
    Initialise l'exécuteur d'ordres avec le connecteur MT5.
    """
    global _mt5_connector
    _mt5_connector = connector
    if _mt5_connector:
        logger.info("Executor initialisé avec le connecteur MT5.")
    else:
        logger.error("Échec de l'initialisation de l'Executor : connecteur non valide.")


def place_order(symbol, order_type, volume, sl_price, tp_price, comment="Kasperbot SMC Entry"):
    """
    Place un ordre de marché (BUY ou SELL) avec SL et TP.
    """
    if not _mt5_connector:
        logger.error("Impossible de passer l'ordre : Executor non initialisé.")
        return None

    # Récupérer les infos du symbole pour l'arrondi ET le type de remplissage
    symbol_info = _mt5_connector.mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error(f"Impossible de récupérer les infos du symbole {symbol} pour l'arrondi.")
        return None
        
    digits = symbol_info.digits

    # Conversion du string 'BUY'/'SELL' en variable 'mt5_order_type'
    if order_type.upper() == "BUY":
        mt5_order_type = mt5.ORDER_TYPE_BUY
        price = _mt5_connector.mt5.symbol_info_tick(symbol).ask
    elif order_type.upper() == "SELL":
        mt5_order_type = mt5.ORDER_TYPE_SELL
        price = _mt5_connector.mt5.symbol_info_tick(symbol).bid
    else:
        logger.error(f"Type d'ordre non reconnu : {order_type}")
        return None
        
    # ARRONDIR le SL et le TP en utilisant le nombre de décimales ('digits') du symbole
    sl = round(float(sl_price), digits) if sl_price is not None and sl_price > 0 else 0.0
    tp = round(float(tp_price), digits) if tp_price is not None and tp_price > 0 else 0.0
    
    # --- MODIFICATION (Version 2.0.13) ---
    # Implémentation de la logique de remplissage dynamique
    
    filling_type = mt5.ORDER_FILLING_IOC # Défaut = 1
    
    # Vérifie les modes de remplissage autorisés par le courtier pour ce symbole
    filling_modes = symbol_info.filling_mode
    
    if filling_modes & mt5.ORDER_FILLING_FOK:
        filling_type = mt5.ORDER_FILLING_FOK # (Valeur 0)
    elif filling_modes & mt5.ORDER_FILLING_IOC:
         filling_type = mt5.ORDER_FILLING_IOC # (Valeur 1)
    # Si aucun des deux n'est explicitement listé, on garde IOC par défaut
    
    # --- FIN MODIFICATION ---

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5_order_type, # Utilise l'entier (0 ou 1)
        "price": float(price), 
        "sl": float(sl),       # Utilise le prix arrondi dynamiquement
        "tp": float(tp),       # Utilise le prix arrondi dynamiquement
        "deviation": 20, 
        "magic": 13579,
        "comment": comment, # Utilise le commentaire dynamique (de main.py)
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type, # Utilise le type de remplissage dynamique
    }

    try:
        # Log de la requête COMPLÈTE avant l'envoi
        logger.info(f"Envoi de la requête MT5 : {request}")
        
        # S'assurer que le symbole est visible
        if not _mt5_connector.mt5.symbol_select(symbol, True):
            logger.warning(f"Symbole {symbol} non visible, tentative d'activation...")
            time.sleep(0.5)

        result = _mt5_connector.mt5.order_send(request)
        
        if result is None:
            logger.error(f"order_send() a échoué. Code d'erreur MT5 : {_mt5_connector.mt5.last_error()}")
            return None
            
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Ordre placé avec succès. Ticket: {result.order}")
            return result.order
        else:
            logger.error(f"Échec de l'ordre : retcode={result.retcode}, comment={result.comment}")
            logger.error(f"Détails de l'erreur MT5 : {_mt5_connector.mt5.last_error()}")
            return None
            
    except Exception as e:
        logger.critical(f"Exception lors de l'envoi de l'ordre : {e}", exc_info=True)
        return None

# --- NOUVELLE FONCTION (V3.0.0) ---
def modify_position_sl(position_id, symbol, new_sl_price):
    """
    Modifie le Stop Loss d'une position ouverte.
    Utilisé pour le Break-Even et le Trailing Stop.
    """
    if not _mt5_connector:
        logger.error(f"Impossible de modifier {position_id} : Executor non initialisé.")
        return False

    # Récupérer les infos du symbole pour l'arrondi
    symbol_info = _mt5_connector.mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error(f"Impossible de récupérer les infos {symbol} pour l'arrondi (Modify SL).")
        return False
        
    digits = symbol_info.digits
    
    # Arrondir le nouveau SL
    new_sl = round(float(new_sl_price), digits)

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position_id,
        "sl": new_sl,
        # "tp" n'est pas inclus, donc il n'est pas modifié
    }

    try:
        logger.info(f"Tentative de modification SL pour {position_id}: Nouveau SL = {new_sl}")
        result = _mt5_connector.mt5.order_send(request)
        
        if result is None:
            logger.error(f"order_send(Modify SL) a échoué. Code MT5: {mt5.last_error()}")
            return False
            
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"SL modifié avec succès pour {position_id}. Nouveau SL: {result.sl}")
            return True
        else:
            logger.error(f"Échec modification SL pour {position_id}: retcode={result.retcode}, comment={result.comment}")
            logger.error(f"Détails de l'erreur MT5 : {mt5.last_error()}")
            return False
            
    except Exception as e:
        logger.critical(f"Exception lors de la modification du SL pour {position_id}: {e}", exc_info=True)
        return False
# --- FIN NOUVELLE FONCTION ---

def get_last_entry_price(order_id):
    """
    Tente de trouver le prix d'entrée réel d'un ordre qui vient d'être exécuté.
    """
    if not _mt5_connector:
        return 0.0

    try:
        # Tenter de récupérer par l'historique des ordres
        history_order = _mt5_connector.mt5.history_orders_get(ticket=order_id)
        if history_order and len(history_order) > 0:
            return history_order[0].price_current

        # Tenter de récupérer par les positions (si elle est déjà ouverte)
        position = _mt5_connector.mt5.positions_get(ticket=order_id)
        if position and len(position) > 0:
            return position[0].price_open
            
        logger.warning(f"Impossible de trouver le prix d'entrée pour l'ordre {order_id} immédiatement.")
        return 0.0

    except Exception as e:
        logger.error(f"Erreur get_last_entry_price: {e}")
        return 0.0
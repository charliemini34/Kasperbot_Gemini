
"""
Module pour l'exécution des ordres MT5 (Version compatible SMC procédurale).

Ce module gère :
- L'initialisation avec la connexion MT5.
- Le placement d'ordres (Achat/Vente) avec SL/TP.
"""

import MetaTrader5 as mt5
import logging
import time

logger = logging.getLogger(__name__)

# Variable globale pour stocker la connexion MT5 (simplifié)
_mt5_connector = None

def initialize_executor(connector):
    """
    Initialise l'exécuteur d'ordres avec le connecteur MT5.
    (C'est la fonction que main.py essaye d'appeler)
    """
    global _mt5_connector
    _mt5_connector = connector
    if _mt5_connector:
        logger.info("Executor initialisé avec le connecteur MT5.")
    else:
        logger.error("Échec de l'initialisation de l'Executor : connecteur non valide.")

def place_order(symbol, order_type, volume, sl_price, tp_price):
    """
    Place un ordre de marché (BUY ou SELL) avec SL et TP.
    """
    if not _mt5_connector:
        logger.error("Impossible de passer l'ordre : Executor non initialisé.")
        return None

    # Mapping du type d'ordre
    if order_type.upper() == "BUY":
        mt5_order_type = mt5.ORDER_TYPE_BUY
        price = _mt5_connector.mt5.symbol_info_tick(symbol).ask
    elif order_type.upper() == "SELL":
        mt5_order_type = mt5.ORDER_TYPE_SELL
        price = _mt5_connector.mt5.symbol_info_tick(symbol).bid
    else:
        logger.error(f"Type d'ordre non reconnu : {order_type}")
        return None
        
    # S'assurer que les SL/TP ne sont pas nuls (MT5 n'aime pas 0.0)
    sl = float(sl_price) if sl_price is not None and sl_price > 0 else 0.0
    tp = float(tp_price) if tp_price is not None and tp_price > 0 else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5_order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20, # 20 points de déviation max
        "magic": 13579, # (Devrait être dans config)
        "comment": "Kasperbot SMC Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC, # ou FOK
    }

    try:
        logger.info(f"Envoi de l'ordre : {request['type']} {request['symbol']} {request['volume']} @ {request['price']} SL={request['sl']} TP={request['tp']}")
        
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
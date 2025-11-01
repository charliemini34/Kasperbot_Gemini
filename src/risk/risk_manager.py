
"""
Module pour la gestion des risques (Version compatible SMC procédurale).

Ce module fournit des fonctions pour :
- Initialiser le module avec une connexion MT5.
- Calculer la taille de lot basée sur le risque en pourcentage et le prix du Stop Loss.
"""

import MetaTrader5 as mt5
import logging

logger = logging.getLogger(__name__)

# Variable globale pour stocker la connexion MT5 (simplifié)
_mt5_connector = None

def initialize_risk_manager(connector):
    """
    Initialise le gestionnaire de risques avec le connecteur MT5.
    (C'est la fonction que main.py essaye d'appeler)
    """
    global _mt5_connector
    _mt5_connector = connector
    if _mt5_connector:
        logger.info("Risk Manager initialisé avec le connecteur MT5.")
    else:
        logger.error("Échec de l'initialisation du Risk Manager : connecteur non valide.")

def get_account_balance():
    """Récupère la balance ou l'équité du compte."""
    if not _mt5_connector:
        logger.error("Risk Manager non initialisé.")
        return None
        
    try:
        account_info = _mt5_connector.mt5.account_info()
        if account_info:
            # Utiliser l'équité (equity) est plus prudent que la balance
            return account_info.equity
        else:
            logger.error(f"Échec de la récupération des infos du compte: {mt5.last_error()}")
            return None
    except Exception as e:
        logger.error(f"Erreur lors de la récupération de la balance du compte: {e}")
        return None

def get_symbol_tick(symbol):
    """Récupère le tick (prix) actuel pour un symbole."""
    if not _mt5_connector:
        logger.error("Risk Manager non initialisé.")
        return None
    try:
        tick = _mt5_connector.mt5.symbol_info_tick(symbol)
        if tick:
            return tick
        else:
            logger.error(f"Échec de la récupération du tick pour {symbol}: {mt5.last_error()}")
            return None
    except Exception as e:
        logger.error(f"Erreur lors de la récupération du tick pour {symbol}: {e}")
        return None

def calculate_lot_size(risk_percent, sl_price, symbol=None):
    """
    Calcule la taille de lot en fonction du risque en pourcentage et du SL.
    (C'est la fonction que main.py essaye d'appeler)
    """
    if not _mt5_connector:
        logger.error("Impossible de calculer le lot : Risk Manager non initialisé.")
        return None
    
    if symbol is None:
        # Si aucun symbole n'est fourni, essayez de le deviner (mauvaise pratique, mais pour la compatibilité)
        # Dans un vrai scénario, main.py devrait le fournir.
        logger.warning("Calcul de lot sans symbole explicite. (Ceci devrait être corrigé)")
        return None # Correction : Nous devons savoir le symbole.
        
    # --- Récupération des informations ---
    account_balance = get_account_balance()
    if account_balance is None or account_balance <= 0:
        logger.error("Balance du compte invalide ou nulle.")
        return None
    
    tick = get_symbol_tick(symbol)
    if tick is None:
        return None
        
    symbol_info = _mt5_connector.mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error(f"Échec de la récupération des infos pour {symbol}: {mt5.last_error()}")
        return None

    # --- Paramètres ---
    contract_size = symbol_info.trade_contract_size
    volume_step = symbol_info.volume_step
    volume_min = symbol_info.volume_min
    account_currency = _mt5_connector.mt5.account_info().currency
    symbol_currency_profit = symbol_info.currency_profit

    # --- Calcul ---
    risk_amount = account_balance * (risk_percent / 100.0)
    
    # Déterminer le prix d'entrée (approximatif pour le calcul)
    # Pour un achat (sl < prix), entrée = ask. Pour une vente (sl > prix), entrée = bid.
    if sl_price < tick.bid: # Achat probable
        entry_price = tick.ask
        sl_points = entry_price - sl_price
    else: # Vente probable
        entry_price = tick.bid
        sl_points = sl_price - entry_price

    if sl_points <= 0:
        logger.error(f"Distance SL invalide ou nulle (Points: {sl_points}). SL: {sl_price}, Entrée: {entry_price}")
        return None

    # Valeur d'un lot dans la devise de profit
    value_per_lot = contract_size * sl_points
    
    # Conversion si nécessaire
    if account_currency != symbol_currency_profit:
        # Tenter de trouver le taux de conversion
        pair_name = f"{symbol_currency_profit}{account_currency}"
        pair_tick = _mt5_connector.mt5.symbol_info_tick(pair_name)
        
        if pair_tick:
            conversion_rate = pair_tick.bid # Combien de devise de compte pour 1 de devise de profit
        else:
            # Tenter l'inverse
            pair_name_inv = f"{account_currency}{symbol_currency_profit}"
            pair_tick_inv = _mt5_connector.mt5.symbol_info_tick(pair_name_inv)
            if pair_tick_inv and pair_tick_inv.ask > 0:
                conversion_rate = 1.0 / pair_tick_inv.ask
            else:
                logger.error(f"Impossible de trouver le taux de conversion {pair_name} ou {pair_name_inv}")
                return None
        
        value_per_lot_account_ccy = value_per_lot * conversion_rate
    else:
        value_per_lot_account_ccy = value_per_lot

    if value_per_lot_account_ccy <= 0:
        logger.error(f"Perte par lot calculée invalide: {value_per_lot_account_ccy}")
        return None

    # Calcul final du volume
    lot_size = risk_amount / value_per_lot_account_ccy
    
    # Arrondir au "step" (ex: 0.01)
    lot_size = (lot_size // volume_step) * volume_step
    
    # S'assurer que c'est au moins le minimum
    if lot_size < volume_min:
        logger.warning(f"Taille de lot ({lot_size}) inférieure au min ({volume_min}). Mise à {volume_min}.")
        lot_size = volume_min
        
    return round(lot_size, 2)
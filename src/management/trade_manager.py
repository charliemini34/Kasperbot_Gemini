# Fichier: src/management/trade_manager.py
"""
Nouveau Module (Version 3.0.2)
Gère la logique de trade post-ouverture:
- Break-Even (BE)
- Trailing Stop Loss (TSL)

--- MODIFIÉ V3.0.2 ---
- Correction du crash AttributeError (position.symbol -> position['symbol'])
- Correction de la syntaxe (order_type == 0 (mt5...) -> order_type == mt5.ORDER_TYPE_BUY)
"""

__version__ = "3.0.2"

import logging
import MetaTrader5 as mt5 # Importation nécessaire pour les constantes
from src.execution import mt5_executor
from src.data_ingest import mt5_connector # Nécessaire pour les infos symbole/tick

logger = logging.getLogger(__name__)

def manage_open_position(position: dict, config: dict):
    """
    Logique principale de gestion pour une position ouverte.
    Appelée par main.py à chaque cycle pour chaque trade.
    """
    
    # --- CORRIGÉ V3.0.2: Accès par Dictionnaire ---
    symbol = position['symbol']
    position_id = position['ticket']
    
    try:
        # --- 1. Récupérer les données nécessaires ---
        price_open = position['price_open']
        sl_initial = position['sl']
        tp_initial = position['tp']
        order_type = position['type'] # 0 = BUY, 1 = SELL
        
        if sl_initial == 0.0:
            logger.warning(f"[{symbol}] Trade {position_id} n'a pas de SL initial. Impossible de gérer.")
            return

        # Récupérer les paramètres de gestion depuis le config
        mgmt_config = config['risk'].get('trade_management', {})
        be_enabled = mgmt_config.get('breakeven_enabled', False)
        be_trigger_rr = mgmt_config.get('breakeven_trigger_rr', 1.0)
        
        tsl_enabled = mgmt_config.get('trailing_sl_enabled', False)
        tsl_trigger_rr = mgmt_config.get('trailing_sl_trigger_rr', 2.0)
        tsl_distance_rr = mgmt_config.get('trailing_sl_distance_rr', 1.0)

        # Récupérer le prix actuel
        tick = mt5_connector.get_symbol_tick(symbol)
        if not tick:
            logger.warning(f"[{symbol}] Impossible de récupérer le tick. Gestion de trade annulée.")
            return

        # Déterminer le prix pertinent (Bid pour un Achat, Ask pour une Vente)
        # --- CORRIGÉ V3.0.1: Syntaxe Python ---
        current_price = tick.bid if order_type == mt5.ORDER_TYPE_BUY else tick.ask
        
        # Récupérer le pip_size pour les calculs de RR
        pip_size = config['risk']['pip_sizes'].get(symbol, config['risk']['default_pip_size'])

        # --- 2. Calculer le Risque "1R" en Pips ---
        # C'est la distance entre l'entrée et le SL initial
        risk_distance_pips = 0.0
        if order_type == mt5.ORDER_TYPE_BUY: # Achat
             risk_distance_pips = (price_open - sl_initial) / pip_size
        else: # Vente
             risk_distance_pips = (sl_initial - price_open) / pip_size
        # --- FIN CORRECTION V3.0.1 ---

        if risk_distance_pips <= 0:
            logger.warning(f"[{symbol}] Distance de risque (1R) invalide: {risk_distance_pips}. Gestion annulée.")
            return

        # --- 3. Logique de Trailing Stop (TSL) ---
        # Le TSL a priorité sur le BE (s'il est plus haut)
        if tsl_enabled:
            tsl_trigger_profit_pips = risk_distance_pips * tsl_trigger_rr
            tsl_distance_pips = risk_distance_pips * tsl_distance_rr
            
            new_tsl_price = 0.0
            
            # --- CORRIGÉ V3.0.1: Syntaxe Python ---
            if order_type == mt5.ORDER_TYPE_BUY: # Achat
                current_profit_pips = (current_price - price_open) / pip_size
                if current_profit_pips >= tsl_trigger_profit_pips:
                    # Calculer le nouveau SL (à 1R derrière le prix actuel)
                    new_tsl_price = current_price - (tsl_distance_pips * pip_size)
                    # S'assurer que le nouveau TSL est > SL initial ET > prix d'entrée
                    if new_tsl_price > sl_initial and new_tsl_price > price_open:
                        logger.info(f"[{symbol} TSL] Déclenchement Trailing SL (Achat). SL de {sl_initial} -> {new_tsl_price}")
                        mt5_executor.modify_position_sl(position_id, symbol, new_tsl_price)
                        return # TSL exécuté, on arrête ici pour ce cycle

            else: # Vente
                current_profit_pips = (price_open - current_price) / pip_size
                if current_profit_pips >= tsl_trigger_profit_pips:
                    # Calculer le nouveau SL (à 1R derrière le prix actuel)
                    new_tsl_price = current_price + (tsl_distance_pips * pip_size)
                    # S'assurer que le nouveau TSL est < SL initial ET < prix d'entrée
                    if new_tsl_price < sl_initial and new_tsl_price < price_open:
                        logger.info(f"[{symbol} TSL] Déclenchement Trailing SL (Vente). SL de {sl_initial} -> {new_tsl_price}")
                        mt5_executor.modify_position_sl(position_id, symbol, new_tsl_price)
                        return # TSL exécuté, on arrête ici pour ce cycle
            # --- FIN CORRECTION V3.0.1 ---

        # --- 4. Logique de Break-Even (BE) ---
        # Exécutée SEULEMENT si le TSL n'a pas été déclenché (car TSL > BE)
        if be_enabled:
            # Si le SL est déjà au-dessus (BUY) ou en-dessous (SELL) du prix d'entrée, le BE est déjà fait.
            # --- CORRIGÉ V3.0.1: Syntaxe Python ---
            if (order_type == mt5.ORDER_TYPE_BUY and sl_initial >= price_open) or (order_type == mt5.ORDER_TYPE_SELL and sl_initial <= price_open):
                return # Déjà à BE ou en profit (géré par TSL)

            be_trigger_profit_pips = risk_distance_pips * be_trigger_rr
            
            if order_type == mt5.ORDER_TYPE_BUY: # Achat
                current_profit_pips = (current_price - price_open) / pip_size
                if current_profit_pips >= be_trigger_profit_pips:
                    logger.info(f"[{symbol} BE] Déclenchement Break-Even (Achat). SL de {sl_initial} -> {price_open}")
                    mt5_executor.modify_position_sl(position_id, symbol, price_open)
                    return # BE exécuté

            else: # Vente
                current_profit_pips = (price_open - current_price) / pip_size
                if current_profit_pips >= be_trigger_profit_pips:
                    logger.info(f"[{symbol} BE] Déclenchement Break-Even (Vente). SL de {sl_initial} -> {price_open}")
                    mt5_executor.modify_position_sl(position_id, symbol, price_open)
                    return # BE exécuté
            # --- FIN CORRECTION V3.0.1 ---

    except Exception as e:
        # --- CORRIGÉ V3.0.2: Accès par Dictionnaire dans le logger d'erreur ---
        logger.error(f"Erreur majeure dans le Trade Manager pour {position['symbol']}: {e}", exc_info=True)
        # --- FIN CORRECTION V3.0.2 ---
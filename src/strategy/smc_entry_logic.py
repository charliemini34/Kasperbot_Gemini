"""
Module de Stratégie SMC (Smart Money Concepts).

Ce module est le "cerveau" du bot. Il combine l'analyse de structure
multi-timeframe (MTF) avec la détection de POI (Points of Interest)
pour générer des signaux de trading basés sur la logique SMC/ICT.

La stratégie de base est :
1. Définir la tendance de fond (Bias) sur la Timeframe Haute (HTF).
2. Attendre un retracement sur la Timeframe Basse (LTF).
3. Identifier des POI (OB, FVG) dans une zone "Discount" (pour achat) ou "Premium" (pour vente).
4. (Filtre) Privilégier les POI situés dans l'OTE (Optimal Trade Entry).
5. Générer un signal lorsque le prix touche ce POI validé.
"""

import logging
import pandas as pd
import numpy as np

# Importation de nos modules personnalisés
from src.analysis import market_structure as structure
from src.patterns import pattern_detector as patterns

logger = logging.getLogger(__name__)

def _get_fibonacci_zones(start_price: float, end_price: float) -> dict:
    """
    Calcule les niveaux clés de Fibonacci (Discount, Premium, OTE) pour un swing.
    
    Args:
        start_price (float): Le prix de départ du swing (ex: un swing low).
        end_price (float): Le prix de fin du swing (ex: un swing high).

    Returns:
        dict: Un dictionnaire contenant les niveaux de prix 'equilibrium', 
              'discount_top', 'premium_bottom', 'ote_top', 'ote_bottom'.
    """
    if start_price == 0 or end_price == 0:
        return {}
        
    is_bullish_swing = end_price > start_price
    diff = end_price - start_price

    zones = {
        'equilibrium': start_price + diff * 0.5,
        'ote_top': start_price + diff * 0.62 if is_bullish_swing else start_price + diff * 0.38,
        'ote_bottom': start_price + diff * 0.786 if is_bullish_swing else start_price + diff * 0.214,
    }

    if is_bullish_swing:
        # Pour un swing haussier, la zone "discount" est en bas
        zones['discount_top'] = zones['equilibrium']
        # Pour un swing haussier, la zone "premium" est en haut
        zones['premium_bottom'] = zones['equilibrium']
        # L'OTE est dans la zone Discount
        # 
    else:
        # Pour un swing baissier, la zone "discount" est en haut
        zones['discount_top'] = zones['equilibrium']
        # Pour un swing baissier, la zone "premium" est en bas
        zones['premium_bottom'] = zones['equilibrium']
        # L'OTE est dans la zone Premium (swap top/bottom pour la logique)
        zones['ote_top'], zones['ote_bottom'] = zones['ote_bottom'], zones['ote_top']
        # 

    return zones


def check_smc_signal(mtf_data: dict, config: dict):
    """
    Vérifie les données multi-timeframe pour un signal d'entrée SMC.

    Args:
        mtf_data (dict): Dictionnaire de DataFrames. 
                         Ex: {'H4': pd.DataFrame, 'M15': pd.DataFrame}
        config (dict): Dictionnaire de configuration de la stratégie.

    Returns:
        tuple: (signal, raison, sl_price, tp_price) ou (None, None, None, None)
    """
    
    try:
        # --- Étape 1: Récupérer les paramètres et données ---
        strategy_params = config['strategy']
        htf_tf = strategy_params['htf_timeframe'] # Ex: 'H4'
        ltf_tf = strategy_params['ltf_timeframe'] # Ex: 'M15'
        
        htf_data = mtf_data.get(htf_tf)
        ltf_data = mtf_data.get(ltf_tf)

        if htf_data is None or ltf_data is None:
            logger.warning(f"Données manquantes pour {htf_tf} or {ltf_tf}. Signal ignoré.")
            return None, None, None, None

        current_low = ltf_data['low'].iloc[-1]
        current_high = ltf_data['high'].iloc[-1]
        
        # --- Étape 2: Définir la tendance de fond (Bias HTF) ---
        htf_swings_high, htf_swings_low = structure.find_swing_highs_lows(
            htf_data, order=strategy_params['htf_swing_order']
        )
        _htf_events, htf_trend = structure.identify_structure(htf_swings_high, htf_swings_low)
        
        if htf_trend not in ["BULLISH", "BEARISH"]:
            logger.info(f"Tendance HTF ({htf_tf}) non claire ({htf_trend}). Pas de signal.")
            return None, None, None, None
            
        logger.info(f"Tendance HTF ({htf_tf}) confirmée : {htf_trend}")

        # --- Étape 3: Analyser la structure LTF pour le retracement ---
        ltf_swings_high, ltf_swings_low = structure.find_swing_highs_lows(
            ltf_data, order=strategy_params['ltf_swing_order']
        )
        if len(ltf_swings_high) < 2 or len(ltf_swings_low) < 2:
            logger.info("Pas assez de points de structure LTF. En attente...")
            return None, None, None, None

        # --- Étape 4: Logique d'ACHAT (HTF Bullish) ---
        if htf_trend == "BULLISH":
            # On cherche un retracement vers une zone "Discount" + OTE
            
            # 1. Trouver le dernier swing haussier LTF à retracer
            last_high_point = ltf_swings_high[-1]
            # Trouver le swing low qui a précédé ce swing high
            relevant_low_points = [s for s in ltf_swings_low if s[0] < last_high_point[0]]
            if not relevant_low_points:
                logger.info("Structure LTF (Bullish) non claire pour le retracement.")
                return None, None, None, None
            
            last_low_point = relevant_low_points[-1]
            
            # 2. Calculer les zones Fib de ce swing haussier
            fib_zones = _get_fibonacci_zones(last_low_point[1], last_high_point[1])
            if not fib_zones:
                return None, None, None, None

            # 3. Vérifier si le prix est dans la zone Discount/OTE
            if current_low > fib_zones['discount_top']:
                logger.debug("Le prix est toujours en 'Premium'. En attente de retracement.")
                return None, None, None, None

            # 4. Trouver des POI (Bullish OB/FVG) dans la zone OTE
            # 
            pois_in_ote = []
            
            # Trouver les Order Blocks Bullish non mitigés
            all_bullish_obs = [ob for ob in patterns.find_order_blocks(ltf_data) if ob['type'] == 'BULLISH']
            for ob in all_bullish_obs:
                # Si le POI est dans la zone OTE
                if (ob['top'] <= fib_zones['ote_top'] and 
                    ob['bottom'] >= fib_zones['ote_bottom']):
                    # Et si le POI n'a pas déjà été testé par une mèche récente
                    if current_low > ob['top']: 
                        pois_in_ote.append({'poi_type': 'OB', **ob})

            # Trouver les Imbalances Bullish non mitigées
            all_bullish_fvgs = [fvg for fvg in patterns.find_imbalances(ltf_data) 
                                if fvg['type'] == 'BULLISH' and fvg['mitigated_at'] is None]
            for fvg in all_bullish_fvgs:
                if (fvg['top'] <= fib_zones['ote_top'] and 
                    fvg['bottom'] >= fib_zones['ote_bottom']):
                    if current_low > fvg['top']:
                        pois_in_ote.append({'poi_type': 'FVG', **fvg})

            # 5. Chercher le signal d'entrée
            for poi in sorted(pois_in_ote, key=lambda x: x['top'], reverse=True): # Prioriser le POI le plus haut
                # Si la mèche actuelle (current_low) vient de toucher le haut du POI
                if current_low <= poi['top']:
                    reason = f"ACHAT: HTF({htf_tf}) {htf_trend} + LTF({ltf_tf}) OTE + {poi['poi_type']}"
                    sl_price = poi['bottom'] * (1 - 0.0005) # SL 0.05% sous le bas du POI
                    tp_price = last_high_point[1] * (1 + 0.0005) # TP 0.05% au-dessus du dernier high (cible de liquidité)
                    
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "BUY", reason, sl_price, tp_price

        # --- Étape 5: Logique de VENTE (HTF Bearish) ---
        elif htf_trend == "BEARISH":
            # On cherche un retracement vers une zone "Premium" + OTE
            
            # 1. Trouver le dernier swing baissier LTF à retracer
            last_low_point = ltf_swings_low[-1]
            relevant_high_points = [s for s in ltf_swings_high if s[0] < last_low_point[0]]
            if not relevant_high_points:
                logger.info("Structure LTF (Bearish) non claire pour le retracement.")
                return None, None, None, None
            
            last_high_point = relevant_high_points[-1]
            
            # 2. Calculer les zones Fib de ce swing baissier
            fib_zones = _get_fibonacci_zones(last_high_point[1], last_low_point[1])
            if not fib_zones:
                return None, None, None, None

            # 3. Vérifier si le prix est dans la zone Premium/OTE
            if current_high < fib_zones['premium_bottom']:
                logger.debug("Le prix est toujours en 'Discount'. En attente de retracement.")
                return None, None, None, None

            # 4. Trouver des POI (Bearish OB/FVG) dans la zone OTE
            # 
            pois_in_ote = []

            all_bearish_obs = [ob for ob in patterns.find_order_blocks(ltf_data) if ob['type'] == 'BEARISH']
            for ob in all_bearish_obs:
                if (ob['top'] <= fib_zones['ote_top'] and 
                    ob['bottom'] >= fib_zones['ote_bottom']):
                    if current_high < ob['bottom']:
                        pois_in_ote.append({'poi_type': 'OB', **ob})

            all_bearish_fvgs = [fvg for fvg in patterns.find_imbalances(ltf_data) 
                                if fvg['type'] == 'BEARISH' and fvg['mitigated_at'] is None]
            for fvg in all_bearish_fvgs:
                if (fvg['top'] <= fib_zones['ote_top'] and 
                    fvg['bottom'] >= fib_zones['ote_bottom']):
                    if current_high < fvg['bottom']:
                        pois_in_ote.append({'poi_type': 'FVG', **fvg})

            # 5. Chercher le signal d'entrée
            for poi in sorted(pois_in_ote, key=lambda x: x['bottom']): # Prioriser le POI le plus bas
                # Si la mèche actuelle (current_high) vient de toucher le bas du POI
                if current_high >= poi['bottom']:
                    reason = f"VENTE: HTF({htf_tf}) {htf_trend} + LTF({ltf_tf}) OTE + {poi['poi_type']}"
                    sl_price = poi['top'] * (1 + 0.0005) # SL 0.05% au-dessus du haut du POI
                    tp_price = last_low_point[1] * (1 - 0.0005) # TP 0.05% sous le dernier low (cible de liquidité)
                    
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "SELL", reason, sl_price, tp_price

    except Exception as e:
        logger.error(f"Erreur majeure dans la logique de stratégie SMC: {e}", exc_info=True)
        return None, None, None, None
    
    # Si aucune condition n'est remplie
    logger.debug("Aucun signal SMC valide trouvé pour le moment.")
    return None, None, None, None
"""
Fichier: src/strategy/smc_orchestrator.py
Version: 1.0.0
Description: Module d'orchestration de la stratégie SMC.
             Combine l'analyse MTF (structure, POI) pour générer 
             un signal de trade compatible avec le RiskManager v13.
"""

import logging
import pandas as pd
import numpy as np

# Importation de nos nouveaux modules et des modules de patterns existants
from src.analysis import market_structure as structure
from src.patterns import pattern_detector as patterns # Nous l'importerons (il sera modifié)
from src.constants import BUY, SELL

logger = logging.getLogger(__name__)

def _get_fibonacci_zones(start_price: float, end_price: float) -> dict:
    """
    Calcule les niveaux clés de Fibonacci (Discount, Premium, OTE) pour un swing.
    """
    if start_price == 0 or end_price == 0 or start_price == end_price:
        return {}
        
    is_bullish_swing = end_price > start_price
    diff = end_price - start_price

    zones = {
        'equilibrium': start_price + diff * 0.5,
        'ote_top': start_price + diff * (1.0 - 0.62),  # 0.62
        'ote_bottom': start_price + diff * (1.0 - 0.786), # 0.786
    }
    
    # 
    if is_bullish_swing:
        # Pour un swing haussier, la zone "discount" est en bas
        zones['discount_top'] = zones['equilibrium']
        zones['premium_bottom'] = zones['equilibrium']
        # L'OTE est dans la zone Discount. 'ote_top' est le 0.62 (plus haut), 'ote_bottom' est le 0.786 (plus bas)
        zones['ote_top'] = start_price + diff * (1.0 - 0.62)
        zones['ote_bottom'] = start_price + diff * (1.0 - 0.786)

    # 
    else: # Swing Baissier
        # Pour un swing baissier, la zone "premium" est en haut
        zones['discount_top'] = zones['equilibrium']
        zones['premium_bottom'] = zones['equilibrium']
        # L'OTE est dans la zone Premium. 'ote_top' est le 0.786 (plus haut), 'ote_bottom' est le 0.62 (plus bas)
        zones['ote_top'] = start_price + diff * (1.0 - 0.786) 
        zones['ote_bottom'] = start_price + diff * (1.0 - 0.62)

    return zones

def find_smc_signal(mtf_data: dict, config: dict):
    """
    Orchestre l'analyse SMC complète et retourne un signal compatible RiskManager v13.

    Args:
        mtf_data (dict): Dictionnaire de DataFrames. 
                         Ex: {'H4': pd.DataFrame, 'M15': pd.DataFrame}
        config (dict): Dictionnaire de configuration.

    Returns:
        dict: Un dictionnaire de signal (ex: {'direction': BUY, ...}) 
              ou None si aucun signal n'est trouvé.
    """
    
    try:
        # --- Étape 1: Récupérer les paramètres et données ---
        strategy_params = config.get('smc_strategy', {}) # Nouveaux params de config
        htf_tf = strategy_params.get('htf_timeframe', 'H4')
        ltf_tf = strategy_params.get('ltf_timeframe', 'M15')
        
        htf_data = mtf_data.get(htf_tf)
        ltf_data = mtf_data.get(ltf_tf)

        if htf_data is None or ltf_data is None:
            logger.warning(f"SMC: Données manquantes pour {htf_tf} or {ltf_tf}. Signal ignoré.")
            return None

        current_low = ltf_data['low'].iloc[-1]
        current_high = ltf_data['high'].iloc[-1]
        
        # --- Étape 2: Définir la tendance de fond (Bias HTF) ---
        htf_swing_order = strategy_params.get('htf_swing_order', 10)
        htf_swings_high, htf_swings_low = structure.find_swing_highs_lows(htf_data, order=htf_swing_order)
        _htf_events, htf_trend, _h, _l = structure.identify_structure(htf_swings_high, htf_swings_low)
        
        if htf_trend not in ["BULLISH", "BEARISH"]:
            logger.info(f"SMC: Tendance HTF ({htf_tf}) non claire ({htf_trend}). Pas de signal.")
            return None
            
        logger.info(f"SMC: Tendance HTF ({htf_tf}) confirmée : {htf_trend}")

        # --- Étape 3: Analyser la structure LTF pour le retracement ---
        ltf_swing_order = strategy_params.get('ltf_swing_order', 5)
        ltf_swings_high, ltf_swings_low = structure.find_swing_highs_lows(ltf_data, order=ltf_swing_order)
        if len(ltf_swings_high) < 2 or len(ltf_swings_low) < 2:
            logger.info("SMC: Pas assez de points de structure LTF. En attente...")
            return None
            
        # --- Étape 4: Logique d'ACHAT (HTF Bullish) ---
        if htf_trend == "BULLISH":
            # 1. Trouver le dernier swing haussier LTF à retracer
            last_high_point = ltf_swings_high[-1]
            relevant_low_points = [s for s in ltf_swings_low if s[0] < last_high_point[0]]
            if not relevant_low_points:
                logger.info("SMC (Bullish): Structure LTF non claire pour le retracement.")
                return None
            last_low_point = relevant_low_points[-1]

            # 2. Calculer les zones Fib de ce swing haussier
            fib_zones = _get_fibonacci_zones(last_low_point[1], last_high_point[1])
            if not fib_zones: return None

            # 3. Vérifier si le prix est dans la zone Discount
            if current_low > fib_zones['discount_top']:
                logger.debug("SMC: Le prix est toujours en 'Premium'. En attente de retracement.")
                return None

            # 4. Trouver des POI (Bullish OB/FVG) dans la zone OTE
            # (Nous allons modifier 'patterns.py' pour contenir ces fonctions)
            pois_in_ote = []
            all_bullish_obs = patterns.find_order_blocks(ltf_data, config) # Modifié
            for ob in [o for o in all_bullish_obs if o['type'] == BUY]:
                if (ob['top'] <= fib_zones['ote_top'] and ob['bottom'] >= fib_zones['ote_bottom']):
                    pois_in_ote.append({'poi_type': 'OB', **ob})

            all_bullish_fvgs = patterns.find_imbalances(ltf_data, config) # Modifié
            for fvg in [f for f in all_bullish_fvgs if f['type'] == BUY and not f['mitigated_at']]:
                if (fvg['top'] <= fib_zones['ote_top'] and fvg['bottom'] >= fib_zones['ote_bottom']):
                    pois_in_ote.append({'poi_type': 'FVG', **fvg})

            # 5. Chercher le signal d'entrée (Prioriser le POI le plus haut)
            best_poi = max(pois_in_ote, key=lambda x: x['top'], default=None)
            
            if best_poi and current_low <= best_poi['top']:
                # Formatage du signal pour RiskManager v13
                return {
                    "direction": BUY,
                    "pattern": f"SMC_OTE_{best_poi['poi_type']}",
                    "entry_zone_start": best_poi['top'],
                    "entry_zone_end": best_poi['bottom'],
                    "stop_loss_level": best_poi['bottom'], # SL structurel (sera bufferisé par RM)
                    "target_price": last_high_point[1],  # Cible = Liquidité du dernier High
                    "reason": f"ACHAT: HTF({htf_tf}) {htf_trend} + LTF({ltf_tf}) OTE + {best_poi['poi_type']}"
                }

        # --- Étape 5: Logique de VENTE (HTF Bearish) ---
        elif htf_trend == "BEARISH":
            # 1. Trouver le dernier swing baissier LTF
            last_low_point = ltf_swings_low[-1]
            relevant_high_points = [s for s in ltf_swings_high if s[0] < last_low_point[0]]
            if not relevant_high_points:
                logger.info("SMC (Bearish): Structure LTF non claire pour le retracement.")
                return None
            last_high_point = relevant_high_points[-1]
            
            # 2. Calculer les zones Fib
            fib_zones = _get_fibonacci_zones(last_high_point[1], last_low_point[1])
            if not fib_zones: return None

            # 3. Vérifier si le prix est dans la zone Premium
            if current_high < fib_zones['premium_bottom']:
                logger.debug("SMC: Le prix est toujours en 'Discount'. En attente de retracement.")
                return None

            # 4. Trouver des POI (Bearish OB/FVG) dans la zone OTE
            pois_in_ote = []
            all_bearish_obs = patterns.find_order_blocks(ltf_data, config) # Modifié
            for ob in [o for o in all_bearish_obs if o['type'] == SELL]:
                if (ob['top'] <= fib_zones['ote_top'] and ob['bottom'] >= fib_zones['ote_bottom']):
                    pois_in_ote.append({'poi_type': 'OB', **ob})

            all_bearish_fvgs = patterns.find_imbalances(ltf_data, config) # Modifié
            for fvg in [f for f in all_bearish_fvgs if f['type'] == SELL and not f['mitigated_at']]:
                if (fvg['top'] <= fib_zones['ote_top'] and fvg['bottom'] >= fib_zones['ote_bottom']):
                    pois_in_ote.append({'poi_type': 'FVG', **fvg})

            # 5. Chercher le signal d'entrée (Prioriser le POI le plus bas)
            best_poi = min(pois_in_ote, key=lambda x: x['bottom'], default=None)

            if best_poi and current_high >= best_poi['bottom']:
                # Formatage du signal pour RiskManager v13
                return {
                    "direction": SELL,
                    "pattern": f"SMC_OTE_{best_poi['poi_type']}",
                    "entry_zone_start": best_poi['top'],
                    "entry_zone_end": best_poi['bottom'],
                    "stop_loss_level": best_poi['top'], # SL structurel (sera bufferisé par RM)
                    "target_price": last_low_point[1], # Cible = Liquidité du dernier Low
                    "reason": f"VENTE: HTF({htf_tf}) {htf_trend} + LTF({ltf_tf}) OTE + {best_poi['poi_type']}"
                }

    except Exception as e:
        logger.error(f"Erreur majeure dans l'orchestrateur SMC: {e}", exc_info=True)
        return None
    
    logger.debug("SMC: Aucune opportunité OTE trouvée pour le moment.")
    return None
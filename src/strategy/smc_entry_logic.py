# Fichier: src/strategy/smc_entry_logic.py
"""
Module de Stratégie SMC (Smart Money Concepts).

Contient la logique de détection pour les Modèles M1, M2 et M3.

Version: 2.1.2
"""

__version__ = "2.1.2"

import logging
import pandas as pd
import numpy as np
from typing import Dict, Any, Tuple, Optional, List

# Importation de nos modules personnalisés
from src.analysis import market_structure as structure
from src.patterns import pattern_detector as patterns

logger = logging.getLogger(__name__)

# --- Logique de base SMC (Fibonacci) ---
def _get_fibonacci_zones(start_price: float, end_price: float) -> Optional[Dict[str, float]]:
    """
    Calcule les niveaux clés de Fibonacci (Discount, Premium, OTE) pour un swing.
    """
    if start_price == 0 or end_price == 0 or start_price == end_price:
        logger.debug("Calcul Fib impossible: prix de départ ou de fin invalide.")
        return None
        
    is_bullish_swing = end_price > start_price
    diff = end_price - start_price # Positif si bullish, négatif si bearish
    
    level_0_500 = start_price + diff * 0.5
    level_0_618 = start_price + diff * (1 - 0.618) # Niveau 0.62 Fibo (OTE Top pour Bullish)
    level_0_786 = start_price + diff * (1 - 0.786) # Niveau 0.786 Fibo (OTE Bottom pour Bullish)

    zones = {
        'equilibrium': level_0_500
    }

    if is_bullish_swing:
        zones['premium_zone_top'] = end_price
        zones['premium_zone_bottom'] = level_0_500
        zones['discount_zone_top'] = level_0_500
        zones['discount_zone_bottom'] = start_price
        zones['ote_zone_top'] = level_0_618
        zones['ote_zone_bottom'] = level_0_786
    else: # Bearish Swing
        zones['premium_zone_top'] = start_price
        zones['premium_zone_bottom'] = level_0_500
        zones['discount_zone_top'] = level_0_500
        zones['discount_zone_bottom'] = end_price
        zones['ote_zone_top'] = level_0_786 # OTE Top pour Bearish
        zones['ote_zone_bottom'] = level_0_618 # OTE Bottom pour Bearish

    # Assurons-nous que top > bottom (pour les 2 cas)
    if zones['ote_zone_top'] < zones['ote_zone_bottom']:
        zones['ote_zone_top'], zones['ote_zone_bottom'] = zones['ote_zone_bottom'], zones['ote_zone_top']

    return zones


def _find_valid_htf_pois(data: pd.DataFrame, swings_high: list, swings_low: list, trend: str) -> List[Dict[str, Any]]:
    """
    Trouve les POI HTF (OBs, FVGs) qui sont valides pour un setup.
    Un POI est valide s'il est non-mitigé ET dans la bonne zone (Premium/Discount).
    
    --- MODIFIÉ V2.1.0 ---
    Filtre les OBs pour n'inclure que ceux avec FVG (selon pattern_detector v2.1.0)
    """
    valid_pois = []
    
    # 1. Trouver le dernier swing pertinent pour le retracement
    if trend == "BULLISH":
        if not swings_high or not swings_low: return []
        last_high_point = swings_high[-1]
        relevant_low_points = [s for s in swings_low if s[0] < last_high_point[0]]
        if not relevant_low_points: return []
        last_low_point = relevant_low_points[-1]
        
        fib_zones = _get_fibonacci_zones(last_low_point[1], last_high_point[1])
        target_zone = "DISCOUNT"
        
    elif trend == "BEARISH":
        if not swings_high or not swings_low: return []
        last_low_point = swings_low[-1]
        relevant_high_points = [s for s in swings_high if s[0] < last_low_point[0]]
        if not relevant_high_points: return []
        last_high_point = relevant_high_points[-1]
        
        fib_zones = _get_fibonacci_zones(last_high_point[1], last_low_point[1])
        target_zone = "PREMIUM"
        
    else:
        return [] # Pas de tendance claire

    if not fib_zones:
        return []

    # 2. Définir la zone de prix cible
    if target_zone == "DISCOUNT":
        zone_top = fib_zones['discount_zone_top']
        zone_bottom = fib_zones['discount_zone_bottom']
        poi_type_needed = "BULLISH"
    else: # PREMIUM
        zone_top = fib_zones['premium_zone_top']
        zone_bottom = fib_zones['premium_zone_bottom']
        poi_type_needed = "BEARISH"

    # 3. Trouver les POIs et les filtrer
    
    # Filtre FVGs (inchangé)
    all_fvgs = patterns.find_fvgs(data)
    for fvg in all_fvgs:
        if fvg['type'] == poi_type_needed and not fvg['mitigated']:
            # Vérifie s'il y a chevauchement entre le FVG et la zone P/D
            if max(fvg['bottom'], zone_bottom) < min(fvg['top'], zone_top):
                fvg['poi_type'] = 'FVG'
                valid_pois.append(fvg)

    # Filtre OBs (MODIFIÉ V2.1.0)
    all_obs = patterns.find_order_blocks(data, swings_high, swings_low)
    for ob in all_obs:
         # Vérifie 1: Type, 2: Mitigé, 3: S'il a un FVG (logique vidéo)
         if (ob['type'] == poi_type_needed and 
             not ob['mitigated'] and 
             ob.get("has_fvg") == True): # <-- NOUVEAU FILTRE (V2.1.0)
            
            # Vérifie s'il y a chevauchement entre l'OB et la zone P/D
            if max(ob['bottom'], zone_bottom) < min(ob['top'], zone_top):
                ob['poi_type'] = 'OB'
                valid_pois.append(ob)
                
    logger.info(f"Trouvé {len(valid_pois)} POIs HTF valides (filtrés FVG) dans la zone {target_zone}.")
    return valid_pois


# --- MODÈLE 1 (MODIFIÉ V2.1.1) ---
def _check_model_1_confirmation(
    htf_trend: str, 
    htf_data: pd.DataFrame, 
    ltf_data: pd.DataFrame, 
    htf_swings_high: list, 
    htf_swings_low: list,
    ltf_events: list,
    ltf_swings_high: list,
    ltf_swings_low: list,
    current_price: float,
    config: dict
) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """
    Vérifie le "Modèle 1: Confirmation POI HTF + CHOCH LTF + Retour OTE" (Logique Vidéo 2)
    """
    strategy_params = config['strategy']
    
    # --- Étape 1: Identifier les POI HTF valides (Filtrés P/D) ---
    valid_htf_pois = _find_valid_htf_pois(htf_data, htf_swings_high, htf_swings_low, htf_trend)
    if not valid_htf_pois:
        logger.debug("[M1] Aucun POI HTF valide trouvé.")
        return None, None, None, None

    # --- Étape 2: Vérifier si le prix est dans une zone HTF POI ---
    is_in_htf_poi = False
    active_htf_poi = None
    for poi in valid_htf_pois:
        # On vérifie si le prix TOUCHE ou a TOUCHE RECEMMENT le POI
        # (on regarde les 5 dernières bougies LTF)
        if (ltf_data['low'].iloc[-5:].min() <= poi['top']) and (ltf_data['high'].iloc[-5:].max() >= poi['bottom']):
            is_in_htf_poi = True
            active_htf_poi = poi
            break
    
    if not is_in_htf_poi:
        logger.debug(f"[M1] Le prix n'a pas réagi dans une zone POI HTF. En attente...")
        return None, None, None, None
        
    logger.info(f"[M1] Le prix est DANS ou A RÉAGI sur le POI HTF {active_htf_poi['poi_type']}")

    # --- Étape 3: "Zoomer" sur LTF et attendre la confirmation (CHOCH) ---
    if not ltf_events:
        logger.info("[M1] En attente d'événements de structure LTF...")
        return None, None, None, None
        
    last_ltf_event = ltf_events[-1]

    # --- Logique de Confirmation (Le cœur du Modèle 1 - V2.1.1) ---
    
    if htf_trend == "BULLISH":
        if last_ltf_event['type'] == 'CHOCH' and last_ltf_event['trend'] == 'BULLISH':
            logger.info(f"[M1] CHOCH Haussier LTF confirmé (dans POI HTF).")
            # --- Étape 4: Attendre Retour à l'OTE du swing CHOCH ---
            choch_swing_low = ltf_swings_low[-1] # Le plus bas avant le CHOCH
            choch_swing_high = ltf_swings_high[-1] # Le plus haut qui a cassé
            
            fib_zones = _get_fibonacci_zones(choch_swing_low[1], choch_swing_high[1])
            if not fib_zones: return None, None, None, None

            ote_top = fib_zones['ote_zone_top']
            ote_bottom = fib_zones['ote_zone_bottom']
            
            # Trouver POI (FVG) dans OTE
            ltf_pois = patterns.find_fvgs(ltf_data.loc[choch_swing_low[0]:choch_swing_high[0]])
            valid_ote_poi = None
            for poi in ltf_pois:
                if poi['type'] == 'BULLISH' and max(poi['bottom'], ote_bottom) < min(poi['top'], ote_top):
                    valid_ote_poi = poi
                    break # On prend le premier
            
            if valid_ote_poi:
                # --- Étape 5: Vérifier si le prix entre dans le POI OTE ---
                if (current_price <= valid_ote_poi['top']) and (current_price >= valid_ote_poi['bottom']):
                    logger.info(f"[M1] Prix entre dans le POI OTE LTF ({valid_ote_poi['top']}-{valid_ote_poi['bottom']}).")
                    reason = f"ACHAT [M1]: HTF Biais Haussier + POI HTF + CHOCH LTF + Retour OTE-POI."
                    sl_price = choch_swing_low[1] * (1 - 0.0005) # SL sous le bas du swing CHOCH
                    tp_price = htf_swings_high[-1][1] # Cible = High HTF
                    
                    if sl_price >= current_price or tp_price <= current_price:
                        logger.warning(f"[M1] Signal ACHAT ignoré: SL/TP invalide.")
                        return None, None, None, None
                        
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "BUY", reason, sl_price, tp_price

    elif htf_trend == "BEARISH":
         if last_ltf_event['type'] == 'CHOCH' and last_ltf_event['trend'] == 'BEARISH':
            logger.info(f"[M1] CHOCH Baissier LTF confirmé (dans POI HTF).")
            # --- Étape 4: Attendre Retour à l'OTE du swing CHOCH ---
            choch_swing_high = ltf_swings_high[-1]
            choch_swing_low = ltf_swings_low[-1]
            
            fib_zones = _get_fibonacci_zones(choch_swing_high[1], choch_swing_low[1])
            if not fib_zones: return None, None, None, None

            ote_top = fib_zones['ote_zone_top']
            ote_bottom = fib_zones['ote_zone_bottom']
            
            # Trouver POI (FVG) dans OTE
            ltf_pois = patterns.find_fvgs(ltf_data.loc[choch_swing_high[0]:choch_swing_low[0]])
            valid_ote_poi = None
            for poi in ltf_pois:
                if poi['type'] == 'BEARISH' and max(poi['bottom'], ote_bottom) < min(poi['top'], ote_top):
                    valid_ote_poi = poi
                    break
            
            if valid_ote_poi:
                 # --- Étape 5: Vérifier si le prix entre dans le POI OTE ---
                if (current_price <= valid_ote_poi['top']) and (current_price >= valid_ote_poi['bottom']):
                    logger.info(f"[M1] Prix entre dans le POI OTE LTF ({valid_ote_poi['top']}-{valid_ote_poi['bottom']}).")
                    reason = f"VENTE [M1]: HTF Biais Baissier + POI HTF + CHOCH LTF + Retour OTE-POI."
                    sl_price = choch_swing_high[1] * (1 + 0.0005) # SL au-dessus du swing CHOCH
                    tp_price = htf_swings_low[-1][1] # Cible = Low HTF
                    
                    if sl_price <= current_price or tp_price >= current_price:
                        logger.warning(f"[M1] Signal VENTE ignoré: SL/TP invalide.")
                        return None, None, None, None
                        
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "SELL", reason, sl_price, tp_price
            
    logger.debug("[M1] Aucune confirmation (CHOCH + Retour OTE) trouvée pour le moment.")
    return None, None, None, None

# --- MODÈLE 2 (MODIFIÉ V2.1.2) ---
def _check_model_2_inducement(
    htf_trend: str, 
    ltf_data: pd.DataFrame, 
    ltf_events: list,
    ltf_swings_high: list,
    ltf_swings_low: list,
    # --- AJOUT V2.1.2 ---
    htf_swings_high: list,
    htf_swings_low: list,
    # --- FIN AJOUT ---
    current_low: float,
    current_high: float,
    current_price: float,
    config: dict,
    pip_size: float # Argument requis
) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """
    Vérifie le "Modèle 2: Inducement (Sweep) + Confirmation CHOCH" (Logique Vidéo 4)
    Biais HTF -> Sweep LTF (EQL/Asia) -> CHOCH LTF -> Entrée Marché Agressive
    """
    strategy_params = config['strategy']
    
    # --- Étape 1: Détecter la liquidité LTF (EQL / Session Low) ---
    ltf_liquidity_zones = []
    
    # 1a. Liquidité de Session (Asia Range)
    try:
        # --- MODIFIÉ V2.1.1: Lecture config V2.1 ---
        sessions_cfg = strategy_params.get('sessions', {})
        asia_range = patterns.find_session_range(
            ltf_data, 
            session_start_hour=sessions_cfg.get('asia_start_hour', 0),
            session_end_hour=sessions_cfg.get('asia_end_hour', 8),
            timezone=sessions_cfg.get('session_timezone', 'Etc/UTC')
        )
        if asia_range:
            ltf_liquidity_zones.append({"type": "ASIA_LOW", "level": asia_range['low']})
            ltf_liquidity_zones.append({"type": "ASIA_HIGH", "level": asia_range['high']})
    except Exception as e:
        logger.warning(f"[M2] Erreur lors de la détection du range de session: {e}")

    # 1b. Equal Highs/Lows (EQL)
    # --- MODIFIÉ V2.1.1: Lecture config V2.1 ---
    models_cfg = strategy_params.get('models', {})
    eql_zones = patterns.find_equal_highs_lows(
        ltf_data, 
        lookback=models_cfg.get('liquidity_lookback', 50),
        tolerance_pips=models_cfg.get('liquidity_tolerance_pips', 5),
        pip_size=pip_size # Utilise l'argument
    )
    for eql in eql_zones['equal_lows']:
        ltf_liquidity_zones.append({"type": "EQL", "level": eql['level']})
    for eqh in eql_zones['equal_highs']:
        ltf_liquidity_zones.append({"type": "EQH", "level": eqh['level']})

    if not ltf_liquidity_zones:
        logger.debug("[M2] Aucune zone de liquidité LTF trouvée.")
        return None, None, None, None

    # --- Étape 2: Chercher un "Sweep" de liquidité + Confirmation ---
    if not ltf_events:
        logger.debug("[M2] En attente d'événements de structure LTF...")
        return None, None, None, None
        
    last_ltf_event = ltf_events[-1]

    if htf_trend == "BULLISH":
        # Biais HTF Haussier: On cherche un sweep d'un BAS (EQL ou Asia Low)
        swept_zone = None
        for zone in ltf_liquidity_zones:
            if zone['type'] in ["EQL", "ASIA_LOW"]:
                # Le "sweep" : la mèche (low) d'une bougie récente est passée SOUS la liquidité
                if ltf_data['low'].iloc[-5:].min() < zone['level']:
                    swept_zone = zone
                    break
        
        if swept_zone:
            logger.info(f"[M2] Sweep de liquidité détecté: {swept_zone['type']} @ {swept_zone['level']}")
            # --- Étape 3: Confirmation (CHOCH) ---
            # On attend le CHOCH Haussier
            if last_ltf_event['type'] == 'CHOCH' and last_ltf_event['trend'] == 'BULLISH':
                reason = f"ACHAT [M2]: HTF({strategy_params['htf_timeframe']}) Biais Haussier + Sweep LTF ({swept_zone['type']}) + LTF({strategy_params['ltf_timeframe']}) CHOCH Haussier."
                # SL sous le point le plus bas du sweep (le 'wick' ou la mèche)
                sl_price = ltf_swings_low[-1][1] * (1 - 0.0005) # Bas du swing CHOCH
                # --- CORRIGÉ V2.1.2: Utilisation de htf_swings_high (passé en arg) ---
                tp_price = htf_swings_high[-1][1] # Cible = High HTF
                
                if sl_price >= current_price or tp_price <= current_price:
                    logger.warning(f"[M2] Signal ACHAT ignoré: SL/TP invalide.")
                    return None, None, None, None
                
                logger.warning(f"SIGNAL TROUVÉ: {reason}")
                return "BUY", reason, sl_price, tp_price

    elif htf_trend == "BEARISH":
        # Biais HTF Baissier: On cherche un sweep d'un HAUT (EQH ou Asia High)
        swept_zone = None
        for zone in ltf_liquidity_zones:
            if zone['type'] in ["EQH", "ASIA_HIGH"]:
                # Le "sweep" : la mèche (high) d'une bougie récente est passée AU-DESSUS de la liquidité
                if ltf_data['high'].iloc[-5:].max() > zone['level']:
                    swept_zone = zone
                    break
        
        if swept_zone:
            logger.info(f"[M2] Sweep de liquidité détecté: {swept_zone['type']} @ {swept_zone['level']}")
            # --- Étape 3: Confirmation (CHOCH) ---
            if last_ltf_event['type'] == 'CHOCH' and last_ltf_event['trend'] == 'BEARISH':
                reason = f"VENTE [M2]: HTF({strategy_params['htf_timeframe']}) Biais Baissier + Sweep LTF ({swept_zone['type']}) + LTF({strategy_params['ltf_timeframe']}) CHOCH Baissier."
                sl_price = ltf_swings_high[-1][1] * (1 + 0.0005) # Haut du swing CHOCH
                # --- CORRIGÉ V2.1.2: Utilisation de htf_swings_low (passé en arg) ---
                tp_price = htf_swings_low[-1][1] # Cible = Low HTF
                
                if sl_price <= current_price or tp_price >= current_price:
                    logger.warning(f"[M2] Signal VENTE ignoré: SL/TP invalide.")
                    return None, None, None, None

                logger.warning(f"SIGNAL TROUVÉ: {reason}")
                return "SELL", reason, sl_price, tp_price

    return None, None, None, None


# --- ORCHESTRATEUR DE SIGNAUX (M1 & M2) (MODIFIÉ V2.1.2) ---
def check_all_smc_signals(mtf_data: dict, config: dict, pip_size: float): 
    """
    Orchestre la vérification de tous les modèles de signaux SMC (M1, M2).
    
    --- MODIFIÉ V2.1.2 ---
    - Retourne 4 valeurs (signal, reason, sl_price, tp_price) pour main.py v2.1.0
    - Corrige l'appel à _check_model_2_inducement
    """
    
    try:
        # --- Étape 1: Récupérer les données et analyses communes ---
        strategy_params = config['strategy']
        htf_tf = strategy_params['htf_timeframe']
        ltf_tf = strategy_params['ltf_timeframe']
        
        htf_data = mtf_data.get(htf_tf)
        ltf_data = mtf_data.get(ltf_tf)

        if htf_data is None or ltf_data is None or htf_data.empty or ltf_data.empty:
            logger.warning(f"Données manquantes pour {htf_tf} or {ltf_tf}. Signal ignoré.")
            return None, None, None, None # 4 valeurs

        current_low = ltf_data['low'].iloc[-1]
        current_high = ltf_data['high'].iloc[-1]
        current_price = ltf_data['close'].iloc[-1]
        
        # --- Étape 2: Analyse HTF (Commune à tous les modèles) ---
        htf_swings_high, htf_swings_low = structure.find_swing_highs_lows(
            htf_data, order=strategy_params.get('htf_swing_order', 10) 
        )
        _htf_events, htf_trend = structure.identify_structure(htf_swings_high, htf_swings_low)
        
        if htf_trend not in ["BULLISH", "BEARISH"]:
            logger.info(f"Tendance HTF ({htf_tf}) non claire ({htf_trend}). Pas de signal.")
            return None, None, None, None # 4 valeurs
            
        logger.info(f"Tendance HTF ({htf_tf}) confirmée : {htf_trend}")

        # --- Étape 3: Analyse LTF (Commune à tous les modèles) ---
        ltf_swings_high, ltf_swings_low = structure.find_swing_highs_lows(
            ltf_data, order=strategy_params.get('ltf_swing_order', 5) 
        )
        ltf_events, ltf_trend = structure.identify_structure(ltf_swings_high, ltf_swings_low)

        if not ltf_swings_high or not ltf_swings_low:
             logger.info("Pas assez de points de structure LTF. En attente...")
             return None, None, None, None # 4 valeurs

        # --- Étape 4: Vérifier les modèles en séquence ---

        # 4a. Vérifier Modèle 1 (Sniper OTE - Stratégie principale)
        signal_m1 = _check_model_1_confirmation(
            htf_trend, htf_data, ltf_data, htf_swings_high, htf_swings_low,
            ltf_events, ltf_swings_high, ltf_swings_low, current_price, config
        )
        if signal_m1[0]:
            return signal_m1 # Signal trouvé !

        # 4b. Vérifier Modèle 2 (Inducement/Sweep Agressif)
        # --- CORRIGÉ V2.1.2: Ajout des 3 arguments manquants ---
        signal_m2 = _check_model_2_inducement(
            htf_trend, ltf_data, ltf_events, ltf_swings_high, ltf_swings_low,
            htf_swings_high, htf_swings_low, # <-- AJOUT
            current_low, current_high, current_price, config, # <-- AJOUT current_price
            pip_size
        )
        # --- FIN CORRECTION V2.1.2 ---
        if signal_m2[0]:
            return signal_m2 # Signal trouvé !

    except Exception as e:
        logger.error(f"Erreur majeure dans l'orchestrateur de signaux: {e}", exc_info=True)
        return None, None, None, None # 4 valeurs
    
    # Si aucun modèle n'a trouvé de signal
    logger.debug("Aucun modèle SMC (M1/M2) n'a trouvé de signal valide pour le moment.")
    return None, None, None, None # 4 valeurs


# --- MODÈLE 3 (MODIFIÉ V2.1.1) ---
def check_model_3_opening_range(
    opening_range_data: pd.DataFrame,
    entry_tf_data: pd.DataFrame,
    config: dict,
    opening_range_tf_str: str,
    entry_tf_str: str,
    pip_size: float # Argument requis
) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """
    Vérifie le "Modèle 3: Opening Range Retracement" (Logique Vidéos 7 & 11)
    Range (M30/M5) -> Breakout/Displacement avec FVG (M5/M1) -> Retour au FVG -> Entrée Marché
    
    --- MODIFIÉ V2.1.1 ---
    - Retourne 4 valeurs (signal, reason, sl_price, tp_price) pour main.py v2.1.0
    - Logique de retour au FVG (de v2.1.0) conservée.
    """
    
    try:
        strategy_params = config['strategy']
        models_cfg = strategy_params.get('models', {})
        model_3_rr = models_cfg.get('model_3_rr', 2.0)
        
        if opening_range_data.empty or entry_tf_data.empty:
            logger.debug("[M3] Données de range ou d'entrée manquantes.")
            return None, None, None, None # 4 valeurs
            
        # 1. Définir le Range (basé sur la *dernière* bougie M30/M5 fournie)
        if len(opening_range_data) < 2:
             logger.debug("[M3] Pas assez de bougies de range pour analyse.")
             return None, None, None, None # 4 valeurs
             
        range_candle = opening_range_data.iloc[-2] # L'avant-dernière, la dernière est en cours
        range_high = range_candle['high']
        range_low = range_candle['low']
        logger.info(f"[M3] Range {opening_range_tf_str} défini: H={range_high}, L={range_low}")
        
        # 2. Vérifier le Breakout sur la dernière bougie d'entrée
        if len(entry_tf_data) < 2:
            logger.debug("[M3] Pas assez de bougies d'entrée pour analyse.")
            return None, None, None, None # 4 valeurs
        
        breakout_candle = entry_tf_data.iloc[-2] # L'avant-dernière
        current_candle = entry_tf_data.iloc[-1] # La bougie actuelle
        current_price = current_candle['close']
        
        is_bullish_breakout = breakout_candle['close'] > range_high
        is_bearish_breakout = breakout_candle['close'] < range_low
        
        if not is_bullish_breakout and not is_bearish_breakout:
            logger.debug("[M3] Pas de clôture de breakout M5/M1 pour le moment.")
            return None, None, None, None # 4 valeurs

        # 3. Confirmer avec Imbalance (FVG)
        # On cherche un FVG créé PENDANT ou JUSTE APRES le breakout
        recent_entry_data = entry_tf_data.iloc[-10:] # Cherche FVG sur les 10 dernières bougies
        all_fvgs = patterns.find_fvgs(recent_entry_data)
        
        if not all_fvgs:
            logger.info("[M3] Breakout détecté, mais PAS d'Imbalance (FVG) de confirmation.")
            return None, None, None, None # 4 valeurs

        # 4. Générer le Signal (sur retour au FVG)
        if is_bullish_breakout:
            # Cherche le FVG haussier le plus proche créé par le breakout
            target_fvg = None
            for fvg in reversed(all_fvgs):
                if fvg['type'] == 'BULLISH' and fvg['bottom'] >= range_high:
                    target_fvg = fvg
                    break
            
            if target_fvg:
                # --- Étape 5: Vérifier si le prix revient dans le FVG ---
                if (current_price <= target_fvg['top']) and (current_price >= target_fvg['bottom']):
                    logger.info(f"[M3] Breakout Haussier + Retour au FVG ({target_fvg['top']}-{target_fvg['bottom']}).")
                    
                    sl_price = range_low # SL de l'autre côté du range
                    
                    risk_pips = (current_price - sl_price) / pip_size
                    if risk_pips <= 0: return None, None, None, None
                    
                    tp_price = current_price + (risk_pips * model_3_rr * pip_size)
                    reason = f"ACHAT [M3]: {opening_range_tf_str} Breakout Haussier + {entry_tf_str} Retour FVG."
                    
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "BUY", reason, sl_price, tp_price

        elif is_bearish_breakout:
            # Cherche le FVG baissier le plus proche créé par le breakout
            target_fvg = None
            for fvg in reversed(all_fvgs):
                if fvg['type'] == 'BEARISH' and fvg['top'] <= range_low:
                    target_fvg = fvg
                    break

            if target_fvg:
                # --- Étape 5: Vérifier si le prix revient dans le FVG ---
                if (current_price <= target_fvg['top']) and (current_price >= target_fvg['bottom']):
                    logger.info(f"[M3] Breakout Baissier + Retour au FVG ({target_fvg['top']}-{target_fvg['bottom']}).")

                    sl_price = range_high # SL de l'autre côté du range

                    risk_pips = (sl_price - current_price) / pip_size
                    if risk_pips <= 0: return None, None, None, None

                    tp_price = current_price - (risk_pips * model_3_rr * pip_size)
                    reason = f"VENTE [M3]: {opening_range_tf_str} Breakout Baissier + {entry_tf_str} Retour FVG."
                    
                    logger.warning(f"SIGNAL TROUVÉ: {reason}")
                    return "SELL", reason, sl_price, tp_price

    except Exception as e:
        logger.error(f"Erreur majeure dans la logique du Modèle 3: {e}", exc_info=True)
        return None, None, None, None # 4 valeurs

    logger.debug("[M3] Aucune condition de retour au FVG valide trouvée.")
    return None, None, None, None # 4 valeurs
"""
Fichier: src/analysis/market_structure.py
Version: 1.0.0
Description: Module d'analyse de la structure de marché (SMC).
             Identifie les points pivots (swings), les BOS et les CHOCH.
"""

import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
import logging

logger = logging.getLogger(__name__)

def find_swing_highs_lows(data: pd.DataFrame, order: int = 5):
    """
    Identifie les points de swing (highs et lows) dans les données de marché.
    
    Args:
        data (pd.DataFrame): DataFrame avec 'high' et 'low'.
        order (int): Le nombre de bougies de chaque côté à considérer.

    Returns:
        tuple: (list_of_swing_highs, list_of_swing_lows)
               Chaque élément est un tuple (index, prix).
    """
    
    try:
        # Utilise scipy pour trouver les indices des extrema locaux
        high_indices = argrelextrema(data['high'].values, np.greater_equal, order=order)[0]
        low_indices = argrelextrema(data['low'].values, np.less_equal, order=order)[0]

        # Filtre pour ne garder que les points valides (ignorer les bords)
        valid_high_indices = [i for i in high_indices if i >= order and i < len(data) - order]
        valid_low_indices = [i for i in low_indices if i >= order and i < len(data) - order]

        # Formatte la sortie en (index, prix)
        swing_highs = [(data.index[i], data['high'].iloc[i]) for i in valid_high_indices]
        swing_lows = [(data.index[i], data['low'].iloc[i]) for i in valid_low_indices]

        return swing_highs, swing_lows
    
    except Exception as e:
        logger.error(f"Erreur dans find_swing_highs_lows: {e}", exc_info=True)
        return [], []

def identify_structure(swing_highs: list, swing_lows: list):
    """
    Analyse la séquence de swings pour identifier la tendance, les BOS et les CHOCH.

    Args:
        swing_highs (list): Liste de tuples (index, prix) des swing highs.
        swing_lows (list): Liste de tuples (index, prix) des swing lows.

    Returns:
        tuple: (list_of_structure_events, str_current_trend, last_significant_high, last_significant_low)
    """
    
    all_swings = sorted(swing_highs + swing_lows, key=lambda x: x[0])
    structure_events = []
    current_trend = "SIDEWAYS"
    
    last_high = None
    last_low = None
    last_significant_high = None
    last_significant_low = None

    if not all_swings:
        return [], "SIDEWAYS", None, None

    # Initialisation
    first_swing = all_swings[0]
    if first_swing in swing_highs:
        last_high = first_swing
        last_significant_high = first_swing
    else:
        last_low = first_swing
        last_significant_low = first_swing

    for i in range(1, len(all_swings)):
        current_swing = all_swings[i]
        is_high = current_swing in swing_highs

        if is_high:
            last_high = current_swing
            if last_significant_high is None:
                last_significant_high = current_swing
                continue

            # Scénario Haussier
            if current_trend == "BULLISH":
                if current_swing[1] > last_significant_high[1]:
                    # Break of Structure (BOS) Haussier
                    structure_events.append({
                        "type": "BOS", "trend": "BULLISH",
                        "level": last_significant_high[1], "timestamp": current_swing[0]
                    })
                    last_significant_high = current_swing
                    if last_low and (last_significant_low is None or last_low[0] > last_significant_low[0]):
                         last_significant_low = last_low

            # Scénario Baissier
            elif current_trend == "BEARISH":
                if current_swing[1] > last_significant_high[1]:
                    # Change of Character (CHOCH) Haussier
                    structure_events.append({
                        "type": "CHOCH", "trend": "BULLISH",
                        "level": last_significant_high[1], "timestamp": current_swing[0]
                    })
                    current_trend = "BULLISH"
                    last_significant_high = current_swing
                    if last_low: 
                        last_significant_low = last_low
            
            # Initialisation Tendance
            elif current_trend == "SIDEWAYS" and last_significant_low:
                if current_swing[1] > last_significant_high[1]:
                    current_trend = "BULLISH"
                    last_significant_high = current_swing

        else: # C'est un Swing Low
            last_low = current_swing
            if last_significant_low is None:
                last_significant_low = current_swing
                continue

            # Scénario Haussier
            if current_trend == "BULLISH":
                if current_swing[1] < last_significant_low[1]:
                    # Change of Character (CHOCH) Baissier
                    structure_events.append({
                        "type": "CHOCH", "trend": "BEARISH",
                        "level": last_significant_low[1], "timestamp": current_swing[0]
                    })
                    current_trend = "BEARISH"
                    last_significant_low = current_swing
                    if last_high: 
                        last_significant_high = last_high

            # Scénario Baissier
            elif current_trend == "BEARISH":
                if current_swing[1] < last_significant_low[1]:
                    # Break of Structure (BOS) Baissier
                    structure_events.append({
                        "type": "BOS", "trend": "BEARISH",
                        "level": last_significant_low[1], "timestamp": current_swing[0]
                    })
                    last_significant_low = current_swing
                    if last_high and (last_significant_high is None or last_high[0] > last_significant_high[0]):
                        last_significant_high = last_high
            
            # Initialisation Tendance
            elif current_trend == "SIDEWAYS" and last_significant_high:
                if current_swing[1] < last_significant_low[1]:
                    current_trend = "BEARISH"
                    last_significant_low = current_swing

    return structure_events, current_trend, last_significant_high, last_significant_low
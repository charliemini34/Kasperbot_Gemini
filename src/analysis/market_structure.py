"""
Module pour l'analyse de la structure de marché (SMC).

Ce module contient les fonctions nécessaires pour identifier les points pivots (swing highs/lows)
et pour détecter la structure du marché (BOS, CHOCH) basée sur ces points.

Version: 1.0.1
"""

__version__ = "1.0.1"

import pandas as pd
import numpy as np
from scipy.signal import argrelextrema

def find_swing_highs_lows(data: pd.DataFrame, order: int = 5):
    """
    Identifie les points de swing (highs et lows) dans les données de marché.
    
    Un swing high est un pic plus haut que les 'order' bougies précédentes et suivantes.
    Un swing low est un creux plus bas que les 'order' bougies précédentes et suivantes.

    Args:
        data (pd.DataFrame): DataFrame contenant les données de marché (doit avoir 'high' et 'low').
        order (int): Le nombre de bougies de chaque côté à considérer pour définir un pic/creux.

    Returns:
        tuple: (list_of_swing_highs, list_of_swing_lows)
               Chaque élément dans les listes est un tuple (index, prix).
    """
    
    # --- Ajout Robustesse v1.0.1 ---
    # Vérifie si les données sont suffisantes pour l'analyse, évite un crash de scipy
    if len(data) < (2 * order + 1):
        # Pas assez de données pour trouver des extrema avec l'ordre donné
        return [], []
    # --- Fin Ajout ---

    # Utilise scipy pour trouver les indices des extrema locaux
    high_indices = argrelextrema(data['high'].values, np.greater_equal, order=order)[0]
    low_indices = argrelextrema(data['low'].values, np.less_equal, order=order)[0]

    # --- Commentaire de Validation (v1.0.1) ---
    # Le filtre ci-dessous est crucial pour une stratégie non-repainting.
    # 'i >= order' : Assure qu'on a 'order' bougies *avant* le point.
    # 'i < len(data) - order' : Assure qu'on a 'order' bougies *après* le point.
    # Cela signifie qu'un swing n'est confirmé qu'après 'order' bougies,
    # introduisant un "lag" nécessaire pour garantir que le point est définitif.
    # --- Fin Commentaire ---
    
    # Filtre pour ne garder que les points valides (ignorer les bords si nécessaire)
    high_indices = [i for i in high_indices if i >= order and i < len(data) - order]
    low_indices = [i for i in low_indices if i >= order and i < len(data) - order]

    # Formatte la sortie en (index, prix)
    swing_highs = [(data.index[i], data['high'].iloc[i]) for i in high_indices]
    swing_lows = [(data.index[i], data['low'].iloc[i]) for i in low_indices]

    return swing_highs, swing_lows

def identify_structure(swing_highs: list, swing_lows: list):
    """
    Analyse la séquence de swing highs et lows pour identifier la tendance
    et les événements de structure de marché (BOS et CHOCH).

    Args:
        swing_highs (list): Liste de tuples (index, prix) des swing highs.
        swing_lows (list): Liste de tuples (index, prix) des swing lows.

    Returns:
        tuple: (list_of_structure_events, str_current_trend)
               - list_of_structure_events: Liste de dictionnaires (type, level, timestamp).
               - str_current_trend: "BULLISH", "BEARISH", ou "SIDEWAYS".
    """
    
    # Combine et trie tous les points de swing par date (index)
    all_swings = sorted(swing_highs + swing_lows, key=lambda x: x[0])

    structure_events = []
    current_trend = "SIDEWAYS"
    
    last_high = None
    last_low = None
    last_significant_high = None
    last_significant_low = None

    if not all_swings:
        return [], "SIDEWAYS"

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
                        "type": "BOS",
                        "trend": "BULLISH",
                        "level": last_significant_high[1],
                        "timestamp": current_swing[0]
                    })
                    last_significant_high = current_swing
                    # Le dernier plus bas qui a créé ce nouveau plus haut devient le "low" protégé
                    if last_low and last_significant_low and last_low[0] > last_significant_low[0]:
                         last_significant_low = last_low

            # Scénario Baissier
            elif current_trend == "BEARISH":
                if current_swing[1] > last_significant_high[1]:
                    # Change of Character (CHOCH) Haussier
                    structure_events.append({
                        "type": "CHOCH",
                        "trend": "BULLISH",
                        "level": last_significant_high[1],
                        "timestamp": current_swing[0]
                    })
                    current_trend = "BULLISH"
                    last_significant_high = current_swing
                    if last_low: # Le point bas d'où part le CHOCH
                        last_significant_low = last_low
            
            # Initialisation Tendance
            elif current_trend == "SIDEWAYS" and last_significant_low:
                if last_significant_high is None: # Cas où on n'a que des lows
                   last_significant_high = current_swing
                elif current_swing[1] > last_significant_high[1]:
                    current_trend = "BULLISH" # Première tendance établie
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
                        "type": "CHOCH",
                        "trend": "BEARISH",
                        "level": last_significant_low[1],
                        "timestamp": current_swing[0]
                    })
                    current_trend = "BEARISH"
                    last_significant_low = current_swing
                    if last_high: # Le point haut d'où part le CHOCH
                        last_significant_high = last_high

            # Scénario Baissier
            elif current_trend == "BEARISH":
                if current_swing[1] < last_significant_low[1]:
                    # Break of Structure (BOS) Baissier
                    structure_events.append({
                        "type": "BOS",
                        "trend": "BEARISH",
                        "level": last_significant_low[1],
                        "timestamp": current_swing[0]
                    })
                    last_significant_low = current_swing
                    # Le dernier plus haut qui a créé ce nouveau plus bas devient le "high" protégé
                    if last_high and last_significant_high and last_high[0] > last_significant_high[0]:
                        last_significant_high = last_high
            
            # Initialisation Tendance
            elif current_trend == "SIDEWAYS" and last_significant_high:
                if last_significant_low is None: # Cas où on n'a que des highs
                    last_significant_low = current_swing
                elif current_swing[1] < last_significant_low[1]:
                    current_trend = "BEARISH" # Première tendance établie
                    last_significant_low = current_swing

    return structure_events, current_trend
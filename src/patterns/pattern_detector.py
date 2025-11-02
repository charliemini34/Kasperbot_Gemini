"""
Module pour la détection des patterns SMC (Fair Value Gaps, Order Blocks)
et des zones de liquidité (EQH/EQL, Session Ranges).

Version: 1.0.1
"""

__version__ = "1.0.1"

import pandas as pd
import numpy as np
# --- Ajouts v1.0.1 ---
from typing import List, Dict, Any, Optional
from datetime import time, datetime
import pytz
# --- Fin Ajouts ---


# --- NOUVELLE FONCTION v1.0.1 ---
def find_equal_highs_lows(data: pd.DataFrame, lookback: int = 20, tolerance_pips: float = 5.0, pip_size: float = 0.0001) -> Dict[str, List[Dict[str, Any]]]:
    """
    Identifie les zones de liquidité "Equal Highs" (EQH) et "Equal Lows" (EQL)
    sur une période de lookback récente.

    Args:
        data (pd.DataFrame): DataFrame avec 'high', 'low'.
        lookback (int): Nombre de bougies récentes à analyser.
        tolerance_pips (float): L'écart maximal en pips pour considérer deux mèches "égales".
        pip_size (float): La taille d'un pip (ex: 0.0001 pour EURUSD).

    Returns:
        Dict: Un dictionnaire contenant les listes 'equal_highs' et 'equal_lows'.
              Chaque élément est un dict {'level': prix, 'timestamps': [dates...]}
    """
    if len(data) < lookback:
        return {"equal_highs": [], "equal_lows": []}

    recent_data = data.iloc[-lookback:]
    tolerance = tolerance_pips * pip_size

    # Equal Highs (EQH)
    max_high = recent_data['high'].max()
    eqh_candles = recent_data[abs(recent_data['high'] - max_high) <= tolerance]
    
    equal_highs = []
    if len(eqh_candles) > 1: # Plus d'une mèche touche ce niveau
        equal_highs.append({
            "level": max_high,
            "timestamps": eqh_candles.index.tolist()
        })

    # Equal Lows (EQL)
    min_low = recent_data['low'].min()
    eql_candles = recent_data[abs(recent_data['low'] - min_low) <= tolerance]
    
    equal_lows = []
    if len(eql_candles) > 1: # Plus d'une mèche touche ce niveau
        equal_lows.append({
            "level": min_low,
            "timestamps": eql_candles.index.tolist()
        })

    return {"equal_highs": equal_highs, "equal_lows": equal_lows}


# --- NOUVELLE FONCTION v1.0.1 ---
def find_session_range(data: pd.DataFrame, 
                       session_start_hour: int, 
                       session_end_hour: int, 
                       timezone: str = 'Etc/UTC') -> Optional[Dict[str, Any]]:
    """
    Identifie le plus haut et le plus bas d'une session de trading spécifique
    (ex: Asia Range) pour la journée la plus récente dans les données.

    Args:
        data (pd.DataFrame): Données de marché avec un index DatetimeIndex.
        session_start_hour (int): Heure de début de la session (ex: 0 pour minuit).
        session_end_hour (int): Heure de fin de la session (ex: 8 pour 8h00).
        timezone (str): Le fuseau horaire à utiliser pour définir la session (ex: 'Etc/UTC', 'Asia/Tokyo').

    Returns:
        Optional[Dict]: Un dictionnaire avec 'high', 'low', 'start_time', 'end_time'
                        ou None si les données ne sont pas suffisantes.
    """
    if data.empty:
        return None

    try:
        tz = pytz.timezone(timezone)
    except pytz.UnknownTimeZoneError:
        print(f"Erreur: Fuseau horaire '{timezone}' inconnu. Utilisation de 'Etc/UTC'.")
        tz = pytz.timezone('Etc/UTC')

    # S'assurer que l'index est localisé dans le bon fuseau horaire
    if data.index.tzinfo is None:
        try:
            # Assumons que les données MT5 sont en UTC si non spécifié
            data.index = data.index.tz_localize('Etc/UTC').tz_convert(tz)
        except Exception as e:
            # Gérer le cas où l'index est déjà localisé mais pas explicitement (rare)
            data.index = data.index.tz_convert(tz)
    else:
        data.index = data.index.tz_convert(tz)

    # Définir les heures de début et de fin
    start_time = time(session_start_hour, 0)
    end_time = time(session_end_hour, 0)
    
    # Trouver la date la plus récente dans les données
    latest_date = data.index.max().date()

    # Sélectionner les données de la session pour la date la plus récente
    # Note: Gère le cas où la session (ex: 00:00-08:00) est sur une seule journée
    if start_time < end_time:
        session_data = data.between_time(start_time, end_time)
        session_data = session_data[session_data.index.date == latest_date]
    else:
        # Gère les sessions qui chevauchent minuit (ex: 22:00 - 06:00)
        # Non implémenté pour rester simple, focus sur Asia Range (00:00-08:00)
         session_data = data.between_time(start_time, end_time) # Ne fonctionnera pas correctement
         print("Avertissement: Les ranges de session chevauchant minuit ne sont pas gérés.")
         # Pour l'instant, on se concentre sur le cas simple (ex: 00h-08h)
         session_data = data[(data.index.time >= start_time) | (data.index.time < end_time)]
         # Logique complexe de date nécessaire ici, simplification pour l'instant:
         session_data = data.between_time(start_time, end_time)
         session_data = session_data[session_data.index.date == latest_date]


    if session_data.empty:
        return None

    session_high = session_data['high'].max()
    session_low = session_data['low'].min()
    
    return {
        "high": session_high,
        "low": session_low,
        "start_time": session_data.index.min(),
        "end_time": session_data.index.max()
    }


# --- CODE ORIGINAL (NON MODIFIÉ) ---

def find_fvgs(data: pd.DataFrame):
    """
    Identifie les Fair Value Gaps (FVG) / Imbalances dans les données.
    Un FVG est identifié là où la mèche basse de la bougie N-1 ne touche pas
    la mèche haute de la bougie N+1 (pour un FVG haussier/bullish), et vice-versa.

    Args:
        data (pd.DataFrame): DataFrame avec 'high', 'low'.

    Returns:
        list: Liste de dictionnaires pour les FVGs trouvés.
              Ex: [{'type': 'BULLISH', 'top': 1.1000, 'bottom': 1.0990, 'timestamp': ...}]
    """
    fvgs = []
    
    # Vectorisation pour la vitesse
    highs = data['high']
    lows = data['low']
    
    # Décalages pour comparer N-1, N, et N+1
    prev_high = highs.shift(1)
    next_low = lows.shift(-1)
    
    prev_low = lows.shift(1)
    next_high = highs.shift(-1)

    # Condition Bullish FVG: low[N] > high[N-1] ET high[N+1] < low[N]
    # Non, la définition SMC est : high[N+1] < low[N-1] (pour un FVG baissier)
    # Et low[N+1] > high[N-1] (pour un FVG haussier)
    
    # --- Bullish FVG (Gap entre high[N-1] et low[N+1]) ---
    # La bougie N est la bougie qui crée le FVG
    # Le FVG est l'espace entre le high de N-1 et le low de N+1
    bullish_fvg_condition = (lows.shift(-1) > highs.shift(1)) & (highs > highs.shift(1)) & (highs.shift(-1) > lows.shift(-1))
    
    # --- Bearish FVG (Gap entre low[N-1] et high[N+1]) ---
    # Le FVG est l'espace entre le low de N-1 et le high de N+1
    bearish_fvg_condition = (highs.shift(-1) < lows.shift(1)) & (lows < lows.shift(1)) & (lows.shift(-1) < highs.shift(-1))

    bullish_indices = data[bullish_fvg_condition].index
    bearish_indices = data[bearish_fvg_condition].index

    for i in bullish_indices:
        # L'index 'i' est la bougie N. Le FVG est créé par (N-1), N, (N+1).
        # Le FVG se situe sur la bougie N, entre high[N-1] et low[N+1]
        try:
            fvg_top = data.loc[i.shift(1), 'high']
            fvg_bottom = data.loc[i.shift(-1), 'low']
            # Correction: FVG Bullish: top = low[N+1], bottom = high[N-1]
            fvg_top = data.loc[data.index[data.index.get_loc(i)+1], 'low']
            fvg_bottom = data.loc[data.index[data.index.get_loc(i)-1], 'high']

            if fvg_top > fvg_bottom: # Assure-toi que c'est bien un gap
                fvgs.append({
                    "type": "BULLISH",
                    "top": fvg_top,
                    "bottom": fvg_bottom,
                    "timestamp": i,
                    "mitigated": False # État initial
                })
        except (KeyError, IndexError):
            continue # Ignorer les bords

    for i in bearish_indices:
        # L'index 'i' est la bougie N.
        # FVG Bearish: top = low[N-1], bottom = high[N+1]
        try:
            fvg_top = data.loc[data.index[data.index.get_loc(i)-1], 'low']
            fvg_bottom = data.loc[data.index[data.index.get_loc(i)+1], 'high']
            
            if fvg_top > fvg_bottom: # Assure-toi que c'est bien un gap
                fvgs.append({
                    "type": "BEARISH",
                    "top": fvg_top,
                    "bottom": fvg_bottom,
                    "timestamp": i,
                    "mitigated": False # État initial
                })
        except (KeyError, IndexError):
            continue # Ignorer les bords
            
    # La détection ci-dessus est complexe. Une version plus simple :
    fvgs = [] # Reset pour une implémentation plus claire
    
    for i in range(1, len(data) - 1):
        prev_candle = data.iloc[i-1]
        current_candle = data.iloc[i]
        next_candle = data.iloc[i+1]
        
        # Bullish FVG (Imbalance haussière)
        # La mèche basse de la 3ème bougie (N+1) est plus haute que
        # la mèche haute de la 1ère bougie (N-1).
        if next_candle['low'] > prev_candle['high']:
            fvgs.append({
                "type": "BULLISH",
                "top": next_candle['low'],
                "bottom": prev_candle['high'],
                "timestamp_start": data.index[i-1],
                "timestamp_end": data.index[i+1],
                "mitigated": False
            })
            
        # Bearish FVG (Imbalance baissière)
        # La mèche haute de la 3ème bougie (N+1) est plus basse que
        # la mèche basse de la 1ère bougie (N-1).
        if next_candle['high'] < prev_candle['low']:
            fvgs.append({
                "type": "BEARISH",
                "top": prev_candle['low'],
                "bottom": next_candle['high'],
                "timestamp_start": data.index[i-1],
                "timestamp_end": data.index[i+1],
                "mitigated": False
            })

    return fvgs

def find_order_blocks(data: pd.DataFrame, swing_highs: list, swing_lows: list):
    """
    Identifie les Order Blocks (OB) basés sur les points de swing.
    Un OB est la dernière bougie baissière avant un swing high (Bullish OB)
    ou la dernière bougie haussière avant un swing low (Bearish OB).

    Args:
        data (pd.DataFrame): DataFrame avec 'open', 'high', 'low', 'close'.
        swing_highs (list): Liste de (index, prix) des swing highs.
        swing_lows (list): Liste de (index, prix) des swing lows.

    Returns:
        list: Liste de dictionnaires pour les OBs trouvés.
              Ex: [{'type': 'BULLISH', 'top': 1.1000, 'bottom': 1.0990, 'timestamp': ...}]
    """
    order_blocks = []
    
    # Note: Cette fonction est une ébauche simple.
    # Une détection d'OB robuste devrait aussi vérifier la présence
    # d'une imbalance (FVG) juste après l'OB, comme vu dans les vidéos.
    
    # Détection Bearish OB (basée sur les Swing Highs)
    for sh_time, sh_price in swing_highs:
        try:
            sh_index = data.index.get_loc(sh_time)
            
            # Cherche la dernière bougie haussière *avant* le swing high
            # (Simplification : on prend la bougie *du* swing high si elle est haussière,
            # ou celle d'avant)
            
            candle = data.iloc[sh_index]
            if candle['close'] > candle['open']: # Bougie haussière
                target_candle = candle
            else:
                # Si la bougie du swing high est baissière, on prend la haussière d'avant
                if sh_index > 0:
                    target_candle = data.iloc[sh_index - 1]
                else:
                    continue # Ne peut pas être la première bougie
                
            if target_candle['close'] > target_candle['open']: # Doit être haussière
                order_blocks.append({
                    "type": "BEARISH",
                    "top": target_candle['high'],
                    "bottom": target_candle['low'],
                    "timestamp": target_candle.name,
                    "mitigated": False
                })
        except (KeyError, IndexError):
            continue

    # Détection Bullish OB (basée sur les Swing Lows)
    for sl_time, sl_price in swing_lows:
        try:
            sl_index = data.index.get_loc(sl_time)
            
            candle = data.iloc[sl_index]
            if candle['close'] < candle['open']: # Bougie baissière
                target_candle = candle
            else:
                # Si la bougie du swing low est haussière, on prend la baissière d'avant
                if sl_index > 0:
                    target_candle = data.iloc[sl_index - 1] 
                else:
                    continue # Ne peut pas être la première bougie

            if target_candle['close'] < target_candle['open']: # Doit être baissière
                order_blocks.append({
                    "type": "BULLISH",
                    "top": target_candle['high'],
                    "bottom": target_candle['low'],
                    "timestamp": target_candle.name,
                    "mitigated": False
                })
        except (KeyError, IndexError):
            continue

    return order_blocks
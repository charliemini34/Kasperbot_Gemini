"""
Module pour la détection de "patterns" ou figures sur les données de marché.

Initialement, ce module était basé sur des indicateurs (EMA, RSI).
Il est maintenant adapté pour détecter les concepts SMC (Smart Money Concepts)
tels que les Imbalances (FVG), les Order Blocks (OB), et les zones de Liquidité.
"""

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# --- ANCIENNE LOGIQUE (INDICATEURS) ---
# Ces fonctions sont conservées à titre d'archive mais ne sont plus utilisées
# par la stratégie SMC.

def find_ema_crossover(data: pd.DataFrame, short_window: int, long_window: int):
    """
    Trouve les signaux de croisement d'EMA.
    (CONSERVÉ POUR ARCHIVE - NON UTILISÉ PAR SMC)
    """
    if 'close' not in data.columns:
        logger.error("La colonne 'close' est manquante pour le calcul EMA.")
        return None, None
    
    data['ema_short'] = data['close'].ewm(span=short_window, adjust=False).mean()
    data['ema_long'] = data['close'].ewm(span=long_window, adjust=False).mean()
    
    # Signal: 1 pour achat (short > long), -1 pour vente (short < long)
    data['signal'] = 0
    data.loc[data['ema_short'] > data['ema_long'], 'signal'] = 1
    data.loc[data['ema_short'] < data['ema_long'], 'signal'] = -1
    
    # Détecter le croisement
    data['prev_signal'] = data['signal'].shift(1)
    
    buy_signal = (data['signal'] == 1) & (data['prev_signal'] == -1)
    sell_signal = (data['signal'] == -1) & (data['prev_signal'] == 1)
    
    # On vérifie la dernière bougie
    if not buy_signal.empty and buy_signal.iloc[-1]:
        return "BUY", "EMA Crossover"
    if not sell_signal.empty and sell_signal.iloc[-1]:
        return "SELL", "EMA Crossover"
        
    return None, None

def find_rsi_oversold_overbought(data: pd.DataFrame, window: int, oversold: int, overbought: int):
    """
    Trouve les signaux de RSI en surachat/survente.
    (CONSERVÉ POUR ARCHIVE - NON UTILISÉ PAR SMC)
    """
    if 'close' not in data.columns:
        logger.error("La colonne 'close' est manquante pour le calcul RSI.")
        return None, None
    
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()

    # Éviter la division par zéro si 'loss' est 0
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = gain / loss
        data['rsi'] = 100 - (100 / (1 + rs))
    
    # Remplacer les infinis (si loss était 0) par 100 (RSI max)
    data['rsi'].replace([np.inf, -np.inf], 100, inplace=True)
    # Gérer les NaNs initiaux
    data['rsi'].fillna(method='bfill', inplace=True)

    if data['rsi'].empty:
        return None, None
        
    rsi_value = data['rsi'].iloc[-1]
    
    if rsi_value < oversold:
        return "BUY", f"RSI Oversold ({rsi_value:.2f})"
    if rsi_value > overbought:
        return "SELL", f"RSI Overbought ({rsi_value:.2f})"
        
    return None, None


# --- NOUVELLE LOGIQUE (SMC - SMART MONEY CONCEPTS) ---

def find_imbalances(data: pd.DataFrame) -> list:
    """
    Identifie les Imbalances (Fair Value Gaps - FVG) dans les données.
    Un FVG est un déséquilibre où les mèches de la 1ère et 3ème bougie ne se touchent pas.
    
    Args:
        data (pd.DataFrame): Données de marché avec 'high', 'low'.

    Returns:
        list: Une liste de dictionnaires, où chaque dict représente un FVG non mitigé.
              Ex: [{'type': 'BULLISH', 'top': 1.0850, 'bottom': 1.0845, 
                    'start_time': ..., 'end_time': ..., 'mitigated_at': None}]
    """
    fvgs = []
    if len(data) < 3:
        return fvgs

    # Convertir en listes pour un accès plus rapide
    highs = data['high'].values
    lows = data['low'].values
    times = data.index

    for i in range(len(data) - 2):
        candle_1_high = highs[i]
        candle_1_low = lows[i]
        candle_3_high = highs[i+2]
        candle_3_low = lows[i+2]

        fvg_info = None

        # Bullish Imbalance (FVG Haussier): Le bas de la bougie 3 est plus haut que le haut de la bougie 1
        # 
        if candle_1_high < candle_3_low:
            fvg_info = {
                'type': 'BULLISH',
                'top': candle_3_low,
                'bottom': candle_1_high,
                'start_time': times[i],
                'end_time': times[i+2]
            }
            
        # Bearish Imbalance (FVG Baissier): Le haut de la bougie 3 est plus bas que le bas de la bougie 1
        # 
        elif candle_1_low > candle_3_high:
            fvg_info = {
                'type': 'BEARISH',
                'top': candle_1_low,
                'bottom': candle_3_high,
                'start_time': times[i],
                'end_time': times[i+2]
            }

        # Si on a trouvé un FVG, on vérifie s'il est déjà mitigé
        if fvg_info:
            mitigated_at = None
            # Regarder les bougies futures (à partir de la 4ème bougie, i+3)
            for j in range(i + 3, len(data)):
                if fvg_info['type'] == 'BULLISH':
                    # Mitigé si un 'low' futur touche le 'top' du FVG
                    if lows[j] <= fvg_info['top']:
                        mitigated_at = times[j]
                        break 
                elif fvg_info['type'] == 'BEARISH':
                    # Mitigé si un 'high' futur touche le 'bottom' du FVG
                    if highs[j] >= fvg_info['bottom']:
                        mitigated_at = times[j]
                        break
            
            fvg_info['mitigated_at'] = mitigated_at
            fvgs.append(fvg_info)
            
    logger.debug(f"Trouvé {len(fvgs)} FVG au total.")
    return fvgs

def find_order_blocks(data: pd.DataFrame) -> list:
    """
    Identifie les Order Blocks (OB) potentiels.
    Définition simple : La dernière bougie inverse avant un mouvement impulsif.

    Args:
        data (pd.DataFrame): Données de marché avec 'open', 'close', 'high', 'low'.

    Returns:
        list: Une liste de dictionnaires, où chaque dict représente un OB.
              Ex: [{'type': 'BULLISH', 'top': 1.0850, 'bottom': 1.0840, 'time': ...}]
    """
    obs = []
    if len(data) < 2:
        return obs
    
    opens = data['open'].values
    closes = data['close'].values
    highs = data['high'].values
    lows = data['low'].values
    times = data.index

    for i in range(1, len(data)):
        prev_open = opens[i-1]
        prev_close = closes[i-1]
        prev_high = highs[i-1]
        prev_low = lows[i-1]
        
        curr_open = opens[i]
        curr_close = closes[i]
        curr_low = lows[i]
        curr_high = highs[i]

        # Bullish OB (Haussier): Une bougie baissière (close < open)
        # suivie d'une bougie haussière impulsive qui "engloutit" la précédente.
        # 
        if (prev_close < prev_open and      # Bougie 1 est baissière
            curr_close > curr_open and      # Bougie 2 est haussière
            curr_close > prev_high):      # Bougie 2 casse le haut de la bougie 1 (signe d'impulsion)
            
            ob = {
                'type': 'BULLISH',
                'top': prev_high,
                'bottom': prev_low,
                'time': times[i-1]
            }
            obs.append(ob)

        # Bearish OB (Baissier): Une bougie haussière (close > open)
        # suivie d'une bougie baissière impulsive qui "engloutit" la précédente.
        # 
        elif (prev_close > prev_open and    # Bougie 1 est haussière
              curr_close < curr_open and    # Bougie 2 est baissière
              curr_close < prev_low):     # Bougie 2 casse le bas de la bougie 1 (signe d'impulsion)
            
            ob = {
                'type': 'BEARISH',
                'top': prev_high,
                'bottom': prev_low,
                'time': times[i-1]
            }
            obs.append(ob)

    logger.debug(f"Trouvé {len(obs)} OB potentiels.")
    return obs


def find_liquidity_zones(swing_highs: list, swing_lows: list, tolerance_percent: float = 0.001) -> dict:
    """
    Identifie les zones de liquidité (Equal Highs/Lows) à partir des listes de swing points.
    Regroupe les points de swing qui sont très proches en prix.

    Args:
        swing_highs (list): Liste de tuples (timestamp, prix) de src.analysis.market_structure
        swing_lows (list): Liste de tuples (timestamp, prix) de src.analysis.market_structure
        tolerance_percent (float): Le pourcentage d'écart pour considérer
                                   deux points comme "égaux". 0.001 = 0.1%

    Returns:
        dict: Contenant 'eqh' (Equal Highs) et 'eql' (Equal Lows).
              Chaque zone est un dict {'level': prix_moyen, 'points': [liste_des_points]}
    """
    
    # Fonction d'aide pour regrouper les points proches
    def group_nearby_prices(points: list, tolerance: float) -> list:
        if not points:
            return []
        
        # Trie les points par prix pour un regroupement facile
        sorted_points = sorted(points, key=lambda x: x[1])
        
        groups = []
        if not sorted_points:
            return groups

        current_group = [sorted_points[0]]
        
        for i in range(1, len(sorted_points)):
            point = sorted_points[i]
            price = point[1]
            # Calcule la moyenne du groupe actuel
            group_avg = np.mean([p[1] for p in current_group])
            
            # Si le nouveau point est proche de la moyenne du groupe
            if abs(price - group_avg) / group_avg < tolerance:
                current_group.append(point)
            else:
                # Si le groupe a plus d'un point (donc "Equal"), on le garde
                if len(current_group) > 1:
                    groups.append({
                        'level': np.mean([p[1] for p in current_group]),
                        'points': current_group
                    })
                # Commencer un nouveau groupe
                current_group = [point]
        
        # Ne pas oublier le dernier groupe
        if len(current_group) > 1:
            groups.append({
                'level': np.mean([p[1] for p in current_group]),
                'points': current_group
            })
            
        return groups

    eqh_zones = group_nearby_prices(swing_highs, tolerance_percent)
    eql_zones = group_nearby_prices(swing_lows, tolerance_percent)

    logger.debug(f"Trouvé {len(eqh_zones)} zones EQH et {len(eql_zones)} zones EQL.")
    # 
    return {'eqh': eqh_zones, 'eql': eql_zones}
# Fichier: src/patterns/pattern_detector.py
"""
Module pour la détection des patterns SMC (Fair Value Gaps, Order Blocks)
et des zones de liquidité (EQH/EQL, Session Ranges).

Version: 2.2.0
"""

__version__ = "2.2.0"

import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
from datetime import time, datetime
import pytz
import logging 

# Ajout d'un logger pour ce module
logger = logging.getLogger(__name__)

# --- FONCTIONS DE LIQUIDITÉ ---

def find_equal_highs_lows(data: pd.DataFrame, lookback: int = 20, tolerance_pips: float = 5.0, pip_size: float = 0.0001) -> Dict[str, List[Dict[str, Any]]]:
    """
    Identifie les zones de liquidité "Equal Highs" (EQH) et "Equal Lows" (EQL)
    sur une période de lookback récente.
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


def find_session_range(data: pd.DataFrame, 
                       session_start_hour: int, 
                       session_end_hour: int, 
                       timezone: str = 'Etc/UTC') -> Optional[Dict[str, Any]]:
    """
    Identifie le plus haut et le plus bas d'une session de trading spécifique
    (ex: Asia Range) pour la journée la plus récente dans les données.
    
    --- MODIFIÉ V2.2.0 ---
    Gère désormais correctement les sessions qui chevauchent minuit (ex: 20:00 - 00:00)
    """
    if data.empty:
        return None

    try:
        tz = pytz.timezone(timezone)
    except pytz.UnknownTimeZoneError:
        logger.warning(f"Fuseau horaire '{timezone}' inconnu. Utilisation de 'Etc/UTC'.")
        tz = pytz.timezone('Etc/UTC')

    # S'assurer que l'index est localisé dans le bon fuseau horaire
    if data.index.tzinfo is None:
        try:
            # Assumons que les données MT5 sont en UTC si non spécifié
            data.index = data.index.tz_localize('Etc/UTC').tz_convert(tz)
        except Exception as e:
            # Gérer le cas où les données sont déjà localisées (par ex. lors de re-conversions)
            data.index = data.index.tz_convert(tz)
    else:
        data.index = data.index.tz_convert(tz)

    start_time = time(session_start_hour, 0)
    end_time = time(session_end_hour, 0)
    
    # Le "dernier jour" est la date de la dernière bougie des données
    latest_date = data.index.max().date()

    if start_time < end_time:
        # Cas simple: Session sur un seul jour (ex: 08:00 - 16:00)
        session_data = data.between_time(start_time, end_time)
        # On ne prend que les données du dernier jour complet disponible
        session_data = session_data[session_data.index.date == latest_date]
    else:
        # --- CORRECTION V2.2.0: Gère les sessions qui chevauchent minuit ---
        # (ex: 20:00 - 00:00 ou 20:00 - 06:00)
        # On a besoin des données de la veille (pour le début) et du jour J (pour la fin)
        yesterday_date = latest_date - pd.Timedelta(days=1)
        
        # Données du jour J-1 (ex: 20:00 à 23:59:59)
        data_yesterday = data.between_time(start_time, time(23, 59, 59))
        data_yesterday = data_yesterday[data_yesterday.index.date == yesterday_date]
        
        # Données du jour J (ex: 00:00:00 à end_time)
        data_today = data.between_time(time(0, 0, 0), end_time)
        data_today = data_today[data_today.index.date == latest_date]

        # Concaténer les deux parties pour former la session complète
        session_data = pd.concat([data_yesterday, data_today])
        # --- FIN CORRECTION V2.2.0 ---

    if session_data.empty:
        logger.debug(f"[find_session_range] Aucune donnée trouvée pour la session {start_time}-{end_time} à la date {latest_date}")
        return None

    session_high = session_data['high'].max()
    session_low = session_data['low'].min()
    
    return {
        "high": session_high,
        "low": session_low,
        "start_time": session_data.index.min(),
        "end_time": session_data.index.max()
    }


# --- DÉTECTION FVG / OB ---

def find_fvgs(data: pd.DataFrame):
    """
    Identifie les Fair Value Gaps (FVG) / Imbalances dans les données.
    (Version itérative robuste)
    """
    fvgs = [] 
    
    for i in range(1, len(data) - 1):
        prev_candle = data.iloc[i-1]
        # current_candle = data.iloc[i] # Non utilisé
        next_candle = data.iloc[i+1]
        
        # Bullish FVG (Imbalance haussière)
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
    
    --- MODIFIÉ V2.1.0 ---
    Ajoute la vérification de la création d'un FVG (Imbalance) 
    lors du mouvement impulsif qui suit le swing, comme vu dans la Vidéo 1.
    """
    order_blocks = []
    
    # Détection Bearish OB (basée sur les Swing Highs)
    for sh_time, sh_price in swing_highs:
        try:
            sh_index = data.index.get_loc(sh_time)
            
            candle = data.iloc[sh_index]
            target_candle = None # Initialisation
            
            if candle['close'] > candle['open']: # Bougie haussière (est l'OB)
                target_candle = candle
            else:
                if sh_index > 0:
                    prev_candle = data.iloc[sh_index - 1]
                    if prev_candle['close'] > prev_candle['open']: # Bougie haussière précédente (est l'OB)
                        target_candle = prev_candle
                else:
                    continue
                
            if target_candle is not None:
                
                # --- AJOUT V2.1.0: Vérification FVG ---
                has_fvg = False
                # Le mouvement impulsif commence après le Swing High (sh_index)
                if sh_index + 3 < len(data):
                    fvg_candle_1 = data.iloc[sh_index + 1]
                    # fvg_candle_2 = data.iloc[sh_index + 2] # Bougie du milieu
                    fvg_candle_3 = data.iloc[sh_index + 3]
                    
                    # Vérifie s'il y a un FVG baissier
                    if fvg_candle_3['high'] < fvg_candle_1['low']:
                        has_fvg = True
                # --- FIN AJOUT V2.1.0 ---
                        
                order_blocks.append({
                    "type": "BEARISH",
                    "top": target_candle['high'],
                    "bottom": target_candle['low'],
                    "timestamp": target_candle.name,
                    "mitigated": False,
                    "has_fvg": has_fvg # Ajout du nouveau champ
                })
        except (KeyError, IndexError):
            continue

    # Détection Bullish OB (basée sur les Swing Lows)
    for sl_time, sl_price in swing_lows:
        try:
            sl_index = data.index.get_loc(sl_time)
            
            candle = data.iloc[sl_index]
            target_candle = None # Initialisation
            
            if candle['close'] < candle['open']: # Bougie baissière (est l'OB)
                target_candle = candle
            else:
                if sl_index > 0:
                    prev_candle = data.iloc[sl_index - 1]
                    if prev_candle['close'] < prev_candle['open']: # Bougie baissière précédente (est l'OB)
                        target_candle = prev_candle
                else:
                    continue

            if target_candle is not None:

                # --- AJOUT V2.1.0: Vérification FVG ---
                has_fvg = False
                # Le mouvement impulsif commence après le Swing Low (sl_index)
                if sl_index + 3 < len(data):
                    fvg_candle_1 = data.iloc[sl_index + 1]
                    # fvg_candle_2 = data.iloc[sl_index + 2] # Bougie du milieu
                    fvg_candle_3 = data.iloc[sl_index + 3]
                    
                    # Vérifie s'il y a un FVG haussier
                    if fvg_candle_3['low'] > fvg_candle_1['high']:
                        has_fvg = True
                # --- FIN AJOUT V2.1.0 ---

                order_blocks.append({
                    "type": "BULLISH",
                    "top": target_candle['high'],
                    "bottom": target_candle['low'],
                    "timestamp": target_candle.name,
                    "mitigated": False,
                    "has_fvg": has_fvg # Ajout du nouveau champ
                })
        except (KeyError, IndexError):
            continue

    return order_blocks
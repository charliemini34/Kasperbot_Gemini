
# Fichier: src/patterns/pattern_detector.py
# Version: 20.0.0 (Build Stabilisé)
# Dépendances: MetaTrader5, pandas, numpy, logging, typing, src.constants
# Description: Version stable intégrant P1-P6 et corrections proactives (Logs INFO -> DEBUG P-Proactif 3).

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from src.constants import *
from typing import Tuple, Optional, Dict, List

# Constantes P/D
PREMIUM = "Premium"
DISCOUNT = "Discount"
EQUILIBRIUM = "Equilibrium"


class PatternDetector:
    """
    Détecte les patterns de trading SMC (Smart Money Concepts)
    v20.0.0: Stable. Logs de signaux internes passés en DEBUG.
    """

    def __init__(self, config: dict, digits: int):
        self.config = config
        self.digits = digits 
        
        self.pattern_settings = config.get('pattern_detection', {})
        self.use_trend_filter = config.get('trend_filter', {}).get('enabled', True)
        self.ema_period = config.get('trend_filter', {}).get('ema_period', 200)
        self.htf_timeframe = config.get('trend_filter', {}).get('higher_timeframe', 'H4')

        self.fvg_imbalance_ratio = self.pattern_settings.get('fvg_imbalance_ratio', 0.5)
        self.ob_wick_ratio = self.pattern_settings.get('ob_wick_ratio', 0.3)
        self.swing_lookback = self.pattern_settings.get('swing_lookback_periods', 10)
        self.premium_threshold = self.pattern_settings.get('premium_threshold', 0.5)
        
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self) -> dict:
        return self.detected_patterns_info.copy()


    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str) -> Optional[Dict]:
        if ohlc_data is None or len(ohlc_data) < 50:
             logging.warning(f"Données insuffisantes pour détection SMC {symbol}.")
             return None

        self.detected_patterns_info = {}
        df = ohlc_data.copy()
        
        if not isinstance(df.index, pd.DatetimeIndex):
             if 'time' in df.columns:
                 df['time'] = pd.to_datetime(df['time'], unit='s')
                 df.set_index('time', inplace=True)
             else:
                 logging.error("OHLC n'a ni DatetimeIndex ni colonne 'time'."); return None

        # 1. Filtre de Tendance
        main_trend = "NEUTRAL"
        if self.use_trend_filter:
            htf_data = connector.get_ohlc(symbol, self.htf_timeframe, self.ema_period + 50)
            if htf_data is not None and not htf_data.empty:
                try:
                    htf_data['close'] = pd.to_numeric(htf_data['close'])
                    htf_data[f'ema_{self.ema_period}'] = htf_data['close'].ewm(span=self.ema_period, adjust=False).mean()
                    last_close_htf = htf_data['close'].iloc[-1]
                    last_ema_htf = htf_data[f'ema_{self.ema_period}'].iloc[-1]
                    if last_close_htf > last_ema_htf: main_trend = "BULLISH"
                    elif last_close_htf < last_ema_htf: main_trend = "BEARISH"
                    self.detected_patterns_info['main_trend'] = f"{main_trend} (HTF {self.htf_timeframe})"
                except Exception as e:
                     logging.error(f"Erreur calcul filtre tendance HTF pour {symbol}: {e}")
                     self.detected_patterns_info['main_trend'] = "ERREUR HTF"
            else:
                 logging.warning(f"Impossible charger données HTF {self.htf_timeframe}.")
                 self.detected_patterns_info['main_trend'] = "ERREUR HTF"

        # 2. Identification Structure
        structure = self._get_market_structure(df)
        self.detected_patterns_info['structure'] = structure

        # 3. Détection POI (Zones Premium/Discount)
        premium_discount = self._get_premium_discount_zones(structure)
        self.detected_patterns_info.update(premium_discount)

        order_blocks = self._detect_order_block(df, structure, premium_discount)
        fvgs = self._detect_fvg(df, structure, premium_discount)
        self.detected_patterns_info['pois'] = {"order_blocks": f"Found {len(order_blocks)}", "fvgs": f"Found {len(fvgs)}"}

        # 4. Détection Triggers
        signal = None
        
        # --- MODIFICATION (P-Proactif 3) : Logs INFO -> DEBUG ---
        # Scénario 1: Pullback Achat (Priorité si tendance haussière)
        if main_trend != "BEARISH" and self.pattern_settings.get('ORDER_BLOCK', True):
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "BUY", structure)
            if signal:
                logging.debug(f"Signal détecté (interne) sur {symbol}: {signal['pattern']}")
                return signal

        # Scénario 2: Pullback Vente (Priorité si tendance baissière)
        if main_trend != "BULLISH" and self.pattern_settings.get('ORDER_BLOCK', True):
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "SELL", structure)
            if signal:
                 logging.debug(f"Signal détecté (interne) sur {symbol}: {signal['pattern']}")
                 return signal

        # Scénario 3: CHoCH
        if self.pattern_settings.get('CHANGE_OF_CHARACTER', True):
            signal = self._detect_choch(df, structure, main_trend)
            if signal:
                 logging.debug(f"Signal détecté (interne) sur {symbol}: {signal['pattern']}")
                 return signal
        
        # Scénario 4: Liquidity Grab
        if self.pattern_settings.get('LIQUIDITY_GRAB', True):
             signal = self._detect_liquidity_grab(df, structure, main_trend)
             if signal:
                 logging.debug(f"Signal détecté (interne) sur {symbol}: {signal['pattern']}")
                 return signal
        # --- FIN MODIFICATION ---
        
        logging.debug(f"Aucun signal SMC valide trouvé pour {symbol} (Tendance: {main_trend})")
        return None


    def _get_market_structure(self, df: pd.DataFrame) -> dict:
        n = self.swing_lookback
        if len(df) < n * 2 + 1:
             return {"last_swing_high": df['high'].iloc[-1], "last_swing_low": df['low'].iloc[-1], "internal_structure": "UNKNOWN"}

        df_copy = df.copy()
        n_fractal = max(2, n // 2)
        
        df_copy['high_shifted'] = df_copy['high'].shift(1)
        df_copy['low_shifted'] = df_copy['low'].shift(1)
        
        df_copy['is_swing_high'] = (df_copy['high_shifted'] == df_copy['high_shifted'].rolling(window=n, center=False, min_periods=n_fractal).max())
        df_copy['is_swing_low'] = (df_copy['low_shifted'] == df_copy['low_shifted'].rolling(window=n, center=False, min_periods=n_fractal).min())

        df_copy['is_swing_high'] = df_copy['is_swing_high'].fillna(False).infer_objects(copy=False)
        df_copy['is_swing_low'] = df_copy['is_swing_low'].fillna(False).infer_objects(copy=False)
        
        recent_swings_high_series = df_copy[df_copy['is_swing_high']]['high'].tail(5)
        recent_swings_low_series = df_copy[df_copy['is_swing_low']]['low'].tail(5)

        structure_info = {
            "last_swing_high": recent_swings_high_series.iloc[-1] if not recent_swings_high_series.empty else df_copy['high'].iloc[-1],
            "last_swing_low": recent_swings_low_series.iloc[-1] if not recent_swings_low_series.empty else df_copy['low'].iloc[-1],
            "internal_structure": "UNKNOWN"
        }
        return structure_info


    def _get_premium_discount_zones(self, structure: dict) -> dict:
        low = structure.get('last_swing_low', 0.0)
        high = structure.get('last_swing_high', 0.0)
        if low == 0.0 or high == 0.0 or high <= low:
            return {"equilibrium": 0.0, "premium_start": 0.0, "discount_start": 0.0}
        equilibrium = low + (high - low) * 0.5
        premium_threshold = self.premium_threshold
        discount_threshold = 1.0 - premium_threshold
        premium_start = low + (high - low) * premium_threshold
        discount_start = low + (high - low) * discount_threshold
        return {"equilibrium": equilibrium, "premium_start": premium_start, "discount_start": discount_start}


    def _detect_fvg(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        fvgs = []
        for i in range(len(df) - 50, len(df) - 1):
            if i < 1: continue
            try:
                prev_high = df['high'].iloc[i-1]; curr_high = df['high'].iloc[i]; curr_low = df['low'].iloc[i]; next_low = df['low'].iloc[i+1]
                prev_low = df['low'].iloc[i-1]; next_high = df['high'].iloc[i+1]
                is_bullish_candle = df['close'].iloc[i] > df['open'].iloc[i]
                is_bearish_candle = df['close'].iloc[i] < df['open'].iloc[i]
                if is_bullish_candle and curr_high > prev_high and next_low > prev_high:
                     fvg_top = next_low; fvg_bottom = prev_high
                     if fvg_bottom < pd_zones.get('discount_start', 0.0):
                         fvgs.append({"type": "FVG_BULLISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i})
                if is_bearish_candle and curr_low < prev_low and next_high < prev_low:
                     fvg_top = prev_low; fvg_bottom = next_high
                     if fvg_top > pd_zones.get('premium_start', 0.0):
                         fvgs.append({"type": "FVG_BEARISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i})
            except IndexError: break
            except Exception as e: logging.warning(f"Erreur détection FVG index {i}: {e}")
        return fvgs


    def _detect_order_block(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        order_blocks = []
        for i in range(len(df) - 50, len(df) - 2):
            try:
                candle_n = df.iloc[i]; candle_n_plus_1 = df.iloc[i+1]
                is_n_bullish = candle_n['close'] > candle_n['open']; is_n_bearish = candle_n['close'] < candle_n['open']
                is_n1_bullish = candle_n_plus_1['close'] > candle_n_plus_1['open']; is_n1_bearish = candle_n_plus_1['close'] < candle_n_plus_1['open']
                if is_n_bullish and is_n1_bearish:
                    if candle_n_plus_1['close'] < candle_n['low']:
                        ob_top = candle_n['high']; ob_bottom = candle_n['low']
                        if ob_bottom > pd_zones.get('premium_start', 0.0):
                             order_blocks.append({"type": "OB_BEARISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i})
                if is_n_bearish and is_n1_bullish:
                     if candle_n_plus_1['close'] > candle_n['high']:
                         ob_top = candle_n['high']; ob_bottom = candle_n['low']
                         if ob_top < pd_zones.get('discount_start', 0.0):
                             order_blocks.append({"type": "OB_BULLISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i})
            except IndexError: break
            except Exception as e: logging.warning(f"Erreur détection OB index {i}: {e}")
        return order_blocks


    def _detect_choch(self, df: pd.DataFrame, structure: dict, main_trend: str) -> Optional[Dict]:
        last_candle = df.iloc[-1]
        last_internal_low = structure.get('last_swing_low')
        if last_candle['close'] < last_internal_low:
             logging.debug(f"CHoCH Baissier détecté {df.index[-1]}: Clôture {last_candle['close']} < Dernier Low {last_internal_low}")
             sl_price = structure.get('last_swing_high', last_candle['high'] + (last_candle['high']-last_candle['low']))
             return {"pattern": "CHANGE_OF_CHARACTER (Bearish)", "direction": "SELL", "sl_price": sl_price, "tp_price": 0.0 }
        last_internal_high = structure.get('last_swing_high')
        if last_candle['close'] > last_internal_high:
             logging.debug(f"CHoCH Haussier détecté {df.index[-1]}: Clôture {last_candle['close']} > Dernier High {last_internal_high}")
             sl_price = structure.get('last_swing_low', last_candle['low'] - (last_candle['high']-last_candle['low']))
             return {"pattern": "CHANGE_OF_CHARACTER (Bullish)", "direction": "BUY", "sl_price": sl_price, "tp_price": 0.0 }
        return None
    
    def _detect_liquidity_grab(self, df: pd.DataFrame, structure: dict, main_trend: str) -> Optional[Dict]:
        if len(df) < 3: return None
        last_candle = df.iloc[-1]; prev_candle = df.iloc[-2]
        sl_price = 0.0; tp_price = 0.0
        if last_candle['low'] < prev_candle['low'] and last_candle['close'] > prev_candle['low']:
             logging.debug(f"Liquidity Grab (Haussier) détecté sur {df.index[-1]}")
             sl_price = last_candle['low']
             tp_price = structure.get('last_swing_high', 0.0)
             return {"pattern": "LIQUIDITY_GRAB (Bullish)", "direction": "BUY", "sl_price": sl_price, "tp_price": tp_price}
        if last_candle['high'] > prev_candle['high'] and last_candle['close'] < prev_candle['high']:
             logging.debug(f"Liquidity Grab (Baissier) détecté sur {df.index[-1]}")
             sl_price = last_candle['high']
             tp_price = structure.get('last_swing_low', 0.0)
             return {"pattern": "LIQUIDITY_GRAB (Bearish)", "direction": "SELL", "sl_price": sl_price, "tp_price": tp_price}
        return None


    def _check_pullback_to_poi(self, df: pd.DataFrame, order_blocks: list, fvgs: list, direction: str, structure: dict) -> Optional[Dict]:
        last_candle = df.iloc[-1]
        last_low = last_candle['low']
        last_high = last_candle['high']
        
        digits_fmt = self.digits 

        if direction == "BUY":
            pois = [p for p in order_blocks if p['type'] == "OB_BULLISH"] + \
                   [p for p in fvgs if p['type'] == "FVG_BULLISH"]
            if not pois: return None
            pois.sort(key=lambda x: x['top'], reverse=True)
            for poi in pois:
                poi_top = poi['top']; poi_bottom = poi['bottom']
                if last_low <= poi_top and last_high >= poi_bottom:
                     logging.debug(f"Contact POI Achat {poi['type']} @ {poi_top:.{digits_fmt}f} (Index {poi['candle_index']})")
                     sl_price = poi_bottom
                     tp_price = structure.get('last_swing_high', 0.0)
                     return {"pattern": f"POI_PULLBACK ({poi['type']})", "direction": "BUY", "sl_price": sl_price, "tp_price": tp_price}

        if direction == "SELL":
            pois = [p for p in order_blocks if p['type'] == "OB_BEARISH"] + \
                   [p for p in fvgs if p['type'] == "FVG_BEARISH"]
            if not pois: return None
            pois.sort(key=lambda x: x['bottom'], reverse=False)
            for poi in pois:
                poi_top = poi['top']; poi_bottom = poi['bottom']
                if last_high >= poi_bottom and last_low <= poi_top:
                     logging.debug(f"Contact POI Vente {poi['type']} @ {poi_bottom:.{digits_fmt}f} (Index {poi['candle_index']})")
                     sl_price = poi_top
                     tp_price = structure.get('last_swing_low', 0.0)
                     return {"pattern": f"POI_PULLBACK ({poi['type']})", "direction": "SELL", "sl_price": sl_price, "tp_price": tp_price}
        return None
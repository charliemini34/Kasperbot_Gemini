# Fichier: src/patterns/pattern_detector.py
# Version: 19.0.3 (Digits-Fix)
# Dépendances: MetaTrader5, pandas, numpy, logging, src.constants
# Description: Corrige AttributeError 'DataFrame' object has no attribute 'digits'.

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from src.constants import * # Importer les constantes
from typing import Tuple, Optional, Dict # Assurer que typing est importé

# Définir les constantes P/D si elles ne sont pas dans constants.py
PREMIUM = "Premium"
DISCOUNT = "Discount"
EQUILIBRIUM = "Equilibrium"


class PatternDetector:
    """
    Détecte les patterns de trading SMC (Smart Money Concepts)
    basés sur les données OHLC fournies.
    v19.0.3: Corrige l'AttributeError 'digits' en passant le paramètre.
    """

    def __init__(self, config: dict, digits: int): # Ajout de digits
        self.config = config
        self.digits = digits # Stocker le nombre de décimales du symbole
        self.pattern_settings = config.get('pattern_detection', {}) # Corrigé 'pattern_settings' -> 'pattern_detection'
        self.use_trend_filter = config.get('trend_filter', {}).get('enabled', True)
        self.ema_period = config.get('trend_filter', {}).get('ema_period', 200)
        self.htf_timeframe = config.get('trend_filter', {}).get('higher_timeframe', 'H4')

        # Paramètres SMC spécifiques
        self.fvg_imbalance_ratio = self.pattern_settings.get('fvg_imbalance_ratio', 0.5)
        self.ob_wick_ratio = self.pattern_settings.get('ob_wick_ratio', 0.3)
        self.swing_lookback = self.pattern_settings.get('swing_lookback_periods', 10)
        
        # Seuil Premium/Discount
        self.premium_threshold = self.pattern_settings.get('premium_threshold', 0.5)

        self.detected_patterns_info = {}

    def get_detected_patterns_info(self) -> dict:
        return self.detected_patterns_info.copy()


    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str) -> dict:
        if ohlc_data is None or len(ohlc_data) < 50:
             logging.warning(f"Données insuffisantes pour la détection SMC sur {symbol}.")
             return None

        self.detected_patterns_info = {}
        df = ohlc_data.copy()
        
        # Convertir index en datetime si ce n'est pas déjà fait (ex: ohlc_data vient de get_ohlc)
        if not isinstance(df.index, pd.DatetimeIndex):
             if 'time' in df.columns:
                 df['time'] = pd.to_datetime(df['time'], unit='s')
                 df.set_index('time', inplace=True)
             else:
                 logging.error("DataFrame OHLC n'a ni DatetimeIndex ni colonne 'time'.")
                 return None

        # 1. Filtre de Tendance
        main_trend = "NEUTRAL"
        if self.use_trend_filter:
            htf_data = connector.get_ohlc(symbol, self.htf_timeframe, self.ema_period + 50)
            if htf_data is not None and not htf_data.empty:
                htf_data[f'ema_{self.ema_period}'] = htf_data['close'].ewm(span=self.ema_period, adjust=False).mean()
                last_close_htf = htf_data['close'].iloc[-1]
                last_ema_htf = htf_data[f'ema_{self.ema_period}'].iloc[-1]
                if last_close_htf > last_ema_htf: main_trend = "BULLISH"
                elif last_close_htf < last_ema_htf: main_trend = "BEARISH"
                self.detected_patterns_info['main_trend'] = f"{main_trend} (HTF {self.htf_timeframe})"
            else:
                 logging.warning(f"Impossible de charger données HTF {self.htf_timeframe} pour filtre tendance.")
                 self.detected_patterns_info['main_trend'] = "ERREUR HTF"

        # 2. Identification Structure
        structure = self._get_market_structure(df)
        self.detected_patterns_info['structure'] = structure

        # 3. Détection POI (Zones Premium/Discount)
        premium_discount = self._get_premium_discount_zones(structure)
        self.detected_patterns_info.update(premium_discount)

        order_blocks = self._detect_order_block(df, structure, premium_discount)
        fvgs = self._detect_fvg(df, structure, premium_discount)
        self.detected_patterns_info['pois'] = {"order_blocks": f"Found {len(order_blocks)}", "fvgs": f"Found {len(fvgs)}"} # Rendre loggable

        # 4. Détection Triggers
        signal = None
        
        # Scénario 1: Pullback Achat (Priorité si tendance haussière)
        if main_trend != "BEARISH" and self.pattern_settings.get('ORDER_BLOCK', True): # Nom du pattern
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "BUY", structure)
            if signal:
                logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                return signal

        # Scénario 2: Pullback Vente (Priorité si tendance baissière)
        if main_trend != "BULLISH" and self.pattern_settings.get('ORDER_BLOCK', True): # Nom du pattern
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "SELL", structure)
            if signal:
                 logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                 return signal

        # Scénario 3: CHoCH
        if self.pattern_settings.get('CHANGE_OF_CHARACTER', True): # Nom du pattern
            signal = self._detect_choch(df, structure, main_trend)
            if signal:
                 logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                 return signal
        
        # Scénario 4: Liquidity Grab
        if self.pattern_settings.get('LIQUIDITY_GRAB', True): # Nom du pattern
             signal = self._detect_liquidity_grab(df, structure, main_trend)
             if signal:
                 logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                 return signal

        # (Logique AMD omise pour l'instant, car elle est plus complexe)

        logging.debug(f"Aucun signal SMC valide trouvé pour {symbol} (Tendance: {main_trend})")
        return None


    def _get_market_structure(self, df: pd.DataFrame) -> dict:
        n = self.swing_lookback
        if len(df) < n * 2 + 1: # Pas assez de données
             return {"last_swing_high": df['high'].iloc[-1], "last_swing_low": df['low'].iloc[-1], "internal_structure": "UNKNOWN"}

        # --- CORRECTION (Lookahead Bias & FutureWarning) ---
        # Utilise une méthode causale (sans 'center=True')
        # rolling(n+1) regarde les n bougies passées + la bougie actuelle
        df_copy = df.copy() # Eviter SettingWithCopyWarning
        df_copy['min_past_n'] = df_copy['low'].shift(1).rolling(window=n, min_periods=n//2).min()
        df_copy['max_past_n'] = df_copy['high'].shift(1).rolling(window=n, min_periods=n//2).max()
        
        # Un swing low est un 'low' plus bas que les N précédents ET les N suivants
        # Pour le rendre causal, on ne peut regarder que les N précédents.
        # Simplification : un swing low est le point le plus bas des N dernières bougies
        # C'est une définition faible. Utilisons une définition standard de fractal (plus simple)
        n_fractal = max(2, n // 2) # 2 bougies de chaque côté pour un fractal
        
        df_copy['is_swing_high'] = (df_copy['high'] > df_copy['high'].shift(i) for i in range(1, n_fractal + 1)) & \
                                 (df_copy['high'] > df_copy['high'].shift(-i) for i in range(1, n_fractal + 1))
        df_copy['is_swing_low'] = (df_copy['low'] < df_copy['low'].shift(i) for i in range(1, n_fractal + 1)) & \
                                 (df_copy['low'] < df_copy['low'].shift(-i) for i in range(1, n_fractal + 1))

        # Rendre causal (un swing n'est confirmé qu'après n_fractal bougies)
        df_copy['is_swing_high'] = df_copy['is_swing_high'].shift(n_fractal).fillna(False)
        df_copy['is_swing_low'] = df_copy['is_swing_low'].shift(n_fractal).fillna(False)
        # --- FIN CORRECTION ---
        
        recent_swings_high_series = df_copy[df_copy['is_swing_high']]['high'].tail(5)
        recent_swings_low_series = df_copy[df_copy['is_swing_low']]['low'].tail(5)

        structure_info = {
            "last_swing_high": recent_swings_high_series.iloc[-1] if not recent_swings_high_series.empty else df_copy['high'].iloc[-1],
            "last_swing_low": recent_swings_low_series.iloc[-1] if not recent_swings_low_series.empty else df_copy['low'].iloc[-1],
            "internal_structure": "UNKNOWN" # (Logique complexe de suivi HH/LL omise)
        }
        return structure_info


    def _get_premium_discount_zones(self, structure: dict) -> dict:
        # ... (inchangé) ...
        low = structure.get('last_swing_low', 0.0)
        high = structure.get('last_swing_high', 0.0)
        if low == 0.0 or high == 0.0 or high <= low:
            return {"equilibrium": 0.0, "premium_start": 0.0, "discount_start": 0.0}
        equilibrium = low + (high - low) * 0.5
        premium_threshold = self.premium_threshold # Utilise 0.5 par défaut
        discount_threshold = 1.0 - premium_threshold
        premium_start = low + (high - low) * premium_threshold
        discount_start = low + (high - low) * discount_threshold
        return {"equilibrium": equilibrium, "premium_start": premium_start, "discount_start": discount_start}


    def _detect_fvg(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        # ... (inchangé) ...
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
                         fvgs.append({"type": "FVG_BULLISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i}) # Nom type clarifié

                if is_bearish_candle and curr_low < prev_low and next_high < prev_low:
                     fvg_top = prev_low; fvg_bottom = next_high
                     if fvg_top > pd_zones.get('premium_start', 0.0):
                         fvgs.append({"type": "FVG_BEARISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i}) # Nom type clarifié
            except IndexError: break
            except Exception as e: logging.warning(f"Erreur détection FVG index {i}: {e}")
        return fvgs


    def _detect_order_block(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        # ... (inchangé) ...
        order_blocks = []
        for i in range(len(df) - 50, len(df) - 2):
            try:
                candle_n = df.iloc[i]; candle_n_plus_1 = df.iloc[i+1]
                is_n_bullish = candle_n['close'] > candle_n['open']; is_n_bearish = candle_n['close'] < candle_n['open']
                is_n1_bullish = candle_n_plus_1['close'] > candle_n_plus_1['open']; is_n1_bearish = candle_n_plus_1['close'] < candle_n_plus_1['open']
                
                if is_n_bullish and is_n1_bearish: # OB Baissier potentiel
                    if candle_n_plus_1['close'] < candle_n['low']: # Impulsion
                        ob_top = candle_n['high']; ob_bottom = candle_n['low']
                        if ob_bottom > pd_zones.get('premium_start', 0.0): # En Premium
                             order_blocks.append({"type": "OB_BEARISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i}) # Nom type clarifié

                if is_n_bearish and is_n1_bullish: # OB Haussier potentiel
                     if candle_n_plus_1['close'] > candle_n['high']: # Impulsion
                         ob_top = candle_n['high']; ob_bottom = candle_n['low']
                         if ob_top < pd_zones.get('discount_start', 0.0): # En Discount
                             order_blocks.append({"type": "OB_BULLISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i}) # Nom type clarifié
            except IndexError: break
            except Exception as e: logging.warning(f"Erreur détection OB index {i}: {e}")
        return order_blocks


    def _detect_choch(self, df: pd.DataFrame, structure: dict, main_trend: str) -> Optional[Dict]:
        # ... (inchangé) ...
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
        """ Détecte une prise de liquidité (mèche). """
        if len(df) < 3: return None
        
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2] # Bougie N-1

        sl_price = 0.0
        tp_price = 0.0 # ATR ou structure externe
        
        # Grab Haussier (mèche basse N-1 prise, clôture N > N-1 low)
        # (Logique v19.0.0 était complexe, simplification)
        if last_candle['low'] < prev_candle['low'] and last_candle['close'] > prev_candle['low']:
             logging.debug(f"Liquidity Grab (Haussier) détecté sur {df.index[-1]}")
             sl_price = last_candle['low'] # SL sous la mèche
             tp_price = structure.get('last_swing_high', 0.0) # Vise liquidité externe
             return {"pattern": "LIQUIDITY_GRAB (Bullish)", "direction": "BUY", "sl_price": sl_price, "tp_price": tp_price}

        # Grab Baissier (mèche haute N-1 prise, clôture N < N-1 high)
        if last_candle['high'] > prev_candle['high'] and last_candle['close'] < prev_candle['high']:
             logging.debug(f"Liquidity Grab (Baissier) détecté sur {df.index[-1]}")
             sl_price = last_candle['high'] # SL au-dessus de la mèche
             tp_price = structure.get('last_swing_low', 0.0)
             return {"pattern": "LIQUIDITY_GRAB (Bearish)", "direction": "SELL", "sl_price": sl_price, "tp_price": tp_price}

        return None


    def _check_pullback_to_poi(self, df: pd.DataFrame, order_blocks: list, fvgs: list, direction: str, structure: dict) -> Optional[Dict]:
        # ... (Logique inchangée, mais correction du log) ...
        last_candle = df.iloc[-1]
        last_low = last_candle['low']
        last_high = last_candle['high']
        
        # --- CORRECTION : Utiliser self.digits stocké dans __init__ ---
        digits_fmt = self.digits 
        # --- FIN CORRECTION ---

        if direction == "BUY":
            pois = [p for p in order_blocks if p['type'] == "OB_BULLISH"] + \
                   [p for p in fvgs if p['type'] == "FVG_BULLISH"]
            if not pois: return None
            pois.sort(key=lambda x: x['top'], reverse=True)
            for poi in pois:
                poi_top = poi['top']; poi_bottom = poi['bottom']
                if last_low <= poi_top and last_high >= poi_bottom: # Contact avec la zone
                     # --- CORRECTION Log ---
                     logging.debug(f"Contact POI Achat {poi['type']} @ {poi_top:.{digits_fmt}f} (Index {poi['candle_index']})")
                     # --- FIN CORRECTION ---
                     sl_price = poi_bottom # SL basé sur bas de la POI
                     tp_price = structure.get('last_swing_high', 0.0)
                     return {"pattern": f"POI_PULLBACK ({poi['type']})", "direction": "BUY", "sl_price": sl_price, "tp_price": tp_price}

        if direction == "SELL":
            pois = [p for p in order_blocks if p['type'] == "OB_BEARISH"] + \
                   [p for p in fvgs if p['type'] == "FVG_BEARISH"]
            if not pois: return None
            pois.sort(key=lambda x: x['bottom'], reverse=False)
            for poi in pois:
                poi_top = poi['top']; poi_bottom = poi['bottom']
                if last_high >= poi_bottom and last_low <= poi_top: # Contact avec la zone
                     # --- CORRECTION Log ---
                     logging.debug(f"Contact POI Vente {poi['type']} @ {poi_bottom:.{digits_fmt}f} (Index {poi['candle_index']})")
                     # --- FIN CORRECTION ---
                     sl_price = poi_top # SL basé sur haut de la POI
                     tp_price = structure.get('last_swing_low', 0.0)
                     return {"pattern": f"POI_PULLBACK ({poi['type']})", "direction": "SELL", "sl_price": sl_price, "tp_price": tp_price}
        return None
# Fichier: src/patterns/pattern_detector.py

import pandas as pd
import numpy as np
import logging
from datetime import time

class PatternDetector:
    """
    Module de reconnaissance de patterns Smart Money Concepts (SMC).
    v7.3 : Correction du bug AttributeError dans la détection d'Order Block.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info

    def _get_trend_filter_direction(self, df: pd.DataFrame) -> str:
        """Détermine la tendance de fond avec une EMA pour filtrer les trades."""
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Disabled'}
            return "ANY"

        period = filter_cfg.get('ema_period', 200)
        ema = df['close'].ewm(span=period, adjust=False).mean()

        if df['close'].iloc[-1] > ema.iloc[-1]:
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Bullish'}
            return "BUY"
        else:
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Bearish'}
            return "SELL"

    def detect_patterns(self, ohlc_data: pd.DataFrame):
        """Passe en revue toutes les stratégies de détection et les filtre par tendance."""
        self.detected_patterns_info = {}
        
        df = ohlc_data.copy()
        if 'time' in df.columns and not isinstance(df.index, pd.DatetimeIndex):
            df.set_index(pd.to_datetime(df['time'], unit='s'), inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(df)

        detection_functions = {
            "SMC_AMD_SESSION": self._detect_amd_session,
            "INBALANCE": self._detect_inbalance,
            "ORDER_BLOCK": self._detect_order_block,
        }

        for name, func in detection_functions.items():
            if self.config['pattern_detection'].get(name, False):
                trade_signal = func(df)
                if trade_signal:
                    if allowed_direction == "ANY" or trade_signal['direction'] == allowed_direction:
                        return trade_signal
                    else:
                        self.log.info(f"Pattern {name} ignoré (direction {trade_signal['direction']} vs tendance {allowed_direction}).")
        
        return None

    def _find_swing_points(self, series: pd.Series, n=3):
        """Trouve les points de swing (hauts/bas) dans une série de prix."""
        lows = series[(series.shift(1) > series) & (series.shift(-1) > series)]
        highs = series[(series.shift(1) < series) & (series.shift(-1) < series)]
        return lows, highs

    def _detect_amd_session(self, df: pd.DataFrame):
        """Détecte le pattern Accumulation-Manipulation-Distribution (AMD)."""
        self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': 'Analyzing...'}
        last_candle_time = df.index[-1]

        if not (time(7, 0) <= last_candle_time.time() <= time(20, 0)): return None

        asian_session = df.between_time('00:00', '06:59').loc[last_candle_time.date().strftime('%Y-%m-%d')]
        if len(asian_session) < 5: return None

        asian_high, asian_low = asian_session['high'].max(), asian_session['low'].min()
        self.detected_patterns_info['SMC_AMD_SESSION']['status'] = f'Range: {asian_low:.2f}-{asian_high:.2f}'

        recent_candles = df.loc[df.index > asian_session.index[-1]]
        if recent_candles.empty: return None

        if recent_candles['high'].max() > asian_high:
            swing_lows, _ = self._find_swing_points(recent_candles['low'])
            if swing_lows.empty: return None
            choch_level = swing_lows.iloc[-1]
            if df['close'].iloc[-1] < choch_level and df['close'].iloc[-2] >= choch_level:
                return {'pattern': 'SMC_AMD_Sell', 'direction': 'SELL'}

        if recent_candles['low'].min() < asian_low:
            _, swing_highs = self._find_swing_points(recent_candles['high'])
            if swing_highs.empty: return None
            choch_level = swing_highs.iloc[-1]
            if df['close'].iloc[-1] > choch_level and df['close'].iloc[-2] <= choch_level:
                return {'pattern': 'SMC_AMD_Buy', 'direction': 'BUY'}
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        """Détecte un Fair Value Gap (Inbalance) dans la zone de Discount/Premium."""
        self.detected_patterns_info['INBALANCE'] = {'status': 'No Signal'}
        if len(df) < 50: return None
        
        recent_high, recent_low = df['high'].iloc[-50:].max(), df['low'].iloc[-50:].min()
        equilibrium_mid = (recent_high + recent_low) / 2

        for i in range(len(df) - 3, len(df) - 20, -1):
            c1, c3 = df.iloc[i-2], df.iloc[i]
            if c1['high'] < c3['low']:
                fvg_top, fvg_bottom = c3['low'], c1['high']
                if fvg_top < equilibrium_mid and df['low'].iloc[-1] <= fvg_top and df['high'].iloc[-1] >= fvg_bottom:
                    return {'pattern': 'Inbalance_Buy', 'direction': 'BUY'}
            
            if c1['low'] > c3['high']:
                fvg_top, fvg_bottom = c1['low'], c3['high']
                if fvg_bottom > equilibrium_mid and df['high'].iloc[-1] >= fvg_bottom and df['low'].iloc[-1] <= fvg_top:
                    return {'pattern': 'Inbalance_Sell', 'direction': 'SELL'}
        return None

    def _detect_order_block(self, df: pd.DataFrame):
        """Détecte un retour sur un Order Block après une cassure de structure."""
        self.detected_patterns_info['ORDER_BLOCK'] = {'status': 'No Signal'}
        if len(df) < 20: return None

        # --- CORRECTION ICI ---
        # On appelle _find_swing_points séparément pour 'low' et 'high'
        swing_lows, _ = self._find_swing_points(df['low'].iloc[-20:])
        _, swing_highs = self._find_swing_points(df['high'].iloc[-20:])
        
        if len(swing_highs) > 1 and len(swing_lows) > 0:
            if swing_highs.index[-1] > swing_lows.index[-1] and swing_highs.iloc[-1] > swing_highs.iloc[-2]:
                bos_candle_idx = df.index.get_loc(swing_highs.index[-1])
                candles_before_bos = df.iloc[:bos_candle_idx]
                down_candles = candles_before_bos[candles_before_bos['close'] < candles_before_bos['open']]
                if not down_candles.empty:
                    ob = down_candles.iloc[-1]
                    if df['low'].iloc[-1] <= ob['high'] and df['high'].iloc[-1] >= ob['low']:
                        return {'pattern': 'Order_Block_Buy', 'direction': 'BUY'}

        if len(swing_lows) > 1 and len(swing_highs) > 0:
            if swing_lows.index[-1] > swing_highs.index[-1] and swing_lows.iloc[-1] < swing_lows.iloc[-2]:
                bos_candle_idx = df.index.get_loc(swing_lows.index[-1])
                candles_before_bos = df.iloc[:bos_candle_idx]
                up_candles = candles_before_bos[candles_before_bos['close'] > candles_before_bos['open']]
                if not up_candles.empty:
                    ob = up_candles.iloc[-1]
                    if df['high'].iloc[-1] >= ob['low'] and df['low'].iloc[-1] <= ob['high']:
                        return {'pattern': 'Order_Block_Sell', 'direction': 'SELL'}
        
        return None
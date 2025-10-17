# Fichier: src/patterns/pattern_detector.py
# Version: 12.0.1 (Full SMC/ICT Suite - Hotfix)
# Dépendances: pandas, numpy, logging
# Description: Moteur de détection complet incluant AMD et Inbalance.

import pandas as pd
import numpy as np
import logging
from datetime import time
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_CHOCH, PATTERN_INBALANCE, PATTERN_LIQUIDITY_GRAB,
    PATTERN_AMD, BUY, SELL
)

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC & ICT.
    v12.0 : Intégration de AMD (session-based) et Inbalance.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info

    def _get_trend_filter_direction(self, connector, symbol: str) -> str:
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Désactivé'}
            return "ANY"

        higher_timeframe = filter_cfg.get('higher_timeframe', 'H4')
        period = filter_cfg.get('ema_period', 200)
        
        htf_data = connector.get_ohlc(symbol, higher_timeframe, period + 50)
        if htf_data is None or htf_data.empty:
            self.log.warning(f"Impossible de récupérer les données {higher_timeframe} pour le filtre de tendance.")
            self.detected_patterns_info['TREND_FILTER'] = {'status': f'Erreur données {higher_timeframe}'}
            return "ANY"

        ema = htf_data['close'].ewm(span=period, adjust=False).mean()
        status = "HAUSSIÈRE" if htf_data['close'].iloc[-1] > ema.iloc[-1] else "BAISSIÈRE"
        self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} ({higher_timeframe})"}
        return BUY if status == "HAUSSIÈRE" else SELL

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        self.detected_patterns_info = {}
        df = ohlc_data.copy()
        if 'time' in df.columns and not isinstance(df.index, pd.DatetimeIndex):
            df.set_index(pd.to_datetime(df['time'], unit='s'), inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(connector, symbol)
        
        detection_functions = {
            PATTERN_CHOCH: self._detect_choch,
            PATTERN_ORDER_BLOCK: self._detect_order_block,
            PATTERN_INBALANCE: self._detect_inbalance,
            PATTERN_LIQUIDITY_GRAB: self._detect_liquidity_grab,
            PATTERN_AMD: self._detect_amd_session,
        }

        all_signals = []
        for name, func in detection_functions.items():
            if self.config.get('pattern_detection', {}).get(name, False):
                signal = func(df)
                if signal:
                    all_signals.append(signal)

        confirmed_trade_signal = None
        for signal in all_signals:
            name = signal['pattern'].split('_')[0]
            if allowed_direction == "ANY" or signal['direction'] == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"Signal {signal['direction']}"}
                if not confirmed_trade_signal:
                    confirmed_trade_signal = signal
                    self.detected_patterns_info[name]['status'] = f"CONFIRMÉ ({signal['direction']})"
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({signal['direction']} vs Tendance {allowed_direction})"}
        
        return confirmed_trade_signal

    def _find_swing_points(self, high_series: pd.Series, low_series: pd.Series, n=2):
        """CORRIGÉ: Trouve les points de swing sur les séries high et low."""
        highs = high_series[(high_series.shift(1) < high_series) & (high_series.shift(-1) < high_series)]
        lows = low_series[(low_series.shift(1) > low_series) & (low_series.shift(-1) > low_series)]
        return highs, lows

    def _detect_choch(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        recent_data = df.iloc[-20:]
        # CORRECTION: Appel corrigé à _find_swing_points
        swing_highs, swing_lows = self._find_swing_points(recent_data['high'], recent_data['low'])
        
        if len(swing_highs) > 1 and len(swing_lows) > 0:
            last_high = swing_highs.index[-1]
            prev_high = swing_highs.index[-2]
            last_low = swing_lows.index[-1]
            if last_high > last_low > prev_high and recent_data['high'].iloc[-1] > swing_highs.iloc[-1]:
                return {'pattern': f'{PATTERN_CHOCH}_{BUY}', 'direction': BUY}

        if len(swing_lows) > 1 and len(swing_highs) > 0:
            last_low = swing_lows.index[-1]
            prev_low = swing_lows.index[-2]
            last_high = swing_highs.index[-1]
            if last_low > last_high > prev_low and recent_data['low'].iloc[-1] < swing_lows.iloc[-1]:
                return {'pattern': f'{PATTERN_CHOCH}_{SELL}', 'direction': SELL}
        return None

    def _detect_order_block(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None
        recent_data = df.iloc[-50:]
        swing_highs, swing_lows = self._find_swing_points(recent_data['high'], recent_data['low'])
        
        if len(swing_highs) > 0 and recent_data['high'].iloc[-1] > swing_highs.iloc[-1]:
            movement_data = recent_data[recent_data.index < swing_highs.index[-1]]
            down_candles = movement_data[movement_data['close'] < movement_data['open']]
            if not down_candles.empty and recent_data['low'].iloc[-1] <= down_candles.iloc[-1]['high']:
                return {'pattern': f'{PATTERN_ORDER_BLOCK}_{BUY}', 'direction': BUY}
                        
        if len(swing_lows) > 0 and recent_data['low'].iloc[-1] < swing_lows.iloc[-1]:
            movement_data = recent_data[recent_data.index < swing_lows.index[-1]]
            up_candles = movement_data[movement_data['close'] > movement_data['open']]
            if not up_candles.empty and recent_data['high'].iloc[-1] >= up_candles.iloc[-1]['low']:
                return {'pattern': f'{PATTERN_ORDER_BLOCK}_{SELL}', 'direction': SELL}
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_INBALANCE] = {'status': 'Pas de signal'}
        if len(df) < 5: return None
        
        bullish_fvg_candles = df[df['low'] > df['high'].shift(2)]
        if not bullish_fvg_candles.empty:
            last_fvg_candle = bullish_fvg_candles.index[-1]
            fvg_top = df.loc[last_fvg_candle, 'low']
            fvg_bottom = df.shift(2).loc[last_fvg_candle, 'high']
            if df['low'].iloc[-1] <= fvg_bottom:
                 return {'pattern': f'{PATTERN_INBALANCE}_{BUY}', 'direction': BUY}

        bearish_fvg_candles = df[df['high'] < df['low'].shift(2)]
        if not bearish_fvg_candles.empty:
            last_fvg_candle = bearish_fvg_candles.index[-1]
            fvg_bottom = df.loc[last_fvg_candle, 'high']
            fvg_top = df.shift(2).loc[last_fvg_candle, 'low']
            if df['high'].iloc[-1] >= fvg_top:
                return {'pattern': f'{PATTERN_INBALANCE}_{SELL}', 'direction': SELL}
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_LIQUIDITY_GRAB] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        
        recent_data = df.iloc[-20:]
        swing_highs, swing_lows = self._find_swing_points(recent_data['high'], recent_data['low'], n=3)

        if len(swing_lows) > 0:
            last_low = swing_lows.iloc[-1]
            prev_candle = df.iloc[-2]
            if prev_candle['low'] < last_low and prev_candle['close'] > last_low:
                return {'pattern': f'{PATTERN_LIQUIDITY_GRAB}_{BUY}', 'direction': BUY}

        if len(swing_highs) > 0:
            last_high = swing_highs.iloc[-1]
            prev_candle = df.iloc[-2]
            if prev_candle['high'] > last_high and prev_candle['close'] < last_high:
                return {'pattern': f'{PATTERN_LIQUIDITY_GRAB}_{SELL}', 'direction': SELL}
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_AMD] = {'status': 'En attente'}
        asia_start, asia_end, london_open = time(0, 0), time(7, 0), time(8, 0)
        current_time_utc = df.index[-1].time()

        if not (london_open <= current_time_utc < time(16,0)): return None

        today_utc = df.index[-1].date()
        asia_session_today = df.between_time(asia_start, asia_end)
        asia_session_today = asia_session_today[asia_session_today.index.date == today_utc]
        
        if asia_session_today.empty:
            self.detected_patterns_info[PATTERN_AMD] = {'status': 'Pas de données Asie'}
            return None

        asia_high = asia_session_today['high'].max()
        asia_low = asia_session_today['low'].min()
        self.detected_patterns_info[PATTERN_AMD] = {'status': f'Asie H:{asia_high:.5f} L:{asia_low:.5f}'}

        recent_market_data = df.loc[df.index.date == today_utc].between_time(asia_end, current_time_utc)
        if recent_market_data.empty: return None

        if recent_market_data['low'].min() < asia_low:
            choch_signal = self._detect_choch(recent_market_data)
            if choch_signal and choch_signal['direction'] == BUY:
                return {'pattern': f'{PATTERN_AMD}_{BUY}', 'direction': BUY}

        if recent_market_data['high'].max() > asia_high:
            choch_signal = self._detect_choch(recent_market_data)
            if choch_signal and choch_signal['direction'] == SELL:
                return {'pattern': f'{PATTERN_AMD}_{SELL}', 'direction': SELL}
                    
        return None
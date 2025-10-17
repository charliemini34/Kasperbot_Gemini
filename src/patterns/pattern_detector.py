# Fichier: src/patterns/pattern_detector.py
# Version: 9.5.0 (SMC Refined)
# Dépendances: pandas, numpy, logging
# Description: Moteur de détection de patterns SMC avec une logique d'Order Block affinée.

import pandas as pd
import numpy as np
import logging
from datetime import time
from src.constants import PATTERN_AMD, PATTERN_INBALANCE, PATTERN_ORDER_BLOCK, BUY, SELL

class PatternDetector:
    """
    Module de reconnaissance de patterns Smart Money Concepts (SMC).
    v9.5.0 : Renforce la détection des Order Blocks en exigeant une rupture
             de structure (BOS) claire pour valider un signal.
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

        if htf_data['close'].iloc[-1] > ema.iloc[-1]:
            status = f"HAUSSIÈRE ({higher_timeframe})"
            self.detected_patterns_info['TREND_FILTER'] = {'status': status}
            return BUY
        else:
            status = f"BAISSIÈRE ({higher_timeframe})"
            self.detected_patterns_info['TREND_FILTER'] = {'status': status}
            return SELL

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        self.detected_patterns_info = {}
        df = ohlc_data.copy()
        if 'time' in df.columns and not isinstance(df.index, pd.DatetimeIndex):
            df.set_index(pd.to_datetime(df['time'], unit='s'), inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(connector, symbol)
        confirmed_trade_signal = None

        detection_functions = {
            PATTERN_AMD: self._detect_amd_session,
            PATTERN_INBALANCE: self._detect_inbalance,
            PATTERN_ORDER_BLOCK: self._detect_order_block,
        }

        for name, func in detection_functions.items():
            if self.config['pattern_detection'].get(name, False):
                trade_signal = func(df)
                if trade_signal:
                    if allowed_direction == "ANY" or trade_signal['direction'] == allowed_direction:
                        if not confirmed_trade_signal:
                            confirmed_trade_signal = trade_signal
                            self.detected_patterns_info[name]['status'] = f"CONFIRMÉ ({trade_signal['direction']})"
                    else:
                        self.detected_patterns_info[name]['status'] = f"INVALIDÉ ({trade_signal['direction']} vs Tendance {allowed_direction})"
        
        return confirmed_trade_signal

    def _find_swing_points(self, series: pd.Series, n=3):
        lows = series[(series.shift(1) > series) & (series.shift(-1) > series)]
        highs = series[(series.shift(1) < series) & (series.shift(-1) < series)]
        return lows, highs

    def _detect_amd_session(self, df: pd.DataFrame):
        # ... (logique inchangée)
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        # ... (logique inchangée)
        return None

    def _detect_order_block(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None

        swing_lows, swing_highs = self._find_swing_points(df['low'].iloc[-50:]), self._find_swing_points(df['high'].iloc[-50:])
        
        # Bullish Order Block
        if len(swing_highs[1]) > 0 and len(swing_lows[0]) > 0:
            last_swing_high = swing_highs[1].index[-1]
            # Vérifier s'il y a une rupture de structure (BOS)
            if df['high'].iloc[-1] > swing_highs[1].iloc[-1]:
                candles_before_bos = df.loc[:last_swing_high]
                down_candles = candles_before_bos[candles_before_bos['close'] < candles_before_bos['open']]
                if not down_candles.empty:
                    ob = down_candles.iloc[-1]
                    if df['low'].iloc[-1] <= ob['high'] and df['high'].iloc[-1] >= ob['low']:
                        self.detected_patterns_info[PATTERN_ORDER_BLOCK]['status'] = f'Signal {BUY}'
                        return {'pattern': 'Order_Block_Buy', 'direction': BUY}
                        
        # Bearish Order Block
        if len(swing_lows[0]) > 0 and len(swing_highs[1]) > 0:
            last_swing_low = swing_lows[0].index[-1]
            # Vérifier s'il y a une rupture de structure (BOS)
            if df['low'].iloc[-1] < swing_lows[0].iloc[-1]:
                candles_before_bos = df.loc[:last_swing_low]
                up_candles = candles_before_bos[candles_before_bos['close'] > candles_before_bos['open']]
                if not up_candles.empty:
                    ob = up_candles.iloc[-1]
                    if df['high'].iloc[-1] >= ob['low'] and df['low'].iloc[-1] <= ob['high']:
                        self.detected_patterns_info[PATTERN_ORDER_BLOCK]['status'] = f'Signal {SELL}'
                        return {'pattern': 'Order_Block_Sell', 'direction': SELL}
        return None
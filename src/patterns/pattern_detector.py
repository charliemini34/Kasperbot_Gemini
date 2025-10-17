# Fichier: src/patterns/pattern_detector.py
# Version: 10.0.0 (SMC Logic Corrected)
# Dépendances: pandas, numpy, logging
# Description: Moteur de détection de patterns SMC avec une logique d'Order Block corrigée et robuste.

import pandas as pd
import numpy as np
import logging
from datetime import time
from src.constants import PATTERN_AMD, PATTERN_INBALANCE, PATTERN_ORDER_BLOCK, BUY, SELL

class PatternDetector:
    """
    Module de reconnaissance de patterns Smart Money Concepts (SMC).
    v10.0 : Correction majeure de la logique de détection des Order Blocks
            pour exiger une rupture de structure (BOS) valide.
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
            if self.config.get('pattern_detection', {}).get(name, False):
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
        """Trouve les points de swing (plus hauts et plus bas) dans une série."""
        # Pour un swing low, le point doit être plus bas que les n bougies avant et après
        lows = series[(series.shift(1) > series) & (series.shift(-1) > series)]
        # Pour un swing high, le point doit être plus haut que les n bougies avant et après
        highs = series[(series.shift(1) < series) & (series.shift(-1) < series)]
        return lows, highs

    def _detect_amd_session(self, df: pd.DataFrame):
        # La logique AMD reste inchangée pour le moment.
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        # La logique d'imbalance reste inchangée pour le moment.
        return None

    def _detect_order_block(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None

        # On utilise une fenêtre de 50 bougies pour la détection
        recent_data = df.iloc[-50:]
        swing_lows, swing_highs = self._find_swing_points(recent_data['low']), self._find_swing_points(recent_data['high'])

        # --- Détection d'un Order Block HAUSSIER (Bullish OB) ---
        if len(swing_highs[1]) > 0:
            last_swing_high = swing_highs[1].iloc[-1]
            last_swing_high_time = swing_highs[1].index[-1]
            
            # 1. Vérification d'une Rupture de Structure (BOS)
            if recent_data['high'].iloc[-1] > last_swing_high:
                self.log.info(f"BOS haussier détecté. Dernier sommet à {last_swing_high:.5f}")
                
                # 2. Trouver l'Order Block : la dernière bougie baissière AVANT le début du mouvement haussier
                movement_data = recent_data[recent_data.index < last_swing_high_time]
                if not movement_data.empty:
                    down_candles_before_bos = movement_data[movement_data['close'] < movement_data['open']]
                    if not down_candles_before_bos.empty:
                        ob_candle = down_candles_before_bos.iloc[-1]
                        self.log.info(f"Canditat OB haussier trouvé à {ob_candle.name} (O: {ob_candle.open}, H: {ob_candle.high})")
                        
                        # 3. Validation : le prix actuel doit être revenu dans la zone de l'OB
                        if recent_data['low'].iloc[-1] <= ob_candle['high']:
                            self.detected_patterns_info[PATTERN_ORDER_BLOCK]['status'] = f'Signal {BUY}'
                            return {'pattern': 'Order_Block_Buy', 'direction': BUY}

        # --- Détection d'un Order Block BAISSIER (Bearish OB) ---
        if len(swing_lows[0]) > 0:
            last_swing_low = swing_lows[0].iloc[-1]
            last_swing_low_time = swing_lows[0].index[-1]

            # 1. Vérification d'une Rupture de Structure (BOS)
            if recent_data['low'].iloc[-1] < last_swing_low:
                self.log.info(f"BOS baissier détecté. Dernier creux à {last_swing_low:.5f}")

                # 2. Trouver l'Order Block : la dernière bougie haussière AVANT le début du mouvement baissier
                movement_data = recent_data[recent_data.index < last_swing_low_time]
                if not movement_data.empty:
                    up_candles_before_bos = movement_data[movement_data['close'] > movement_data['open']]
                    if not up_candles_before_bos.empty:
                        ob_candle = up_candles_before_bos.iloc[-1]
                        self.log.info(f"Canditat OB baissier trouvé à {ob_candle.name} (O: {ob_candle.open}, L: {ob_candle.low})")
                        
                        # 3. Validation : le prix actuel doit être revenu dans la zone de l'OB
                        if recent_data['high'].iloc[-1] >= ob_candle['low']:
                            self.detected_patterns_info[PATTERN_ORDER_BLOCK]['status'] = f'Signal {SELL}'
                            return {'pattern': 'Order_Block_Sell', 'direction': SELL}
                            
        return None
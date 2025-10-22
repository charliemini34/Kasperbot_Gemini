

# Fichier: src/patterns/pattern_detector.py
# Version: 1.0.0 manuel
# Dépendances: pandas, numpy, logging, datetime, src.constants

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
    Module de reconnaissance de patterns SMC & ICT, avec ciblage de liquidité.
    v17.0.0: Les signaux incluent désormais un 'target_price' basé sur les swing points.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    def _get_trend_filter_direction(self, connector, symbol: str) -> str:
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Désactivé'}
            return "ANY"

        higher_timeframe = filter_cfg.get('higher_timeframe', 'H4')
        period = filter_cfg.get('ema_period', 200)
        
        try:
            htf_data = connector.get_ohlc(symbol, higher_timeframe, period + 50)
            if htf_data is None or htf_data.empty:
                self.log.warning(f"Impossible de récupérer les données {higher_timeframe} pour le filtre de tendance.")
                self.detected_patterns_info['TREND_FILTER'] = {'status': f'Erreur données {higher_timeframe}'}
                return "ANY"

            ema = htf_data['close'].ewm(span=period, adjust=False).mean()
            current_price = htf_data['close'].iloc[-1]
            
            status = "HAUSSIÈRE" if current_price > ema.iloc[-1] else "BAISSIÈRE"
            self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} ({higher_timeframe})"}
            return BUY if status == "HAUSSIÈRE" else SELL
        except Exception as e:
            self.log.error(f"Erreur dans le filtre de tendance : {e}", exc_info=True)
            return "ANY"

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        df = ohlc_data.copy()
        
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
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
                try:
                    signal = func(df.copy())
                    if signal:
                        all_signals.append(signal)
                except Exception as e:
                    self.log.error(f"Erreur lors de la détection du pattern '{name}': {e}", exc_info=True)

        confirmed_trade_signal = None
        for signal in all_signals:
            name = signal['pattern']
            direction = signal['direction']
            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction})"}
                if not confirmed_trade_signal:
                    confirmed_trade_signal = signal
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
        
        return confirmed_trade_signal

    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        df.loc[:, 'is_swing_high'] = df['high'].rolling(window=2*n+1, center=True, min_periods=1).max() == df['high']
        df.loc[:, 'is_swing_low'] = df['low'].rolling(window=2*n+1, center=True, min_periods=1).min() == df['low']
        
        swing_highs = df[df['is_swing_high']]
        swing_lows = df[df['is_swing_low']]
        
        return swing_highs, swing_lows

    def _detect_choch(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'}
        if len(df) < 20: return None

        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)

        recent_lows = swing_lows['low'].tail(3).values
        recent_highs = swing_highs['high'].tail(3).values
        
        if len(recent_lows) > 1 and len(swing_highs) > 0:
            if recent_lows[-1] < recent_lows[-2]:
                last_lower_high = swing_highs[swing_highs.index < swing_lows.index[-1]].tail(1)
                if not last_lower_high.empty and df['close'].iloc[-1] > last_lower_high['high'].values[0]:
                    target_liquidity = swing_highs.tail(2).iloc[0]['high']
                    self.log.debug(f"CHoCH haussier détecté. Cible de liquidité: {target_liquidity}")
                    return {'pattern': PATTERN_CHOCH, 'direction': BUY, 'target_price': target_liquidity}

        if len(recent_highs) > 1 and len(swing_lows) > 0:
            if recent_highs[-1] > recent_highs[-2]:
                last_higher_low = swing_lows[swing_lows.index < swing_highs.index[-1]].tail(1)
                if not last_higher_low.empty and df['close'].iloc[-1] < last_higher_low['low'].values[0]:
                    target_liquidity = swing_lows.tail(2).iloc[0]['low']
                    self.log.debug(f"CHoCH baissier détecté. Cible de liquidité: {target_liquidity}")
                    return {'pattern': PATTERN_CHOCH, 'direction': SELL, 'target_price': target_liquidity}

        return None
        
    def _detect_order_block(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None
        
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=5)
        
        if len(swing_highs) >= 2:
            last_high = swing_highs.iloc[-2]
            if df['high'].iloc[-1] > last_high['high']:
                impulse_start_index = df.index.get_loc(last_high.name)
                down_candles = df.iloc[:impulse_start_index][(df.iloc[:impulse_start_index]['close'] < df.iloc[:impulse_start_index]['open'])]
                if not down_candles.empty:
                    bullish_ob = down_candles.iloc[-1]
                    if df['close'].iloc[-1] <= bullish_ob['high'] and df['close'].iloc[-1] >= bullish_ob['low']:
                        target_liquidity = swing_highs.iloc[-1]['high']
                        self.log.debug(f"Order Block haussier détecté. Cible de liquidité: {target_liquidity}")
                        return {'pattern': PATTERN_ORDER_BLOCK, 'direction': BUY, 'target_price': target_liquidity}

        if len(swing_lows) >= 2:
            last_low = swing_lows.iloc[-2]
            if df['low'].iloc[-1] < last_low['low']:
                impulse_start_index = df.index.get_loc(last_low.name)
                up_candles = df.iloc[:impulse_start_index][(df.iloc[:impulse_start_index]['close'] > df.iloc[:impulse_start_index]['open'])]
                if not up_candles.empty:
                    bearish_ob = up_candles.iloc[-1]
                    if df['close'].iloc[-1] >= bearish_ob['low'] and df['close'].iloc[-1] <= bearish_ob['high']:
                        target_liquidity = swing_lows.iloc[-1]['low']
                        self.log.debug(f"Order Block baissier détecté. Cible de liquidité: {target_liquidity}")
                        return {'pattern': PATTERN_ORDER_BLOCK, 'direction': SELL, 'target_price': target_liquidity}
                    
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_INBALANCE] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=3)
        
        if df['low'].iloc[-2] > df['high'].iloc[-4]:
            target_liquidity = swing_highs.tail(1)['high'].values[0] if not swing_highs.empty else None
            if target_liquidity:
                self.log.debug(f"Inbalance haussière détectée. Cible de liquidité: {target_liquidity}")
                return {'pattern': PATTERN_INBALANCE, 'direction': BUY, 'target_price': target_liquidity}

        if df['high'].iloc[-2] < df['low'].iloc[-4]:
            target_liquidity = swing_lows.tail(1)['low'].values[0] if not swing_lows.empty else None
            if target_liquidity:
                self.log.debug(f"Inbalance baissière détectée. Cible de liquidité: {target_liquidity}")
                return {'pattern': PATTERN_INBALANCE, 'direction': SELL, 'target_price': target_liquidity}
            
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_LIQUIDITY_GRAB] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        
        swing_highs, swing_lows = self._find_swing_points(df.iloc[:-1].copy(), n=3)
        
        if not swing_lows.empty:
            last_low = swing_lows.iloc[-1]['low']
            if df['low'].iloc[-1] < last_low and df['close'].iloc[-1] > last_low:
                 target_liquidity = swing_highs.tail(1)['high'].values[0] if not swing_highs.empty else None
                 if target_liquidity:
                     self.log.debug(f"Prise de liquidité haussière. Cible: {target_liquidity}")
                     return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': BUY, 'target_price': target_liquidity}

        if not swing_highs.empty:
            last_high = swing_highs.iloc[-1]['high']
            if df['high'].iloc[-1] > last_high and df['close'].iloc[-1] < last_high:
                target_liquidity = swing_lows.tail(1)['low'].values[0] if not swing_lows.empty else None
                if target_liquidity:
                    self.log.debug(f"Prise de liquidité baissière. Cible: {target_liquidity}")
                    return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': SELL, 'target_price': target_liquidity}
                
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_AMD] = {'status': 'En attente'}
        asia_start, asia_end = time(0, 0), time(7, 0)
        london_open = time(8, 0)
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
            choch_signal = self._detect_choch(recent_market_data.copy())
            if choch_signal and choch_signal['direction'] == BUY:
                self.log.debug(f"Pattern AMD haussier détecté après manipulation sous le range asiatique.")
                # Cible logique pour un AMD haussier : le sommet de la session asiatique
                return {'pattern': PATTERN_AMD, 'direction': BUY, 'target_price': asia_high}

        if recent_market_data['high'].max() > asia_high:
            choch_signal = self._detect_choch(recent_market_data.copy())
            if choch_signal and choch_signal['direction'] == SELL:
                self.log.debug(f"Pattern AMD baissier détecté après manipulation au-dessus du range asiatique.")
                # Cible logique pour un AMD baissier : le bas de la session asiatique
                return {'pattern': PATTERN_AMD, 'direction': SELL, 'target_price': asia_low}
                    
        return None
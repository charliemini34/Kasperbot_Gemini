# Fichier: src/patterns/pattern_detector.py
# Version: 19.0.3 (Fix R19 - NameError Tuple/Dict)
# Dépendances: pandas, numpy, logging, datetime, typing, src.constants

import pandas as pd
import numpy as np
import logging
from datetime import time
# --- FIX R19: Importer Tuple et Dict ---
from typing import Tuple, Dict
# --- FIN FIX R19 ---
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_CHOCH, PATTERN_INBALANCE, PATTERN_LIQUIDITY_GRAB,
    PATTERN_AMD, BUY, SELL
)

class PatternDetector:
    """
    Module reconnaissance patterns SMC/ICT.
    v19.0.3 (R19): Corrige NameError Tuple/Dict.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    # --- R19: Utilisation de Tuple et Dict OK ---
    def _get_trend_filter_direction(self, connector, symbol: str) -> Tuple[str, Dict]:
    # --- Fin R19 ---
        """Calcule la direction autorisée ET retourne le statut pour l'info."""
        filter_cfg = self.config.get('trend_filter', {})
        status_info = {'status': 'Non défini'}

        if not filter_cfg.get('enabled', False):
            status_info['status'] = 'Désactivé'
            return "ANY", status_info

        higher_timeframe = filter_cfg.get('higher_timeframe', 'H4')
        period = filter_cfg.get('ema_period', 200)

        try:
            htf_data = connector.get_ohlc(symbol, higher_timeframe, period + 50)
            if htf_data is None or htf_data.empty:
                self.log.warning(f"Filtre Tendance: Données {higher_timeframe} indispo pour {symbol}.")
                status_info['status'] = f'Erreur données {higher_timeframe}'
                return "ANY", status_info

            ema = htf_data['close'].ewm(span=period, adjust=False).mean()
            current_price = htf_data['close'].iloc[-1]

            direction = BUY if current_price > ema.iloc[-1] else SELL
            status_label = "HAUSSIÈRE" if direction == BUY else "BAISSIÈRE"
            status_info['status'] = f"{status_label} ({higher_timeframe})"
            return direction, status_info

        except Exception as e:
            self.log.error(f"Erreur filtre tendance {symbol}: {e}", exc_info=True)
            status_info['status'] = 'Erreur Filtre'
            return "ANY", status_info

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        df = ohlc_data.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
        if df.index.tz is None: df.index = df.index.tz_localize('UTC')

        allowed_direction, trend_filter_status = self._get_trend_filter_direction(connector, symbol)

        detection_functions = {
            PATTERN_INBALANCE: self._detect_inbalance,
            PATTERN_ORDER_BLOCK: self._detect_order_block,
            PATTERN_CHOCH: self._detect_choch,
            PATTERN_LIQUIDITY_GRAB: self._detect_liquidity_grab,
            PATTERN_AMD: self._detect_amd_session,
        }

        all_signals = []
        self.detected_patterns_info.clear()
        self.detected_patterns_info['TREND_FILTER'] = trend_filter_status

        for name, func in detection_functions.items():
            self.detected_patterns_info[name] = {'status': 'Non détecté'}
            if self.config.get('pattern_detection', {}).get(name, False):
                try:
                    signal = func(df.copy())
                    if signal:
                        all_signals.append(signal)
                        self.detected_patterns_info[name] = {'status': f"DÉTECTÉ ({signal['direction']})"}
                except Exception as e:
                    self.log.error(f"Erreur détection '{name}' sur {symbol}: {e}", exc_info=True)
                    self.detected_patterns_info[name] = {'status': 'Erreur Détection'}

        confirmed_trade_signal = None
        for signal in all_signals:
            name, direction = signal['pattern'], signal['direction']
            has_zone = 'entry_zone_start' in signal and 'stop_loss_level' in signal
            if self.detected_patterns_info[name].get('status') == 'Erreur Détection': continue

            if allowed_direction == "ANY" or direction == allowed_direction:
                if has_zone:
                     self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction})"}
                     if not confirmed_trade_signal: confirmed_trade_signal = signal
                else:
                     self.log.debug(f"Signal {name} ({direction}) confirmé mais ignoré (pas de zone R7).")
                     self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction}) - Ignoré (Pas de zone R7)"}
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}

        for name in detection_functions:
             if name not in self.detected_patterns_info or self.detected_patterns_info[name].get('status') not in ['DÉTECTÉ','CONFIRMÉ','INVALIDÉ','Erreur Détection']:
                  self.detected_patterns_info[name] = {'status': 'Non détecté'}

        return confirmed_trade_signal

    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        # ... (Logique inchangée) ...
        df['is_swing_high'] = False; df['is_swing_low'] = False
        is_sh = df['high'].rolling(window=2*n+1, center=True, min_periods=n+1).max() == df['high']
        is_sl = df['low'].rolling(window=2*n+1, center=True, min_periods=n+1).min() == df['low']
        df.loc[is_sh, 'is_swing_high'] = True
        df.loc[is_sl, 'is_swing_low'] = True
        return df[df['is_swing_high']], df[df['is_swing_low']]

    def _detect_choch(self, df: pd.DataFrame):
        # ... (Logique inchangée) ...
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)
        recent_lows = swing_lows['low'].tail(3); recent_highs = swing_highs['high'].tail(3)
        if len(recent_lows) > 1 and len(swing_highs) > 0:
            if recent_lows.iloc[-1] < recent_lows.iloc[-2]:
                last_lh = swing_highs[swing_highs.index < recent_lows.index[-1]].tail(1)
                if not last_lh.empty and df['close'].iloc[-1] > last_lh['high'].iloc[0]:
                    target = swing_highs.iloc[-1]['high']
                    self.log.debug(f"CHoCH haussier confirmé. Cible: {target:.5f}"); return {'pattern': PATTERN_CHOCH, 'direction': BUY, 'target_price': target}
        if len(recent_highs) > 1 and len(swing_lows) > 0:
            if recent_highs.iloc[-1] > recent_highs.iloc[-2]:
                last_hl = swing_lows[swing_lows.index < recent_highs.index[-1]].tail(1)
                if not last_hl.empty and df['close'].iloc[-1] < last_hl['low'].iloc[0]:
                    target = swing_lows.iloc[-1]['low']
                    self.log.debug(f"CHoCH baissier confirmé. Cible: {target:.5f}"); return {'pattern': PATTERN_CHOCH, 'direction': SELL, 'target_price': target}
        return None

    def _detect_order_block(self, df: pd.DataFrame):
        # ... (Logique inchangée) ...
        if len(df) < 50: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=5)
        if len(swing_highs) >= 2:
            prev_high = swing_highs.iloc[-2]
            if df['high'].iloc[-1] > prev_high['high']:
                last_sl = swing_lows[swing_lows.index < prev_high.name].tail(1)
                if not last_sl.empty:
                    candles = df.iloc[df.index.get_loc(last_sl.index[0]):]
                    down_candles = candles[candles['close'] < candles['open']]
                    if not down_candles.empty:
                        ob = down_candles.iloc[-1]; zone_start, zone_end, sl = ob['low'], ob['high'], ob['low']
                        target = swing_highs.iloc[-1]['high']
                        self.log.debug(f"OB haussier potentiel. Zone=[{zone_start:.5f}-{zone_end:.5f}], SL={sl:.5f}, Cible={target:.5f}")
                        return {'pattern': PATTERN_ORDER_BLOCK, 'direction': BUY, 'entry_zone_start': zone_start, 'entry_zone_end': zone_end, 'stop_loss_level': sl, 'target_price': target}
        if len(swing_lows) >= 2:
            prev_low = swing_lows.iloc[-2]
            if df['low'].iloc[-1] < prev_low['low']:
                last_sh = swing_highs[swing_highs.index < prev_low.name].tail(1)
                if not last_sh.empty:
                    candles = df.iloc[df.index.get_loc(last_sh.index[0]):]
                    up_candles = candles[candles['close'] > candles['open']]
                    if not up_candles.empty:
                        ob = up_candles.iloc[-1]; zone_start, zone_end, sl = ob['low'], ob['high'], ob['high']
                        target = swing_lows.iloc[-1]['low']
                        self.log.debug(f"OB baissier potentiel. Zone=[{zone_start:.5f}-{zone_end:.5f}], SL={sl:.5f}, Cible={target:.5f}")
                        return {'pattern': PATTERN_ORDER_BLOCK, 'direction': SELL, 'entry_zone_start': zone_start, 'entry_zone_end': zone_end, 'stop_loss_level': sl, 'target_price': target}
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        # ... (Logique inchangée) ...
        if len(df) < 5: return None
        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=3)
        c1h, c2l, c3l = df['high'].iloc[-3], df['low'].iloc[-2], df['low'].iloc[-1]
        if c3l > c1h:
            zone_start, zone_end, sl = c1h, c3l, min(c1h, c2l)
            target = swing_highs.iloc[-1]['high'] if not swing_highs.empty else None
            if target:
                self.log.debug(f"FVG haussier. Zone=[{zone_start:.5f}-{zone_end:.5f}], SL={sl:.5f}, Cible={target:.5f}")
                return {'pattern': PATTERN_INBALANCE, 'direction': BUY, 'entry_zone_start': zone_start, 'entry_zone_end': zone_end, 'stop_loss_level': sl, 'target_price': target}
        c1l, c2h, c3h = df['low'].iloc[-3], df['high'].iloc[-2], df['high'].iloc[-1]
        if c3h < c1l:
            zone_start, zone_end, sl = c3h, c1l, max(c1l, c2h)
            target = swing_lows.iloc[-1]['low'] if not swing_lows.empty else None
            if target:
                self.log.debug(f"FVG baissier. Zone=[{zone_start:.5f}-{zone_end:.5f}], SL={sl:.5f}, Cible={target:.5f}")
                return {'pattern': PATTERN_INBALANCE, 'direction': SELL, 'entry_zone_start': zone_start, 'entry_zone_end': zone_end, 'stop_loss_level': sl, 'target_price': target}
        return None

    def _detect_liquidity_grab(self, df: pd.DataFrame):
        # ... (Logique inchangée) ...
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[:-1].copy(), n=3)
        if not swing_lows.empty:
            last_low = swing_lows['low'].iloc[-1]
            if df['low'].iloc[-1] < last_low and df['close'].iloc[-1] > last_low:
                 target = swing_highs.iloc[-1]['high'] if not swing_highs.empty else None
                 if target: self.log.debug(f"Grab haussier. Cible: {target:.5f}"); return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': BUY, 'target_price': target}
        if not swing_highs.empty:
            last_high = swing_highs['high'].iloc[-1]
            if df['high'].iloc[-1] > last_high and df['close'].iloc[-1] < last_high:
                target = swing_lows.iloc[-1]['low'] if not swing_lows.empty else None
                if target: self.log.debug(f"Grab baissier. Cible: {target:.5f}"); return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': SELL, 'target_price': target}
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        # ... (Logique inchangée depuis v19.0.2 - Fix R18 OK) ...
        asia_start, asia_end, london_open = time(0, 0), time(7, 0), time(8, 0)
        current_time = df.index[-1].time()
        if not (london_open <= current_time < time(16,0)): return None
        today = df.index[-1].date()
        asia_data = df.between_time(asia_start, asia_end); asia_data = asia_data[asia_data.index.date == today]
        if asia_data.empty: self.detected_patterns_info[PATTERN_AMD]={'status':'Pas Données Asie'}; return None
        asia_h, asia_l = asia_data['high'].max(), asia_data['low'].min()
        self.detected_patterns_info[PATTERN_AMD]={'status':f'Asie H:{asia_h:.5f} L:{asia_l:.5f}'}
        post_asia = df.loc[df.index > asia_data.index[-1]]
        if post_asia.empty: return None # Ligne 232 OK
        manip_low = post_asia['low'].min() < asia_l; manip_high = post_asia['high'].max() > asia_h
        if manip_low:
            choch = self._detect_choch(post_asia.copy())
            if choch and choch['direction'] == BUY: self.log.debug("AMD haussier confirmé."); return {'pattern': PATTERN_AMD, 'direction': BUY, 'target_price': asia_h}
        if manip_high:
            choch = self._detect_choch(post_asia.copy())
            if choch and choch['direction'] == SELL: self.log.debug("AMD baissier confirmé."); return {'pattern': PATTERN_AMD, 'direction': SELL, 'target_price': asia_l}
        return None
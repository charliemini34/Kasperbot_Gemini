# Fichier: src/patterns/pattern_detector.py
# Version: 18.0.2 (AMD-Timezone-Fix) # <-- Version mise à jour
# Dépendances: pandas, numpy, logging, datetime, pytz, typing, src.constants

import pandas as pd
import numpy as np
import logging
from datetime import time, timedelta, datetime
import pytz
from typing import Tuple, Dict, Optional # Import corrigé (était dans v18.0.1)

from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_CHOCH, PATTERN_INBALANCE, PATTERN_LIQUIDITY_GRAB,
    PATTERN_AMD, PATTERN_BOS,
    BUY, SELL, ANY
)

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC & ICT, avec ciblage de liquidité.
    v18.0.2: Corrige TypeError 'offset-naive vs offset-aware' dans _detect_amd_session
             et robustifie la conversion de l'index en UTC.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {
            'TREND_FILTER': {'status': 'Non exécuté'},
            PATTERN_CHOCH: {'status': 'Non exécuté'},
            PATTERN_ORDER_BLOCK: {'status': 'Non exécuté'},
            PATTERN_INBALANCE: {'status': 'Non exécuté'},
            PATTERN_LIQUIDITY_GRAB: {'status': 'Non exécuté'},
            PATTERN_AMD: {'status': 'Non exécuté'},
            PATTERN_BOS: {'status': 'Non exécuté'}
        }

    def get_detected_patterns_info(self) -> Dict:
        return self.detected_patterns_info.copy()

    def _get_htf_bias(self, connector, symbol: str) -> str:
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Désactivé'}
            return ANY

        higher_timeframe = filter_cfg.get('higher_timeframe', 'H4')
        period = filter_cfg.get('ema_period', 200)
        num_bars_htf = period + 50

        try:
            htf_data = connector.get_ohlc(symbol, higher_timeframe, num_bars_htf)
            if htf_data is None or htf_data.empty or len(htf_data) < period:
                self.log.warning(f"Données HTF ({higher_timeframe}) insuffisantes ({len(htf_data) if htf_data is not None else 0}/{period}) pour le filtre de tendance sur {symbol}.")
                self.detected_patterns_info['TREND_FILTER'] = {'status': f'Erreur données {higher_timeframe}'}
                return ANY

            ema = htf_data['close'].ewm(span=period, adjust=False).mean()
            current_price = htf_data['close'].iloc[-1]
            previous_price = htf_data['close'].iloc[-2]
            ema_value = ema.iloc[-1]
            ema_previous = ema.iloc[-2]

            if current_price > ema_value and previous_price > ema_previous:
                status = "HAUSSIÈRE"
                bias = BUY
            elif current_price < ema_value and previous_price < ema_previous:
                status = "BAISSIÈRE"
                bias = SELL
            else:
                status = "NEUTRE/TRANSITION"
                bias = ANY

            self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} ({higher_timeframe} EMA{period})"}
            return bias
        except Exception as e:
            self.log.error(f"Erreur dans le filtre de tendance HTF : {e}", exc_info=True)
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Erreur calcul'}
            return ANY

    # --- MODIFIÉ (Robustification conversion UTC) ---
    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str) -> Optional[Dict]:
        """Détecte les patterns SMC et retourne le premier signal confirmé par le filtre MTF."""
        for key in self.detected_patterns_info:
            if key != 'TREND_FILTER' or self.detected_patterns_info[key]['status'] == 'Non exécuté':
                 self.detected_patterns_info[key] = {'status': 'En attente...'}

        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < 50:
            self.log.warning(f"Données OHLC insuffisantes ({len(ohlc_data) if ohlc_data is not None else 0}) pour la détection de patterns sur {symbol}.")
            return None

        df = ohlc_data.copy()

        # --- Logique de conversion UTC améliorée ---
        # 1. Si l'index N'EST PAS un DatetimeIndex (cas: 'time' est une colonne)
        if not isinstance(df.index, pd.DatetimeIndex):
            if 'time' in df.columns:
                # Convertir la colonne 'time' (supposée en secondes epoch/UTC) en DatetimeIndex AWARE
                df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
                df.set_index('time', inplace=True)
            else:
                self.log.error(f"Erreur critique: ohlc_data pour {symbol} n'a ni DatetimeIndex, ni colonne 'time'.")
                return None
        # 2. Si l'index EST un DatetimeIndex mais NAIVE (sans fuseau horaire)
        elif df.index.tz is None:
            try:
                # Localiser l'index comme étant UTC (cas: MT5 a fourni des temps UTC naifs)
                df.index = df.index.tz_localize(pytz.UTC)
            except Exception as e: # Gérer les erreurs de localisation (ex: DST)
                 self.log.error(f"Erreur lors de la localisation de l'index en UTC pour {symbol}: {e}")
                 # Tenter une conversion plus agressive (peut être risqué)
                 try:
                      df.index = df.index.map(lambda x: x.replace(tzinfo=pytz.UTC))
                 except Exception:
                      return None # Abandon si impossible
        # 3. Si l'index EST un DatetimeIndex mais PAS UTC
        elif df.index.tz != pytz.UTC:
            df.index = df.index.tz_convert(pytz.UTC)
        # --- Fin logique UTC ---
        
        # À ce stade, df.index DOIT être AWARE et en UTC

        htf_bias = self._get_htf_bias(connector, symbol)

        detection_functions = {
            PATTERN_AMD: self._detect_amd_session,
            PATTERN_LIQUIDITY_GRAB: self._detect_liquidity_grab,
            PATTERN_CHOCH: self._detect_choch,
            PATTERN_ORDER_BLOCK: self._detect_order_block,
            PATTERN_INBALANCE: self._detect_inbalance,
            PATTERN_BOS: self._detect_bos
        }

        all_signals = []
        for name, func in detection_functions.items():
            if self.config.get('pattern_detection', {}).get(name, False) or name == PATTERN_BOS:
                try:
                    signal = func(df.copy())
                    if signal:
                        signal['pattern'] = name
                        all_signals.append(signal)
                        self.detected_patterns_info[name] = {'status': f"DÉTECTÉ ({signal['direction']})"}
                    else:
                        self.detected_patterns_info[name] = {'status': 'Pas de signal'}
                except Exception as e:
                    self.log.error(f"Erreur lors de la détection du pattern '{name}' sur {symbol}: {e}", exc_info=True)
                    self.detected_patterns_info[name] = {'status': 'Erreur détection'}

        confirmed_trade_signal = None
        bos_signal_info = None

        for signal in all_signals:
            name = signal['pattern']
            direction = signal['direction']
            if name == PATTERN_BOS:
                bos_signal_info = signal
                continue
            is_aligned = (htf_bias == ANY or direction == htf_bias)
            if is_aligned:
                self.detected_patterns_info[name]['status'] = f"CONFIRMÉ ({direction}, Tendance={htf_bias})"
                if not confirmed_trade_signal:
                    confirmed_trade_signal = signal
            else:
                self.detected_patterns_info[name]['status'] = f"INVALIDÉ ({direction} vs Tendance {htf_bias})"

        if confirmed_trade_signal:
             confirmed_trade_signal['is_with_bos'] = (bos_signal_info['direction'] == confirmed_trade_signal['direction']) if bos_signal_info else None

        return confirmed_trade_signal

    def _find_swing_points(self, df: pd.DataFrame, n: int = 2) -> Tuple[pd.DataFrame, pd.DataFrame]:
        if len(df) < (2*n + 1):
            return pd.DataFrame(), pd.DataFrame()
        df['is_swing_high'] = df['high'] == df['high'].rolling(window=2*n+1, center=True, min_periods=n+1).max()
        df['is_swing_low'] = df['low'] == df['low'].rolling(window=2*n+1, center=True, min_periods=n+1).min()
        swing_highs = df[df['is_swing_high']].copy()
        swing_lows = df[df['is_swing_low']].copy()
        if not swing_highs.empty:
             swing_highs = swing_highs[~swing_highs.index.duplicated(keep='first')]
        if not swing_lows.empty:
             swing_lows = swing_lows[~swing_lows.index.duplicated(keep='first')]
        return swing_highs, swing_lows

    def _detect_choch(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)
        if swing_highs.empty or swing_lows.empty: return None

        last_close = df['close'].iloc[-1]
        last_candle_index = df.index[-1]

        # CHoCH Haussier
        last_swing_low_time = swing_lows.index[-1]
        if last_swing_low_time >= last_candle_index:
             if len(swing_lows) < 2: return None
             last_swing_low_time = swing_lows.index[-2]
        swing_high_before_last_low = swing_highs[swing_highs.index < last_swing_low_time]
        if not swing_high_before_last_low.empty:
            choch_level_high = swing_high_before_last_low['high'].iloc[-1]
            if last_close > choch_level_high:
                potential_targets = swing_highs[swing_highs.index < swing_high_before_last_low.index[-1]]
                target_liquidity = potential_targets['high'].iloc[-1] if not potential_targets.empty else swing_highs['high'].iloc[-1]
                self.log.debug(f"CHoCH haussier (clôture) détecté. Niveau cassé: {choch_level_high:.5f}, Cible: {target_liquidity:.5f}")
                return {'direction': BUY, 'target_price': target_liquidity, 'choch_level': choch_level_high}

        # CHoCH Baissier
        last_swing_high_time = swing_highs.index[-1]
        if last_swing_high_time >= last_candle_index:
             if len(swing_highs) < 2: return None
             last_swing_high_time = swing_highs.index[-2]
        swing_low_before_last_high = swing_lows[swing_lows.index < last_swing_high_time]
        if not swing_low_before_last_high.empty:
            choch_level_low = swing_low_before_last_high['low'].iloc[-1]
            if last_close < choch_level_low:
                potential_targets = swing_lows[swing_lows.index < swing_low_before_last_high.index[-1]]
                target_liquidity = potential_targets['low'].iloc[-1] if not potential_targets.empty else swing_lows['low'].iloc[-1]
                self.log.debug(f"CHoCH baissier (clôture) détecté. Niveau cassé: {choch_level_low:.5f}, Cible: {target_liquidity:.5f}")
                return {'direction': SELL, 'target_price': target_liquidity, 'choch_level': choch_level_low}

        return None

    def _detect_bos(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)
        if swing_highs.empty or swing_lows.empty: return None
        last_close = df['close'].iloc[-1]

        if len(swing_highs) >= 2:
             if swing_highs['high'].iloc[-1] > swing_highs['high'].iloc[-2]:
                 bos_level_high = swing_highs['high'].iloc[-1]
                 if last_close > bos_level_high:
                     self.log.debug(f"BOS haussier (clôture) détecté. Niveau cassé: {bos_level_high:.5f}")
                     return {'direction': BUY, 'bos_level': bos_level_high}

        if len(swing_lows) >= 2:
             if swing_lows['low'].iloc[-1] < swing_lows['low'].iloc[-2]:
                 bos_level_low = swing_lows['low'].iloc[-1]
                 if last_close < bos_level_low:
                     self.log.debug(f"BOS baissier (clôture) détecté. Niveau cassé: {bos_level_low:.5f}")
                     return {'direction': SELL, 'bos_level': bos_level_low}
        return None

    def _detect_order_block(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 50: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)
        if swing_highs.empty or swing_lows.empty: return None
        last_5_candles = df.iloc[-5:]

        impulse_up = (last_5_candles['close'].iloc[-3:] > last_5_candles['open'].iloc[-3:]).all()
        if impulse_up:
            search_df = df.iloc[-10:-3]
            potential_ob_candles = search_df[search_df['close'] < search_df['open']]
            if not potential_ob_candles.empty:
                potential_ob_candle = potential_ob_candles.iloc[-1]
                ob_low, ob_high = potential_ob_candle['low'], potential_ob_candle['high']
                if ob_low <= df['close'].iloc[-1] <= ob_high:
                    self.log.debug(f"Order Block haussier détecté [{ob_low:.5f}-{ob_high:.5f}]. Cible: {swing_highs['high'].iloc[-1]:.5f}")
                    return {'direction': BUY, 'target_price': swing_highs['high'].iloc[-1], 'ob_low': ob_low, 'ob_high': ob_high}

        impulse_down = (last_5_candles['close'].iloc[-3:] < last_5_candles['open'].iloc[-3:]).all()
        if impulse_down:
            search_df = df.iloc[-10:-3]
            potential_ob_candles = search_df[search_df['close'] > search_df['open']]
            if not potential_ob_candles.empty:
                potential_ob_candle = potential_ob_candles.iloc[-1]
                ob_low, ob_high = potential_ob_candle['low'], potential_ob_candle['high']
                if ob_low <= df['close'].iloc[-1] <= ob_high:
                    self.log.debug(f"Order Block baissier détecté [{ob_low:.5f}-{ob_high:.5f}]. Cible: {swing_lows['low'].iloc[-1]:.5f}")
                    return {'direction': SELL, 'target_price': swing_lows['low'].iloc[-1], 'ob_low': ob_low, 'ob_high': ob_high}
        return None

    def _detect_inbalance(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 5: return None
        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=3)

        for i in range(-5, -3):
            if abs(i) + 1 > len(df): continue # Vérifier limites
            fvg_high_candle_idx, fvg_low_candle_idx, middle_candle_idx = i - 1, i + 1, i
            fvg_high, fvg_low = df['high'].iloc[fvg_high_candle_idx], df['low'].iloc[fvg_low_candle_idx]
            middle_low = df['low'].iloc[middle_candle_idx]
            if middle_low > fvg_high:
                if (df['low'].iloc[fvg_low_candle_idx+1:].min() > fvg_high): # Non mitigé
                    target = swing_highs['high'].iloc[-1] if not swing_highs.empty else df['high'].iloc[-20:].max()
                    self.log.debug(f"Inbalance haussière non mitigée détectée [{fvg_high:.5f}-{fvg_low:.5f}]. Cible: {target:.5f}")
                    return {'direction': BUY, 'target_price': target, 'fvg_low': fvg_high, 'fvg_high': fvg_low}

        for i in range(-5, -3):
            if abs(i) + 1 > len(df): continue
            fvg_high_candle_idx, fvg_low_candle_idx, middle_candle_idx = i - 1, i + 1, i
            fvg_low, fvg_high = df['low'].iloc[fvg_high_candle_idx], df['high'].iloc[fvg_low_candle_idx]
            middle_high = df['high'].iloc[middle_candle_idx]
            if middle_high < fvg_low:
                if (df['high'].iloc[fvg_low_candle_idx+1:].max() < fvg_low): # Non mitigé
                    target = swing_lows['low'].iloc[-1] if not swing_lows.empty else df['low'].iloc[-20:].min()
                    self.log.debug(f"Inbalance baissière non mitigée détectée [{fvg_low:.5f}-{fvg_high:.5f}]. Cible: {target:.5f}")
                    return {'direction': SELL, 'target_price': target, 'fvg_low': fvg_low, 'fvg_high': fvg_high}
        return None

    def _detect_liquidity_grab(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 20: return None
        swing_highs, swing_lows = self._find_swing_points(df.iloc[:-1].copy(), n=3)
        if swing_lows.empty and swing_highs.empty: return None
        last_candle = df.iloc[-1]

        if not swing_lows.empty:
            last_relevant_low = swing_lows['low'].iloc[-1]
            if last_candle['low'] < last_relevant_low and last_candle['close'] > last_relevant_low:
                 target = swing_highs['high'].iloc[-1] if not swing_highs.empty else df['high'].iloc[-20:].max()
                 self.log.debug(f"Prise de liquidité haussière sous {last_relevant_low:.5f}. Cible: {target:.5f}")
                 return {'direction': BUY, 'target_price': target}

        if not swing_highs.empty:
            last_relevant_high = swing_highs['high'].iloc[-1]
            if last_candle['high'] > last_relevant_high and last_candle['close'] < last_relevant_high:
                target = swing_lows['low'].iloc[-1] if not swing_lows.empty else df['low'].iloc[-20:].min()
                self.log.debug(f"Prise de liquidité baissière au-dessus de {last_relevant_high:.5f}. Cible: {target:.5f}")
                return {'direction': SELL, 'target_price': target}
        return None

    # --- MODIFIÉ (Correction Timezone) ---
    def _detect_amd_session(self, df: pd.DataFrame) -> Optional[Dict]:
        """Détecte un pattern AMD basé sur la session Asiatique (comparaison en string)."""
        # Définir les heures en UTC (comme objets time pour référence)
        asia_start_utc = time(0, 0, tzinfo=pytz.utc)
        asia_end_utc = time(7, 0, tzinfo=pytz.utc)
        london_open_utc = time(8, 0, tzinfo=pytz.utc)
        london_close_utc = time(16, 0, tzinfo=pytz.utc)
        
        # Obtenir l'heure actuelle de la dernière bougie (doit être AWARE UTC grâce à detect_patterns)
        current_datetime_utc = df.index[-1]
        
        # --- CORRECTION ---
        # Utiliser des strings pour la comparaison avec between_time pour éviter les erreurs
        asia_start_str = asia_start_utc.strftime('%H:%M') # "00:00"
        asia_end_str = asia_end_utc.strftime('%H:%M') # "07:00"
        london_open_str = london_open_utc.strftime('%H:%M') # "08:00"
        london_close_str = london_close_utc.strftime('%H:%M') # "16:00"
        current_time_str = current_datetime_utc.strftime('%H:%M')

        # Vérifier si on est dans la session de Londres (basé sur les strings)
        if not (london_open_str <= current_time_str < london_close_str):
            self.detected_patterns_info[PATTERN_AMD] = {'status': 'Hors session Londres'}
            return None
        # --- FIN CORRECTION ---

        today_utc_date = current_datetime_utc.date()

        # Sélectionner les données de la session asiatique du jour
        # between_time est inclusif par défaut
        asia_session_today = df.between_time(asia_start_str, asia_end_str)
        # S'assurer qu'on ne prend que les données d'aujourd'hui (si between_time prend J-1)
        asia_session_today = asia_session_today[asia_session_today.index.date == today_utc_date]
        
        if asia_session_today.empty:
            self.detected_patterns_info[PATTERN_AMD] = {'status': 'Pas de données Asie'}
            return None

        asia_high = asia_session_today['high'].max()
        asia_low = asia_session_today['low'].min()
        self.detected_patterns_info[PATTERN_AMD]['status'] = f'Asie H:{asia_high:.5f} L:{asia_low:.5f}'

        # Données depuis l'ouverture de Londres jusqu'à maintenant
        recent_market_data = df.between_time(london_open_str, current_time_str)
        if recent_market_data.empty: return None

        # Manipulation basse
        manipulation_low = recent_market_data['low'].min()
        if manipulation_low < asia_low:
            # Chercher un CHoCH haussier DANS les données récentes (session Londres)
            choch_signal = self._detect_choch(recent_market_data.copy())
            if choch_signal and choch_signal['direction'] == BUY:
                self.log.debug(f"Pattern AMD haussier détecté (manipulation sous {asia_low:.5f}). Cible: {asia_high:.5f}")
                return {'direction': BUY, 'target_price': asia_high}

        # Manipulation haute
        manipulation_high = recent_market_data['high'].max()
        if manipulation_high > asia_high:
            choch_signal = self._detect_choch(recent_market_data.copy())
            if choch_signal and choch_signal['direction'] == SELL:
                self.log.debug(f"Pattern AMD baissier détecté (manipulation au-dessus de {asia_high:.5f}). Cible: {asia_low:.5f}")
                return {'direction': SELL, 'target_price': asia_low}

        return None
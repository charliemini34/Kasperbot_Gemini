# Fichier: src/patterns/pattern_detector.py

import pandas as pd
import numpy as np
import logging
from datetime import time

class PatternDetector:
    """
    Module de reconnaissance de patterns Smart Money Concepts (SMC).
    v6.0 : Implémentation du cycle AMD (Accumulation, Manipulation, Distribution).
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info

    def detect_patterns(self, ohlc_data: pd.DataFrame):
        """Passe en revue les stratégies de détection SMC activées."""
        self.detected_patterns_info = {}
        
        df = ohlc_data.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')

        if self.config.get('SMC_AMD_SESSION', False):
            trade_signal = self._detect_smc_amd_session(df)
            if trade_signal:
                return trade_signal
        
        return None

    def _find_swing_points(self, series, n=5):
        """Trouve les points de swing (hauts/bas) dans une série de prix."""
        lows = (series.shift(n) > series) & (series.shift(-n) > series)
        highs = (series.shift(n) < series) & (series.shift(-n) < series)
        return series[lows], series[highs]

    def _detect_smc_amd_session(self, df: pd.DataFrame):
        """
        Détecte le pattern Accumulation-Manipulation-Distribution (AMD) basé sur le range asiatique.
        """
        self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': 'Analyzing...'}
        last_candle_time = df.index[-1]

        # --- Phase 1: Accumulation (le range asiatique) ---
        # On ne procède à l'analyse que pendant les sessions de Londres/New York
        if not (time(7, 0) <= last_candle_time.time() <= time(20, 0)):
            return None

        asian_session = df.between_time('00:00', '06:59').loc[last_candle_time.date().strftime('%Y-%m-%d')]
        if len(asian_session) < 10:
            self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': 'Waiting for Asian Range'}
            return None

        asian_high = asian_session['high'].max()
        asian_low = asian_session['low'].min()
        self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': f'Asian Range: {asian_low:.2f}-{asian_high:.2f}'}

        # --- Phase 2: Manipulation (Prise de liquidité) ---
        recent_candles = df.loc[last_candle_time - pd.Timedelta(hours=6):]
        
        # --- Scénario de VENTE (Manipulation haussière) ---
        if recent_candles['high'].max() > asian_high:
            # On a bien pris la liquidité au-dessus de l'Asie.
            # Cherchons maintenant la confirmation : un Changement de Caractère (CHoCH) baissier.
            
            # 1. Trouver le dernier swing low avant le prix actuel
            _, swing_highs = self._find_swing_points(recent_candles['high'])
            swing_lows, _ = self._find_swing_points(recent_candles['low'])
            
            if swing_lows.empty or swing_highs.empty: return None

            # On prend le dernier swing low qui s'est formé APRES le dernier swing high
            # C'est la structure qui doit être cassée pour un CHoCH
            if swing_lows.index[-1] < swing_highs.index[-1]: return None
            choch_level = swing_lows.iloc[-1]

            # 2. Vérifier la cassure de structure (CHoCH)
            # La bougie actuelle doit clôturer sous le CHoCH, et la précédente devait être au-dessus.
            if df['close'].iloc[-1] < choch_level and df['close'].iloc[-2] >= choch_level:
                self.log.info(f"SMC AMD (SELL) Signal: Manipulation above {asian_high:.2f} confirmed by CHoCH below {choch_level:.2f}")
                self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': 'SELL SIGNAL'}
                return {'pattern': 'SMC_AMD_Sell', 'direction': 'SELL'}

        # --- Scénario d'ACHAT (Manipulation baissière) ---
        if recent_candles['low'].min() < asian_low:
            # On a pris la liquidité sous l'Asie. Cherchons un CHoCH haussier.
            
            # 1. Trouver le dernier swing high
            swing_lows, swing_highs = self._find_swing_points(recent_candles['low']), self._find_swing_points(recent_candles['high'])[1]
            if swing_highs.empty or swing_lows[0].empty: return None
            
            # On cherche le dernier swing high qui a précédé le nouveau plus bas
            if swing_highs.index[-1] < swing_lows[0].index[-1]: return None
            choch_level = swing_highs.iloc[-1]

            # 2. Vérifier la cassure de structure (CHoCH)
            if df['close'].iloc[-1] > choch_level and df['close'].iloc[-2] <= choch_level:
                self.log.info(f"SMC AMD (BUY) Signal: Manipulation below {asian_low:.2f} confirmed by CHoCH above {choch_level:.2f}")
                self.detected_patterns_info['SMC_AMD_SESSION'] = {'status': 'BUY SIGNAL'}
                return {'pattern': 'SMC_AMD_Buy', 'direction': 'BUY'}

        return None
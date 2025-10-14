# Fichier: src/scorer/strategy_scorer.py

import pandas as pd
import numpy as np
from datetime import time

class StrategyScorer:
    """
    Calcule les scores pour diverses stratégies de trading basées sur des données OHLC.
    Version améliorée pour une meilleure fiabilité des signaux.
    """

    def calculate_all(self, ohlc_data: pd.DataFrame) -> dict:
        """
        Calcule et retourne les scores bruts pour toutes les stratégies implémentées.

        Args:
            ohlc_data (pd.DataFrame): DataFrame contenant les données OHLC.

        Returns:
            dict: Un dictionnaire contenant les scores et directions pour chaque stratégie.
        """
        if ohlc_data is None or len(ohlc_data) < 50:
            return {}
        
        df = ohlc_data.copy()
            
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)

        scores = {
            "TREND": self._score_trend(df),
            "MEAN_REV": self._score_mean_reversion(df),
            "SMC": self._score_smc(df),
            "VOL_BRK": self._score_volatility_breakout(df),
            "LONDON_BRK": self._score_london_breakout(df),
            "INBALANCE": self._score_inbalance(df), # <-- NOUVELLE STRATÉGIE
        }
        return scores

    def _score_trend(self, df: pd.DataFrame) -> dict:
        """
        Score la force de la tendance basé sur le croisement des EMA et le MACD.
        Un score plus élevé indique une tendance plus forte et bien établie.
        """
        ema_fast = df['close'].ewm(span=12, adjust=False).mean()
        ema_slow = df['close'].ewm(span=26, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        
        score = 0.0
        direction = "NEUTRAL"

        distance = (ema_fast.iloc[-1] - ema_slow.iloc[-1]) / df['close'].iloc[-1]

        if ema_fast.iloc[-1] > ema_slow.iloc[-1] and macd_line.iloc[-1] > signal_line.iloc[-1]:
            direction = "BUY"
            score = np.tanh(distance * 200) * 100
        elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and macd_line.iloc[-1] < signal_line.iloc[-1]:
            direction = "SELL"
            score = np.tanh(abs(distance) * 200) * 100
        
        return {"score": max(0, score), "direction": direction}

    def _score_mean_reversion(self, df: pd.DataFrame) -> dict:
        """
        Score le potentiel de retour à la moyenne basé sur les Bandes de Bollinger et le RSI.
        """
        window = 20
        std_dev = df['close'].rolling(window).std()
        moving_average = df['close'].rolling(window).mean()
        upper_band = moving_average + (std_dev * 2)
        lower_band = moving_average - (std_dev * 2)
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        rs = gain / loss if not loss.empty and loss.iloc[-1] != 0 else pd.Series([np.inf] * len(gain))
        rsi = 100 - (100 / (1 + rs))

        last_close = df['close'].iloc[-1]
        score = 0.0
        direction = "NEUTRAL"

        if last_close > upper_band.iloc[-1] and rsi.iloc[-1] > 70:
            direction = "SELL"
            score = (rsi.iloc[-1] - 70) * (100 / 30)
        elif last_close < lower_band.iloc[-1] and rsi.iloc[-1] < 30:
            direction = "BUY"
            score = (30 - rsi.iloc[-1]) * (100 / 30)
        
        return {"score": min(100, max(0, score)), "direction": direction}

    def _score_smc(self, df: pd.DataFrame) -> dict:
        """
        Concepts Smart Money simplifiés : recherche une prise de liquidité suivie d'une cassure de structure (BoS).
        """
        if len(df) < 20: return {"score": 0, "direction": "NEUTRAL"}
        
        recent_candles = df.iloc[-20:]
        
        lows = recent_candles['low']
        swing_lows = lows[(lows.shift(2) > lows) & (lows.shift(1) > lows) & (lows.shift(-1) > lows) & (lows.shift(-2) > lows)]
        
        if not swing_lows.empty:
            last_swing_low_idx = swing_lows.index[-1]
            candles_after_low = recent_candles.loc[last_swing_low_idx:]
            
            highs_after_low = candles_after_low['high']
            swing_highs = highs_after_low[(highs_after_low.shift(1) < highs_after_low) & (highs_after_low.shift(-1) < highs_after_low)]
            
            if not swing_highs.empty:
                bos_level = swing_highs.max()
                if df['close'].iloc[-1] > bos_level:
                    return {"score": 85, "direction": "BUY"}

        highs = recent_candles['high']
        swing_highs = highs[(highs.shift(2) < highs) & (highs.shift(1) < highs) & (highs.shift(-1) < highs) & (highs.shift(-2) < highs)]

        if not swing_highs.empty:
            last_swing_high_idx = swing_highs.index[-1]
            candles_after_high = recent_candles.loc[last_swing_high_idx:]
            
            lows_after_high = candles_after_high['low']
            swing_lows = lows_after_high[(lows_after_high.shift(1) > lows_after_high) & (lows_after_high.shift(-1) > lows_after_high)]
            
            if not swing_lows.empty:
                bos_level = swing_lows.min()
                if df['close'].iloc[-1] < bos_level:
                    return {"score": 85, "direction": "SELL"}
                
        return {"score": 0, "direction": "NEUTRAL"}


    def _score_volatility_breakout(self, df: pd.DataFrame) -> dict:
        """Score une cassure d'un range de prix récent (canal de Donchian)."""
        window = 20
        recent_high = df['high'].iloc[-window:-1].max()
        recent_low = df['low'].iloc[-window:-1].min()
        
        range_size = (recent_high - recent_low)
        if range_size == 0: return {"score": 0, "direction": "NEUTRAL"}

        last_close = df['close'].iloc[-1]

        if last_close > recent_high:
            breakout_strength = (last_close - recent_high) / range_size
            score = min(100, 60 + breakout_strength * 80)
            return {"score": score, "direction": "BUY"}
            
        if last_close < recent_low:
            breakout_strength = (recent_low - last_close) / range_size
            score = min(100, 60 + breakout_strength * 80)
            return {"score": score, "direction": "SELL"}
            
        return {"score": 0, "direction": "NEUTRAL"}


    def _score_london_breakout(self, df: pd.DataFrame) -> dict:
        """
        Score l'activité durant l'ouverture de la session de Londres.
        """
        try:
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC')
            else:
                df.index = df.index.tz_convert('UTC')
        except TypeError:
            df.index = df.index.tz_convert('UTC')

        last_candle_time = df.index[-1].time()
        
        if time(8, 0) <= last_candle_time <= time(10, 0):
            asian_session_df = df.between_time('00:00', '07:59')
            
            if not asian_session_df.empty:
                asian_high = asian_session_df['high'].max()
                asian_low = asian_session_df['low'].min()
                
                if df['close'].iloc[-1] > asian_high:
                    return {"score": 80, "direction": "BUY"}
                if df['close'].iloc[-1] < asian_low:
                    return {"score": 80, "direction": "SELL"}
                    
        return {"score": 0, "direction": "NEUTRAL"}

    def _score_inbalance(self, df: pd.DataFrame) -> dict:
        """
        Détecte les inbalances (Fair Value Gaps) et score leur pertinence.
        Combine la détection graphique, la non-mitigation et le concept d'equilibrium.
        """
        if len(df) < 3:
            return {"score": 0, "direction": "NEUTRAL"}

        score = 0
        direction = "NEUTRAL"
        last_candle_idx = len(df) - 1

        # Calculer le swing high/low pour l'equilibrium (similaire au Fibonacci)
        # Pour le day trading sur 30min, un swing sur 20-30 bougies est pertinent
        swing_window = 30
        if len(df) < swing_window:
            return {"score": 0, "direction": "NEUTRAL"}
            
        recent_high = df['high'].iloc[-swing_window:].max()
        recent_low = df['low'].iloc[-swing_window:].min()
        equilibrium_mid = (recent_high + recent_low) / 2

        # Itérer sur les bougies récentes pour trouver une inbalance non-mitigée
        # On cherche à partir de la dernière bougie en remontant
        for i in range(last_candle_idx, 1, -1): # i est l'index de la 3ème bougie dans la séquence 1-2-3
            candle1 = df.iloc[i - 2]
            candle2 = df.iloc[i - 1]
            candle3 = df.iloc[i]

            # Détection d'inbalance haussière (Bullish FVG)
            if candle1['high'] < candle3['low'] and candle3.name == df.index[last_candle_idx]:
                # Zone de l'inbalance
                fvg_low = candle1['high']
                fvg_high = candle3['low']

                # Vérifier la non-mitigation: aucune bougie entre l'inbalance et la bougie actuelle ne doit avoir touché la FVG
                # Pour simplifier, on vérifie que le prix n'est pas déjà revenu dans la zone de l'inbalance.
                # Une vraie mitigation serait plus complexe (test des low/high dans la zone)
                is_mitigated = False
                if i < last_candle_idx: # Si ce n'est pas l'inbalance de la bougie actuelle
                    if (df['low'].iloc[i+1:last_candle_idx+1] <= fvg_high).any() and \
                       (df['high'].iloc[i+1:last_candle_idx+1] >= fvg_low).any():
                        is_mitigated = True
                
                # S'assurer que le prix actuel est au-dessus de l'inbalance ou en train de la re-tester
                if not is_mitigated and df['close'].iloc[last_candle_idx] > fvg_high:
                    # Vérifier l'equilibrium: l'inbalance doit être en discount pour un achat
                    if fvg_low < equilibrium_mid:
                        score = 90  # Score élevé pour une inbalance haussière non-mitigée en discount
                        direction = "BUY"
                        # Une fois trouvée, on prend la plus récente et la plus pertinente
                        return {"score": score, "direction": direction, "fvg_low": fvg_low, "fvg_high": fvg_high}

            # Détection d'inbalance baissière (Bearish FVG)
            if candle1['low'] > candle3['high'] and candle3.name == df.index[last_candle_idx]:
                # Zone de l'inbalance
                fvg_low = candle3['high']
                fvg_high = candle1['low']

                is_mitigated = False
                if i < last_candle_idx:
                    if (df['high'].iloc[i+1:last_candle_idx+1] >= fvg_low).any() and \
                       (df['low'].iloc[i+1:last_candle_idx+1] <= fvg_high).any():
                        is_mitigated = True

                if not is_mitigated and df['close'].iloc[last_candle_idx] < fvg_low:
                    # Vérifier l'equilibrium: l'inbalance doit être en premium pour une vente
                    if fvg_high > equilibrium_mid:
                        score = 90  # Score élevé pour une inbalance baissière non-mitigée en premium
                        direction = "SELL"
                        return {"score": score, "direction": direction, "fvg_low": fvg_low, "fvg_high": fvg_high}
        
        return {"score": 0, "direction": "NEUTRAL"}
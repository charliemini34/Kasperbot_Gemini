import pandas as pd
import numpy as np
from datetime import datetime, time

class StrategyScorer:
    """
    Calcule les scores pour diverses stratégies de trading basées sur des données OHLC.
    Version améliorée pour une meilleure fiabilité des signaux.
    """

    def calculate_all(self, ohlc_data: pd.DataFrame) -> dict:
        """Calcule et retourne les scores bruts pour toutes les stratégies implémentées."""
        if ohlc_data is None or len(ohlc_data) < 50:
            return {}
            
        # S'assurer que l'index est bien un DatetimeIndex pour les opérations temporelles
        if not isinstance(ohlc_data.index, pd.DatetimeIndex):
            ohlc_data['time'] = pd.to_datetime(ohlc_data['time'])
            ohlc_data.set_index('time', inplace=True)

        scores = {
            "TREND": self._score_trend(ohlc_data.copy()),
            "MEAN_REV": self._score_mean_reversion(ohlc_data.copy()),
            "SMC": self._score_smc(ohlc_data.copy()),
            "VOL_BRK": self._score_volatility_breakout(ohlc_data.copy()),
            "LONDON_BRK": self._score_london_breakout(ohlc_data.copy()),
        }
        return scores

    def _score_trend(self, df):
        """Score la force de la tendance basé sur le croisement des EMA et le MACD."""
        ema_fast = df['close'].ewm(span=12, adjust=False).mean()
        ema_slow = df['close'].ewm(span=26, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        
        score = 0
        direction = "NEUTRAL"

        # Condition d'achat : les indicateurs sont alignés à la hausse
        if ema_fast.iloc[-1] > ema_slow.iloc[-1] and macd_line.iloc[-1] > signal_line.iloc[-1]:
            direction = "BUY"
            # Le score est proportionnel à la distance entre les EMAs, normalisé
            distance = (ema_fast.iloc[-1] - ema_slow.iloc[-1]) / df['close'].iloc[-1]
            score = min(100, distance * 5000)
        # Condition de vente : les indicateurs sont alignés à la baisse
        elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and macd_line.iloc[-1] < signal_line.iloc[-1]:
            direction = "SELL"
            distance = (ema_slow.iloc[-1] - ema_fast.iloc[-1]) / df['close'].iloc[-1]
            score = min(100, distance * 5000)
        
        return {"score": score, "direction": direction}

    def _score_mean_reversion(self, df):
        """Score le potentiel de retour à la moyenne basé sur les Bandes de Bollinger et le RSI."""
        window = 20
        std_dev = df['close'].rolling(window).std()
        moving_average = df['close'].rolling(window).mean()
        upper_band = moving_average + (std_dev * 2)
        lower_band = moving_average - (std_dev * 2)
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        last_close = df['close'].iloc[-1]
        score = 0
        direction = "NEUTRAL"

        # Le prix est suracheté (au-dessus de la bande sup + RSI > 70)
        if last_close > upper_band.iloc[-1] and rsi.iloc[-1] > 70:
            direction = "SELL"
            score = rsi.iloc[-1]
        # Le prix est survendu (en dessous de la bande inf + RSI < 30)
        elif last_close < lower_band.iloc[-1] and rsi.iloc[-1] < 30:
            direction = "BUY"
            score = 100 - rsi.iloc[-1]
        
        return {"score": score, "direction": direction}

    def _score_smc(self, df):
        """Concepts Smart Money simplifiés : recherche une prise de liquidité suivie d'une cassure de structure (Break of Structure)."""
        if len(df) < 10: return {"score": 0, "direction": "NEUTRAL"}
        
        last_10_candles = df.iloc[-10:]
        
        # Recherche d'un signal haussier (Bullish)
        # 1. Trouver un "swing low" (un plus bas entouré de deux plus bas plus hauts)
        lows = last_10_candles['low']
        swing_low_idx = (lows < lows.shift(1)) & (lows < lows.shift(-1))
        if swing_low_idx.any():
            # 2. Après ce plus bas, y a-t-il eu une cassure d'un "swing high" précédent ?
            recent_high = last_10_candles['high'][swing_low_idx.idxmax():].max()
            if df['close'].iloc[-1] > recent_high:
                return {"score": 85, "direction": "BUY"}

        # Recherche d'un signal baissier (Bearish)
        # 1. Trouver un "swing high" (un plus haut entouré de deux plus hauts plus bas)
        highs = last_10_candles['high']
        swing_high_idx = (highs > highs.shift(1)) & (highs > highs.shift(-1))
        if swing_high_idx.any():
            # 2. Après ce plus haut, y a-t-il eu une cassure d'un "swing low" précédent ?
            recent_low = last_10_candles['low'][swing_high_idx.idxmax():].min()
            if df['close'].iloc[-1] < recent_low:
                return {"score": 85, "direction": "SELL"}
                
        return {"score": 0, "direction": "NEUTRAL"}


    def _score_volatility_breakout(self, df):
        """Score une cassure d'un range de prix récent (canal de Donchian)."""
        window = 20
        recent_high = df['high'].iloc[-window:-1].max()
        recent_low = df['low'].iloc[-window:-1].min()
        
        range_size = (recent_high - recent_low)
        if range_size == 0: return {"score": 0, "direction": "NEUTRAL"}

        # Cassure haussière
        if df['close'].iloc[-1] > recent_high:
            # Le score est plus élevé si la cassure est forte
            breakout_strength = (df['close'].iloc[-1] - recent_high) / range_size
            score = min(100, 70 + breakout_strength * 50)
            return {"score": score, "direction": "BUY"}
            
        # Cassure baissière
        if df['close'].iloc[-1] < recent_low:
            breakout_strength = (recent_low - df['close'].iloc[-1]) / range_size
            score = min(100, 70 + breakout_strength * 50)
            return {"score": score, "direction": "SELL"}
            
        return {"score": 0, "direction": "NEUTRAL"}


    def _score_london_breakout(self, df):
        """Score l'activité durant l'ouverture de la session de Londres en se basant sur l'UTC."""
        last_candle_time_utc = df.index[-1].tz_localize(None) # S'assurer que le temps est naïf (sans fuseau)
        
        # Session de Londres : ~8h à 17h UTC. On cible l'ouverture, de 8h à 10h UTC.
        if time(8, 0) <= last_candle_time_utc.time() <= time(10, 0):
            # Session asiatique : ~0h à 8h UTC
            asian_session_df = df.between_time('00:00', '07:59')
            
            if not asian_session_df.empty:
                asian_high = asian_session_df['high'].max()
                asian_low = asian_session_df['low'].min()
                
                # Cassure du plus haut de la session asiatique
                if df['high'].iloc[-1] > asian_high:
                    return {"score": 80, "direction": "BUY"}
                # Cassure du plus bas de la session asiatique
                if df['low'].iloc[-1] < asian_low:
                    return {"score": 80, "direction": "SELL"}
                    
        return {"score": 0, "direction": "NEUTRAL"}
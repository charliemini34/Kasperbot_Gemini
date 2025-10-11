import pandas as pd
import numpy as np
from datetime import datetime

class StrategyScorer:
    """Calculates scores for various trading strategies based on OHLC data."""
    
    def calculate_all(self, ohlc_data: pd.DataFrame) -> dict:
        """Calculates and returns the raw scores for all implemented strategies."""
        if ohlc_data is None or len(ohlc_data) < 50: # Need enough data
            return {}
            
        scores = {
            "TREND": self._score_trend(ohlc_data.copy()),
            "MEAN_REV": self._score_mean_reversion(ohlc_data.copy()),
            "SMC": self._score_smc(ohlc_data.copy()),
            "VOL_BRK": self._score_volatility_breakout(ohlc_data.copy()),
            "LONDON_BRK": self._score_london_breakout(ohlc_data.copy()),
        }
        return scores

    def _score_trend(self, df):
        """Scores trend strength based on EMA crossover and MACD."""
        ema_fast = df['close'].ewm(span=12, adjust=False).mean()
        ema_slow = df['close'].ewm(span=26, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        
        score = 0
        direction = "NEUTRAL"

        if ema_fast.iloc[-1] > ema_slow.iloc[-1] and macd_line.iloc[-1] > signal_line.iloc[-1]:
            direction = "BUY"
            # Score based on distance between EMAs, normalized
            score = min(100, abs(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / df['close'].iloc[-1] * 5000)
        elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and macd_line.iloc[-1] < signal_line.iloc[-1]:
            direction = "SELL"
            score = min(100, abs(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / df['close'].iloc[-1] * 5000)
        
        return {"score": score, "direction": direction}

    def _score_mean_reversion(self, df):
        """Scores mean reversion potential based on Bollinger Bands and RSI."""
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

        if last_close > upper_band.iloc[-1] and rsi.iloc[-1] > 70:
            direction = "SELL"
            score = rsi.iloc[-1] # Score is how overbought it is
        elif last_close < lower_band.iloc[-1] and rsi.iloc[-1] < 30:
            direction = "BUY"
            score = 100 - rsi.iloc[-1] # Score is how oversold it is
        
        return {"score": score, "direction": direction}

    def _score_smc(self, df):
        """Simplified Smart Money Concepts: looks for a 'liquidity sweep'."""
        last_3 = df.iloc[-3:]
        # Bullish sweep: candle -2 takes the low of candle -3, and candle -1 closes above -2's high
        if len(last_3) == 3 and last_3['low'].iloc[1] < last_3['low'].iloc[0] and last_3['close'].iloc[2] > last_3['high'].iloc[1]:
            return {"score": 85, "direction": "BUY"}
        # Bearish sweep: candle -2 takes the high of candle -3, and candle -1 closes below -2's low
        if len(last_3) == 3 and last_3['high'].iloc[1] > last_3['high'].iloc[0] and last_3['close'].iloc[2] < last_3['low'].iloc[1]:
            return {"score": 85, "direction": "SELL"}
        return {"score": 5, "direction": "NEUTRAL"}

    def _score_volatility_breakout(self, df):
        """Scores a breakout from a recent price range."""
        window = 20
        recent_high = df['high'].iloc[-window:-1].max()
        recent_low = df['low'].iloc[-window:-1].min()
        
        if df['close'].iloc[-1] > recent_high:
            return {"score": 75, "direction": "BUY"}
        if df['close'].iloc[-1] < recent_low:
            return {"score": 75, "direction": "SELL"}
        return {"score": 5, "direction": "NEUTRAL"}

    def _score_london_breakout(self, df):
        """Scores activity during the London session open."""
        last_candle_time = df['time'].iloc[-1]
        # Assuming server time is EET (GMT+2/3), London open (8am GMT) is 10-11am server time
        if 10 <= last_candle_time.hour <= 12:
            df['hour'] = df['time'].dt.hour
            asian_session_df = df[df['hour'].between(2, 8)]
            if not asian_session_df.empty:
                asian_high = asian_session_df['high'].max()
                asian_low = asian_session_df['low'].min()
                if df['high'].iloc[-1] > asian_high:
                    return {"score": 80, "direction": "BUY"}
                if df['low'].iloc[-1] < asian_low:
                    return {"score": 80, "direction": "SELL"}
        return {"score": 0, "direction": "NEUTRAL"}
# Fichier: src/patterns/pattern_detector.py
# Version: 14.0.1 (Guardian+ Hotfix)
# Dépendances: pandas, numpy, logging
# Description: Correction de la syntaxe dans la détection des points de swing.

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
    Module de reconnaissance de patterns SMC & ICT, avec des définitions plus strictes.
    v14.0.1 : Correction de la syntaxe dans la fonction _find_swing_points.
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
        self.detected_patterns_info = {}
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
                    signal = func(df)
                    if signal:
                        all_signals.append(signal)
                except Exception as e:
                    self.log.error(f"Erreur lors de la détection du pattern '{name}': {e}", exc_info=True)

        confirmed_trade_signal = None
        for signal in all_signals:
            name = signal['pattern']
            direction = signal['direction']
            # Premier signal valide qui correspond à la tendance
            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction})"}
                if not confirmed_trade_signal:
                    confirmed_trade_signal = signal
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
        
        return confirmed_trade_signal

    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        """
        CORRIGÉ: Trouve les points de swing (hauts et bas) sur un DataFrame.
        Un swing high est une bougie avec n "highs" plus bas des deux côtés.
        Un swing low est une bougie avec n "lows" plus hauts des deux côtés.
        """
        highs_condition = pd.Series(True, index=df.index)
        lows_condition = pd.Series(True, index=df.index)

        for i in range(1, n + 1):
            highs_condition &= (df['high'].shift(i) < df['high']) & (df['high'].shift(-i) < df['high'])
            lows_condition &= (df['low'].shift(i) > df['low']) & (df['low'].shift(-i) > df['low'])

        # Remplir les valeurs NaN générées par shift() avec False pour éviter les erreurs
        highs_condition = highs_condition.fillna(False)
        lows_condition = lows_condition.fillna(False)

        swing_highs = df[highs_condition]
        swing_lows = df[lows_condition]
        
        return swing_highs, swing_lows

    def _detect_choch(self, df: pd.DataFrame):
        """Détection de Changement de Caractère (CHoCH) révisée."""
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        
        recent_data = df.iloc[-20:]
        swing_highs, swing_lows = self._find_swing_points(recent_data, n=3)
        
        # Bullish CHOCH: La dernière bougie casse le dernier swing high significatif.
        if not swing_highs.empty:
            last_swing_high_price = swing_highs.iloc[-1]['high']
            if df['close'].iloc[-1] > last_swing_high_price:
                self.log.debug(f"CHoCH haussier détecté: Clôture ({df['close'].iloc[-1]}) > dernier swing high ({last_swing_high_price})")
                return {'pattern': PATTERN_CHOCH, 'direction': BUY}

        # Bearish CHOCH: La dernière bougie casse le dernier swing low significatif.
        if not swing_lows.empty:
            last_swing_low_price = swing_lows.iloc[-1]['low']
            if df['close'].iloc[-1] < last_swing_low_price:
                self.log.debug(f"CHoCH baissier détecté: Clôture ({df['close'].iloc[-1]}) < dernier swing low ({last_swing_low_price})")
                return {'pattern': PATTERN_CHOCH, 'direction': SELL}

        return None

    def _detect_order_block(self, df: pd.DataFrame):
        """Détection d'Order Block (OB) révisée avec validation par rupture de structure."""
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None
        
        # Bullish OB: cherche la dernière bougie baissière avant une forte montée qui casse un swing high.
        swing_highs, _ = self._find_swing_points(df.iloc[-50:], n=5)
        if not swing_highs.empty:
            last_bos_price = swing_highs.iloc[-1]['high']
            # On cherche une bougie baissière avant cette rupture
            down_candles_before_bos = df[(df.index < swing_highs.index[-1]) & (df['close'] < df['open'])]
            if not down_candles_before_bos.empty:
                bullish_ob = down_candles_before_bos.iloc[-1]
                # Si le prix actuel est revenu dans la zone de l'OB
                if df['close'].iloc[-1] <= bullish_ob['high'] and df['close'].iloc[-1] >= bullish_ob['low']:
                    self.log.debug(f"Order Block haussier détecté près de {bullish_ob.name}")
                    return {'pattern': PATTERN_ORDER_BLOCK, 'direction': BUY}

        # Bearish OB: cherche la dernière bougie haussière avant une forte baisse qui casse un swing low.
        _, swing_lows = self._find_swing_points(df.iloc[-50:], n=5)
        if not swing_lows.empty:
            last_bos_price = swing_lows.iloc[-1]['low']
            # On cherche une bougie haussière avant cette rupture
            up_candles_before_bos = df[(df.index < swing_lows.index[-1]) & (df['close'] > df['open'])]
            if not up_candles_before_bos.empty:
                bearish_ob = up_candles_before_bos.iloc[-1]
                # Si le prix actuel est revenu dans la zone de l'OB
                if df['close'].iloc[-1] >= bearish_ob['low'] and df['close'].iloc[-1] <= bearish_ob['high']:
                    self.log.debug(f"Order Block baissier détecté près de {bearish_ob.name}")
                    return {'pattern': PATTERN_ORDER_BLOCK, 'direction': SELL}
                    
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        """Détecte les Fair Value Gaps (FVG) / Inbalances."""
        self.detected_patterns_info[PATTERN_INBALANCE] = {'status': 'Pas de signal'}
        if len(df) < 5: return None
        
        # Bullish FVG: Le bas de la bougie N-2 est plus haut que le haut de la bougie N-4.
        if df['low'].iloc[-2] > df['high'].iloc[-4]:
            self.log.debug("Inbalance (FVG) haussière détectée.")
            return {'pattern': PATTERN_INBALANCE, 'direction': BUY}

        # Bearish FVG: Le haut de la bougie N-2 est plus bas que le bas de la bougie N-4.
        if df['high'].iloc[-2] < df['low'].iloc[-4]:
            self.log.debug("Inbalance (FVG) baissière détectée.")
            return {'pattern': PATTERN_INBALANCE, 'direction': SELL}
            
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        """Détecte une prise de liquidité sous un ancien bas ou au-dessus d'un ancien haut."""
        self.detected_patterns_info[PATTERN_LIQUIDITY_GRAB] = {'status': 'Pas de signal'}
        if len(df) < 20: return None
        
        _, swing_lows = self._find_swing_points(df.iloc[-20:-1], n=3)
        if not swing_lows.empty:
            last_low = swing_lows.iloc[-1]['low']
            # Si la dernière bougie a méché sous le dernier swing low puis a clôturé au-dessus
            if df['low'].iloc[-1] < last_low and df['close'].iloc[-1] > last_low:
                 self.log.debug(f"Prise de liquidité haussière sous {last_low:.5f}")
                 return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': BUY}

        swing_highs, _ = self._find_swing_points(df.iloc[-20:-1], n=3)
        if not swing_highs.empty:
            last_high = swing_highs.iloc[-1]['high']
            # Si la dernière bougie a méché au-dessus du dernier swing high puis a clôturé en dessous
            if df['high'].iloc[-1] > last_high and df['close'].iloc[-1] < last_high:
                self.log.debug(f"Prise de liquidité baissière au-dessus de {last_high:.5f}")
                return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': SELL}
                
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        """Détecte un pattern d'Accumulation, Manipulation, Distribution (AMD)."""
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
        
        # Manipulation: le prix prend la liquidité au-dessus ou en dessous du range asiatique
        recent_market_data = df.loc[df.index.date == today_utc].between_time(asia_end, current_time_utc)
        if recent_market_data.empty: return None

        # Manipulation baissière (prise de liquidité sous le bas asiatique) -> cherche signal d'achat
        if recent_market_data['low'].min() < asia_low:
            choch_signal = self._detect_choch(recent_market_data)
            if choch_signal and choch_signal['direction'] == BUY:
                self.log.debug(f"Pattern AMD haussier détecté après manipulation sous le range asiatique.")
                return {'pattern': PATTERN_AMD, 'direction': BUY}

        # Manipulation haussière (prise de liquidité au-dessus du haut asiatique) -> cherche signal de vente
        if recent_market_data['high'].max() > asia_high:
            choch_signal = self._detect_choch(recent_market_data)
            if choch_signal and choch_signal['direction'] == SELL:
                self.log.debug(f"Pattern AMD baissier détecté après manipulation au-dessus du range asiatique.")
                return {'pattern': PATTERN_AMD, 'direction': SELL}
                    
        return None
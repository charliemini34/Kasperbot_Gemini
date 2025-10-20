# Fichier: src/patterns/pattern_detector.py
# Version: 18.0.1 (Typing-Import-Fix)
# Dépendances: pandas, numpy, logging, datetime, typing, src.constants
# Description: Ajout de l'importation manquante pour Tuple et Optional.

import pandas as pd
import numpy as np
import logging
from datetime import time
# --- MODIFICATION : Ajout de l'importation ---
from typing import Tuple, Optional
# --- FIN MODIFICATION ---
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_CHOCH, PATTERN_INBALANCE, PATTERN_LIQUIDITY_GRAB,
    PATTERN_AMD, BUY, SELL
)

# Constantes pour Premium/Discount
PREMIUM = "Premium"
DISCOUNT = "Discount"
EQUILIBRIUM = "Equilibrium"

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC & ICT avancés.
    v18.0.1: Correction de l'importation manquante pour Tuple/Optional.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    def _get_trend_filter_direction(self, connector, symbol: str) -> str:
        # ... (inchangé) ...
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
        # ... (inchangé) ...
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
            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction})"}
                if not confirmed_trade_signal:
                    confirmed_trade_signal = signal
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
        
        return confirmed_trade_signal

    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        # ... (inchangé) ...
        df_copy = df.copy()
        df_copy.loc[:, 'is_swing_high'] = df_copy['high'].rolling(window=2*n+1, center=True, min_periods=1).max() == df_copy['high']
        df_copy.loc[:, 'is_swing_low'] = df_copy['low'].rolling(window=2*n+1, center=True, min_periods=1).min() == df_copy['low']
        
        swing_highs = df_copy[df_copy['is_swing_high']]
        swing_lows = df_copy[df_copy['is_swing_low']]
        
        return swing_highs, swing_lows

    def _get_premium_discount_zones(self, df: pd.DataFrame, n_major: int = 10) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        # ... (inchangé) ...
        if len(df) < (2 * n_major + 1) * 2: 
             return None, None, None
             
        swing_highs_major, swing_lows_major = self._find_swing_points(df, n=n_major)
        
        if swing_highs_major.empty or swing_lows_major.empty:
            return None, None, None
            
        relevant_high = swing_highs_major['high'].tail(5).max()
        relevant_low = swing_lows_major['low'].tail(5).min()
        
        if pd.isna(relevant_high) or pd.isna(relevant_low) or relevant_high == relevant_low:
            return None, None, None
            
        equilibrium = relevant_low + (relevant_high - relevant_low) / 2
        return relevant_high, relevant_low, equilibrium

    def _check_inducement_taken(self, df: pd.DataFrame, poi_index, direction: str, n_minor: int = 2) -> bool:
        # ... (inchangé) ...
        if poi_index <= n_minor * 2: 
             return False 
             
        data_before_poi = df.iloc[:poi_index]
        swing_highs_minor, swing_lows_minor = self._find_swing_points(data_before_poi, n=n_minor)

        if direction == BUY: 
            if swing_lows_minor.empty: return False
            inducement_level = swing_lows_minor['low'].iloc[-1]
            try: # Utiliser try-except au cas où l'index n'est pas trouvé (rare)
                inducement_index = df.index.get_loc(swing_lows_minor.index[-1])
            except KeyError:
                return False
            price_path_after_idm = df.iloc[inducement_index + 1 : poi_index]
            if not price_path_after_idm.empty and (price_path_after_idm['low'] < inducement_level).any():
                return True
        elif direction == SELL: 
             if swing_highs_minor.empty: return False
             inducement_level = swing_highs_minor['high'].iloc[-1]
             try:
                inducement_index = df.index.get_loc(swing_highs_minor.index[-1])
             except KeyError:
                 return False
             price_path_after_idm = df.iloc[inducement_index + 1 : poi_index]
             if not price_path_after_idm.empty and (price_path_after_idm['high'] > inducement_level).any():
                return True

        return False

    def _detect_choch(self, df: pd.DataFrame):
        # ... (inchangé) ...
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'}
        if len(df) < 50: return None 

        swing_highs, swing_lows = self._find_swing_points(df, n=3) 
        relevant_high, relevant_low, equilibrium = self._get_premium_discount_zones(df)
        
        if equilibrium is None: 
            self.log.debug("CHoCH: Range Premium/Discount non identifiable.")
            return None 

        current_price = df['close'].iloc[-1]
        
        if len(swing_lows) > 1 and len(swing_highs) > 0:
            if swing_lows['low'].iloc[-1] < swing_lows['low'].iloc[-2]: 
                last_lower_high = swing_highs[swing_highs.index < swing_lows.index[-1]].tail(1)
                if not last_lower_high.empty and current_price > last_lower_high['high'].values[0]:
                    if current_price < equilibrium:
                        target_liquidity = swing_highs.iloc[-1]['high'] 
                        self.log.debug(f"CHoCH haussier validé (en Discount). Cible: {target_liquidity}")
                        return {'pattern': PATTERN_CHOCH, 'direction': BUY, 'target_price': target_liquidity}
                    else:
                        self.log.debug("CHoCH haussier détecté mais invalidé (en Premium).")

        if len(swing_highs) > 1 and len(swing_lows) > 0:
            if swing_highs['high'].iloc[-1] > swing_highs['high'].iloc[-2]: 
                last_higher_low = swing_lows[swing_lows.index < swing_highs.index[-1]].tail(1)
                if not last_higher_low.empty and current_price < last_higher_low['low'].values[0]:
                    if current_price > equilibrium:
                        target_liquidity = swing_lows.iloc[-1]['low'] 
                        self.log.debug(f"CHoCH baissier validé (en Premium). Cible: {target_liquidity}")
                        return {'pattern': PATTERN_CHOCH, 'direction': SELL, 'target_price': target_liquidity}
                    else:
                        self.log.debug("CHoCH baissier détecté mais invalidé (en Discount).")

        return None
        
    def _detect_order_block(self, df: pd.DataFrame):
        # ... (inchangé) ...
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Pas de signal'}
        if len(df) < 50: return None
        
        relevant_high, relevant_low, equilibrium = self._get_premium_discount_zones(df)
        if equilibrium is None: 
            self.log.debug("OB: Range Premium/Discount non identifiable.")
            return None
        
        swing_highs, swing_lows = self._find_swing_points(df, n=5)
        current_price = df['close'].iloc[-1]
        
        if len(swing_highs) >= 2:
            last_high_before_bos = swing_highs.iloc[-2]
            if df['high'].iloc[-1] > last_high_before_bos['high']:
                try:
                    bos_candle_index = df.index.get_loc(last_high_before_bos.name)
                    down_candles = df.iloc[:bos_candle_index][(df.iloc[:bos_candle_index]['close'] < df.iloc[:bos_candle_index]['open'])]
                    if not down_candles.empty:
                        bullish_ob_candle = down_candles.iloc[-1]
                        ob_index = df.index.get_loc(bullish_ob_candle.name)
                        ob_low, ob_high = bullish_ob_candle['low'], bullish_ob_candle['high']
                        
                        is_in_discount = ob_low < equilibrium 
                        inducement_taken = self._check_inducement_taken(df, ob_index, BUY)
                        price_returned_to_ob = (current_price <= ob_high) and (current_price >= ob_low)

                        if is_in_discount and inducement_taken and price_returned_to_ob:
                            target_liquidity = swing_highs.iloc[-1]['high'] 
                            self.log.debug(f"Order Block haussier validé (Discount, IDM pris). Cible: {target_liquidity}")
                            return {'pattern': PATTERN_ORDER_BLOCK, 'direction': BUY, 'target_price': target_liquidity}
                        else:
                             self.log.debug(f"OB Haussier détecté mais invalidé (Discount={is_in_discount}, IDM={inducement_taken}, Retourné={price_returned_to_ob})")
                except Exception as e:
                    self.log.error(f"Erreur interne détection OB haussier: {e}")

        if len(swing_lows) >= 2:
            last_low_before_bos = swing_lows.iloc[-2]
            if df['low'].iloc[-1] < last_low_before_bos['low']:
                try:
                    bos_candle_index = df.index.get_loc(last_low_before_bos.name)
                    up_candles = df.iloc[:bos_candle_index][(df.iloc[:bos_candle_index]['close'] > df.iloc[:bos_candle_index]['open'])]
                    if not up_candles.empty:
                        bearish_ob_candle = up_candles.iloc[-1]
                        ob_index = df.index.get_loc(bearish_ob_candle.name)
                        ob_low, ob_high = bearish_ob_candle['low'], bearish_ob_candle['high']

                        is_in_premium = ob_high > equilibrium 
                        inducement_taken = self._check_inducement_taken(df, ob_index, SELL)
                        price_returned_to_ob = (current_price >= ob_low) and (current_price <= ob_high)

                        if is_in_premium and inducement_taken and price_returned_to_ob:
                            target_liquidity = swing_lows.iloc[-1]['low'] 
                            self.log.debug(f"Order Block baissier validé (Premium, IDM pris). Cible: {target_liquidity}")
                            return {'pattern': PATTERN_ORDER_BLOCK, 'direction': SELL, 'target_price': target_liquidity}
                        else:
                             self.log.debug(f"OB Baissier détecté mais invalidé (Premium={is_in_premium}, IDM={inducement_taken}, Retourné={price_returned_to_ob})")
                except Exception as e:
                    self.log.error(f"Erreur interne détection OB baissier: {e}")
                    
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
         # ... (inchangé) ...
        self.detected_patterns_info[PATTERN_INBALANCE] = {'status': 'Pas de signal'}
        if len(df) < 50: return None 
        
        relevant_high, relevant_low, equilibrium = self._get_premium_discount_zones(df)
        if equilibrium is None: 
            self.log.debug("FVG: Range Premium/Discount non identifiable.")
            return None

        current_price = df['close'].iloc[-1]
        swing_highs, swing_lows = self._find_swing_points(df, n=3) 

        fvg_high_bull = df['low'].iloc[-2]
        fvg_low_bull = df['high'].iloc[-4]
        if len(df) > 4 and fvg_high_bull > fvg_low_bull:
             fvg_candle_index = len(df) - 3 
             is_in_discount = fvg_high_bull < equilibrium 
             inducement_taken = self._check_inducement_taken(df, fvg_candle_index, BUY)
             price_returned_to_fvg = (current_price <= fvg_high_bull) and (current_price >= fvg_low_bull)

             if is_in_discount and inducement_taken and price_returned_to_fvg:
                target_liquidity = swing_highs.tail(1)['high'].values[0] if not swing_highs.empty else None
                if target_liquidity:
                    self.log.debug(f"FVG haussier validé (Discount, IDM pris). Cible: {target_liquidity}")
                    return {'pattern': PATTERN_INBALANCE, 'direction': BUY, 'target_price': target_liquidity}
             else:
                 self.log.debug(f"FVG Haussier détecté mais invalidé (Discount={is_in_discount}, IDM={inducement_taken}, Retourné={price_returned_to_fvg})")

        fvg_low_bear = df['high'].iloc[-2]
        fvg_high_bear = df['low'].iloc[-4]
        if len(df) > 4 and fvg_low_bear < fvg_high_bear:
            fvg_candle_index = len(df) - 3 
            is_in_premium = fvg_low_bear > equilibrium 
            inducement_taken = self._check_inducement_taken(df, fvg_candle_index, SELL)
            price_returned_to_fvg = (current_price >= fvg_high_bear) and (current_price <= fvg_low_bear)

            if is_in_premium and inducement_taken and price_returned_to_fvg:
                target_liquidity = swing_lows.tail(1)['low'].values[0] if not swing_lows.empty else None
                if target_liquidity:
                    self.log.debug(f"FVG baissier validé (Premium, IDM pris). Cible: {target_liquidity}")
                    return {'pattern': PATTERN_INBALANCE, 'direction': SELL, 'target_price': target_liquidity}
            else:
                 self.log.debug(f"FVG Baissier détecté mais invalidé (Premium={is_in_premium}, IDM={inducement_taken}, Retourné={price_returned_to_fvg})")
            
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        # ... (inchangé) ...
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
        # ... (inchangé) ...
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
            choch_signal = self._detect_choch(df) 
            if choch_signal and choch_signal['direction'] == BUY:
                self.log.debug(f"Pattern AMD haussier détecté après manipulation sous range asiatique.")
                return {'pattern': PATTERN_AMD, 'direction': BUY, 'target_price': asia_high}

        if recent_market_data['high'].max() > asia_high:
            choch_signal = self._detect_choch(df) 
            if choch_signal and choch_signal['direction'] == SELL:
                self.log.debug(f"Pattern AMD baissier détecté après manipulation au-dessus range asiatique.")
                return {'pattern': PATTERN_AMD, 'direction': SELL, 'target_price': asia_low}
                    
        return None
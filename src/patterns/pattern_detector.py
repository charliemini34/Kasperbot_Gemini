# Fichier: src/patterns/pattern_detector.py
# Version: 19.0.0 (R7)
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
    Module de reconnaissance de patterns SMC & ICT.
    v19.0.0 (R7): Retourne la zone d'entrée (FVG/OB) et le niveau SL structurel.
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
        df = ohlc_data.copy()
        
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(connector, symbol)
        
        detection_functions = {
            PATTERN_INBALANCE: self._detect_inbalance, # FVG prioritaire pour R7
            PATTERN_ORDER_BLOCK: self._detect_order_block,
            PATTERN_CHOCH: self._detect_choch, # Peut générer OB/FVG
            PATTERN_LIQUIDITY_GRAB: self._detect_liquidity_grab, # Peut générer OB/FVG
            PATTERN_AMD: self._detect_amd_session, # Peut générer OB/FVG
        }

        all_signals = []
        # (R7) Nettoyer les infos à chaque cycle
        self.detected_patterns_info.clear() 
        self.detected_patterns_info['TREND_FILTER'] = {'status': 'Non défini'} # Sera écrasé

        for name, func in detection_functions.items():
            # Initialiser le statut
            self.detected_patterns_info[name] = {'status': 'Pas de signal'}
            if self.config.get('pattern_detection', {}).get(name, False):
                try:
                    signal = func(df.copy())
                    if signal:
                        all_signals.append(signal)
                        # Mettre à jour le statut immédiatement si signal trouvé
                        self.detected_patterns_info[name] = {'status': f"DÉTECTÉ ({signal['direction']})"}
                except Exception as e:
                    self.log.error(f"Erreur lors de la détection du pattern '{name}': {e}", exc_info=True)
                    self.detected_patterns_info[name] = {'status': 'Erreur Détection'}

        confirmed_trade_signal = None
        # Donner la priorité aux signaux avec zone (FVG/OB) pour R7
        priority_signals = [s for s in all_signals if s['pattern'] in [PATTERN_INBALANCE, PATTERN_ORDER_BLOCK]]
        other_signals = [s for s in all_signals if s['pattern'] not in [PATTERN_INBALANCE, PATTERN_ORDER_BLOCK]]

        # Vérifier d'abord FVG/OB
        for signal in priority_signals:
            name = signal['pattern']
            direction = signal['direction']
            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction})"}
                confirmed_trade_signal = signal
                break # Prendre le premier FVG/OB valide
            else:
                self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
        
        # Si aucun FVG/OB valide, vérifier les autres
        if not confirmed_trade_signal:
            for signal in other_signals:
                name = signal['pattern']
                direction = signal['direction']
                if allowed_direction == "ANY" or direction == allowed_direction:
                    # Ces signaux sont des confirmations, mais n'ont pas de zone d'entrée R7
                    # On les logue mais on ne les trade pas directement en mode R7 pour l'instant
                    self.log.debug(f"Signal {name} ({direction}) détecté mais ignoré car pas de zone FVG/OB pour entrée R7.")
                    self.detected_patterns_info[name] = {'status': f"CONFIRMÉ ({direction}) - Ignoré (Pas de zone R7)"}
                    # Ne pas assigner à confirmed_trade_signal
                else:
                    self.detected_patterns_info[name] = {'status': f"INVALIDÉ ({direction} vs Tendance {allowed_direction})"}

        return confirmed_trade_signal


    def _find_swing_points(self, df: pd.DataFrame, n: int = 2):
        # (R7) S'assurer que les colonnes sont créées même si vides
        df['is_swing_high'] = False
        df['is_swing_low'] = False
        
        # Calculer les points swing
        is_sh = df['high'].rolling(window=2*n+1, center=True, min_periods=n+1).max() == df['high']
        is_sl = df['low'].rolling(window=2*n+1, center=True, min_periods=n+1).min() == df['low']
        
        # Appliquer les résultats
        df.loc[is_sh, 'is_swing_high'] = True
        df.loc[is_sl, 'is_swing_low'] = True

        swing_highs = df[df['is_swing_high']]
        swing_lows = df[df['is_swing_low']]
        
        return swing_highs, swing_lows

    # (R7) Fonctions modifiées pour retourner la zone et le SL structurel
    # Note: CHOCH et Liquidity Grab ne retournent pas de zone eux-mêmes,
    # mais peuvent être utilisés pour *confirmer* une zone FVG/OB créée par le mouvement.

    def _detect_choch(self, df: pd.DataFrame):
        # CHOCH est une confirmation, pas une zone d'entrée directe pour R7
        # On peut le garder pour info dans detected_patterns_info
        if len(df) < 20: return None

        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=3)

        recent_lows = swing_lows['low'].tail(3)
        recent_highs = swing_highs['high'].tail(3)
        
        # Bullish CHOCH
        if len(recent_lows) > 1 and len(swing_highs) > 0:
            # Vérifie si le dernier low est plus bas que le précédent (tendance baissière locale)
            if recent_lows.iloc[-1] < recent_lows.iloc[-2]:
                # Trouver le dernier Lower High *avant* le dernier Lower Low
                last_lower_high = swing_highs[swing_highs.index < recent_lows.index[-1]].tail(1)
                # Si on casse ce LH
                if not last_lower_high.empty and df['close'].iloc[-1] > last_lower_high['high'].iloc[0]:
                    target_liquidity = swing_highs.iloc[-1]['high'] # Cible = dernier SH
                    self.log.debug(f"CHoCH haussier confirmé. Cible de liquidité: {target_liquidity}")
                    # Ne retourne PAS de zone d'entrée R7
                    return {'pattern': PATTERN_CHOCH, 'direction': BUY, 'target_price': target_liquidity} 

        # Bearish CHOCH
        if len(recent_highs) > 1 and len(swing_lows) > 0:
             # Vérifie si le dernier high est plus haut que le précédent (tendance haussière locale)
            if recent_highs.iloc[-1] > recent_highs.iloc[-2]:
                # Trouver le dernier Higher Low *avant* le dernier Higher High
                last_higher_low = swing_lows[swing_lows.index < recent_highs.index[-1]].tail(1)
                # Si on casse ce HL
                if not last_higher_low.empty and df['close'].iloc[-1] < last_higher_low['low'].iloc[0]:
                    target_liquidity = swing_lows.iloc[-1]['low'] # Cible = dernier SL
                    self.log.debug(f"CHoCH baissier confirmé. Cible de liquidité: {target_liquidity}")
                    # Ne retourne PAS de zone d'entrée R7
                    return {'pattern': PATTERN_CHOCH, 'direction': SELL, 'target_price': target_liquidity}

        return None
        
    def _detect_order_block(self, df: pd.DataFrame):
        # (R7) Retourne la zone de l'OB et le SL structurel
        if len(df) < 50: return None
        
        swing_highs, swing_lows = self._find_swing_points(df.iloc[-50:].copy(), n=5)
        
        # Bullish OB Check (après cassure d'un ancien High)
        if len(swing_highs) >= 2:
            # Prendre le Swing High *précédent* le plus récent
            previous_high = swing_highs.iloc[-2] 
            # Si la bougie actuelle casse ce précédent high (BOS)
            if df['high'].iloc[-1] > previous_high['high']:
                # Chercher la dernière bougie baissière *avant* le début de l'impulsion
                # L'impulsion a commencé après le dernier SL avant le BOS
                last_swing_low_before_bos = swing_lows[swing_lows.index < previous_high.name].tail(1)
                if not last_swing_low_before_bos.empty:
                    search_start_index = df.index.get_loc(last_swing_low_before_bos.index[0])
                    relevant_candles = df.iloc[search_start_index:] 
                    # Dernière bougie baissière dans cette section
                    down_candles = relevant_candles[relevant_candles['close'] < relevant_candles['open']]
                    if not down_candles.empty:
                        bullish_ob_candle = down_candles.iloc[-1]
                        ob_top = bullish_ob_candle['high']
                        ob_bottom = bullish_ob_candle['low']
                        # SL structurel: juste sous le bas de l'OB
                        sl_level = ob_bottom 
                        target_liquidity = swing_highs.iloc[-1]['high'] # Cible = dernier SH formé
                        self.log.debug(f"Order Block haussier potentiel détecté. Zone [{ob_bottom:.5f} - {ob_top:.5f}], SL={sl_level:.5f}, Cible={target_liquidity:.5f}")
                        return {
                            'pattern': PATTERN_ORDER_BLOCK, 'direction': BUY,
                            'entry_zone_start': ob_bottom, 'entry_zone_end': ob_top,
                            'stop_loss_level': sl_level, 'target_price': target_liquidity
                        }

        # Bearish OB Check (après cassure d'un ancien Low)
        if len(swing_lows) >= 2:
            previous_low = swing_lows.iloc[-2]
            if df['low'].iloc[-1] < previous_low['low']: # BOS
                last_swing_high_before_bos = swing_highs[swing_highs.index < previous_low.name].tail(1)
                if not last_swing_high_before_bos.empty:
                    search_start_index = df.index.get_loc(last_swing_high_before_bos.index[0])
                    relevant_candles = df.iloc[search_start_index:]
                    up_candles = relevant_candles[relevant_candles['close'] > relevant_candles['open']]
                    if not up_candles.empty:
                        bearish_ob_candle = up_candles.iloc[-1]
                        ob_top = bearish_ob_candle['high']
                        ob_bottom = bearish_ob_candle['low']
                        sl_level = ob_top # SL au dessus de l'OB
                        target_liquidity = swing_lows.iloc[-1]['low'] # Cible = dernier SL formé
                        self.log.debug(f"Order Block baissier potentiel détecté. Zone [{ob_bottom:.5f} - {ob_top:.5f}], SL={sl_level:.5f}, Cible={target_liquidity:.5f}")
                        return {
                            'pattern': PATTERN_ORDER_BLOCK, 'direction': SELL,
                            'entry_zone_start': ob_bottom, 'entry_zone_end': ob_top,
                            'stop_loss_level': sl_level, 'target_price': target_liquidity
                        }
                    
        return None

    def _detect_inbalance(self, df: pd.DataFrame):
        # (R7) Retourne la zone FVG et le SL structurel
        if len(df) < 5: return None # Besoin d'au moins 3 bougies + 2 pour contexte
        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=3) # Utiliser n=3 pour ciblage
        
        # Bullish FVG check (entre bougie -3 et -1)
        # Bougie -3: candle1, Bougie -2: candle2 (impulsion), Bougie -1: candle3
        candle1_high = df['high'].iloc[-3]
        candle3_low = df['low'].iloc[-1]
        
        if candle3_low > candle1_high: # Condition FVG haussier
            fvg_top = candle3_low
            fvg_bottom = candle1_high
            # SL structurel: juste sous le bas du FVG (ou le bas de la bougie d'impulsion si plus bas)
            sl_level = min(fvg_bottom, df['low'].iloc[-2]) 
            target_liquidity = swing_highs.tail(1)['high'].values[0] if not swing_highs.empty else None
            if target_liquidity:
                self.log.debug(f"Inbalance (FVG) haussière détectée. Zone [{fvg_bottom:.5f} - {fvg_top:.5f}], SL={sl_level:.5f}, Cible={target_liquidity:.5f}")
                return {
                    'pattern': PATTERN_INBALANCE, 'direction': BUY, 
                    'entry_zone_start': fvg_bottom, 'entry_zone_end': fvg_top, 
                    'stop_loss_level': sl_level, 'target_price': target_liquidity
                }

        # Bearish FVG check (entre bougie -3 et -1)
        candle1_low = df['low'].iloc[-3]
        candle3_high = df['high'].iloc[-1]

        if candle3_high < candle1_low: # Condition FVG baissier
            fvg_top = candle1_low
            fvg_bottom = candle3_high
            # SL structurel: juste au-dessus du haut du FVG (ou haut bougie impulsion)
            sl_level = max(fvg_top, df['high'].iloc[-2]) 
            target_liquidity = swing_lows.tail(1)['low'].values[0] if not swing_lows.empty else None
            if target_liquidity:
                self.log.debug(f"Inbalance (FVG) baissière détectée. Zone [{fvg_bottom:.5f} - {fvg_top:.5f}], SL={sl_level:.5f}, Cible={target_liquidity:.5f}")
                return {
                    'pattern': PATTERN_INBALANCE, 'direction': SELL, 
                    'entry_zone_start': fvg_bottom, 'entry_zone_end': fvg_top, 
                    'stop_loss_level': sl_level, 'target_price': target_liquidity
                }
            
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        # Confirmation, pas de zone d'entrée R7
        if len(df) < 20: return None
        
        # Identifier les points swing sur les données *précédentes* la bougie actuelle
        swing_highs, swing_lows = self._find_swing_points(df.iloc[:-1].copy(), n=3) 
        
        # Bullish Grab (Mèche sous ancien low, clôture au-dessus)
        if not swing_lows.empty:
            last_low_level = swing_lows['low'].iloc[-1]
            if df['low'].iloc[-1] < last_low_level and df['close'].iloc[-1] > last_low_level:
                 target_liquidity = swing_highs.tail(1)['high'].values[0] if not swing_highs.empty else None
                 if target_liquidity:
                     self.log.debug(f"Prise de liquidité haussière (Grab). Cible: {target_liquidity}")
                     # Pas de zone R7
                     return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': BUY, 'target_price': target_liquidity} 

        # Bearish Grab (Mèche au-dessus ancien high, clôture en dessous)
        if not swing_highs.empty:
            last_high_level = swing_highs['high'].iloc[-1]
            if df['high'].iloc[-1] > last_high_level and df['close'].iloc[-1] < last_high_level:
                target_liquidity = swing_lows.tail(1)['low'].values[0] if not swing_lows.empty else None
                if target_liquidity:
                    self.log.debug(f"Prise de liquidité baissière (Grab). Cible: {target_liquidity}")
                    # Pas de zone R7
                    return {'pattern': PATTERN_LIQUIDITY_GRAB, 'direction': SELL, 'target_price': target_liquidity}
                
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        # AMD peut créer une zone FVG/OB après la manipulation + CHOCH.
        # Pour R7, on va chercher cette zone *après* la confirmation AMD.
        # Cette fonction détecte la confirmation AMD, mais ne retourne pas de zone directe.
        
        asia_start, asia_end = time(0, 0), time(7, 0)
        london_open = time(8, 0)
        current_time_utc = df.index[-1].time()

        if not (london_open <= current_time_utc < time(16,0)): return None # Hors session Londres/NY

        today_utc = df.index[-1].date()
        asia_session_today = df.between_time(asia_start, asia_end)
        asia_session_today = asia_session_today[asia_session_today.index.date == today_utc]
        
        if asia_session_today.empty:
            self.detected_patterns_info[PATTERN_AMD] = {'status': 'Pas de données Asie'}
            return None

        asia_high = asia_session_today['high'].max()
        asia_low = asia_session_today['low'].min()
        self.detected_patterns_info[PATTERN_AMD] = {'status': f'Asie H:{asia_high:.5f} L:{asia_low:.5f}'}
        
        # Données depuis la fin de l'Asie jusqu'à maintenant
        post_asia_market_data = df.loc[df.index > asia_session_today.index[-1]]
        if post_asia_market_data.empty: return None

        # Manipulation sous le low asiatique?
        manipulation_low = post_asia_market_data['low'].min() < asia_low
        # Manipulation au-dessus du high asiatique?
        manipulation_high = post_asia_market_data['high'].max() > asia_high

        # Scénario AMD Haussier: Manipulation sous l'Asie + CHOCH Haussier ensuite
        if manipulation_low:
            # Chercher un CHOCH haussier *après* la manipulation
            choch_signal = self._detect_choch(post_asia_market_data.copy())
            if choch_signal and choch_signal['direction'] == BUY:
                self.log.debug(f"Pattern AMD haussier confirmé (Manipulation Low + CHOCH Buy).")
                # La cible est le high asiatique
                # Pas de zone R7 retournée par cette fonction seule.
                # Il faudrait chercher un FVG/OB créé par le CHOCH.
                return {'pattern': PATTERN_AMD, 'direction': BUY, 'target_price': asia_high} 

        # Scénario AMD Baissier: Manipulation au-dessus de l'Asie + CHOCH Baissier ensuite
        if manipulation_high:
            choch_signal = self._detect_choch(post_asia_market_data.copy())
            if choch_signal and choch_signal['direction'] == SELL:
                self.log.debug(f"Pattern AMD baissier confirmé (Manipulation High + CHOCH Sell).")
                # La cible est le low asiatique
                # Pas de zone R7 retournée.
                return {'pattern': PATTERN_AMD, 'direction': SELL, 'target_price': asia_low}
                    
        return None
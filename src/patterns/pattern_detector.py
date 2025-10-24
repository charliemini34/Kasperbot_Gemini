# Fichier: src/patterns/pattern_detector.py
# Version: 19.0.0 (Sugg-TopDown)
# Dépendances: pandas, numpy, logging, datetime, src.constants, typing
# DESCRIPTION: Refonte (Sugg 1-5) vers logique Top-Down (H4/M15).

import pandas as pd
import numpy as np
import logging
from datetime import time, timedelta
from typing import Optional, List, Dict
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_INBALANCE, PATTERN_BOS, PATTERN_CHOCH,
    BUY, SELL, NEUTRAL
)

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC.
    v19.0.0: Logique Top-Down (HTF Biais -> HTF POI -> LTF Confirmation).
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}
        # Stockage des zones POI HTF non mitigées (par symbole)
        self._unmitigated_htf_poi = {}
        # Stockage des états LTF (pour détecter CHOCH)
        self._ltf_state = {}

    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    def _calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        """ Calcule l'ATR (utilisé pour le filtre HTF). """
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period + 1:
            return None
        try:
             high_low = ohlc_data['high'] - ohlc_data['low']
             high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
             low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
             ranges = pd.concat([high_low, high_close, low_close], axis=1)
             true_range = np.max(ranges, axis=1)
             atr = true_range.ewm(span=period, adjust=False).mean().iloc[-1]
             if pd.isna(atr) or atr <= 0: return None
             return atr
        except Exception: return None

    def _find_swing_points(self, df: pd.DataFrame, n: int = 3):
        """ Détecte les swing points (fractals) n=3 (fenêtre 7 bougies). """
        window_size = n * 2 + 1
        df_historical = df.iloc[:-1] # Exclure bougie actuelle
        if 'is_swing_high' not in df.columns: df['is_swing_high'] = False
        if 'is_swing_low' not in df.columns: df['is_swing_low'] = False
        if len(df_historical) >= window_size:
             high_swings = df_historical['high'].rolling(window=window_size, center=True, min_periods=window_size).max() == df_historical['high']
             low_swings = df_historical['low'].rolling(window=window_size, center=True, min_periods=window_size).min() == df_historical['low']
             df.loc[high_swings.index, 'is_swing_high'] = high_swings
             df.loc[low_swings.index, 'is_swing_low'] = low_swings
        return df[df['is_swing_high'] == True], df[df['is_swing_low'] == True]

    # --- [Suggestion 1.1] Filtre de tendance Multi-EMA ---
    def _get_htf_bias(self, htf_data: pd.DataFrame, connector, symbol: str) -> str:
        """
        Détermine le biais HTF (BUY, SELL, NEUTRAL, ANY)
        basé sur la confluence EMA et la zone neutre ATR.
        """
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Désactivé'}
            return "ANY"

        ema_fast_period = filter_cfg.get('ema_fast_period', 50)
        ema_slow_period = filter_cfg.get('ema_slow_period', 200)
        dead_zone_atr_multiple = filter_cfg.get('ema_dead_zone_atr_multiple', 0.5)

        try:
            ema_fast = htf_data['close'].ewm(span=ema_fast_period, adjust=False).mean()
            ema_slow = htf_data['close'].ewm(span=ema_slow_period, adjust=False).mean()
            
            if pd.isna(ema_fast.iloc[-1]) or pd.isna(ema_slow.iloc[-1]):
                 self.log.warning(f"Calcul EMA ({ema_fast_period}/{ema_slow_period}) invalide (NaN).")
                 self.detected_patterns_info['TREND_FILTER'] = {'status': f'Erreur EMA HTF'}
                 return "ANY"

            current_price = htf_data['close'].iloc[-1]
            ema_fast_val = ema_fast.iloc[-1]
            ema_slow_val = ema_slow.iloc[-1]

            # Déterminer le biais de confluence
            is_bullish_bias = current_price > ema_fast_val and ema_fast_val > ema_slow_val
            is_bearish_bias = current_price < ema_fast_val and ema_fast_val < ema_slow_val
            
            bias_direction = "ANY"
            if is_bullish_bias: bias_direction = BUY
            elif is_bearish_bias: bias_direction = SELL
            else: bias_direction = NEUTRAL # Pas de confluence

            # Appliquer la zone neutre (autour de l'EMA rapide)
            if dead_zone_atr_multiple > 0:
                atr_period = self.config.get('risk_management', {}).get('atr_settings', {}).get('default', {}).get('period', 14)
                atr_htf = self._calculate_atr(htf_data, atr_period)
                if atr_htf:
                    dead_zone_size = atr_htf * dead_zone_atr_multiple
                    upper_band = ema_fast_val + dead_zone_size
                    lower_band = ema_fast_val - dead_zone_size
                    
                    if lower_band <= current_price <= upper_band:
                        status = f"NEUTRE (Zone ATR {dead_zone_size:.5f})"
                        self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} (HTF)"}
                        return NEUTRAL

            # Retourner le biais de confluence si hors zone neutre
            status = f"{bias_direction} (Confluence)"
            self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} (HTF)"}
            return bias_direction

        except Exception as e:
            self.log.error(f"Erreur dans le filtre de tendance HTF: {e}", exc_info=True)
            return "ANY"
    # --- Fin [Suggestion 1.1] ---

    # --- [Suggestion 4.1] Logique FVG (appliquée au HTF) ---
    def _identify_fvg_zones(self, impulse_data: pd.DataFrame, direction: str) -> List[Dict]:
        """ Identifie les FVG (Inbalance) dans le dataframe fourni. """
        zones = []
        # Parcourir les 10 dernières bougies
        search_start_index = max(0, len(impulse_data) - 10)
        
        for i in range(search_start_index, len(impulse_data)):
             if i < 2: continue # Besoin N, N-1, N-2

             candle_minus_2 = impulse_data.iloc[i-2] # N-2
             candle_current = impulse_data.iloc[i]  # N

             if direction == BUY:
                 if candle_current['low'] > candle_minus_2['high']:
                     fvg_low = candle_minus_2['high']
                     fvg_high = candle_current['low']
                     zone = {'type': PATTERN_INBALANCE, 'zone': (fvg_high, fvg_low), 'timestamp': candle_current.name}
                     zones.append(zone)
             elif direction == SELL:
                 if candle_current['high'] < candle_minus_2['low']:
                     fvg_low = candle_current['high']
                     fvg_high = candle_minus_2['low']
                     zone = {'type': PATTERN_INBALANCE, 'zone': (fvg_high, fvg_low), 'timestamp': candle_current.name}
                     zones.append(zone)
        return zones

    def _identify_ob_zones(self, df: pd.DataFrame, swing_point: pd.Series, direction: str) -> List[Dict]:
        """ Identifie l'OB pertinent (dernière bougie opposée avant le swing). """
        zones = []
        if direction == BUY: # Cherche OB Haussier (dernière bougie Sell avant swing Low)
            search_area = df[df.index < swing_point.name].tail(5)
            down_candles = search_area[search_area['close'] < search_area['open']]
            if not down_candles.empty:
                ob = down_candles.iloc[-1]
                zone = {'type': PATTERN_ORDER_BLOCK, 'zone': (ob['high'], ob['low']), 'timestamp': ob.name}
                zones.append(zone)
        elif direction == SELL: # Cherche OB Baissier (dernière bougie Buy avant swing High)
            search_area = df[df.index < swing_point.name].tail(5)
            up_candles = search_area[search_area['close'] > search_area['open']]
            if not up_candles.empty:
                ob = up_candles.iloc[-1]
                zone = {'type': PATTERN_ORDER_BLOCK, 'zone': (ob['high'], ob['low']), 'timestamp': ob.name}
                zones.append(zone)
        return zones

    # --- [Suggestion 2.1 / 3.1] Logique de recherche de POI HTF ---
    def _find_htf_poi(self, htf_data: pd.DataFrame, htf_bias: str) -> (List[Dict], float):
        """
        Identifie les POI HTF (FVG/OB) non mitigés dans le sens du biais.
        Retourne (liste_poi, target_liquidity_htf).
        """
        htf_swing_highs, htf_swing_lows = self._find_swing_points(htf_data.copy(), n=3)
        if htf_swing_highs.empty or htf_swing_lows.empty:
            return [], 0.0

        current_price = htf_data['close'].iloc[-1]
        all_poi = []
        target_liquidity = 0.0

        if htf_bias == BUY:
            last_htf_low = htf_swing_lows.iloc[-1]
            last_htf_high = htf_swing_highs.iloc[-1]
            target_liquidity = last_htf_high['high'] # Cible = liquidité externe
            
            # Chercher POI entre le dernier low et le prix actuel (dans le retracement)
            search_data = htf_data[htf_data.index >= last_htf_low.name]
            search_data = search_data[search_data['close'] < current_price] # Zones sous le prix actuel
            
            if not search_data.empty:
                # FVG Haussiers
                all_poi.extend(self._identify_fvg_zones(search_data, BUY))
                # OB Haussier (associé au dernier low)
                all_poi.extend(self._identify_ob_zones(htf_data, last_htf_low, BUY))

        elif htf_bias == SELL:
            last_htf_high = htf_swing_highs.iloc[-1]
            last_htf_low = htf_swing_lows.iloc[-1]
            target_liquidity = last_htf_low['low'] # Cible = liquidité externe
            
            search_data = htf_data[htf_data.index >= last_htf_high.name]
            search_data = search_data[search_data['close'] > current_price] # Zones au-dessus du prix actuel
            
            if not search_data.empty:
                # FVG Baissiers
                all_poi.extend(self._identify_fvg_zones(search_data, SELL))
                # OB Baissier (associé au dernier high)
                all_poi.extend(self._identify_ob_zones(htf_data, last_htf_high, SELL))

        # Filtrer les POI mitigés (déjà touchés par le prix actuel)
        if htf_bias == BUY:
            unmitigated_poi = [p for p in all_poi if p['zone'][0] > current_price] # Haut de la zone > prix
        elif htf_bias == SELL:
            unmitigated_poi = [p for p in all_poi if p['zone'][1] < current_price] # Bas de la zone < prix
        else:
            unmitigated_poi = []

        return unmitigated_poi, target_liquidity
    # --- Fin [Suggestion 2.1 / 3.1] ---

    # --- [Suggestion 2.1 / 4.2] Logique de confirmation LTF ---
    def _check_ltf_confirmation(self, ltf_data: pd.DataFrame, poi_htf: Dict, htf_bias: str, symbol: str) -> (str, float):
        """
        Vérifie si le prix LTF entre dans la POI HTF et forme un CHOCH
        (Change of Character) ou un BOS (Break of Structure) de confirmation.
        Retourne (pattern_name, entry_price) si confirmé.
        """
        if symbol not in self._ltf_state:
            self._ltf_state[symbol] = {'last_swing_high': None, 'last_swing_low': None}

        ltf_swing_highs, ltf_swing_lows = self._find_swing_points(ltf_data.copy(), n=3)
        if ltf_swing_highs.empty or ltf_swing_lows.empty:
            return None, 0.0

        current_ltf_candle = ltf_data.iloc[-1]
        poi_high, poi_low = poi_htf['zone']

        if htf_bias == BUY:
            # 1. Le prix LTF doit entrer dans la POI HTF (zone d'achat)
            if current_ltf_candle['low'] > poi_high: return None, 0.0 # Pas encore dans la zone
            
            # 2. Détecter la structure LTF baissière (pendant le retracement)
            if not ltf_swing_highs[ltf_swing_highs.index < current_ltf_candle.name].empty:
                self._ltf_state[symbol]['last_swing_high'] = ltf_swing_highs[ltf_swing_highs.index < current_ltf_candle.name].iloc[-1]['high']

            # 3. Chercher la confirmation (CHOCH Haussier LTF)
            last_ltf_high = self._ltf_state[symbol].get('last_swing_high')
            if last_ltf_high and current_ltf_candle['close'] > last_ltf_high:
                self.log.info(f"CONFIRMATION LTF (CHOCH Buy) sur {symbol}: Rupture du swing high LTF {last_ltf_high:.5f} dans la POI HTF {poi_htf['type']}.")
                self._ltf_state[symbol]['last_swing_high'] = None # Réinitialiser
                return PATTERN_CHOCH, current_ltf_candle['close']

        elif htf_bias == SELL:
            # 1. Le prix LTF doit entrer dans la POI HTF (zone de vente)
            if current_ltf_candle['high'] < poi_low: return None, 0.0 # Pas encore dans la zone
            
            # 2. Détecter la structure LTF haussière (pendant le retracement)
            if not ltf_swing_lows[ltf_swing_lows.index < current_ltf_candle.name].empty:
                self._ltf_state[symbol]['last_swing_low'] = ltf_swing_lows[ltf_swing_lows.index < current_ltf_candle.name].iloc[-1]['low']

            # 3. Chercher la confirmation (CHOCH Baissier LTF)
            last_ltf_low = self._ltf_state[symbol].get('last_swing_low')
            if last_ltf_low and current_ltf_candle['close'] < last_ltf_low:
                self.log.info(f"CONFIRMATION LTF (CHOCH Sell) sur {symbol}: Rupture du swing low LTF {last_ltf_low:.5f} dans la POI HTF {poi_htf['type']}.")
                self._ltf_state[symbol]['last_swing_low'] = None # Réinitialiser
                return PATTERN_CHOCH, current_ltf_candle['close']

        return None, 0.0
    # --- Fin [Suggestion 2.1 / 4.2] ---

    # --- [Suggestion 2.1] Fonction principale (Top-Down) ---
    def detect_patterns(self, htf_data: pd.DataFrame, ltf_data: pd.DataFrame, connector, symbol: str):
        
        # S'assurer que les index sont des Datetime UTC
        for df in [htf_data, ltf_data]:
            if not isinstance(df.index, pd.DatetimeIndex):
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df.set_index('time', inplace=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC')

        if symbol not in self._unmitigated_htf_poi:
            self._unmitigated_htf_poi[symbol] = []

        # 1. Déterminer le biais HTF (Sugg 1.1)
        htf_bias = self._get_htf_bias(htf_data, connector, symbol)
        if htf_bias == NEUTRAL or htf_bias == "ANY":
            self.detected_patterns_info['HTF_POI'] = {'status': f'Biais HTF Non Concluant ({htf_bias})'}
            return None

        # 2. Identifier les POI HTF (Sugg 2.1)
        # Nous mettons à jour les POI uniquement si la dernière bougie HTF a changé
        # (Pour l'instant, nous recalculons à chaque cycle LTF pour simplicité)
        unmitigated_poi, target_liquidity_htf = self._find_htf_poi(htf_data, htf_bias)
        
        if not unmitigated_poi:
            self.detected_patterns_info['HTF_POI'] = {'status': 'Aucune POI HTF valide'}
            return None
            
        # Trier les POI pour vérifier la plus proche en premier
        if htf_bias == BUY:
            unmitigated_poi.sort(key=lambda p: p['zone'][1], reverse=True) # Trier par bas de zone (plus haut en premier)
        elif htf_bias == SELL:
            unmitigated_poi.sort(key=lambda p: p['zone'][0], reverse=False) # Trier par haut de zone (plus bas en premier)

        self.detected_patterns_info['HTF_POI'] = {'status': f'{len(unmitigated_poi)} POI HTF trouvées'}

        # 3. Vérifier la confirmation LTF (Sugg 2.1 / 4.2)
        # Nous vérifions si le prix LTF actuel entre dans la POI HTF *la plus proche*
        
        poi_to_watch = unmitigated_poi[0] # La POI la plus proche
        
        pattern_name, entry_price = self._check_ltf_confirmation(
            ltf_data, poi_to_watch, htf_bias, symbol
        )

        if pattern_name and entry_price > 0:
            
            # (Sugg 3.1) Assigner la cible de liquidité HTF
            if target_liquidity_htf == 0.0:
                 self.log.warning(f"Signal {pattern_name} trouvé, mais cible HTF invalide (0.0).")
                 return None

            self.log.info(f"SIGNAL (Top-Down) sur {symbol}: Biais {htf_bias} -> POI {poi_to_watch['type']} -> Conf. {pattern_name}.")
            
            # Marquer la POI comme "observée" (pourrait être marquée mitigée après confirmation)
            # (Logique de mitigation plus avancée nécessaire ici)
            
            return {
                'pattern': f"{poi_to_watch['type']}_H4_CONF_{pattern_name}_M15",
                'direction': htf_bias,
                'target_price': target_liquidity_htf # (Sugg 3.1)
            }

        return None
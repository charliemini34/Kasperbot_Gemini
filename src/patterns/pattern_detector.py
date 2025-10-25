# Fichier: src/patterns/pattern_detector.py
# Version: 19.1.2 (Implémentation Sugg 5, 9.1)
# Dépendances: pandas, numpy, logging, datetime, src.constants, typing
# DESCRIPTION: Ajout Sugg 5 (Validation OB avec FVG) et Sugg 9.1 (Retour poi_zone).

import pandas as pd
import numpy as np
import logging
from datetime import time, timedelta
from typing import Optional, List, Dict, Tuple
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_INBALANCE, PATTERN_BOS, PATTERN_CHOCH,
    BUY, SELL, NEUTRAL, PREMIUM_THRESHOLD
)

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC (Top-Down).
    v19.1.2: Ajout Sugg 5 (Validation OB avec FVG), Sugg 9.1 (Retour poi_zone).
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}
        # Stockage POI HTF non mitigées (par symbole)
        # Format: {symbol: [{'type':..., 'zone':(h,l), 'timestamp':..., 'mitigated': False}, ...]}
        self._unmitigated_htf_poi: Dict[str, List[Dict]] = {}
        # Stockage états LTF (pour CHOCH)
        self._ltf_state: Dict[str, Dict] = {}
        
        # --- [Optimisation 2] Cache pour Indicateurs HTF ---
        # Format: {symbol: {'timestamp': ..., 'bias': ..., 'poi': [...], 'target': ...}}
        self._htf_cache: Dict[str, Dict] = {}
        # --- Fin [Optimisation 2] ---

    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    # --- Fonctions utilitaires (inchangées) ---
    def _calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period + 1: return None
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
        window_size = n * 2 + 1
        df_historical = df.iloc[:-1]
        if 'is_swing_high' not in df.columns: df['is_swing_high'] = False
        if 'is_swing_low' not in df.columns: df['is_swing_low'] = False
        if len(df_historical) >= window_size:
             high_swings = df_historical['high'].rolling(window=window_size, center=True, min_periods=window_size).max() == df_historical['high']
             low_swings = df_historical['low'].rolling(window=window_size, center=True, min_periods=window_size).min() == df_historical['low']
             df.loc[high_swings.index, 'is_swing_high'] = high_swings
             df.loc[low_swings.index, 'is_swing_low'] = low_swings
        return df[df['is_swing_high'] == True], df[df['is_swing_low'] == True]

    def _get_htf_bias(self, htf_data: pd.DataFrame, connector, symbol: str) -> str:
        # (Logique inchangée - Confluence EMA + Zone Neutre)
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False): return "ANY"
        ema_fast_period = filter_cfg.get('ema_fast_period', 50)
        ema_slow_period = filter_cfg.get('ema_slow_period', 200)
        dead_zone_atr_multiple = filter_cfg.get('ema_dead_zone_atr_multiple', 0.5)
        try:
            ema_fast = htf_data['close'].ewm(span=ema_fast_period, adjust=False).mean()
            ema_slow = htf_data['close'].ewm(span=ema_slow_period, adjust=False).mean()
            if pd.isna(ema_fast.iloc[-1]) or pd.isna(ema_slow.iloc[-1]): return "ANY"
            current_price = htf_data['close'].iloc[-1]
            ema_fast_val = ema_fast.iloc[-1]; ema_slow_val = ema_slow.iloc[-1]
            is_bullish_bias = current_price > ema_fast_val and ema_fast_val > ema_slow_val
            is_bearish_bias = current_price < ema_fast_val and ema_fast_val < ema_slow_val
            bias_direction = BUY if is_bullish_bias else SELL if is_bearish_bias else NEUTRAL
            if dead_zone_atr_multiple > 0:
                atr_period = self.config.get('risk_management',{}).get('atr_settings',{}).get('default',{}).get('period', 14)
                atr_htf = self._calculate_atr(htf_data, atr_period)
                if atr_htf:
                    dead_zone_size = atr_htf * dead_zone_atr_multiple
                    upper_band = ema_fast_val + dead_zone_size; lower_band = ema_fast_val - dead_zone_size
                    if lower_band <= current_price <= upper_band: bias_direction = NEUTRAL
            status = f"{bias_direction} (Confluence)"
            self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} (HTF)"}
            return bias_direction
        except Exception as e:
            self.log.error(f"Erreur filtre tendance HTF: {e}", exc_info=True)
            return "ANY"

    def _identify_fvg_zones(self, impulse_data: pd.DataFrame, direction: str) -> List[Dict]:
        # (Logique inchangée - Corrigée en V19.0.0)
        zones = []
        search_start_index = max(0, len(impulse_data) - 10)
        for i in range(search_start_index, len(impulse_data)):
             if i < 2: continue
             candle_minus_2 = impulse_data.iloc[i-2]; candle_current = impulse_data.iloc[i]
             if direction == BUY and candle_current['low'] > candle_minus_2['high']:
                 zones.append({'type': PATTERN_INBALANCE, 'zone': (candle_current['low'], candle_minus_2['high']), 'timestamp': candle_current.name, 'mitigated': False})
             elif direction == SELL and candle_current['high'] < candle_minus_2['low']:
                 zones.append({'type': PATTERN_INBALANCE, 'zone': (candle_minus_2['low'], candle_current['high']), 'timestamp': candle_current.name, 'mitigated': False})
        return zones

    # --- MODIFICATION SUGGESTION 5 ---
    def _identify_ob_zones(self, df: pd.DataFrame, swing_point: pd.Series, direction: str) -> List[Dict]:
        """
        Identifie les zones d'Order Block (OB) avant un swing.
        Inclut la validation optionnelle par FVG (Sugg 5).
        """
        zones = []
        ob = None
        
        if direction == BUY: # Cherche OB Haussier (dernière bougie Sell avant swing Low)
            search_area = df[df.index < swing_point.name].tail(5)
            down_candles = search_area[search_area['close'] < search_area['open']]
            if not down_candles.empty:
                ob = down_candles.iloc[-1]
        
        elif direction == SELL: # Cherche OB Baissier (dernière bougie Buy avant swing High)
            search_area = df[df.index < swing_point.name].tail(5)
            up_candles = search_area[search_area['close'] > search_area['open']]
            if not up_candles.empty:
                ob = up_candles.iloc[-1]

        if ob is None:
            return zones # Aucun OB candidat trouvé

        # Vérification des options de validation (Sugg 5)
        validation_cfg = self.config.get('pattern_detection', {}).get('ob_validation', {})
        require_fvg = validation_cfg.get('require_fvg_after', False)

        if not require_fvg:
            # Pas de validation requise, ajouter l'OB trouvé
            zones.append({'type': PATTERN_ORDER_BLOCK, 'zone': (ob['high'], ob['low']), 'timestamp': ob.name, 'mitigated': False})
            return zones

        # --- Validation FVG Requise ---
        try:
            ob_index_loc = df.index.get_loc(ob.name)
            # Regarder les 5 bougies après l'OB pour un FVG (l'impulsion)
            data_after_ob = df.iloc[ob_index_loc + 1 : ob_index_loc + 6]

            if data_after_ob.empty or len(data_after_ob) < 3:
                self.log.debug(f"Validation OB: Pas assez de données après l'OB {ob.name} pour chercher FVG.")
                return zones # Pas assez de données pour FVG

            fvg_found = False
            # Chercher un FVG (Imbalance) dans la MÊME direction que le biais
            for i in range(2, len(data_after_ob)):
                candle_minus_2 = data_after_ob.iloc[i-2]
                candle_current = data_after_ob.iloc[i]
                
                # FVG Haussier (pour Biais BUY)
                if direction == BUY and candle_current['low'] > candle_minus_2['high']:
                    fvg_found = True
                    break
                # FVG Baissier (pour Biais SELL)
                elif direction == SELL and candle_current['high'] < candle_minus_2['low']:
                    fvg_found = True
                    break
            
            if fvg_found:
                self.log.debug(f"Validation OB: OB {ob.name} validé par FVG suivant.")
                zones.append({'type': PATTERN_ORDER_BLOCK, 'zone': (ob['high'], ob['low']), 'timestamp': ob.name, 'mitigated': False})
            else:
                self.log.debug(f"Validation OB: OB {ob.name} REJETÉ (pas de FVG trouvé après).")

        except Exception as e:
            self.log.error(f"Erreur validation OB FVG: {e}", exc_info=True)

        return zones
    # --- FIN MODIFICATION SUGGESTION 5 ---

    # --- [Opt 3 + Opt 5] Logique POI HTF avec Mitigation et Premium/Discount ---
    def _find_htf_poi(self, htf_data: pd.DataFrame, htf_bias: str, symbol: str) -> (List[Dict], float):
        """
        Identifie POI HTF non mitigés, priorise Premium/Discount, gère mitigation.
        Retourne (liste_poi_priorisee, target_liquidity_htf).
        Utilise _identify_ob_zones (modifié Sugg 5)
        """
        htf_swing_highs, htf_swing_lows = self._find_swing_points(htf_data.copy(), n=3)
        if htf_swing_highs.empty or htf_swing_lows.empty: return [], 0.0

        current_price = htf_data['close'].iloc[-1]
        all_new_poi = []
        target_liquidity = 0.0
        htf_range_high = 0.0
        htf_range_low = 0.0

        # Récupérer les POI existantes (potentiellement mitigées)
        existing_poi_list = self._unmitigated_htf_poi.get(symbol, [])

        if htf_bias == BUY:
            last_htf_low_serie = htf_swing_lows.iloc[-1]
            last_htf_high_serie = htf_swing_highs.iloc[-1]
            target_liquidity = last_htf_high_serie['high']
            htf_range_low = last_htf_low_serie['low']
            htf_range_high = last_htf_high_serie['high']
            
            search_data = htf_data[htf_data.index >= last_htf_low_serie.name]
            if not search_data.empty:
                all_new_poi.extend(self._identify_fvg_zones(search_data, BUY))
                # Appel à la fonction modifiée (Sugg 5)
                all_new_poi.extend(self._identify_ob_zones(htf_data, last_htf_low_serie, BUY))

        elif htf_bias == SELL:
            last_htf_high_serie = htf_swing_highs.iloc[-1]
            last_htf_low_serie = htf_swing_lows.iloc[-1]
            target_liquidity = last_htf_low_serie['low']
            htf_range_low = last_htf_low_serie['low']
            htf_range_high = last_htf_high_serie['high']

            search_data = htf_data[htf_data.index >= last_htf_high_serie.name]
            if not search_data.empty:
                all_new_poi.extend(self._identify_fvg_zones(search_data, SELL))
                # Appel à la fonction modifiée (Sugg 5)
                all_new_poi.extend(self._identify_ob_zones(htf_data, last_htf_high_serie, SELL))

        # Fusionner nouvelles POI et existantes, dédupliquer, mettre à jour mitigation
        updated_poi_list = []
        seen_zones = set()
        
        # D'abord les anciennes pour conserver l'état 'mitigated'
        for poi in existing_poi_list:
            zone_tuple = tuple(poi['zone'])
            if zone_tuple not in seen_zones:
                 # [Opt 3] Vérifier mitigation par prix actuel
                 if not poi.get('mitigated', False): # Ne pas remittiger si déjà fait
                      if htf_bias == BUY and current_price <= poi['zone'][0]: # Prix a touché/dépassé le haut
                           poi['mitigated'] = True
                           self.log.debug(f"POI HTF {poi['type']} {poi['zone']} marquée mitigée par prix actuel ({symbol}).")
                      elif htf_bias == SELL and current_price >= poi['zone'][1]: # Prix a touché/dépassé le bas
                           poi['mitigated'] = True
                           self.log.debug(f"POI HTF {poi['type']} {poi['zone']} marquée mitigée par prix actuel ({symbol}).")
                 
                 updated_poi_list.append(poi)
                 seen_zones.add(zone_tuple)

        # Ajouter les nouvelles POI non vues
        for poi in all_new_poi:
            zone_tuple = tuple(poi['zone'])
            if zone_tuple not in seen_zones:
                 # Vérifier mitigation initiale par prix actuel
                 if htf_bias == BUY and current_price <= poi['zone'][0]: poi['mitigated'] = True
                 elif htf_bias == SELL and current_price >= poi['zone'][1]: poi['mitigated'] = True
                 
                 updated_poi_list.append(poi)
                 seen_zones.add(zone_tuple)
        
        # Conserver la liste mise à jour
        self._unmitigated_htf_poi[symbol] = updated_poi_list
        
        # Filtrer seulement les POI non mitigées pour la priorisation
        active_poi = [p for p in updated_poi_list if not p.get('mitigated', False)]

        # [Opt 5] Priorisation Premium/Discount
        prioritized_poi = []
        if htf_range_high > htf_range_low: # S'assurer que le range est valide
            equilibrium = htf_range_low + (htf_range_high - htf_range_low) * PREMIUM_THRESHOLD
            
            if htf_bias == BUY:
                discount_poi = [p for p in active_poi if p['zone'][0] < equilibrium] # Haut de zone < 50%
                discount_poi.sort(key=lambda p: p['zone'][1], reverse=True) # Plus proche du prix en premier
                prioritized_poi = discount_poi
            elif htf_bias == SELL:
                premium_poi = [p for p in active_poi if p['zone'][1] > equilibrium] # Bas de zone > 50%
                premium_poi.sort(key=lambda p: p['zone'][0], reverse=False) # Plus proche du prix en premier
                prioritized_poi = premium_poi
        else:
             # Fallback si range invalide: trier par proximité
             if htf_bias == BUY: active_poi.sort(key=lambda p: p['zone'][1], reverse=True)
             elif htf_bias == SELL: active_poi.sort(key=lambda p: p['zone'][0], reverse=False)
             prioritized_poi = active_poi
             
        if prioritized_poi:
            self.log.debug(f"POI HTF Priorisée ({symbol}, Biais {htf_bias}): {prioritized_poi[0]['type']} {prioritized_poi[0]['zone']}")
        
        return prioritized_poi, target_liquidity
    # --- Fin [Opt 3 + Opt 5] ---

    # --- [Opt 3] Logique de confirmation LTF avec mitigation ---
    def _check_ltf_confirmation(self, ltf_data: pd.DataFrame, poi_htf: Dict, htf_bias: str, symbol: str) -> (str, float, Dict):
        """
        Vérifie confirmation LTF (CHOCH) dans POI HTF.
        Retourne (pattern_name, entry_price, poi_used_dict) si confirmé.
        Marque la POI utilisée comme mitigée.
        """
        if symbol not in self._ltf_state:
            self._ltf_state[symbol] = {'last_swing_high': None, 'last_swing_low': None}

        ltf_swing_highs, ltf_swing_lows = self._find_swing_points(ltf_data.copy(), n=3)
        if ltf_swing_highs.empty or ltf_swing_lows.empty: return None, 0.0, None

        current_ltf_candle = ltf_data.iloc[-1]
        poi_high, poi_low = poi_htf['zone']
        poi_timestamp = poi_htf['timestamp'] # Pour identifier la POI à mitiger

        confirmed_pattern = None
        entry_price = 0.0

        if htf_bias == BUY:
            if current_ltf_candle['low'] <= poi_high: # Prix dans ou sous la zone d'achat POI
                if not ltf_swing_highs[ltf_swing_highs.index < current_ltf_candle.name].empty:
                    self._ltf_state[symbol]['last_swing_high'] = ltf_swing_highs[ltf_swing_highs.index < current_ltf_candle.name].iloc[-1]['high']
                last_ltf_high = self._ltf_state[symbol].get('last_swing_high')
                if last_ltf_high and current_ltf_candle['close'] > last_ltf_high:
                    confirmed_pattern = PATTERN_CHOCH; entry_price = current_ltf_candle['close']
                    self._ltf_state[symbol]['last_swing_high'] = None # Reset après confirmation
            else: # Prix n'est pas encore entré
                 self._ltf_state[symbol]['last_swing_high'] = None # Reset si prix sort par le haut

        elif htf_bias == SELL:
            if current_ltf_candle['high'] >= poi_low: # Prix dans ou au-dessus de la zone de vente POI
                if not ltf_swing_lows[ltf_swing_lows.index < current_ltf_candle.name].empty:
                    self._ltf_state[symbol]['last_swing_low'] = ltf_swing_lows[ltf_swing_lows.index < current_ltf_candle.name].iloc[-1]['low']
                last_ltf_low = self._ltf_state[symbol].get('last_swing_low')
                if last_ltf_low and current_ltf_candle['close'] < last_ltf_low:
                    confirmed_pattern = PATTERN_CHOCH; entry_price = current_ltf_candle['close']
                    self._ltf_state[symbol]['last_swing_low'] = None # Reset après confirmation
            else: # Prix n'est pas encore entré
                 self._ltf_state[symbol]['last_swing_low'] = None # Reset si prix sort par le bas

        if confirmed_pattern:
            self.log.info(f"CONFIRMATION LTF ({confirmed_pattern} {htf_bias}) sur {symbol} dans POI HTF {poi_htf['type']} {poi_htf['zone']}.")
            # [Opt 3] Marquer la POI comme mitigée dans la liste principale
            for poi in self._unmitigated_htf_poi.get(symbol, []):
                 if poi['timestamp'] == poi_timestamp and tuple(poi['zone']) == tuple(poi_htf['zone']):
                      poi['mitigated'] = True
                      self.log.debug(f"POI HTF {poi['type']} {poi['zone']} marquée mitigée après confirmation LTF ({symbol}).")
                      break
            # [MODIFICATION SUGG 9.1] Retourner poi_htf (le dict)
            return confirmed_pattern, entry_price, poi_htf
            
        return None, 0.0, None
    # --- Fin [Opt 3] ---

    # --- [Opt 2] Fonction principale avec Cache HTF ---
    def detect_patterns(self, htf_data: pd.DataFrame, ltf_data: pd.DataFrame, connector, symbol: str):
        
        # Vérifier et préparer les DataFrames (inchangé)
        for df in [htf_data, ltf_data]:
            if not isinstance(df.index, pd.DatetimeIndex):
                try:
                    df['time'] = pd.to_datetime(df['time'], unit='s'); df.set_index('time', inplace=True)
                except Exception as e:
                    self.log.error(f"Échec conversion index temps pour {symbol}: {e}. Colonnes: {df.columns}")
                    return None
            if df.index.tz is None: df.index = df.index.tz_localize('UTC')

        current_htf_timestamp = htf_data.index[-1]
        htf_bias = NEUTRAL
        prioritized_poi = []
        target_liquidity_htf = 0.0

        # [Opt 2] Utiliser le cache HTF si possible
        cached_data = self._htf_cache.get(symbol)
        if cached_data and cached_data.get('timestamp') == current_htf_timestamp:
            htf_bias = cached_data.get('bias', NEUTRAL)
            prioritized_poi = cached_data.get('poi', [])
            target_liquidity_htf = cached_data.get('target', 0.0)
            self.log.debug(f"Cache HTF HIT pour {symbol} @ {current_htf_timestamp}")
            # [Opt 3] Re-vérifier la mitigation des POI cachées par le prix actuel LTF
            current_ltf_price = ltf_data['close'].iloc[-1]
            poi_updated = False
            for poi in prioritized_poi:
                 if not poi.get('mitigated'):
                     if htf_bias == BUY and current_ltf_price <= poi['zone'][0]:
                          poi['mitigated'] = True; poi_updated = True
                     elif htf_bias == SELL and current_ltf_price >= poi['zone'][1]:
                          poi['mitigated'] = True; poi_updated = True
            if poi_updated: # Filtrer à nouveau si mitigation a eu lieu
                prioritized_poi = [p for p in prioritized_poi if not p.get('mitigated')]
                
        else:
            # [Opt 2] Cache MISS ou Invalide -> Recalculer HTF
            self.log.debug(f"Cache HTF MISS/EXPIRED pour {symbol}. Recalcul HTF...")
            htf_bias = self._get_htf_bias(htf_data, connector, symbol)
            
            if htf_bias != NEUTRAL and htf_bias != "ANY":
                # [Opt 3 + Opt 5] _find_htf_poi gère mitigation et priorisation
                prioritized_poi, target_liquidity_htf = self._find_htf_poi(htf_data, htf_bias, symbol)
            
            # [Opt 2] Mettre à jour le cache HTF
            self._htf_cache[symbol] = {
                'timestamp': current_htf_timestamp,
                'bias': htf_bias,
                'poi': prioritized_poi, # Contient déjà l'état 'mitigated'
                'target': target_liquidity_htf
            }

        # Suite de la logique (inchangée): Vérifier confirmation LTF sur POI priorisée
        if htf_bias == NEUTRAL or htf_bias == "ANY":
            self.detected_patterns_info['HTF_POI'] = {'status': f'Biais HTF Non Concluant ({htf_bias})'}
            return None

        if not prioritized_poi:
            self.detected_patterns_info['HTF_POI'] = {'status': 'Aucune POI HTF active/priorisée'}
            return None
            
        self.detected_patterns_info['HTF_POI'] = {'status': f'{len(prioritized_poi)} POI HTF active(s)'}

        # Vérifier confirmation sur la POI la plus priorisée (la première)
        poi_to_watch = prioritized_poi[0]
        
        # [Opt 3] _check_ltf_confirmation marque la POI comme mitigée si confirmation
        # [MODIFICATION SUGG 9.1] poi_used est maintenant un dict
        pattern_name, entry_price, poi_used = self._check_ltf_confirmation(
            ltf_data, poi_to_watch, htf_bias, symbol
        )

        if pattern_name and entry_price > 0:
            if target_liquidity_htf == 0.0:
                 self.log.warning(f"Signal {pattern_name} trouvé, mais cible HTF invalide (0.0).")
                 return None

            self.log.info(f"SIGNAL (Top-Down) sur {symbol}: Biais {htf_bias} -> POI {poi_used['type']} -> Conf. {pattern_name}.")
            
            # [MODIFICATION SUGG 9.1] Ajouter poi_zone
            return {
                'pattern': f"{poi_used['type']}_HTF_CONF_{pattern_name}_LTF", # Nom plus générique
                'direction': htf_bias,
                'target_price': target_liquidity_htf,
                'poi_zone': poi_used['zone'] # Ajouté pour Sugg 9 (Rating)
            }

        return None
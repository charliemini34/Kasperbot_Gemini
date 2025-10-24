
# Fichier: src/patterns/pattern_detector.py
# Version: 18.2.0 (SMC-Retest-AMD)
# Dépendances: pandas, numpy, logging, datetime, src.constants
# DESCRIPTION: Intègre le pattern AMD avec une logique de retest cohérente
#              avec FVG/OB après une manipulation de session Asiatique suivie d'un BOS.

import pandas as pd
import numpy as np
import logging
from datetime import time, timedelta # Ajout timedelta pour AMD
from src.constants import (
    PATTERN_ORDER_BLOCK, PATTERN_CHOCH, PATTERN_INBALANCE, PATTERN_LIQUIDITY_GRAB,
    PATTERN_AMD, BUY, SELL
)

class PatternDetector:
    """
    Module de reconnaissance de patterns SMC & ICT.
    v18.2.0: Ajoute la logique de retest pour le pattern AMD.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}
        # Stockage des zones non mitigées (pour le retest)
        self._unmitigated_zones = {}
        # Stockage de l'état AMD pour la journée en cours
        self._amd_state = {'date': None, 'manipulation_direction': None, 'asia_high': None, 'asia_low': None}


    def get_detected_patterns_info(self):
        return self.detected_patterns_info.copy()

    def _get_trend_filter_direction(self, connector, symbol: str) -> str:
        # (Fonction inchangée - cf. V18.0.0)
        filter_cfg = self.config.get('trend_filter', {})
        if not filter_cfg.get('enabled', False):
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Désactivé'}
            return "ANY"

        higher_timeframe = filter_cfg.get('higher_timeframe', 'H4')
        period = filter_cfg.get('ema_period', 200)

        try:
            # Demander plus de données pour s'assurer que l'EMA est calculable
            htf_data = connector.get_ohlc(symbol, higher_timeframe, period + 100)
            if htf_data is None or htf_data.empty or len(htf_data) < period:
                self.log.warning(f"Données {higher_timeframe} insuffisantes ({len(htf_data) if htf_data is not None else 0}) pour le filtre de tendance (EMA {period}).")
                self.detected_patterns_info['TREND_FILTER'] = {'status': f'Données {higher_timeframe} insuff.'}
                return "ANY" # Retourner ANY si données insuffisantes

            ema = htf_data['close'].ewm(span=period, adjust=False).mean()
            # S'assurer que l'EMA a des valeurs valides à la fin
            if pd.isna(ema.iloc[-1]):
                 self.log.warning(f"Calcul EMA ({period}) sur {higher_timeframe} invalide (NaN).")
                 self.detected_patterns_info['TREND_FILTER'] = {'status': f'Erreur EMA {higher_timeframe}'}
                 return "ANY"

            current_price = htf_data['close'].iloc[-1]

            status = "HAUSSIÈRE" if current_price > ema.iloc[-1] else "BAISSIÈRE"
            self.detected_patterns_info['TREND_FILTER'] = {'status': f"{status} ({higher_timeframe})"}
            return BUY if status == "HAUSSIÈRE" else SELL
        except Exception as e:
            self.log.error(f"Erreur dans le filtre de tendance : {e}", exc_info=True)
            self.detected_patterns_info['TREND_FILTER'] = {'status': 'Erreur Filtre'}
            return "ANY"


    def _find_swing_points(self, df: pd.DataFrame, n: int = 3):
        """
        Détecte les swing points (fractals) en utilisant n=3 (fenêtre de 7 bougies).
        """
        # (Fonction inchangée - cf. V18.1.0)
        window_size = n * 2 + 1
        df_historical = df.iloc[:-1]

        # Initialiser les colonnes si elles n'existent pas pour éviter KeyError
        if 'is_swing_high' not in df.columns: df['is_swing_high'] = False
        if 'is_swing_low' not in df.columns: df['is_swing_low'] = False

        if len(df_historical) >= window_size:
             high_swings = df_historical['high'].rolling(window=window_size, center=True, min_periods=window_size).max() == df_historical['high']
             low_swings = df_historical['low'].rolling(window=window_size, center=True, min_periods=window_size).min() == df_historical['low']
             df.loc[high_swings.index, 'is_swing_high'] = high_swings
             df.loc[low_swings.index, 'is_swing_low'] = low_swings

        swing_highs = df[df['is_swing_high'] == True]
        swing_lows = df[df['is_swing_low'] == True]

        return swing_highs, swing_lows


    def _identify_fvg_zones(self, impulse_data: pd.DataFrame, direction: str):
        """
        Identifie les FVG (Inbalance) créés lors du mouvement impulsif.
        """
        # (Fonction inchangée - cf. V18.1.0)
        zones = []
        for i in range(max(5, len(impulse_data) - 5), len(impulse_data)):
             if i < 4: continue

             df_slice = impulse_data.iloc[:i+1]

             if direction == BUY:
                 if df_slice['low'].iloc[-2] > df_slice['high'].iloc[-4]:
                     fvg_low = df_slice['high'].iloc[-4]
                     fvg_high = df_slice['low'].iloc[-2]
                     zone = {'type': PATTERN_INBALANCE, 'zone': (fvg_high, fvg_low)}
                     if zone not in zones:
                         zones.append(zone)
                         self.log.debug(f"FVG Haussier (retest) identifié: ({fvg_high:.5f} - {fvg_low:.5f})")

             elif direction == SELL:
                 if df_slice['high'].iloc[-2] < df_slice['low'].iloc[-4]:
                     fvg_low = df_slice['low'].iloc[-2]
                     fvg_high = df_slice['high'].iloc[-4]
                     zone = {'type': PATTERN_INBALANCE, 'zone': (fvg_high, fvg_low)}
                     if zone not in zones:
                         zones.append(zone)
                         self.log.debug(f"FVG Baissier (retest) identifié: ({fvg_high:.5f} - {fvg_low:.5f})")

        return zones

    def _identify_ob_zones(self, impulse_data: pd.DataFrame, swing_low_before_bos: pd.Series, swing_high_before_bos: pd.Series, direction: str):
        """
        Identifie l'Order Block (OB) pertinent.
        """
        # (Fonction inchangée - cf. V18.1.0)
        zones = []
        if direction == BUY:
            if swing_low_before_bos is not None and not swing_low_before_bos.empty:
                search_area = impulse_data[impulse_data.index <= swing_low_before_bos.name].tail(5)
                down_candles = search_area[search_area['close'] < search_area['open']]
                if not down_candles.empty:
                    ob = down_candles.iloc[-1]
                    ob_high = ob['high']
                    ob_low = ob['low']
                    zone = {'type': PATTERN_ORDER_BLOCK, 'zone': (ob_high, ob_low)}
                    zones.append(zone)
                    self.log.debug(f"OB Haussier (retest) identifié: ({ob_high:.5f} - {ob_low:.5f})")

        elif direction == SELL:
            if swing_high_before_bos is not None and not swing_high_before_bos.empty:
                search_area = impulse_data[impulse_data.index <= swing_high_before_bos.name].tail(5)
                up_candles = search_area[search_area['close'] > search_area['open']]
                if not up_candles.empty:
                    ob = up_candles.iloc[-1]
                    ob_high = ob['high']
                    ob_low = ob['low']
                    zone = {'type': PATTERN_ORDER_BLOCK, 'zone': (ob_high, ob_low)}
                    zones.append(zone)
                    self.log.debug(f"OB Baissier (retest) identifié: ({ob_high:.5f} - {ob_low:.5f})")

        return zones

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        df = ohlc_data.copy()

        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(connector, symbol)

        if symbol not in self._unmitigated_zones:
            self._unmitigated_zones[symbol] = {BUY: [], SELL: []}

        # --- [SMC-3] Réinitialiser état AMD si nouveau jour ---
        current_date = df.index[-1].date()
        if self._amd_state.get('date') != current_date:
            self._amd_state = {'date': current_date, 'manipulation_direction': None, 'asia_high': None, 'asia_low': None}

        # 1. Détecter manipulation AMD (si activé)
        if self.config.get('pattern_detection', {}).get(PATTERN_AMD, False):
            self._detect_amd_manipulation(df.copy(), symbol)

        # 2. Détecter les nouvelles zones (BOS + FVG/OB) - inclut logique AMD si manipulation détectée
        self._update_zones_on_bos(df.copy(), symbol)

        # 3. Vérifier si le prix actuel reteste une zone non mitigée
        confirmed_trade_signal = self._check_retest_of_zones(df, symbol)

        # 4. Filtrer par tendance
        if confirmed_trade_signal:
            name = confirmed_trade_signal['pattern']
            direction = confirmed_trade_signal['direction']

            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"RETEST CONFIRMÉ ({direction})"}
                return confirmed_trade_signal
            else:
                self.detected_patterns_info[name] = {'status': f"RETEST INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
                return None

        return None

    # --- [SMC-3] Nouvelle fonction pour détecter la manipulation AMD ---
    def _detect_amd_manipulation(self, df: pd.DataFrame, symbol: str):
        """
        Identifie le range Asiatique et détecte si une manipulation a eu lieu
        pendant la session de Londres. Met à jour _amd_state.
        """
        self.detected_patterns_info[PATTERN_AMD] = {'status': 'En attente'}
        asia_start, asia_end = time(0, 0), time(7, 0)
        london_open = time(8, 0)
        current_time_utc = df.index[-1].time()
        current_date = df.index[-1].date()

        # Ne chercher la manipulation que pendant Londres et si pas déjà détectée
        if not (london_open <= current_time_utc < time(16,0)) or self._amd_state.get('manipulation_direction'):
            return

        # Si les infos Asie ne sont pas déjà calculées pour aujourd'hui
        if self._amd_state.get('asia_high') is None:
            try:
                # S'assurer qu'on a les données du jour même
                asia_session_today = df.between_time(asia_start, asia_end)
                asia_session_today = asia_session_today[asia_session_today.index.date == current_date]

                if asia_session_today.empty:
                    self.detected_patterns_info[PATTERN_AMD] = {'status': 'Pas de données Asie'}
                    return
                
                self._amd_state['asia_high'] = asia_session_today['high'].max()
                self._amd_state['asia_low'] = asia_session_today['low'].min()
                self.log.debug(f"Range Asie du {current_date}: H={self._amd_state['asia_high']:.5f}, L={self._amd_state['asia_low']:.5f}")
            except Exception as e:
                self.log.error(f"Erreur calcul range Asie: {e}")
                self.detected_patterns_info[PATTERN_AMD] = {'status': 'Erreur Range Asie'}
                return


        # Vérifier la manipulation pendant Londres (jusqu'à la bougie actuelle)
        london_data = df.loc[df.index.date == current_date].between_time(london_open, current_time_utc)
        if london_data.empty: return

        asia_high = self._amd_state['asia_high']
        asia_low = self._amd_state['asia_low']

        if london_data['low'].min() < asia_low:
             self.log.debug("Manipulation détectée SOUS le range Asiatique.")
             self._amd_state['manipulation_direction'] = SELL # Manipulation baissière
             self.detected_patterns_info[PATTERN_AMD] = {'status': f'Manip. SOUS Asie L {asia_low:.5f}'}

        elif london_data['high'].max() > asia_high:
             self.log.debug("Manipulation détectée AU-DESSUS du range Asiatique.")
             self._amd_state['manipulation_direction'] = BUY # Manipulation haussière
             self.detected_patterns_info[PATTERN_AMD] = {'status': f'Manip. SUR Asie H {asia_high:.5f}'}


    def _update_zones_on_bos(self, df: pd.DataFrame, symbol: str):
        """
        Détecte un BOS (Break of Structure) et identifie les zones FVG et OB.
        Intègre la logique AMD : si une manipulation a eu lieu, la cible devient
        le high/low Asiatique opposé.
        """
        # (Fonction inchangée par rapport à V18.1.0 SAUF pour l'ajout de la cible AMD)
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'} # Reset status
        # ... (Reset autres status si besoin) ...

        if len(df) < 20: return

        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=5)

        if swing_highs.empty or swing_lows.empty:
             return

        current_close = df['close'].iloc[-1]
        self._unmitigated_zones[symbol][BUY] = [z for z in self._unmitigated_zones[symbol][BUY] if z['zone'][0] > current_close]
        self._unmitigated_zones[symbol][SELL] = [z for z in self._unmitigated_zones[symbol][SELL] if z['zone'][1] < current_close]


        # Détection BOS Haussier
        last_swing_high = swing_highs.iloc[-1]
        if df['high'].iloc[-1] > last_swing_high['high'] or df['high'].iloc[-2] > last_swing_high['high']:

            last_swing_high_price = last_swing_high['high']
            swing_low_before_bos = swing_lows[swing_lows.index < last_swing_high.name].iloc[-1] if not swing_lows[swing_lows.index < last_swing_high.name].empty else None

            if swing_low_before_bos is None: return

            self.log.debug(f"BOS Haussier détecté (Rupture de {last_swing_high_price:.5f}). Recherche de zones.")
            self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'BOS Haussier'}

            impulse_data = df[df.index >= swing_low_before_bos.name]

            new_ob_zones = self._identify_ob_zones(impulse_data, swing_low_before_bos, None, BUY)
            new_fvg_zones = self._identify_fvg_zones(impulse_data, BUY)

            # --- [SMC-3] Définir la cible (Standard ou AMD) ---
            target_price = last_swing_high_price # Cible standard
            is_amd_setup = False
            if self._amd_state.get('manipulation_direction') == SELL: # Si manip. baissière avant BOS haussier
                 target_price = self._amd_state.get('asia_high', target_price) # Cible = Haut Asie
                 is_amd_setup = True
                 self.log.debug(f"BOS Haussier correspond à un setup AMD. Cible: Haut Asie {target_price:.5f}")
            # --- Fin [SMC-3] ---

            # Marquer les zones et ajouter la cible
            all_new_zones = new_ob_zones + new_fvg_zones
            for z in all_new_zones:
                 z['target'] = target_price
                 if is_amd_setup: z['type'] = PATTERN_AMD # Remplacer type par AMD

            # Fusionner et dédupliquer
            self._unmitigated_zones[symbol][BUY].extend(all_new_zones)
            # Déduplication plus robuste (basée sur type et zone)
            seen_zones = set()
            unique_zones = []
            for z in self._unmitigated_zones[symbol][BUY]:
                 zone_tuple = tuple(z['zone'])
                 identifier = (z['type'], zone_tuple)
                 if identifier not in seen_zones:
                      unique_zones.append(z)
                      seen_zones.add(identifier)
            self._unmitigated_zones[symbol][BUY] = unique_zones


        # Détection BOS Baissier
        last_swing_low = swing_lows.iloc[-1]
        if df['low'].iloc[-1] < last_swing_low['low'] or df['low'].iloc[-2] < last_swing_low['low']:
            last_swing_low_price = last_swing_low['low']
            swing_high_before_bos = swing_highs[swing_highs.index < last_swing_low.name].iloc[-1] if not swing_highs[swing_highs.index < last_swing_low.name].empty else None

            if swing_high_before_bos is None: return

            self.log.debug(f"BOS Baissier détecté (Rupture de {last_swing_low_price:.5f}). Recherche de zones.")
            self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'BOS Baissier'}

            impulse_data = df[df.index >= swing_high_before_bos.name]

            new_ob_zones = self._identify_ob_zones(impulse_data, None, swing_high_before_bos, SELL)
            new_fvg_zones = self._identify_fvg_zones(impulse_data, SELL)

            # --- [SMC-3] Définir la cible (Standard ou AMD) ---
            target_price = last_swing_low_price # Cible standard
            is_amd_setup = False
            if self._amd_state.get('manipulation_direction') == BUY: # Si manip. haussière avant BOS baissier
                 target_price = self._amd_state.get('asia_low', target_price) # Cible = Bas Asie
                 is_amd_setup = True
                 self.log.debug(f"BOS Baissier correspond à un setup AMD. Cible: Bas Asie {target_price:.5f}")
            # --- Fin [SMC-3] ---

            all_new_zones = new_ob_zones + new_fvg_zones
            for z in all_new_zones:
                 z['target'] = target_price
                 if is_amd_setup: z['type'] = PATTERN_AMD

            self._unmitigated_zones[symbol][SELL].extend(all_new_zones)
            seen_zones = set()
            unique_zones = []
            for z in self._unmitigated_zones[symbol][SELL]:
                 zone_tuple = tuple(z['zone'])
                 identifier = (z['type'], zone_tuple)
                 if identifier not in seen_zones:
                      unique_zones.append(z)
                      seen_zones.add(identifier)
            self._unmitigated_zones[symbol][SELL] = unique_zones


    def _check_retest_of_zones(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        Vérifie si le prix actuel reteste une zone FVG ou OB non mitigée.
        """
        # (Fonction inchangée - cf. V18.1.0)
        current_high = df['high'].iloc[-1]
        current_low = df['low'].iloc[-1]

        zones_buy = self._unmitigated_zones[symbol][BUY]
        zones_buy.sort(key=lambda z: z['zone'][1], reverse=True)

        for i, zone in enumerate(zones_buy):
            zone_high, zone_low = zone['zone']
            if current_low <= zone_high:
                self.log.info(f"SIGNAL (Retest Achat {zone['type']}): Prix {current_low:.5f} a touché la zone ({zone_high:.5f} - {zone_low:.5f})")
                self._unmitigated_zones[symbol][BUY].pop(i) # Mitigée
                return {'pattern': zone['type'], 'direction': BUY, 'target_price': zone['target']}

        zones_sell = self._unmitigated_zones[symbol][SELL]
        zones_sell.sort(key=lambda z: z['zone'][0], reverse=False)

        for i, zone in enumerate(zones_sell):
            zone_high, zone_low = zone['zone']
            if current_high >= zone_low:
                self.log.info(f"SIGNAL (Retest Vente {zone['type']}): Prix {current_high:.5f} a touché la zone ({zone_high:.5f} - {zone_low:.5f})")
                self._unmitigated_zones[symbol][SELL].pop(i) # Mitigée
                return {'pattern': zone['type'], 'direction': SELL, 'target_price': zone['target']}

        return None

    # --- Liquidity Grab reste désactivé (logique non-retest) ---
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_LIQUIDITY_GRAB] = {'status': 'Désactivé (Logique Retest v18)'}
        return None
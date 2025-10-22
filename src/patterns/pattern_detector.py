# Fichier: src/patterns/pattern_detector.py
# Version: 18.0.0 (SMC-Retest-Logic)
# Dépendances: pandas, numpy, logging, datetime, src.constants
# DESCRIPTION: Correction critique de la logique SMC. Les signaux ne sont
#              plus générés à la création des zones, mais lors du retest
#              (pullback) dans ces zones après une rupture de structure (BOS/CHoCH).

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
    v18.0.0: Correction de la logique d'entrée. Détecte les zones (FVG/OB) après
    un BOS/CHoCH, et attend un retest dans ces zones pour générer un signal.
    """
    def __init__(self, config):
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self.detected_patterns_info = {}
        # Stockage des zones non mitigées (pour le retest)
        # Format: {'symbol': {'BUY': [{'fvg': (high, low), 'target': price}, ...], 'SELL': [...]}}
        self._unmitigated_zones = {}

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

    # --- [FIX-5] Détection de swing corrigée ---
    def _find_swing_points(self, df: pd.DataFrame, n: int = 3):
        """
        Détecte les swing points (fractals) en utilisant n=3 (fenêtre de 7 bougies).
        Utilise center=True, min_periods=n*2+1 pour éviter le lookahead bias
        et ne trouver que les swings historiques confirmés.
        """
        window_size = n * 2 + 1
        df.loc[:, 'is_swing_high'] = df['high'].rolling(window=window_size, center=True, min_periods=window_size).max() == df['high']
        df.loc[:, 'is_swing_low'] = df['low'].rolling(window=window_size, center=True, min_periods=window_size).min() == df['low']
        
        # Filtre pour ne garder que les swings confirmés
        swing_highs = df[df['is_swing_high']]
        swing_lows = df[df['is_swing_low']]
        
        return swing_highs, swing_lows
    
    # --- [FIX-5] Fonction helper pour identifier les FVG (Inbalance) ---
    def _identify_fvg_zones(self, df: pd.DataFrame, last_swing_high_price: float, last_swing_low_price: float, direction: str):
        """
        Identifie les FVG (Inbalance) créés lors du mouvement impulsif.
        Un FVG haussier est (low[-2] > high[-4]).
        Un FVG baissier est (high[-2] < low[-4]).
        """
        zones = []
        if direction == BUY:
            # Chercher FVG Haussier (Discount)
            # Bougie 1 (N-4), Bougie 2 (N-3), Bougie 3 (N-2)
            # On vérifie si Bougie 3 (N-2) a créé un FVG avec Bougie 1 (N-4)
            if len(df) > 5 and df['low'].iloc[-3] > df['high'].iloc[-5]:
                fvg_low = df['high'].iloc[-5]
                fvg_high = df['low'].iloc[-3]
                # S'assurer que le FVG est sous le swing high (dans la zone de discount/premium)
                if fvg_low < last_swing_high_price:
                    # La cible est le prochain swing high externe (le high qui a été cassé)
                    # Note: Le 'risk_manager' utilise ATR TP en fallback si RR < 1
                    zones.append({'fvg': (fvg_high, fvg_low), 'target': last_swing_high_price})
                    self.log.debug(f"FVG Haussier (retest) identifié: ({fvg_high:.5f} - {fvg_low:.5f})")

        elif direction == SELL:
            # Chercher FVG Baissier (Premium)
            if len(df) > 5 and df['high'].iloc[-3] < df['low'].iloc[-5]:
                fvg_low = df['low'].iloc[-3]
                fvg_high = df['high'].iloc[-5]
                if fvg_high > last_swing_low_price:
                    zones.append({'fvg': (fvg_high, fvg_low), 'target': last_swing_low_price})
                    self.log.debug(f"FVG Baissier (retest) identifié: ({fvg_high:.5f} - {fvg_low:.5f})")
        
        return zones

    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str):
        df = ohlc_data.copy()
        
        if not isinstance(df.index, pd.DatetimeIndex):
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')

        allowed_direction = self._get_trend_filter_direction(connector, symbol)
        
        # Initialiser les zones pour ce symbole si elles n'existent pas
        if symbol not in self._unmitigated_zones:
            self._unmitigated_zones[symbol] = {BUY: [], SELL: []}

        # 1. Détecter les nouvelles zones (BOS/CHoCH + FVG/OB)
        # Note: Nous utilisons une logique simplifiée de CHoCH comme détection de BOS/Impulsion
        # (La logique AMD et LG est désactivée pour cette révision car elles sont des entrées 'sur création')

        # --- [FIX-5] Révision Logique CHOCH (utilisée comme BOS/Impulsion) ---
        # Cette fonction va maintenant IDENTIFIER les zones, pas générer des trades.
        self._update_zones_on_choch(df.copy(), symbol)

        # 2. Vérifier si le prix actuel reteste une zone non mitigée
        confirmed_trade_signal = self._check_retest_of_zones(df, symbol)

        # 3. Filtrer par tendance (si un signal de retest a été trouvé)
        if confirmed_trade_signal:
            name = confirmed_trade_signal['pattern']
            direction = confirmed_trade_signal['direction']
            
            if allowed_direction == "ANY" or direction == allowed_direction:
                self.detected_patterns_info[name] = {'status': f"RETEST CONFIRMÉ ({direction})"}
                return confirmed_trade_signal
            else:
                self.detected_patterns_info[name] = {'status': f"RETEST INVALIDÉ ({direction} vs Tendance {allowed_direction})"}
                # Optionnel: vider les zones si elles sont contre-tendance?
                # self._unmitigated_zones[symbol][direction] = [] 
                return None
        
        return None

    def _update_zones_on_choch(self, df: pd.DataFrame, symbol: str):
        """
        Détecte un CHoCH (utilisé comme BOS) et, si trouvé,
        identifie les zones FVG créées pendant ce mouvement impulsif.
        """
        self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'Pas de signal'}
        if len(df) < 20: return

        # Utiliser n=5 pour des swings plus significatifs (externes)
        swing_highs, swing_lows = self._find_swing_points(df.copy(), n=5)
        
        # Nettoyer les zones invalidées (celles au-delà du prix actuel)
        current_close = df['close'].iloc[-1]
        self._unmitigated_zones[symbol][BUY] = [z for z in self._unmitigated_zones[symbol][BUY] if z['fvg'][0] > current_close]
        self._unmitigated_zones[symbol][SELL] = [z for z in self._unmitigated_zones[symbol][SELL] if z['fvg'][1] < current_close]


        # Détection BOS/CHoCH Haussier (Rupture d'un swing high précédent)
        if len(swing_highs) > 1 and df['high'].iloc[-1] > swing_highs['high'].iloc[-2]:
            last_valid_high_price = swing_highs['high'].iloc[-2]
            last_valid_low_price = swing_lows[swing_lows.index < swing_highs.index[-2]].iloc[-1]['low'] if not swing_lows[swing_lows.index < swing_highs.index[-2]].empty else 0
            
            self.log.debug(f"BOS Haussier détecté (Rupture de {last_valid_high_price:.5f}). Recherche de zones FVG.")
            self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'BOS Haussier'}
            
            # Identifier les FVG dans le mouvement qui a causé le BOS
            # (On regarde les données jusqu'à la rupture)
            impulse_data = df[df.index > (swing_lows[swing_lows.index < swing_highs.index[-2]].index[-1] if not swing_lows[swing_lows.index < swing_highs.index[-2]].empty else df.index[0])]
            new_zones = self._identify_fvg_zones(impulse_data, last_valid_high_price, last_valid_low_price, BUY)
            if new_zones:
                self._unmitigated_zones[symbol][BUY].extend(new_zones)

        # Détection BOS/CHoCH Baissier (Rupture d'un swing low précédent)
        if len(swing_lows) > 1 and df['low'].iloc[-1] < swing_lows['low'].iloc[-2]:
            last_valid_low_price = swing_lows['low'].iloc[-2]
            last_valid_high_price = swing_highs[swing_highs.index < swing_lows.index[-2]].iloc[-1]['high'] if not swing_highs[swing_highs.index < swing_lows.index[-2]].empty else 0

            self.log.debug(f"BOS Baissier détecté (Rupture de {last_valid_low_price:.5f}). Recherche de zones FVG.")
            self.detected_patterns_info[PATTERN_CHOCH] = {'status': 'BOS Baissier'}

            impulse_data = df[df.index > (swing_highs[swing_highs.index < swing_lows.index[-2]].index[-1] if not swing_highs[swing_highs.index < swing_lows.index[-2]].empty else df.index[0])]
            new_zones = self._identify_fvg_zones(impulse_data, last_valid_high_price, last_valid_low_price, SELL)
            if new_zones:
                self._unmitigated_zones[symbol][SELL].extend(new_zones)


    def _check_retest_of_zones(self, df: pd.DataFrame, symbol: str) -> dict:
        """
        Vérifie si le prix actuel (dernière bougie) entre en contact
        avec une zone FVG non mitigée.
        """
        current_high = df['high'].iloc[-1]
        current_low = df['low'].iloc[-1]
        
        # Vérifier Retest Achat (BUY)
        zones_buy = self._unmitigated_zones[symbol][BUY]
        for i, zone in enumerate(zones_buy):
            fvg_high, fvg_low = zone['fvg']
            # Si le low actuel touche ou pénètre le FVG (fvg_high)
            if current_low <= fvg_high:
                self.log.info(f"SIGNAL (Retest FVG Achat): Prix {current_low:.5f} a touché la zone FVG ({fvg_high:.5f} - {fvg_low:.5f})")
                # Supprimer la zone (elle est mitigée)
                self._unmitigated_zones[symbol][BUY].pop(i)
                return {'pattern': PATTERN_INBALANCE, 'direction': BUY, 'target_price': zone['target']}

        # Vérifier Retest Vente (SELL)
        zones_sell = self._unmitigated_zones[symbol][SELL]
        for i, zone in enumerate(zones_sell):
            fvg_high, fvg_low = zone['fvg']
            # Si le high actuel touche ou pénètre le FVG (fvg_low)
            if current_high >= fvg_low:
                self.log.info(f"SIGNAL (Retest FVG Vente): Prix {current_high:.5f} a touché la zone FVG ({fvg_high:.5f} - {fvg_low:.5f})")
                # Supprimer la zone (elle est mitigée)
                self._unmitigated_zones[symbol][SELL].pop(i)
                return {'pattern': PATTERN_INBALANCE, 'direction': SELL, 'target_price': zone['target']}

        return None

    # --- Les logiques OB, LG, AMD (entrées directes) sont désactivées pour cette correction ---
    # --- car elles étaient la source des mauvais trades (entrées non-retest) ---

    def _detect_order_block(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_ORDER_BLOCK] = {'status': 'Désactivé (Logique Retest v18)'}
        return None
        
    def _detect_inbalance(self, df: pd.DataFrame):
        # Cette fonction est maintenant implicitement gérée par _update_zones_on_choch
        self.detected_patterns_info[PATTERN_INBALANCE] = {'status': 'Logique Retest v18'}
        return None
        
    def _detect_liquidity_grab(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_LIQUIDITY_GRAB] = {'status': 'Désactivé (Logique Retest v18)'}
        return None

    def _detect_amd_session(self, df: pd.DataFrame):
        self.detected_patterns_info[PATTERN_AMD] = {'status': 'Désactivé (Logique Retest v18)'}
        return None
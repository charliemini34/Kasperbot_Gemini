# Fichier: src/patterns/pattern_detector.py
# Version: 19.0.3 (Patch-Digits-FutureWarning)
# Dépendances: MetaTrader5, pandas, numpy, logging
# Description: Correction AttributeError (df.digits) et FutureWarning (fillna/downcasting).

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from src.constants import * # Importer les constantes (ex: PREMIUM_THRESHOLD)

# Helper pour l'analyse de structure (swing points)
# (Inclus ici pour la portabilité, pourrait être dans un module 'utils')
def get_swing_points(df, lookback=10):
    """
    Identifie les points de swing High et Low basés sur un lookback simple.
    Un high est plus haut que 'lookback' bougies avant/après.
    Un low est plus bas que 'lookback' bougies avant/après.
    (Utilise 'shift' pour une identification non-répulsive simple)
    """
    df['is_swing_high'] = (df['high'] > df['high'].shift(i) for i in range(1, lookback + 1)) & \
                           (df['high'] > df['high'].shift(-i) for i in range(1, lookback + 1))
    df['is_swing_low'] = (df['low'] < df['low'].shift(i) for i in range(1, lookback + 1)) & \
                          (df['low'] < df['low'].shift(-i) for i in range(1, lookback + 1))

    # Simplification pour l'exemple (une méthode plus robuste utiliserait zigzag ou fractales)
    # Cette méthode simple est peu fiable. Utilisons une méthode rolling.
    
    n = lookback # Fenêtre de chaque côté
    
    # ATTENTION: center=True utilise les données futures (Lookahead Bias)
    # Ne pas utiliser pour le trading réel ou backtest valide.
    # Laissé ici pour compatibilité avec l'exemple, mais _get_market_structure est corrigé.
    df['is_swing_high'] = (df['high'] == df['high'].rolling(window=2*n+1, center=True).max())
    df['is_swing_low'] = (df['low'] == df['low'].rolling(window=2*n+1, center=True).min())

    # Filtrer les swings consécutifs
    # (Logique complexe omise pour cet exemple, supposons que 'get_structure' gère cela)

    return df


class PatternDetector:
    """
    Détecte les patterns de trading SMC (Smart Money Concepts)
    basés sur les données OHLC fournies.
    """

    def __init__(self, config: dict):
        self.config = config
        self.pattern_settings = config.get('pattern_settings', {})
        self.use_trend_filter = config.get('trend_filter', {}).get('enabled', True)
        self.ema_period = config.get('trend_filter', {}).get('ema_period', 200)
        self.htf_timeframe = config.get('trend_filter', {}).get('higher_timeframe', 'H4')

        # Paramètres SMC spécifiques
        self.fvg_imbalance_ratio = self.pattern_settings.get('fvg_imbalance_ratio', 0.5) # Ratio min FVG
        self.ob_wick_ratio = self.pattern_settings.get('ob_wick_ratio', 0.3) # Ratio max mèche OB
        self.swing_lookback = self.pattern_settings.get('swing_lookback_periods', 10)
        
        # --- CORRECTION (NameError) ---
        # Chargement du seuil Premium/Discount depuis la config, avec 0.5 (50%) par défaut.
        self.premium_threshold = self.pattern_settings.get('premium_threshold', 0.5)
        # --- FIN CORRECTION ---


        # Stockage des patterns détectés (pour l'API)
        self.detected_patterns_info = {}

    def get_detected_patterns_info(self) -> dict:
        """Retourne les infos sur les patterns détectés pour l'API."""
        return self.detected_patterns_info


    def detect_patterns(self, ohlc_data: pd.DataFrame, connector, symbol: str) -> dict:
        """
        Orchestre la détection de tous les patterns SMC configurés.
        Retourne un dictionnaire 'trade_signal' si un setup valide est trouvé,
        sinon None.
        """
        if ohlc_data is None or len(ohlc_data) < 50: # Besoin de données suffisantes
             logging.warning(f"Données insuffisantes pour la détection SMC sur {symbol}.")
             return None

        # --- 0. Nettoyage et initialisation ---
        self.detected_patterns_info = {} # Reset pour ce symbole
        df = ohlc_data.copy()

        # --- 1. Filtre de Tendance (HTF EMA) ---
        main_trend = "NEUTRAL"
        if self.use_trend_filter:
            htf_data = connector.get_ohlc(symbol, self.htf_timeframe, self.ema_period + 50)
            if htf_data is not None and not htf_data.empty:
                htf_data[f'ema_{self.ema_period}'] = htf_data['close'].ewm(span=self.ema_period, adjust=False).mean()
                last_close_htf = htf_data['close'].iloc[-1]
                last_ema_htf = htf_data[f'ema_{self.ema_period}'].iloc[-1]
                
                if last_close_htf > last_ema_htf: main_trend = "BULLISH"
                elif last_close_htf < last_ema_htf: main_trend = "BEARISH"
                self.detected_patterns_info['main_trend'] = f"{main_trend} (HTF {self.htf_timeframe} EMA {self.ema_period})"
            else:
                 logging.warning(f"Impossible de charger les données HTF {self.htf_timeframe} pour le filtre de tendance.")
                 # Option: Ne pas trader si filtre HTF échoue ? (Prudence)
                 # return None 
                 self.detected_patterns_info['main_trend'] = "ERREUR HTF"


        # --- 2. Identification de la Structure (Swings, BOS, CHoCH) ---
        # (Logique simplifiée pour l'exemple. Une vraie détection SMC
        # nécessiterait une identification robuste des 'protected' et 'targeted' highs/lows)
        
        # Supposons que nous avons une fonction (ou que nous l'implémentons)
        # qui identifie la structure récente:
        structure = self._get_market_structure(df)
        self.detected_patterns_info['structure'] = structure # ex: {"last_swing_high": 1.2500, "last_swing_low": 1.2300, "internal_structure": "BULLISH"}

        # --- 3. Détection des Zones d'Intérêt (POI) ---
        # (OB, FVG) dans les zones Premium/Discount
        premium_discount = self._get_premium_discount_zones(structure)
        self.detected_patterns_info.update(premium_discount) # ex: {"premium_zone_start": 1.2450, "discount_zone_start": 1.2350}

        order_blocks = self._detect_order_block(df, structure, premium_discount)
        fvgs = self._detect_fvg(df, structure, premium_discount)
        
        self.detected_patterns_info['pois'] = {"order_blocks": order_blocks, "fvgs": fvgs}

        # --- 4. Détection des Triggers d'Entrée (CHoCH, Liquidité, etc.) ---
        # C'est la logique principale: Avons-nous un signal MAINTENANT ?
        
        # Scénario 1: Pullback vers un OB/FVG en zone Discount (pour Achat)
        if main_trend in ["BULLISH", "NEUTRAL"] and self.pattern_settings.get('enable_ob_pullback', True):
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "BUY", structure)
            if signal:
                # Valider que le signal est aligné sur la tendance (si filtre activé)
                if self.use_trend_filter and main_trend == "BEARISH":
                     logging.debug(f"Signal Achat {symbol} ignoré (Filtre Tendance Baissier).")
                     return None
                logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                return signal

        # Scénario 2: Pullback vers un OB/FVG en zone Premium (pour Vente)
        if main_trend in ["BEARISH", "NEUTRAL"] and self.pattern_settings.get('enable_ob_pullback', True):
            signal = self._check_pullback_to_poi(df, order_blocks, fvgs, "SELL", structure)
            if signal:
                 if self.use_trend_filter and main_trend == "BULLISH":
                     logging.debug(f"Signal Vente {symbol} ignoré (Filtre Tendance Haussier).")
                     return None
                 logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                 return signal

        # Scénario 3: CHoCH (Change of Character)
        if self.pattern_settings.get('enable_choch', True):
            signal = self._detect_choch(df, structure, main_trend)
            if signal:
                 # Le filtre de tendance s'applique différemment au CHoCH
                 # (un CHoCH baissier est OK dans une tendance haussière, c'est un retracement)
                 # Sauf si on ne trade que les CHoCH pro-tendance (config ?)
                 logging.info(f"Signal détecté sur {symbol}: {signal['pattern']}")
                 return signal

        # (Ajouter d'autres triggers: Liquidity Grab, etc.)
        
        logging.debug(f"Aucun signal SMC valide trouvé pour {symbol} (Tendance: {main_trend})")
        return None


    def _get_market_structure(self, df: pd.DataFrame) -> dict:
        """
        [DEFINITION SMC]
        Identifie la structure de marché (interne et externe/swing).
        - Swing High/Low: Points extrêmes (utilisant `swing_lookback`).
        - BOS (Break of Structure): Cassure d'un 'protected' swing high/low 
          dans la direction de la tendance.
        - CHoCH (Change of Character): Cassure d'un 'internal' swing high/low 
          dans la direction opposée à la tendance (premier signe de retournement).
        
        Cette fonction (v19) est une simplification. Elle retourne juste
        les swings les plus récents pour le calcul P/D.
        """
        
        n = self.swing_lookback

        # --- CORRECTION (Lookahead Bias) ---
        # L'ancienne méthode (center=True) utilisait les données futures.
        # Cette nouvelle méthode est causale (n'utilise que le passé).

        # 1. Calculer les swings "parfaits" (non-causals, utilisent le futur)
        rolling_max = df['high'].rolling(window=2*n+1, center=True, min_periods=n//2).max()
        rolling_min = df['low'].rolling(window=2*n+1, center=True, min_periods=n//2).min()
        
        is_swing_high_non_causal = (df['high'] == rolling_max)
        is_swing_low_non_causal = (df['low'] == rolling_min)

        # 2. Rendre le signal causal en le décalant de 'n'
        # Un swing qui se produit à 'T' n'est confirmé (connu) qu'à 'T+n'.
        # Donc, à l'instant 'T', nous regardons si un swing a été confirmé à 'T-n'.
        df['is_swing_high'] = is_swing_high_non_causal.shift(n)
        df['is_swing_low'] = is_swing_low_non_causal.shift(n)
        
        # 3. Remplir les NaN créés par le décalage (nécessaire pour .tail())
        # --- PATCH v19.0.3: Ajout de .infer_objects(copy=False) pour corriger FutureWarning ---
        df['is_swing_high'] = df['is_swing_high'].fillna(False).infer_objects(copy=False)
        df['is_swing_low'] = df['is_swing_low'].fillna(False).infer_objects(copy=False)
        # --- FIN PATCH ---
        
        recent_swings_high = df[df['is_swing_high']]['high'].tail(5).to_list()
        recent_swings_low = df[df['is_swing_low']]['low'].tail(5).to_list()

        # (Logique de détermination BOS/CHoCH omise ici, gérée dans _detect_choch)
        
        structure_info = {
            "last_swing_high": recent_swings_high[-1] if recent_swings_high else df['high'].iloc[-1],
            "last_swing_low": recent_swings_low[-1] if recent_swings_low else df['low'].iloc[-1],
            "internal_structure": "UNKNOWN" # Nécessite une logique plus complexe
        }
        return structure_info


    def _get_premium_discount_zones(self, structure: dict) -> dict:
        """
        [DEFINITION SMC]
        Calcule les zones Premium (chères, > 50%) et Discount (bon marché, < 50%)
        basées sur le dernier "leg" de prix (entre last_swing_low et last_swing_high).
        - Zone Premium: propice aux ventes.
        - Zone Discount: propice aux achats.
        """
        low = structure.get('last_swing_low', 0.0)
        high = structure.get('last_swing_high', 0.0)
        
        if low == 0.0 or high == 0.0 or high <= low:
            return {"equilibrium": 0.0, "premium_start": 0.0, "discount_start": 0.0}
            
        equilibrium = low + (high - low) * 0.5
        
        # Définir les seuils stricts (optionnel, ex: 62% Fibo)
        
        # --- CORRECTION (NameError) ---
        # Utilisation de la variable chargée depuis la config (self)
        premium_threshold = self.premium_threshold # ex: 0.5
        # --- FIN CORRECTION ---
        
        discount_threshold = 1.0 - premium_threshold # ex: 0.5
        
        premium_start = low + (high - low) * premium_threshold
        discount_start = low + (high - low) * discount_threshold # (ou high - (high-low)*premium)

        return {
            "equilibrium": premium_start, # 0.5
            "premium_start": premium_start, # Zone de vente commence ici
            "discount_start": discount_start # Zone d'achat commence ici (en dessous)
        }


    def _detect_fvg(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        """
        [DEFINITION SMC]
        Détecte les FVG (Fair Value Gaps) / Imbalances.
        Un FVG est un écart de prix entre le high de la bougie N-1 et
        le low de la bougie N+1 (pour un FVG haussier créé par N) ou
        le low de N-1 et le high de N+1 (pour un FVG baissier créé par N).
        La bougie N doit être impulsive (corps large).
        """
        fvgs = []
        # Boucle sur les X dernières bougies (ex: 50)
        for i in range(len(df) - 50, len(df) - 1):
            if i < 1: continue # Besoin de N-1 et N+1

            try:
                prev_high = df['high'].iloc[i-1]
                curr_high = df['high'].iloc[i]
                curr_low = df['low'].iloc[i]
                next_low = df['low'].iloc[i+1]
                
                prev_low = df['low'].iloc[i-1]
                next_high = df['high'].iloc[i+1]

                is_bullish_candle = df['close'].iloc[i] > df['open'].iloc[i]
                is_bearish_candle = df['close'].iloc[i] < df['open'].iloc[i]
                
                # FVG Haussier (Gap entre prev_high et next_low)
                if is_bullish_candle and curr_high > prev_high and next_low > prev_high:
                     # Le FVG est la zone [prev_high, next_low]
                     # (ou [prev_high, curr_low] si N+1 n'a pas atteint N)
                     # Définition standard: FVG = [prev_high, next_low]
                     fvg_top = next_low
                     fvg_bottom = prev_high
                     # Vérifier si FVG est "assez grand" (ex: ratio du FVG / bougie N)
                     # (Logique omise pour simplicité)
                     
                     # Est-il en zone Discount ?
                     if fvg_bottom < pd_zones.get('discount_start', 0.0):
                         fvgs.append({"type": "BULLISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i})

                # FVG Baissier (Gap entre prev_low et next_high)
                if is_bearish_candle and curr_low < prev_low and next_high < prev_low:
                     fvg_top = prev_low
                     fvg_bottom = next_high
                     
                     # Est-il en zone Premium ?
                     if fvg_top > pd_zones.get('premium_start', 0.0):
                         fvgs.append({"type": "BEARISH", "top": fvg_top, "bottom": fvg_bottom, "candle_index": i})
                         
            except IndexError:
                 break # Fin du dataframe
            except Exception as e:
                 logging.warning(f"Erreur détection FVG index {i}: {e}")
                 
        return fvgs


    def _detect_order_block(self, df: pd.DataFrame, structure: dict, pd_zones: dict) -> list:
        """
        [DEFINITION SMC]
        Détecte les Order Blocks (OB).
        - OB Haussier: La dernière bougie baissière avant une forte 
          impulsion haussière (qui casse la structure ou crée un FVG).
          Doit être en zone Discount.
        - OB Baissier: La dernière bougie haussière avant une forte
          impulsion baissière (qui casse la structure ou crée un FVG).
          Doit être en zone Premium.
        
        Filtre (optionnel): Le corps doit être > X% de la bougie (wick_ratio).
        """
        order_blocks = []
        # Boucle sur les X dernières bougies (ex: 50)
        for i in range(len(df) - 50, len(df) - 2): # Besoin de N et N+1 (impulsion)
            try:
                candle_n = df.iloc[i]
                candle_n_plus_1 = df.iloc[i+1]
                
                is_n_bullish = candle_n['close'] > candle_n['open']
                is_n_bearish = candle_n['close'] < candle_n['open']
                is_n1_bullish = candle_n_plus_1['close'] > candle_n_plus_1['open']
                is_n1_bearish = candle_n_plus_1['close'] < candle_n_plus_1['open']
                
                # 1. OB Baissier (Cherche bougie N haussière, N+1 impulsion baissière)
                if is_n_bullish and is_n1_bearish:
                    # N+1 est-elle une impulsion ? (ex: casse le low de N)
                    if candle_n_plus_1['close'] < candle_n['low']:
                        # N est un OB Baissier potentiel. Zone = [low, high] de N
                        ob_top = candle_n['high']
                        ob_bottom = candle_n['low']
                        # (On pourrait utiliser 'open' ou 'close' selon la définition)
                        
                        # Est-il en zone Premium ?
                        if ob_bottom > pd_zones.get('premium_start', 0.0):
                             # (Vérifier si FVG créé? Vérifier si BOS créé? - Logique avancée)
                             order_blocks.append({"type": "BEARISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i})

                # 2. OB Haussier (Cherche bougie N baissière, N+1 impulsion haussière)
                if is_n_bearish and is_n1_bullish:
                     # N+1 est-elle une impulsion ? (ex: casse le high de N)
                     if candle_n_plus_1['close'] > candle_n['high']:
                         ob_top = candle_n['high']
                         ob_bottom = candle_n['low']
                         
                         # Est-il en zone Discount ?
                         if ob_top < pd_zones.get('discount_start', 0.0):
                             order_blocks.append({"type": "BULLISH", "top": ob_top, "bottom": ob_bottom, "candle_index": i})

            except IndexError:
                 break
            except Exception as e:
                 logging.warning(f"Erreur détection OB index {i}: {e}")
                 
        return order_blocks


    def _detect_choch(self, df: pd.DataFrame, structure: dict, main_trend: str) -> dict:
        """
        [DEFINITION SMC]
        Détecte un Change of Character (CHoCH) récent.
        - CHoCH Haussier: La tendance interne était baissière (Lower Lows/Lower Highs)
          et le prix vient de casser le dernier 'internal' Lower High.
        - CHoCH Baissier: La tendance interne était haussière (Higher Highs/Higher Lows)
          et le prix vient de casser le dernier 'internal' Higher Low.
        
        Cette détection (v19) est simplifiée:
        Regarde si la dernière bougie clôture au-dessus du dernier swing high (pour Sell)
        ou en dessous du dernier swing low (pour Buy), indiquant un potentiel
        retournement contre la structure immédiate.
        """
        
        last_candle = df.iloc[-1]
        
        # Simplification extrême:
        # On cherche un CHoCH Baissier (signal VENTE)
        # Si la tendance récente était haussière (HH/HL)
        # Et qu'on casse le dernier HL
        # (Nécessite identification HL, ex: structure['last_internal_low'])
        last_internal_low = structure.get('last_swing_low') # Utilise swing low (simplification)
        
        if last_candle['close'] < last_internal_low:
             # CHoCH Baissier (Signal VENTE)
             logging.debug(f"CHoCH Baissier détecté {df.index[-1]}: Clôture {last_candle['close']} < Dernier Low {last_internal_low}")
             
             # Où est le SL/TP ?
             # SL: Au-dessus du High qui a causé la casse (ex: last_swing_high)
             # TP: Prochaine zone de liquidité (ex: external low)
             return {
                 "pattern": "CHANGE_OF_CHARACTER (Bearish)",
                 "direction": "SELL",
                 "sl_price": structure.get('last_swing_high', last_candle['high'] + (last_candle['high']-last_candle['low'])), # Fallback
                 "tp_price": 0.0 # Doit être calculé (ex: ATR ou external low)
             }

        # CHoCH Haussier (Signal ACHAT)
        last_internal_high = structure.get('last_swing_high')
        
        if last_candle['close'] > last_internal_high:
             logging.debug(f"CHoCH Haussier détecté {df.index[-1]}: Clôture {last_candle['close']} > Dernier High {last_internal_high}")
             
             return {
                 "pattern": "CHANGE_OF_CHARACTER (Bullish)",
                 "direction": "BUY",
                 "sl_price": structure.get('last_swing_low', last_candle['low'] - (last_candle['high']-last_candle['low'])), # Fallback
                 "tp_price": 0.0 # Doit être calculé
             }
             
        return None


    def _check_pullback_to_poi(self, df: pd.DataFrame, order_blocks: list, fvgs: list, direction: str, structure: dict) -> dict:
        """
        [DEFINITION SMC]
        Vérifie si le prix actuel (dernière bougie) est entré en contact
        avec une POI (OB ou FVG) valide pour la direction donnée.
        
        Ex: Direction "BUY" -> Cherche contact avec OB Haussier ou FVG Haussier
            (qui sont en zone Discount).
        """
        
        last_candle = df.iloc[-1]
        last_low = last_candle['low']
        last_high = last_candle['high']
        
        # 1. Recherche Achat (contact POI Haussière en Discount)
        if direction == "BUY":
            pois = [p for p in order_blocks if p['type'] == "BULLISH"] + \
                   [p for p in fvgs if p['type'] == "BULLISH"]
            if not pois: return None
            
            # Trier les POI (la plus haute en premier, car on vient d'en haut)
            pois.sort(key=lambda x: x['top'], reverse=True)
            
            for poi in pois:
                poi_top = poi['top']
                poi_bottom = poi['bottom']
                
                # Le prix (last_low) a-t-il touché la POI ?
                if last_low <= poi_top:
                     # Oui, contact.
                     # --- PATCH v19.0.3: Correction AttributeError (df.digits) ---
                     logging.debug(f"Contact POI Achat {poi['type']} @ {poi_top} (Index {poi['candle_index']})")
                     # --- FIN PATCH ---
                     
                     # SL: En dessous du bas de la POI (ou structure)
                     # TP: Prochaine liquidité (ex: last_swing_high)
                     sl_price = poi_bottom # SL basé sur la POI
                     tp_price = structure.get('last_swing_high', 0.0) # Target
                     
                     return {
                         "pattern": f"POI_PULLBACK ({poi['type']} Bullish)",
                         "direction": "BUY",
                         "sl_price": sl_price,
                         "tp_price": tp_price
                     }

        # 2. Recherche Vente (contact POI Baissière en Premium)
        if direction == "SELL":
            pois = [p for p in order_blocks if p['type'] == "BEARISH"] + \
                   [p for p in fvgs if p['type'] == "BEARISH"]
            if not pois: return None

            # Trier les POI (la plus basse en premier)
            pois.sort(key=lambda x: x['bottom'], reverse=False)

            for poi in pois:
                poi_top = poi['top']
                poi_bottom = poi['bottom']
                
                # Le prix (last_high) a-t-il touché la POI ?
                if last_high >= poi_bottom:
                     # --- PATCH v19.0.3: Correction AttributeError (df.digits) ---
                     logging.debug(f"Contact POI Vente {poi['type']} @ {poi_bottom} (Index {poi['candle_index']})")
                     # --- FIN PATCH ---
                     
                     sl_price = poi_top # SL basé sur la POI
                     tp_price = structure.get('last_swing_low', 0.0) # Target

                     return {
                         "pattern": f"POI_PULLBACK ({poi['type']} Bearish)",
                         "direction": "SELL",
                         "sl_price": sl_price,
                         "tp_price": tp_price
                     }

        return None
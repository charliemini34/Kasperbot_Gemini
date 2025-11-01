# src/analysis/market_structure.py

import pandas as pd
import numpy as np
from scipy.signal import argrelextrema

class MarketStructure:
    def __init__(self, config):
        self.config = config
        self.htf_order = config.get('htf_swing_order', 5)  # Ordre pour les swings HTF (plus larges)
        self.ltf_order = config.get('ltf_swing_order', 3)  # Ordre pour les swings LTF (plus fins)

    def _find_swing_points(self, df, order):
        """
        Trouve les points de swing (highs et lows) dans un DataFrame.
        Utilise argrelextrema pour une détection efficace.
        'order' détermine le nombre de bougies de chaque côté pour définir un swing.
        """
        # Ajout de padding pour détecter les swings aux extrémités
        n = order
        
        # Trouver les indices des extrema
        high_indices = argrelextrema(df['high'].values, np.greater, order=order)[0]
        low_indices = argrelextrema(df['low'].values, np.less, order=order)[0]
        
        # Filtrer pour s'assurer qu'ils sont valides
        high_indices = [i for i in high_indices if i >= order and i < len(df) - order]
        low_indices = [i for i in low_indices if i >= order and i < len(df) - order]

        # Stocker les swings sous forme de liste de tuples (index, prix)
        swing_highs = [(df.index[i], df['high'].iloc[i]) for i in high_indices]
        swing_lows = [(df.index[i], df['low'].iloc[i]) for i in low_indices]
        
        # Trier par index (temps) pour s'assurer de l'ordre chronologique
        swing_highs.sort(key=lambda x: x[0])
        swing_lows.sort(key=lambda x: x[0])

        return swing_highs, swing_lows

    def _get_market_bias(self, swing_highs, swing_lows):
        """
        Détermine le biais du marché (tendance) en se basant sur les 2 derniers swing highs et lows.
        """
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "RANGING"  # Pas assez de points pour déterminer une tendance

        # Récupérer les deux derniers swings de chaque type
        last_high = swing_highs[-1][1]
        prev_high = swing_highs[-2][1]
        
        last_low = swing_lows[-1][1]
        prev_low = swing_lows[-2][1]

        # Logique SMC : Higher Highs (HH) et Higher Lows (HL)
        if last_high > prev_high and last_low > prev_low:
            return "BULLISH"
        # Logique SMC : Lower Lows (LL) et Lower Highs (LH)
        elif last_low < prev_low and last_high < prev_high:
            return "BEARISH"
        # Autres cas (ex: HH et LL, ou LH et HL) sont des ranges ou des retournements
        else:
            return "RANGING"

    def _identify_structure_breaks(self, df, swing_highs, swing_lows, bias):
        """
        Identifie les événements BOS (Break of Structure) et CHoCH (Change of Character)
        en se basant sur le dernier prix de clôture et le biais actuel.
        """
        bos_event = None
        choch_event = None
        
        if not swing_highs or not swing_lows:
            return bos_event, choch_event  # Ne peut rien faire sans swings

        last_close = df['close'].iloc[-1]
        last_high = swing_highs[-1][1]  # Le plus récent Lower High (LH) en tendance baissière
        last_low = swing_lows[-1][1]    # Le plus récent Higher Low (HL) en tendance haussière

        if bias == "BULLISH":
            # Un BOS est une cassure du dernier High (continuation)
            if last_close > last_high:
                bos_event = "BULLISH_BOS"
            # Un CHoCH est une cassure du dernier Low (retournement)
            elif last_close < last_low:
                choch_event = "BEARISH_CHOCH"
                
        elif bias == "BEARISH":
            # Un BOS est une cassure du dernier Low (continuation)
            if last_close < last_low:
                bos_event = "BEARISH_BOS"
            # Un CHoCH est une cassure du dernier High (retournement)
            elif last_close > last_high:
                choch_event = "BULLISH_CHOCH"
        
        # Si en RANGING, une cassure d'un côté ou de l'autre peut être un CHoCH
        elif bias == "RANGING":
            if last_close > last_high:
                choch_event = "BULLISH_CHOCH"
            elif last_close < last_low:
                choch_event = "BEARISH_CHOCH"

        return bos_event, choch_event

    def analyze(self, htf_data, ltf_data):
        """
        Analyse la structure de marché multi-timeframe (HTF/LTF).
        C'est la méthode principale appelée par l'orchestrateur.
        
        'htf_data' est le DataFrame pour le High Timeframe (Biais).
        'ltf_data' est le DataFrame pour le Low Timeframe (Structure d'entrée).
        """
        
        # 1. Analyse du HTF (High Timeframe) pour le BIAIS
        htf_swing_highs, htf_swing_lows = self._find_swing_points(htf_data, order=self.htf_order)
        market_bias = self._get_market_bias(htf_swing_highs, htf_swing_lows)

        # 2. Analyse du LTF (Low Timeframe) pour la STRUCTURE
        ltf_swing_highs, ltf_swing_lows = self._find_swing_points(ltf_data, order=self.ltf_order)
        
        # 3. Identification des événements (BOS/CHOCH) sur le LTF, en utilisant le BIAIS du HTF
        bos_event, choch_event = self._identify_structure_breaks(
            ltf_data, 
            ltf_swing_highs, 
            ltf_swing_lows, 
            market_bias
        )

        print(f"Analyse Structure: Biais HTF = {market_bias}, Événement LTF_BOS = {bos_event}, Événement LTF_CHOCH = {choch_event}")

        # 4. Retourne l'analyse complète
        structure_analysis = {
            "bias": market_bias,  # "BULLISH", "BEARISH", "RANGING"
            "ltf_structure": {
                "bos": bos_event,
                "choch": choch_event
            },
            "htf_swings": {
                "highs": htf_swing_highs,
                "lows": htf_swing_lows
            },
            "ltf_swings": {
                "highs": ltf_swing_highs,
                "lows": ltf_swing_lows
            }
        }
        
        return structure_analysis

    # D'autres méthodes peuvent être ajoutées pour des analyses spécifiques
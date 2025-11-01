# src/patterns/pattern_detector.py

import pandas as pd
import numpy as np

class PatternDetector:
    def __init__(self, config):
        self.config = config
        self.eql_threshold_pct = config.get('eql_threshold_pct', 0.0005)  # 0.05% de tolérance
        self.ob_imbalance_check_candles = config.get('ob_imbalance_check_candles', 5)
        self.ob_structure_check_candles = config.get('ob_structure_check_candles', 10)

    def _find_imbalances(self, df):
        """
        Détecte les Imbalances (Fair Value Gaps - FVG) dans le DataFrame.
        Un FVG est un "trou" de prix entre 3 bougies.
        """
        fvgs = []
        # Nous avons besoin d'au moins 3 bougies (indices i, i+1, i+2)
        for i in range(len(df) - 2):
            candle1_high = df['high'].iloc[i]
            candle3_low = df['low'].iloc[i+2]
            
            candle1_low = df['low'].iloc[i]
            candle3_high = df['high'].iloc[i+2]
            
            timestamp = df.index[i+1] # Le FVG est associé à la 2ème bougie

            # Bullish FVG (Gap entre la mèche haute de la 1ère et basse de la 3ème)
            if candle1_high < candle3_low:
                fvg = {
                    "timestamp": timestamp,
                    "type": "BULLISH_FVG",
                    "top": candle3_low,
                    "bottom": candle1_high,
                    "mitigated": False, # Sera vérifié plus tard
                }
                fvgs.append(fvg)

            # Bearish FVG (Gap entre la mèche basse de la 1ère et haute de la 3ème)
            elif candle1_low > candle3_high:
                fvg = {
                    "timestamp": timestamp,
                    "type": "BEARISH_FVG",
                    "top": candle1_low,
                    "bottom": candle3_high,
                    "mitigated": False, # Sera vérifié plus tard
                }
                fvgs.append(fvg)
        
        # Vérification de la mitigation (simple)
        # Un FVG est mitigé si le prix futur y est revenu
        for i, fvg in enumerate(fvgs):
            future_df = df[df.index > fvg['timestamp']]
            if not future_df.empty:
                if fvg['type'] == 'BULLISH_FVG':
                    # Mitigé si le low futur a touché le FVG
                    if future_df['low'].min() <= fvg['top']:
                        fvgs[i]['mitigated'] = True
                elif fvg['type'] == 'BEARISH_FVG':
                    # Mitigé si le high futur a touché le FVG
                    if future_df['high'].max() >= fvg['bottom']:
                        fvgs[i]['mitigated'] = True

        return fvgs

    def _find_liquidity(self, ltf_swings):
        """
        Identifie la liquidité (Equal Highs/Lows) à partir des points de swing.
        Utilise 'ltf_swings' de l'analyse de structure.
        """
        liquidity_zones = {"eqh": [], "eql": []}
        
        # Trouver Equal Highs (EQH)
        highs = ltf_swings['highs']
        for i in range(len(highs) - 1):
            price1 = highs[i][1]
            price2 = highs[i+1][1]
            
            # Si les prix sont très proches (ex: 0.05%)
            if abs(price1 - price2) <= price1 * self.eql_threshold_pct:
                zone = min(price1, price2)
                if zone not in liquidity_zones['eqh']:
                    liquidity_zones['eqh'].append(zone)

        # Trouver Equal Lows (EQL)
        lows = ltf_swings['lows']
        for i in range(len(lows) - 1):
            price1 = lows[i][1]
            price2 = lows[i+1][1]
            
            if abs(price1 - price2) <= price1 * self.eql_threshold_pct:
                zone = max(price1, price2)
                if zone not in liquidity_zones['eql']:
                    liquidity_zones['eql'].append(zone)
                    
        return liquidity_zones

    def _find_order_blocks(self, df, ltf_swings, fvgs):
        """
        Détecte les Order Blocks (OB) valides.
        Un OB est valide s'il :
        1. Est la bonne bougie (dernière inverse).
        2. Le mouvement suivant crée une Imbalance (FVG).
        3. Le mouvement suivant casse la structure (BOS/CHOCH, ici simplifié par "casse le dernier swing").
        4. N'est pas encore mitigé.
        """
        valid_obs = []
        
        # Nous avons besoin d'au moins N bougies pour l'analyse
        if len(df) < self.ob_structure_check_candles:
            return []

        for i in range(1, len(df) - self.ob_structure_check_candles):
            ob_candidate = None
            ob_type = None

            # 1. Trouver un candidat Bullish OB (dernière bougie baissière)
            if df['close'].iloc[i] < df['open'].iloc[i] and df['close'].iloc[i+1] > df['open'].iloc[i+1]:
                # i = Bougie baissière (l'OB)
                # i+1 = Bougie haussière (le début du mouvement)
                ob_candidate = df.iloc[i]
                ob_type = "BULLISH_OB"
                zone_top = ob_candidate['high']
                zone_bottom = ob_candidate['low']

            # 1. Trouver un candidat Bearish OB (dernière bougie haussière)
            elif df['close'].iloc[i] > df['open'].iloc[i] and df['close'].iloc[i+1] < df['open'].iloc[i+1]:
                # i = Bougie haussière (l'OB)
                # i+1 = Bougie baissière (le début du mouvement)
                ob_candidate = df.iloc[i]
                ob_type = "BEARISH_OB"
                zone_top = ob_candidate['high']
                zone_bottom = ob_candidate['low']
                
            if ob_candidate is None:
                continue

            # === Validation du Candidat ===
            
            # 2. Le mouvement suivant crée-t-il une Imbalance (FVG) ?
            move_start_index = i + 1
            move_end_index = i + 1 + self.ob_imbalance_check_candles
            move_fvgs = [
                fvg for fvg in fvgs 
                if fvg['timestamp'] >= df.index[move_start_index] and 
                   fvg['timestamp'] <= df.index[move_end_index] and
                   not fvg['mitigated']
            ]
            
            has_imbalance = False
            if ob_type == "BULLISH_OB" and any(f['type'] == 'BULLISH_FVG' for f in move_fvgs):
                has_imbalance = True
            if ob_type == "BEARISH_OB" and any(f['type'] == 'BEARISH_FVG' for f in move_fvgs):
                has_imbalance = True

            if not has_imbalance:
                continue # Invalide, pas de "displacement"

            # 3. Le mouvement suivant casse-t-il la structure ?
            # (Simplifié : casse le dernier swing high/low pertinent)
            structure_broken = False
            move_df = df.iloc[move_start_index : i + 1 + self.ob_structure_check_candles]

            if ob_type == "BULLISH_OB":
                # Doit casser le dernier swing high
                relevant_highs = [h[1] for h in ltf_swings['highs'] if h[0] < ob_candidate.name]
                if not relevant_highs: continue # Pas de structure à casser
                last_swing_high = max(relevant_highs)
                if move_df['high'].max() > last_swing_high:
                    structure_broken = True
            
            elif ob_type == "BEARISH_OB":
                # Doit casser le dernier swing low
                relevant_lows = [l[1] for l in ltf_swings['lows'] if l[0] < ob_candidate.name]
                if not relevant_lows: continue # Pas de structure à casser
                last_swing_low = min(relevant_lows)
                if move_df['low'].min() < last_swing_low:
                    structure_broken = True

            if not structure_broken:
                continue # Invalide, n'a pas cassé la structure

            # 4. L'OB est-il déjà mitigé ?
            # (Le prix est-il revenu dans l'OB *après* le mouvement ?)
            future_df = df.iloc[i + 2:] # Bougies après le début du mouvement
            mitigated = False
            if ob_type == "BULLISH_OB":
                # Mitigé si le prix est revenu toucher le 'high' de l'OB
                if not future_df.empty and future_df['low'].min() <= zone_top:
                    mitigated = True
            elif ob_type == "BEARISH_OB":
                # Mitigé si le prix est revenu toucher le 'low' de l'OB
                if not future_df.empty and future_df['high'].max() >= zone_bottom:
                    mitigated = True
                    
            if mitigated:
                continue # Invalide, déjà mitigé

            # === Si tout est OK, c'est un OB valide ===
            valid_obs.append({
                "timestamp": ob_candidate.name,
                "type": ob_type,
                "top": zone_top,
                "bottom": zone_bottom,
                "mitigated": False
            })

        return valid_obs


    def detect(self, df, structure_analysis):
        """
        Méthode principale pour détecter tous les patterns SMC.
        'df' est le DataFrame du Low Timeframe (LTF).
        'structure_analysis' est le dictionnaire de la Phase 1.
        """
        
        # Récupérer les swings LTF pour l'analyse des POI
        ltf_swings = structure_analysis.get('ltf_swings', {'highs': [], 'lows': []})

        # 1. Trouver les Imbalances (FVG)
        # On ne garde que les FVGs non mitigés
        all_fvgs = self._find_imbalances(df)
        unmitigated_fvgs = [fvg for fvg in all_fvgs if not fvg['mitigated']]
        
        # 2. Trouver la Liquidité (Cibles)
        liquidity_zones = self._find_liquidity(ltf_swings)

        # 3. Trouver les Order Blocks valides
        # Note: _find_order_blocks utilise les 'all_fvgs' pour sa validation interne
        valid_order_blocks = self._find_order_blocks(df, ltf_swings, all_fvgs)
        
        
        # 4. Combiner tous les POI (Points of Interest)
        # Les POI sont les zones où nous cherchons à entrer
        points_of_interest = unmitigated_fvgs + valid_order_blocks
        
        # Trier les POI par timestamp pour l'orchestrateur
        points_of_interest.sort(key=lambda x: x['timestamp'])

        print(f"Détection Patterns: {len(points_of_interest)} POI trouvés, {len(liquidity_zones['eqh'])} Cibles EQH, {len(liquidity_zones['eql'])} Cibles EQL.")

        # ### MODIFICATION ICI ###
        # Retourner le format attendu par l'orchestrateur
        return {
            "order_blocks": valid_order_blocks,
            "imbalances": unmitigated_fvgs,
            "liquidity_zones": liquidity_zones # 'liquidity_zones' est déjà {'eqh': [], 'eql': []}
        }
        # ### FIN MODIFICATION ###
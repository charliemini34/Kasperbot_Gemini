# src/strategy/smc_orchestrator.py

import time
import datetime
import pytz  # Nécessaire pour les fuseaux horaires
import pandas as pd
from src.analysis.market_structure import MarketStructure
from src.patterns.pattern_detector import PatternDetector
from src.strategy.smc_entry_logic import SMCEntryLogic

class SMCOrchestrator:
    def __init__(self, connector, executor, risk_manager, journal, config, shared_state, symbol):
        # CORRECTION: Signature __init__ restaurée pour inclure 'symbol'
        self.connector = connector
        self.executor = executor
        self.risk_manager = risk_manager
        self.journal = journal
        self.config = config
        self.shared_state = shared_state
        
        # Initialisation des modules d'analyse et de détection
        self.market_structure = MarketStructure(config.get('analysis', {}))
        self.pattern_detector = PatternDetector(config.get('patterns', {}))
        
        # Initialisation de la logique d'entrée (la gâchette)
        self.entry_logic = SMCEntryLogic(
            executor=self.executor,
            risk_manager=self.risk_manager,
            journal=self.journal,
            config=config.get('strategy', {}),
            shared_state=self.shared_state
        )
        
        # Récupération des paramètres de la config
        self.strategy_config = config.get('strategy', {})
        
        # CORRECTION: Utiliser le 'symbol' passé par main.py
        self.symbol = symbol 
        
        self.htf = self.strategy_config.get('htf', 'H1')
        self.ltf = self.strategy_config.get('ltf', 'M5')
        self.num_htf_candles = self.strategy_config.get('num_htf_candles', 200)
        self.num_ltf_candles = self.strategy_config.get('num_ltf_candles', 100)
        
        # Paramètres Fibo/OTE
        self.ote_premium_level = self.strategy_config.get('ote_premium_level', 0.5)
        self.ote_discount_level = self.strategy_config.get('ote_discount_level', 0.5)

        # --- AJOUT PHASE 4 : Kill Zones ---
        # Définir le fuseau horaire du serveur MT5 (généralement UTC)
        self.mt5_timezone = pytz.utc
        # Définir la Kill Zone de New York (ex: 14h30 - 17h00 Paris -> 12h30 - 15h00 UTC en hiver)
        # Note : MT5 utilise souvent l'heure UTC. Ajustez selon votre broker.
        # Nous utilisons les heures UTC ici.
        self.ny_kz_start = datetime.time(12, 30) # 12:30 UTC
        self.ny_kz_end = datetime.time(16, 0)  # 16:00 UTC
        # Bougie d'ouverture NY (ex: 14h30 Paris -> 12:30 UTC)
        self.ny_opening_candle_time = datetime.time(12, 30)
        self.ny_strategy_executed = False # Pour n'exécuter qu'une fois par jour
        # --- FIN AJOUT PHASE 4 ---

    def _get_fibonacci_levels(self, last_swing_low, last_swing_high):
        """Calcule les niveaux de Fibo OTE pour le dernier swing."""
        if last_swing_low is None or last_swing_high is None:
            return None, None
            
        swing_range = last_swing_high - last_swing_low
        
        # Niveaux Premium (pour les Ventes, au-dessus)
        premium_level = last_swing_low + (swing_range * self.ote_premium_level)
        
        # Niveaux Discount (pour les Achats, en dessous)
        discount_level = last_swing_high - (swing_range * self.ote_discount_level)
        
        return premium_level, discount_level

    def _filter_poi_by_bias(self, poi_list, bias):
        """Filtre les POI pour ne garder que ceux alignés avec le biais."""
        if bias == "BULLISH":
            return [poi for poi in poi_list if "BULLISH" in poi['type']]
        elif bias == "BEARISH":
            return [poi for poi in poi_list if "BEARISH" in poi['type']]
        return [] # Ne rien faire si en Ranging

    def _filter_poi_by_ote(self, poi_list, bias, htf_swings):
        """
        Filtre les POI alignés par biais en fonction de la zone OTE (Premium/Discount).
        """
        if not htf_swings['highs'] or not htf_swings['lows']:
            print("Filtre OTE: Manque de swings HTF pour le calcul Fibo.")
            return [] # Ne peut pas filtrer sans swings

        # Utiliser le dernier swing HTF complet pour le Fibo
        last_htf_high = htf_swings['highs'][-1][1]
        last_htf_low = htf_swings['lows'][-1][1]
        
        premium_level, discount_level = self._get_fibonacci_levels(last_htf_low, last_htf_high)
        
        if premium_level is None:
            return [] # Erreur de calcul Fibo

        high_prob_poi = []
        
        if bias == "BULLISH":
            # Pour un Achat, le POI doit être en zone "Discount" (en dessous de 0.5)
            for poi in poi_list:
                if poi['bottom'] < discount_level:
                    high_prob_poi.append(poi)
            print(f"Filtre OTE (Achat): {len(poi_list)} POI -> {len(high_prob_poi)} POI en zone Discount (< {discount_level:.2f})")

        elif bias == "BEARISH":
            # Pour une Vente, le POI doit être en zone "Premium" (au-dessus de 0.5)
            for poi in poi_list:
                if poi['top'] > premium_level:
                    high_prob_poi.append(poi)
            print(f"Filtre OTE (Vente): {len(poi_list)} POI -> {len(high_prob_poi)} POI en zone Premium (> {premium_level:.2f})")
            
        return high_prob_poi

    def _find_trade_target(self, bias, liquidity_zones):
        """Trouve la cible de liquidité (TP) la plus proche."""
        if bias == "BULLISH":
            # Cible = le EQH (Equal High) le plus proche
            if liquidity_zones['eqh']:
                return min(liquidity_zones['eqh']) # Le plus bas des "highs"
        elif bias == "BEARISH":
            # Cible = le EQL (Equal Low) le plus proche
            if liquidity_zones['eql']:
                return max(liquidity_zones['eql']) # Le plus haut des "lows"
        return None

    # --- NOUVELLE FONCTION PHASE 4 ---
    def _run_ny_kill_zone_strategy(self, ltf_data, structure_analysis):
        """
        Implémente la stratégie de scalping d'ouverture NY (Vidéo: 14h30 M5).
        """
        print("--- Stratégie KILL ZONE NEW YORK activée ---")
        
        # 1. Trouver la bougie d'ouverture de NY (ex: 12:30 UTC en M5)
        try:
            # S'assurer que les données sont indexées par datetime
            ltf_data.index = pd.to_datetime(ltf_data.index)
            opening_candle = ltf_data.at_time(self.ny_opening_candle_time)
            
            if opening_candle.empty:
                print(f"KZ NY: Bougie d'ouverture de {self.ny_opening_candle_time} non trouvée.")
                return False
                
            # Prendre la première si plusieurs (ne devrait pas arriver en M5)
            opening_candle = opening_candle.iloc[0]
            
        except Exception as e:
            print(f"Erreur lors de la recherche de la bougie d'ouverture: {e}")
            return False

        opening_high = opening_candle['high']
        opening_low = opening_candle['low']
        print(f"KZ NY: Bougie d'ouverture M5 ({self.ny_opening_candle_time}) détectée. High: {opening_high}, Low: {opening_low}")

        # 2. Analyser les bougies *après* la bougie d'ouverture
        data_after_open = ltf_data[ltf_data.index > opening_candle.name]
        if data_after_open.empty:
            print("KZ NY: Pas de données après la bougie d'ouverture.")
            return False

        trade_type = None
        breakout_candle = None
        
        # 3. Chercher la cassure (Breakout)
        for i in range(len(data_after_open)):
            candle = data_after_open.iloc[i]
            
            # Cassure Haussière (clôture au-dessus du High)
            if candle['close'] > opening_high:
                print(f"KZ NY: Cassure Haussière détectée à {candle.name}")
                trade_type = "BUY"
                breakout_candle = candle
                break
                
            # Cassure Baissière (clôture en dessous du Low)
            elif candle['close'] < opening_low:
                print(f"KZ NY: Cassure Baissière détectée à {candle.name}")
                trade_type = "SELL"
                breakout_candle = candle
                break
        
        if trade_type is None:
            print("KZ NY: Pas de cassure des niveaux d'ouverture pour l'instant.")
            return False

        # 4. Chercher "Displacement" (Imbalance) après la cassure
        # (Stratégie vidéo: la cassure crée une Imbalance, on rentre dessus)
        data_since_breakout = data_after_open[data_after_open.index >= breakout_candle.name]
        
        # Nous avons besoin d'au moins 3 bougies pour une Imbalance
        if len(data_since_breakout) < 3:
            print("KZ NY: Pas assez de bougies après la cassure pour détecter une Imbalance.")
            return False
            
        # Ré-analyser les patterns (surtout FVG) uniquement sur les nouvelles données
        # (Nous passons une analyse de structure vide car non pertinente ici)
        breakout_patterns = self.pattern_detector.detect(data_since_breakout, {'ltf_swings': {'highs': [], 'lows': []}})
        new_fvgs = breakout_patterns.get('poi', [])
        
        if not new_fvgs:
            print("KZ NY: Cassure sans Imbalance (Displacement). Pas de trade.")
            return False

        # 5. Trouver le FVG A+ (le premier FVG non-mitigé dans le sens du trade)
        target_fvg = None
        if trade_type == "BUY":
            bullish_fvgs = [fvg for fvg in new_fvgs if fvg['type'] == 'BULLISH_FVG' and not fvg['mitigated']]
            if bullish_fvgs:
                target_fvg = bullish_fvgs[0] # Prendre le premier FVG créé
        elif trade_type == "SELL":
            bearish_fvgs = [fvg for fvg in new_fvgs if fvg['type'] == 'BEARISH_FVG' and not fvg['mitigated']]
            if bearish_fvgs:
                target_fvg = bearish_fvgs[0]
                
        if target_fvg is None:
            print("KZ NY: Aucune Imbalance (FVG) valide trouvée après la cassure.")
            return False

        print(f"KZ NY: Imbalance (Displacement) trouvée. POI: {target_fvg}")

        # 6. Vérifier si le prix actuel est revenu sur ce FVG
        current_candle = ltf_data.iloc[-1]
        
        trade_opportunity = None # Réinitialiser
        if trade_type == "BUY" and current_candle['low'] <= target_fvg['top']:
            print(f"KZ NY: Opportunité d'ACHAT (retracement FVG) détectée.")
            trade_opportunity = "BUY"
            # TP = 2R (comme dans la vidéo de scalping)
            target_price = None # Le Risk Manager le calculera avec 2R
        
        elif trade_type == "SELL" and current_candle['high'] >= target_fvg['bottom']:
            print(f"KZ NY: Opportunité de VENTE (retracement FVG) détectée.")
            trade_opportunity = "SELL"
            target_price = None
        
        else:
            print("KZ NY: En attente de retracement vers le FVG...")
            return False # Le prix n'est pas encore revenu

        # 7. Exécution
        if trade_opportunity:
            # Nous avons une opportunité A+ basée sur la Kill Zone
            self.entry_logic.check_entry_confirmation(
                symbol=self.symbol,
                trade_type=trade_opportunity,
                poi_zone=target_fvg, # Notre zone d'entrée est l'Imbalance
                target_price=target_price, # Mettre None force le R:R fixe
                current_candle=current_candle,
                force_rr_target=2.0 # Forcer un R:R de 2:1 pour cette stratégie
            )
            
            # Marquer comme exécuté pour aujourd'hui
            self.ny_strategy_executed = True
            return True
        
        return False

    # --- FIN NOUVELLE FONCTION ---

    def run_strategy(self):
        """Boucle principale de la stratégie, appelée par main.py."""
        
        try:
            # --- Logique de Kill Zone (Phase 4) ---
            current_time_utc = datetime.datetime.now(self.mt5_timezone).time()
            
            # Réinitialiser le flag d'exécution chaque jour
            if current_time_utc < self.ny_kz_start:
                self.ny_strategy_executed = False

            # Vérifier si nous sommes dans la Kill Zone NY et que la strat n'a pas déjà été exécutée
            if (self.ny_kz_start <= current_time_utc <= self.ny_kz_end) and not self.ny_strategy_executed:
                
                # Récupérer les données LTF (plus de données pour l'analyse KZ)
                ltf_data_kz = self.connector.get_market_data(self.symbol, self.ltf, self.num_ltf_candles)
                
                if not ltf_data_kz.empty:
                    # Tenter d'exécuter la stratégie de Kill Zone
                    # Nous passons une analyse de structure vide car non nécessaire pour cette strat
                    strategy_ran = self._run_ny_kill_zone_strategy(ltf_data_kz, {})
                    if strategy_ran:
                        # Si la stratégie KZ a trouvé et exécuté un trade, on s'arrête là pour ce cycle
                        print("KZ NY: Trade exécuté. Cycle terminé.")
                        return 
                # Si la strat KZ a échoué (ex: pas de bougie), on passe à la strat normale
                print("KZ NY: Stratégie KZ vérifiée (pas de trade), passage à la stratégie SMC normale.")
            
            # --- Fin Logique Kill Zone ---


            # 1. Récupérer les données Multi-Timeframe
            # Note: self.symbol est maintenant défini par main.py
            print(f"Récupération des données: {self.num_htf_candles}x {self.htf} / {self.num_ltf_candles}x {self.ltf} pour {self.symbol}")
            htf_data = self.connector.get_market_data(self.symbol, self.htf, self.num_htf_candles)
            ltf_data = self.connector.get_market_data(self.symbol, self.ltf, self.num_ltf_candles)
            
            if htf_data.empty or ltf_data.empty:
                print("Données vides reçues de MT5. Attente.")
                return

            # 2. Phase 1: Analyser la Structure et le Biais
            structure_analysis = self.market_structure.analyze(htf_data, ltf_data)
            bias = structure_analysis['bias']
            htf_swings = structure_analysis['htf_swings']
            
            if bias == "RANGING":
                print(f"Biais HTF en Ranging pour {self.symbol}. Aucune action.")
                return

            # 3. Phase 2: Détecter les "Aimants" (POI et Liquidité)
            patterns = self.pattern_detector.detect(ltf_data, structure_analysis)
            all_poi = patterns['poi']
            liquidity_targets = patterns['liquidity']
            
            if not all_poi:
                print(f"Aucun POI (OB/FVG) valide trouvé pour {self.symbol}. Attente.")
                return

            # 4. Phase 3: Filtrage "Checklist A+"
            
            # 4a. Filtrer les POI par Biais
            biased_poi = self._filter_poi_by_bias(all_poi, bias)
            if not biased_poi:
                print(f"Aucun POI aligné avec le biais {bias} pour {self.symbol}. Attente.")
                return

            # 4b. Filtrer les POI par OTE (Premium/Discount)
            high_probability_pois = self._filter_poi_by_ote(biased_poi, bias, htf_swings)
            if not high_probability_pois:
                print(f"Aucun POI dans la zone OTE (Premium/Discount) pour {self.symbol}. Attente.")
                return

            # 5. Vérification d'Entrée
            # Le "cerveau" a trouvé des zones A+. Il vérifie si le prix actuel est dans l'une d'elles.
            current_price_low = ltf_data['low'].iloc[-1]
            current_price_high = ltf_data['high'].iloc[-1]
            
            for poi in high_probability_pois:
                
                trade_opportunity = None
                
                # Vérifier si le prix actuel touche le POI
                if bias == "BULLISH" and current_price_low <= poi['top']:
                    # Le prix touche un POI haussier A+
                    print(f"Opportunité d'ACHAT détectée: Prix touche POI {poi['type']} à {poi['top']:.2f} sur {self.symbol}")
                    trade_opportunity = "BUY"
                    
                elif bias == "BEARISH" and current_price_high >= poi['bottom']:
                    # Le prix touche un POI baissier A+
                    print(f"Opportunité de VENTE détectée: Prix touche POI {poi['type']} à {poi['bottom']:.2f} sur {self.symbol}")
                    trade_opportunity = "SELL"
                
                if trade_opportunity:
                    # Trouver la cible (TP)
                    target_price = self._find_trade_target(bias, liquidity_targets)
                    
                    # 6. Phase 3 (Gâchette): Déléguer à la logique d'entrée
                    # Transmettre la zone (POI) et la cible (TP) pour confirmation finale
                    self.entry_logic.check_entry_confirmation(
                        symbol=self.symbol,
                        trade_type=trade_opportunity,
                        poi_zone=poi,
                        target_price=target_price,
                        current_candle=ltf_data.iloc[-1] # Transmettre la dernière bougie pour confirmation
                    )
                    # On ne traite qu'une seule opportunité à la fois
                    break 

        except Exception as e:
            print(f"Erreur dans l'orchestrateur SMC pour {self.symbol}: {e}")
            # Gérer l'exception (ex: logging)
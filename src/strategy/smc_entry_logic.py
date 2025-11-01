# src/strategy/smc_entry_logic.py

class SMCEntryLogic:
    def __init__(self, executor, risk_manager, journal, config, shared_state):
        self.executor = executor
        self.risk_manager = risk_manager
        self.journal = journal
        self.config = config
        self.shared_state = shared_state
        self.min_rr = config.get('min_rr', 2.0) # Risk/Reward minimum de 2:1
        self.use_entry_confirmation = config.get('use_entry_confirmation', True)

    def _check_simple_confirmation(self, trade_type, current_candle, poi_zone):
        """
        Vérifie une confirmation d'entrée simple sur la bougie actuelle.
        (Logique de confirmation des vidéos : réaction, bougie englobante, etc.)
        
        Pour l'instant, nous vérifions si la bougie a bien réagi au POI.
        """
        if trade_type == "BUY":
            # Confirmation Achat: La bougie a touché le POI et a clôturé fortement (au-dessus de son ouverture)
            touched_poi = current_candle['low'] <= poi_zone['top']
            closed_strong = current_candle['close'] > current_candle['open']
            
            # Idéalement: la clôture est au-dessus du point médian de la bougie
            mid_point = (current_candle['high'] + current_candle['low']) / 2
            closed_above_mid = current_candle['close'] > mid_point
            
            return touched_poi and closed_strong and closed_above_mid
            
        elif trade_type == "SELL":
            # Confirmation Vente: La bougie a touché le POI et a clôturé faiblement (en dessous de son ouverture)
            touched_poi = current_candle['high'] >= poi_zone['bottom']
            closed_weak = current_candle['close'] < current_candle['open']
            
            # Idéalement: la clôture est en dessous du point médian
            mid_point = (current_candle['high'] + current_candle['low']) / 2
            closed_below_mid = current_candle['close'] < mid_point
            
            return touched_poi and closed_weak and closed_below_mid
            
        return False

    def check_entry_confirmation(self, symbol, trade_type, poi_zone, target_price, current_candle):
        """
        Appelée par l'Orchestrateur lorsqu'un POI A+ est touché.
        Valide la confirmation finale, le risque, et exécute le trade.
        """
        
        # Éviter les trades multiples sur la même zone
        if self.shared_state.is_trade_active():
            print("Logique d'Entrée: Trade déjà actif. Pas de nouvelle entrée.")
            return

        # 1. Confirmation d'entrée (Optionnel mais recommandé)
        if self.use_entry_confirmation:
            if not self._check_simple_confirmation(trade_type, current_candle, poi_zone):
                print("Logique d'Entrée: POI A+ touché, mais pas de confirmation (réaction) sur la bougie actuelle.")
                return
        
        print(f"Logique d'Entrée: Confirmation reçue pour {trade_type}.")

        # 2. Définir les paramètres du Trade (SL et Entrée)
        entry_price = 0
        stop_loss = 0
        
        if trade_type == "BUY":
            # Entrée: Nous pouvons entrer au marché ou à la limite sur le POI
            entry_price = current_candle['close'] # Entrée au marché
            # SL: Sous le plus bas du POI
            stop_loss = poi_zone['bottom']
            
        elif trade_type == "SELL":
            entry_price = current_candle['close'] # Entrée au marché
            # SL: Au-dessus du plus haut du POI
            stop_loss = poi_zone['top']
            
        if target_price is None:
            print("Logique d'Entrée: Pas de cible de liquidité (TP) trouvée. Annulation.")
            return

        # 3. Validation du Risque (Taille de lot et R:R)
        risk_validation = self.risk_manager.calculate_trade_parameters(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=target_price,
            trade_type=trade_type
        )
        
        if not risk_validation:
            print("Logique d'Entrée: Calcul de risque échoué (Stop trop proche?). Annulation.")
            return
            
        # Vérifier si le R:R est suffisant
        if risk_validation['rr'] < self.min_rr:
            print(f"Logique d'Entrée: R:R ({risk_validation['rr']:.2f}) insuffisant. Requis: {self.min_rr}. Annulation.")
            return

        print(f"Logique d'Entrée: Validation du Risque OK. Lot: {risk_validation['lot_size']}, R:R: {risk_validation['rr']:.2f}")

        # 4. Exécution du Trade
        try:
            trade_result = self.executor.execute_trade(
                symbol=symbol,
                lot_size=risk_validation['lot_size'],
                trade_type=trade_type,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=target_price,
                comment=f"SMC Bot: {poi_zone['type']}"
            )
            
            if trade_result and trade_result.get('request_id'):
                print(f"Trade Exécuté avec succès: {trade_result.get('request_id')}")
                self.shared_state.set_trade_active(True)
                
                # 5. Journalisation
                self.journal.record_trade(
                    symbol=symbol,
                    trade_type=trade_type,
                    entry=entry_price,
                    stop_loss=stop_loss,
                    take_profit=target_price,
                    lot_size=risk_validation['lot_size'],
                    rr=risk_validation['rr'],
                    strategy="SMC_Checklist_A+",
                    reason=f"POI: {poi_zone['type']}, Bias: {trade_type}",
                    result="OPEN"
                )
            else:
                print(f"Échec de l'exécution du trade: {trade_result}")

        except Exception as e:
            print(f"Erreur lors de l'exécution du trade: {e}")
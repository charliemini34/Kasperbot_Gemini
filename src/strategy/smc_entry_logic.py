# Fichier: src/strategy/smc_entry_logic.py
# Description: Logique de décision pour les entrées SMC (v20.0.9).

import logging
import pandas as pd
import numpy as np
from ..constants import ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT

class SMCEntryLogic:
    """
    Contient la logique de décision finale pour exécuter un trade SMC.
    Utilise le Biais HTF, la Structure HTF et les Patterns LTF pour trouver
    une confluence.
    """
    
    def __init__(self, config, executor, risk_manager, shared_state, symbol):
        self.log = logging.getLogger(f"{self.__class__.__name__}({symbol})")
        self.config = config
        self.executor = executor
        self.risk_manager = risk_manager
        self.shared_state = shared_state
        self.symbol = symbol

        # Charger les paramètres de risque et de pattern
        self.risk_config = config.get('risk_management', {})
        self.pattern_config = config.get('pattern_detection', {}).get('entry_logic', {})
        
        self.min_rr = self.risk_config.get('min_required_rr', 2.0)
        self.ob_entry_level = self.pattern_config.get('ob_entry_level', 0.5)
        self.fvg_entry_level = self.pattern_config.get('fvg_entry_level', 0.5)

    def find_smc_entry(self, htf_data, ltf_data, htf_bias, htf_structure, ltf_patterns, trading_enabled: bool): # ### MODIFICATION ICI ###
        """
        Point d'entrée principal de la logique de décision.
        Cherche une confluence de signaux pour placer un ordre limite.
        
        Args:
            htf_data (pd.DataFrame): Données OHLC High Timeframe.
            ltf_data (pd.DataFrame): Données OHLC Low Timeframe.
            htf_bias (str): 'Bullish', 'Bearish', ou 'Range'.
            htf_structure (dict): Dictionnaire de la structure HTF (highs, lows).
            ltf_patterns (dict): Dictionnaire des patterns LTF (OB, FVG, Liq).
            trading_enabled (bool): (NOUVEAU) Drapeau indiquant si le trading est autorisé.

        Returns:
            dict: Un dictionnaire représentant l'entrée, ou None.
        """
        
        # Mettre à jour l'état : "Recherche de confluence"
        self.shared_state.update_symbol_pattern_status(self.symbol, "Signal", "Recherche confluence...")

        # --- SCÉNARIO 1: Achat (Bullish Bias) ---
        if htf_bias == "Bullish":
            # 1. Identifier les POI (Points of Interest) valides
            # POI = Order Blocks Bullish ou FVG Bullish sous le prix actuel
            current_price = ltf_data.iloc[-1]['close']
            
            # Filtrer les OB Bullish pertinents
            valid_obs = [
                ob for ob in ltf_patterns.get('order_blocks', [])
                if ob['type'] == 'Bullish' and ob['price_high'] < current_price
            ]
            
            # Filtrer les FVG Bullish pertinents
            valid_fvgs = [
                fvg for fvg in ltf_patterns.get('imbalances', [])
                if fvg['type'] == 'Bullish' and fvg['price_high'] < current_price
            ]

            # 2. Identifier la Cible (Target)
            # Target = Liquidité Bearish (ex: EQL, Weak Highs) au-dessus du prix
            valid_targets = [
                liq for liq in ltf_patterns.get('liquidity_zones', [])
                if liq['type'] == 'Bearish' and liq['price_low'] > current_price
            ]

            # 3. Logique de confluence (très simplifiée pour l'instant)
            # Nous prenons le POI le plus proche et la Cible la plus proche
            
            best_poi = self._find_best_poi(valid_obs + valid_fvgs, current_price, direction='buy')
            best_target = self._find_best_target(valid_targets, current_price, direction='buy')

            if best_poi and best_target:
                # 4. Calculer l'entrée, le SL et le TP
                entry_price = best_poi['entry_price']
                stop_loss = best_poi['stop_loss']
                take_profit = best_target['target_price']

                # 5. Vérifier le Risk/Reward
                trade_params = self.risk_manager.check_trade_risk_smc(
                    entry_price, stop_loss, take_profit, ORDER_TYPE_BUY_LIMIT
                )

                if trade_params and trade_params['rr'] >= self.min_rr:
                    self.log.info(f"Signal d'ACHAT (BUY_LIMIT) trouvé. RR: {trade_params['rr']:.2f}")
                    
                    # ### MODIFICATION ICI ### : Vérifier si le trading est activé
                    if trading_enabled:
                        self.log.info(f"Exécution du trade (BUY_LIMIT) pour {self.symbol}...")
                        trade_result = self.executor.place_trade(
                            trade_type=ORDER_TYPE_BUY_LIMIT,
                            symbol=self.symbol,
                            lot_size=trade_params['lot_size'],
                            price=entry_price,
                            sl=stop_loss,
                            tp=take_profit,
                            magic_number=self.config.get('trading_settings', {}).get('magic_number', 13579),
                            comment=f"SMC-BUY-{htf_bias}"
                        )
                        decision = f"SIGNAL BUY_LIMIT @ {entry_price:.5f}"
                    else:
                        self.log.info(f"SYNCHRO: Signal d'ACHAT (BUY_LIMIT) trouvé mais non exécuté (trading désactivé).")
                        trade_result = None # Pas de trade
                        decision = "Signal (Synchro)"
                    
                    return {"decision": decision, "params": trade_params, "result": trade_result}
                else:
                    self.shared_state.update_symbol_pattern_status(self.symbol, "Signal", "Rejeté (RR Faible)")

        # --- SCÉNARIO 2: Vente (Bearish Bias) ---
        elif htf_bias == "Bearish":
            current_price = ltf_data.iloc[-1]['close']
            
            # 1. POI = Order Blocks Bearish ou FVG Bearish au-dessus du prix
            valid_obs = [
                ob for ob in ltf_patterns.get('order_blocks', [])
                if ob['type'] == 'Bearish' and ob['price_low'] > current_price
            ]
            valid_fvgs = [
                fvg for fvg in ltf_patterns.get('imbalances', [])
                if fvg['type'] == 'Bearish' and fvg['price_low'] > current_price
            ]
            
            # 2. Target = Liquidité Bullish (ex: EQL, Weak Lows) en dessous du prix
            valid_targets = [
                liq for liq in ltf_patterns.get('liquidity_zones', [])
                if liq['type'] == 'Bullish' and liq['price_high'] < current_price
            ]
            
            # 3. Confluence
            best_poi = self._find_best_poi(valid_obs + valid_fvgs, current_price, direction='sell')
            best_target = self._find_best_target(valid_targets, current_price, direction='sell')

            if best_poi and best_target:
                # 4. Calculer l'entrée, le SL et le TP
                entry_price = best_poi['entry_price']
                stop_loss = best_poi['stop_loss']
                take_profit = best_target['target_price']

                # 5. Vérifier le Risk/Reward
                trade_params = self.risk_manager.check_trade_risk_smc(
                    entry_price, stop_loss, take_profit, ORDER_TYPE_SELL_LIMIT
                )

                if trade_params and trade_params['rr'] >= self.min_rr:
                    self.log.info(f"Signal de VENTE (SELL_LIMIT) trouvé. RR: {trade_params['rr']:.2f}")

                    # ### MODIFICATION ICI ### : Vérifier si le trading est activé
                    if trading_enabled:
                        self.log.info(f"Exécution du trade (SELL_LIMIT) pour {self.symbol}...")
                        trade_result = self.executor.place_trade(
                            trade_type=ORDER_TYPE_SELL_LIMIT,
                            symbol=self.symbol,
                            lot_size=trade_params['lot_size'],
                            price=entry_price,
                            sl=stop_loss,
                            tp=take_profit,
                            magic_number=self.config.get('trading_settings', {}).get('magic_number', 13579),
                            comment=f"SMC-SELL-{htf_bias}"
                        )
                        decision = f"SIGNAL SELL_LIMIT @ {entry_price:.5f}"
                    else:
                        self.log.info(f"SYNCHRO: Signal de VENTE (SELL_LIMIT) trouvé mais non exécuté (trading désactivé).")
                        trade_result = None # Pas de trade
                        decision = "Signal (Synchro)"
                        
                    return {"decision": decision, "params": trade_params, "result": trade_result}
                else:
                    self.shared_state.update_symbol_pattern_status(self.symbol, "Signal", "Rejeté (RR Faible)")

        # Si aucun scénario n'est rencontré
        if htf_bias != "Range":
             self.shared_state.update_symbol_pattern_status(self.symbol, "Signal", f"En attente POI/Cible ({htf_bias})")
        
        return None

    # --- Fonctions utilitaires (simplifiées) ---

    def _find_best_poi(self, pois, current_price, direction='buy'):
        """ Trouve le POI le plus proche. """
        if not pois:
            return None
            
        best_poi = None
        if direction == 'buy':
            # Cherche le POI (OB ou FVG) le plus haut (prix le plus élevé)
            # qui est SOUS le prix actuel
            best_poi = max(pois, key=lambda x: x['price_high'])
            # Définir l'entrée et le SL pour un Achat
            if 'imbalance' in best_poi['name']:
                entry = best_poi['price_high'] - (best_poi['price_high'] - best_poi['price_low']) * self.fvg_entry_level
            else: # Order Block
                entry = best_poi['price_high'] - (best_poi['price_high'] - best_poi['price_low']) * self.ob_entry_level
            best_poi['entry_price'] = entry
            best_poi['stop_loss'] = best_poi['sl_price'] # SL défini par pattern_detector
            
        elif direction == 'sell':
            # Cherche le POI le plus bas (prix le plus bas)
            # qui est AU-DESSUS du prix actuel
            best_poi = min(pois, key=lambda x: x['price_low'])
            # Définir l'entrée et le SL pour une Vente
            if 'imbalance' in best_poi['name']:
                entry = best_poi['price_low'] + (best_poi['price_high'] - best_poi['price_low']) * self.fvg_entry_level
            else: # Order Block
                entry = best_poi['price_low'] + (best_poi['price_high'] - best_poi['price_low']) * self.ob_entry_level
            best_poi['entry_price'] = entry
            best_poi['stop_loss'] = best_poi['sl_price'] # SL défini par pattern_detector
            
        return best_poi

    def _find_best_target(self, targets, current_price, direction='buy'):
        """ Trouve la cible de liquidité la plus proche. """
        if not targets:
            return None
            
        best_target = None
        if direction == 'buy':
            # Cherche la cible (liquidité) la plus basse
            # qui est AU-DESSUS du prix actuel
            best_target = min(targets, key=lambda x: x['price_low'])
            best_target['target_price'] = best_target['price_low'] # Viser le bas de la zone
            
        elif direction == 'sell':
            # Cherche la cible (liquidité) la plus haute
            # qui est SOUS le prix actuel
            best_target = max(targets, key=lambda x: x['price_high'])
            best_target['target_price'] = best_target['price_high'] # Viser le haut de la zone
            
        return best_target
# Fichier: src/strategy/smc_orchestrator.py
# Description: Orchestrateur principal pour la stratégie SMC (v20.0.9).

import logging
import pandas as pd
from ..analysis.market_structure import MarketStructure
from ..patterns.pattern_detector import PatternDetector
from .smc_entry_logic import SMCEntryLogic

class SMCOrchestrator:
    """
    Orchestre l'ensemble de la logique SMC :
    1. Récupère les données MTF.
    2. Analyse la structure (Biais) sur le HTF.
    3. Détecte les POI (OB, FVG) et la liquidité sur le LTF.
    4. Cherche des entrées basées sur la confluence.
    5. Met à jour le SharedState pour l'API.
    """
    
    def __init__(self, connector, executor, risk_manager, journal, config, shared_state, symbol):
        self.log = logging.getLogger(f"{self.__class__.__name__}({symbol})")
        self.connector = connector
        self.executor = executor
        self.risk_manager = risk_manager # (v20.0.9) Chaque orchestrateur a son propre RM
        self.journal = journal
        self.config = config
        self.shared_state = shared_state
        self.symbol = symbol

        # Récupérer les timeframes de la config
        strategy_config = config.get('smc_strategy', {})
        self.htf = strategy_config.get('htf_timeframe', 'H4')
        self.ltf = strategy_config.get('ltf_timeframe', 'M15')
        
        # Récupérer le nombre de bougies de la config
        data_config = config.get('trading_settings', {}).get('mtf_data_config', {})
        self.htf_candles = data_config.get(self.htf, 300)
        self.ltf_candles = data_config.get(self.ltf, 500)

        # Initialisation des modules d'analyse
        self.structure_analyzer = MarketStructure(config)
        self.pattern_detector = PatternDetector(config)
        
        # (v20.0.9) Passer le RM spécifique au symbole à la logique d'entrée
        self.entry_logic = SMCEntryLogic(
            config=config,
            executor=executor,
            risk_manager=self.risk_manager,
            shared_state=shared_state,
            symbol=symbol
        )
        
        # Initialiser l'état pour ce symbole
        self.shared_state.initialize_symbol_status(self.symbol)
        self.log.info(f"Initialisé avec HTF={self.htf} ({self.htf_candles} bougies) et LTF={self.ltf} ({self.ltf_candles} bougies).")

    def run_strategy(self, trading_enabled: bool):
        """
        Exécute un cycle complet d'analyse et de trading.
        """
        try:
            # 1. Récupérer les données
            htf_data = self.connector.get_ohlc(self.symbol, self.htf, self.htf_candles)
            ltf_data = self.connector.get_ohlc(self.symbol, self.ltf, self.ltf_candles)

            if htf_data is None or ltf_data is None or htf_data.empty or ltf_data.empty:
                self.log.warning("Données OHLC (HTF ou LTF) indisponibles. Cycle sauté.")
                self.shared_state.update_symbol_pattern_status(self.symbol, "Biais HTF", "ERREUR: Données HTF")
                return

            # 2. Analyse de la Structure (Biais HTF)
            structure_analysis = self.structure_analyzer.analyze(htf_data, ltf_data)
            htf_bias = structure_analysis['bias']
            htf_structure = structure_analysis['htf_swings'] # On passe les swings HTF
            
            self.shared_state.update_symbol_pattern_status(self.symbol, "Biais HTF", htf_bias)

            # 3. Détection des POI et de la Liquidité (LTF)
            
            # ### MODIFICATION ICI ###
            # Appel de la VRAIE méthode 'detect' de 'pattern_detector.py'
            # en lui passant les arguments qu'elle attend.
            ltf_patterns = self.pattern_detector.detect(ltf_data, structure_analysis)
            # ### FIN MODIFICATION ###
            
            # Mettre à jour le shared_state avec les détections
            self.shared_state.update_symbol_pattern_status(self.symbol, "Order Blocks", f"{len(ltf_patterns.get('order_blocks', []))} trouvés")
            self.shared_state.update_symbol_pattern_status(self.symbol, "Imbalances (FVG)", f"{len(ltf_patterns.get('imbalances', []))} trouvés")
            self.shared_state.update_symbol_pattern_status(self.symbol, "Liquidité", f"{len(ltf_patterns.get('liquidity_zones', []))} trouvées")

            # 4. Logique d'Entrée (Confluence)
            entry = self.entry_logic.find_smc_entry(
                htf_data=htf_data,
                ltf_data=ltf_data,
                htf_bias=htf_bias,
                htf_structure=htf_structure,
                ltf_patterns=ltf_patterns,
                trading_enabled=trading_enabled
            )

            if entry:
                self.log.info(f"Logique d'entrée a retourné un signal: {entry.get('decision')}")
                self.shared_state.update_symbol_pattern_status(self.symbol, "Signal", entry.get('decision'))
            
            # (R.8) Mettre à jour l'état global (pour l'API)
            self.shared_state.update_analysis_data(self.symbol, htf_bias, ltf_patterns, htf_structure)


        except Exception as e:
            self.log.error(f"Erreur inattendue dans run_strategy pour {self.symbol}: {e}", exc_info=True)
            self.shared_state.update_symbol_pattern_status(self.symbol, "Orchestrateur", f"ERREUR: {e}")
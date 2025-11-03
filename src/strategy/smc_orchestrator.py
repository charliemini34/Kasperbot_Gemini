# __version__ = "1.6"
# Nom du fichier : src/strategy/smc_orchestrator.py
import logging
import time
from typing import Dict, Any, Optional

# Imports des modules du projet
from src.data_ingest import mt5_connector
from src.analysis import market_structure
from src.patterns import pattern_detector
from src.strategy import smc_entry_logic
from src.risk import risk_manager
from src.execution import mt5_executor
from src.management import trade_manager
from src.shared_state import shared_state

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_analysis_cycle(connector: mt5_connector.MT5Connector, symbol: str, config: Dict[str, Any]):
    """
    Exécute un cycle complet d'analyse SMC, de la recherche d'opportunités à la gestion des trades.
    Cette fonction est le "cœur" du bot.
    """
    try:
        # 0. Récupérer les informations de base
        symbol_info = connector.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"Orchestrateur : Impossible de récupérer les infos pour {symbol}.")
            return

        # 1. Analyse de Tendance (HTF - Higher Timeframe)
        htf_timeframe = config['analysis']['htf_timeframe']
        htf_data = connector.get_market_data(symbol, htf_timeframe, config['analysis']['htf_data_points'])
        
        if htf_data is None or htf_data.empty:
            logger.warning(f"Orchestrateur : Aucune donnée HTF ({htf_timeframe}) disponible pour {symbol}.")
            return

        htf_trend = market_structure.get_market_trend(htf_data, window=config['analysis']['htf_trend_window'])
        shared_state.update_symbol_state(symbol, {"htf_trend": htf_trend})
        logger.info(f"Orchestrateur : Tendance HTF ({htf_timeframe}) pour {symbol} est {htf_trend}")

        # 2. Analyse de Structure (LTF - Lower Timeframe)
        ltf_timeframe = config['analysis']['ltf_timeframe']
        ltf_data = connector.get_market_data(symbol, ltf_timeframe, config['analysis']['ltf_data_points'])
        
        if ltf_data is None or ltf_data.empty:
            logger.warning(f"Orchestrateur : Aucune donnée LTF ({ltf_timeframe}) disponible pour {symbol}.")
            return
        
        # 2a. Trouver les pivots (Étape 1a)
        sh_ltf, sl_ltf = market_structure.find_swing_highs_lows(ltf_data, n=config['analysis']['ltf_pivot_n'])
        
        # 2b. Identifier BOS/ChoCH (Étape 1b)
        structure_ltf = market_structure.identify_bos_choch(ltf_data, sh_ltf, sl_ltf)
        shared_state.update_symbol_state(symbol, {"ltf_structure": structure_ltf})

        # 3. Détection des Patterns (Étape 2)
        # MISE À JOUR (v1.6) : On passe les pivots à detect_patterns pour la détection de liquidité
        pivot_data = {"swing_highs": sh_ltf, "swing_lows": sl_ltf}
        patterns_ltf = pattern_detector.detect_patterns(ltf_data, structure_analysis=pivot_data)
        shared_state.update_symbol_state(symbol, {"ltf_patterns": patterns_ltf})
        
        # 4. Logique d'Entrée (Étape 3)
        entry_signal = smc_entry_logic.check_entry_conditions(ltf_data, structure_ltf, patterns_ltf, htf_trend)
        
        if entry_signal:
            logger.info(f"Orchestrateur : Signal d'entrée {entry_signal['signal']} trouvé pour {symbol}.")
            
            # 5. Validation du Risque (Étape 4)
            account_info = connector.get_account_info()
            if not account_info:
                logger.error("Orchestrateur : Impossible de récupérer les infos du compte pour valider le risque.")
                return
            
            trade_params = risk_manager.validate_trade_risk(
                entry_signal, 
                patterns_ltf, 
                account_info, 
                symbol_info, 
                config
            )
            
            # 6. Exécution du Trade (si le risque est valide)
            if trade_params:
                # Vérifier si on a déjà un trade ouvert sur ce symbole
                open_positions = connector.get_open_positions(symbol)
                if not open_positions: # TODO: Permettre plusieurs trades si configuré
                    logger.info(f"Orchestrateur : Validation du risque OK. Passage de l'ordre pour {symbol}.")
                    executor = mt5_executor.MT5Executor(config)
                    # MISE À JOUR (v1.6) : L'exécuteur doit ajouter le COMMENTAIRE (sera fait dans mt5_executor.py)
                    trade_result = executor.place_trade(trade_params)
                    if trade_result and trade_result['retcode'] == 10009: # Code MT5 pour "requête exécutée"
                        logger.info(f"Orchestrateur : Trade {trade_result['order']} ouvert avec succès.")
                        shared_state.log_trade(trade_result, trade_params)
                    else:
                        logger.error(f"Orchestrateur : Échec de l'ouverture du trade. Résultat: {trade_result}")
                else:
                    logger.info(f"Orchestrateur : Signal trouvé, mais un trade est déjà ouvert pour {symbol}. Ordre non-passé.")

        # 7. Gestion des Trades Ouverts (Étape 5)
        open_positions = connector.get_open_positions(symbol)
        if open_positions:
            logger.info(f"Orchestrateur : {len(open_positions)} trade(s) ouvert(s) pour {symbol}. Vérification de la gestion...")
            
            # MISE À JOUR (v1.6) : Nouvel appel de fonction
            # Nous avons besoin des données de structure (pour le TS) et du tick (pour le BE)
            
            tick_info = connector.get_symbol_tick(symbol)
            if not tick_info:
                logger.warning("Orchestrateur (Gestion) : Impossible de récupérer le tick_info pour le BE.")
                return

            modification_requests = trade_manager.manage_open_trades(
                open_positions,
                symbol_info,
                config,
                structure_ltf,  # Argument (structure) ajouté
                tick_info       # Argument (tick) ajouté
            )
            
            if modification_requests:
                logger.info(f"Orchestrateur : {len(modification_requests)} demande(s) de modification (BE/TS) pour {symbol}.")
                executor = mt5_executor.MT5Executor(config)
                for request in modification_requests:
                    executor.modify_trade(request)
            else:
                logger.info("Orchestrateur : Aucune action de gestion (BE/TS) requise.")
                
    except Exception as e:
        logger.error(f"Erreur inattendue dans le cycle d'analyse (Orchestrateur) : {e}", exc_info=True)

if __name__ == "__main__":
    # Bloc de test pour l'orchestrateur
    logger.info("Démarrage du test de l'orchestrateur SMC (simulation)...")
    
    # Simuler la configuration
    test_config = {
        "mt5": { "path": "C:\\Program Files\\MetaTrader 5\\mt5.exe" }, # Mettre un chemin valide si nécessaire
        "analysis": {
            "htf_timeframe": "H1", "htf_data_points": 200, "htf_trend_window": 50,
            "ltf_timeframe": "M5", "ltf_data_points": 100, "ltf_pivot_n": 5
        },
        "trading": {
            "symbols": ["EURUSD"],
            "risk_per_trade_percent": 1.0,
            "sl_buffer_pips": 2.0,
            "min_rrr": 2.0,
            "enable_break_even": True,
            "be_trigger_rrr": 1.0,
            "enable_trailing_stop": True,
            "enable_partials": False
        }
    }
    
    # Simuler un connecteur (pour éviter une connexion réelle lors du test)
    class MockConnector:
        def __init__(self, config):
            logger.info("MockConnector initialisé.")
        
        def connect(self): return True
        def disconnect(self): pass
        
        def get_symbol_info(self, symbol):
            return {"name": symbol, "point": 0.00001, "trade_contract_size": 100000.0, "volume_step": 0.01, "volume_min": 0.01}
        
        def get_market_data(self, symbol, timeframe, points):
            # Créer un faux DataFrame pandas
            import pandas as pd
            import numpy as np
            dates = pd.date_range(end=pd.Timestamp.now(tz='UTC'), periods=points, freq='min')
            prices = 1.10000 + (np.random.randn(points).cumsum() * 0.0001)
            df = pd.DataFrame({
                'time': dates,
                'open': prices,
                'high': prices + 0.0005,
                'low': prices - 0.0005,
                'close': prices,
                'tick_volume': np.random.randint(10, 100, size=points)
            })
            df.set_index('time', inplace=True)
            return df
            
        def get_account_info(self):
            return {"balance": 10000.0, "equity": 10000.0}
            
        def get_symbol_tick(self, symbol):
            return {"ask": 1.10100, "bid": 1.10090}
            
        def get_open_positions(self, symbol):
            return [] # Simuler aucun trade ouvert

    # Exécuter le cycle avec le faux connecteur
    mock_conn = MockConnector(test_config)
    try:
        run_analysis_cycle(mock_conn, "EURUSD", test_config)
    except Exception as e:
        logger.error(f"Test de l'orchestrateur échoué : {e}")
        
    logger.info("Test de l'orchestrateur SMC terminé.")
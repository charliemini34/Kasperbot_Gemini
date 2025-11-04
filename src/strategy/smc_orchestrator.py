# __version__ = "1.6" <- Ancienne version
# Nom du fichier : src/strategy/smc_orchestrator.py
# Version: 3.0
#
# Ce module est le "cœur" du bot. Il orchestre le cycle d'analyse :
# 1. Récupérer les données
# 2. Analyser la tendance HTF
# 3. Analyser la structure LTF
# 4. Détecter les patterns (POI, Liquidité)
# 5. Appeler la logique d'entrée
# 6. Valider le risque
# 7. Exécuter le trade
# 8. Gérer les trades ouverts
#
# V3.0: Ajout du reporting en temps réel vers le shared_state pour le dashboard.
# --------------------------------------------------------------------------

import logging
import time
from typing import Dict, Any, Optional

# NOUVEAUX IMPORTS (V3.0)
import pytz
from datetime import datetime

# Imports des modules du projet
from src.data_ingest import mt5_connector
from src.analysis import market_structure
from src.patterns import pattern_detector
from src.strategy import smc_entry_logic
from src.risk import risk_manager
from src.execution import mt5_executor
from src.management import trade_manager

# NOUVEAUX IMPORTS (V3.0) pour le reporting d'état
from src.shared_state import shared_state, update_symbol_check, update_symbol_signal

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

__version__ = "3.0"

def run_analysis_cycle(connector: mt5_connector.MT5Connector, symbol: str, config: Dict[str, Any]):
    """
    Exécute un cycle complet d'analyse SMC, de la recherche d'opportunités à la gestion des trades.
    Cette fonction est le "cœur" du bot.
    """
    try:
        # --- DÉBUT MODIFICATIONS V3.0 (Reporting) ---
        
        # Dictionnaire pour suivre les checks de notation de ce cycle
        checks_status = {
            "trend": "pending",
            "zone": "pending",
            "confirmation": "pending",
            "session": "pending",
            "risk_sl": "pending",
            "risk_rr": "pending",
            "poi": "pending" # Check supplémentaire
        }
        
        # 0. Récupérer les informations de base
        symbol_info = connector.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"Orchestrateur : Impossible de récupérer les infos pour {symbol}.")
            return

        # Récupérer le pip_size de la config (nécessaire pour le check RRR)
        pip_size = config.get('risk', {}).get('pip_sizes', {}).get(symbol, 
                   config.get('risk', {}).get('default_pip_size', 0.0001))

        # Check 5 (Session / Volatilité)
        is_active_session, session_name = _is_in_killzone(config)
        checks_status["session"] = "valid" if is_active_session else "invalid"
        update_symbol_check(symbol, "session", checks_status["session"])
        
        # --- FIN MODIFICATIONS V3.0 ---


        # 1. Analyse de Tendance (HTF - Higher Timeframe)
        htf_timeframe = config['analysis']['htf_timeframe']
        htf_data = connector.get_market_data(symbol, htf_timeframe, config['analysis']['htf_data_points'])
        
        if htf_data is None or htf_data.empty:
            logger.warning(f"Orchestrateur : Aucune donnée HTF ({htf_timeframe}) disponible pour {symbol}.")
            return

        htf_trend = market_structure.get_market_trend(htf_data, window=config['analysis']['htf_trend_window'])
        shared_state.update_symbol_state(symbol, {"htf_trend": htf_trend})
        logger.info(f"Orchestrateur : Tendance HTF ({htf_timeframe}) pour {symbol} est {htf_trend}")

        # --- DÉBUT MODIFICATIONS V3.0 (Reporting) ---
        # Check 1 (Tendance HTF)
        if htf_trend in ["BULLISH", "BEARISH"]:
            checks_status["trend"] = "valid"
        else:
            checks_status["trend"] = "invalid" # Ex: SIDEWAYS
        update_symbol_check(symbol, "trend", checks_status["trend"])
        # --- FIN MODIFICATIONS V3.0 ---


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
        # NOTE V3.0: Pour le moment, check_entry_conditions ne retourne pas les checks intermédiaires.
        # Nous mettrons à jour les checks "zone", "poi", "confirmation" seulement si un signal est trouvé.
        entry_signal = smc_entry_logic.check_entry_conditions(ltf_data, structure_ltf, patterns_ltf, htf_trend)
        
        if entry_signal:
            logger.info(f"Orchestrateur : Signal d'entrée {entry_signal['signal']} trouvé pour {symbol}.")
            
            # --- DÉBUT MODIFICATIONS V3.0 (Reporting) ---
            # Un signal a été trouvé, donc les checks logiques sont implicitement valides
            checks_status["zone"] = "valid"
            checks_status["confirmation"] = "valid"
            checks_status["poi"] = "valid"
            update_symbol_check(symbol, "zone", "valid")
            update_symbol_check(symbol, "confirmation", "valid")
            update_symbol_check(symbol, "poi", "valid")
            # --- FIN MODIFICATIONS V3.0 ---

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
                # --- DÉBUT MODIFICATIONS V3.0 (Reporting) ---
                # Le risque est valide !
                checks_status["risk_sl"] = "valid"
                checks_status["risk_rr"] = "valid"
                update_symbol_check(symbol, "risk_sl", "valid")
                update_symbol_check(symbol, "risk_rr", "valid")
                
                # Publier le signal complet sur le dashboard
                _rate_and_publish_signal(symbol, trade_params, checks_status, config, pip_size)
                # --- FIN MODIFICATIONS V3.0 ---

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

            else:
                # --- DÉBUT MODIFICATIONS V3.0 (Reporting) ---
                # Le risque a été rejeté (ex: RRR trop faible, SL trop large)
                logger.warning(f"Orchestrateur: Signal pour {symbol} REJETÉ par le risk_manager.")
                checks_status["risk_sl"] = "invalid"
                checks_status["risk_rr"] = "invalid"
                update_symbol_check(symbol, "risk_sl", "invalid")
                update_symbol_check(symbol, "risk_rr", "invalid")
                # --- FIN MODIFICATIONS V3.0 ---


        # 7. Gestion des Trades Ouverts (Étape 5)
        # (Cette section est inchangée)
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


# --- NOUVELLE FONCTION (V3.0) ---
def _is_in_killzone(config: Dict[str, Any]) -> (bool, str):
    """
    Vérifie si l'heure UTC actuelle se trouve dans une des Killzones
    définies dans le config.yaml.
    """
    try:
        killzones = config.get('killzones', {})
        if not killzones:
            return True, "N/A" # Si non configuré, on considère toujours comme valide

        now_utc = datetime.now(pytz.utc).time()

        for zone_name, times in killzones.items():
            try:
                start_time = datetime.strptime(times['start_utc'], '%H:%M').time()
                end_time = datetime.strptime(times['end_utc'], '%H:%M').time()
                
                if start_time <= now_utc <= end_time:
                    return True, zone_name.upper()
            except Exception as e:
                logger.warning(f"[Orchestrateur] Format de Killzone incorrect pour '{zone_name}': {e}")
                
        return False, "NONE"
        
    except Exception as e:
        logger.error(f"Erreur lors de la vérification de la Killzone: {e}")
        return False, "ERROR"


# --- NOUVELLE FONCTION (V3.0) ---
def _rate_and_publish_signal(symbol: str, 
                             trade_params: Dict[str, Any], 
                             checks_status: Dict[str, str], 
                             config: Dict[str, Any],
                             pip_size: float):
    """
    Calcule la note (5 étoiles) du signal et le publie dans le shared_state.
    """
    try:
        rating = 0
        
        # 1. Tendance (Biais HTF)
        if checks_status.get("trend") == "valid":
            rating += 1
            
        # 2. Point d'entrée (Zone OTE/Discount)
        # Note: 'zone' est implicitement validé si un signal est trouvé (simplification V3.0)
        if checks_status.get("zone") == "valid":
            rating += 1
            
        # 3. SL (Risque/Bruit)
        # Note: 'risk_sl' est validé par le risk_manager
        if checks_status.get("risk_sl") == "valid":
            rating += 1
            
        # 4. RRR (Risk/Reward Ratio)
        if checks_status.get("risk_rr") == "valid":
            rating += 1
            
        # 5. Session (Volatilité idéale)
        if checks_status.get("session") == "valid":
            rating += 1

        # Formatage de la chaîne "Copier/Coller"
        signal_type = trade_params.get('type_str', 'N/A').upper()
        price = trade_params.get('price', 0.0)
        sl = trade_params.get('sl', 0.0)
        tp = trade_params.get('tp', 0.0)
        
        # Arrondir à un nombre de décimales raisonnable (basé sur le pip_size)
        decimals = 5 if pip_size < 0.001 else 2
        
        copy_string = f"{signal_type} {symbol} {price:.{decimals}f}, SL {sl:.{decimals}f}, TP {tp:.{decimals}f}"

        # Création du payload pour le shared_state
        signal_data = {
            "is_valid": True,
            "rating": rating,
            "stars": "★" * rating + "☆" * (5 - rating), # ex: ★★★☆☆
            "copy_string": copy_string
        }
        
        # Publication
        update_symbol_signal(symbol, signal_data)
        logger.info(f"Signal pour {symbol} publié sur le dashboard (Note: {rating}/5)")

    except Exception as e:
        logger.error(f"Erreur lors de la notation et publication du signal: {e}", exc_info=True)


if __name__ == "__main__":
    # Bloc de test pour l'orchestrateur
    logger.info("Démarrage du test de l'orchestrateur SMC (simulation)...")
    
    # Simuler la configuration
    test_config = {
        # --- V3.0 ---
        "killzones": {
            "london": {"start_utc": "07:00", "end_utc": "10:00"},
            "ny": {"start_utc": "13:00", "end_utc": "16:00"}
        },
        # --- Fin V3.0 ---
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
        },
        # --- V3.0 ---
        "risk": {
            "pip_sizes": {"EURUSD": 0.0001},
            "default_pip_size": 0.0001
        }
        # --- Fin V3.0 ---
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
        # --- V3.0 ---
        # Initialiser le shared_state pour le test
        shared_state.initialize_symbols(test_config['trading']['symbols'])
        # --- Fin V3.0 ---
        
        run_analysis_cycle(mock_conn, "EURUSD", test_config)
    except Exception as e:
        logger.error(f"Test de l'orchestrateur échoué : {e}")
        
    logger.info("Test de l'orchestrateur SMC terminé.")
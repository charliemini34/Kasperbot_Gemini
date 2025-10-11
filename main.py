# ==============================================================================
#                 BOT DE TRADING XAUUSD v3.0 - LANCEUR PRINCIPAL
# ==============================================================================
import time
import threading
import logging
import yaml
import webbrowser
import os
import math

# --- Importation des modules du projet ---
from src.data_ingest.mt5_connector import MT5Connector
from src.scorer.strategy_scorer import StrategyScorer
from src.scorer.aggregator import Aggregator
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer
from src.analysis.ai_assistant import AIAssistant

# --- Configuration du logging ---
def setup_logging(state):
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    
    ui_handler = LogHandler(state)
    ui_handler.setFormatter(log_formatter)
    
    file_handler = logging.FileHandler("trading_bot.log", mode='w')
    file_handler.setFormatter(log_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        root_logger.addHandler(ui_handler)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
    
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath):
    """Charge un fichier de configuration YAML de manière sécurisée."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.error(f"FATAL: Le fichier '{filepath}' est introuvable. Arrêt du programme.")
        exit()
    except Exception as e:
        logging.error(f"FATAL: Erreur de lecture de '{filepath}': {e}")
        exit()

def main_trading_loop(state):
    """Boucle principale qui orchestre le bot."""
    logging.info("Démarrage de la boucle de trading...")
    
    config = load_yaml('config.yaml')
    profiles = load_yaml('profiles.yaml')
    state.update_config(config)

    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 échouée", is_emergency=True)
        return

    analyzer = PerformanceAnalyzer('trade_history.csv')
    ai_assistant = AIAssistant()
    trade_count_since_analysis = 0

    while not state.is_shutdown():
        try:
            if state.config_changed_flag:
                config = load_yaml('config.yaml')
                profiles = load_yaml('profiles.yaml')
                state.update_config(config)
                state.clear_config_changed_flag()
                logging.info("Configuration et profils rechargés.")
            
            active_profile_name = config['trading_logic']['active_profile']
            strategy_weights = profiles.get(active_profile_name)
            if not strategy_weights:
                logging.error(f"Profil '{active_profile_name}' non trouvé dans profiles.yaml. Utilisation du profil 'custom'.")
                strategy_weights = profiles['custom']
            
            scorer = StrategyScorer()
            aggregator = Aggregator(strategy_weights)
            executor = MT5Executor(connector.get_connection(), analyzer)
            risk_manager = RiskManager(config['risk_management'], executor, config['trading_settings']['symbol'])

            symbol = config['trading_settings']['symbol']
            timeframe_str = config['trading_settings']['timeframe']

            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les informations du compte.")
                time.sleep(10)
                continue

            state.update_status("En cours", f"Balance: {account_info.balance:.2f} {account_info.currency}")

            daily_pnl = executor.get_daily_pnl()
            state.update_pnl(daily_pnl)
            if risk_manager.is_daily_loss_limit_reached(account_info.equity, daily_pnl):
                 state.update_status("Arrêt d'urgence", "Perte journalière max atteinte", is_emergency=True)
                 logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte.")
                 break

            open_positions = executor.get_open_positions(symbol)
            state.update_positions(open_positions)
            if open_positions:
                current_tick = connector.get_tick(symbol)
                if current_tick:
                    risk_manager.manage_open_positions(open_positions, current_tick)
            
            executor.check_for_closed_trades()
            trade_count_since_analysis += executor.get_newly_closed_trades_count()

            if trade_count_since_analysis >= 5:
                analyzer.run_analysis()
                trade_count_since_analysis = 0

            ohlc_data = connector.get_ohlc(symbol, timeframe_str, 200)
            if ohlc_data is None or ohlc_data.empty:
                time.sleep(5)
                continue

            raw_scores = scorer.calculate_all(ohlc_data)
            state.update_scores(raw_scores)
            final_score, trade_direction = aggregator.calculate_final_score(raw_scores)
            
            if final_score > 10: logging.info(f"Analyse: Score={final_score:.2f} | Dir={trade_direction}")

            if final_score >= config['trading_logic']['execution_threshold'] and trade_direction != "NEUTRAL":
                logging.info(f"SEUIL ATTEINT: Score {final_score:.2f} >= {config['trading_logic']['execution_threshold']}.")
                
                is_trade_already_open = any((p.type == 0 and trade_direction == "BUY") or (p.type == 1 and trade_direction == "SELL") for p in open_positions)
                if not is_trade_already_open:
                    # --- NOUVELLE ÉTAPE : CONFIRMATION PAR IA ---
                    ai_approved = True 
                    if config.get('learning', {}).get('ai_confirmation_enabled', False):
                        logging.warning("Consultation de l'IA pour un second avis sur le trade...")
                        signal_context = {
                            "direction": trade_direction, 
                            "confidence": final_score, 
                            "scores": raw_scores
                        }
                        ai_approved, justification = ai_assistant.confirm_trade_with_ai(signal_context)
                        logging.info(f"Avis de l'IA : {justification}")
                    
                    if not ai_approved:
                        logging.warning(f"TRADE ANNULÉ PAR L'IA: Le signal {trade_direction} pour {symbol} a été rejeté.")
                    else:
                        if config['trading_settings']['live_trading_enabled']:
                            executor.execute_trade(account_info, risk_manager, symbol, trade_direction, final_score, raw_scores)
                        else:
                            logging.info(f"SIGNAL (SIMULATION): {trade_direction} @ Score {final_score:.2f} [Validé]")
                else:
                    logging.info(f"Un trade {trade_direction} est déjà ouvert.")

            time.sleep(15)

        except Exception as e:
            logging.error(f"Erreur dans la boucle principale: {e}", exc_info=True)
            time.sleep(30)

    connector.disconnect()
    logging.info("Boucle de trading terminée.")
    state.update_status("Arrêté", "Bot arrêté proprement.", is_emergency=True)

if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    
    config = load_yaml('config.yaml')
    url = f"http://{config['api']['host']}:{config['api']['port']}"
    
    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
    api_thread.start()
    logging.info(f"Interface web démarrée sur {url}")

    threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    main_trading_loop(shared_state)
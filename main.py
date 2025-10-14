# Fichier: main.py

import time
import threading
import logging
import yaml
import webbrowser
import os

from src.data_ingest.mt5_connector import MT5Connector
from src.scorer.strategy_scorer import StrategyScorer
from src.scorer.aggregator import Aggregator
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer
from src.analysis.ai_assistant import AIAssistant

# --- Fonctions utilitaires ---
def setup_logging(state: SharedState):
    """Configure le système de logging."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    
    ui_handler = LogHandler(state)
    ui_handler.setFormatter(log_formatter)
    
    file_handler = logging.FileHandler("trading_bot.log", mode='w', encoding='utf-8')
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

def load_yaml(filepath: str) -> dict:
    """Charge un fichier de configuration YAML de manière sécurisée."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Le fichier '{filepath}' est introuvable. Arrêt.")
        exit()
    except Exception as e:
        logging.critical(f"FATAL: Erreur de lecture de '{filepath}': {e}")
        exit()
    return {}

# --- Fonctions de la boucle principale (refactorisées) ---
def reload_config_if_needed(state: SharedState):
    """Recharge la configuration si un changement a été signalé par l'API."""
    if state.config_changed_flag:
        config = load_yaml('config.yaml')
        state.update_config(config)
        state.clear_config_changed_flag()
        logging.info("Configuration rechargée dynamiquement.")
    return state.get_config(), load_yaml('profiles.yaml')

def check_account_safety(risk_manager: RiskManager, executor: MT5Executor, state: SharedState) -> tuple[bool, dict | None]:
    """Vérifie la connexion et les limites de perte journalière."""
    account_info = executor.get_account_info()
    if not account_info:
        logging.warning("Impossible de récupérer les informations du compte.")
        return False, None

    state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")
    daily_pnl = executor.get_daily_pnl()
    state.update_pnl(daily_pnl)

    if risk_manager.is_daily_loss_limit_reached(account_info.equity, daily_pnl):
        state.update_status("Arrêt d'urgence", "Perte journalière max atteinte.", is_emergency=True)
        logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte.")
        state.shutdown() # Signale l'arrêt
        return False, None
        
    return True, account_info

def manage_open_trades(executor: MT5Executor, risk_manager: RiskManager, symbol: str, magic_number: int, state: SharedState):
    """Gère le BE et le Trailing Stop pour les positions ouvertes."""
    open_positions = executor.get_open_positions(symbol, magic=magic_number)
    state.update_positions(open_positions)
    if open_positions:
        current_tick = executor._mt5.symbol_info_tick(symbol)
        if current_tick:
            risk_manager.manage_open_positions(open_positions, current_tick)
    return open_positions

def evaluate_new_trade_signal(config, final_score, trade_direction, open_positions, magic_number, **kwargs):
    """Évalue si un nouveau trade doit être ouvert."""
    execution_threshold = config['trading_logic']['execution_threshold']
    
    if final_score < 20: return

    logging.info(f"ÉVALUATION DU SIGNAL: Score={final_score:.2f} | Dir={trade_direction} (Seuil: {execution_threshold})")
    
    if final_score >= execution_threshold and trade_direction != "NEUTRAL":
        logging.info(f"CONFIRMATION: Score ({final_score:.2f}) dépasse le seuil.")
        
        is_trade_already_open = any((p.type == 0 and trade_direction == "BUY") or (p.type == 1 and trade_direction == "SELL") for p in open_positions)
        
        if not is_trade_already_open:
            if config.get('learning', {}).get('ai_confirmation_enabled', False):
                ai_assistant = kwargs.get('ai_assistant')
                if ai_assistant:
                    logging.info("Consultation de l'IA pour un second avis...")
                    signal_context = {"direction": trade_direction, "confidence": final_score, "scores": kwargs['raw_scores']}
                    ai_approved, justification = ai_assistant.confirm_trade_with_ai(signal_context)
                    logging.info(f"Avis de l'IA : {justification}")
                    if not ai_approved:
                        logging.warning(f"TRADE REJETÉ PAR L'IA.")
                        return

            if config['trading_settings']['live_trading_enabled']:
                kwargs['executor'].execute_trade(kwargs['account_info'], kwargs['risk_manager'], kwargs['symbol'], trade_direction, final_score, kwargs['raw_scores'], kwargs['ohlc_data'])
            else:
                logging.info(f"ACTION (SIMULATION): Ouverture d'un trade {trade_direction} @ Score {final_score:.2f}")
        else:
            logging.info(f"ACTION IGNORÉE: Un trade (MAGIC: {magic_number}) dans la direction {trade_direction} est déjà ouvert.")
    
    elif trade_direction != "NEUTRAL":
        logging.info(f"ACTION IGNORÉE: Le score ({final_score:.2f}) est inférieur au seuil.")

# --- Boucle de Trading Principale ---
def main_trading_loop(state: SharedState):
    logging.info("Démarrage de la boucle de trading...")
    
    # --- CORRECTION : Charger la config initiale et l'enregistrer dans l'état partagé ---
    initial_config = load_yaml('config.yaml')
    state.update_config(initial_config)
    
    connector = MT5Connector(initial_config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 échouée.", is_emergency=True)
        return

    analyzer = PerformanceAnalyzer('trade_history.csv')
    ai_assistant = AIAssistant()
    trade_count_since_analysis = 0

    while not state.is_shutdown():
        try:
            config, profiles = reload_config_if_needed(state)
            
            active_profile_name = config['trading_logic']['active_profile']
            strategy_weights = profiles.get(active_profile_name, profiles.get('custom', {}))
            magic_number = config.get('trading_settings', {}).get('magic_number', 0)

            executor = MT5Executor(connector.get_connection(), analyzer)
            risk_manager = RiskManager(config['risk_management'], executor, config['trading_settings']['symbol'])

            is_safe, account_info = check_account_safety(risk_manager, executor, state)
            if not is_safe:
                if state.is_shutdown(): break
                time.sleep(10)
                continue

            symbol = config['trading_settings']['symbol']
            open_positions = manage_open_trades(executor, risk_manager, symbol, magic_number, state)
            
            ohlc_data = connector.get_ohlc(symbol, config['trading_settings']['timeframe'], 200)
            if ohlc_data is None or ohlc_data.empty:
                time.sleep(5)
                continue

            scorer = StrategyScorer()
            raw_scores = scorer.calculate_all(ohlc_data)
            state.update_scores(raw_scores)
            
            aggregator = Aggregator(strategy_weights)
            final_score, trade_direction = aggregator.calculate_final_score(raw_scores)
            
            evaluate_new_trade_signal(
                config, final_score, trade_direction, open_positions, magic_number,
                executor=executor, account_info=account_info, risk_manager=risk_manager,
                symbol=symbol, raw_scores=raw_scores, ohlc_data=ohlc_data, ai_assistant=ai_assistant
            )

            time.sleep(15)

        except Exception as e:
            logging.error(f"Erreur majeure dans la boucle principale: {e}", exc_info=True)
            time.sleep(30)

    connector.disconnect()
    logging.info("Boucle de trading terminée.")
    state.update_status("Arrêté", "Bot arrêté proprement.", is_emergency=True)

if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    
    config = load_yaml('config.yaml')
    url = f"http://{config.get('api', {}).get('host', '127.0.0.1')}:{config.get('api', {}).get('port', 5000)}"
    
    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
    api_thread.start()
    logging.info(f"Interface web démarrée sur {url}")

    try: webbrowser.open(url)
    except Exception: logging.warning("Impossible d'ouvrir le navigateur automatiquement.")

    main_trading_loop(shared_state)
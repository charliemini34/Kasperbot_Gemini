# Fichier: main.py

import time
import threading
import logging
import yaml
import webbrowser
import os

from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler

def setup_logging(state: SharedState):
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    ui_handler, file_handler, console_handler = LogHandler(state), logging.FileHandler("trading_bot.log", mode='w'), logging.StreamHandler()
    for handler in [ui_handler, file_handler, console_handler]:
        handler.setFormatter(log_formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        root_logger.addHandler(ui_handler)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable.")
        exit()
    return {}

def main_trading_loop(state: SharedState):
    logging.info("Démarrage de la boucle de trading v6.0 (SMC Engine)...")
    
    initial_config = load_yaml('config.yaml')
    state.update_config(initial_config)
    
    connector = MT5Connector(initial_config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 échouée.", is_emergency=True)
        return

    while not state.is_shutdown():
        try:
            if state.config_changed_flag:
                config = load_yaml('config.yaml')
                state.update_config(config)
                state.clear_config_changed_flag()
                logging.info("Configuration rechargée.")

            config = state.get_config()
            magic_number = config['trading_settings'].get('magic_number', 0)
            symbol = config['trading_settings']['symbol']

            executor = MT5Executor(connector.get_connection())
            risk_manager = RiskManager(config['risk_management'], executor, symbol)
            
            account_info = executor.get_account_info()
            if not account_info:
                time.sleep(10); continue

            state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")
            
            ohlc_data = connector.get_ohlc(symbol, config['trading_settings']['timeframe'], 200)
            if ohlc_data is None or ohlc_data.empty:
                time.sleep(5); continue

            detector = PatternDetector(config)
            trade_signal = detector.detect_patterns(ohlc_data)
            state.update_patterns(detector.get_detected_patterns_info())

            if trade_signal:
                direction = trade_signal['direction']
                pattern_name = trade_signal['pattern']
                logging.info(f"PATTERN DÉTECTÉ: [{pattern_name}] - Direction: {direction}")

                open_positions = executor.get_open_positions(symbol, magic=magic_number)
                is_trade_already_open = any(p.type == (0 if direction == "BUY" else 1) for p in open_positions)

                if not is_trade_already_open:
                    if config['trading_settings']['live_trading_enabled']:
                        executor.execute_trade(account_info, risk_manager, symbol, direction, ohlc_data, pattern_name)
                    else:
                        logging.info(f"ACTION (SIMULATION): Ouverture d'un trade {direction} sur le pattern [{pattern_name}]")
                else:
                    logging.info(f"ACTION IGNORÉE: Un trade (MAGIC: {magic_number}) est déjà ouvert.")
            
            time.sleep(20) # Temps d'attente légèrement augmenté

        except Exception as e:
            logging.error(f"Erreur majeure dans la boucle principale: {e}", exc_info=True)
            time.sleep(30)

    connector.disconnect()
    logging.info("Boucle de trading terminée.")

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
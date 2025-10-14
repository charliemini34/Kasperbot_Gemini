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
    """Configure le système de logging."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    
    ui_handler = LogHandler(state)
    file_handler = logging.FileHandler("trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    
    for handler in [ui_handler, file_handler, console_handler]:
        handler.setFormatter(log_formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        [root_logger.addHandler(h) for h in [ui_handler, file_handler, console_handler]]
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    """Charge un fichier YAML de manière sécurisée."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable. Arrêt.")
        exit()
    return {}

def main_trading_loop(state: SharedState):
    """Boucle principale du bot, v8.1 avec gestion des trades par symbole."""
    logging.info("Démarrage de la boucle de trading v8.1 (SMC Multi-Asset)...")
    
    initial_config = load_yaml('config.yaml')
    state.update_config(initial_config)
    
    connector = MT5Connector(initial_config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 échouée.", is_emergency=True); return

    while not state.is_shutdown():
        try:
            config = state.get_config()
            magic_number = config['trading_settings'].get('magic_number', 0)
            symbols_to_trade = config['trading_settings'].get('symbols', [])
            
            executor = MT5Executor(connector.get_connection())
            account_info = executor.get_account_info()
            if not account_info:
                time.sleep(10); continue
            state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")

            # Gère toutes les positions ouvertes par le bot, tous symboles confondus
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)
            if all_bot_positions:
                for pos in all_bot_positions:
                    # Crée un RiskManager spécifique pour chaque trade à gérer
                    rm_pos = RiskManager(config.get('risk_management', {}), executor, pos.symbol)
                    tick = connector.get_tick(pos.symbol)
                    if tick:
                        rm_pos.manage_open_positions([pos], tick)

            # Boucle d'analyse pour chaque symbole
            for symbol in symbols_to_trade:
                logging.info(f"--- Analyse de {symbol} ---")
                
                # Vérifie s'il y a déjà un trade ouvert pour CE symbole
                is_trade_already_open = any(pos.symbol == symbol for pos in all_bot_positions)
                
                if is_trade_already_open:
                    logging.info(f"Analyse suspendue pour {symbol} : un trade est déjà en cours.")
                    continue

                # Si aucun trade n'est ouvert pour ce symbole, on analyse
                risk_manager = RiskManager(config.get('risk_management', {}), executor, symbol)
                ohlc_data = connector.get_ohlc(symbol, config['trading_settings']['timeframe'], 200)
                if ohlc_data is None or ohlc_data.empty: continue

                detector = PatternDetector(config)
                trade_signal = detector.detect_patterns(ohlc_data)
                state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())

                if trade_signal:
                    direction, pattern_name = trade_signal['direction'], trade_signal['pattern']
                    logging.info(f"PATTERN DÉTECTÉ sur {symbol}: [{pattern_name}] - Direction: {direction}")
                    
                    if config['trading_settings']['live_trading_enabled']:
                        executor.execute_trade(account_info, risk_manager, symbol, direction, ohlc_data, pattern_name)
                    else:
                        logging.info(f"ACTION (SIMULATION) sur {symbol}: Ouverture d'un trade {direction}.")

            time.sleep(20)

        except Exception as e:
            logging.error(f"Erreur majeure dans la boucle principale: {e}", exc_info=True); time.sleep(30)
    
    connector.disconnect()
    logging.info("Boucle de trading terminée.")

if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    config = load_yaml('config.yaml')
    url = f"http://{config.get('api', {}).get('host', '127.0.0.1')}:{config.get('api', {}).get('port', 5000)}"
    
    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True); api_thread.start()
    logging.info(f"Interface web démarrée sur {url}")
    
    try: webbrowser.open(url)
    except Exception: logging.warning("Impossible d'ouvrir le navigateur.")
    
    main_trading_loop(shared_state)
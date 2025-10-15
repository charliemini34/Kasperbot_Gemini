# Fichier: main.py

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, timedelta

from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer

def setup_logging(state: SharedState):
    # ... (cette fonction ne change pas)
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
    # ... (cette fonction ne change pas)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable. Arrêt.")
        exit()
    return {}

def main_trading_loop(state: SharedState):
    """Boucle principale du bot v9.2, avec moteur d'apprentissage intégré."""
    logging.info("Démarrage de la boucle de trading v9.2 (avec Kasper-Learn)...")
    
    initial_config = load_yaml('config.yaml')
    state.update_config(initial_config)
    
    connector = MT5Connector(initial_config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 échouée.", is_emergency=True); return

    executor = MT5Executor(connector.get_connection())
    analyzer = PerformanceAnalyzer(state)
    last_analysis_time = datetime.now()

    while not state.is_shutdown():
        try:
            if not connector.check_connection():
                state.update_status("Déconnecté", "Connexion MT5 perdue...", is_emergency=True)
                if not connector.connect():
                    logging.warning("Échec reconnexion, nouvel essai dans 30s.")
                    time.sleep(30)
                    continue
                else:
                    state.update_status("Connecté", "Reconnexion réussie.", is_emergency=False)

            config = state.get_config()
            magic_number = config['trading_settings'].get('magic_number', 0)
            symbols_to_trade = config['trading_settings'].get('symbols', [])
            timeframe = config['trading_settings'].get('timeframe', 'M15')
            
            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les infos du compte.")
                time.sleep(10)
                continue
            
            state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")

            # Vérifie et archive les trades fermés
            executor.check_for_closed_trades(magic_number)

            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)
            
            if all_bot_positions:
                # ... (logique de gestion de position existante)
                positions_by_symbol = {}
                for pos in all_bot_positions:
                    if pos.symbol not in positions_by_symbol: positions_by_symbol[pos.symbol] = []
                    positions_by_symbol[pos.symbol].append(pos)
                for symbol, positions in positions_by_symbol.items():
                    try:
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None:
                            rm_pos = RiskManager(config, executor, symbol)
                            rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                    except ValueError as e:
                        logging.warning(f"Impossible de gérer les positions sur {symbol}: {e}")

            for symbol in symbols_to_trade:
                # ... (logique de détection de pattern existante)
                logging.info(f"--- Analyse de {symbol} ---")
                if any(pos.symbol == symbol for pos in all_bot_positions):
                    logging.info(f"Analyse suspendue pour {symbol} : un trade est déjà en cours.")
                    continue
                try:
                    risk_manager = RiskManager(config, executor, symbol)
                except ValueError as e:
                    logging.error(f"Init RiskManager échouée pour {symbol}: {e}.")
                    continue
                ohlc_data = connector.get_ohlc(symbol, timeframe, 200)
                if ohlc_data is None or ohlc_data.empty:
                    logging.warning(f"Aucune donnée OHLC pour {symbol}.")
                    continue
                detector = PatternDetector(config)
                trade_signal = detector.detect_patterns(ohlc_data)
                state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())
                if trade_signal:
                    direction, pattern_name = trade_signal['direction'], trade_signal['pattern']
                    logging.info(f"PATTERN DÉTECTÉ sur {symbol}: [{pattern_name}] - Direction: {direction}")
                    if config['trading_settings']['live_trading_enabled']:
                        executor.execute_trade(account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number)
                    else:
                        logging.info(f"ACTION (SIMULATION) sur {symbol}: Ouverture d'un trade {direction}.")
            
            # --- MOTEUR D'APPRENTISSAGE ---
            analysis_period = timedelta(hours=config.get('learning', {}).get('analysis_period_hours', 1))
            if datetime.now() - last_analysis_time > analysis_period:
                analyzer.run_analysis()
                last_analysis_time = datetime.now()

            time.sleep(20)

        except Exception as e:
            logging.error(f"Erreur majeure dans la boucle principale: {e}", exc_info=True)
            time.sleep(30)
    
    connector.disconnect()
    logging.info("Boucle de trading terminée.")

if __name__ == "__main__":
    # ... (cette partie ne change pas)
    shared_state = SharedState()
    setup_logging(shared_state)
    config = load_yaml('config.yaml')
    url = f"http://{config.get('api', {}).get('host', '127.0.0.1')}:{config.get('api', {}).get('port', 5000)}"
    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
    api_thread.start()
    logging.info(f"Interface web démarrée sur {url}")
    try: webbrowser.open(url)
    except Exception: logging.warning("Impossible d'ouvrir le navigateur.")
    main_trading_loop(shared_state)
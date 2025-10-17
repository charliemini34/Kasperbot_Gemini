# Fichier: main.py
# Version: 13.0.2 (Guardian+ Stable)
# Dépendances: MetaTrader5, pytz, PyYAML, Flask
# Description: Version stable avec gestion des positions et disjoncteur restaurés.

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, timedelta
import pytz

import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer

def setup_logging(state: SharedState):
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    ui_handler = LogHandler(state)
    if not os.path.exists('logs'): os.makedirs('logs')
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    for handler in [ui_handler, file_handler, console_handler]:
        handler.setFormatter(log_formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        [root_logger.addHandler(h) for h in [ui_handler, file_handler, console_handler]]
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    try:
        with open(filepath, 'r', encoding='utf-8') as f: return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable. Arrêt.")
        exit()
    return {}

def get_timeframe_seconds(timeframe_str: str) -> int:
    if 'M' in timeframe_str: return int(timeframe_str.replace('M', '')) * 60
    elif 'H' in timeframe_str: return int(timeframe_str.replace('H', '')) * 3600
    elif 'D' in timeframe_str: return int(timeframe_str.replace('D', '')) * 86400
    return 60

def main_trading_loop(state: SharedState):
    logging.info("Démarrage de la boucle de trading v13.2 (Guardian+ Stable)...")
    config = load_yaml('config.yaml')
    state.update_config(config)
    
    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion MT5 initiale échouée.", is_emergency=True); return

    executor = MT5Executor(connector.get_connection())
    analyzer = PerformanceAnalyzer(state)
    last_analysis_time = datetime.now()

    while not state.is_shutdown():
        try:
            if not connector.check_connection():
                state.update_status("Déconnecté", "Connexion MT5 perdue...", is_emergency=True)
                if not connector.connect(): time.sleep(30); continue
                state.update_status("Connecté", "Reconnexion MT5 réussie.")

            config = state.get_config()
            timeframe = config['trading_settings'].get('timeframe', 'M15')
            magic_number = config['trading_settings'].get('magic_number', 0)
            
            account_info = executor.get_account_info()
            if not account_info: time.sleep(10); continue
            
            state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")
            executor.check_for_closed_trades(magic_number)
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)
            
            if all_bot_positions:
                positions_by_symbol = {}
                for pos in all_bot_positions:
                    if pos.symbol not in positions_by_symbol: positions_by_symbol[pos.symbol] = []
                    positions_by_symbol[pos.symbol].append(pos)
                
                for symbol, positions in positions_by_symbol.items():
                    try:
                        rm_pos = RiskManager(config, executor, symbol)
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None:
                            rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                    except ValueError as e:
                        logging.warning(f"Impossible de gérer les positions sur {symbol}: {e}")

            symbols_to_trade = config['trading_settings'].get('symbols', [])
            if symbols_to_trade:
                main_rm = RiskManager(config, executor, symbols_to_trade[0])
                limit_reached, _ = main_rm.is_daily_loss_limit_reached()
                if limit_reached:
                    state.update_status("Arrêt d'Urgence", "Perte journalière max atteinte", is_emergency=True)
                    time.sleep(60); continue

            for symbol in symbols_to_trade:
                if any(pos.symbol == symbol for pos in all_bot_positions):
                    continue
                try:
                    risk_manager = RiskManager(config, executor, symbol)
                    ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
                    if ohlc_data is None or ohlc_data.empty:
                        continue
                    detector = PatternDetector(config)
                    trade_signal = detector.detect_patterns(ohlc_data, connector, symbol)
                    state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())
                    if trade_signal:
                        direction, pattern_name = trade_signal['direction'], trade_signal['pattern']
                        logging.info(f"PATTERN DÉTECTÉ sur {symbol}: [{pattern_name}] - Direction: {direction}")
                        volume, sl, tp = risk_manager.calculate_trade_parameters(account_info.equity, ohlc_data['close'].iloc[-1], direction, ohlc_data)
                        if volume > 0:
                            executor.execute_trade(account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number, "trend", 0.0) # Dummy values for now
                except Exception as e:
                    logging.error(f"Erreur d'analyse sur {symbol}: {e}")
            
            timeframe_seconds = get_timeframe_seconds(timeframe)
            now_utc = datetime.now(pytz.utc)
            time_since_epoch = now_utc.timestamp()
            next_candle_epoch = (time_since_epoch // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(0, (next_candle_epoch - time_since_epoch))
            logging.info(f"Synchronisation... Prochaine analyse dans {sleep_duration:.0f} secondes.")
            time.sleep(sleep_duration)

        except ConnectionError as conn_err:
            logging.error(f"Erreur de connexion MT5: {conn_err}", exc_info=False)
            state.update_status("Erreur Connexion", str(conn_err), is_emergency=True)
            time.sleep(20)
        except Exception as e:
            logging.critical(f"Erreur majeure non gérée dans la boucle principale: {e}", exc_info=True)
            state.update_status("ERREUR CRITIQUE", str(e), is_emergency=True)
            time.sleep(60)
    
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
    except Exception: logging.warning("Impossible d'ouvrir le navigateur.")
    main_trading_loop(shared_state)
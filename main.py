# Fichier: main.py
# Version 9.4.1 (Correctif Interface) par votre Partenaire de Code

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
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable. Arrêt.")
        exit()
    return {}

def get_timeframe_seconds(timeframe_str: str) -> int:
    """Convertit une chaîne de timeframe (ex: 'M15') en secondes."""
    if 'M' in timeframe_str:
        return int(timeframe_str.replace('M', '')) * 60
    elif 'H' in timeframe_str:
        return int(timeframe_str.replace('H', '')) * 3600
    elif 'D' in timeframe_str:
        return int(timeframe_str.replace('D', '')) * 86400
    return 60 # Par défaut, 1 minute

def main_trading_loop(state: SharedState):
    """Boucle principale du bot v9.4.1, synchronisée sur les bougies et avec UI corrigée."""
    logging.info("Démarrage de la boucle de trading v9.4.1 (Kasper-Enhanced)...")
    
    config = load_yaml('config.yaml')
    state.update_config(config)
    
    connector = MT5Connector(config['mt5_credentials'])
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
                state.update_status("Connecté", "Reconnexion réussie.", is_emergency=False)

            config = state.get_config()
            magic_number = config['trading_settings'].get('magic_number', 0)
            symbols_to_trade = config['trading_settings'].get('symbols', [])
            timeframe = config['trading_settings'].get('timeframe', 'M15')
            is_verbose = config.get('logging', {}).get('verbose_log', True)
            
            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les infos du compte.")
                time.sleep(10)
                continue
            
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
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None:
                            rm_pos = RiskManager(config, executor, symbol)
                            rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                    except ValueError as e:
                        logging.warning(f"Impossible de gérer les positions sur {symbol}: {e}")

            for symbol in symbols_to_trade:
                if is_verbose: logging.info(f"--- Analyse de {symbol} ---")
                
                # CORRECTION UI : On initialise le statut du symbole pour qu'il s'affiche toujours
                state.update_symbol_patterns(symbol, {})

                if any(pos.symbol == symbol for pos in all_bot_positions):
                    if is_verbose: logging.info(f"Analyse suspendue pour {symbol} : un trade est déjà en cours.")
                    # On met à jour l'état pour que l'interface affiche l'information
                    state.update_symbol_patterns(symbol, {'STATUT': {'status': 'Trade en cours'}})
                    continue
                
                try:
                    risk_manager = RiskManager(config, executor, symbol)
                    ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
                    if ohlc_data is None or ohlc_data.empty:
                        if is_verbose: logging.warning(f"Aucune donnée OHLC pour {symbol}.")
                        state.update_symbol_patterns(symbol, {'STATUT': {'status': 'Données indisponibles'}})
                        continue

                    detector = PatternDetector(config)
                    trade_signal = detector.detect_patterns(ohlc_data, connector, symbol)
                    state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())
                    
                    if trade_signal:
                        direction, pattern_name = trade_signal['direction'], trade_signal['pattern']
                        logging.info(f"PATTERN DÉTECTÉ sur {symbol}: [{pattern_name}] - Direction: {direction}")
                        
                        trend_info = detector.get_detected_patterns_info().get('TREND_FILTER', {})
                        market_trend = trend_info.get('status', 'Indéterminée')
                        volatility_atr = risk_manager.calculate_atr(ohlc_data, 14)

                        executor.execute_trade(
                            account_info, risk_manager, symbol, direction, ohlc_data, 
                            pattern_name, magic_number, market_trend, volatility_atr
                        )
                except ValueError as e:
                    logging.error(f"Erreur d'initialisation pour {symbol}: {e}.")
                    state.update_symbol_patterns(symbol, {'STATUT': {'status': 'Erreur RiskManager'}})
                    continue
            
            analysis_period = timedelta(hours=config.get('learning', {}).get('analysis_period_hours', 1))
            if datetime.now() - last_analysis_time > analysis_period:
                analyzer.run_analysis()
                last_analysis_time = datetime.now()

            sync_mode = config.get('timing', {}).get('sync_mode', 'new_candle')
            if sync_mode == 'new_candle':
                timeframe_seconds = get_timeframe_seconds(timeframe)
                now_utc = datetime.now(pytz.utc)
                time_since_epoch = now_utc.timestamp()
                next_candle_epoch = (time_since_epoch // timeframe_seconds + 1) * timeframe_seconds
                sleep_duration = next_candle_epoch - time_since_epoch
                sleep_duration = max(1, sleep_duration + 2) 
                logging.info(f"Synchronisation... Prochaine analyse dans {sleep_duration:.0f} secondes.")
                time.sleep(sleep_duration)
            else:
                sleep_interval = config.get('timing', {}).get('sleep_interval_seconds', 20)
                time.sleep(sleep_interval)

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
    except Exception: logging.warning("Impossible d'ouvrir le navigateur.")
    main_trading_loop(shared_state)
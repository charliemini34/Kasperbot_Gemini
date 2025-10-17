# Fichier: main.py
# Version: 17.0.1 (SMC-Targeting-Fix)
# Dépendances: MetaTrader5, pytz, PyYAML, Flask
# Description: Corrige l'appel à calculate_trade_parameters.

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, time as dt_time
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
    """Configure la journalisation pour la console, les fichiers et l'interface utilisateur."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    if not os.path.exists('logs'):
        os.makedirs('logs')
        
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    ui_handler = LogHandler(state)

    for handler in [file_handler, console_handler, ui_handler]:
        handler.setFormatter(log_formatter)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(ui_handler)

    logging.getLogger('werkzeug').setLevel(logging.ERROR)


def load_yaml(filepath: str) -> dict:
    """Charge un fichier de configuration YAML de manière sécurisée."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable. Le programme va s'arrêter.")
        exit()
    except yaml.YAMLError as e:
        logging.critical(f"FATAL: Erreur de syntaxe dans le fichier '{filepath}': {e}. Le programme va s'arrêter.")
        exit()
    return {}

def get_timeframe_seconds(timeframe_str: str) -> int:
    """Convertit une chaîne de caractères de timeframe (ex: 'M15') en secondes."""
    if 'M' in timeframe_str: return int(timeframe_str.replace('M', '')) * 60
    if 'H' in timeframe_str: return int(timeframe_str.replace('H', '')) * 3600
    if 'D' in timeframe_str: return int(timeframe_str.replace('D', '')) * 86400
    logging.warning(f"Timeframe inconnu '{timeframe_str}', utilisation de 60 secondes par défaut.")
    return 60

def validate_symbols(symbols_list, mt5_connection):
    """Vérifie que les symboles sont disponibles sur la plateforme MT5."""
    valid_symbols = []
    for symbol in symbols_list:
        if mt5_connection.symbol_info(symbol):
            valid_symbols.append(symbol)
        else:
            logging.error(f"Le symbole '{symbol}' n'est pas disponible ou est mal orthographié. Il sera ignoré.")
    return valid_symbols

def is_within_trading_session(symbol: str, config: dict) -> bool:
    """Vérifie si le symbole peut être tradé à l'heure UTC actuelle."""
    sessions_config = config.get('trading_settings', {}).get('trading_sessions', [])
    crypto_symbols = config.get('trading_settings', {}).get('crypto_symbols', [])

    if symbol in crypto_symbols:
        return True

    if not sessions_config:
        return True

    now_utc = datetime.now(pytz.utc)
    current_weekday = (now_utc.weekday() + 1) % 7
    current_time = now_utc.time()

    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str)
            start_time = dt_time.fromisoformat(start_str)
            end_time = dt_time.fromisoformat(end_str)

            if day == current_weekday and start_time <= current_time < end_time:
                return True
        except (ValueError, TypeError):
            logging.error(f"Format de session invalide: '{session}'")
            continue
    return False

def main_trading_loop(state: SharedState):
    """Boucle principale qui orchestre le bot de trading."""
    logging.info("Démarrage de la boucle de trading v17.0.1 (SMC-Targeting-Fix)...")
    config = load_yaml('config.yaml')
    state.update_config(config)
    
    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "La connexion initiale à MT5 a échoué.", is_emergency=True)
        return

    executor = MT5Executor(connector.get_connection(), config)
    analyzer = PerformanceAnalyzer(state)

    symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
    if not symbols_to_trade:
        logging.critical("Aucun symbole valide à trader. Arrêt du bot.")
        state.update_status("Arrêté", "Aucun symbole valide.", is_emergency=True)
        return

    is_first_cycle = True
    
    while not state.is_shutdown():
        try:
            if not connector.check_connection():
                state.update_status("Déconnecté", "Connexion MT5 perdue. Tentative de reconnexion...", is_emergency=True)
                if not connector.connect():
                    time.sleep(20)
                    continue
                state.update_status("Connecté", "Reconnexion à MT5 réussie.")

            if state.config_changed_flag:
                logging.info("Changement de configuration détecté. Rechargement...")
                config = load_yaml('config.yaml')
                state.update_config(config)
                executor = MT5Executor(connector.get_connection(), config)
                symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
                state.clear_config_changed_flag()
                logging.info("Configuration rechargée.")

            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les informations du compte.")
                time.sleep(10)
                continue
            
            state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")
            
            magic_number = config['trading_settings'].get('magic_number', 0)
            executor.check_for_closed_trades(magic_number)
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)
            
            if all_bot_positions:
                positions_by_symbol = {}
                for pos in all_bot_positions:
                    positions_by_symbol.setdefault(pos.symbol, []).append(pos)
                
                for symbol, positions in positions_by_symbol.items():
                    try:
                        rm_pos = RiskManager(config, executor, symbol)
                        timeframe = config['trading_settings'].get('timeframe', 'M15')
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None:
                            rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                    except ValueError as e:
                        logging.error(f"Erreur de validation pour la gestion des positions sur {symbol}: {e}")
                    except Exception as e:
                        logging.error(f"Erreur lors de la gestion des positions sur {symbol}: {e}", exc_info=True)

            if symbols_to_trade:
                try:
                    main_rm = RiskManager(config, executor, symbols_to_trade[0])
                    limit_reached, _ = main_rm.is_daily_loss_limit_reached()
                    if limit_reached:
                        state.update_status("Arrêt d'Urgence", "Limite de perte journalière atteinte.", is_emergency=True)
                        time.sleep(60)
                        continue
                except ValueError:
                    pass
            
            if is_first_cycle:
                logging.info("Premier cycle d'analyse : le trading est désactivé pour synchronisation.")

            for symbol in symbols_to_trade:
                try:
                    if not is_within_trading_session(symbol, config):
                        continue

                    if any(pos.symbol == symbol for pos in all_bot_positions):
                        continue
                    
                    risk_manager = RiskManager(config, executor, symbol)
                    timeframe = config['trading_settings'].get('timeframe', 'M15')
                    ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
                    
                    if ohlc_data is None or ohlc_data.empty:
                        continue

                    detector = PatternDetector(config)
                    trade_signal = detector.detect_patterns(ohlc_data, connector, symbol)
                    state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())

                    if trade_signal and not is_first_cycle:
                        logging.info(f"SIGNAL VALIDE sur {symbol}: [{trade_signal['pattern']}] en direction de {trade_signal['direction']}.")
                        
                        volume, sl, tp = risk_manager.calculate_trade_parameters(
                            account_info.equity, ohlc_data['close'].iloc[-1], ohlc_data, trade_signal
                        )
                        
                        if volume > 0:
                            executor.execute_trade(
                                account_info, risk_manager, symbol, trade_signal['direction'], ohlc_data, 
                                trade_signal['pattern'], magic_number
                            )
                        else:
                            logging.warning(f"Le volume calculé pour {symbol} est de 0 ou SL/TP invalide. Le trade est annulé.")
                
                except ValueError as e:
                    logging.error(f"Impossible de traiter le symbole '{symbol}': {e}.")
                except Exception as e:
                    logging.error(f"Erreur d'analyse sur {symbol}: {e}", exc_info=True)
            
            timeframe_str = config['trading_settings'].get('timeframe', 'M15')
            timeframe_seconds = get_timeframe_seconds(timeframe_str)
            now_utc = datetime.now(pytz.utc).timestamp()
            next_candle_epoch = (now_utc // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(1, next_candle_epoch - now_utc)
            
            if is_first_cycle:
                logging.info("Fin du cycle de synchronisation. Le trading sera activé au prochain cycle.")
                is_first_cycle = False
            
            logging.info(f"Cycle terminé. Attente de {sleep_duration:.0f} secondes.")
            time.sleep(sleep_duration)

        except (ConnectionError, BrokenPipeError) as e:
            logging.error(f"Erreur de connexion critique: {e}", exc_info=True)
            state.update_status("Déconnecté", f"Erreur de connexion: {e}", is_emergency=True)
            time.sleep(30)
        except Exception as e:
            logging.critical(f"ERREUR CRITIQUE non gérée dans la boucle principale: {e}", exc_info=True)
            state.update_status("ERREUR CRITIQUE", str(e), is_emergency=True)
            time.sleep(60)
    
    connector.disconnect()
    logging.info("Boucle de trading terminée proprement.")

if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    config = load_yaml('config.yaml')
    
    host = config.get('api', {}).get('host', '127.0.0.1')
    port = config.get('api', {}).get('port', 5000)
    url = f"http://{host}:{port}"

    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
    api_thread.start()
    logging.info(f"L'interface web a démarré sur {url}")
    
    try:
        webbrowser.open(url)
    except Exception:
        logging.warning("Impossible d'ouvrir le navigateur web automatiquement.")
        
    main_trading_loop(shared_state)
# Fichier: main.py
# Version: 17.0.7 (Digits-Inject & Log-Fix)
# Dépendances: MetaTrader5, pytz, PyYAML, Flask, playsound, time, threading, logging, webbrowser, os, datetime
# Description: Injecte 'digits' dans PatternDetector et corrige log version.

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, time as dt_time
import pytz
try:
    from playsound import playsound
except ImportError:
    logging.warning("Bibliothèque 'playsound' non trouvée. Alertes sonores désactivées.")
    playsound = None

import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer

# --- MODIFICATION : Définir la version ici ---
BOT_VERSION = "v19.0.4-patch" # Basé sur la version de pattern_detector corrigée
# --- FIN MODIFICATION ---

def setup_logging(state: SharedState):
    # ... (inchangé) ...
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    if not os.path.exists('logs'): os.makedirs('logs')
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    ui_handler = LogHandler(state)
    for handler in [file_handler, console_handler, ui_handler]: handler.setFormatter(log_formatter)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(ui_handler)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    # ... (inchangé) ...
    try:
        with open(filepath, 'r', encoding='utf-8') as f: return yaml.safe_load(f)
    except FileNotFoundError: logging.critical(f"FATAL: Fichier config '{filepath}' introuvable."); exit()
    except yaml.YAMLError as e: logging.critical(f"FATAL: Erreur YAML dans '{filepath}': {e}."); exit()
    return {}

def get_timeframe_seconds(timeframe_str: str) -> int:
    # ... (inchangé) ...
    if 'M' in timeframe_str: return int(timeframe_str.replace('M', '')) * 60
    if 'H' in timeframe_str: return int(timeframe_str.replace('H', '')) * 3600
    if 'D' in timeframe_str: return int(timeframe_str.replace('D', '')) * 86400
    logging.warning(f"Timeframe '{timeframe_str}' non reconnu, utilisation 60s."); return 60

def validate_symbols(symbols_list, mt5_connection):
    # ... (inchangé) ...
    valid_symbols = []
    if not symbols_list: logging.warning("Liste symboles config vide."); return []
    for symbol in symbols_list:
        if mt5_connection.symbol_info(symbol): valid_symbols.append(symbol)
        else: logging.error(f"Symbole '{symbol}' invalide/indisponible. Ignoré.")
    return valid_symbols

def is_within_trading_session(symbol: str, config: dict) -> bool:
    # ... (inchangé) ...
    sessions_config = config.get('trading_settings', {}).get('trading_sessions', [])
    crypto_symbols = config.get('trading_settings', {}).get('crypto_symbols', [])
    if symbol in crypto_symbols: return True
    if not sessions_config: return True
    now_utc = datetime.now(pytz.utc)
    current_weekday_config_format = now_utc.weekday() + 1
    current_time = now_utc.time()
    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str)
            start_time = dt_time.fromisoformat(start_str)
            end_time = dt_time.fromisoformat(end_str)
            if day == current_weekday_config_format and start_time <= current_time < end_time:
                return True
        except (ValueError, TypeError) as e: logging.error(f"Format session invalide: '{session}'. Erreur: {e}"); continue
    return False

def play_alert_sound(config: dict):
    # ... (inchangé) ...
    if playsound is None: return
    sound_config = config.get('sound_alerts', {})
    if sound_config.get('enabled', False):
        sound_file = sound_config.get('sound_file', './alert.wav')
        if os.path.exists(sound_file):
            try:
                sound_thread = threading.Thread(target=playsound, args=(sound_file,), daemon=True)
                sound_thread.start()
                logging.info("Alerte sonore jouée.")
            except Exception as e: logging.error(f"Impossible de jouer son '{sound_file}': {e}")
        else: logging.warning(f"Fichier son '{sound_file}' introuvable.")

# --- Fonctions refactorisées (inchangées) ---

def check_connection_and_config(state: SharedState, connector: MT5Connector, executor: MT5Executor) -> tuple:
    # ... (inchangé) ...
    config = state.get_config()
    symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
    if not connector.check_connection():
        state.update_status("Déconnecté", "Connexion MT5 perdue...", is_emergency=True)
        if not connector.connect():
            time.sleep(20)
            return None, None, None
        state.update_status("Connecté", "Reconnexion MT5 OK.")
        symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
    if state.config_changed_flag:
        logging.info("Rechargement configuration...")
        config = load_yaml('config.yaml')
        state.update_config(config)
        executor = MT5Executor(connector.get_connection(), config)
        symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
        state.clear_config_changed_flag()
        logging.info("Configuration rechargée.")
        if not symbols_to_trade:
             logging.critical("Aucun symbole valide après rechargement.")
             state.update_status("Erreur Config", "Aucun symbole valide.", is_emergency=True)
    return config, executor, symbols_to_trade

def process_open_positions(state: SharedState, config: dict, connector: MT5Connector, executor: MT5Executor):
    # ... (inchangé) ...
    magic_number = config['trading_settings'].get('magic_number', 0)
    executor.check_for_closed_trades(magic_number)
    all_bot_positions = executor.get_open_positions(magic=magic_number)
    state.update_positions(all_bot_positions)
    if not all_bot_positions: return
    positions_by_symbol = {}
    for pos in all_bot_positions: positions_by_symbol.setdefault(pos.symbol, []).append(pos)
    for symbol, positions in positions_by_symbol.items():
        try:
            rm_pos = RiskManager(config, executor, symbol)
            timeframe = config['trading_settings'].get('timeframe', 'M15')
            ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
            tick = connector.get_tick(symbol)
            if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty:
                rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
            else: logging.warning(f"Données manquantes (tick/OHLC) gestion pos {symbol}.")
        except ValueError as e: logging.error(f"Erreur init RiskManager gestion pos {symbol}: {e}")
        except Exception as e: logging.error(f"Erreur gestion pos {symbol}: {e}", exc_info=True)

def check_risk_limits(state: SharedState, config: dict, executor: MT5Executor, symbols_to_trade: list) -> bool:
    # ... (inchangé) ...
    if not symbols_to_trade: return False
    try:
        main_rm = RiskManager(config, executor, symbols_to_trade[0])
        # --- MODIFICATION : Appel fonction corrigée ---
        # (La v18.1.9 de risk_manager a 'is_daily_loss_limit_reached')
        limit_reached, _ = main_rm.is_daily_loss_limit_reached()
        # --- FIN MODIFICATION ---
        if limit_reached:
            state.update_status("Arrêt Urgence", "Limite perte jour atteinte.", is_emergency=True)
            return True
    except ValueError as e: logging.error(f"Erreur init RiskManager check limite perte: {e}")
    except Exception as e: logging.error(f"Erreur check limite perte: {e}", exc_info=True)
    return False

# --- MODIFICATION : analyze_and_trade_symbol ---
def analyze_and_trade_symbol(symbol: str, state: SharedState, config: dict, connector: MT5Connector, executor: MT5Executor, account_info, is_first_cycle: bool):
    """Analyse un symbole et exécute un trade."""
    magic_number = config['trading_settings'].get('magic_number', 0)
    
    try:
        all_bot_positions = executor.get_open_positions(magic=magic_number)
        if any(pos.symbol == symbol for pos in all_bot_positions):
            return
    except Exception as e:
         logging.error(f"Erreur get_open_positions dans analyze_symbol {symbol}: {e}")
         return

    if not is_within_trading_session(symbol, config): return

    try:
        risk_manager = RiskManager(config, executor, symbol)
        
        timeframe = config['trading_settings'].get('timeframe', 'M15')
        ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < 50:
            logging.warning(f"Données OHLC insuffisantes {symbol} {timeframe}.")
            state.update_symbol_patterns(symbol, {})
            return

        # --- MODIFICATION : Passer 'digits' au PatternDetector ---
        detector_instance = PatternDetector(config, risk_manager.digits)
        # --- FIN MODIFICATION ---
        
        trade_signal = detector_instance.detect_patterns(ohlc_data, connector, symbol)
        state.update_symbol_patterns(symbol, detector_instance.get_detected_patterns_info())

        if trade_signal and not is_first_cycle:
            logging.info(f"SIGNAL VALIDE sur {symbol}: [{trade_signal['pattern']}] en direction de {trade_signal['direction']}.")
            play_alert_sound(config)

            last_close_price = ohlc_data['close'].iloc[-1]
            
            volume, sl, tp = risk_manager.calculate_trade_parameters(
                account_info.equity, last_close_price, ohlc_data, trade_signal
            )

            if volume > 0 and sl > 0 and tp > 0:
                if config['trading_settings'].get('live_trading_enabled', False):
                    logging.info(f"Exécution ordre LIVE: {trade_signal['direction']} {volume:.4f} lots {symbol}")
                    executor.execute_trade(
                        account_info, risk_manager, symbol, trade_signal['direction'],
                        volume, sl, tp,
                        trade_signal['pattern'], magic_number
                    )
                else:
                    logging.info(f"DRY RUN: Ordre {trade_signal['direction']} {volume:.4f} lots {symbol} @ ~{last_close_price:.{risk_manager.digits}f} (SL={sl:.{risk_manager.digits}f}, TP={tp:.{risk_manager.digits}f}) non envoyé. Pattern: {trade_signal['pattern']}")
            else:
                logging.warning(f"Volume calculé ({volume}) ou SL/TP ({sl}/{tp}) invalide pour {symbol}. Trade basé sur signal [{trade_signal['pattern']}] annulé.")

    except ValueError as e: logging.error(f"Impossible de traiter '{symbol}': {e}.")
    except Exception as e: logging.error(f"Erreur analyse {symbol}: {e}", exc_info=True)


def wait_for_next_candle(config: dict) -> float:
    # ... (inchangé) ...
    timeframe_str = config['trading_settings'].get('timeframe', 'M15')
    timeframe_seconds = get_timeframe_seconds(timeframe_str)
    now_utc_ts = datetime.now(pytz.utc).timestamp()
    next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
    sleep_duration = max(1.0, next_candle_epoch - now_utc_ts)
    logging.info(f"Cycle terminé. Attente de {sleep_duration:.1f} secondes jusqu'à prochaine bougie {timeframe_str}.")
    time.sleep(sleep_duration)
    return sleep_duration

# --- Boucle principale (inchangée) ---
def main_trading_loop(state: SharedState):
    # --- MODIFICATION : Log de version corrigé ---
    logging.info(f"Démarrage de la boucle de trading {BOT_VERSION}...")
    # --- FIN MODIFICATION ---
    
    is_first_cycle = True
    config = None; connector = None; executor = None; symbols_to_trade = []

    try:
        config = load_yaml('config.yaml')
        state.update_config(config)
        connector = MT5Connector(config['mt5_credentials'])
        if not connector.connect():
            state.update_status("Déconnecté", "Connexion initiale MT5 échouée.", is_emergency=True); return

        executor = MT5Executor(connector.get_connection(), config)
        # analyzer = PerformanceAnalyzer(state) # Optionnel

        while not state.is_shutdown():
            try:
                config, executor, symbols_to_trade = check_connection_and_config(state, connector, executor)
                if config is None: continue
                if not symbols_to_trade:
                     logging.warning("Aucun symbole valide à trader.")
                     time.sleep(30); continue

                account_info = executor.get_account_info()
                if not account_info:
                    logging.warning("Infos compte MT5 inaccessibles."); time.sleep(10); continue
                state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

                process_open_positions(state, config, connector, executor)

                if check_risk_limits(state, config, executor, symbols_to_trade):
                    time.sleep(60); continue

                if is_first_cycle:
                    logging.info("Premier cycle terminé: Synchro effectuée. Trading activé.")
                    is_first_cycle = False
                else:
                    for symbol in symbols_to_trade:
                        analyze_and_trade_symbol(symbol, state, config, connector, executor, account_info, is_first_cycle)

                wait_for_next_candle(config)

            except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err:
                logging.error(f"Erreur connexion MT5 critique: {conn_err}", exc_info=False)
                state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", is_emergency=True)
                time.sleep(30)
            except Exception as loop_err:
                logging.critical(f"ERREUR CRITIQUE boucle: {loop_err}", exc_info=True)
                state.update_status("ERREUR CRITIQUE", str(loop_err), is_emergency=True)
                time.sleep(60)

    except KeyboardInterrupt:
        logging.info("Arrêt manuel (Ctrl+C).")
        state.shutdown()
    except Exception as startup_err:
        logging.critical(f"ERREUR FATALE démarrage: {startup_err}", exc_info=True)
        state.update_status("ERREUR FATALE", str(startup_err), is_emergency=True)
        if connector: connector.disconnect()
    finally:
        logging.info("Arrêt boucle trading...")
        if connector: connector.disconnect()
        logging.info("Connexion MT5 fermée.")
        state.update_status("Arrêté", "Bot arrêté.")


if __name__ == "__main__":
    # ... (inchangé) ...
    shared_state = SharedState()
    setup_logging(shared_state)
    try:
        config = load_yaml('config.yaml')
        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        url = f"http://{host}:{port}"
        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
        api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")
        try: threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception as e: logging.warning(f"Impossible ouvrir navigateur auto: {e}")
        main_trading_loop(shared_state)
    except Exception as e:
        logging.critical(f"Erreur __main__: {e}", exc_info=True)
        if 'shared_state' in locals(): shared_state.update_status("ERREUR MAIN", str(e), is_emergency=True)
        time.sleep(5)
    logging.info("Programme principal terminé.")
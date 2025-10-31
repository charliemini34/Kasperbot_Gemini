# Fichier: main.py
# Version: 20.0.0 (SMC Integration)
# Dépendances: MetaTrader5, pytz, PyYAML, Flask
# Description: Modifié pour charger les données MTF et appeler le PatternDetector SMC.

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, timedelta, time as dt_time
import pytz

import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer
from src.constants import BUY, SELL

# --- Fonctions setup_logging, load_yaml, get_timeframe_seconds, validate_symbols, is_within_trading_session ---
# --- (INCHANGÉES PAR RAPPORT À VOTRE v19.1.1) ---

def setup_logging(state: SharedState):
    """Configure la journalisation pour la console, les fichiers et l'interface utilisateur."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    if not os.path.exists('logs'):
        os.makedirs('logs')

    log_level = logging.DEBUG if os.environ.get("KASPERBOT_DEBUG") else logging.INFO

    file_handler = logging.FileHandler("logs/trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    ui_handler = LogHandler(state)

    for handler in [file_handler, console_handler, ui_handler]:
        handler.setFormatter(log_formatter)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.setLevel(log_level) # Utiliser le niveau de log défini
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(ui_handler)
        logging.info(f"Niveau de logging réglé sur: {logging.getLevelName(root_logger.level)}")

    logging.getLogger('werkzeug').setLevel(logging.ERROR) # Garder Flask silencieux

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
    current_weekday_config_format = now_utc.isoweekday()
    current_time = now_utc.time()

    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str)
            start_time = dt_time.fromisoformat(start_str)
            end_time = dt_time.fromisoformat(end_str)

            if day == current_weekday_config_format and start_time <= current_time < end_time:
                return True
        except (ValueError, TypeError):
            logging.error(f"Format de session invalide: '{session}'")
            continue
    return False

# --- Fonction manage_pending_orders (INCHANGÉE PAR RAPPORT À VOTRE v19.1.1) ---
def manage_pending_orders(state: SharedState, executor: MT5Executor, config: dict):
    magic_number = config['trading_settings'].get('magic_number', 0)
    
    # (Fix J.9/R7) Utiliser 'trading_settings' pour 'pending_order_expiry_candles' (logique v19.0.0 originale)
    pending_cfg_trading = config.get('trading_settings', {})
    cancel_after_candles = pending_cfg_trading.get('pending_order_expiry_candles', 5) 
    
    timeframe_seconds = get_timeframe_seconds(config['trading_settings'].get('timeframe', 'M15'))
    cancel_after_seconds = cancel_after_candles * timeframe_seconds

    try:
        pending_orders = executor.get_pending_orders(magic=magic_number)
        state.update_pending_orders(pending_orders) # Mettre à jour l'état partagé

        now_timestamp = datetime.now(pytz.utc).timestamp()

        for order in pending_orders:
            order_age_seconds = now_timestamp - order.time_setup
            if order_age_seconds > cancel_after_seconds:
                logging.warning(f"Ordre limite #{order.ticket} ({order.symbol}) annulé car trop ancien ({order_age_seconds:.0f}s > {cancel_after_seconds}s).")
                if executor.cancel_order(order.ticket): # (J.3) Utilise retry
                    # Déverrouiller le symbole si l'ordre est annulé
                    state.unlock_symbol(order.symbol)

    except Exception as e:
        logging.error(f"Erreur lors de la gestion des ordres en attente: {e}", exc_info=True)

# --- Boucle Principale (MODIFIÉE) ---
def main_trading_loop(state: SharedState):
    """Boucle principale qui orchestre le bot de trading."""
    logging.info("Démarrage de la boucle de trading v20.0.0 (SMC Integration)...") # Version mise à jour
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

    # --- NOUVEAU (SMC Integration) ---
    # Charger la configuration MTF
    mtf_data_config = config['trading_settings'].get('mtf_data_config')
    if not mtf_data_config:
        logging.critical("Configuration 'trading_settings.mtf_data_config' manquante. (Requise pour SMC). Arrêt.")
        state.update_status("Arrêté", "Config MTF manquante.", is_emergency=True)
        return
    # --- FIN NOUVEAU ---

    state.initialize_symbol_data(symbols_to_trade)
    is_first_cycle = True
    last_pending_check_time = 0
    last_deal_check_timestamp = state.get_last_deal_check_timestamp()

    while not state.is_shutdown():
        try:
            current_time = time.time() 

            # 1. Vérifier connexion & Recharger config
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
                # --- NOUVEAU (SMC Integration) ---
                # Recharger aussi la config MTF
                mtf_data_config = config['trading_settings'].get('mtf_data_config')
                if not mtf_data_config:
                    logging.critical("Config MTF manquante après rechargement. Arrêt.")
                    state.update_status("Arrêté", "Config MTF manquante.", is_emergency=True)
                    break
                # --- FIN NOUVEAU ---
                state.initialize_symbol_data(symbols_to_trade)
                state.clear_config_changed_flag()
                logging.info("Configuration rechargée.")
                if not symbols_to_trade:
                    logging.critical("Aucun symbole valide à trader après rechargement. Arrêt du bot.")
                    state.update_status("Arrêté", "Aucun symbole valide après rechargement.", is_emergency=True)
                    break

            # 2. Infos compte (Inchangé)
            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les informations du compte.")
                time.sleep(10)
                continue
            state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

            # 3. Gérer trades fermés et positions ouvertes (Inchangé)
            magic_number = config['trading_settings'].get('magic_number', 0)
            
            new_timestamp = executor.check_for_closed_trades(magic_number, last_deal_check_timestamp)
            if new_timestamp > last_deal_check_timestamp:
                state.set_last_deal_check_timestamp(new_timestamp)
                last_deal_check_timestamp = new_timestamp
            
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions) # Met à jour l'UI

            executor.update_context_for_new_positions(all_bot_positions)

            if all_bot_positions:
                positions_by_symbol = {}
                for pos in all_bot_positions:
                    positions_by_symbol.setdefault(pos.symbol, []).append(pos)

                for symbol, positions in positions_by_symbol.items():
                    try:
                        rm_pos = RiskManager(config, executor, symbol) 
                        timeframe = config['trading_settings'].get('timeframe', 'M15')
                        # Note: La gestion de position utilise toujours la timeframe simple
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200)
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty:
                            rm_pos.manage_open_positions(
                                positions, tick, ohlc_data_for_pos, executor._trade_context
                            )
                    except ValueError as e: logging.error(f"Erreur validation gestion pos {symbol}: {e}")
                    except Exception as e: logging.error(f"Erreur gestion pos {symbol}: {e}", exc_info=True)

            # 4. Gérer les ordres en attente (Inchangé)
            pending_check_interval = config.get('pending_order_settings', {}).get('check_interval_seconds', 60) # Fallback 60s
            if current_time - last_pending_check_time > pending_check_interval:
                manage_pending_orders(state, executor, config)
                last_pending_check_time = current_time

            # 5. Vérifier limites de risque globales (Inchangé)
            skip_new_signals = False
            if symbols_to_trade:
                try:
                    main_rm = RiskManager(config, executor, symbols_to_trade[0])
                    
                    limit_reached, _ = main_rm.is_daily_loss_limit_reached()
                    if limit_reached:
                        state.update_status("Arrêt d'Urgence", "Limite de perte journalière atteinte.", is_emergency=True)
                        skip_new_signals = True 
                    
                    max_total_risk = config.get('risk_management', {}).get('max_total_risk_percent', 5.0) / 100.0
                    if max_total_risk > 0 and not skip_new_signals:
                        current_total_risk = main_rm.get_current_total_risk(all_bot_positions, account_info.equity)
                        if current_total_risk >= max_total_risk:
                            logging.warning(f"Limite risque globale atteinte ({current_total_risk*100:.2f}% >= {max_total_risk*100:.2f}%). Scan de nouveaux signaux suspendu.")
                            skip_new_signals = True
                        elif current_total_risk > 0:
                            logging.info(f"Risque global actuel: {current_total_risk*100:.2f}% / {max_total_risk*100:.2f}%")

                except ValueError: pass
                except Exception as e: logging.error(f"Erreur vérification limite risque: {e}", exc_info=True)

            # 6. Boucle d'analyse et de trading (MODIFIÉE)
            if is_first_cycle: logging.info("Premier cycle: trading désactivé pour synchro.")

            current_pending_orders = executor.get_pending_orders(magic=magic_number)
            pending_symbols = {order.symbol for order in current_pending_orders}

            if skip_new_signals and not is_first_cycle:
                logging.debug("Scan de nouveaux signaux ignoré (Limites atteintes).")
            else:
                for symbol in symbols_to_trade:
                    try:
                        # Conditions (Inchangées)
                        if not is_within_trading_session(symbol, config): continue
                        if any(pos.symbol == symbol for pos in all_bot_positions): continue 
                        if symbol in pending_symbols: continue 
                        if state.is_symbol_locked(symbol): 
                            logging.debug(f"Symbole {symbol} verrouillé. Scan ignoré.")
                            continue

                        # --- MODIFIÉ (SMC Integration) ---
                        # A. Obtenir les données MTF (au lieu de OHLC simple)
                        logging.debug(f"Analyse SMC pour {symbol}...")
                        mtf_data = connector.get_mtf_data(symbol, mtf_data_config)

                        if mtf_data is None or any(v is None or v.empty for v in mtf_data.values()):
                            logging.warning(f"Données MTF non disponibles ou incomplètes pour {symbol}.")
                            continue
                        
                        # B. Initialiser le RiskManager (pour l'exécution)
                        risk_manager = RiskManager(config, executor, symbol)
                        
                        # C. Détecter le pattern (en passant les données MTF)
                        detector = PatternDetector(config)
                        trade_signal = detector.detect_patterns(mtf_data, connector, symbol) # Retourne le format v13
                        
                        # Mettre à jour l'état de l'API
                        state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())
                        # --- FIN MODIFICATION ---

                        # D. Exécution (Logique v19 Inchangée)
                        if trade_signal and not is_first_cycle:
                            logging.info(f"SIGNAL VALIDE sur {symbol}: [{trade_signal['pattern']}] direction {trade_signal['direction']} - ZONE [{trade_signal.get('entry_zone_start'):.5f} - {trade_signal.get('entry_zone_end'):.5f}].")

                            ttl_lock = config['trading_settings'].get('idempotency_lock_seconds', 900)
                            state.lock_symbol(symbol, ttl_lock) 

                            executor.execute_trade(
                                account_info, risk_manager, symbol, trade_signal['direction'],
                                mtf_data[config['trading_settings']['timeframe']], # Passer les données M15 au risk_manager
                                trade_signal['pattern'], magic_number,
                                trade_signal 
                            )

                    except ValueError as e: logging.error(f"Impossible de traiter '{symbol}': {e}.")
                    except Exception as e: logging.error(f"Erreur analyse sur {symbol}: {e}", exc_info=True)

            # 7. Attendre prochaine bougie (Inchangé)
            timeframe_str = config['trading_settings'].get('timeframe', 'M15')
            timeframe_seconds = get_timeframe_seconds(timeframe_str)
            now_utc_ts = datetime.now(pytz.utc).timestamp()
            next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(1, next_candle_epoch - now_utc_ts)

            if is_first_cycle:
                logging.info("Fin cycle synchro. Trading activé.")
                is_first_cycle = False

            logging.info(f"Cycle terminé. Attente de {sleep_duration:.1f}s.")
            time.sleep(sleep_duration)

        except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err:
            logging.error(f"Erreur connexion critique: {conn_err}", exc_info=False)
            state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", is_emergency=True)
            time.sleep(30)
        except KeyboardInterrupt:
            logging.info("Arrêt manuel demandé (Ctrl+C).")
            state.shutdown()
            break
        except Exception as loop_err:
            logging.critical(f"ERREUR CRITIQUE boucle principale: {loop_err}", exc_info=True)
            state.update_status("ERREUR CRITIQUE", str(loop_err), is_emergency=True)
            time.sleep(60)

    connector.disconnect()
    logging.info("Boucle de trading terminée.")


# --- Bloc if __name__ == "__main__": (INCHANGÉ PAR RAPPORT À VOTRE v19.1.1) ---
if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)

    try:
        config = load_yaml('config.yaml')
        shared_state.update_config(config) # (J'ajoute ceci pour m'assurer que l'API l'a dès le début)

        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        url = f"http://{host}:{port}"

        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
        api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")

        try:
            time.sleep(1) # Laisse le temps au serveur de démarrer
            webbrowser.open(url)
        except Exception:
            logging.warning("Impossible d'ouvrir le navigateur web automatiquement.")

        main_trading_loop(shared_state)

    except Exception as startup_err:
        logging.critical(f"ERREUR FATALE au démarrage: {startup_err}", exc_info=True)
        shared_state.update_status("ERREUR FATALE", str(startup_err), is_emergency=True)
        if 'api_thread' in locals() and api_thread.is_alive():
            logging.info("Tentative de maintien de l'API active...")
            while True: time.sleep(3600)

    logging.info("Programme principal terminé.")
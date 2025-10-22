# Fichier: main.py
# Version: 1.0.0
# Dépendances: MetaTrader5, pytz, PyYAML, Flask, os, logging, threading, webbrowser, time, datetime
# Description: Version initiale avant modifications v1.0.1+.

import time
import threading
import logging
import yaml
import webbrowser
import os
from datetime import datetime, time as dt_time
import pytz

# --- ATTENTION: Les imports suivants correspondent à la version 1.0.0 des fichiers ---
import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
# !! Assurez-vous que les fichiers importés sont bien ceux de la v1.0.0 !!
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer
# --- Fin Attention ---

def setup_logging(state: SharedState):
    """Configure la journalisation."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    if not os.path.exists('logs'): os.makedirs('logs')
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='w', encoding='utf-8')
    console_handler = logging.StreamHandler()
    ui_handler = LogHandler(state) # Assurez-vous que LogHandler existe tel quel en v1.0.0
    for handler in [file_handler, console_handler, ui_handler]: handler.setFormatter(log_formatter)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler); root_logger.addHandler(console_handler); root_logger.addHandler(ui_handler)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    """Charge un fichier YAML."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f: return yaml.safe_load(f)
    # --- Gestion Erreur Simplifiée (v1.0.0) ---
    except FileNotFoundError: logging.critical(f"FATAL: Fichier config '{filepath}' introuvable."); exit()
    except yaml.YAMLError as e: logging.critical(f"FATAL: Erreur YAML dans '{filepath}': {e}."); exit()
    return {}
    # --- Fin Gestion Erreur ---

def get_timeframe_seconds(timeframe_str: str) -> int:
    """Convertit timeframe str en secondes."""
    # --- Version 1.0.0 ---
    if 'M' in timeframe_str: return int(timeframe_str.replace('M', '')) * 60
    if 'H' in timeframe_str: return int(timeframe_str.replace('H', '')) * 3600
    if 'D' in timeframe_str: return int(timeframe_str.replace('D', '')) * 86400
    logging.warning(f"Timeframe '{timeframe_str}' non reconnu, défaut 60s."); return 60
    # --- Fin Version ---

def validate_symbols(symbols_list, mt5_connection):
    """Vérifie disponibilité symboles."""
    # --- Version 1.0.0 ---
    valid_symbols = []
    for symbol in symbols_list:
        if mt5_connection.symbol_info(symbol): # Appel direct, pas via wrapper
            valid_symbols.append(symbol)
        else:
            logging.error(f"Symbole '{symbol}' indisponible/invalide. Ignoré.") # Log simple
    return valid_symbols
    # --- Fin Version ---

def is_within_trading_session(symbol: str, config: dict) -> bool:
    """Vérifie si dans session de trading."""
    # --- Version 1.0.0 ---
    sessions_config = config.get('trading_settings', {}).get('trading_sessions', [])
    crypto_symbols = config.get('trading_settings', {}).get('crypto_symbols', [])
    if symbol in crypto_symbols: return True
    if not sessions_config: return True
    now_utc = datetime.now(pytz.utc); current_weekday_iso = now_utc.isoweekday(); current_time_utc = now_utc.time()
    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str); start_time = dt_time.fromisoformat(start_str); end_time = dt_time.fromisoformat(end_str)
            if day == current_weekday_iso and start_time <= current_time_utc < end_time: return True
        except (ValueError, TypeError, IndexError) as e: logging.error(f"Format session invalide: '{session}'. Erreur: {e}")
    return False
    # --- Fin Version ---

def main_trading_loop(state: SharedState):
    """Boucle principale du bot."""
    logging.info("Démarrage boucle trading v1.0.0...") # Version
    config = load_yaml('config.yaml')
    state.update_config(config)

    # --- Version 1.0.0: Lecture directe config ---
    connector = MT5Connector(config['mt5_credentials'])
    # --- Fin Version ---
    if not connector.connect():
        state.update_status("Déconnecté", "Connexion initiale MT5 échouée.", True); return

    # --- Version 1.0.0: Utilisation connexion brute ---
    executor = MT5Executor(connector.get_connection(), config) # Passe la connexion brute
    # --- Fin Version ---
    analyzer = PerformanceAnalyzer(state)

    # --- Version 1.0.0: Pas d'activation symboles ---
    symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
    # --- Fin Version ---
    if not symbols_to_trade:
        logging.critical("Aucun symbole valide. Vérifiez config/connexion MT5. Arrêt."); state.update_status("Arrêté", "Aucun symbole valide.", True); connector.disconnect(); return

    state.initialize_symbol_data(symbols_to_trade) # Assurez-vous que cette méthode existe en v1.0.0
    logging.info(f"Symboles à surveiller: {', '.join(symbols_to_trade)}")

    is_first_cycle = True
    # --- Version 1.0.0: Pas de dry_run ---
    # dry_run_mode = False
    # --- Fin Version ---

    while not state.is_shutdown():
        try:
            # 1. Vérif connexion & Recharge config
            # --- Version 1.0.0: Gestion connexion basique ---
            if not connector.check_connection(): # Assurez-vous que check_connection existe en v1.0.0
                state.update_status("Déconnecté", "Connexion MT5 perdue...", True); logging.warning("Connexion MT5 perdue. Reconnexion...")
                if not connector.connect(): logging.error("Reconnexion échouée."); time.sleep(20); continue # Délai fixe
                state.update_status("Connecté", "Reconnexion MT5 OK."); logging.info("Reconnexion MT5 réussie.")
            # --- Fin Version ---

            if state.config_changed_flag:
                logging.info("Changement config détecté. Rechargement..."); config = load_yaml('config.yaml')
                # --- Version 1.0.0: Pas de validation config au rechargement ---
                state.update_config(config)
                executor = MT5Executor(connector.get_connection(), config) # Recréer avec connexion brute
                symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
                state.initialize_symbol_data(symbols_to_trade); state.clear_config_changed_flag()
                logging.info(f"Config rechargée. Symboles actifs: {', '.join(symbols_to_trade)}")
                if not symbols_to_trade: logging.critical("Aucun symbole valide post-recharge. Arrêt."); state.update_status("Arrêté", "Aucun symbole valide post-recharge.", True); break
                # --- Fin Version ---

            # 2. Infos compte
            account_info = executor.get_account_info()
            if not account_info: logging.warning("Infos compte MT5 inaccessibles."); time.sleep(10); continue
            state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

            # 3. Gérer trades fermés et positions ouvertes
            magic_number = config['trading_settings'].get('magic_number', 0); executor.check_for_closed_trades(magic_number)
            all_bot_positions = executor.get_open_positions(magic=magic_number); state.update_positions(all_bot_positions)
            if all_bot_positions:
                positions_by_symbol = {}; [positions_by_symbol.setdefault(p.symbol, []).append(p) for p in all_bot_positions]
                for symbol, positions in positions_by_symbol.items():
                    try:
                        # --- Version 1.0.0: RiskManager sans wrapper executor ---
                        rm_pos = RiskManager(config, executor, symbol)
                        # --- Fin Version ---
                        timeframe = config['trading_settings'].get('timeframe', 'M15')
                        # --- Version 1.0.0: Appels directs connector ---
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200); tick = connector.get_tick(symbol)
                        # --- Fin Version ---
                        if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty: rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                    except ValueError as e: logging.error(f"Erreur init RiskManager gestion pos {symbol}: {e}")
                    except Exception as e: logging.error(f"Erreur gestion pos ouverte {symbol}: {e}", exc_info=True)

            # 4. Vérifier limites risque globales
            if symbols_to_trade:
                try:
                    main_rm = RiskManager(config, executor, symbols_to_trade[0])
                    limit_reached, current_pnl = main_rm.is_daily_loss_limit_reached() # Assurez-vous que ça existe en v1.0.0
                    if limit_reached: logging.critical(f"Limite perte jour atteinte ({current_pnl:.2f}). Trading suspendu."); state.update_status("Arrêt Urgence", f"Perte jour {current_pnl:.2f} >= limite.", True); time.sleep(60); continue
                except ValueError as e: logging.error(f"Erreur init RiskManager limite perte: {e}")
                except Exception as e: logging.error(f"Erreur vérif limite perte: {e}", exc_info=True)

            # 5. Boucle analyse/trade
            if is_first_cycle: logging.info("Premier cycle: trading désactivé (synchro).")
            for symbol in symbols_to_trade:
                try:
                    if not is_within_trading_session(symbol, config): continue
                    if any(pos.symbol == symbol for pos in all_bot_positions): continue
                    risk_manager = RiskManager(config, executor, symbol); timeframe = config['trading_settings'].get('timeframe', 'M15')
                    ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
                    if ohlc_data is None or ohlc_data.empty: logging.warning(f"Données OHLC ({timeframe}) non dispo pour {symbol}."); continue
                    detector = PatternDetector(config); trade_signal = detector.detect_patterns(ohlc_data, connector, symbol) # Passer connector v1.0.0
                    state.update_symbol_patterns(symbol, detector.get_detected_patterns_info()) # Assurez-vous que ça existe en v1.0.0
                    if trade_signal and not is_first_cycle:
                        logging.info(f"SIGNAL VALIDE {symbol}: [{trade_signal['pattern']}] dir {trade_signal['direction']}.")
                        # --- Version 1.0.0: Pas de dry_run check ---
                        executor.execute_trade(account_info, risk_manager, symbol, trade_signal['direction'], ohlc_data, trade_signal['pattern'], magic_number, trade_signal)
                        # --- Fin Version ---
                except ValueError as e: logging.error(f"Erreur traitement symbole '{symbol}': {e}.")
                except Exception as e: logging.error(f"Erreur inattendue analyse/trade {symbol}: {e}", exc_info=True)

            # 6. Attendre prochaine bougie
            timeframe_str = config['trading_settings'].get('timeframe', 'M15'); timeframe_seconds = get_timeframe_seconds(timeframe_str)
            now_utc_ts = datetime.now(pytz.utc).timestamp(); next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(1.0, next_candle_epoch - now_utc_ts)
            if is_first_cycle: logging.info("Fin cycle synchro. Trading activé."); is_first_cycle = False
            logging.info(f"Cycle terminé. Attente {sleep_duration:.1f}s (prochaine bougie {timeframe_str}).")
            time.sleep(sleep_duration)

        # Gestion erreurs boucle (version 1.0.0 potentiellement moins spécifique)
        except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err: logging.error(f"Erreur connexion MT5 critique: {conn_err}"); state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", True); time.sleep(30)
        except KeyboardInterrupt: logging.info("Arrêt manuel (Ctrl+C)."); state.shutdown(); break
        except Exception as loop_err: logging.critical(f"ERREUR CRITIQUE boucle: {loop_err}", exc_info=True); state.update_status("ERREUR CRITIQUE", str(loop_err), True); time.sleep(60)

    # Nettoyage
    connector.disconnect(); logging.info("Connexion MT5 fermée."); state.update_status("Arrêté", "Bot arrêté."); logging.info("Boucle trading terminée.")

# Point d'entrée
if __name__ == "__main__":
    shared_state = SharedState(); setup_logging(shared_state)
    api_thread = None
    try:
        config = load_yaml('config.yaml')
        # --- Version 1.0.0: Pas de validation config ici ---
        shared_state.update_config(config)
        host = config.get('api', {}).get('host', '127.0.0.1'); port = config.get('api', {}).get('port', 5000); url = f"http://{host}:{port}"
        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True); api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")
        try: time.sleep(1); webbrowser.open(url)
        except Exception: logging.warning("Impossible ouvrir navigateur auto.")
        # --- Fin Version ---
        main_trading_loop(shared_state)
    except Exception as startup_err:
         logging.critical(f"ERREUR FATALE démarrage: {startup_err}", exc_info=True); shared_state.update_status("ERREUR FATALE", str(startup_err), True)
         if api_thread and api_thread.is_alive():
              logging.info("Maintien API active malgré échec...")
              try:
                  while True: time.sleep(3600)
              except KeyboardInterrupt: logging.info("Arrêt manuel API.")
    logging.info("Programme principal terminé.")
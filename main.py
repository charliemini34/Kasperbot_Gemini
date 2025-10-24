# Fichier: main.py
# Version: 18.0.0 (Sugg-TopDown)
# Dépendances: MetaTrader5, pytz, PyYAML, Flask, time, threading, logging, webbrowser, os, datetime, src modules
# Description: Refonte majeure (Sugg 1-5) vers logique Top-Down (H4/M15) et Killzones.

import time
import threading
import logging
import yaml
import webbrowser
import os
import sys
from datetime import datetime, time as dt_time
import pytz
from typing import List, Dict

import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer

def setup_logging(state: SharedState):
    """Configure la journalisation."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    if not os.path.exists('logs'): os.makedirs('logs')
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='a', encoding='utf-8')
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
    """Charge un fichier YAML."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier de configuration '{filepath}' introuvable.")
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.critical(f"FATAL: Erreur YAML dans '{filepath}': {e}.")
        sys.exit(1)
    return {}

def validate_config(config: dict):
    """Valide les paramètres de configuration critiques."""
    logging.info("Validation des paramètres de configuration...")
    is_valid = True
    rm_config = config.get('risk_management', {})
    if not isinstance(rm_config.get('daily_loss_limit_percent'), (int, float)) or not (0 < rm_config.get('daily_loss_limit_percent', 5.0) < 20):
        logging.critical(f"FATAL: 'daily_loss_limit_percent' invalide. Doit être > 0 et < 20.")
        is_valid = False
    if not isinstance(rm_config.get('risk_per_trade'), (int, float)) or not (0 < rm_config.get('risk_per_trade', 1.0) < 10):
        logging.critical(f"FATAL: 'risk_per_trade' invalide. Doit être > 0 et < 10.")
        is_valid = False
    if not isinstance(rm_config.get('min_rr'), (int, float)) or rm_config.get('min_rr', 2.0) <= 0:
        logging.critical(f"FATAL: 'min_rr' ({rm_config.get('min_rr')}) doit être > 0.")
        is_valid = False
    if not config.get('trading_settings', {}).get('killzones'):
         logging.critical("FATAL: 'trading_settings.killzones' est manquant ou vide.")
         is_valid = False
    if not config.get('trend_filter', {}).get('higher_timeframe'):
         logging.critical("FATAL: 'trend_filter.higher_timeframe' est manquant.")
         is_valid = False

    if not is_valid:
        logging.critical("Validation de la configuration échouée.")
        sys.exit(1)
    logging.info("Configuration validée.")

def get_timeframe_seconds(timeframe_str: str) -> int:
    """Convertit une chaîne de timeframe (ex: 'M15') en secondes."""
    tf = timeframe_str.upper()
    if 'M' in tf: return int(tf.replace('M', '')) * 60
    if 'H' in tf: return int(tf.replace('H', '')) * 3600
    if 'D' in tf: return int(tf.replace('D', '')) * 86400
    logging.warning(f"Timeframe inconnu '{timeframe_str}', défaut 60s.")
    return 60

# --- [Suggestion 5.1 / 5.2] Remplacement de 'is_within_trading_session' ---
def get_active_symbols_for_session(config: dict) -> List[str]:
    """
    Retourne la liste des symboles actifs basés sur les killzones UTC actuelles.
    """
    killzones_config = config.get('trading_settings', {}).get('killzones', {})
    if not killzones_config:
        return []

    now_utc = datetime.now(pytz.utc)
    current_time = now_utc.time()
    active_symbols = set() # Utiliser un set pour éviter les doublons

    for session_name, (start_str, end_str, symbols) in killzones_config.items():
        try:
            start_time = dt_time.fromisoformat(start_str)
            end_time = dt_time.fromisoformat(end_str)

            # Gérer les sessions 24h (ex: Crypto)
            if start_time == end_time or (start_time == dt_time(0,0) and end_time == dt_time(23,59)):
                 if session_name == "CRYPTO": # Logique spécifique Crypto
                     crypto_list = config.get('trading_settings', {}).get('crypto_symbols', [])
                     active_symbols.update(crypto_list)
                 continue

            # Gérer les sessions qui traversent minuit (ex: 22:00-06:00)
            if start_time <= end_time: # Session dans la même journée
                if start_time <= current_time < end_time:
                    active_symbols.update(symbols)
            else: # Session sur deux jours
                if current_time >= start_time or current_time < end_time:
                     active_symbols.update(symbols)
                     
        except (ValueError, TypeError) as e:
            logging.error(f"Format de Killzone invalide pour '{session_name}': {e}")
            continue

    return list(active_symbols)
# --- Fin [Suggestion 5.1 / 5.2] ---


def main_trading_loop(state: SharedState):
    """Boucle principale orchestrant le bot (Logique Top-Down H4/M15)."""
    logging.info(f"Démarrage de la boucle de trading v{__import__('main').__version__}... ")
    config = load_yaml('config.yaml')
    validate_config(config)
    state.update_config(config)

    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "La connexion initiale à MT5 a échoué.", is_emergency=True)
        return

    executor = MT5Executor(connector.get_connection(), config)
    analyzer = PerformanceAnalyzer(state)

    # (Sugg 5.1) Initialiser *tous* les symboles possibles pour l'UI
    all_symbols = set()
    for (start, end, symbols) in config.get('trading_settings', {}).get('killzones', {}).values():
        all_symbols.update(symbols)
    
    if not all_symbols:
        logging.critical("Aucun symbole défini dans les killzones. Arrêt.")
        state.update_status("Arrêté", "Aucun symbole configuré.", is_emergency=True)
        connector.disconnect()
        return

    state.initialize_symbol_data(list(all_symbols))

    max_concurrent_trades = config.get('risk_management', {}).get('max_concurrent_trades', 5)
    logging.info(f"Limite de positions simultanées fixée à : {max_concurrent_trades}")
    is_first_cycle = True
    
    # (Sugg 2.1) Définir les timeframes
    ltf_str = config.get('trading_settings', {}).get('timeframe', 'M15')
    htf_str = config.get('trend_filter', {}).get('higher_timeframe', 'H4')

    while not state.is_shutdown():
        try:
            # 1. Vérifier connexion & Recharger config
            if not connector.check_connection():
                state.update_status("Déconnecté", "Connexion MT5 perdue. Tentative...", is_emergency=True)
                if not connector.connect():
                    time.sleep(20)
                    continue
                state.update_status("Connecté", "Reconnexion à MT5 réussie.")

            if state.config_changed_flag:
                logging.info("Changement de configuration détecté. Rechargement...")
                config = load_yaml('config.yaml')
                validate_config(config) # Re-valider
                state.update_config(config)
                connector = MT5Connector(config['mt5_credentials'])
                if not connector.connect(): continue
                executor = MT5Executor(connector.get_connection(), config)
                analyzer = PerformanceAnalyzer(state)
                # Recharger les symboles et TFs
                all_symbols = set()
                for (start, end, symbols) in config.get('trading_settings', {}).get('killzones', {}).values():
                    all_symbols.update(symbols)
                state.initialize_symbol_data(list(all_symbols))
                max_concurrent_trades = config.get('risk_management', {}).get('max_concurrent_trades', 5)
                ltf_str = config.get('trading_settings', {}).get('timeframe', 'M15')
                htf_str = config.get('trend_filter', {}).get('higher_timeframe', 'H4')
                state.clear_config_changed_flag()
                logging.info("Configuration rechargée.")

            # 2. Infos compte
            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Infos compte MT5 inaccessibles.")
                time.sleep(10)
                continue
            state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

            # 3. Gérer trades fermés et positions ouvertes
            magic_number = config['trading_settings'].get('magic_number', 0)
            executor.check_for_closed_trades(magic_number)
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)

            # Gestion BE/Trailing/TP Partiels (Logique inchangée, utilise LTF)
            if all_bot_positions:
                positions_by_symbol = {pos.symbol: [] for pos in all_bot_positions}
                for pos in all_bot_positions: positions_by_symbol[pos.symbol].append(pos)

                for symbol, positions in positions_by_symbol.items():
                    partial_close_actions = []
                    try:
                        rm_pos = RiskManager(config, executor, symbol)
                        # Utiliser le timeframe LTF (M15) pour la gestion de position
                        ohlc_data_for_pos = connector.get_ohlc(symbol, ltf_str, 300) 
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty:
                            partial_close_actions = rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                        else:
                             logging.warning(f"Données LTF manquantes pour gérer la position sur {symbol}")
                    except Exception as e:
                        logging.error(f"Erreur gestion position ouverte sur {symbol}: {e}", exc_info=True)
                    
                    if partial_close_actions:
                         for action in partial_close_actions:
                              try:
                                   executor.close_partial_position(action['ticket'], action['volume'], action['trade_id'])
                                   all_bot_positions = executor.get_open_positions(magic=magic_number)
                                   state.update_positions(all_bot_positions)
                              except Exception as e:
                                   logging.error(f"Erreur exécution clôture partielle #{action['ticket']}: {e}", exc_info=True)

            # 4. Vérifier limites de risque globales
            # (Sugg 5.1) Trouver un symbole valide pour l'initialisation du RM
            symbols_for_check = list(all_symbols)
            if symbols_for_check:
                try:
                    rm_symbol_for_check = symbols_for_check[0]
                    main_rm = RiskManager(config, executor, rm_symbol_for_check)
                    limit_reached, daily_pnl = main_rm.is_daily_loss_limit_reached()

                    if limit_reached:
                        if not state.status.get('is_emergency', False):
                            logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte.")
                            state.update_status("Arrêt Urgence", f"Limite perte jour atteinte ({daily_pnl:.2f}).", is_emergency=True)
                        time.sleep(60 * 5)
                        continue
                    elif state.status.get('is_emergency', False) and state.status.get('message', '').startswith("Limite perte jour"):
                         logging.info("Réinitialisation du statut après atteinte de la limite de perte.")
                         state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}", is_emergency=False)
                except Exception as e:
                     logging.error(f"Erreur vérification limite de perte: {e}", exc_info=True)

            # 5. Boucle d'analyse (Logique Killzone)
            current_trade_count = len(all_bot_positions)
            if current_trade_count >= max_concurrent_trades:
                logging.info(f"Limite de {max_concurrent_trades} positions atteinte. Pause de l'analyse.")
            else:
                if is_first_cycle: logging.info("Premier cycle: trading désactivé pour synchro.")

                # (Sugg 5.1 / 5.2) Obtenir les symboles actifs pour la Killzone actuelle
                active_symbols = get_active_symbols_for_session(config)
                if active_symbols:
                    logging.debug(f"Symboles actifs pour la session actuelle: {active_symbols}")

                for symbol in active_symbols:
                    try:
                        # Vérifications (Position existante, Limite atteinte)
                        if any(pos.symbol == symbol for pos in all_bot_positions):
                            logging.debug(f"Position déjà ouverte sur {symbol}, analyse ignorée.")
                            continue
                        if len(executor.get_open_positions(magic=magic_number)) >= max_concurrent_trades:
                             logging.info(f"Limite de {max_concurrent_trades} atteinte pendant l'analyse. Arrêt.")
                             break

                        # --- (Sugg 2.1) Récupération données HTF et LTF ---
                        # HTF (ex: H4) pour Biais et POI
                        htf_data = connector.get_ohlc(symbol, htf_str, 300) # Assez de données pour EMA 200 + swings
                        # LTF (ex: M15) pour Confirmation
                        ltf_data = connector.get_ohlc(symbol, ltf_str, 200) # Assez pour swings M15

                        if htf_data is None or htf_data.empty or len(htf_data) < 210: # Marge pour EMA 200
                            logging.warning(f"Données HTF ({htf_str}) insuffisantes pour {symbol}.")
                            continue
                        if ltf_data is None or ltf_data.empty or len(ltf_data) < 50:
                            logging.warning(f"Données LTF ({ltf_str}) insuffisantes pour {symbol}.")
                            continue
                        # --- Fin (Sugg 2.1) ---

                        detector = PatternDetector(config)
                        # (Sugg 2.1) Appel avec les deux timeframes
                        trade_signal = detector.detect_patterns(htf_data, ltf_data, connector, symbol)
                        
                        state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())

                        if trade_signal and not is_first_cycle:
                            logging.info(f"SIGNAL VALIDE (Top-Down) sur {symbol}: [{trade_signal['pattern']}] direction {trade_signal['direction']}.")

                            # RiskManager utilise les données LTF (M15) pour le calcul SL (si SMC_STRUCTURE)
                            risk_manager = RiskManager(config, executor, symbol)
                            
                            executor.execute_trade(
                                account_info, risk_manager, symbol, trade_signal['direction'],
                                ltf_data, # Passer LTF data pour calcul SL/ATR
                                trade_signal['pattern'], magic_number,
                                trade_signal # Contient target_price HTF (Sugg 3.1)
                            )
                            time.sleep(0.5)
                            all_bot_positions = executor.get_open_positions(magic=magic_number)
                            state.update_positions(all_bot_positions)

                    except ValueError as e: logging.error(f"Impossible de traiter le symbole '{symbol}': {e}.")
                    except Exception as e: logging.error(f"Erreur inattendue analyse {symbol}: {e}", exc_info=True)

            # 6. Attendre prochaine bougie (basé sur LTF)
            timeframe_seconds = get_timeframe_seconds(ltf_str)
            now_utc_ts = datetime.now(pytz.utc).timestamp()
            next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(1.0, next_candle_epoch - now_utc_ts)

            if is_first_cycle:
                logging.info("Fin du premier cycle de synchronisation. Trading activé.")
                is_first_cycle = False

            logging.info(f"Cycle terminé. Attente de {sleep_duration:.1f} secondes (prochaine bougie {ltf_str}).")
            time.sleep(sleep_duration)

        except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err:
             logging.error(f"Erreur connexion MT5 critique: {conn_err}", exc_info=False)
             state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", is_emergency=True)
             time.sleep(30)
        except KeyboardInterrupt:
             logging.info("Arrêt manuel demandé (Ctrl+C). Fermeture...")
             state.shutdown()
             break
        except Exception as loop_err:
             logging.critical(f"ERREUR CRITIQUE non gérée dans la boucle: {loop_err}", exc_info=True)
             state.update_status("ERREUR CRITIQUE", str(loop_err), is_emergency=True)
             state.shutdown()
             break

    connector.disconnect()
    logging.info("Connexion MT5 fermée.")
    state.update_status("Arrêté", "Bot arrêté proprement.")
    logging.info("Boucle de trading terminée.")


# --- Version Info ---
__version__ = "18.0.0" # Version de refonte Top-Down

# Bloc principal
if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    logging.info(f"--- Démarrage KasperBot Gemini v{__version__} ---")
    api_thread = None
    try:
        config = load_yaml('config.yaml')
        validate_config(config) 
        shared_state.update_config(config)
        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        url = f"http://{host}:{port}"
        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
        api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")
        try:
            threading.Timer(2.0, lambda: webbrowser.open(url)).start()
        except Exception as browser_err:
            logging.warning(f"Impossible d'ouvrir le navigateur: {browser_err}")
        main_trading_loop(shared_state)
    except SystemExit:
         logging.critical("Arrêt du programme suite à une configuration invalide.")
    except Exception as startup_err:
         logging.critical(f"ERREUR FATALE au démarrage: {startup_err}", exc_info=True)
         shared_state.update_status("ERREUR FATALE", str(startup_err), is_emergency=True)
         if api_thread and api_thread.is_alive():
              logging.info("Échec boucle trading, maintien de l'API active...")
              try:
                  while True: time.sleep(3600)
              except KeyboardInterrupt:
                  logging.info("Arrêt manuel de l'API.")
                  shared_state.shutdown()
    logging.info(f"Statut final: {shared_state.status.get('status')}")
    logging.info("Programme principal terminé.")
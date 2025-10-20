# Fichier: main.py
# Version: 17.0.5 (Refactoring-Loop) # <- Version incrémentée
# Dépendances: MetaTrader5, pytz, PyYAML, Flask, playsound, time, threading, logging, webbrowser, os, datetime
# Description: Refactorisation de main_trading_loop pour meilleure lisibilité.

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
    logging.warning("Bibliothèque 'playsound' non trouvée. Les alertes sonores seront désactivées. Installez avec: pip install playsound==1.2.2")
    playsound = None

import MetaTrader5 as mt5
from src.data_ingest.mt5_connector import MT5Connector
from src.patterns.pattern_detector import PatternDetector
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer

# --- Fonctions utilitaires (inchangées) ---
def setup_logging(state: SharedState):
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
    try:
        with open(filepath, 'r', encoding='utf-8') as f: return yaml.safe_load(f)
    except FileNotFoundError:
        logging.critical(f"FATAL: Fichier config '{filepath}' introuvable.")
        exit()
    except yaml.YAMLError as e:
        logging.critical(f"FATAL: Erreur YAML dans '{filepath}': {e}.")
        exit()
    return {}

def get_timeframe_seconds(timeframe_str: str) -> int:
    if 'M' in timeframe_str: return int(timeframe_str.replace('M', '')) * 60
    if 'H' in timeframe_str: return int(timeframe_str.replace('H', '')) * 3600
    if 'D' in timeframe_str: return int(timeframe_str.replace('D', '')) * 86400
    logging.warning(f"Timeframe '{timeframe_str}' non reconnu, utilisation de 60s par défaut.")
    return 60

def validate_symbols(symbols_list, mt5_connection):
    valid_symbols = []
    if not symbols_list: # Vérifier si la liste est vide ou None
        logging.warning("La liste des symboles dans la configuration est vide.")
        return []
    for symbol in symbols_list:
        if mt5_connection.symbol_info(symbol): valid_symbols.append(symbol)
        else: logging.error(f"Symbole '{symbol}' invalide/indisponible. Ignoré.")
    return valid_symbols

def is_within_trading_session(symbol: str, config: dict) -> bool:
    sessions_config = config.get('trading_settings', {}).get('trading_sessions', [])
    crypto_symbols = config.get('trading_settings', {}).get('crypto_symbols', [])
    if symbol in crypto_symbols: return True # Crypto trade 24/7
    if not sessions_config: return True # Pas de config = toujours ouvert
    now_utc = datetime.now(pytz.utc)
    # MT5 weekday: Sunday=0, Monday=1, ..., Saturday=6
    # Python weekday: Monday=0, Tuesday=1, ..., Sunday=6
    # Conversion: (Python weekday + 1) % 7 maps to MT5 Sunday=0
    # Utilisons directement now_utc.weekday() (0-6) et adaptons la config si nécessaire ou la logique ici.
    # Assumons que la config utilise Lundi=1 ... Dimanche=7 (plus intuitif) ou 0 pour Dimanche? La config utilise 1-5.
    # Python: Monday=0, Sunday=6. Config: Monday=1, Friday=5.
    current_weekday_config_format = now_utc.weekday() + 1 # Lundi=1, Mardi=2.. Dimanche=7

    current_time = now_utc.time()
    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str)
            start_time = dt_time.fromisoformat(start_str) # Attend HH:MM:SS ou HH:MM
            end_time = dt_time.fromisoformat(end_str)
            # Correction: Gestion du passage à minuit (ex: 22:00-02:00) n'est pas gérée ici.
            # Supposons pour l'instant que les sessions sont intra-journalières.
            if day == current_weekday_config_format and start_time <= current_time < end_time:
                return True
        except (ValueError, TypeError) as e:
            logging.error(f"Format session invalide: '{session}'. Erreur: {e}")
            continue # Ignorer cette session mal formatée
    # logging.debug(f"Hors session pour {symbol} (Jour: {current_weekday_config_format}, Heure UTC: {current_time})")
    return False


def play_alert_sound(config: dict):
    if playsound is None: return
    sound_config = config.get('sound_alerts', {})
    if sound_config.get('enabled', False):
        sound_file = sound_config.get('sound_file', './alert.wav')
        if os.path.exists(sound_file):
            try:
                # Exécuter dans un thread pour ne pas bloquer
                sound_thread = threading.Thread(target=playsound, args=(sound_file,), daemon=True)
                sound_thread.start()
                logging.info("Alerte sonore jouée.")
            except Exception as e:
                # Peut échouer sur certains systèmes sans GUI ou avec des drivers audio spécifiques
                logging.error(f"Impossible de jouer le son '{sound_file}': {e}")
        else:
            logging.warning(f"Fichier son configuré '{sound_file}' introuvable.")


# --- Nouvelles fonctions refactorisées ---

def check_connection_and_config(state: SharedState, connector: MT5Connector, executor: MT5Executor) -> tuple:
    """Vérifie la connexion MT5 et recharge la config si nécessaire."""
    config = state.get_config() # Récupère la config actuelle
    symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())

    if not connector.check_connection():
        state.update_status("Déconnecté", "Connexion MT5 perdue...", is_emergency=True)
        if not connector.connect():
            time.sleep(20)
            return None, None, None # Indique un échec de connexion
        state.update_status("Connecté", "Reconnexion MT5 OK.")
        # Re-vérifier les symboles après reconnexion
        symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())

    if state.config_changed_flag:
        logging.info("Rechargement configuration...")
        config = load_yaml('config.yaml')
        state.update_config(config)
        # Recréer executor car il dépend de la config (ex: journal pro)
        executor = MT5Executor(connector.get_connection(), config)
        # Revalider les symboles avec la nouvelle config
        symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
        state.clear_config_changed_flag()
        logging.info("Configuration rechargée.")
        if not symbols_to_trade:
             logging.critical("Aucun symbole valide après rechargement. Vérifiez config.")
             state.update_status("Erreur Config", "Aucun symbole valide.", is_emergency=True)
             # Ne pas retourner None ici, laisser la boucle principale s'arrêter si nécessaire

    return config, executor, symbols_to_trade


def process_open_positions(state: SharedState, config: dict, connector: MT5Connector, executor: MT5Executor):
    """Gère les SL/TP/Trailing/BE pour les positions ouvertes."""
    magic_number = config['trading_settings'].get('magic_number', 0)
    executor.check_for_closed_trades(magic_number) # Archive les trades fermés
    all_bot_positions = executor.get_open_positions(magic=magic_number)
    state.update_positions(all_bot_positions) # Met à jour l'état pour l'API

    if not all_bot_positions:
        return # Pas de positions à gérer

    positions_by_symbol = {}
    for pos in all_bot_positions:
        positions_by_symbol.setdefault(pos.symbol, []).append(pos)

    for symbol, positions in positions_by_symbol.items():
        try:
            rm_pos = RiskManager(config, executor, symbol)
            timeframe = config['trading_settings'].get('timeframe', 'M15')
            ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 200) # Assez pour ATR/Trailing
            tick = connector.get_tick(symbol)
            if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty:
                rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
            else:
                 logging.warning(f"Données manquantes (tick ou OHLC) pour gérer la position sur {symbol}.")
        except ValueError as e: # Erreur spécifique de RiskManager si infos symbole manquantes
            logging.error(f"Erreur initialisation RiskManager pour gestion pos {symbol}: {e}")
        except Exception as e:
            logging.error(f"Erreur gestion position ouverte sur {symbol}: {e}", exc_info=True)


def check_risk_limits(state: SharedState, config: dict, executor: MT5Executor, symbols_to_trade: list) -> bool:
    """Vérifie la limite de perte journalière."""
    if not symbols_to_trade: return False # Pas de symbole, pas de risque à vérifier

    try:
        # Utilise le premier symbole valide pour initialiser RiskManager (juste pour accéder à history_deals)
        main_rm = RiskManager(config, executor, symbols_to_trade[0])
        limit_reached, _ = main_rm.is_daily_loss_limit_reached()
        if limit_reached:
            state.update_status("Arrêt Urgence", "Limite perte jour atteinte.", is_emergency=True)
            logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte.")
            return True # Limite atteinte
    except ValueError as e:
        logging.error(f"Erreur initialisation RiskManager pour vérifier limite de perte: {e}")
    except Exception as e:
        logging.error(f"Erreur vérification limite de perte: {e}", exc_info=True)

    return False # Limite non atteinte ou erreur


def analyze_and_trade_symbol(symbol: str, state: SharedState, config: dict, connector: MT5Connector, executor: MT5Executor, account_info, is_first_cycle: bool):
    """Analyse un symbole et exécute un trade si les conditions sont remplies."""
    magic_number = config['trading_settings'].get('magic_number', 0)
    # Vérifie si une position gérée par CE bot est déjà ouverte sur ce symbole
    if any(pos.symbol == symbol for pos in executor.get_open_positions(magic=magic_number)):
        # logging.debug(f"Position déjà ouverte sur {symbol}, analyse ignorée.")
        return

    if not is_within_trading_session(symbol, config):
        # logging.debug(f"Hors session de trading pour {symbol}.")
        return

    try:
        risk_manager = RiskManager(config, executor, symbol)
        timeframe = config['trading_settings'].get('timeframe', 'M15')
        # Récupérer plus de données pour l'analyse des patterns (ex: zones P/D, swings)
        ohlc_data = connector.get_ohlc(symbol, timeframe, 300)
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < 50: # Besoin d'assez de données pour l'analyse
            logging.warning(f"Données OHLC insuffisantes ou indisponibles pour {symbol} sur {timeframe}.")
            state.update_symbol_patterns(symbol, {}) # Nettoyer les anciens patterns affichés
            return

        detector_instance = PatternDetector(config)
        trade_signal = detector_instance.detect_patterns(ohlc_data, connector, symbol)
        # Mettre à jour l'état partagé avec les derniers status des patterns détectés
        state.update_symbol_patterns(symbol, detector_instance.get_detected_patterns_info())

        if trade_signal and not is_first_cycle:
            logging.info(f"SIGNAL VALIDE sur {symbol}: [{trade_signal['pattern']}] en direction de {trade_signal['direction']}.")
            play_alert_sound(config) # Joue l'alerte si configuré

            # Utiliser la dernière clôture comme prix de référence pour le calcul SL/TP/Volume
            # Note: Le prix d'exécution réel peut varier légèrement.
            last_close_price = ohlc_data['close'].iloc[-1]

            volume, sl, tp = risk_manager.calculate_trade_parameters(
                account_info.equity, last_close_price, ohlc_data, trade_signal
            )

            if volume > 0 and sl > 0 and tp > 0: # Vérifier aussi validité SL/TP retournés
                # --- Condition Dry Run / Live Trading ---
                if config['trading_settings'].get('live_trading_enabled', False): # False par défaut si clé absente
                    logging.info(f"Exécution ordre LIVE: {trade_signal['direction']} {volume:.{risk_manager.symbol_info.volume_digits}f} lots {symbol} (Pattern: {trade_signal['pattern']})")
                    executor.execute_trade(
                        account_info, risk_manager, symbol, trade_signal['direction'],
                        volume, sl, tp,
                        trade_signal['pattern'], magic_number
                    )
                else:
                    logging.info(f"DRY RUN: Ordre {trade_signal['direction']} {volume:.{risk_manager.symbol_info.volume_digits}f} lots {symbol} @ ~{last_close_price:.{risk_manager.digits}f} (SL={sl:.{risk_manager.digits}f}, TP={tp:.{risk_manager.digits}f}) non envoyé. Pattern: {trade_signal['pattern']}")
            else:
                logging.warning(f"Volume calculé ({volume}) ou SL/TP ({sl}/{tp}) invalide pour {symbol}. Trade basé sur signal [{trade_signal['pattern']}] annulé.")

    except ValueError as e: # Erreurs potentielles de RiskManager ou PatternDetector
        logging.error(f"Impossible de traiter le symbole '{symbol}': {e}.")
    except Exception as e:
        logging.error(f"Erreur inattendue lors de l'analyse de {symbol}: {e}", exc_info=True)


def wait_for_next_candle(config: dict) -> float:
    """Calcule et effectue l'attente jusqu'à la prochaine bougie."""
    timeframe_str = config['trading_settings'].get('timeframe', 'M15')
    timeframe_seconds = get_timeframe_seconds(timeframe_str)
    now_utc_ts = datetime.now(pytz.utc).timestamp()
    next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
    sleep_duration = max(1.0, next_candle_epoch - now_utc_ts) # Attendre au moins 1 seconde
    logging.info(f"Cycle terminé. Attente de {sleep_duration:.1f} secondes jusqu'à la prochaine bougie {timeframe_str}.")
    time.sleep(sleep_duration)
    return sleep_duration


# --- Boucle principale refactorisée ---

def main_trading_loop(state: SharedState):
    """Boucle principale orchestrant le bot."""
    logging.info(f"Démarrage de la boucle de trading v{__import__('main').__file__.split('/')[-1]}...") # Met à jour la version automatiquement si possible
    is_first_cycle = True
    config = None
    connector = None
    executor = None
    symbols_to_trade = []

    try:
        config = load_yaml('config.yaml')
        state.update_config(config)

        connector = MT5Connector(config['mt5_credentials'])
        if not connector.connect():
            state.update_status("Déconnecté", "Connexion initiale MT5 échouée.", is_emergency=True)
            return

        executor = MT5Executor(connector.get_connection(), config)
        # analyzer = PerformanceAnalyzer(state) # Décommenter si l'analyse périodique est souhaitée

        while not state.is_shutdown():
            try:
                # 1. Vérifier connexion et recharger config si nécessaire
                config, executor, symbols_to_trade = check_connection_and_config(state, connector, executor)
                if config is None: continue # Echec connexion grave
                if not symbols_to_trade:
                     logging.warning("Aucun symbole valide à trader actuellement. Vérifiez config ou connexion.")
                     time.sleep(30) # Attendre avant de réessayer
                     continue

                # 2. Obtenir infos compte et mettre à jour statut
                account_info = executor.get_account_info()
                if not account_info:
                    logging.warning("Infos compte MT5 inaccessibles. Tentative au prochain cycle.")
                    time.sleep(10)
                    continue
                state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

                # 3. Gérer les positions ouvertes (SL, TP, etc.)
                process_open_positions(state, config, connector, executor)

                # 4. Vérifier les limites de risque (perte journalière)
                if check_risk_limits(state, config, executor, symbols_to_trade):
                    time.sleep(60) # Attendre en état d'urgence
                    continue # Revenir au début pour revérifier l'état et la connexion

                # 5. Analyser les symboles pour de nouvelles opportunités (sauf 1er cycle)
                if is_first_cycle:
                    logging.info("Premier cycle terminé: Synchronisation initiale effectuée. Trading activé pour les cycles suivants.")
                    is_first_cycle = False
                else:
                    for symbol in symbols_to_trade:
                        analyze_and_trade_symbol(symbol, state, config, connector, executor, account_info, is_first_cycle)

                # 6. Attendre la prochaine bougie
                wait_for_next_candle(config)

            # --- Gestion des erreurs spécifiques à la boucle interne ---
            except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err: # Erreurs liées à MT5
                logging.error(f"Erreur connexion MT5 critique: {conn_err}", exc_info=False) # Pas besoin de trace complète souvent
                state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", is_emergency=True)
                # Tentative de reconnexion gérée au début de la boucle suivante via check_connection_and_config
                time.sleep(30) # Attente avant nouvelle tentative
            except Exception as loop_err:
                logging.critical(f"ERREUR CRITIQUE non gérée dans la boucle: {loop_err}", exc_info=True)
                state.update_status("ERREUR CRITIQUE", str(loop_err), is_emergency=True)
                time.sleep(60) # Attente significative après erreur grave

    except KeyboardInterrupt:
        logging.info("Arrêt manuel demandé (Ctrl+C).")
        state.shutdown()
    except Exception as startup_err:
        # Erreurs lors de l'initialisation (avant la boucle while)
        logging.critical(f"ERREUR FATALE au démarrage: {startup_err}", exc_info=True)
        state.update_status("ERREUR FATALE", str(startup_err), is_emergency=True)
        # Essayer de déconnecter proprement si possible
        if connector:
            connector.disconnect()
    finally:
        logging.info("Arrêt de la boucle de trading...")
        if connector:
            connector.disconnect()
        logging.info("Connexion MT5 fermée.")
        state.update_status("Arrêté", "Bot arrêté proprement.")


if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state) # Configurer logging AVANT tout le reste

    try:
        config = load_yaml('config.yaml') # Charger config tôt pour API
        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        url = f"http://{host}:{port}"

        # Démarrer l'API dans un thread séparé
        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
        api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")

        # Tenter d'ouvrir le navigateur (optionnel)
        try:
             # Attendre une seconde que le serveur Flask démarre
             threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception as e:
            logging.warning(f"Impossible d'ouvrir le navigateur automatiquement: {e}")

        # Démarrer la boucle de trading principale
        main_trading_loop(shared_state)

    except Exception as e:
        logging.critical(f"Erreur non gérée dans le bloc __main__: {e}", exc_info=True)
        # Assurer que l'état reflète l'erreur si possible
        if 'shared_state' in locals():
            shared_state.update_status("ERREUR MAIN", str(e), is_emergency=True)
        # Attendre un peu pour que les logs soient potentiellement écrits/vus
        time.sleep(5)

    logging.info("Programme principal terminé.")
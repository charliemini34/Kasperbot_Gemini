
# Fichier: main.py
# Version: 17.0.5 (FIX-2) # <-- Version mise à jour
# Dépendances: MetaTrader5, pytz, PyYAML, Flask, time, threading, logging, webbrowser, os, datetime, src modules
# Description: Corrige la faute de frappe 'isoweckday' en 'isoweekday'.

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

# ... (les fonctions setup_logging, load_yaml, etc. restent inchangées) ...
def setup_logging(state: SharedState):
    """Configure la journalisation pour la console, les fichiers et l'interface utilisateur."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    if not os.path.exists('logs'):
        os.makedirs('logs')

    # Utiliser 'a' pour append au lieu de 'w' pour écraser, sauf si un système de rotation est mis en place
    file_handler = logging.FileHandler("logs/trading_bot.log", mode='a', encoding='utf-8')
    console_handler = logging.StreamHandler()
    ui_handler = LogHandler(state)

    for handler in [file_handler, console_handler, ui_handler]:
        handler.setFormatter(log_formatter)

    root_logger = logging.getLogger()
    # Empêcher l'ajout multiple de handlers si la fonction est appelée plusieurs fois
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(ui_handler)
        # Assurer que les logs de niveau inférieur (DEBUG) sont aussi capturés si nécessaire
        # mais le niveau global reste INFO pour la sortie standard.
        # file_handler.setLevel(logging.DEBUG) # Optionnel: pour logs fichiers plus détaillés

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
    tf = timeframe_str.upper()
    if 'M' in tf: return int(tf.replace('M', '')) * 60
    if 'H' in tf: return int(tf.replace('H', '')) * 3600
    if 'D' in tf: return int(tf.replace('D', '')) * 86400
    logging.warning(f"Timeframe inconnu '{timeframe_str}', utilisation de 60 secondes par défaut.")
    return 60

def validate_symbols(symbols_list, mt5_connection):
    """Vérifie que les symboles sont disponibles sur la plateforme MT5."""
    valid_symbols = []
    if not symbols_list: # Gérer le cas où la liste est vide ou None
        logging.warning("La liste des symboles dans la configuration est vide.")
        return []
    for symbol in symbols_list:
        symbol_info = mt5_connection.symbol_info(symbol)
        if symbol_info:
            valid_symbols.append(symbol)
        else:
            logging.error(f"Le symbole '{symbol}' n'est pas disponible ou est mal orthographié. Il sera ignoré.")
    return valid_symbols

def is_within_trading_session(symbol: str, config: dict) -> bool:
    """Vérifie si le symbole peut être tradé à l'heure UTC actuelle."""
    sessions_config = config.get('trading_settings', {}).get('trading_sessions', [])
    crypto_symbols = config.get('trading_settings', {}).get('crypto_symbols', [])

    if symbol in crypto_symbols:
        return True # Crypto trade 24/7 pour cet exemple

    if not sessions_config:
        logging.debug("Aucune session de trading définie, trading autorisé par défaut.")
        return True # Autoriser si aucune session n'est définie

    now_utc = datetime.now(pytz.utc)
    
    # --- CORRECTION [FIX-2] ---
    # Correction de la faute de frappe 'isoweckday' -> 'isoweekday'
    current_weekday_config_format = now_utc.isoweekday() # Lundi=1 ... Dimanche=7
    # --- FIN CORRECTION [FIX-2] ---
    
    current_time = now_utc.time()

    for session in sessions_config:
        try:
            day_str, start_str, end_str = session.split('-')
            day = int(day_str)
            start_time = dt_time.fromisoformat(start_str)
            end_time = dt_time.fromisoformat(end_str)

            # Gérer les sessions qui traversent minuit (ex: 22:00-06:00)
            if start_time <= end_time: # Session dans la même journée
                if day == current_weekday_config_format and start_time <= current_time < end_time:
                    return True
            else: # Session sur deux jours (ex: se termine le lendemain matin)
                if day == current_weekday_config_format and current_time >= start_time:
                     return True # Dans la première partie de la session
                # Vérifier si on est dans la deuxième partie (jour suivant avant end_time)
                previous_day_config_format = (current_weekday_config_format - 2 + 7) % 7 + 1 # Jour précédent (Dimanche=7)
                if day == previous_day_config_format and current_time < end_time:
                     return True # Dans la seconde partie de la session

        except (ValueError, TypeError) as e:
            logging.error(f"Format de session invalide: '{session}'. Erreur: {e}")
            continue

    logging.debug(f"{symbol} est en dehors des sessions de trading définies.")
    return False


def main_trading_loop(state: SharedState):
    """Boucle principale qui orchestre le bot de trading."""
    logging.info(f"Démarrage de la boucle de trading v{__import__('main').__version__}... ") # Utiliser __version__ si défini
    config = load_yaml('config.yaml')
    state.update_config(config)

    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "La connexion initiale à MT5 a échoué.", is_emergency=True)
        return

    executor = MT5Executor(connector.get_connection(), config)
    analyzer = PerformanceAnalyzer(state) # L'analyzer peut être utilisé plus tard

    symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
    if not symbols_to_trade:
        logging.critical("Aucun symbole valide à trader. Vérifiez config.yaml et la connexion MT5.")
        state.update_status("Arrêté", "Aucun symbole valide.", is_emergency=True)
        connector.disconnect() # Déconnecter proprement
        return

    state.initialize_symbol_data(symbols_to_trade)

    # --- [Risk-1] Limite d'Exposition ---
    # Récupérer la limite depuis config.yaml, avec une valeur par défaut
    max_concurrent_trades = config.get('risk_management', {}).get('max_concurrent_trades', 5)
    logging.info(f"Limite de positions simultanées fixée à : {max_concurrent_trades}")
    # --- Fin [Risk-1] ---

    is_first_cycle = True

    while not state.is_shutdown():
        try:
            # 1. Vérifier connexion & Recharger config
            if not connector.check_connection():
                state.update_status("Déconnecté", "Connexion MT5 perdue. Tentative de reconnexion...", is_emergency=True)
                if not connector.connect():
                    time.sleep(20) # Attente plus longue si reconnexion échoue
                    continue
                state.update_status("Connecté", "Reconnexion à MT5 réussie.")

            if state.config_changed_flag:
                logging.info("Changement de configuration détecté. Rechargement...")
                config = load_yaml('config.yaml')
                state.update_config(config)
                # Réinitialiser les composants qui dépendent de la config
                connector = MT5Connector(config['mt5_credentials']) # Recréer au cas où les credentials changent
                if not connector.connect(): # Assurer la connexion après changement
                     state.update_status("Déconnecté", "Échec connexion après recharge config.", is_emergency=True)
                     continue
                executor = MT5Executor(connector.get_connection(), config)
                analyzer = PerformanceAnalyzer(state)
                symbols_to_trade = validate_symbols(config['trading_settings'].get('symbols', []), connector.get_connection())
                state.initialize_symbol_data(symbols_to_trade)
                max_concurrent_trades = config.get('risk_management', {}).get('max_concurrent_trades', 5) # Recharger aussi la limite
                logging.info(f"Nouvelle limite de positions simultanées : {max_concurrent_trades}")
                state.clear_config_changed_flag()
                logging.info("Configuration rechargée.")
                if not symbols_to_trade:
                     logging.warning("Aucun symbole valide à trader après rechargement.")
                     # Ne pas arrêter le bot ici, pourrait être temporaire

            # 2. Infos compte
            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Infos compte MT5 inaccessibles. Tentative au prochain cycle.")
                time.sleep(10)
                continue
            state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}")

            # 3. Gérer trades fermés et positions ouvertes
            magic_number = config['trading_settings'].get('magic_number', 0)
            executor.check_for_closed_trades(magic_number)
            all_bot_positions = executor.get_open_positions(magic=magic_number)
            state.update_positions(all_bot_positions)

            # Gestion BE/Trailing (existante)
            if all_bot_positions:
                positions_by_symbol = {}
                for pos in all_bot_positions:
                    positions_by_symbol.setdefault(pos.symbol, []).append(pos)

                for symbol, positions in positions_by_symbol.items():
                    partial_close_actions = [] # Initialiser ici
                    try:
                        # Créer une instance RiskManager par symbole si nécessaire
                        rm_pos = RiskManager(config, executor, symbol)
                        timeframe = config['trading_settings'].get('timeframe', 'M15')
                        # Récupérer suffisamment de données pour les calculs (ex: ATR, swings)
                        ohlc_data_for_pos = connector.get_ohlc(symbol, timeframe, 300) # Augmenté pour contexte
                        tick = connector.get_tick(symbol)
                        if tick and ohlc_data_for_pos is not None and not ohlc_data_for_pos.empty:
                            # --- [Risk-2] Capture des actions de TP Partiels ---
                            # La logique de détection est dans rm_pos.manage_open_positions
                            partial_close_actions = rm_pos.manage_open_positions(positions, tick, ohlc_data_for_pos)
                        else:
                             logging.warning(f"Données manquantes (tick ou OHLC) pour gérer la position sur {symbol}")
                    except ValueError as e:
                        logging.error(f"Erreur initialisation RiskManager pour gestion pos {symbol}: {e}")
                    except Exception as e:
                        logging.error(f"Erreur gestion position ouverte sur {symbol}: {e}", exc_info=True)
                    
                    # --- [Risk-2] Exécution des actions de TP Partiels ---
                    if partial_close_actions:
                         for action in partial_close_actions:
                              try:
                                   logging.info(f"Exécution action TP Partiel pour ticket #{action['ticket']} (Volume: {action['volume']})")
                                   executor.close_partial_position(action['ticket'], action['volume'], action['trade_id'])
                                   # Mettre à jour l'état des positions après clôture partielle
                                   all_bot_positions = executor.get_open_positions(magic=magic_number)
                                   state.update_positions(all_bot_positions)
                              except Exception as e:
                                   logging.error(f"Erreur lors de l'exécution de la clôture partielle pour ticket #{action['ticket']}: {e}", exc_info=True)


            # 4. Vérifier limites de risque globales AVANT d'analyser de nouveaux trades
            if symbols_to_trade: # Seulement s'il y a des symboles valides
                try:
                    # Utiliser le premier symbole valide pour obtenir une instance RM
                    rm_symbol_for_check = symbols_to_trade[0]
                    main_rm = RiskManager(config, executor, rm_symbol_for_check)
                    limit_reached, daily_pnl = main_rm.is_daily_loss_limit_reached()
                    # Mettre à jour le PNL journalier dans le statut (peut être utile pour l'UI)
                    # state.update_pnl(daily_pnl) # Assurez-vous que cette méthode existe dans SharedState si nécessaire

                    if limit_reached:
                        if not state.status.get('is_emergency', False): # Éviter logs répétitifs
                            logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte.")
                            state.update_status("Arrêt Urgence", f"Limite perte jour atteinte ({daily_pnl:.2f}).", is_emergency=True)
                        time.sleep(60 * 5) # Attente plus longue en cas d'arrêt d'urgence
                        continue
                    # Si la limite n'est plus atteinte (ex: jour suivant), réinitialiser le statut d'urgence
                    elif state.status.get('is_emergency', False) and state.status.get('message', '').startswith("Limite perte jour"):
                         logging.info("Réinitialisation du statut après atteinte de la limite de perte journalière (probablement nouveau jour).")
                         state.update_status("Connecté", f"Solde: {account_info.balance:.2f} {account_info.currency}", is_emergency=False)

                except ValueError as e:
                     logging.error(f"Erreur initialisation RiskManager pour vérifier limite de perte (symbole: {rm_symbol_for_check}): {e}")
                except Exception as e:
                     logging.error(f"Erreur vérification limite de perte: {e}", exc_info=True)


            # --- [Risk-1] Vérification Limite Positions ---
            current_trade_count = len(all_bot_positions)
            if current_trade_count >= max_concurrent_trades:
                logging.info(f"Limite de {max_concurrent_trades} positions simultanées atteinte ({current_trade_count} ouvertes). Pause de l'analyse pour nouveaux trades.")
            # --- Fin [Risk-1] ---
            else: # Seulement analyser si la limite n'est pas atteinte
                # 5. Boucle d'analyse et de trading (seulement si limite non atteinte)
                if is_first_cycle: logging.info("Premier cycle: trading désactivé pour synchro.")

                for symbol in symbols_to_trade:
                    try:
                        # Vérifications déplacées avant récupération de données pour efficacité
                        if not is_within_trading_session(symbol, config):
                            # logging.debug(f"Symbole {symbol} hors session.") # Log si besoin
                            continue
                        if any(pos.symbol == symbol for pos in all_bot_positions):
                            logging.debug(f"Position déjà ouverte sur {symbol}, analyse ignorée.")
                            continue
                        # Re-vérifier la limite de trades au cas où un trade aurait été ouvert pendant la boucle
                        if len(executor.get_open_positions(magic=magic_number)) >= max_concurrent_trades:
                             logging.info(f"Limite de {max_concurrent_trades} positions atteinte pendant l'analyse. Arrêt de l'analyse pour ce cycle.")
                             break # Sortir de la boucle 'for symbol in symbols_to_trade'


                        # Récupération données et analyse
                        risk_manager = RiskManager(config, executor, symbol)
                        timeframe = config['trading_settings'].get('timeframe', 'M15')
                        ohlc_data = connector.get_ohlc(symbol, timeframe, 300)

                        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < 50: # S'assurer d'avoir assez de données
                            logging.warning(f"Données OHLC insuffisantes ou indisponibles pour {symbol} sur {timeframe}. Reçu: {len(ohlc_data) if ohlc_data is not None else 0}")
                            continue

                        detector = PatternDetector(config)
                        trade_signal = detector.detect_patterns(ohlc_data, connector, symbol)
                        state.update_symbol_patterns(symbol, detector.get_detected_patterns_info())

                        if trade_signal and not is_first_cycle:
                            logging.info(f"SIGNAL VALIDE sur {symbol}: [{trade_signal['pattern']}] direction {trade_signal['direction']}.")

                            # Appel à execute_trade (inchangé, passe maintenant trade_signal)
                            executor.execute_trade(
                                account_info, risk_manager, symbol, trade_signal['direction'],
                                ohlc_data, trade_signal['pattern'], magic_number,
                                trade_signal
                            )
                            # Petite pause après avoir envoyé un ordre pour laisser le temps à MT5 de traiter?
                            time.sleep(0.5) # Attente 500ms
                            # Re-vérifier immédiatement le nombre de positions après tentative d'ordre
                            all_bot_positions = executor.get_open_positions(magic=magic_number)
                            state.update_positions(all_bot_positions)


                    except ValueError as e: logging.error(f"Impossible de traiter le symbole '{symbol}': {e}.")
                    except Exception as e: logging.error(f"Erreur inattendue lors de l'analyse de {symbol}: {e}", exc_info=True)


            # 6. Attendre prochaine bougie
            timeframe_str = config['trading_settings'].get('timeframe', 'M15')
            timeframe_seconds = get_timeframe_seconds(timeframe_str)
            now_utc_ts = datetime.now(pytz.utc).timestamp()
            # Calcul pour s'aligner sur le début de la prochaine bougie UTC
            next_candle_epoch = (now_utc_ts // timeframe_seconds + 1) * timeframe_seconds
            sleep_duration = max(1.0, next_candle_epoch - now_utc_ts) # Attendre au moins 1 seconde

            if is_first_cycle:
                logging.info("Fin du premier cycle de synchronisation. Trading activé pour les cycles suivants.")
                is_first_cycle = False

            logging.info(f"Cycle terminé. Attente de {sleep_duration:.1f} secondes jusqu'à la prochaine bougie ({datetime.fromtimestamp(next_candle_epoch, tz=pytz.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}).")
            time.sleep(sleep_duration)

        except (ConnectionError, BrokenPipeError, TimeoutError) as conn_err:
             logging.error(f"Erreur connexion MT5 critique: {conn_err}", exc_info=False)
             state.update_status("Déconnecté", f"Erreur connexion: {conn_err}", is_emergency=True)
             time.sleep(30) # Attente avant de retenter
        except KeyboardInterrupt:
             logging.info("Arrêt manuel demandé (Ctrl+C). Fermeture...")
             state.shutdown()
             break # Sortir de la boucle while
        except Exception as loop_err:
             logging.critical(f"ERREUR CRITIQUE non gérée dans la boucle principale: {loop_err}", exc_info=True)
             state.update_status("ERREUR CRITIQUE", str(loop_err), is_emergency=True)
             # Continuer après une erreur critique? Ou arrêter? Pour l'instant, on continue après une pause.
             time.sleep(60)

    # Actions après sortie de la boucle (arrêt normal ou sur erreur)
    connector.disconnect()
    logging.info("Connexion MT5 fermée.")
    state.update_status("Arrêté", "Bot arrêté proprement.")
    logging.info("Boucle de trading terminée.")


# --- Version Info ---
__version__ = "17.0.5" # Mettre à jour la version ici

# Bloc principal
if __name__ == "__main__":
    # Initialisation de l'état partagé avant tout le reste
    shared_state = SharedState()
    # Configuration du logging très tôt
    setup_logging(shared_state)
    logging.info(f"--- Démarrage KasperBot Gemini v{__version__} ---")

    api_thread = None # Initialiser la variable

    try:
        config = load_yaml('config.yaml')
        shared_state.update_config(config) # Mettre à jour l'état partagé avec la config initiale

        # Démarrage de l'API Flask dans un thread séparé
        host = config.get('api', {}).get('host', '127.0.0.1')
        port = config.get('api', {}).get('port', 5000)
        url = f"http://{host}:{port}"

        api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
        api_thread.start()
        logging.info(f"Interface web démarrée sur {url}")

        # Tenter d'ouvrir le navigateur
        try:
            # Ajouter un délai pour s'assurer que le serveur Flask est prêt
            threading.Timer(2.0, lambda: webbrowser.open(url)).start()
        except Exception as browser_err:
            logging.warning(f"Impossible d'ouvrir le navigateur automatiquement: {browser_err}")

        # Lancer la boucle de trading principale
        main_trading_loop(shared_state)

    except Exception as startup_err:
         logging.critical(f"ERREUR FATALE au démarrage: {startup_err}", exc_info=True)
         shared_state.update_status("ERREUR FATALE", str(startup_err), is_emergency=True)
         # Tenter de maintenir l'API active même si la boucle de trading échoue au démarrage
         if api_thread and api_thread.is_alive():
              logging.info("La boucle de trading a échoué au démarrage, mais tentative de maintien de l'API active...")
              try:
                  # Boucle infinie pour maintenir le thread principal en vie
                  while True: time.sleep(3600)
              except KeyboardInterrupt:
                  logging.info("Arrêt manuel de l'API après échec du démarrage du bot.")
                  shared_state.shutdown() # Signaler l'arrêt aux autres threads si nécessaire

    # Assurer que l'état final est loggué
    logging.info(f"Statut final du bot: {shared_state.status.get('status')} - {shared_state.status.get('message')}")
    logging.info("Programme principal terminé.")
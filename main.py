# ==============================================================================
#                 BOT DE TRADING XAUUSD v3.0 - LANCEUR PRINCIPAL
# ==============================================================================
# (Version révisée et améliorée)
# ==============================================================================
import time
import threading
import logging
import yaml
import webbrowser
import os

# --- Importation des modules du projet ---
from src.data_ingest.mt5_connector import MT5Connector
from src.scorer.strategy_scorer import StrategyScorer
from src.scorer.aggregator import Aggregator
from src.risk.risk_manager import RiskManager
from src.execution.mt5_executor import MT5Executor
from src.api.server import start_api_server
from src.shared_state import SharedState, LogHandler
from src.analysis.performance_analyzer import PerformanceAnalyzer
from src.analysis.ai_assistant import AIAssistant

# --- Configuration du logging ---
def setup_logging(state: SharedState):
    """Configure le système de logging pour logger dans un fichier, la console et l'interface web."""
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    
    # Handler pour l'interface web (via SharedState)
    ui_handler = LogHandler(state)
    ui_handler.setFormatter(log_formatter)
    
    # Handler pour le fichier trading_bot.log
    file_handler = logging.FileHandler("trading_bot.log", mode='w', encoding='utf-8')
    file_handler.setFormatter(log_formatter)

    # Handler pour la console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    
    # Configuration du logger racine
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        root_logger.addHandler(ui_handler)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
    
    # Réduire le bruit des logs de Flask/Werkzeug
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

def load_yaml(filepath: str) -> dict:
    """Charge un fichier de configuration YAML de manière sécurisée."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.error(f"FATAL: Le fichier de configuration '{filepath}' est introuvable. Assurez-vous qu'il est présent. Arrêt du programme.")
        exit()
    except Exception as e:
        logging.error(f"FATAL: Erreur de lecture ou de formatage de '{filepath}': {e}")
        exit()
    return {}

def main_trading_loop(state: SharedState):
    """Boucle principale qui orchestre le bot de trading."""
    logging.info("Démarrage de la boucle de trading...")
    
    # --- Initialisation des composants ---
    config = load_yaml('config.yaml')
    profiles = load_yaml('profiles.yaml')
    state.update_config(config)

    connector = MT5Connector(config['mt5_credentials'])
    if not connector.connect():
        state.update_status("Déconnecté", "La connexion à MetaTrader 5 a échoué. Vérifiez vos identifiants.", is_emergency=True)
        return

    analyzer = PerformanceAnalyzer('trade_history.csv')
    ai_assistant = AIAssistant() # L'assistant gère lui-même la présence de la clé API
    trade_count_since_analysis = 0

    # --- Boucle de trading principale ---
    while not state.is_shutdown():
        try:
            # Rechargement de la configuration si modifiée depuis l'interface web
            if state.config_changed_flag:
                config = load_yaml('config.yaml')
                profiles = load_yaml('profiles.yaml')
                state.update_config(config)
                state.clear_config_changed_flag()
                logging.info("Configuration et profils rechargés dynamiquement.")
            
            # Sélection du profil de stratégie actif
            active_profile_name = config.get('trading_logic', {}).get('active_profile', 'custom')
            strategy_weights = profiles.get(active_profile_name)
            if not strategy_weights:
                logging.error(f"Profil '{active_profile_name}' non trouvé dans profiles.yaml. Utilisation du profil 'custom' par défaut.")
                strategy_weights = profiles.get('custom', {})
            
            # Initialisation des modules avec la configuration à jour
            scorer = StrategyScorer()
            aggregator = Aggregator(strategy_weights)
            executor = MT5Executor(connector.get_connection(), analyzer)
            risk_manager = RiskManager(config.get('risk_management', {}), executor, config['trading_settings']['symbol'])

            symbol = config['trading_settings']['symbol']
            timeframe_str = config['trading_settings']['timeframe']

            account_info = executor.get_account_info()
            if not account_info:
                logging.warning("Impossible de récupérer les informations du compte. Tentative de reconnexion...")
                connector.connect() # Tente de se reconnecter
                time.sleep(10)
                continue

            state.update_status("Connecté", f"Balance: {account_info.balance:.2f} {account_info.currency}")

            # Vérification de la limite de perte journalière
            daily_pnl = executor.get_daily_pnl()
            state.update_pnl(daily_pnl)
            if risk_manager.is_daily_loss_limit_reached(account_info.equity, daily_pnl):
                 state.update_status("Arrêt d'urgence", "Perte journalière maximale atteinte.", is_emergency=True)
                 logging.critical("ARRÊT D'URGENCE: Limite de perte journalière atteinte. Le bot va s'arrêter.")
                 break

            # Gestion des positions ouvertes (Break-even, Trailing Stop)
            open_positions = executor.get_open_positions(symbol)
            state.update_positions(open_positions)
            if open_positions:
                current_tick = connector.get_tick(symbol)
                risk_manager.manage_open_positions(open_positions, current_tick)
            
            # Vérification des trades récemment fermés pour analyse de performance
            executor.check_for_closed_trades()
            trade_count_since_analysis += executor.get_newly_closed_trades_count()

            if trade_count_since_analysis >= 5: # Lance l'analyse de performance tous les 5 trades
                analyzer.run_analysis()
                trade_count_since_analysis = 0

            # Récupération des données de marché pour l'analyse
            ohlc_data = connector.get_ohlc(symbol, timeframe_str, 200)
            if ohlc_data is None or ohlc_data.empty:
                logging.warning(f"Aucune donnée OHLC reçue pour {symbol}. Nouvel essai dans 5 secondes.")
                time.sleep(5)
                continue

            # Calcul des scores et décision
            raw_scores = scorer.calculate_all(ohlc_data)
            state.update_scores(raw_scores)
            final_score, trade_direction = aggregator.calculate_final_score(raw_scores)
            
            if final_score > 10: 
                logging.info(f"Analyse: Score={final_score:.2f} | Dir={trade_direction}")

            # Condition de déclenchement d'un trade
            if final_score >= config['trading_logic']['execution_threshold'] and trade_direction != "NEUTRAL":
                logging.info(f"SEUIL DE DÉCLENCHEMENT ATTEINT: Score {final_score:.2f} >= {config['trading_logic']['execution_threshold']}.")
                
                is_trade_already_open = any((p.type == 0 and trade_direction == "BUY") or (p.type == 1 and trade_direction == "SELL") for p in open_positions)
                if not is_trade_already_open:
                    ai_approved = True 
                    if config.get('learning', {}).get('ai_confirmation_enabled', False):
                        logging.warning("Consultation de l'IA pour un second avis sur le trade...")
                        signal_context = {"direction": trade_direction, "confidence": final_score, "scores": raw_scores}
                        ai_approved, justification = ai_assistant.confirm_trade_with_ai(signal_context)
                        logging.info(f"Avis de l'IA : {justification}")
                    
                    if not ai_approved:
                        logging.warning(f"TRADE ANNULÉ PAR L'IA: Le signal {trade_direction} pour {symbol} a été rejeté.")
                    else:
                        if config['trading_settings']['live_trading_enabled']:
                            executor.execute_trade(account_info, risk_manager, symbol, trade_direction, final_score, raw_scores)
                        else:
                            logging.info(f"SIGNAL (SIMULATION): {trade_direction} @ Score {final_score:.2f} [Validé par l'IA si activée]")
                else:
                    logging.info(f"Un trade dans la même direction ({trade_direction}) est déjà ouvert. Aucune nouvelle position ne sera prise.")

            time.sleep(15)

        except Exception as e:
            logging.error(f"Erreur inattendue dans la boucle principale: {e}", exc_info=True)
            time.sleep(30) # Pause plus longue en cas d'erreur grave

    connector.disconnect()
    logging.info("Boucle de trading terminée.")
    state.update_status("Arrêté", "Bot arrêté proprement.", is_emergency=True)

if __name__ == "__main__":
    shared_state = SharedState()
    setup_logging(shared_state)
    
    # Démarrage du serveur API dans un thread séparé
    config = load_yaml('config.yaml')
    api_host = config.get('api', {}).get('host', '127.0.0.1')
    api_port = config.get('api', {}).get('port', 5000)
    url = f"http://{api_host}:{api_port}"
    
    api_thread = threading.Thread(target=start_api_server, args=(shared_state,), daemon=True)
    api_thread.start()
    logging.info(f"Interface web démarrée sur {url}")

    # Ouvre automatiquement le navigateur web sur l'interface
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    # Lancement de la boucle de trading principale
    main_trading_loop(shared_state)
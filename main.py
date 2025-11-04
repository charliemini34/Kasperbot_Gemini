# Fichier: main.py
"""
Kasperbot - Bot de Trading MT5
Fichier principal pour l'exécution du bot.

Version: 2.0.4
"""

__version__ = "2.0.4"

import sys
import os
import time
import logging
import yaml
import threading
import re
import pytz
from datetime import datetime, time as datetime_time

# --- Imports des modules du Bot ---
import MetaTrader5 as mt5
from src.data_ingest import mt5_connector
from src.execution import mt5_executor
from src.risk import risk_manager
from src.journal import professional_journal as journal
from src.api import server as api_server
from src import shared_state # Utilise l'état partagé de l'API

# --- IMPORTS MODIFIÉS POUR LA STRATÉGIE SMC ---
from src.strategy import smc_entry_logic as smc_strategy
# --- FIN DES IMPORTS MODIFIÉS ---

# Configuration du logging
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "kasperbot.log")),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def load_config():
    """Charge la configuration depuis config.yaml."""
    logger.info("Chargement de la configuration...")
    if not os.path.exists("config.yaml"):
        logger.critical("Fichier config.yaml introuvable !")
        return None
        
    try:
        with open("config.yaml", 'r') as f:
            config = yaml.safe_load(f)
        
        log_level = config.get('logging', {}).get('level', 'INFO').upper()
        logging.getLogger().setLevel(log_level)
        logger.info(f"Niveau de logging réglé sur {log_level}")
        
        #shared_state.set_config(config)
        
        return config
    except Exception as e:
        logger.critical(f"Erreur lors du chargement de config.yaml: {e}")
        return None

# --- NOUVELLE FONCTION (log_to_api) ---
def log_to_api(message: str):
    """Envoie un message de log à l'état partagé (pour l'API)."""
    logger.info(message) # Logge aussi localement
    shared_state.add_log(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

# --- KASPERBOT (Orchestrateur principal) ---
class Kasperbot:
    """
    Classe principale du bot.
    Gère la boucle d'analyse et la logique de trading.
    """
    __version__ = "2.0.4" # Version de l'orchestrateur
    
    def __init__(self, config):
        self.config = config
        self.running = False
        self.symbols = config['mt5']['symbols']
        log_to_api(f"Bot v{self.__version__} initialisé.")

        # Initialisation du connecteur
        if not mt5_connector.connect(
            config['mt5'].get('login'), 
            config['mt5'].get('password'), 
            config['mt5'].get('server')
        ):
            log_to_api("[CRITICAL] Échec de l'initialisation MT5. Le bot ne peut pas démarrer.")
            raise ConnectionError("Échec de l'initialisation MT5.")
        
        # Initialisation des modules dépendants
        risk_manager.initialize_risk_manager(mt5_connector)
        mt5_executor.initialize_executor(mt5_connector)
        
        self.journal = journal.ProfessionalJournal(config['journal']['filepath'])
        
        # Configuration des timeframes
        self.setup_timeframes()
        # Configuration Modèle 3
        self.setup_model_3_config()

    def setup_timeframes(self):
        """Configure les timeframes pour M1/M2."""
        self.htf_tf_str = self.config['strategy']['htf_timeframe']
        self.ltf_tf_str = self.config['strategy']['ltf_timeframe']
        
        self.htf_tf = mt5_connector.get_mt5_timeframe(self.htf_tf_str)
        self.ltf_tf = mt5_connector.get_mt5_timeframe(self.ltf_tf_str)

    def setup_model_3_config(self):
        """Configure les paramètres spécifiques au Modèle 3."""
        strategy_cfg = self.config['strategy']
        self.model_3_enabled = strategy_cfg.get('model_3_enabled', False)
        self.model_3_range_tf_str = strategy_cfg.get('model_3_range_tf', 'M30')
        self.model_3_entry_tf_str = strategy_cfg.get('model_3_entry_tf', 'M5')
        
        self.model_3_range_tf = mt5_connector.get_mt5_timeframe(self.model_3_range_tf_str)
        self.model_3_entry_tf = mt5_connector.get_mt5_timeframe(self.model_3_entry_tf_str)
        
        self.model_3_trigger_time = datetime_time.fromisoformat(strategy_cfg.get('model_3_trigger_time', '15:30:00'))
        self.trading_timezone_str = strategy_cfg.get('session_timezone', 'Etc/UTC')
        
        try:
            self.trading_timezone = pytz.timezone(self.trading_timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Fuseau horaire '{self.trading_timezone_str}' inconnu. Utilisation de 'Etc/UTC'.")
            self.trading_timezone = pytz.timezone('Etc/UTC')
        
        self.last_model_3_check_date = {symbol: None for symbol in self.symbols}
        
    def start(self):
        """Démarre la boucle principale du bot."""
        self.running = True
        logger.info(f"Le bot va surveiller les symboles suivants : {self.symbols}")
        shared_state.set_status("RUNNING", f"Surveillance de {len(self.symbols)} symboles.")
        
        check_interval = self.config.get('check_interval', 60)
        
        while self.running and shared_state.is_bot_running():
            start_time = time.time()
            
            logger.info(f"--- Nouveau cycle (Intervalle: {check_interval}s) ---")
            
            for symbol in self.symbols:
                if not shared_state.is_bot_running():
                    break
                
                # La vérification du symbole est gérée dans mt5_connector.get_data()
                self.check_symbol_logic(symbol, self.config)
            
            if not shared_state.is_bot_running():
                break

            elapsed = time.time() - start_time
            sleep_time = max(0, check_interval - elapsed)
            logger.info(f"Cycle terminé. Prochaine vérification dans {sleep_time:.0f} secondes.")
            
            # Utiliser un sleep interruptible
            for _ in range(int(sleep_time)):
                if not shared_state.is_bot_running():
                    break
                time.sleep(1)

        logger.info("Boucle principale du bot terminée.")
        shared_state.set_status("STOPPED", "Boucle du bot terminée.")
        mt5_connector.disconnect()

    def check_symbol_logic(self, symbol, config):
        """
        Exécute la logique de trading complète pour UN SEUL symbole.
        """
        try:
            # 1. Gérer les trades existants
            open_positions = mt5_connector.check_open_positions(symbol)
            if open_positions > 0:
                logger.info(f"Position déjà ouverte pour {symbol}, attente...")
                # TODO: Mettre à jour l'état partagé avec les positions
                return

            # 2. Vérifier Modèle 3 (Temporel)
            signal_m3 = (None, None, None, None)
            if self.model_3_enabled:
                signal_m3 = self._run_model_3_analysis(symbol, config)
            
            if signal_m3[0]:
                if self._process_signal(symbol, *signal_m3, config):
                    return # Un trade a été pris

            # 3. Vérifier Modèles 1 & 2 (Continu)
            signal_m1_m2 = (None, None, None, None)
            signal_m1_m2 = self._run_models_1_and_2_analysis(symbol, config)
            
            if signal_m1_m2[0]:
                if self._process_signal(symbol, *signal_m1_m2, config):
                    return

        except Exception as e:
            logger.critical(f"Erreur critique lors de l'analyse de {symbol}: {e}", exc_info=True)
            shared_state.add_log(f"ERREUR [{symbol}]: {e}")

    def _run_models_1_and_2_analysis(self, symbol, config):
        """Exécute l'analyse continue M1/M2 pour un symbole."""
        logger.info(f"[{symbol}] Analyse SMC (Modèles 1 & 2)...")
        try:
            htf_lookback = config['strategy']['timeframes_config'][self.htf_tf_str]
            ltf_lookback = config['strategy']['timeframes_config'][self.ltf_tf_str]
            
            htf_data = mt5_connector.get_data(symbol, self.htf_tf, htf_lookback)
            ltf_data = mt5_connector.get_data(symbol, self.ltf_tf, ltf_lookback)

            if htf_data is None or ltf_data is None or htf_data.empty or ltf_data.empty:
                logger.warning(f"[{symbol} M1/M2] Données MTF vides, cycle sauté.")
                return None, None, None, None

            mtf_data_dict = {
                self.htf_tf_str: htf_data,
                self.ltf_tf_str: ltf_data
            }
            
            # Récupérer le pip_size (nécessaire pour l'appel de fonction)
            pip_size = config['risk']['pip_sizes'].get(symbol, config['risk']['default_pip_size'])

            # Appel de la fonction de stratégie
            signal, reason, sl_price, tp_price = smc_strategy.check_all_smc_signals(
                mtf_data_dict, 
                config,
                pip_size=pip_size
            )
            
            if not signal:
                 logger.info(f"[{symbol}] Aucun signal SMC (M1/M2) trouvé.")
                 
            return signal, reason, sl_price, tp_price

        except Exception as e:
            logger.critical(f"Erreur critique lors de l'analyse M1/M2 de {symbol}: {e}", exc_info=True)
            shared_state.add_log(f"ERREUR M1/M2 [{symbol}]: {e}")
            return None, None, None, None

    def _run_model_3_analysis(self, symbol, config):
        """Vérifie et exécute la stratégie M3 pour un symbole."""
        current_time_utc = datetime.now(pytz.utc)
        current_time_local = current_time_utc.astimezone(self.trading_timezone)
        
        last_check = self.last_model_3_check_date.get(symbol)
        
        if (current_time_local.time() >= self.model_3_trigger_time and
            (last_check is None or current_time_local.date() > last_check)):
            
            log_to_api(f"[{symbol}] Analyse Modèle 3 {self.model_3_range_tf_str}/{self.model_3_entry_tf_str}...")
            
            self.last_model_3_check_date[symbol] = current_time_local.date()
            
            try:
                range_lookback = config['strategy'].get('model_3_range_lookback', 10)
                entry_lookback = config['strategy'].get('model_3_entry_lookback', 50)
                
                range_data = mt5_connector.get_data(symbol, self.model_3_range_tf, range_lookback)
                entry_data = mt5_connector.get_data(symbol, self.model_3_entry_tf, entry_lookback)
                
                if range_data is None or entry_data is None or range_data.empty or entry_data.empty:
                    logger.warning(f"[{symbol} M3] Données vides, cycle M3 sauté.")
                    return None, None, None, None

                # Récupérer le pip_size (nécessaire pour l'appel de fonction)
                pip_size = config['risk']['pip_sizes'].get(symbol, config['risk']['default_pip_size'])

                # Appel de la fonction de stratégie
                return smc_strategy.check_model_3_opening_range(
                    range_data,
                    entry_data,
                    config,
                    self.model_3_range_tf_str,
                    self.model_3_entry_tf_str,
                    pip_size=pip_size
                )
                
            except Exception as e:
                logger.error(f"Erreur durant l'analyse Modèle 3 de {symbol}: {e}", exc_info=True)
                shared_state.add_log(f"[{symbol}] Erreur: Échec analyse M3.")

        return None, None, None, None # Pas le moment

    def _process_signal(self, symbol, signal, reason, sl_price, tp_price, config):
        """Traite un signal de trading trouvé (calcul de risque, exécution)."""
        
        log_to_api(f"SIGNAL TROUVÉ [{symbol}]: {reason}")
        
        def _extract_model(reason_str):
            match = re.search(r'\[(M\d)\]', reason_str)
            if match:
                return match.group(1)
            return "UNKNOWN"
        model_id = _extract_model(reason)
        
        if not sl_price or not tp_price:
             log_to_api(f"[{symbol}] Signal ignoré: SL/TP invalide.")
             return False

        # A. Calcul du risque
        
        # Récupérer le prix d'entrée (nécessaire si le trade_id échoue)
        data_tf_str = self.ltf_tf_str
        if model_id == "M3":
            data_tf_str = self.model_3_entry_tf_str
        data_tf_mt5 = mt5_connector.get_mt5_timeframe(data_tf_str)
        entry_data = mt5_connector.get_data(symbol, data_tf_mt5, 2)
        if entry_data is None or entry_data.empty:
            log_to_api(f"[{symbol}] Erreur: Prix d'entrée (pour SL) indisponible.")
            return False
        entry_price_fallback = entry_data['close'].iloc[-1]
            
        # Appel corrigé pour le calcul de risque
        lot_size = risk_manager.calculate_lot_size(
            config['risk']['risk_percent'],
            sl_price, # Appel correct (envoi du prix SL)
            symbol=symbol
        )
        
        if lot_size is None or lot_size <= 0:
            log_to_api(f"[{symbol}] Signal ignoré: Volume 0.")
            return False
        
        logger.info(f"Taille de lot calculée ({symbol}) : {lot_size} (pour {config['risk']['risk_percent']}% de risque)")

        # B. Exécution de l'ordre

        # --- MODIFICATION (Version 2.0.4) ---
        # Formatage du commentaire sans espaces, basé sur le script v15.1.0 et v3
        
        # 1. Nettoyer la 'reason' pour en faire un identifiant court
        # Remplace tout ce qui n'est pas une lettre/chiffre par un '_'
        reason_simple = re.sub(r'[^a-zA-Z0-9]', '_', reason)
        # Remplace les '_' multiples par un seul
        reason_simple = re.sub(r'__+', '_', reason_simple)
        
        # 2. Créer le commentaire sans espaces
        trade_comment = f"KasperBot_{model_id}_{reason_simple}"
        
        # 3. Tronquer à 31 caractères
        trade_comment = trade_comment[:20]
        # --- FIN MODIFICATION ---

        trade_id = mt5_executor.place_order(
            symbol=symbol,
            order_type=signal,
            volume=lot_size,
            sl_price=sl_price,
            tp_price=tp_price,
            comment=trade_comment # Utilise le commentaire nettoyé
        )
        
        # C. Journalisation
        if trade_id:
            entry_price_filled = mt5_executor.get_last_entry_price(trade_id)
            if entry_price_filled is None: 
                entry_price_filled = entry_price_fallback
                
            log_to_api(f"TRADE EXÉCUTÉ [{symbol}]: {signal} {lot_size} lots. ID: {trade_id}")
            
            # Log la raison complète dans le journal (pas de limite de taille ici)
            # --- CORRECTION ---
            # 1. Créer le dictionnaire attendu par le journal
            # 2. Appeler self.journal.record_trade
            trade_data = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'symbol': symbol,
                'type': signal,  # Le journal attend 'type' (pas 'order_type')
                'volume': lot_size,
                'entry_price': entry_price_filled,
                'sl': sl_price,  # Le journal attend 'sl' (pas 'sl_price')
                'tp': tp_price,  # Le journal attend 'tp' (pas 'tp_price')
                'reason': reason,
                'setup_model': model_id,
                'status': "OPEN",
                'position_id': trade_id
            }
            
            self.journal.record_trade(trade_data)
            # --- FIN CORRECTION ---
            
            return True
        else:
            log_to_api(f"[{symbol}] Échec exécution MT5.")
            return False

# --- FIN DE LA CLASSE KASPERBOT ---

def run_bot_thread(config):
    """Fonction cible pour le thread du bot."""
    try:
        logger.info("Initialisation du bot...")
        bot = Kasperbot(config)
        logger.info("Bot initialisé avec succès.")
        bot.start()
    except Exception as e:
        logger.critical(f"Erreur critique lors de l'initialisation ou du démarrage du bot: {e}", exc_info=True)
        shared_state.set_status("CRASHED", str(e))

def start_api_server_thread(config):
    """Démarre le serveur Flask dans un thread séparé."""
    if not config.get('api', {}).get('enabled', False):
        logger.info("API server est désactivé dans la configuration.")
        return
        
    def run_server():
        logger.info(f"Démarrage du serveur API...")
        try:
            api_server.start_api_server(shared_state)
        except Exception as e:
            logger.critical(f"Échec du démarrage du serveur API: {e}", exc_info=True)

    api_thread = threading.Thread(target=run_server, daemon=True)
    api_thread.start()
    logger.info(f"Serveur API démarré (voir logs API pour le port).")


if __name__ == "__main__":
    logger.info("Chargement de la configuration...")
    config = load_config()
    
    if config:
        # Démarrer l'API
        start_api_server_thread(config)
        
        # Démarrer le Bot
        bot_thread = threading.Thread(target=run_bot_thread, args=(config,), daemon=True)
        bot_thread.start()
        
        # Boucle principale pour garder le programme en vie
        try:
            while bot_thread.is_alive():
                bot_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("Arrêt manuel demandé (KeyboardInterrupt)...")
            shared_state.stop_bot()
            bot_thread.join() # Attendre que le bot s'arrête proprement
            
    else:
        logger.critical("Échec du chargement de la configuration. Arrêt.")

    logger.info("Programme principal terminé.")
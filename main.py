# Fichier: main.py
"""
Kasperbot - Bot de Trading MT5
Fichier principal pour l'exécution du bot.

Version: 1.2.5 (Correction KeyError 'pip_size' + TypeError 'calculate_lot_size')
"""

__version__ = "1.2.5"

import sys
import os
import time
import logging
import yaml
import threading
# --- Ajouts v1.2.0 ---
import re
import pytz
from datetime import datetime, time as datetime_time
# --- Fin Ajouts ---


# --- Imports des modules du Bot ---
import MetaTrader5 as mt5
from src.data_ingest import mt5_connector
from src.execution import mt5_executor
from src.risk import risk_manager
from src.journal import professional_journal as journal
from src.api import server as api_server
from src import shared_state


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
        
        shared_state.set_config(config)
        
        return config
    except Exception as e:
        logger.critical(f"Erreur lors du chargement de config.yaml: {e}")
        return None

def initialize_bot(config):
    """Initialise la connexion MT5 et les modules dépendants."""
    logger.info("Initialisation du bot...")
    mt5_config = config.get('mt5')
    
    if not mt5_config:
        logger.critical("Section 'mt5' manquante dans config.yaml.")
        return False

    if not mt5_connector.connect(
        mt5_config.get('login'), 
        mt5_config.get('password'), 
        mt5_config.get('server')
    ):
        logger.critical("Échec de l'initialisation du bot. Vérifiez vos identifiants MT5.")
        return False
    
    risk_manager.initialize_risk_manager(mt5_connector)
    mt5_executor.initialize_executor(mt5_connector)
    
    global htf_tf_str, ltf_tf_str, htf_tf, ltf_tf
    htf_tf_str = config['strategy']['htf_timeframe']
    ltf_tf_str = config['strategy']['ltf_timeframe']
    htf_tf = mt5_connector.get_mt5_timeframe(htf_tf_str)
    ltf_tf = mt5_connector.get_mt5_timeframe(ltf_tf_str)
    
    global model_3_enabled, model_3_range_tf_str, model_3_entry_tf_str, model_3_range_tf, model_3_entry_tf
    global model_3_trigger_time, trading_timezone, last_model_3_check_date
    
    strategy_cfg = config['strategy']
    model_3_enabled = strategy_cfg.get('model_3_enabled', False)
    model_3_range_tf_str = strategy_cfg.get('model_3_range_tf', 'M30')
    model_3_entry_tf_str = strategy_cfg.get('model_3_entry_tf', 'M5')
    
    model_3_range_tf = mt5_connector.get_mt5_timeframe(model_3_range_tf_str)
    model_3_entry_tf = mt5_connector.get_mt5_timeframe(model_3_entry_tf_str)
    
    model_3_trigger_time = datetime_time.fromisoformat(strategy_cfg.get('model_3_trigger_time', '15:30:00'))
    trading_timezone_str = strategy_cfg.get('session_timezone', 'Etc/UTC')
    
    try:
        trading_timezone = pytz.timezone(trading_timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.warning(f"Fuseau horaire '{trading_timezone_str}' inconnu. Utilisation de 'Etc/UTC'.")
        trading_timezone = pytz.timezone('Etc/UTC')
    
    last_model_3_check_date = {symbol: None for symbol in config['mt5'].get('symbols', [])}

    logger.info("Bot initialisé avec succès.")
    return True


# --- NOUVELLE FONCTION v1.2.5 ---
def _get_pip_size(symbol, config):
    """
    Récupère la taille de pip correcte pour un symbole donné depuis la config.
    """
    try:
        # Vérifier d'abord les tailles spécifiques
        if symbol in config['risk']['pip_sizes']:
            return config['risk']['pip_sizes'][symbol]
        # Sinon, utiliser la taille par défaut
        return config['risk']['default_pip_size']
    except KeyError as e:
        logger.error(f"Configuration 'risk.pip_sizes' ou 'risk.default_pip_size' manquante! {e}")
        # Retourner une valeur par défaut de secours
        return 0.0001


# --- _process_signal (MODIFIÉ v1.2.5) ---
def _process_signal(symbol, signal, reason, sl_price, tp_price, config):
    """
    Fonction centralisée pour calculer le risque et exécuter un trade.
    """
    if not signal:
        return False

    logger.warning(f"SIGNAL DE TRADING DÉTECTÉ ({symbol}) : {signal} | {reason}")
    shared_state.add_log(f"SIGNAL [{symbol}]: {reason}")

    def _extract_model(reason_str):
        match = re.search(r'\[(M\d)\]', reason_str)
        if match:
            return match.group(1)
        return "UNKNOWN"
    model_id = _extract_model(reason)

    if not sl_price or not tp_price:
         logger.warning(f"[{symbol}] Signal trouvé mais SL/TP invalide. SL={sl_price}, TP={tp_price}. Trade annulé.")
         shared_state.add_log(f"[{symbol}] Signal ignoré: SL/TP invalide.")
         return False

    # A. Calcul du risque
    
    # --- CORRECTION v1.2.5 (TypeError) ---
    data_tf_str = ltf_tf_str
    if model_id == "M3":
        data_tf_str = model_3_entry_tf_str
        
    data_tf_mt5 = mt5_connector.get_mt5_timeframe(data_tf_str)
    entry_data = mt5_connector.get_data(symbol, data_tf_mt5, 2)
    
    if entry_data is None or entry_data.empty:
        logger.error(f"[{symbol}] Impossible de récupérer le prix d'entrée, trade annulé.")
        shared_state.add_log(f"[{symbol}] Erreur: Prix d'entrée (pour SL) indisponible.")
        return False
    entry_price = entry_data['close'].iloc[-1]
    
    # Calcul des SL pips (requis par risk_manager.py)
    try:
        pip_size = _get_pip_size(symbol, config) # Utilisation de la nouvelle fonction helper
             
        sl_pips = abs(entry_price - sl_price) / pip_size
        if sl_pips <= 0:
            logger.warning(f"[{symbol}] SL pips est 0. Trade annulé pour éviter div by zero.")
            return False
            
    except Exception as e:
        logger.error(f"[{symbol}] Erreur calcul SL pips: {e}. Trade annulé.")
        return False

    lot_size = risk_manager.calculate_lot_size(
        config['risk']['risk_percent'],
        sl_pips, # <-- FIX: Passage des Pips
        symbol=symbol
        # <-- FIX: 'entry_price' supprimé
    )
    # --- FIN CORRECTION v1.2.5 ---
    
    if lot_size is None or lot_size <= 0:
        logger.error(f"Calcul de lot invalide ({lot_size}) pour {symbol}. Annulation.")
        shared_state.add_log(f"[{symbol}] Signal ignoré: Volume 0.")
        return False
    
    logger.info(f"Taille de lot calculée ({symbol}) : {lot_size} (pour {config['risk']['risk_percent']}% de risque)")

    # B. Exécution de l'ordre
    trade_id = mt5_executor.place_order(
        symbol=symbol,
        order_type=signal,
        volume=lot_size,
        sl_price=sl_price,
        tp_price=tp_price,
        comment=f"[{model_id}] {reason}"
    )
    
    # C. Journalisation
    if trade_id:
        logger.warning(f"Ordre {trade_id} ({symbol}) placé avec succès.")
        entry_price_filled = mt5_executor.get_last_entry_price(trade_id)
        if entry_price_filled is None:
            entry_price_filled = entry_price
            
        shared_state.add_log(f"TRADE EXÉCUTÉ [{symbol}]: {signal} {lot_size} lots. ID: {trade_id}")
        
        journal.log_trade(
            timestamp=time.strftime('%Y-%m-%d %H:%M:%S'),
            symbol=symbol,
            order_type=signal,
            volume=lot_size,
            entry_price=entry_price_filled,
            sl_price=sl_price,
            tp_price=tp_price,
            reason=reason,
            setup_model=model_id,
            status="OPEN",
            position_id=trade_id
        )
        return True
    else:
        logger.error(f"Échec lors de la tentative de placement de l'ordre pour {symbol}.")
        shared_state.add_log(f"[{symbol}] Échec exécution MT5.")
        return False


# --- MODIFIÉ v1.2.5 ---
def _run_models_1_and_2_analysis(symbol, config):
    """Exécute l'analyse continue M1/M2 pour un symbole."""
    logger.info(f"[{symbol}] Analyse SMC (Modèles 1 & 2)...")
    try:
        # 1. Récupérer les données
        
        # --- CORRECTION v1.2.1 (KeyError) ---
        htf_lookback = config['strategy']['timeframes_config'][htf_tf_str]
        ltf_lookback = config['strategy']['timeframes_config'][ltf_tf_str]
        
        htf_data = mt5_connector.get_data(
            symbol, 
            htf_tf, 
            htf_lookback 
        )
        ltf_data = mt5_connector.get_data(
            symbol, 
            ltf_tf, 
            ltf_lookback
        )
        # --- FIN CORRECTION v1.2.1 ---

        if htf_data is None or ltf_data is None or htf_data.empty or ltf_data.empty:
            logger.warning(f"[{symbol} M1/M2] Données MTF vides, cycle sauté.")
            return None, None, None, None

        mtf_data_dict = {
            htf_tf_str: htf_data,
            ltf_tf_str: ltf_data
        }
        
        # 2. Analyser les patterns SMC
        
        # --- MODIFICATION v1.2.5: Passer le pip_size ---
        pip_size = _get_pip_size(symbol, config)
        
        signal, reason, sl_price, tp_price = smc_strategy.check_all_smc_signals(
            mtf_data_dict, 
            config,
            pip_size # <-- FIX
        )
        # --- FIN MODIFICATION ---
        
        if not signal:
             logger.info(f"[{symbol}] Aucun signal SMC (M1/M2) trouvé.")
             
        return signal, reason, sl_price, tp_price

    except Exception as e:
        logger.critical(f"Erreur critique lors de l'analyse M1/M2 de {symbol}: {e}", exc_info=True)
        shared_state.add_log(f"ERREUR M1/M2 [{symbol}]: {e}")
        return None, None, None, None

# --- MODIFIÉ v1.2.5 ---
def _run_model_3_analysis(symbol, config):
    """Vérifie et exécute la stratégie M3 pour un symbole."""
    global last_model_3_check_date
    
    current_time_utc = datetime.now(pytz.utc)
    current_time_local = current_time_utc.astimezone(trading_timezone)
    
    last_check = last_model_3_check_date.get(symbol)
    
    if (current_time_local.time() >= model_3_trigger_time and
        (last_check is None or current_time_local.date() > last_check)):
        
        logger.info(f"[{symbol}] Déclenchement du Modèle 3 (Opening Range) à {current_time_local.strftime('%H:%M:%S')}")
        shared_state.add_log(f"[{symbol}] Analyse Modèle 3 {model_3_range_tf_str}/{model_3_entry_tf_str}...")
        
        last_model_3_check_date[symbol] = current_time_local.date()
        
        try:
            # Lire les 'counts' depuis la config
            range_lookback = config['strategy'].get('model_3_range_lookback', 10)
            entry_lookback = config['strategy'].get('model_3_entry_lookback', 50)
            
            range_data = mt5_connector.get_data(symbol, model_3_range_tf, range_lookback)
            entry_data = mt5_connector.get_data(symbol, model_3_entry_tf, entry_lookback)
            
            if range_data is None or entry_data is None or range_data.empty or entry_data.empty:
                logger.warning(f"[{symbol} M3] Données vides, cycle M3 sauté.")
                return None, None, None, None

            # --- MODIFICATION v1.2.5: Passer le pip_size ---
            pip_size = _get_pip_size(symbol, config)

            return smc_strategy.check_model_3_opening_range(
                range_data,
                entry_data,
                config,
                model_3_range_tf_str,
                model_3_entry_tf_str,
                pip_size # <-- FIX
            )
            # --- FIN MODIFICATION ---
            
        except Exception as e:
            logger.error(f"Erreur durant l'analyse Modèle 3 de {symbol}: {e}", exc_info=True)
            shared_state.add_log(f"[{symbol}] Erreur: Échec analyse M3.")

    return None, None, None, None # Pas le moment

def check_symbol_logic(symbol, config):
    """
    Exécute la logique de trading complète pour UN SEUL symbole.
    Cette fonction est appelée en boucle par run_bot.
    """
    try:
        # 1. Vérifier les positions ouvertes
        open_positions = mt5_connector.check_open_positions(symbol)
        if open_positions > 0:
            logger.info(f"Position déjà ouverte pour {symbol}, attente...")
            return

        # 2. Vérifier Modèle 3 (Temporel)
        signal_m3 = (None, None, None, None)
        if model_3_enabled:
            signal_m3 = _run_model_3_analysis(symbol, config)
        
        if signal_m3[0]:
            if _process_signal(symbol, *signal_m3, config):
                return 

        # 3. Vérifier Modèles 1 & 2 (Continu)
        signal_m1_m2 = (None, None, None, None)
        signal_m1_m2 = _run_models_1_and_2_analysis(symbol, config)
        
        if signal_m1_m2[0]:
            if _process_signal(symbol, *signal_m1_m2, config):
                return 

    except Exception as e:
        logger.critical(f"Erreur critique lors de l'analyse de {symbol}: {e}", exc_info=True)
        shared_state.add_log(f"ERREUR [{symbol}]: {e}")
# --- FIN REFACTORING ---


def run_bot():
    """
    Boucle principale d'exécution du bot.
    """
    logger.info("Démarrage de la boucle principale du bot...")
    config = load_config()
    if not config:
        shared_state.stop_bot()
        return

    symbols_list = config['mt5'].get('symbols')
    if not symbols_list or not isinstance(symbols_list, list):
        logger.error("Configuration 'mt5.symbols' manquante ou invalide. Doit être une liste. Arrêt.")
        shared_state.stop_bot()
        return
    
    logger.info(f"Le bot va surveiller les symboles suivants : {symbols_list}")
    shared_state.set_status("RUNNING", f"Surveillance de {len(symbols_list)} symboles.")
    
    check_interval = config.get('check_interval', 60)
    
    while shared_state.is_bot_running():
        try:
            logger.info(f"--- Nouveau cycle (Intervalle: {check_interval}s) ---")
            
            for symbol in symbols_list:
                if not shared_state.is_bot_running():
                    break 
                
                check_symbol_logic(symbol, config)
                
                time.sleep(1) 

            if shared_state.is_bot_running():
                logger.info(f"Cycle terminé. Prochaine vérification dans {check_interval} secondes.")
                time.sleep(check_interval)

        except KeyboardInterrupt:
            logger.info("Arrêt manuel du bot (KeyboardInterrupt).")
            shared_state.stop_bot()
            
        except Exception as e:
            logger.critical(f"Erreur critique dans la boucle principale: {e}", exc_info=True)
            shared_state.set_status("ERROR", f"Erreur critique: {e}")
            time.sleep(check_interval * 2)

    logger.info("Boucle principale du bot terminée.")
    shared_state.set_status("STOPPED", "Boucle du bot terminée.")
    mt5_connector.disconnect()

def start_api_server(config):
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
    config = load_config()
    
    if config:
        start_api_server(config)
        
        if initialize_bot(config):
            run_bot()
        else:
            logger.critical("Échec de l'initialisation du bot. Arrêt.")
    else:
        logger.critical("Échec du chargement de la configuration. Arrêt.")

    logger.info("Programme principal terminé.")
"""
Kasperbot - Bot de Trading MT5
Fichier principal pour l'exécution du bot.
"""

import sys
import os
import time
import logging
import yaml
import threading

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
        
        # --- CORRECTION POUR L'API (shared_state) ---
        # L'API (v13) a besoin d'accéder à la config.
        shared_state.set_config(config)
        # --- FIN CORRECTION ---
        
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
    
    # Initialisation des modules (versions procédurales)
    risk_manager.initialize_risk_manager(mt5_connector)
    mt5_executor.initialize_executor(mt5_connector)
    
    logger.info("Bot initialisé avec succès.")
    return True

# --- REFACTORING POUR MULTI-SYMBOLES ---
def check_symbol_logic(symbol, config):
    """
    Exécute la logique de trading complète pour UN SEUL symbole.
    Cette fonction est appelée en boucle par run_bot.
    """
    try:
        logger.info(f"--- Analyse du symbole : {symbol} ---")
        
        # 1. Vérifier les positions ouvertes
        open_positions = mt5_connector.check_open_positions(symbol)
        if open_positions > 0:
            logger.info(f"Position déjà ouverte pour {symbol}, attente...")
            return # Passer au symbole suivant

        # 2. Récupérer les données
        timeframes_config = config['strategy'].get('timeframes_config')
        logger.debug(f"Récupération des données multi-timeframe pour {symbol}...")
        mtf_data = mt5_connector.get_mtf_data(symbol, timeframes_config)
        
        if not mtf_data or any(v is None for v in mtf_data.values()):
            logger.warning(f"Données MTF ({symbol}) incomplètes. Symbole suivant.")
            return
        
        logger.debug(f"Données MTF ({symbol}) récupérées avec succès.")

        # 3. Analyser les patterns SMC
        signal, reason, sl_price, tp_price = smc_strategy.check_smc_signal(
            mtf_data, 
            config
        )
        
        # 4. Exécution
        if signal:
            logger.warning(f"SIGNAL DE TRADING DÉTECTÉ ({symbol}) : {signal} | {reason}")
            
            # A. Calcul du risque
            lot_size = risk_manager.calculate_lot_size(
                config['risk']['risk_percent'],
                sl_price,
                symbol=symbol # Passage du symbole (crucial !)
            )
            
            if lot_size is None or lot_size <= 0:
                logger.error(f"Calcul de lot invalide ({lot_size}) pour {symbol}. Annulation.")
                return
            
            logger.info(f"Taille de lot calculée ({symbol}) : {lot_size} (pour {config['risk']['risk_percent']}% de risque)")

            # B. Exécution de l'ordre
            trade_id = mt5_executor.place_order(
                symbol=symbol,
                order_type=signal,
                volume=lot_size,
                sl_price=sl_price,
                tp_price=tp_price
            )
            
            # C. Journalisation
            if trade_id:
                logger.warning(f"Ordre {trade_id} ({symbol}) placé avec succès.")
                entry_price = mt5_executor.get_last_entry_price(trade_id)
                
                journal.log_trade(
                    timestamp=time.strftime('%Y-%m-%d %H:%M:%S'),
                    symbol=symbol,
                    order_type=signal,
                    volume=lot_size,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    reason=reason,
                    status="OPEN"
                )
            else:
                logger.error(f"Échec lors de la tentative de placement de l'ordre pour {symbol}.")

        else:
            logger.info(f"Aucun signal SMC valide pour {symbol} pour le moment.")

    except Exception as e:
        logger.critical(f"Erreur critique lors de l'analyse de {symbol}: {e}", exc_info=True)

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

    # --- MODIFICATION POUR MULTI-SYMBOLES ---
    # On récupère la LISTE des symboles
    symbols_list = config['mt5'].get('symbols')
    if not symbols_list or not isinstance(symbols_list, list):
        logger.error("Configuration 'mt5.symbols' manquante ou invalide. Doit être une liste. Arrêt.")
        shared_state.stop_bot()
        return
    
    logger.info(f"Le bot va surveiller les symboles suivants : {symbols_list}")
    # --- FIN MODIFICATION ---
    
    check_interval = config.get('check_interval', 60)

    strategy_name = config['strategy'].get('name', 'N/A')
    if strategy_name != "SMC_OTE":
        logger.error(f"Stratégie '{strategy_name}' non reconnue. Arrêt.")
        shared_state.stop_bot()
        return
    
    while shared_state.is_bot_running():
        try:
            logger.info(f"--- Nouveau cycle (Intervalle: {check_interval}s) ---")
            
            # --- BOUCLE MULTI-SYMBOLES ---
            for symbol in symbols_list:
                if not shared_state.is_bot_running():
                    break # Sortir de la boucle si le bot est arrêté
                
                # Appel de la logique pour chaque symbole, un par un
                check_symbol_logic(symbol, config)
                
                # Petite pause entre chaque symbole pour ne pas surcharger MT5
                time.sleep(1) 
            # --- FIN BOUCLE ---

            if shared_state.is_bot_running():
                logger.info(f"Cycle terminé. Prochaine vérification dans {check_interval} secondes.")
                time.sleep(check_interval)

        except KeyboardInterrupt:
            logger.info("Arrêt manuel du bot (KeyboardInterrupt).")
            shared_state.stop_bot()
            
        except Exception as e:
            # Erreur critique hors de la boucle de symbole (ex: chargement config)
            logger.critical(f"Erreur critique dans la boucle principale: {e}", exc_info=True)
            time.sleep(check_interval * 2)

    logger.info("Boucle principale du bot terminée.")
    mt5_connector.disconnect()

def start_api_server(config):
    """Démarre le serveur Flask dans un thread séparé."""
    if not config.get('api', {}).get('enabled', False):
        logger.info("API server est désactivé dans la configuration.")
        return
        
    def run_server():
        logger.info(f"Démarrage du serveur API...")
        try:
            # Appel de la fonction de votre fichier 'src/api/server.py'
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
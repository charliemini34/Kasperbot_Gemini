# main.py

import sys
import logging
import time
from datetime import datetime
import pytz
import traceback
import pandas as pd
import numpy as np
import yaml
from threading import Thread

# Imports des modules du bot
from src.data_ingest.mt5_connector import MT5Connector
from src.execution.mt5_executor import MT5Executor
from src.risk.risk_manager import RiskManager
from src.journal.professional_journal import ProfessionalJournal
from src.shared_state import SharedState

# --- NOUVEAUX IMPORTS SMC ---
from src.strategy.smc_orchestrator import SMCOrchestrator
from src.analysis.market_structure import MarketStructure
from src.patterns.pattern_detector import PatternDetector
# --- FIN NOUVEAUX IMPORTS ---

from src.api.server import start_flask_app # Importation API

# Version
BOT_VERSION = "v20.0.1 (SMC Integration Fix)"

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_activity.log", mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger('root')

# Variables globales pour l'état partagé et l'API
shared_state = SharedState()
connector_global = None # Pour l'API

def load_config(config_path='config.yaml'):
    """Charge la configuration depuis le fichier YAML."""
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)
            log.info(f"Configuration '{config_path}' chargée avec succès.")
            return config
    except FileNotFoundError:
        log.error(f"Erreur: Le fichier '{config_path}' est introuvable.")
        return None
    except yaml.YAMLError as e:
        log.error(f"Erreur lors du parsing de '{config_path}': {e}")
        return None
    except Exception as e:
        log.error(f"Erreur inattendue lors du chargement de la config: {e}")
        return None

def initialize_mt5(config):
    """Initialise le Connecteur et l'Executor MT5."""
    try:
        connector = MT5Connector(config=config.get('mt5', {}))
        if not connector.is_connected():
            log.critical("Échec de l'initialisation du connecteur MT5. Arrêt.")
            return None, None
        
        executor = MT5Executor(
            mt5_instance=connector.get_mt5_instance(), 
            config=config,
            shared_state=shared_state # Transmettre l'état partagé
        )
        
        # Premier check des trades fermés (pour le journal)
        executor.check_closed_positions_pnl()
        
        return connector, executor
    except Exception as e:
        log.error(f"Erreur critique lors de l'initialisation MT5: {e}", exc_info=True)
        return None, None

def setup_logging(config):
    """Configure le niveau de logging."""
    log_level = config.get('logging', {}).get('level', 'INFO').upper()
    try:
        logging.getLogger().setLevel(log_level)
        log.info(f"Niveau de logging réglé sur: {log_level}")
    except ValueError:
        log.warning(f"Niveau de logging '{log_level}' invalide. Utilisation de INFO.")
        logging.getLogger().setLevel(logging.INFO)

def main_trading_loop(config, connector, executor, journal):
    """
    Boucle principale du bot de trading.
    MODIFIÉE pour utiliser SMCOrchestrator.
    """
    
    global connector_global # Mettre à jour la référence globale pour l'API
    connector_global = connector

    # Récupérer les paramètres de trading
    trading_config = config.get('trading_settings', {})
    symbols_to_trade = trading_config.get('symbols', [])
    cycle_sleep_seconds = trading_config.get('cycle_sleep_seconds', 300)
    trading_enabled = trading_config.get('trading_enabled', True)
    
    if not symbols_to_trade:
        log.critical("Aucun symbole à trader n'est défini dans 'config.yaml'. Arrêt de la boucle.")
        return

    log.info(f"Démarrage de la boucle de trading {BOT_VERSION}...")
    log.info(f"Symboles surveillés: {symbols_to_trade}")
    log.info(f"Trading activé: {trading_enabled}")

    # Initialisation des instances de stratégie (une par symbole)
    strategy_instances = {}
    
    for symbol in symbols_to_trade:
        try:
            # Créer un RiskManager *spécifique* pour ce symbole
            risk_manager = RiskManager(config, executor, symbol)
            
            # Créer l'Orchestrateur SMC pour ce symbole
            orchestrator = SMCOrchestrator(
                connector=connector,
                executor=executor,
                risk_manager=risk_manager,
                journal=journal,
                config=config,
                shared_state=shared_state,
                symbol=symbol # Transmettre le symbole
            )
            strategy_instances[symbol] = orchestrator
            log.info(f"Orchestrateur SMC initialisé pour {symbol}.")
            
        except Exception as e:
            log.error(f"Échec de l'initialisation de l'orchestrateur pour {symbol}: {e}", exc_info=True)

    if not strategy_instances:
        log.critical("Aucune instance de stratégie n'a pu être initialisée. Arrêt.")
        return

    # --- Gestion du premier cycle (Synchro) ---
    is_first_cycle = True
    
    # --- Création du RiskManager principal (pour les checks globaux) ---
    # Il utilise le premier symbole de la liste pour l'initialisation
    try:
        main_rm = RiskManager(config, executor, symbols_to_trade[0])
    except Exception as e:
        log.critical(f"Échec de l'initialisation du RiskManager principal: {e}", exc_info=True)
        return

    # Boucle de trading principale
    while True:
        start_time = time.time()
        
        try:
            # 1. Vérifications globales de Risque (avant toute analyse)
            
            # --- CORRECTION ERREUR 1 ---
            # (main_rm a maintenant la fonction is_daily_loss_limit_reached)
            limit_reached, pnl = main_rm.is_daily_loss_limit_reached()
            if limit_reached:
                log.critical(f"LIMITE DE PERTE QUOTIDIENNE ATTEINTE ({pnl:.2f}). Trading suspendu.")
                time.sleep(cycle_sleep_seconds)
                continue
            
            # (Gestion des positions ouvertes - inchangée)
            open_positions = executor.get_open_positions()
            shared_state.update_open_positions(open_positions) # Mettre à jour l'état
            
            if open_positions:
                log.info(f"Gestion des {len(open_positions)} positions ouvertes...")
                equity = main_rm.get_account_balance()
                current_risk_pct = main_rm.get_current_total_risk(open_positions, equity)
                shared_state.set_current_risk_pct(current_risk_pct)
                
                # Appliquer BE/TSL sur toutes les positions
                for pos in open_positions:
                    pos_symbol = pos.symbol
                    if pos_symbol not in strategy_instances:
                        log.warning(f"Position ouverte sur {pos_symbol} non gérée (pas dans config).")
                        continue
                    
                    # Utiliser l'orchestrateur (ou RM) du symbole concerné
                    pos_rm = strategy_instances[pos_symbol].risk_manager
                    
                    pos_tick = connector.get_current_tick(pos_symbol)
                    pos_ohlc = connector.get_market_data(pos_symbol, 'M15', 100) # Données pour ATR
                    
                    pos_rm.manage_open_positions(
                        [pos], # Gérer cette position
                        pos_tick, 
                        pos_ohlc, 
                        shared_state.get_trade_context(pos.ticket)
                    )

            # 2. Logique de premier cycle (Synchro)
            if is_first_cycle:
                log.info("Premier cycle: trading désactivé pour synchro.")
                current_trading_enabled = False
                is_first_cycle = False
            else:
                current_trading_enabled = trading_enabled

            # 3. Boucle d'analyse par symbole
            if not current_trading_enabled:
                log.info("Analyse de cycle (Trading Désactivé).")

            for symbol, orchestrator in strategy_instances.items():
                
                log.info(f"--- Analyse {symbol} ---")
                
                # Mettre à jour les données MTF (déplacé ici, spécifique au symbole)
                mtf_data = connector.get_mtf_data(symbol)
                if not mtf_data or mtf_data.get('M15', pd.DataFrame()).empty:
                    log.warning(f"Données MTF (M15) manquantes pour {symbol}. Cycle ignoré.")
                    continue
                
                # --- NOUVELLE LOGIQUE SMC ---
                # (Remplace tout l'ancien bloc 'detector' et 'risk_manager')
                try:
                    # L'orchestrateur gère tout :
                    # 1. Récupère les données (via son propre connector)
                    # 2. Analyse Structure (MarketStructure)
                    # 3. Détecte POI (PatternDetector)
                    # 4. Filtre (Biais, OTE)
                    # 5. Cherche entrée (SMCEntryLogic)
                    # 6. Calcule risque (RiskManager)
                    # 7. Exécute (MT5Executor)
                    
                    # On n'exécute que si le trading est activé
                    if current_trading_enabled:
                        orchestrator.run_strategy()
                    else:
                        # En mode synchro, on peut lancer une analyse "sèche"
                        # (Pour l'instant, on l'ignore pour ne pas spammer les logs)
                        pass 

                except Exception as e:
                    log.error(f"Erreur analyse SMC sur {symbol}: {e}", exc_info=True)
                    traceback.print_exc() # Imprimer la trace complète
                # --- FIN NOUVELLE LOGIQUE SMC ---

            if is_first_cycle:
                log.info("Fin cycle synchro. Trading activé.")

        except Exception as e:
            log.critical(f"Erreur fatale dans la boucle principale: {e}", exc_info=True)
            traceback.print_exc()

        # Gestion du temps de cycle
        elapsed_time = time.time() - start_time
        sleep_time = max(1, cycle_sleep_seconds - elapsed_time)
        log.info(f"Cycle terminé. Attente de {sleep_time:.1f}s.")
        time.sleep(sleep_time)

def run_bot():
    """Fonction principale pour démarrer le bot."""
    log.info("Démarrage du Kasperbot...")
    
    config = load_config('config.yaml')
    if config is None:
        sys.exit(1)
        
    setup_logging(config)
    
    connector, executor = initialize_mt5(config)
    if connector is None or executor is None:
        sys.exit(1)
        
    journal = ProfessionalJournal(config.get('journal', {}))
    
    # Démarrer l'API Flask dans un thread séparé
    api_config = config.get('api', {})
    if api_config.get('enabled', False):
        api_thread = Thread(
            target=start_flask_app, 
            args=(shared_state, lambda: connector_global, executor, journal, api_config),
            daemon=True
        )
        api_thread.start()
        log.info(f"API démarrée sur Thread: {api_thread.name}")

    # Démarrer la boucle de trading
    try:
        main_trading_loop(config, connector, executor, journal)
    except KeyboardInterrupt:
        log.info("Arrêt manuel demandé (Ctrl+C).")
    except Exception as e:
        log.critical(f"Exception non gérée dans run_bot: {e}", exc_info=True)
    finally:
        connector.disconnect()
        log.info("Bot arrêté. Connexion MT5 fermée.")
        print("Bot déconnecté.")

if __name__ == "__main__":
    run_bot()
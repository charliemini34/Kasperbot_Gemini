import logging
import threading
import time
import yaml

from src.data_ingest.mt5_connector import MT5Connector
from src.execution.mt5_executor import MT5Executor
from src.strategy.smc_orchestrator import SMCOrchestrator
from src.risk.risk_manager import RiskManager
from src.journal.professional_journal import ProfessionalJournal
from src.analysis.performance_analyzer import PerformanceAnalyzer
from src.shared_state import SharedState
from src.api.server import start_api_server
from src.constants import LOG_LEVEL

# Configuration du logging de base (pour le fichier et la console)
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("trading_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

shared_state = SharedState()

def load_config(config_path='config.yaml'):
    """Charge la configuration depuis un fichier YAML."""
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
            logger.info("Configuration chargée avec succès.")
            return config
    except FileNotFoundError:
        logger.error(f"Erreur: Le fichier de configuration '{config_path}' n'a pas été trouvé.", exc_info=True)
        return None
    except yaml.YAMLError as e:
        logger.error(f"Erreur lors de la lecture du fichier YAML: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Une erreur inattendue est survenue lors du chargement de la configuration: {e}", exc_info=True)
        return None

def start_bot_thread(config):
    """Initialise et démarre le bot de trading dans un thread séparé."""
    try:
        
        # Nous passons l'objet 'config' entier.
        # L'initialisation se produit DANS le constructeur (__init__).
        connector = MT5Connector(config)
        
        # MODIFICATION : Suppression du bloc "if not connector.initialize():"
        # La méthode 'initialize' n'existe pas (AttributeError).
        # L'initialisation est gérée par __init__. Si elle échoue,
        # elle lèvera une exception que le 'except' ci-dessous attrapera.
        
        # if not connector.initialize():
        #     logger.critical("Échec de l'initialisation de MT5Connector. Le bot ne peut pas démarrer.")
        #     shared_state.add_log("Échec de l'initialisation de MT5Connector. Le bot ne peut pas démarrer.")
        #     return

        logger.info("MT5Connector initialisé avec succès.")
        shared_state.add_log("Bot: Connexion MT5 initialisée.")

        executor = MT5Executor(connector)
        risk_manager = RiskManager(config.get('risk_management', {}), shared_state)
        journal = ProfessionalJournal(shared_state)
        
        # Initialisation de l'orchestrateur avec toutes les dépendances
        orchestrator = SMCOrchestrator(
            connector=connector,
            executor=executor,
            risk_manager=risk_manager,
            journal=journal,
            shared_state=shared_state,
            config=config  # Passe la configuration complète
        )
        
        logger.info("SMCOrchestrator créé. Démarrage du bot...")
        shared_state.add_log("Bot: Orchestrateur démarré.")
        
        # Démarrage de l'orchestrateur dans son propre thread
        orchestrator.run()

    except KeyError as e:
        logger.error(f"Clé manquante dans la configuration: {e}. Vérifiez config.yaml.", exc_info=True)
        shared_state.add_log(f"Erreur de configuration: Clé manquante {e}")
    except Exception as e:
        logger.error(f"Erreur lors du démarrage du bot: {e}", exc_info=True)
        shared_state.add_log(f"Erreur critique au démarrage du bot: {e}")

def main():
    """Point d'entrée principal de l'application."""
    config = load_config()
    if config is None:
        logger.critical("Échec du chargement de la configuration. L'application va s'arrêter.")
        return

    # Passe la configuration chargée à shared_state
    shared_state.set_config(config)

    # Démarrer le bot dans un thread séparé
    # L'argument 'config' est passé au thread
    bot_thread = threading.Thread(target=start_bot_thread, args=(config,), daemon=True)
    bot_thread.start()
    
    logger.info("Thread du bot démarré.")
    logger.info("Démarrage du serveur WebUI (Flask)...")
    
    # Lancer le serveur Flask via sa fonction dédiée
    try:
        start_api_server(shared_state)
    except Exception as e:
        logger.error(f"Erreur lors du démarrage du serveur Flask/SocketIO: {e}", exc_info=True)

if __name__ == "__main__":
    main()
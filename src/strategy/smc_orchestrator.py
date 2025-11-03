#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Version: 0.1.1
Date: 2025-11-03
Auteur: (Votre Nom ou Pseudo)

Description:
Module Orchestrateur pour la stratégie SMC.
Contient la logique principale pour coordonner l'acquisition de données,
l'analyse de patterns et l'exécution des trades.
"""

# Version du module
__version__ = "0.1.1"

import pandas as pd
from src.strategy import smc_entry_logic
from src import shared_state
from src.journal.professional_journal import log_journal_entry

class SMCOrchestrator:
    """
    Orchestre le flux de travail complet de l'analyse SMC:
    1. Récupère les données
    2. Détecte les nouvelles bougies
    3. Lance l'analyse des patterns
    4. Trouve les opportunités d'entrée
    5. Lance l'exécution des trades
    """
    
    def __init__(self, config, mt5_connector, mt5_executor):
        """
        Initialise l'orchestrateur avec les composants nécessaires.
        
        Args:
            config (dict): Le dictionnaire de configuration chargé.
            mt5_connector (MT5Connector): L'instance du connecteur MT5.
            mt5_executor (MT5Executor): L'instance de l'exécuteur d'ordres MT5.
        """
        self.config = config
        self.mt5_connector = mt5_connector
        self.mt5_executor = mt5_executor
        
        # Paramètres de trading
        self.timeframe = config['trading']['timeframe']
        self.num_candles = config['trading']['num_candles']
        self.trade_enabled = config['trading']['trade_enabled']

    def check_for_smc_patterns(self, symbol):
        """
        Point d'entrée principal pour l'analyse SMC d'un symbole.
        Cette fonction est conçue pour être appelée dans une boucle.
        Elle implémente la logique pour n'analyser que les nouvelles bougies.
        """
        try:
            # 1. Obtenir les données de marché les plus récentes
            data = self.mt5_connector.get_market_data(
                symbol,
                self.timeframe,
                self.num_candles
            )
            
            if data is None or data.empty:
                print(f"[{symbol}] Aucune donnée reçue de MT5 (Données vides).")
                with shared_state.analysis_status_lock:
                    shared_state.analysis_status[symbol] = "Erreur Données"
                return

            # --- DÉBUT DE LA LOGIQUE DE NOUVELLE BOUGIE ---
            
            # Récupérer l'heure de la dernière bougie des données
            # .iloc[-1] signifie "la dernière ligne"
            latest_candle_time = data['time'].iloc[-1]
            
            # Vérifier si nous avons déjà analysé cette bougie
            with shared_state.last_analysis_time_lock:
                last_analyzed_time = shared_state.last_analysis_time.get(symbol)

                if last_analyzed_time is not None and latest_candle_time <= last_analyzed_time:
                    # Ce n'est pas une nouvelle bougie, ne rien faire.
                    # L'état 'analysis_status' reste ce qu'il était.
                    return
                
                # C'est une nouvelle bougie ! Mettre à jour l'heure d'analyse.
                print(f"[{symbol}] Nouvelle bougie détectée: {latest_candle_time}")
                shared_state.last_analysis_time[symbol] = latest_candle_time
                
                # Mettre à jour le statut pour l'interface
                with shared_state.analysis_status_lock:
                    shared_state.analysis_status[symbol] = "Analyse en cours..."

            # --- FIN DE LA LOGIQUE DE NOUVELLE BOUGIE ---

            # 2. Mettre à jour l'état partagé avec les nouvelles données
            with shared_state.symbol_data_lock:
                shared_state.symbol_data[symbol] = data

            # 3. Trouver des opportunités (Ici sera la logique SMC)
            # Pour l'instant, cela appelle les fonctions vides (placeholders)
            opportunity = smc_entry_logic.find_entry_opportunity(data)
            
            if opportunity:
                # Opportunité trouvée
                log_message = f"Opportunité SMC trouvée: {opportunity['type']}"
                print(f"[{symbol}] {log_message}")
                log_journal_entry(symbol, 'INFO', log_message, details=opportunity)
                
                with shared_state.analysis_status_lock:
                    shared_state.analysis_status[symbol] = f"Opportunité {opportunity['type']}"
                
                # 4. Exécuter le trade (si activé dans le config)
                if self.trade_enabled:
                    self.mt5_executor.place_trade(symbol, opportunity)
                else:
                    log_message = f"[{symbol}] Exécution de trade désactivée (trade_enabled: False)"
                    print(log_message)
                    log_journal_entry(symbol, 'INFO', log_message)
                    
            else:
                # Pas d'opportunité trouvée
                with shared_state.analysis_status_lock:
                    shared_state.analysis_status[symbol] = "Recherche (Pas de setup)"

        except Exception as e:
            error_message = f"Erreur dans l'orchestrateur SMC pour {symbol}: {e}"
            print(error_message)
            log_journal_entry(symbol, 'ERROR', error_message)
            try:
                with shared_state.analysis_status_lock:
                    shared_state.analysis_status[symbol] = f"Erreur Orchestrateur"
            except Exception as lock_e:
                print(f"Erreur critique de verrouillage: {lock_e}")

if __name__ == "__main__":
    # Ceci est pour des tests unitaires (si nécessaire)
    print("Test de l'Orchestrateur SMC (simulation)")
    # Simuler les dépendances
    class MockConnector:
        def get_market_data(self, symbol, timeframe, count):
            print(f"Simu: Récupération de {count} bougies pour {symbol}")
            # Simuler 2 bougies
            return pd.DataFrame({
                'time': [pd.Timestamp('2025-11-03 14:00:00'), pd.Timestamp('2025-11-03 14:01:00')],
                'open': [1.0, 1.1], 'high': [1.2, 1.2], 'low': [0.9, 1.0], 'close': [1.1, 1.15],
                'tick_volume': [100, 120]
            })

    class MockExecutor:
        def place_trade(self, symbol, opportunity):
            print(f"Simu: Exécution du trade pour {symbol}: {opportunity['type']}")

    mock_config = {
        'trading': {
            'timeframe': 'M1',
            'num_candles': 100,
            'trade_enabled': True
        }
    }
    
    # Initialiser l'état partagé (nécessaire pour le test)
    shared_state.initialize_state(['EURUSD'])

    # Instance de l'orchestrateur
    orchestrator = SMCOrchestrator(mock_config, MockConnector(), MockExecutor())
    
    # --- Test 1: Première analyse ---
    print("\n--- Test 1: Première analyse ---")
    orchestrator.check_for_smc_patterns('EURUSD')
    print(f"État après Test 1: {shared_state.last_analysis_time.get('EURUSD')}")

    # --- Test 2: Analyse de la même bougie ---
    print("\n--- Test 2: Analyse de la même bougie (devrait ne rien faire) ---")
    orchestrator.check_for_smc_patterns('EURUSD')
    
    # --- Test 3: Analyse d'une nouvelle bougie ---
    print("\n--- Test 3: Analyse d'une nouvelle bougie ---")
    # Simuler de nouvelles données
    class MockConnectorNew(MockConnector):
        def get_market_data(self, symbol, timeframe, count):
            return pd.DataFrame({
                'time': [pd.Timestamp('2025-11-03 14:01:00'), pd.Timestamp('2025-11-03 14:02:00')],
                'open': [1.1, 1.15], 'high': [1.2, 1.25], 'low': [1.0, 1.12], 'close': [1.15, 1.22],
                'tick_volume': [100, 130]
            })
    
    orchestrator.mt5_connector = MockConnectorNew()
    orchestrator.check_for_smc_patterns('EURUSD')
    print(f"État après Test 3: {shared_state.last_analysis_time.get('EURUSD')}")
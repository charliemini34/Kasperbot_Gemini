# Fichier: src/strategy/smc_orchestrator.py
"""
Orchestrateur de Stratégie SMC.

Ce module agit comme le "chef d'orchestre" du bot de trading.
Il est responsable de :
1. Gérer la boucle d'analyse principale.
2. Exécuter les stratégies temporelles (Modèle 3) aux heures définies.
3. Exécuter les stratégies continues (Modèle 1, Modèle 2) le reste du temps.
4. Gérer l'état (ex: "est déjà en position") pour éviter les trades multiples.
5. Transmettre les signaux validés au module d'exécution (mt5_executor).
6. Enregistrer les trades dans le journal (professional_journal).

Version: 1.2.1
"""

__version__ = "1.2.1"

import logging
import time
import pandas as pd
from datetime import datetime, time as datetime_time
import pytz
import re # --- Ajout v1.2.1 ---

# Importation des modules de l'application
from src.data_ingest import mt5_connector
from src.strategy import smc_entry_logic
from src.risk import risk_manager
from src.execution import mt5_executor
from src.journal import professional_journal
from src.shared_state import shared_log_queue

logger = logging.getLogger(__name__)

class SMCOrchestrator:
    """
    La classe principale qui orchestre l'analyse et l'exécution des trades.
    """
    def __init__(self, config: dict, state: dict):
        self.config = config
        self.state = state # État partagé (pour l'UI et le threading)
        self.symbol = config['trading']['symbol']
        self.running = False
        self.journal = professional_journal.ProfessionalJournal(config['journal']['filepath'])
        
        # --- Timeframes Modèles 1 & 2 ---
        self.htf_tf_str = config['strategy']['htf_timeframe']
        self.ltf_tf_str = config['strategy']['ltf_timeframe']
        self.htf_tf = mt5_connector.get_mt5_timeframe(self.htf_tf_str)
        self.ltf_tf = mt5_connector.get_mt5_timeframe(self.ltf_tf_str)
        
        # --- Configuration Modèle 3 (Opening Range) v1.2.0 ---
        strategy_cfg = config['strategy']
        self.model_3_enabled = strategy_cfg.get('model_3_enabled', False)
        self.model_3_range_tf_str = strategy_cfg.get('model_3_range_tf', 'M30')
        self.model_3_entry_tf_str = strategy_cfg.get('model_3_entry_tf', 'M5')
        
        self.model_3_range_tf = mt5_connector.get_mt5_timeframe(self.model_3_range_tf_str)
        self.model_3_entry_tf = mt5_connector.get_mt5_timeframe(self.model_3_entry_tf_str)
        
        # Heure de déclenchement (ex: "15:30:00" pour le range M30 15:00-15:30)
        self.model_3_trigger_time = datetime_time.fromisoformat(strategy_cfg.get('model_3_trigger_time', '15:30:00'))
        self.trading_timezone_str = strategy_cfg.get('session_timezone', 'Etc/UTC') # Doit correspondre à MT5
        
        try:
            self.trading_timezone = pytz.timezone(self.trading_timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Fuseau horaire '{self.trading_timezone_str}' inconnu. Utilisation de 'Etc/UTC'.")
            self.trading_timezone = pytz.timezone('Etc/UTC')

        self.last_model_3_check_date = None # Pour n'exécuter M3 qu'une fois/jour
        # --- Fin Ajouts v1.2.0 ---

        self.state['active_trade'] = None # Pour stocker les infos du trade en cours

    def start(self):
        """Démarre la boucle principale de l'orchestrateur."""
        self.running = True
        logger.info(f"Orchestrateur démarré pour {self.symbol} sur {self.htf_tf_str}/{self.ltf_tf_str}.")
        self.log_to_ui(f"Bot démarré. Symbole: {self.symbol}.")
        if self.model_3_enabled:
             self.log_to_ui(f"Modèle 3 (Opening Range) activé. Déclenchement: {self.model_3_trigger_time} {self.trading_timezone_str}")
        
        while self.running:
            try:
                # Vérifier si l'état global du bot est "running"
                if self.state.get('bot_status') != 'running':
                    time.sleep(1)
                    continue
                
                # Cœur de la logique
                self.run_analysis_cycle()
                
                # Attendre l'intervalle défini avant la prochaine analyse
                time.sleep(self.config['strategy']['analysis_interval_seconds'])
                
            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                logger.error(f"Erreur dans la boucle principale: {e}", exc_info=True)
                self.log_to_ui(f"Erreur: {e}")
                time.sleep(30) # Attendre en cas d'erreur grave

    def stop(self):
        """Arrête la boucle principale."""
        self.running = False
        logger.info("Orchestrateur arrêté.")
        self.log_to_ui("Bot arrêté.")

    def log_to_ui(self, message: str):
        """Envoie un message de log à la file d'attente de l'UI."""
        shared_log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    # --- RESTRUCTURATION v1.2.0 ---
    def run_analysis_cycle(self):
        """
        Exécute un cycle complet d'analyse et de prise de décision.
        Divisé pour gérer les stratégies temporelles (M3) et continues (M1/M2).
        """
        
        # --- 1. Gestion des trades existants (Priorité) ---
        if self.state.get('active_trade'):
            position_id = self.state['active_trade'].get('position_id')
            if not mt5_executor.is_position_open(position_id, self.config):
                self.log_to_ui(f"Trade {position_id} clôturé (SL/TP atteint).")
                self.state['active_trade'] = None
            else:
                logger.info(f"Trade {position_id} toujours actif. Pas de nouvelle analyse.")
                return

        # --- 2. Vérifier la Stratégie Temporelle (Modèle 3) ---
        if self.model_3_enabled:
            # Cette fonction vérifie l'heure et exécute M3 si nécessaire
            signal_m3 = self._run_model_3_analysis()
            if signal_m3[0]: # Si M3 a trouvé un signal
                # Récupérer les données LTF (M5) pour le prix d'entrée
                ltf_data = mt5_connector.get_market_data(self.symbol, self.model_3_entry_tf, 2)
                if ltf_data is None or ltf_data.empty:
                    logger.error("Impossible de récupérer les données M5 pour exécuter le signal M3.")
                    return
                self._process_signal(*signal_m3, entry_data=ltf_data)
                return # Un trade a été pris (ou tenté), on termine le cycle

        # --- 3. Vérifier les Stratégies Continues (Modèle 1 & 2) ---
        # (Seulement si M3 n'a rien fait et qu'aucun trade n'est actif)
        if not self.state.get('active_trade'):
            signal_m1_m2 = self._run_models_1_and_2_analysis()
            if signal_m1_m2[0]: # Si M1 ou M2 a trouvé un signal
                # Récupérer les données LTF (M15) pour le prix d'entrée
                ltf_data = mt5_connector.get_market_data(self.symbol, self.ltf_tf, 2)
                if ltf_data is None or ltf_data.empty:
                    logger.error("Impossible de récupérer les données LTF pour exécuter le signal M1/M2.")
                    return
                self._process_signal(*signal_m1_m2, entry_data=ltf_data)
                return # Un trade a été pris (ou tenté)

    # --- NOUVELLE FONCTION (HELPER) v1.2.0 ---
    # --- MODIFIÉE v1.2.1 ---
    def _process_signal(self, signal, reason, sl_price, tp_price, entry_data):
        """
        Fonction centralisée pour calculer le risque et exécuter un trade.
        """
        if not signal:
            return

        self.log_to_ui(f"SIGNAL TROUVÉ: {reason}")
        
        # --- Ajout v1.2.1: Extraire l'ID du modèle ---
        def _extract_model(reason_str):
            match = re.search(r'\[(M\d)\]', reason_str)
            if match:
                return match.group(1)
            return "UNKNOWN"
        model_id = _extract_model(reason)
        # --- Fin Ajout ---
        
        # Vérifier que le SL/TP est valide
        if not sl_price or not tp_price:
             logger.warning(f"Signal trouvé mais SL/TP invalide. SL={sl_price}, TP={tp_price}. Trade annulé.")
             self.log_to_ui("Signal ignoré: SL/TP invalide.")
             return

        entry_price = entry_data['close'].iloc[-1] # Entrée Market
        
        volume = risk_manager.calculate_position_size(
            account_balance=mt5_executor.get_account_balance(self.config),
            risk_per_trade_pct=self.config['risk']['risk_per_trade_percent'],
            sl_price=sl_price,
            entry_price=entry_price,
            symbol=self.symbol,
            config=self.config
        )
        
        if volume == 0:
            logger.warning("Calcul de volume = 0. Le trade est annulé (SL trop large?).")
            self.log_to_ui("Signal ignoré: Volume 0 (risque/SL invalide?).")
            return

        # Exécuter le trade
        try:
            trade_result = mt5_executor.execute_trade(
                symbol=self.symbol,
                trade_type=signal, # "BUY" ou "SELL"
                volume=volume,
                sl_price=sl_price,
                tp_price=tp_price,
                comment=f"[{model_id}] {reason}", # Ajout de l'ID au commentaire
                config=self.config
            )
            
            if trade_result and trade_result['retcode'] == 10009: # 10009 = Ordre exécuté
                position_id = trade_result['order']
                self.log_to_ui(f"TRADE EXÉCUTÉ: {signal} {volume} lots @ {trade_result['price']}, SL={sl_price}, TP={tp_price}. ID: {position_id}")
                
                trade_info = {
                    "timestamp": datetime.now(),
                    "symbol": self.symbol,
                    "type": signal,
                    "volume": volume,
                    "entry_price": trade_result['price'],
                    "sl": sl_price,
                    "tp": tp_price,
                    "reason": reason,
                    "setup_model": model_id, # --- Ajout v1.2.1 ---
                    "position_id": position_id,
                    "status": "OPEN"
                }
                self.state['active_trade'] = trade_info
                self.journal.record_trade(trade_info)
                
            else:
                error_comment = "Erreur MT5 inconnue"
                if trade_result:
                     error_comment = trade_result.get('comment', 'Erreur MT5')
                logger.error(f"Échec de l'exécution du trade: {trade_result}")
                self.log_to_ui(f"Échec exécution: {error_comment}")

        except Exception as e:
            logger.error(f"Erreur lors de l'exécution du trade: {e}", exc_info=True)
            self.log_to_ui(f"Erreur exécution: {e}")

    # --- NOUVELLE FONCTION v1.2.0 ---
    def _run_model_3_analysis(self):
        """
        Vérifie et exécute la stratégie Modèle 3 (Opening Range) si l'heure correspond.
        """
        current_time_utc = datetime.now(pytz.utc)
        current_time_local = current_time_utc.astimezone(self.trading_timezone)
        
        # Vérifier s'il est temps de déclencher ET si on ne l'a pas déjà fait aujourd'hui
        if (current_time_local.time() >= self.model_3_trigger_time and
            current_time_local.date() != self.last_model_3_check_date):
            
            logger.info(f"Déclenchement du Modèle 3 (Opening Range) à {current_time_local.strftime('%H:%M:%S')}")
            self.log_to_ui(f"Analyse Modèle 3 (Opening Range) {self.model_3_range_tf_str}/{self.model_3_entry_tf_str}...")
            
            # Marquer comme vérifié pour aujourd'hui
            self.last_model_3_check_date = current_time_local.date()
            
            try:
                # Récupérer les données spécifiques pour M3
                range_data = mt5_connector.get_market_data(
                    self.symbol, 
                    self.model_3_range_tf, 
                    10 # 10 dernières bougies M30
                )
                entry_data = mt5_connector.get_market_data(
                    self.symbol, 
                    self.model_3_entry_tf, 
                    50 # 50 dernières bougies M5
                )
                
                if range_data is None or entry_data is None or range_data.empty or entry_data.empty:
                    logger.warning("[M3] Données vides reçues, cycle M3 sauté.")
                    return None, None, None, None

                # Appeler la logique M3
                return smc_entry_logic.check_model_3_opening_range(
                    range_data,
                    entry_data,
                    self.config,
                    self.model_3_range_tf_str,
                    self.model_3_entry_tf_str
                )
                
            except Exception as e:
                logger.error(f"Erreur durant l'analyse Modèle 3: {e}", exc_info=True)
                self.log_to_ui(f"Erreur: Échec analyse M3.")

        return None, None, None, None # Pas le moment, ou déjà exécuté

    # --- NOUVELLE FONCTION v1.2.0 (anciennement run_analysis_cycle) ---
    def _run_models_1_and_2_analysis(self):
        """
        Exécute le cycle d'analyse continue pour les Modèles 1 et 2.
        """
        logger.info("Analyse SMC (Modèles 1 & 2) en cours...")
        try:
            htf_data = mt5_connector.get_market_data(
                self.symbol, 
                self.htf_tf, 
                self.config['strategy']['htf_lookback_candles']
            )
            ltf_data = mt5_connector.get_market_data(
                self.symbol, 
                self.ltf_tf, 
                self.config['strategy']['ltf_lookback_candles']
            )
            
            if htf_data is None or ltf_data is None or htf_data.empty or ltf_data.empty:
                logger.warning("[M1/M2] Données MTF vides reçues, cycle sauté.")
                return None, None, None, None

            mtf_data_dict = {
                self.htf_tf_str: htf_data,
                self.ltf_tf_str: ltf_data
            }
            
        except Exception as e:
            logger.error(f"Erreur de récupération des données M1/M2: {e}")
            self.log_to_ui("Erreur: Échec de la récupération des données MT5.")
            return None, None, None, None

        # Appel de la fonction maître qui teste M1 et M2
        signal, reason, sl_price, tp_price = smc_entry_logic.check_all_smc_signals(
            mtf_data_dict,
            self.config
        )

        if not signal:
             logger.info("Aucun signal SMC (M1/M2) trouvé.")
             
        return signal, reason, sl_price, tp_price
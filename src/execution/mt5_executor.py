# Fichier: src/execution/mt5_executor.py
# Version: 19.1.2 (Fix OSError 22)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants, src.journal.professional_journal, src.shared_state
# Description: Ajout de check_for_closed_trades et correction de l'OSError [Errno 22] en utilisant des timestamps.

import MetaTrader5 as mt5
import logging
import time
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta
from typing import Tuple, List, Dict, Optional, Any

from src.constants import BUY, SELL
from src.journal import professional_journal
from src.shared_state import TradeContext # (R7)

class MT5Executor:
    """
    Gère l'exécution (v19.1.0) des ordres sur MT5.
    Inclut la logique pour les ordres limites (R7) et la gestion de contexte (J.2).
    """

    def __init__(self, mt5_connection, config: dict):
        self.log = logging.getLogger(self.__class__.__name__)
        self._mt5 = mt5_connection
        self._config: Dict = config
        self._trade_context: Dict[int, TradeContext] = {} # (J.2) Contexte {ticket_id: TradeContext}
        self.log.info("MT5Executor (v19.1.2) initialisé.")

    def _retry_mt5_call(self, func, *args, **kwargs):
        """Tente d'exécuter un appel MT5 avec retries en cas de déconnexion."""
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    # Gérer les retcodes d'échec
                    if hasattr(result, 'retcode') and result.retcode != mt5.TRADE_RETCODE_DONE:
                        self.log.warning(f"Appel MT5 {func.__name__} a échoué (retcode {result.retcode}): {result.comment}")
                        # Pas de retry sur un échec logique (ex: fonds insuffisants), seulement sur None
                    return result
                
                # Si result est None, c'est souvent une déconnexion
                last_err = self._mt5.last_error()
                self.log.warning(f"Appel MT5 {func.__name__} a retourné None. Erreur: {last_err}. Tentative {i+1}/{retries}...")
                
            except Exception as e:
                # Gérer les exceptions (ex: connexion rompue)
                self.log.error(f"Exception durant appel MT5 {func.__name__}: {e}. Tentative {i+1}/{retries}...")
            
            time.sleep(delay * (i + 1)) # Backoff exponentiel simple
            
            # Tenter de rafraîchir la connexion (simpliste)
            if not self._mt5.version(): 
                self.log.error("Connexion MT5 perdue. Tentative de reconnexion implicite...")
                # La boucle principale gérera la reconnexion complète.

        self.log.error(f"Échec final de l'appel MT5 {func.__name__} après {retries} tentatives.")
        return None

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic, trade_signal):
        """
        Orchestre le calcul des paramètres et le placement de l'ordre limite (R7).
        """
        self.log.info(f"Tentative d'exécution du signal {pattern_name} sur {symbol}...")
        
        try:
            current_tick = self._get_symbol_info(symbol, tick=True)
            if not current_tick:
                self.log.error(f"Impossible d'exécuter {symbol}: Tick introuvable.")
                return

            # 1. Calculer les paramètres (Volume, Entrée, SL, TP) via RiskManager
            volume, entry_limit, sl_final, tp_final = risk_manager.calculate_trade_parameters(
                account_info.equity, current_tick, ohlc_data, trade_signal
            )

            # 2. Vérifier si le calcul a réussi (RiskManager log les raisons d'échec)
            if volume <= 0 or entry_limit <= 0 or sl_final <= 0 or tp_final <= 0:
                self.log.warning(f"Exécution annulée pour {symbol} (Paramètres invalides: V={volume}, E={entry_limit}, SL={sl_final}, TP={tp_final}).")
                return

            # 3. Définir le type d'ordre limite
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == BUY else mt5.ORDER_TYPE_SELL_LIMIT
            
            # (R7) Expiration de l'ordre
            cfg_trading = self._config.get('trading_settings', {})
            expiry_candles = cfg_trading.get('pending_order_expiry_candles', 5)
            timeframe_seconds = 60 * 15 # (Codé en dur M15, à améliorer)
            expiry_seconds = expiry_candles * timeframe_seconds
            expiry_time = int(datetime.now(pytz.utc).timestamp() + expiry_seconds)

            # 4. Construire la requête
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": float(volume),
                "type": order_type,
                "price": round(entry_limit, risk_manager.digits),
                "sl": round(sl_final, risk_manager.digits),
                "tp": round(tp_final, risk_manager.digits),
                "magic": magic,
                "comment": f"{pattern_name} (Kasperbot v19)",
                "type_time": mt5.ORDER_TIME_SPECIFIED, # (R7)
                "expiration": expiry_time, # (R7)
                "type_filling": mt5.ORDER_FILLING_FOK,
            }

            # 5. Envoyer l'ordre
            self.log.debug(f"Envoi de l'ordre limite {symbol}: {request}")
            result = self._retry_mt5_call(self._mt5.order_send, request)

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"ORDRE LIMITE {direction} PLACÉ: {symbol} @ {entry_limit:.{risk_manager.digits}f}, Vol={volume:.2f}, SL={sl_final:.{risk_manager.digits}f}, TP={tp_final:.{risk_manager.digits}f}. Ticket: {result.order}")
                
                # (J.2) Enregistrer le contexte du trade
                self._trade_context[result.order] = TradeContext(
                    ticket=result.order,
                    original_sl=sl_final,
                    original_volume=volume
                )
            else:
                err_code = result.retcode if result else "N/A"
                err_comm = result.comment if result else self._mt5.last_error()
                self.log.error(f"Échec placement ordre limite {symbol}. Code: {err_code}, Comment: {err_comm}")

        except Exception as e:
            self.log.error(f"Erreur majeure dans execute_trade pour {symbol}: {e}", exc_info=True)
            
    def close_partial_position(self, ticket, volume_to_close, magic, comment) -> bool:
        # ... (Logique inchangée) ...
        position = self._mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            self.log.error(f"TP Partiel: Impossible de trouver la position #{ticket}")
            return False
        
        pos = position[0]
        
        # S'assurer qu'on ne ferme pas plus que le volume restant
        volume = min(pos.volume, round(volume_to_close, 2))
        if volume <= 0: return False # Rien à fermer
        
        order_type = mt5.ORDER_TYPE_SELL if pos.type == BUY else mt5.ORDER_TYPE_BUY
        price = self._get_symbol_info(pos.symbol, tick=True)
        current_price = price.bid if pos.type == BUY else price.ask
        if not current_price:
             self.log.error(f"TP Partiel #{ticket}: Tick introuvable pour {pos.symbol}.")
             return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": float(volume),
            "type": order_type,
            "price": current_price,
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = self._retry_mt5_call(self._mt5.order_send, request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"TP Partiel #{ticket}: Clôturé {volume:.2f} lots sur {pos.symbol}.")
            return True
        else:
            self.log.error(f"TP Partiel #{ticket}: Échec. Code: {result.retcode if result else 'N/A'}, Comment: {result.comment if result else 'N/A'}")
            return False

    def modify_position(self, ticket, sl_price, tp_price) -> bool:
        # ... (Logique inchangée) ...
        position = self._mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            self.log.error(f"ModifyPosition: Position #{ticket} introuvable.")
            return False
        
        pos = position[0]
        
        # Arrondir aux digits du symbole
        try:
            info = self._get_symbol_info(pos.symbol, tick=False)
            digits = info.digits
            sl_price = round(sl_price, digits)
            tp_price = round(tp_price, digits)
        except Exception as e:
            self.log.error(f"ModifyPosition #{ticket}: Erreur récupération digits: {e}. Utilise SL/TP bruts.")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": float(sl_price),
            "tp": float(tp_price),
            "comment": "Kasperbot Mgmt",
        }

        result = self._retry_mt5_call(self._mt5.order_send, request)

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.debug(f"ModifyPosition #{ticket}: SL/TP mis à jour.")
            return True
        else:
            self.log.warning(f"ModifyPosition #{ticket}: Échec. Code: {result.retcode if result else 'N/A'}, Comment: {result.comment if result else 'N/A'}")
            return False

    def cancel_order(self, ticket) -> bool:
        # ... (Logique inchangée) ...
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
            "comment": "Kasperbot Expired"
        }
        result = self._retry_mt5_call(self._mt5.order_send, request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre #{ticket} annulé avec succès.")
            return True
        else:
            self.log.warning(f"Annulation Ordre #{ticket}: Échec. Code: {result.retcode if result else 'N/A'}, Comment: {result.comment if result else 'N/A'}")
            return False

    # --- Fonctions de récupération (inchangées) ---
    
    def get_open_positions(self, magic=0) -> list:
        try:
            positions = self._mt5.positions_get()
            if positions is None:
                self.log.error(f"get_open_positions: échec, code = {self._mt5.last_error()}")
                return []
            
            if magic == 0:
                return list(positions)
            
            return [pos for pos in positions if pos.magic == magic]
        except Exception as e:
            self.log.error(f"Erreur get_open_positions: {e}", exc_info=True)
            return []

    def get_pending_orders(self, magic=0) -> list:
        try:
            orders = self._mt5.orders_get()
            if orders is None:
                self.log.error(f"get_pending_orders: échec, code = {self._mt5.last_error()}")
                return []
            
            if magic == 0:
                return list(orders)
            
            return [order for order in orders if order.magic == magic]
        except Exception as e:
            self.log.error(f"Erreur get_pending_orders: {e}", exc_info=True)
            return []
            
    def get_account_info(self):
        return self._retry_mt5_call(self._mt5.account_info)

    def _get_symbol_info(self, symbol, tick=False):
        func = self._mt5.symbol_info_tick if tick else self._mt5.symbol_info
        return self._retry_mt5_call(func, symbol)

    # --- Gestion du contexte (J.2) ---
    
    def update_context_for_new_positions(self, open_positions: list):
        # ... (Logique inchangée) ...
        for pos in open_positions:
            if pos.ticket not in self._trade_context:
                # C'est une nouvelle position (probablement un ordre limite exécuté)
                self.log.info(f"Nouvelle position #{pos.ticket} détectée. Création du contexte.")
                self._trade_context[pos.ticket] = TradeContext(
                    ticket=pos.ticket,
                    original_sl=pos.sl,
                    original_volume=pos.volume
                )
    
    def update_trade_context_partials(self, ticket: int, percent_closed: float):
        """ (J.7) Met à jour le contexte après un TP partiel. """
        if ticket in self._trade_context:
            self._trade_context[ticket].partial_tp_taken_percent += percent_closed
        else:
            self.log.warning(f"Contexte TP Partiel: Ticket #{ticket} introuvable pour mise à jour.")

    # --- NOUVELLE FONCTION (Ajoutée et Corrigée) ---
    
    def check_for_closed_trades(self, magic: int, last_check_timestamp: int) -> int:
        """
        Vérifie les deals fermés depuis le dernier check (J.6).
        Version 19.1.2: Corrigé pour utiliser des timestamps (int) pour corriger l'OSError [Errno 22].
        """
        self.log.debug(f"Vérification des trades fermés depuis timestamp {last_check_timestamp}")
        
        # Utiliser des timestamps (entiers)
        current_check_timestamp = int(datetime.now(pytz.utc).timestamp())
        start_timestamp = 0

        try:
            if last_check_timestamp == 0:
                # Si premier check, prendre les 24 dernières heures
                start_timestamp = current_check_timestamp - (24 * 3600)
                self.log.info("Premier check des trades fermés (24h).")
            else:
                start_timestamp = last_check_timestamp

            # --- CORRECTION (OSError 22) ---
            # Passer des ENTIERS (timestamps) au lieu d'objets datetime
            deals = self._mt5.history_deals_get(start_timestamp, current_check_timestamp)
            # --- FIN CORRECTION ---
            
            if deals is None:
                self.log.error(f"Erreur check_for_closed_trades (history_deals_get): {self._mt5.last_error()}")
                return current_check_timestamp # Retourner le temps actuel

            closed_positions_processed = set()
            
            # Regrouper les deals par ID de position
            deals_by_position: Dict[int, List[Any]] = {}
            for deal in deals:
                if deal.magic != magic or deal.position_id == 0:
                    continue
                if deal.position_id not in deals_by_position:
                    deals_by_position[deal.position_id] = []
                deals_by_position[deal.position_id].append(deal)

            # Analyser les deals par position
            for position_id, pos_deals in deals_by_position.items():
                # Un trade est "fermé" s'il contient un deal de sortie
                is_closed = any(d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT for d in pos_deals)
                
                if not is_closed:
                    continue
                    
                # Vérifier si ce trade (identifié par son position_id) a déjà été journalisé
                if professional_journal.is_trade_logged(position_id):
                    continue

                # C'est un nouveau trade fermé -> Journaliser
                professional_journal.log_closed_trade(position_id, pos_deals, self._config)
                closed_positions_processed.add(position_id)

            if closed_positions_processed:
                self.log.info(f"{len(closed_positions_processed)} nouveau(x) trade(s) fermé(s) journalisé(s).")
            
            return current_check_timestamp

        except Exception as e:
            self.log.error(f"Erreur majeure dans check_for_closed_trades: {e}", exc_info=True)
            # Retourner le temps actuel pour éviter de re-scanner en boucle
            return current_check_timestamp
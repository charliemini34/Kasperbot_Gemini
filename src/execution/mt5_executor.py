# src/execution/mt5_executor.py
# Fichier: src/execution/mt5_executor.py
# Version: 20.0.0 (SMC Fusion)
# Description: Fusion de la v19.1.2 avec la nouvelle logique d'exécution SMC.

import MetaTrader5 as mt5
import logging
import time
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta
from typing import Tuple, List, Dict, Optional, Any

# Doit correspondre à constants.py
BUY = 0
SELL = 1

# Doit correspondre à shared_state.py
class TradeContext:
    def __init__(self, ticket, original_sl, original_volume):
        self.ticket = ticket
        self.original_sl = original_sl
        self.original_volume = original_volume
        self.partial_tp_taken_percent = 0.0

class MT5Executor:
    """
    Gère l'exécution (v20.0.0) des ordres sur MT5.
    Fusion de la v19.1.2 avec la nouvelle logique d'exécution SMC.
    """

    def __init__(self, mt5_connection, config: dict, shared_state):
        # CORRECTION: Signature corrigée pour main.py
        self.log = logging.getLogger(self.__class__.__name__)
        self._mt5 = mt5_connection
        self._config: Dict = config
        self._shared_state = shared_state # NÉCESSAIRE pour la logique SMC
        
        # Contexte (de votre v19.1.2)
        self._trade_context: Dict[int, TradeContext] = {} 
        self.log.info("MT5Executor (v20.0.0 SMC Fusion) initialisé.")
        
        # Variable pour le journal (de v19.1.2)
        # Supposant que le journal est géré par la classe ProfessionalJournal
        # Si 'professional_journal' est un module, l'import doit être en haut
        # from src.journal import professional_journal
        # Cette partie est gérée par main.py maintenant, cet executor n'a pas besoin de le savoir.


    def _retry_mt5_call(self, func, *args, **kwargs):
        """Tente d'exécuter un appel MT5 avec retries en cas de déconnexion."""
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    if hasattr(result, 'retcode') and result.retcode != mt5.TRADE_RETCODE_DONE:
                        self.log.warning(f"Appel MT5 {func.__name__} a échoué (retcode {result.retcode}): {result.comment}")
                    return result
                
                last_err = self._mt5.last_error()
                self.log.warning(f"Appel MT5 {func.__name__} a retourné None. Erreur: {last_err}. Tentative {i+1}/{retries}...")
                
            except Exception as e:
                self.log.error(f"Exception durant appel MT5 {func.__name__}: {e}. Tentative {i+1}/{retries}...")
            
            time.sleep(delay * (i + 1))
            
            if not self._mt5.version(): 
                self.log.error("Connexion MT5 perdue. Tentative de reconnexion implicite...")

        self.log.error(f"Échec final de l'appel MT5 {func.__name__} après {retries} tentatives.")
        return None

    # --- NOUVELLE FONCTION D'EXÉCUTION (SMC) ---
    def execute_trade(self, symbol, lot_size, trade_type, entry_price, stop_loss, take_profit, comment):
        """
        Exécute un trade au MARCHÉ (utilisé par SMCEntryLogic v20).
        """
        self.log.info(f"Tentative d'exécution (SMC) {trade_type} sur {symbol}...")
        
        try:
            # S'assurer que les infos sont valides
            info_symbol = self.get_symbol_info(symbol)
            if not info_symbol:
                self.log.error(f"Échec exécution SMC: Infos symbole {symbol} introuvables.")
                return None

            if trade_type == BUY:
                order_type = mt5.ORDER_TYPE_BUY
                price = self._mt5.symbol_info_tick(symbol).ask
            elif trade_type == SELL:
                order_type = mt5.ORDER_TYPE_SELL
                price = self._mt5.symbol_info_tick(symbol).bid
            else:
                self.log.error(f"Type de trade inconnu: {trade_type}")
                return None

            # Arrondir SL/TP aux digits du symbole
            stop_loss = round(stop_loss, info_symbol.digits)
            take_profit = round(take_profit, info_symbol.digits)
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lot_size),
                "type": order_type,
                "price": price,
                "sl": stop_loss,
                "tp": take_profit,
                "deviation": 20, # 20 points de déviation autorisée
                "magic": self._config.get('trading_settings', {}).get('magic_number', 0),
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_FOK, # Fill or Kill
            }

            # 5. Envoyer l'ordre
            self.log.debug(f"Envoi de l'ordre Marché (SMC) {symbol}: {request}")
            result = self._retry_mt5_call(self._mt5.order_send, request)

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"ORDRE MARCHÉ (SMC) {trade_type} PLACÉ: {symbol} @ {price}, Vol={lot_size:.2f}, SL={stop_loss}, TP={take_profit}. Ticket: {result.order}")
                
                # (J.2) Enregistrer le contexte du trade
                # (Note: SMCEntryLogic gère le journal, mais l'executor doit gérer le contexte pour BE/TSL)
                self._shared_state.set_trade_context(
                    result.order, 
                    TradeContext(
                        ticket=result.order,
                        original_sl=stop_loss,
                        original_volume=lot_size
                    )
                )
                return {"request_id": result.request_id, "order_id": result.order}
            else:
                err_code = result.retcode if result else "N/A"
                err_comm = result.comment if result else self._mt5.last_error()
                self.log.error(f"Échec placement ordre Marché (SMC) {symbol}. Code: {err_code}, Comment: {err_comm}")
                return None

        except Exception as e:
            self.log.error(f"Erreur majeure dans execute_trade (SMC) pour {symbol}: {e}", exc_info=True)
            return None

    # --- FONCTIONS CONSERVÉES (de v19.1.2) ---
            
    def close_partial_position(self, ticket, volume_to_close, magic, comment) -> bool:
        position = self._mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            self.log.error(f"TP Partiel: Impossible de trouver la position #{ticket}")
            return False
        
        pos = position[0]
        volume = min(pos.volume, round(volume_to_close, 2))
        if volume <= 0: return False
        
        order_type = mt5.ORDER_TYPE_SELL if pos.type == BUY else mt5.ORDER_TYPE_BUY
        price_info = self.get_symbol_info(pos.symbol, tick=True)
        current_price = price_info.bid if pos.type == BUY else price_info.ask
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
        position = self._mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            self.log.error(f"ModifyPosition: Position #{ticket} introuvable.")
            return False
        
        pos = position[0]
        
        try:
            info = self.get_symbol_info(pos.symbol, tick=False)
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
    
    def get_open_positions(self, magic=0) -> list:
        try:
            # Si magic=0 (défaut), récupérer toutes les positions
            if magic == 0:
                 positions = self._mt5.positions_get()
            else:
                 positions = self._mt5.positions_get(magic=magic)
                 
            if positions is None:
                self.log.error(f"get_open_positions: échec, code = {self._mt5.last_error()}")
                return []
            
            return list(positions)
        except Exception as e:
            self.log.error(f"Erreur get_open_positions: {e}", exc_info=True)
            return []

    def get_pending_orders(self, magic=0) -> list:
        try:
            if magic == 0:
                orders = self._mt5.orders_get()
            else:
                orders = self._mt5.orders_get(magic=magic)
                
            if orders is None:
                self.log.error(f"get_pending_orders: échec, code = {self._mt5.last_error()}")
                return []
            
            return list(orders)
        except Exception as e:
            self.log.error(f"Erreur get_pending_orders: {e}", exc_info=True)
            return []
            
    def get_account_info(self):
        return self._retry_mt5_call(self._mt5.account_info)

    def get_symbol_info(self, symbol, tick=False):
        func = self._mt5.symbol_info_tick if tick else self._mt5.symbol_info
        return self._retry_mt5_call(func, symbol)
        
    # Wrapper (pour la compatibilité ascendante avec RM)
    def _get_symbol_info(self, symbol, tick=False):
        return self.get_symbol_info(symbol, tick)

    def update_context_for_new_positions(self, open_positions: list):
        for pos in open_positions:
            if not self._shared_state.get_trade_context(pos.ticket):
                self.log.info(f"Nouvelle position #{pos.ticket} détectée. Création du contexte.")
                self._shared_state.set_trade_context(
                    pos.ticket,
                    TradeContext(
                        ticket=pos.ticket,
                        original_sl=pos.sl,
                        original_volume=pos.volume
                    )
                )
    
    def update_trade_context_partials(self, ticket: int, percent_closed: float):
        ctx = self._shared_state.get_trade_context(ticket)
        if ctx:
            ctx.partial_tp_taken_percent += percent_closed
            self._shared_state.set_trade_context(ticket, ctx)
        else:
            self.log.warning(f"Contexte TP Partiel: Ticket #{ticket} introuvable pour mise à jour.")

    # Renommé depuis check_for_closed_trades pour correspondre à l'appel de main.py
    def check_closed_positions_pnl(self, last_check_timestamp: int = 0):
        """
        Vérifie les deals fermés depuis le dernier check (J.6).
        """
        magic = self._config.get('trading_settings', {}).get('magic_number', 0)
        self.log.debug(f"Vérification des trades fermés (magic {magic}) depuis timestamp {last_check_timestamp}")
        
        current_check_timestamp_int = int(datetime.now(pytz.utc).timestamp())
        start_timestamp_int = 0

        try:
            if last_check_timestamp == 0:
                start_timestamp_int = current_check_timestamp_int - (24 * 3600)
                self.log.info("Premier check des trades fermés (24h).")
            else:
                start_timestamp_int = last_check_timestamp + 1
            
            deals = self._mt5.history_deals_get(start_timestamp_int, current_check_timestamp_int)
            
            if deals is None:
                self.log.error(f"Erreur check_closed_positions_pnl (history_deals_get): {self._mt5.last_error()}")
                return last_check_timestamp 

            closed_positions_processed = set()
            deals_by_position: Dict[int, List[Any]] = {}
            
            for deal in deals:
                if deal.magic != magic or deal.position_id == 0:
                    continue
                if deal.position_id not in deals_by_position:
                    deals_by_position[deal.position_id] = []
                deals_by_position[deal.position_id].append(deal)

            for position_id, pos_deals in deals_by_position.items():
                is_closed = any(d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT for d in pos_deals)
                if not is_closed:
                    continue
                
                # Le journal gère lui-même s'il est loggué ou non
                # from src.journal import professional_journal
                # professional_journal.log_closed_trade(position_id, pos_deals, self._config)
                # Note: La journalisation est maintenant gérée par la classe Journal
                # Cet executor ne devrait pas appeler le journal directement.
                # C'est 'main.py' qui passe le journal à l'EntryLogic.
                
                closed_positions_processed.add(position_id)

            if closed_positions_processed:
                self.log.info(f"{len(closed_positions_processed)} nouveau(x) trade(s) fermé(s) détecté(s) (non journalisé par l'executor).")
            
            return current_check_timestamp_int

        except Exception as e:
            self.log.error(f"Erreur majeure dans check_closed_positions_pnl: {e}", exc_info=True)
            return last_check_timestamp